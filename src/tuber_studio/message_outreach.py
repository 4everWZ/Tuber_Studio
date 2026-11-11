"""One source-bound check before a new, invited private conversation starts."""

import json
import re
from typing import Literal

from pydantic import Field

from .models import StrictModel
from .operation_continuations import content_hash


class OutreachAssessment(StrictModel):
    action: Literal["send", "stay_public", "wait", "duplicate"]
    reason: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    invitation_kind: Literal["invitation", "agreement", "none"]
    invitation_comment_id: str
    invitation_quote: str
    invited_actor_uid: str
    same_matter_draft_id: int | None = None
    promise_fulfilled: bool = False
    new_matter: bool = False
    # Legacy storage name for outgoing payload readiness.
    body_addresses_purpose: bool


def outreach_sources(thread):
    """Keep source hashes unchanged; participant information lives beside them."""
    if not isinstance(thread, dict):
        return []
    rows = []
    addressing = thread.get("addressing") or {}
    for row in [thread.get("source"), thread.get("root"), thread.get("parent"), *thread.get("timeline", []), *thread.get("items", []), *thread.get("grouped_comments", [])]:
        if not isinstance(row, dict) or not row.get("comment_id") or not (row.get("text") or row.get("content")):
            continue
        value = dict(row) | {"comment_id": str(row["comment_id"]),
            "text": row.get("text") or row.get("content"),
            "author_uid": str(row.get("author_uid") or row.get("platform_user_id") or row.get("user_id") or ""),
            "_parent_known": "parent_comment_id" in row and row.get("parent_verified", True),
            "_mentions_known": "mentions" in row}
        if str(value["comment_id"]) == str((thread.get("source") or {}).get("comment_id")):
            value["mentions"] = addressing.get("source_mentions", value.get("mentions", []))
            value["_mentions_known"] = "source_mentions" in addressing or value["_mentions_known"]
            if "source_parent_comment_id" in addressing:
                value["parent_comment_id"] = addressing["source_parent_comment_id"]
                value["_parent_known"] = addressing.get("source_parent_verified", True)
        previous = next((previous for previous in rows if previous["comment_id"] == value["comment_id"]), None)
        if previous is None:
            rows.append(value)
        elif not previous["_parent_known"] and value["_parent_known"]:
            previous["parent_comment_id"], previous["_parent_known"] = value.get("parent_comment_id"), True
        if previous is not None and not previous["_mentions_known"] and value["_mentions_known"]:
            previous["mentions"], previous["_mentions_known"] = value.get("mentions", []), True
    return rows


def _source_addressing(source, sources, actor_uid, owner_uid):
    mentions = source.get("mentions") or []
    known = {str(person.get("uid")) for person in mentions if person.get("uid") not in (None, "", "0", 0)}
    if actor_uid in known:
        return "possible"
    if mentions:
        return "unresolved" if any(person.get("uid") in (None, "", "0", 0) for person in mentions) else "impossible"
    if not source["_parent_known"] or ("@" in source["text"] or "＠" in source["text"]):
        return "unresolved"
    parent_id = source.get("parent_comment_id")
    if parent_id not in (None, "", "0", 0):
        parent = next((row for row in sources if row["comment_id"] == str(parent_id)), {})
        uid = str(parent.get("author_uid") or "")
        return "possible" if uid == actor_uid else "impossible" if uid else "unresolved"
    if source["_parent_known"] and source["_mentions_known"] and owner_uid:
        return "possible" if owner_uid == actor_uid else "impossible"
    return "unresolved"


def invitation_possibility(thread, actor_uid, recipient_uid):
    """Necessary identity relation only; `possible` is not an invitation verdict."""
    if not isinstance(thread, dict) or not actor_uid or not recipient_uid:
        return "unresolved"
    sources = outreach_sources(thread)
    candidates = [row for row in sources if row["author_uid"] == str(recipient_uid)]
    if not candidates:
        return "impossible" if sources and all(row["author_uid"] for row in sources) else "unresolved"
    owner_uid = str(thread.get("resource_author_uid") or "")
    states = {_source_addressing(row, sources, str(actor_uid), owner_uid) for row in candidates}
    return "possible" if "possible" in states else "unresolved" if "unresolved" in states else "impossible"


