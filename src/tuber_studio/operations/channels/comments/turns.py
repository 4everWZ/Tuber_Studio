"""Native ContextTree + multi-turn Responses drafting for imported/thread comments."""

from __future__ import annotations

import json
import time
from typing import Any

from ....core.identity import runtime_identity
from ....core.meter import GenerationMeter
from ....core.models import Reply, content_hash
from ....core.text import operation_output_diagnostics, validate_comment_body
from ...kernel.generation import generate_with_recovery, hold_comment_body
from ...kernel.l2_bridge import (
    build_base_node,
    create_context_tree,
    fork_comment_floor_branch,
    render_openai_messages,
)
from ...kernel.session import run_tool_session, should_use_native_turns
from ...kernel.tools import OperationToolSession, sanitize_spoken_text
from ..mentions import bind_mentions, detect_plain_mentions, mention_participants
from .timeline import comment_source, comment_thread, confirmed_bot_comment_ids


def _cached_video_material(store: Any, bvid: str) -> dict[str, Any] | None:
    """Read already-watched video material without triggering any new subtitle/frame fetch."""
    if not store or not bvid:
        return None
    tables = {r["name"] for r in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
    if "browsing_video_progress" not in tables:
        return None
    row = store.one("SELECT material FROM browsing_video_progress WHERE bvid=?", (bvid,))
    if not row or not row.get("material"):
        return None
    try:
        data = json.loads(row["material"])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def should_use_native_comment_turns(service: Any) -> bool:
    """Use native ContextTree + tool calling unless a test monkeypatched legacy hooks directly."""
    return should_use_native_turns(service.runtime, allow_runtime_hooks=("generate_reply",))


async def generate_native_comment_reply(
    service: Any,
    row: dict[str, Any],
    character: dict[str, Any],
    event: dict[str, Any],
    relationship: dict[str, Any],
    account_id: str | None,
    *,
    materials: list[dict[str, Any]] = (),
) -> tuple[Reply | None, dict[str, Any]]:
    """Build Video Base -> Comment Floor Branch and run native multi-turn tool loop."""
    runtime = service.runtime
    store = service.store
    self_actor = runtime.identity_snapshot(character, account_id)
    unified = getattr(runtime, "unified_memory", None)
    unified_public = unified.for_prompt("public", character_id=character["id"], limit=15) if unified is not None else []
    timeline_data = comment_thread(
        store,
        row["video_id"],
        row["thread_id"],
        account_id=account_id,
        character_id=character["id"],
    )
    timeline_comments = timeline_data.get("comments") or []
    confirmed_ids = confirmed_bot_comment_ids(
        store, str(account_id or ""), str(character["id"]), str(row["video_id"]), str(row["thread_id"])
    ) | {str(item["comment_id"]) for item in timeline_comments if item.get("publication") == "confirmed"}
    video_material = _cached_video_material(store, str(row.get("video_id") or ""))
    owner_uid = str((character.get("bilibili_owner") or {}).get("uid") or "") or None
    uploader_uid = timeline_data.get("resource_author_uid")

    base = build_base_node(
        character,
        channel="comment_reply",
        self_actor=self_actor,
        operating_account=self_actor,
        memories=unified_public,
        memory_scope="public",
        video_material=video_material,
    )
    tree = create_context_tree(base)

    extra_ctx: dict[str, Any] = {}
    if relationship:
        extra_ctx["relationship"] = {
            k: relationship[k] for k in ("affinity", "tone_hint", "interaction_count") if k in relationship
        }
    if event.get("actor_roles"):
        extra_ctx["actor_roles"] = event["actor_roles"]
    if materials:
        extra_ctx["comment_materials"] = materials

    objective = "请自然接住当前评论回复；如果无需发言或对方不是对本账号说话，可调用 skip_reply。"
    branch = fork_comment_floor_branch(
        tree,
        root_id=str(row["thread_id"]),
        timeline=timeline_comments,
        reply_target=comment_source(row),
        self_uid=str(account_id or "") or None,
        confirmed_bot_comment_ids=confirmed_ids,
        uploader_uid=str(uploader_uid or "") or None,
        owner_uid=owner_uid,
        objective=objective,
        extra_context=extra_ctx or None,
    )

    tool_names = ["search_web", "save_memory", "recall_history", "skip_reply"]
    if video_material:
        tool_names.extend(["read_video_segment", "inspect_video_frames"])
    browsing = getattr(runtime, "browsing", None)
    visual_ok = bool(video_material and browsing and getattr(browsing, "evidence", None) and browsing.evidence.has_visual_provider())

    tool_session = OperationToolSession(
        channel="comment_reply",
        character=character,
        account_id=str(account_id or ""),
        store=store,
        unified_memory=unified,
        settings=service.settings,
        material=video_material,
        self_uid=str(account_id or "") or None,
        resource=str(row.get("video_id") or "") or None,
        root_id=str(row.get("thread_id") or "") or None,
        visual_allowed=visual_ok,
        web_search_allowed=True,
    )

    generation_identity = runtime_identity(runtime, character, account_id or None)
    rendered = render_openai_messages(tree, branch.node_id)
    comment_id = str(row.get("id") or row["comment_id"])
    binding = {
        "task": "comments",
        "account_id": account_id,
        "character": {"id": character["id"], "version": character["version"]},
        "comment_id": comment_id,
        "thread_id": row["thread_id"],
        "self_identity": generation_identity,
        "source_sha256": content_hash(event),
    }

    async def invoke_native(_packet, *, web_fetch=None):
        with GenerationMeter(runtime.browsing_repository, str(row.get("thread_id") or comment_id)):
            res = await run_tool_session(
                runtime,
                tree,
                branch.node_id,
                tool_session,
                tool_names=tool_names,
                spoken_kind="comments",
                max_output_tokens=service.settings.data["omni"].get("operations_max_tokens", 1024),
            )
            return {"action": res.action, "text": res.text, "skip_reason": res.skip_reason, "tool_calls": res.tool_calls}

    def validate_result(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "skip":
            return payload
        cleaned = validate_comment_body(sanitize_spoken_text(payload.get("text") or ""))
        return payload | {"text": cleaned}

    def diagnose_result(payload: dict[str, Any]) -> str | None:
        if payload.get("action") == "skip":
            return None
        cleaned = str(payload.get("text") or "")
        hold_comment_body(payload, cleaned, recipient_name=row.get("display_name") or "")
        if operation_output_diagnostics(cleaned)["needs_review"]:
            return "正文包含大量重复内容，保留原文待审"
        return None

    result, _ = await generate_with_recovery(
        runtime,
        channel="generation.comments",
        identity=content_hash(binding),
        binding=binding,
        character=character,
        messages=rendered,
        generate=invoke_native,
        validate=validate_result,
        diagnose=diagnose_result,
        max_tokens=service.settings.data["omni"].get("operations_max_tokens", 1024),
        guard=lambda: None,
        context_data={"source": rendered[-1]["content"]},
        reuse_completed=False,
        allow_web_fetch=False,
    )

    if result.get("action") == "skip":
        return None, {
            "generation_identity": generation_identity,
            "skipped": True,
            "reason": result.get("skip_reason") or "skip",
            "tool_calls": result.get("tool_calls") or [],
            "timestamp": time.time(),
        }

    text = result["text"]
    participants = mention_participants({"source": comment_source(row), "timeline": timeline_comments})
    detected = detect_plain_mentions(text, participants)
    mentions: list[dict[str, Any]] = []
    pending_mentions: list[dict[str, Any]] = []
    if detected:
        mentions, pending_mentions = await bind_mentions(service.account.adapter, text, detected, participants)

    diag = operation_output_diagnostics(text)
    reply = Reply(spoken_text=text)
    check = {
        "generation_identity": generation_identity,
        "output": diag,
        "tool_calls": result.get("tool_calls") or [],
        "sources": [],
        **({"mentions": mentions, "pending_mentions": pending_mentions} if mentions or pending_mentions else {}),
    }
    return reply, check
