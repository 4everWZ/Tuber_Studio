"""Native ContextTree + multi-turn plain-text drafting for dynamics (grounded & standalone)."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....core.text import detect_reasoning_leak, operation_output_diagnostics, validate_operation_text
from ....infra.platforms.bilibili.urls import COMMENT_ID
from ...kernel.generation import generate_with_recovery
from ...kernel.l2_bridge import (
    ContextNode,
    append_branch_turn,
    build_base_node,
    create_context_tree,
    render_openai_messages,
)
from ...kernel.session import run_tool_session, should_use_native_turns
from ...kernel.tools import OperationToolSession, sanitize_spoken_text
from ..mentions import bind_mentions, detect_plain_mentions
from .materials import source_links

_BV_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")

DYNAMIC_TOOL_NAMES = ("search_web", "save_memory", "recall_history", "skip_reply")


def should_use_native_dynamic_turns(service: Any) -> bool:
    """Use native multi-turn dynamic drafting unless a test mocks legacy JSON hooks."""
    llm = getattr(service.runtime, "operation_llm", None)
    if llm is not None and getattr(llm, "output", None) is not None and type(llm).__name__ == "DynamicModel":
        return False
    return should_use_native_turns(service.runtime, llm_override_attrs=("generate_json",))


def _source_bvid(src: dict[str, Any]) -> str:
    """Extract BV id from structured source metadata or linked URLs."""
    for candidate in (
        src.get("bvid"),
        (src.get("video") or {}).get("bvid"),
        (src.get("reference") or {}).get("resource"),
        str(src.get("id") or "").removeprefix("video:"),
    ):
        value = str(candidate or "").strip()
        if re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
            return value
    for candidate in (*source_links(src), src.get("source_url") or ""):
        if match := _BV_RE.search(str(candidate or "")):
            return match.group(1)
    return ""


def _source_author(src: dict[str, Any]) -> tuple[str, str]:
    """Return `(uid, name)` for the primary external/source author on a material item."""
    orig = src.get("original_comment") or {}
    original_author = src.get("original_author") or {}
    ref = src.get("reference") or {}
    participants = src.get("participants") or ()
    first_participant = next((item for item in participants if isinstance(item, dict)), {})
    for uid, name in (
        (original_author.get("uid"), original_author.get("name")),
        (orig.get("author_uid") or orig.get("platform_user_id"), orig.get("author_name") or orig.get("author")),
        (ref.get("author_uid"), ref.get("author_name")),
        (first_participant.get("uid"), first_participant.get("name")),
    ):
        if uid or name:
            return str(uid or "").strip(), str(name or "").strip()
    return "", ""


def _format_dynamic_sources(public_sources: list[dict[str, Any]], *, self_uid: str = "") -> str:
    if not public_sources:
        return "最近没有强制要提的公开互动记录；如果你想分享个人随想、日常观察或角色灵感，也可以直接发表一条独立动态。"
    lines: list[str] = []
    for src in public_sources:
        stype = src.get("type") or src.get("kind") or "material"
        fictional_label = "虚构设定/故事" if src.get("fictional") else "真实公开经历"
        resource = (src.get("reference") or {}).get("resource") or _source_bvid(src)
        if stype == "published_reply":
            orig = src.get("original_comment") or {}
            uid, author = _source_author(src)
            actor = f"@{author}" if author else "来评作者"
            if uid:
                actor += f"（UID {uid}）"
            quote = orig.get("text") or src.get("comment") or ""
            received = f"，原话 {json.dumps(quote, ensure_ascii=False)}" if quote else ""
            reply = src.get("reply") or src.get("text") or ""
            receipt = src.get("receipt_id")
            reply_account = str(src.get("reply_account_uid") or "")
            attributed = self_uid and self_uid == reply_account and COMMENT_ID.fullmatch(str(receipt))
            publisher = "你已发布的回复" if attributed else "记录中的已发布回复"
            lines.append(f"- [评论区互动 {resource}] {actor}的评论 {orig.get('id') or '编号未保存'}{received}；"
                         f"{publisher} {receipt or ''}：{json.dumps(reply, ensure_ascii=False)}")
        elif stype == "public_comment":
            uid, author = _source_author(src)
            text = str(src.get("text") or src.get("quote") or "").strip()
            actor = f"@{author}" if author else "资料缺失"
            if uid:
                actor += f"（UID {uid}）"
            relation = "旁观到的他人评论" if uid else "读到的公开评论"
            if self_uid and uid and str(uid) == str(self_uid):
                relation = "同账号的公开评论，角色归属未确认"
            lines.append(f"- [{relation} {resource}] 作者 {actor}，评论 "
                         f"{(src.get('reference') or {}).get('comment_id') or '编号未保存'}："
                         f"{json.dumps(text, ensure_ascii=False)}")
        elif stype == "public_incident":
            material_kind = src.get("original_material_kind")
            if material_kind:
                uid, name = _source_author(src)
                label = "收录的第三方评论" if material_kind == "comment" else "收录的视频字幕"
                author = f"作者 @{name}" if name else "作者资料未保存"
                if uid:
                    author += f"（UID {uid}）"
                text = str(src.get("text") or "").strip()
                lines.append(f"- [{label} {resource}] {author}，摘录：{json.dumps(text, ensure_ascii=False)}")
                continue
            headline = src.get("headline") or ""
            anecdote = src.get("anecdote") or src.get("text") or ""
            quotes = [
                str(p.get("quote") or p.get("text") or "").strip()
                for p in (src.get("public_sources") or ())
                if isinstance(p, dict) and (p.get("quote") or p.get("text"))
            ]
            quote_part = f"；原评论：「{' / '.join(quotes)}」" if quotes else ""
            lines.append(f"- [公开经历记录 · {headline}] 摘要：{anecdote}{quote_part}")
        elif stype in {"watched_video", "video_watch"}:
            video = src.get("video") or {}
            participant_uid, participant_name = _source_author(src)
            title = str(video.get("title") or src.get("title") or "").strip()
            owner = str(video.get("owner") or src.get("owner") or participant_name or "").strip()
            owner_uid = video.get("owner_uid") or participant_uid
            summary = str(src.get("text") or src.get("summary") or "").strip()
            video_header, separator, excerpts = summary.partition(" · 已读片段\n")
            title = title or (video_header if separator else "")
            owner_label = f"UP主 {owner}" if owner else "UP 主资料未保存"
            if owner_uid:
                owner_label += f"（UID {owner_uid}）"
            title_label = f"《{title}》 " if title else ""
            lines.append(f"- [观看素材 {resource}] {title_label}{owner_label}，视频片段："
                         f"{json.dumps(excerpts if separator else summary, ensure_ascii=False)}")
        else:
            text = src.get("text") or src.get("quote") or src.get("summary") or ""
            if text:
                lines.append(f"- [{stype} · {fictional_label}] {text}")
    return "\n".join(lines)


_QUOTED_SNIPPET_RE = re.compile(r'「([^「」]+)」|“([^“”]+)”|"([^"\n]+)"')
_GENERIC_AUTHOR_NAMES = frozenset({"路人", "作者", "用户", "观众", "网友", "匿名", "UP", "UP主", "资料缺失", "作者资料未保存", "未知"})
_AUTHOR_REF_CUES = ("评论", "刚才", "视频", "那条", "说", "留言", "回复", "吐槽", "提到")


def _nontrivial_chars(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def _has_distinctive_author_ref(content: str, author: str) -> bool:
    name = str(author or "").strip()
    if not name or name in _GENERIC_AUTHOR_NAMES:
        return False
    if name.isascii():
        if len(name) < 3 or not re.search(rf"(?<![0-9A-Za-z_]){re.escape(name)}(?![0-9A-Za-z_])", content):
            return False
    elif len(name) < 2 or name not in content:
        return False
    return any(cue in content for cue in _AUTHOR_REF_CUES)


def _has_fragment_match(content: str, snippet: str) -> bool:
    """Match only verbatim quoted snippets (>= 8 chars) or exact distinctive phrases (>= 10 chars)."""
    text = str(snippet or "").strip()
    if not text:
        return False
    for match in _QUOTED_SNIPPET_RE.finditer(content):
        quoted = next((group for group in match.groups() if group is not None), "").strip()
        nontrivial = _nontrivial_chars(quoted)
        if len(nontrivial) >= 8 and len(set(nontrivial)) >= 4 and quoted in text:
            return True
    full_nontrivial = _nontrivial_chars(text)
    if len(full_nontrivial) < 10 or len(set(full_nontrivial)) < 5:
        return False
    if text in content:
        return True
    for start, ch in enumerate(text):
        if not ch.isalnum():
            continue
        chars: list[str] = []
        for end in range(start, len(text)):
            cur = text[end]
            if cur in "。！？!?\n":
                break
            if cur.isalnum():
                chars.append(cur)
                if len(chars) == 10:
                    if len(set(chars)) >= 5 and text[start : end + 1] in content:
                        return True
                    break
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
        bvid = _source_bvid(src)
        discussion = (stype in {"published_reply", "public_incident", "public_comment"}
                      and src.get("original_material_kind") != "subtitle")
        links = source_links(src)
        title = str(src.get("title") or (src.get("video") or {}).get("title") or "").strip()
        if not title and stype in {"watched_video", "video_watch"} and " · " in str(src.get("text") or ""):
            title = str(src.get("text") or "").splitlines()[0].split(" · ", 1)[0].strip()
        orig = src.get("original_comment") or {}
        _, author = _source_author(src)
        quote = str(orig.get("text") or src.get("text") or "").strip()
        incident_texts = [
            src.get("headline") or "",
            src.get("anecdote") or "",
            *(str(p.get("quote") or "") for p in (src.get("public_sources") or ()) if isinstance(p, dict)),
        ]
        explicit_hit = (
            (not discussion and bvid and bvid.casefold() in lowered)
            or any(re.search(re.escape(link) + r"(?![0-9A-Za-z_/#])", content) for link in links)
            or (author and f"@{author}" in content)
            or (not discussion and len(title) >= 6 and title.casefold() in lowered)
            or _has_distinctive_author_ref(content, author)
            or _has_fragment_match(content, quote)
            or _has_fragment_match(content, src.get("reply") or "")
            or any(_has_fragment_match(content, piece) for piece in incident_texts)
        )
        if explicit_hit:
            matched_ids.append(sid)
            if discussion:
                illustration = "comment_card"
            if links and len(link_ids) < 2:
                link_ids.append(sid)

    return matched_ids, link_ids[:2], illustration


def _detect_dynamic_mentions(content: str, public_sources: list[dict[str, Any]], source_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(source_ids)
    names: dict[str, set[str]] = {}

    def add_candidate(raw_uid: Any, raw_name: Any) -> None:
        uid = str(raw_uid or "").strip()
        name = str(raw_name or "").strip()
        if uid and name:
            names.setdefault(name, set()).add(uid)

    for src in public_sources:
        if wanted and src.get("id") not in wanted:
            continue
        add_candidate(*_source_author(src))
        ref = src.get("reference") or {}
        add_candidate(ref.get("author_uid"), ref.get("author_name"))
        for person in src.get("participants") or ():
            if isinstance(person, dict):
                add_candidate(person.get("uid"), person.get("name"))
        video = src.get("video") or {}
        add_candidate(video.get("owner_uid"), video.get("owner"))
    candidates = [{"name": name, "uid": next(iter(uids)) if len(uids) == 1 else None} for name, uids in names.items()]
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
        parts.append("近期带发布回执的动态正文（供去重）：\n" + recent_block)
    if related_posts:
        history = [
            {"status": "平台已确认" if post["status"] == "shared" else "未确认发布", "content": post["content"]}
            for post in related_posts
        ]
        parts.append("相关的动态记录（供去重或跟进进展）：\n" + json.dumps(history, ensure_ascii=False))
    if topic:
        parts.append(f"本次指定分享主题：{topic}")
    parts.append("可用公开经历与互动参考：\n" + _format_dynamic_sources(public_sources, self_uid=str(evaluation_account or "")))
    parts.append(
        "请用自己的自然口吻直接写出一条要发的 B 站动态正文（不需要 JSON 包裹，也不要写内部编号）。\n"
        "- 可以围绕上面某段看过的视频或评论互动，把起因和来龙去脉讲清楚，让没看过原楼的关注者也能看懂（程序会自动关联真实链接与 @）；\n"
        "- 引用按来源记录的作者和评论编号归属，说明在哪个视频或动态看见了谁的发言；\n"
        "- 也可以纯分享独立思考、心情随感或日常观察（不必强行牵扯视频）；\n"
        "- 如果觉得当下不适合发或会撞车，请调用 skip_reply。"
    )
    branch = tree.add_node(
        ContextNode(
            node_id=f"branch:dynamic_post:{uuid4().hex[:12]}",
            kind="branch",
            parent_id=base.node_id,
            scope="dynamic_post",
            channel="dynamic_post",
            title="公开动态分支",
        )
    )
    branch = append_branch_turn(tree, branch.node_id, {"role": "user", "content": "\n\n".join(parts)})

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
        if decision.get("action") == "post" and content:
            if leak := detect_reasoning_leak(content):
                return leak
            if operation_output_diagnostics(content)["needs_review"]:
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
    sources = row.get("sources") or []
    source_block = _format_dynamic_sources(sources, self_uid=str(row.get("account_id") or "")) if sources else ""
    if not source_block:
        card = row.get("source_comment_card") or row.get("comment_card") or {}
        card_quotes = [
            f"- @{item.get('author') or '路人'}：{item.get('quote')}"
            for item in (card.get("comments") or ())
            if isinstance(item, dict) and item.get("quote")
        ]
        if card_quotes:
            source_block = "\n".join(card_quotes)
    prompt = (
        f"需要修改已发布的动态正文。\n"
        f"原正文：{original.get('text') or ''}\n"
        + (f"原动态关联的公开来源（注意区分本人与第三方发言）：\n{source_block}\n" if source_block else "")
        + f"修改要求：{instruction}\n\n"
        "请直接输出修改后的完整动态正文（自然中文，不用 JSON 包裹）。原本有用的 @ 昵称请保留，讲清起因与来龙去脉。"
    )
    branch = tree.add_node(
        ContextNode(
            node_id=f"branch:dynamic_revise:{uuid4().hex[:12]}",
            kind="branch",
            parent_id=base.node_id,
            scope="dynamic_post",
            channel="dynamic_post",
            title="修改动态分支",
        )
    )
    branch = append_branch_turn(tree, branch.node_id, {"role": "user", "content": prompt})
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
