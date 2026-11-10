"""One source-bound check before a new, invited private conversation starts."""

import json
from typing import Literal

from pydantic import Field

from .generation_meter import budgeted_json
from .models import StrictModel
from .operation_continuations import content_hash
from .operation_identity import identity_messages, runtime_identity


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
    body_addresses_purpose: bool


def outreach_sources(thread):
    """Keep source hashes unchanged; participant information lives beside them."""
    if not isinstance(thread, dict):
        return []
    rows = []
    addressing = thread.get("addressing") or {}
    for row in [thread.get("source"), thread.get("root"), thread.get("parent"), *thread.get("timeline", []), *thread.get("items", [])]:
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
    """Incoming-message replies never call this function."""
    thread = event.get("public_source_thread")
    review_thread = (
        {key: thread[key] for key in ("source", "root", "parent", "timeline", "addressing", "resource_author_uid") if key in thread}
        if isinstance(thread, dict) else thread
    )
    if isinstance(review_thread, dict):
        sources = outreach_sources(thread)
        candidates = [row for row in sources if row["author_uid"] == str(draft["platform_user_id"])]
        parent_ids = {str(row.get("parent_comment_id")) for row in candidates if row.get("parent_comment_id")}
        # Match the validator's full supplied candidate set, including invitations
        # beyond the presentation's short timeline, without adding private history.
        review_thread["items"] = [{key: row[key] for key in ("comment_id", "author_uid", "author_name", "text", "parent_comment_id", "mentions") if key in row}
            for row in sources if row in candidates or row["comment_id"] in parent_ids]
    data = {
        "self_account_uid": draft["account_id"], "recipient_uid": draft["platform_user_id"],
        "public_thread": review_thread, "purpose": draft.get("purpose") or "",
        "outgoing_text": draft["text"] if draft.get("message_kind", "text") != "image" else None,
        "outgoing_image": draft.get("outgoing_image"), "previous_unanswered_contacts": history,
        "completion_evidence": completion_evidence,
    }
    messages = [
        {"role": "system", "content": (
            "核对一条准备首次发出的主动私信。只在真实来源中对方邀请当前账号私聊，或有真实约定且事项已完成时send。"
            "邀请对象按真实UID、@与回复关系判断，@别人或只是有趣、想长聊、曾聊过不能算邀请。"
            "purpose说明具体要交流什么，invitation_quote逐字引用实际邀请/约定原文，invited_actor_uid是被邀请账号。"
            "正文应接该具体事项，body_addresses_purpose明确核对实际将发送的内容是否回应目的。与尚未获回复的已发联系属于同件事时duplicate并给出原draft id，不能换说法追问；"
            "实际发送图片时核对outgoing_image，不把未发送的候选文字当作正文。"
            "无法确认新事由就wait或stay_public，只有真实新的邀请和不同事项才new_matter=true。约定完成须有真实完成证据，不以台词自称完成作证。"
            "真实约定尚未完成时，invitation_kind=agreement且action=wait，也必须指出实际邀请原文和受邀UID；没有真实约定用stay_public，不能把普通评论存成待完成任务。"
        )},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    messages = identity_messages(messages, runtime_identity(runtime, runtime.store.character(draft["character_id"], draft["character_version"]), draft["account_id"]))
    value = await budgeted_json(runtime.llm, messages, OutreachAssessment.model_json_schema(), max_tokens=1024)
    assessment = OutreachAssessment.model_validate(value)
    disposition = validate_outreach(assessment, draft, thread, history, completion_evidence)
    return assessment, disposition


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
