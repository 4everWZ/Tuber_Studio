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
    for row in [thread.get("source"), thread.get("root"), thread.get("parent"), *thread.get("timeline", [])]:
        if not isinstance(row, dict) or not row.get("comment_id") or not row.get("text"):
            continue
        value = dict(row)
        if str(value["comment_id"]) == str((thread.get("source") or {}).get("comment_id")):
            value["mentions"] = thread.get("addressing", {}).get("source_mentions", value.get("mentions", []))
            value["parent_comment_id"] = thread.get("addressing", {}).get("source_parent_comment_id")
        if not any(str(previous["comment_id"]) == str(value["comment_id"]) for previous in rows):
            rows.append(value)
    return rows


def validate_outreach(assessment, draft, thread, history, completion_evidence):
    """Validate real invitation identities, quoted evidence and prior delivery."""
    if assessment.action != "send":
        return assessment.action
    actor = str(draft["account_id"])
    if not actor or assessment.invited_actor_uid != actor or assessment.invitation_kind == "none":
        return "stay_public"
    sources = outreach_sources(thread)
    source = next((r for r in sources if str(r["comment_id"]) == assessment.invitation_comment_id), None)
    if (
        not source or str(source.get("author_uid")) != str(draft["platform_user_id"])
        or not assessment.invitation_quote.strip() or assessment.invitation_quote not in source["text"]
    ):
        return "stay_public"
    mentions = {str(person.get("uid")) for person in source.get("mentions", [])}
    parent_id = source.get("parent_comment_id")
    parent = next((row for row in sources if str(row["comment_id"]) == str(parent_id)), None)
    addressed = actor in mentions or parent and str(parent.get("author_uid")) == actor
    addressed = addressed or str(thread.get("resource_author_uid") or "") == actor
    # A real @ to another account does not invite this account to DM.
    if mentions and actor not in mentions or not addressed:
        return "stay_public"
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
