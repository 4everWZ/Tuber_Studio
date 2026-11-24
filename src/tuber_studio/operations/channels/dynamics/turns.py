"""Native ContextTree + multi-turn plain-text drafting for dynamics (grounded & standalone)."""

from __future__ import annotations

from typing import Any

from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....core.text import operation_output_diagnostics, validate_operation_text
from ...kernel.generation import generate_with_recovery
from ...kernel.l2_bridge import (
    append_branch_turn,
    build_base_node,
    create_context_tree,
    render_openai_messages,
)
from ...kernel.session import run_tool_session, should_use_native_turns
from ...kernel.tools import OperationToolSession, sanitize_spoken_text
from ..mentions import bind_mentions, detect_plain_mentions

DYNAMIC_TOOL_NAMES = ("search_web", "save_memory", "recall_history", "skip_reply")


def should_use_native_dynamic_turns(service: Any) -> bool:
    """Use native multi-turn dynamic drafting unless a test mocks legacy JSON hooks."""
    llm = getattr(service.runtime, "operation_llm", None)
    if llm is not None and getattr(llm, "output", None) is not None and type(llm).__name__ == "DynamicModel":
        return False
    return should_use_native_turns(service.runtime, llm_override_attrs=("generate_json",))


def _format_dynamic_sources(public_sources: list[dict[str, Any]]) -> str:
    if not public_sources:
        return "最近没有强制要提的公开互动记录；如果你想分享个人随想、日常观察或角色灵感，也可以直接发表一条独立动态。"
    lines: list[str] = []
    for src in public_sources:
        stype = src.get("type") or src.get("kind") or "material"
        fictional_label = "虚构设定/故事" if src.get("fictional") else "真实公开经历"
        if stype == "published_reply":
            orig = src.get("original_comment") or {}
            author = orig.get("author_name") or orig.get("author") or "观众"
            quote = orig.get("text") or src.get("comment") or ""
            my_reply = src.get("reply") or src.get("text") or ""
            bvid = src.get("bvid") or (src.get("video") or {}).get("bvid") or ""
            lines.append(f"- [评论区互动 {bvid} · {fictional_label}] @{author} 说：「{quote}」；你回过：「{my_reply}」")
        elif stype == "public_incident":
            headline = src.get("headline") or ""
            anecdote = src.get("anecdote") or src.get("text") or ""
            quotes = [
                str(p.get("quote") or p.get("text") or "").strip()
                for p in (src.get("public_sources") or ())
                if isinstance(p, dict) and (p.get("quote") or p.get("text"))
            ]
            quote_part = f"；原评论：「{' / '.join(quotes)}」" if quotes else ""
            lines.append(f"- [公开经历 · {headline}] {anecdote}{quote_part}")
        elif stype in {"video_watch"}:
            video = src.get("video") or {}
            bvid = src.get("bvid") or video.get("bvid") or ""
            title = video.get("title") or src.get("title") or ""
            owner = video.get("owner") or src.get("owner") or ""
            summary = src.get("text") or src.get("summary") or ""
            lines.append(f"- [视频经历 {bvid}] 《{title}》UP主 {owner}：{summary}")
        else:
            text = src.get("text") or src.get("quote") or src.get("summary") or ""
            if text:
                lines.append(f"- [{stype} · {fictional_label}] {text}")
    return "\n".join(lines)


def _has_fragment_match(content: str, snippet: str) -> bool:
    text = str(snippet or "").strip()
    if len(text) < 4:
        return False
    if text in content:
        return True
    for idx in range(0, max(1, len(text) - 3), 2):
        frag = text[idx : idx + 4].strip()
        if len(frag) >= 4 and not frag.isascii() and frag in content:
            return True
    return False