def confirmed_invitation(thread, actor_uid, recipient_uid):
    """Bind a direct request for private contact to its actual author and addressee."""
    if not isinstance(thread, dict) or not actor_uid or not recipient_uid:
        return None
    actor_uid, recipient_uid = str(actor_uid), str(recipient_uid)
    sources = outreach_sources(thread)
    by_id = {source["comment_id"]: source for source in sources}
    # Historical dialogue supplies participants. Only this pending comment group
    # may authorize a new contact, with the newest request taking precedence.
    pending = [thread.get("source"), *reversed(thread.get("grouped_comments") or [])]
    for item in pending:
        source = by_id.get(str((item or {}).get("comment_id")))
        if source is None:
            continue
        if source["author_uid"] != recipient_uid or actor_uid == recipient_uid:
            continue
        if _source_addressing(source, sources, actor_uid, str(thread.get("resource_author_uid") or "")) != "possible":
            continue
        mentions = source.get("mentions") or []
        if mentions and {str(person.get("uid") or "") for person in mentions} != {actor_uid}:
            continue
        text = source["text"]
        plain = re.sub(r'''“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"]*"|'[^']*'|\x60[^\x60]*\x60''', "", text)
        clauses = re.split(r"[。！？!?；;\n]", plain)
        requests = []
        for clause in clauses:
            if re.search(r"(?:不用|不要|不必|不想|(?<!能)不能|别|无需|禁止|拒绝).{0,12}(?:私信|私聊|私下|[dD][mM])", clause):
                return None
            if re.search(r"我(?:会|想|去|来|先|再|可以|给你|要)?(?:给你发)?私信你|我.{0,4}私聊你", clause):
                continue
            requested = (
                re.search(r"(?:私信|私聊).{0,18}(?:我|继续聊|聊聊|聊一下|联系)", clause)
                or re.search(r"(?:给我|发我|咱们|我们|这个|这件事|这事).{0,30}(?:私信|私聊|私下聊|私下说)", clause)
                or re.search(r"(?:可以|能不能|方便)(?:给我|跟我|和我)?(?:私信|私聊)(?:一下)?[吗么]?\s*$", clause)
                or re.fullmatch(r"\s*(?:私聊|私下聊)(?:吧|吗|么|一下)?\s*", clause)
                or re.search(r"\b(?:dm|pm)\s+me\b|send\s+me.{0,50}privately", clause, re.IGNORECASE)
            )
            if requested:
                requests.append(clause.strip())
        if not requests:
            continue
        quote = requests[0]
        conditional = re.search(r"(?:查到|找到|有结果|有消息|弄清|搞清|确认好|做好|完成|弄好|办好).{0,24}(?:私信|私聊|私下)", quote)
        return {
            "comment_id": source["comment_id"], "quote": quote, "source_text": text,
            "author_uid": recipient_uid, "author_name": source.get("author_name") or source.get("display_name") or "",
            "actor_uid": actor_uid, "kind": "agreement" if conditional else "invitation",
        }
    return None


def invitation_anchor(assessment, draft, thread):
    """A proposed invitation must be bound even when its decision is to wait."""
    actor = str(draft["account_id"])
    if not actor or assessment.invited_actor_uid != actor or assessment.invitation_kind == "none":
        return None
    sources = outreach_sources(thread)
    source = next((r for r in sources if str(r["comment_id"]) == assessment.invitation_comment_id), None)
    if (
        not source or str(source.get("author_uid")) != str(draft["platform_user_id"])
        or not assessment.invitation_quote.strip() or assessment.invitation_quote not in source["text"]
    ):
        return None
    if _source_addressing(source, sources, actor, str(thread.get("resource_author_uid") or "")) != "possible":
        return None
    return source


def verified_agreement(draft, thread):
    check = draft.get("outreach_check") or {}
    check = json.loads(check) if isinstance(check, str) else check
    if check.get("invitation_verified") is not True:
        return False
    try:
        assessment = OutreachAssessment.model_validate(check.get("assessment"))
    except ValueError:
        return False
    return assessment.invitation_kind == "agreement" and invitation_anchor(assessment, draft, thread) is not None


def validate_outreach(assessment, draft, thread, history, completion_evidence):
    """Validate real invitation identities, quoted evidence and prior delivery."""
    if assessment.action == "stay_public":
        return "stay_public"
    if assessment.action == "duplicate":
        return "duplicate" if any(row["id"] == assessment.same_matter_draft_id for row in history) else "stay_public"
    if invitation_anchor(assessment, draft, thread) is None:
        return "stay_public"
    if assessment.action == "wait":
        return "wait"
    if not assessment.body_addresses_purpose:
        return "wait"
    if assessment.invitation_kind == "agreement" and not (assessment.promise_fulfilled and completion_evidence):
        return "wait"
    if assessment.same_matter_draft_id is not None:
        return "duplicate" if any(row["id"] == assessment.same_matter_draft_id for row in history) else "wait"
    if history and (not assessment.new_matter or any(row.get("purpose", "").strip() == assessment.purpose.strip() for row in history)):
        return "wait"
    return "send"


async def assess_outreach(runtime, draft, event, history, completion_evidence):
    """Confirm the source and delivery state without another model decision."""
    invitation = confirmed_invitation(
        event.get("public_source_thread"), draft.get("account_id"), draft.get("platform_user_id")
    )
    assessment = OutreachAssessment(
        action="send" if invitation else "stay_public",
        reason="已确认对当前账号的私信邀请" if invitation else "原评论未确认对当前账号的私信邀请",
        purpose=invitation["quote"] if invitation else draft.get("purpose") or "继续公开交流",
        invitation_kind=invitation["kind"] if invitation else "none",
        invitation_comment_id=invitation["comment_id"] if invitation else "",
        invitation_quote=invitation["quote"] if invitation else "",
        invited_actor_uid=invitation["actor_uid"] if invitation else "",
        promise_fulfilled=bool(completion_evidence),
        new_matter=bool(invitation),
        body_addresses_purpose=bool((draft.get("text") or "").strip() or draft.get("outgoing_image")),
    )
    return assessment, validate_outreach(
        assessment, draft, event.get("public_source_thread"), history, completion_evidence
    )


def invitation_key(draft, assessment):
    return content_hash({
        "account_id": draft["account_id"], "character_id": draft["character_id"],
        "platform": draft["platform"], "recipient_uid": draft["platform_user_id"],
        "invitation_comment_id": assessment.invitation_comment_id,
    })


def contact_state(draft):
    if draft["status"] != "waiting":
        return draft["status"]
    check = draft.get("outreach_check") or {}
    check = json.loads(check) if isinstance(check, str) else check
    return check.get("state") or "waiting"
