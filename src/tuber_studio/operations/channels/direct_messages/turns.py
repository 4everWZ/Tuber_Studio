"""Native ContextTree + multi-turn plain-text DM conversation runner."""

from __future__ import annotations

from typing import Any

from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....core.text import operation_output_diagnostics, validate_operation_text
from ...kernel.generation import generate_with_recovery
from ...kernel.l2_bridge import (
    build_base_node,
    create_context_tree,
    fork_dm_session_branch,
    render_openai_messages,
)
from ...kernel.session import run_tool_session, should_use_native_turns
from ...kernel.tools import OperationToolSession, sanitize_spoken_text
from .models import ConversationReply, IncomingSettlement

DM_TOOL_NAMES = ("search_web", "save_memory", "recall_history", "skip_reply")


def should_use_native_dm_turns(service: Any) -> bool:
    """Use native multi-turn DM flow unless a test monkeypatches legacy generate_json hooks."""
    return should_use_native_turns(service.runtime, llm_override_attrs=("generate_json",))


def _source_comment_from_event(draft: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    invitation = event.get("contact_invitation")
    if isinstance(invitation, dict) and invitation:
        return {
            "comment_id": invitation.get("comment_id") or draft.get("source_comment_id") or "",
            "bvid": invitation.get("bvid") or "",
            "quote": invitation.get("quote") or draft.get("purpose") or "",
            "purpose": draft.get("purpose") or "",
        }
    public_thread = event.get("public_source_thread")
    if isinstance(public_thread, dict) and public_thread.get("source"):
        src = public_thread["source"]
        return {
            "comment_id": src.get("comment_id") or "",
            "bvid": (public_thread.get("target") or {}).get("bvid") or "",
            "quote": src.get("text") or "",
            "purpose": draft.get("purpose") or "",
        }
    return None


def _python_handled_settlement(event: dict[str, Any], refs: dict[str, Any], links: dict[tuple[int, int], Any]) -> list[IncomingSettlement]:
    """Derive handled settlements deterministically from verified outgoing operator turns."""
    settled: list[IncomingSettlement] = []
    conversation = event.get("conversation") or ()
    for incoming_ref in sorted(refs):
        matching_outgoing = max((out for (inc, out) in links if inc == incoming_ref), default=None)
        if matching_outgoing is None:
            continue
        if 1 <= matching_outgoing <= len(conversation):
            quote = str(conversation[matching_outgoing - 1].get("text") or "").strip()[:120]
            if quote:
                settled.append(IncomingSettlement(incoming=incoming_ref, outgoing=matching_outgoing, quote=quote))
    return settled


async def generate_native_dm_reply(
    service: Any,
    draft: dict[str, Any],
    character: dict[str, Any],
    event: dict[str, Any],
    relationship: dict[str, Any],
    refs: dict[str, Any],
    links: dict[tuple[int, int], Any],
) -> tuple[ConversationReply, dict[str, Any]]:
    """Run native multi-turn DM session with owner takeover turns & plain-text speaking."""
    runtime = service.runtime
    snapshot = runtime_identity(runtime, character, draft["account_id"])
    unified = getattr(runtime, "unified_memory", None)
    # Strict scope isolation: DM never reads scope="owner" operator private chats.
    unified_public = unified.for_prompt("public", character_id=character["id"], limit=15) if unified is not None else []
    peer_uid = str(draft["platform_user_id"])
    peer_name = str(draft.get("display_name") or "")

    base = build_base_node(
        character,
        channel="direct_message",
        self_actor=snapshot,
        operating_account=snapshot,
        memories=unified_public,
        memory_scope="public",
        speaker={"uid": peer_uid, "name": peer_name, "relationship": relationship},
    )
    tree = create_context_tree(base)
    objective = (
        "请直接自然地回应对方尚未答复的私信（不要使用JSON包裹）。"
        "如果对话里号主人工接管已经替你回复完、或对方只是客套无需继续回复，请调用 skip_reply。"
    )
    branch = fork_dm_session_branch(
        tree,
        peer_uid=peer_uid,
        peer_name=peer_name,
        conversation=event.get("conversation") or (),
        open_messages=event.get("open_messages") or (),
        source_comment=_source_comment_from_event(draft, event),
        objective=objective,
    )

    tool_session = OperationToolSession(
        channel="direct_message",
        character=character,
        account_id=str(draft["account_id"]),
        store=service.store,
        unified_memory=unified,
        settings=service.settings,
        self_uid=str(draft["account_id"]),
        web_search_allowed=True,
    )

    maximum = service.settings.data["omni"].get("operations_max_tokens", 8192)
    rendered = render_openai_messages(tree, branch.node_id)
    binding = {
        "task": "generation.direct_message",
        "account_id": draft["account_id"],
        "character": {"id": character["id"], "version": character["version"]},
        "event_id": event["event_id"],
        "conversation_sha256": event.get("conversation_sha256") or content_hash(event.get("conversation") or ()),
        "self_identity": snapshot,
    }

    def guard():
        if runtime.character["id"] != character["id"] or runtime_identity(runtime, character, draft["account_id"]) != snapshot:
            raise ValueError("私信生成期间账号或角色已变化")

    async def invoke(_packet, *, web_fetch=None):
        res = await run_tool_session(
            runtime,
            tree,
            branch.node_id,
            tool_session,
            tool_names=DM_TOOL_NAMES,
            spoken_kind="direct_message",
            max_output_tokens=maximum,
        )
        return {
            "action": res.action,
            "text": res.text,
            "skip_reason": res.skip_reason,
            "skip_explanation": res.skip_explanation,
            "tool_calls": res.tool_calls,
        }

    def validate(payload: dict[str, Any] | ConversationReply) -> ConversationReply:
        if isinstance(payload, ConversationReply):
            return payload
        if payload.get("action") == "skip":
            handled = _python_handled_settlement(event, refs, links)
            reason = payload.get("skip_explanation") or payload.get("skip_reason") or "已由号主接管或暂不回复"
            return ConversationReply(text="", reason=reason, reply_to=[], handled=handled)
        text = validate_operation_text(sanitize_spoken_text(payload.get("text") or ""), label="私信正文")
        return ConversationReply(text=text, reason="自然接续当前私信对话", reply_to=sorted(refs), handled=[])

    def diagnose(reply: ConversationReply) -> str | None:
        if reply.text and operation_output_diagnostics(reply.text)["needs_review"]:
            return "正文包含大量重复内容，保留原文待审"
        return None

    decision, audit = await generate_with_recovery(
        runtime,
        channel="generation.direct_message",
        identity=event["event_id"] + ":" + content_hash(binding),
        binding=binding,
        character=character,
        messages=rendered,
        generate=invoke,
        validate=validate,
        diagnose=diagnose,
        max_tokens=maximum,
        guard=guard,
        reuse_completed=False,
        allow_web_fetch=False,
    )
    return decision, {"generation_identity": snapshot, "continuation": audit}