def _match_grounded_sources(content: str, public_sources: list[dict[str, Any]], *, topic: str = "") -> tuple[list[str], list[str], str]:
    """Bind real source_ids & link_source_ids deterministically; standalone thoughts stay []."""
    matched_ids: list[str] = []
    link_ids: list[str] = []
    illustration = "none"
    lowered = content.casefold()

    for src in public_sources:
        sid = str(src.get("id") or "")
        if not sid:
            continue
        if topic and sid.startswith("operator-topic:"):
            matched_ids.append(sid)
            continue
        stype = src.get("type") or src.get("kind") or ""
        bvid = str(src.get("bvid") or (src.get("video") or {}).get("bvid") or "").strip()
        title = str(src.get("title") or (src.get("video") or {}).get("title") or "").strip()
        orig = src.get("original_comment") or {}
        author = str(orig.get("author_name") or orig.get("author") or "").strip()
        quote = str(orig.get("text") or src.get("text") or "").strip()
        incident_texts = [
            src.get("headline") or "",
            src.get("anecdote") or "",
            *(str(p.get("quote") or "") for p in (src.get("public_sources") or ()) if isinstance(p, dict)),
        ]
        explicit_hit = (
            (bvid and bvid.casefold() in lowered)
            or (author and f"@{author}" in content)
            or (len(title) >= 4 and title.casefold() in lowered)
            or (author and len(author) >= 2 and author in content and any(w in content for w in ("评论", "刚才", "视频", "那条", "说")))
            or _has_fragment_match(content, quote)
            or _has_fragment_match(content, src.get("reply") or "")
            or any(_has_fragment_match(content, piece) for piece in incident_texts)
        )
        if explicit_hit:
            matched_ids.append(sid)
            if stype in {"published_reply", "public_incident"} and len(link_ids) < 2:
                link_ids.append(sid)
                illustration = "comment_card"
            elif bvid and len(link_ids) < 2:
                link_ids.append(sid)

    return matched_ids, link_ids[:2], illustration


def _detect_dynamic_mentions(content: str, public_sources: list[dict[str, Any]], source_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(source_ids)
    candidates: list[dict[str, Any]] = []
    for src in public_sources:
        if wanted and src.get("id") not in wanted:
            continue
        orig = src.get("original_comment") or {}
        uid = str(orig.get("author_uid") or orig.get("platform_user_id") or "")
        name = str(orig.get("author_name") or orig.get("author") or "").strip()
        if uid and name:
            candidates.append({"uid": uid, "name": name})
        video = src.get("video") or {}
        v_uid = str(video.get("owner_uid") or "")
        v_name = str(video.get("owner") or "").strip()
        if v_uid and v_name:
            candidates.append({"uid": v_uid, "name": v_name})
    return detect_plain_mentions(content, candidates)


async def evaluate_native_dynamic(
    service: Any,
    *,
    char: dict[str, Any],
    evaluation_account: str,
    recent_posts: list[dict[str, Any]],
    related_posts: list[dict[str, Any]],
    public_sources: list[dict[str, Any]],
    unified_public: list[dict[str, Any]],
    current_time: str,
    topic: str = "",
    manual: bool = False,
    default_interval: int = 1800,
    guard=None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run native multi-turn dynamic post drafting supporting grounded & standalone posts."""
    runtime = service.runtime
    snapshot = runtime_identity(runtime, char, evaluation_account or None)
    base = build_base_node(
        char,
        channel="dynamic_post",
        self_actor=snapshot,
        operating_account=snapshot,
        memories=[item.get("content") or "" for item in unified_public],
        memory_scope="public",
    )
    tree = create_context_tree(base)

    recent_block = "\n".join(f"- {p.get('content', '')}" for p in recent_posts[:6] if p.get("content"))
    parts = [f"当前时间：{current_time}"]
    if recent_block:
        parts.append("你近期发过的动态（不要重复类似内容）：\n" + recent_block)
    if topic:
        parts.append(f"本次指定分享主题：{topic}")
    parts.append("可用公开经历与互动参考：\n" + _format_dynamic_sources(public_sources))
    parts.append(
        "请用自己的自然口吻直接写出一条要发的 B 站动态正文（不需要 JSON 包裹，也不要写内部编号）。\n"
        "- 可以围绕上面某段看过的视频或评论互动讲清起因和感受（程序会自动关联真实链接与 @）；\n"
        "- 也可以纯分享独立思考、心情随感或日常观察（不必强行牵扯视频）；\n"
        "- 如果觉得当下不适合发或会撞车，请调用 skip_reply。"
    )
    branch = append_branch_turn(tree, base.node_id, {"role": "user", "content": "\n\n".join(parts)})

    tool_session = OperationToolSession(
        channel="dynamic_post",
        character=char,
        account_id=str(evaluation_account or ""),
        store=service.store,
        unified_memory=getattr(runtime, "unified_memory", None),
        settings=service.settings,
        self_uid=str(evaluation_account or "") or None,
        web_search_allowed=True,
    )

    maximum = service.settings.data["omni"].get("operations_max_tokens", 8192)
    rendered = render_openai_messages(tree, branch.node_id)
    binding = {
        "task": "generation.dynamics",
        "character": {"id": char["id"], "version": char["version"]},
        "source_sha256": content_hash(public_sources),
        "self_identity": snapshot,
        "topic": topic,
        "manual": manual,
    }

    async def invoke(_packet, *, web_fetch=None):
        res = await run_tool_session(
            runtime,
            tree,
            branch.node_id,
            tool_session,
            tool_names=DYNAMIC_TOOL_NAMES,
            spoken_kind="dynamic",
            max_output_tokens=maximum,
        )
        return {
            "action": res.action,
            "text": res.text,
            "skip_reason": res.skip_reason,
            "skip_explanation": res.skip_explanation,
            "tool_calls": res.tool_calls,
        }

    def validate(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") in {"post", "wait"} and "content" in payload and "source_ids" in payload:
            return payload
        if payload.get("action") == "skip":
            return {
                "action": "wait",
                "reason": payload.get("skip_explanation") or payload.get("skip_reason") or "当下暂不发动态",
                "content": "",
                "source_ids": [],
                "points": [],
                "link_source_ids": [],
                "mentions": [],
                "illustration": "none",
                "next_evaluation_seconds": default_interval,
                "search_queries": [],
            }
        text = validate_operation_text(sanitize_spoken_text(payload.get("text") or ""), label="动态正文")
        source_ids, link_ids, illustration = _match_grounded_sources(text, public_sources, topic=topic)
        mentions = _detect_dynamic_mentions(text, public_sources, source_ids)
        return {
            "action": "post",
            "reason": "分享近期经历与互动" if source_ids else "分享当下随想与观察",
            "content": text,
            "source_ids": source_ids,
            "points": [{"summary": text[:60], "source_ids": source_ids}] if source_ids else [],
            "link_source_ids": link_ids,
            "mentions": mentions,
            "illustration": illustration,
            "next_evaluation_seconds": default_interval,
            "search_queries": [],
        }

    def diagnose(decision: dict[str, Any]) -> str | None:
        content = str(decision.get("content") or "")
        if decision.get("action") == "post" and content and operation_output_diagnostics(content)["needs_review"]:
            return "正文包含大量重复内容，保留原文待审"
        return None

    return await generate_with_recovery(
        runtime,
        channel="generation.dynamics",
        identity=content_hash(binding),
        binding=binding,
        character=char,
        messages=rendered,
        generate=invoke,
        validate=validate,
        diagnose=diagnose,
        max_tokens=maximum,
        guard=guard or (lambda: None),
        context_data={"public_sources": public_sources},
        allow_web_fetch=False,
    )


async def revise_native_dynamic_caption(
    service: Any,
    row: dict[str, Any],
    original: dict[str, Any],
    instruction: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Revise an already-published dynamic caption via direct plain text (no JSON speech wrapper)."""
    runtime = service.runtime
    char = service._character()
    snapshot = runtime_identity(runtime, char, row.get("account_id"))
    base = build_base_node(
        char,
        channel="dynamic_post",
        self_actor=snapshot,
        operating_account=snapshot,
        memory_scope="public",
    )
    tree = create_context_tree(base)
    prompt = (
        f"需要修改已发布的动态正文。\n"
        f"原正文：{original.get('text') or ''}\n"
        f"修改要求：{instruction}\n\n"
        "请直接输出修改后的完整动态正文（自然中文，不用 JSON 包裹）。原本有用的 @ 昵称请保留。"
    )
    branch = append_branch_turn(tree, base.node_id, {"role": "user", "content": prompt})
    tool_session = OperationToolSession(
        channel="dynamic_post",
        character=char,
        account_id=str(row.get("account_id") or ""),
        store=service.store,
        unified_memory=getattr(runtime, "unified_memory", None),
        settings=service.settings,
    )
    res = await run_tool_session(
        runtime,
        tree,
        branch.node_id,
        tool_session,
        tool_names=("search_web", "save_memory", "recall_history"),
        spoken_kind="dynamic",
        max_output_tokens=1024,
    )
    caption = validate_operation_text(sanitize_spoken_text(res.text), label="动态正文")
    participants = original.get("mentions") or []
    mentioned = detect_plain_mentions(caption, participants)
    mentions, pending = await bind_mentions(service.account.adapter, caption, mentioned, participants)
    return caption, mentions, pending
