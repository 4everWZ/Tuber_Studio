"""Universal ContextNode (Base -> Branches) manager and OpenAI multi-turn message builder."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ...core.agent_types import ContextNode, ContextTree, MemoryItem, MemoryScope, parse_memory_scope
from ..self_model import build_metacognitive_profile, render_self_model_prompt
from .subtitles import timestamp

DEFAULT_VIDEO_BASE_TOKEN_LIMIT = 24_000
DEFAULT_SEGMENT_SLICE_MAX_CHARS = 6_000


@dataclass(frozen=True, slots=True)
class VideoSegmentRef:
    """Normalized timestamped subtitle, ASR, or visual observation segment."""

    part: int
    cid: str
    part_title: str
    kind: str
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class VideoChapterDigest:
    """Bounded chapter digest used when full video transcript exceeds the base budget."""

    part: int
    cid: str
    part_title: str
    start: float
    end: float
    segment_count: int
    char_count: int
    preview: str
    frame_notes: tuple[str, ...] = ()


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3000 <= code <= 0x30FF
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_tokens(text: str) -> int:
    """Fast CJK-aware token estimate for local base-context budgeting."""
    if not text:
        return 0
    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    non_cjk = "".join(ch for ch in text if not _is_cjk(ch))
    non_cjk_tokens = (len(non_cjk.encode("utf-8")) + 3) // 4
    return max(1, cjk_count + non_cjk_tokens)


def estimate_messages_tokens(
    messages: Sequence[dict[str, Any]],
    token_counter: Callable[[str], int] | None = None,
) -> int:
    counter = token_counter or estimate_tokens
    total = 0
    for msg in messages:
        total += 4
        content = msg.get("content")
        if isinstance(content, str):
            total += counter(content)
        elif content is not None:
            total += counter(json.dumps(content, ensure_ascii=False))
        if msg.get("tool_calls"):
            total += counter(json.dumps(msg["tool_calls"], ensure_ascii=False))
    return total


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) and num >= 0 else default


def _part_catalog(material: dict[str, Any]) -> dict[str, tuple[int, str]]:
    metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
    parts_meta = metadata.get("parts") if isinstance(metadata.get("parts"), list) else []
    catalog: dict[str, tuple[int, str]] = {}
    for index, entry in enumerate(parts_meta, start=1):
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("cid") or "").strip()
        if not cid:
            continue
        page = entry.get("page")
        page_num = int(page) if isinstance(page, int) and page >= 1 else index
        title = str(entry.get("part") or entry.get("title") or "").strip()
        catalog[cid] = (page_num, title)
    return catalog


def normalize_video_segments(material: dict[str, Any]) -> list[VideoSegmentRef]:
    """Extract normalized, sorted subtitle/ASR/visual segments from a video material dict."""
    if not isinstance(material, dict):
        return []
    catalog = _part_catalog(material)
    raw_parts = material.get("parts") if isinstance(material.get("parts"), list) else []
    segments: list[VideoSegmentRef] = []
    fallback_part = 1
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        cid = str(part.get("cid") or "").strip()
        if cid and cid in catalog:
            part_num, part_title = catalog[cid]
        else:
            raw_page = part.get("page")
            part_num = int(raw_page) if isinstance(raw_page, int) and raw_page >= 1 else fallback_part
            part_title = str(part.get("title") or part.get("part") or "").strip()
            if cid:
                catalog[cid] = (part_num, part_title)
            fallback_part += 1
        kind = str(part.get("kind") or "").strip()
        if not kind:
            if part.get("visual"):
                kind = "visual_observation"
            elif part.get("asr"):
                kind = "audio_transcript"
            else:
                kind = "subtitle"
        for seg in part.get("segments") or ():
            if not isinstance(seg, dict):
                continue
            text = " ".join(str(seg.get("text") or "").split())
            if not text:
                continue
            start = _safe_float(seg.get("start"), 0.0)
            end = max(start, _safe_float(seg.get("end"), start))
            segments.append(
                VideoSegmentRef(
                    part=part_num,
                    cid=cid,
                    part_title=part_title,
                    kind=kind,
                    start=start,
                    end=end,
                    text=text,
                )
            )
    kind_order = {"subtitle": 0, "audio_transcript": 1, "visual_observation": 2}
    segments.sort(key=lambda item: (item.part, item.start, kind_order.get(item.kind, 3), item.end))
    return segments


def _format_segment_line(seg: VideoSegmentRef) -> str:
    prefix = "画面" if seg.kind == "visual_observation" else "语音" if seg.kind == "audio_transcript" else "字幕"
    return f"[{prefix} {timestamp(seg.start)}] {seg.text}"


def build_video_chapter_digests(
    material: dict[str, Any],
    *,
    target_chapters: int = 10,
    max_window_seconds: float = 300.0,
) -> list[VideoChapterDigest]:
    """Group long video segments into bounded chapter digests for >24K fallback."""
    segments = normalize_video_segments(material)
    if not segments:
        return []
    metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
    explicit_chapters = metadata.get("chapters") if isinstance(metadata.get("chapters"), list) else []

    by_part: dict[int, list[VideoSegmentRef]] = {}
    for seg in segments:
        by_part.setdefault(seg.part, []).append(seg)

    digests: list[VideoChapterDigest] = []
    if explicit_chapters:
        for chapter in explicit_chapters:
            if not isinstance(chapter, dict):
                continue
            part_num = int(chapter.get("part") or 1)
            start = _safe_float(chapter.get("start"), 0.0)
            end = max(start, _safe_float(chapter.get("end"), start))
            matched = [
                seg for seg in by_part.get(part_num, ()) if seg.end >= start and seg.start <= end
            ]
            if not matched:
                continue
            text_segs = [seg for seg in matched if seg.kind != "visual_observation"]
            frame_segs = [seg for seg in matched if seg.kind == "visual_observation"]
            title = str(chapter.get("title") or matched[0].part_title or f"章节 {timestamp(start)}").strip()
            preview = _chapter_preview(text_segs or matched, headline=title)
            digests.append(
                VideoChapterDigest(
                    part=part_num,
                    cid=matched[0].cid,
                    part_title=matched[0].part_title,
                    start=start,
                    end=end,
                    segment_count=len(matched),
                    char_count=sum(len(seg.text) for seg in matched),
                    preview=preview,
                    frame_notes=tuple(f"[{timestamp(seg.start)}] {seg.text[:80]}" for seg in frame_segs[:3]),
                )
            )
        if digests:
            return digests

    per_part_target = max(2, min(target_chapters, max(2, target_chapters // max(1, len(by_part)))))
    for part_num in sorted(by_part):
        part_segs = by_part[part_num]
        part_start = part_segs[0].start
        part_end = max(seg.end for seg in part_segs)
        span = max(1.0, part_end - part_start)
        window = min(max_window_seconds, max(30.0, span / per_part_target))
        if span / window > per_part_target * 2:
            window = span / per_part_target
        buckets: dict[int, list[VideoSegmentRef]] = {}
        for seg in part_segs:
            idx = int((seg.start - part_start) // window)
            buckets.setdefault(idx, []).append(seg)
        for idx in sorted(buckets):
            bucket = buckets[idx]
            win_start = bucket[0].start
            win_end = max(seg.end for seg in bucket)
            text_segs = [seg for seg in bucket if seg.kind != "visual_observation"]
            frame_segs = [seg for seg in bucket if seg.kind == "visual_observation"]
            digests.append(
                VideoChapterDigest(
                    part=part_num,
                    cid=bucket[0].cid,
                    part_title=bucket[0].part_title,
                    start=win_start,
                    end=win_end,
                    segment_count=len(bucket),
                    char_count=sum(len(seg.text) for seg in bucket),
                    preview=_chapter_preview(text_segs or bucket),
                    frame_notes=tuple(f"[{timestamp(seg.start)}] {seg.text[:80]}" for seg in frame_segs[:3]),
                )
            )
    return digests


def _chapter_preview(segments: Sequence[VideoSegmentRef], *, headline: str = "") -> str:
    if not segments:
        return headline
    parts: list[str] = []
    if headline:
        parts.append(headline)
    head = segments[0].text[:110]
    parts.append(head)
    if len(segments) >= 3:
        mid = segments[len(segments) // 2].text[:90]
        if mid and mid != head:
            parts.append(mid)
    if len(segments) >= 2:
        tail = segments[-1].text[:90]
        if tail and tail != head:
            parts.append(tail)
    return " / ".join(parts)[:280]


def slice_video_segments(
    material: dict[str, Any],
    *,
    part: int | None = None,
    cid: str | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    max_chars: int = DEFAULT_SEGMENT_SLICE_MAX_CHARS,
) -> dict[str, Any]:
    """Fetch bounded timestamped subtitle/ASR/visual slices for `read_video_segment`."""
    metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
    bvid = str(metadata.get("bvid") or "")
    segments = normalize_video_segments(material)
    target_cid = str(cid).strip() if cid is not None else None
    start_bound = _safe_float(start_seconds, 0.0) if start_seconds is not None else None
    end_bound = _safe_float(end_seconds, 0.0) if end_seconds is not None else None

    filtered: list[VideoSegmentRef] = []
    for seg in segments:
        if part is not None and seg.part != part:
            continue
        if target_cid and seg.cid != target_cid:
            continue
        if start_bound is not None and seg.end < start_bound:
            continue
        if end_bound is not None and seg.start > end_bound:
            continue
        filtered.append(seg)

    lines: list[str] = []
    returned: list[dict[str, Any]] = []
    used = 0
    has_more = False
    next_start: float | None = None
    last_part: int | None = None

    for idx, seg in enumerate(filtered):
        line = _format_segment_line(seg)
        header = f"P{seg.part}" + (f" {seg.part_title}" if seg.part_title else "")
        need_header = seg.part != last_part
        projected = used + (len(header) + 1 if need_header else 0) + len(line) + 1
        if returned and projected > max(200, int(max_chars)):
            has_more = True
            next_start = seg.start
            break
        if need_header:
            lines.append(header)
            used += len(header) + 1
            last_part = seg.part
        lines.append(line)
        used += len(line) + 1
        returned.append(
            {
                "part": seg.part,
                "cid": seg.cid,
                "kind": seg.kind,
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
            }
        )
        if not returned and idx == len(filtered) - 1:
            break

    actual_start = returned[0]["start"] if returned else (start_bound or 0.0)
    actual_end = max((item["end"] for item in returned), default=(end_bound or actual_start))
    return {
        "ok": True,
        "bvid": bvid,
        "part": part,
        "cid": target_cid,
        "start_seconds": actual_start,
        "end_seconds": actual_end,
        "content": "\n".join(lines),
        "segment_count": len(returned),
        "has_more": has_more,
        "next_start_seconds": next_start,
        "segments": returned,
    }


def _video_header_lines(material: dict[str, Any], *, first_impression: str = "") -> list[str]:
    metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
    lines = ["【观看视频材料】"]
    if bvid := str(metadata.get("bvid") or "").strip():
        lines.append(f"- BV号：{bvid}")
    if title := str(metadata.get("title") or "").strip():
        lines.append(f"- 标题：{title}")
    owner = str(metadata.get("owner") or "").strip()
    owner_uid = str(metadata.get("owner_uid") or "").strip()
    if owner or owner_uid:
        uid_part = f" (UID:{owner_uid})" if owner_uid else ""
        lines.append(f"- UP主：{owner or '未知'}{uid_part}")
    if duration := metadata.get("duration"):
        lines.append(f"- 时长：{timestamp(_safe_float(duration))}（{int(_safe_float(duration))}秒）")
    tags = [str(tag).strip() for tag in (metadata.get("tags") or ()) if str(tag).strip()]
    if tags:
        lines.append(f"- 标签：{'、'.join(tags[:12])}")
    if desc := str(metadata.get("description") or "").strip():
        bounded_desc = desc if len(desc) <= 1200 else desc[:1200] + "…"
        lines.append(f"- 简介：{bounded_desc}")
    if impression := str(first_impression or material.get("first_impression") or "").strip():
        lines.append(f"- 初看印象：{impression}")
    return lines


def format_video_base_text(
    material: dict[str, Any],
    *,
    first_impression: str = "",
    max_tokens: int = DEFAULT_VIDEO_BASE_TOKEN_LIMIT,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[str, bool, int]:
    """Render watched video base text, degrading to chapter digests if > max_tokens."""
    counter = token_counter or estimate_tokens
    header_lines = _video_header_lines(material, first_impression=first_impression)
    segments = normalize_video_segments(material)

    if not segments:
        text = "\n".join([*header_lines, "", "（暂无可用字幕或画面观察记录）"])
        return text, False, counter(text)

    body_lines: list[str] = ["", "【完整字幕、转录与画面记录】"]
    current_part: int | None = None
    for seg in segments:
        if seg.part != current_part:
            part_label = f"P{seg.part}" + (f" {seg.part_title}" if seg.part_title else "")
            body_lines.append(f"## {part_label}")
            current_part = seg.part
        body_lines.append(_format_segment_line(seg))

    full_text = "\n".join([*header_lines, *body_lines])
    full_tokens = counter(full_text)
    if full_tokens <= max_tokens:
        return full_text, False, full_tokens

    digests = build_video_chapter_digests(material)
    digest_lines = [
        "",
        "【分章节摘要（全文较长已折叠）】",
        f"提示：完整字幕/转录约 {full_tokens} tokens，超过 {max_tokens} tokens 直载上限。"
        "若需查看某一段的逐字原话或细看画面，请调用 `read_video_segment`（传入 part、start_seconds、end_seconds）或 `inspect_video_frames`。",
    ]
    for chapter in digests:
        part_tag = f"P{chapter.part}" + (f" {chapter.part_title}" if chapter.part_title else "")
        line = (
            f"- {part_tag} [{timestamp(chapter.start)}-{timestamp(chapter.end)}] "
            f"(start_seconds={int(chapter.start)}, end_seconds={int(math.ceil(chapter.end))}, "
            f"{chapter.segment_count}段): {chapter.preview}"
        )
        digest_lines.append(line)
        for note in chapter.frame_notes:
            digest_lines.append(f"  · 画面观察 {note}")

    degraded_text = "\n".join([*header_lines, *digest_lines])
    return degraded_text, True, counter(degraded_text)


def _format_memories_block(memories: Iterable[MemoryItem | dict[str, Any] | str], scope: MemoryScope) -> str:
    lines: list[str] = []
    for item in memories:
        if isinstance(item, MemoryItem):
            if not item.active:
                continue
            text = item.content.strip()
        elif isinstance(item, dict):
            if item.get("active") is False:
                continue
            text = str(item.get("content") or item.get("text") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            lines.append(f"- {text}")
    if not lines:
        return ""
    heading = "【已保存的私有长期记忆】" if scope == MemoryScope.OWNER else "【已保存的公开长期记忆】"
    return (
        f"{heading}\n"
        + "\n".join(lines)
        + "\n（作为自然背景参考即可，勿生硬复读。）"
    )


def build_base_node(
    character: dict[str, Any],
    *,
    channel: str = "video_browsing",
    self_actor: dict[str, Any] | None = None,
    operating_account: dict[str, Any] | None = None,
    speaker: dict[str, Any] | None = None,
    situation: str | None = None,
    extra_boundaries: Sequence[str] = (),
    extra_context: dict[str, Any] | None = None,
    memories: Iterable[MemoryItem | dict[str, Any] | str] = (),
    memory_scope: str | MemoryScope = MemoryScope.PUBLIC,
    video_material: dict[str, Any] | None = None,
    first_impression: str = "",
    extra_base_blocks: Sequence[str] = (),
    max_video_tokens: int = DEFAULT_VIDEO_BASE_TOKEN_LIMIT,
    token_counter: Callable[[str], int] | None = None,
    node_id: str | None = None,
    scope: str = "",
    title: str = "",
    scene: str | None = None,
    task_intent: str | None = None,
    include_persona_shell: bool = True,
    compact: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ContextNode:
    """Create an immutable Base ContextNode shared identically across all child branches."""
    resolved_scope = parse_memory_scope(memory_scope)
    profile = build_metacognitive_profile(
        character,
        channel=channel,
        scene=scene,
        task_intent=task_intent,
        self_actor=self_actor,
        operating_account=operating_account,
        speaker=speaker,
        situation=situation,
        extra_boundaries=extra_boundaries,
        extra_context=extra_context,
    )
    sys_parts = [
        render_self_model_prompt(
            profile,
            character=character,
            include_persona_shell=include_persona_shell,
            compact=compact,
        )
    ]
    if mem_block := _format_memories_block(memories, resolved_scope):
        sys_parts.append(mem_block)
    for block in extra_base_blocks:
        if isinstance(block, str) and block.strip():
            sys_parts.append(block.strip())

    messages: list[dict[str, Any]] = [{"role": "system", "content": "\n\n".join(sys_parts)}]
    degraded = False
    meta = dict(metadata or {})

    if video_material is not None:
        video_text, degraded, video_tokens = format_video_base_text(
            video_material,
            first_impression=first_impression,
            max_tokens=max_video_tokens,
            token_counter=token_counter,
        )
        messages.append({"role": "user", "content": video_text})
        vmeta = video_material.get("metadata") if isinstance(video_material.get("metadata"), dict) else {}
        meta.setdefault("bvid", vmeta.get("bvid", ""))
        meta.setdefault("video_tokens", video_tokens)
        if not title:
            title = str(vmeta.get("title") or vmeta.get("bvid") or "")
        if not scope and vmeta.get("bvid"):
            scope = f"video:{vmeta['bvid']}"
    if first_impression and str(first_impression).strip():
        messages.append({"role": "assistant", "content": str(first_impression).strip()})

    total_tokens = estimate_messages_tokens(messages, token_counter=token_counter)
    return ContextNode(
        node_id=node_id or f"base:{uuid4().hex[:12]}",
        kind="base",
        parent_id=None,
        scope=scope or f"base:{channel}:{character.get('id', '')}",
        channel=channel,
        title=title or profile.scene,
        messages=tuple(messages),
        metadata=meta,
        token_estimate=total_tokens,
        degraded=degraded,
    )


def create_context_tree(base_node: ContextNode, *, tree_id: str | None = None) -> ContextTree:
    """Create a ContextTree anchored at an immutable Base ContextNode."""
    if base_node.kind != "base" and base_node.parent_id is not None:
        raise ValueError("Root node of ContextTree must be a Base node with parent_id=None")
    return ContextTree(
        tree_id=tree_id or f"tree:{base_node.node_id}",
        root_id=base_node.node_id,
        nodes={base_node.node_id: base_node},
    )


def _resolve_parent_id(tree: ContextTree, parent_id: str | None) -> str:
    pid = tree.root_id if parent_id is None else parent_id
    if tree.get(pid) is None:
        raise KeyError(f"Parent node {pid!r} not found")
    return pid


def _branch_node(
    tree: ContextTree,
    *,
    parent_id: str | None,
    branch_id: str | None,
    channel: str,
    scope: str,
    title: str,
    messages: Sequence[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> ContextNode:
    pid = _resolve_parent_id(tree, parent_id)
    node = ContextNode(
        node_id=branch_id or f"branch:{channel}:{uuid4().hex[:12]}",
        kind="branch",
        parent_id=pid,
        scope=scope,
        channel=channel,
        title=title,
        messages=tuple(messages),
        metadata=dict(metadata or {}),
        token_estimate=estimate_messages_tokens(messages),
    )
    return tree.add_node(node)


def fork_top_level_comment_branch(
    tree: ContextTree,
    *,
    parent_id: str | None = None,
    existing_hot_comments: Sequence[dict[str, Any] | str] = (),
    objective: str = "",
    extra_context: dict[str, Any] | None = None,
    branch_id: str | None = None,
) -> ContextNode:
    """Fork a top-level video comment/reaction branch sharing the immutable Video Base."""
    lines = ["【当前分支：视频评论区主评】"]
    if existing_hot_comments:
        lines.append("当前评论区已有部分热门/近期评论（避免跟风复读相同内容）：")
        for idx, item in enumerate(existing_hot_comments[:10], start=1):
            if isinstance(item, dict):
                author = str(item.get("author_name") or item.get("display_name") or "评论者").strip()
                text = " ".join(str(item.get("text") or item.get("content") or "").split())[:160]
                lines.append(f"{idx}. {author}：{text}")
            else:
                lines.append(f"{idx}. {' '.join(str(item).split())[:160]}")
    if extra_context:
        lines.append("补充状态：" + json.dumps(extra_context, ensure_ascii=False, separators=(",", ":")))
    lines.append(
        objective.strip()
        or "请结合看过的视频内容，以自然第一人称决定是否发一条主评。"
        "想发就直接输出纯文本评论（或先按需调用工具查证/看帧/补读片段/点赞）；不发则调用 skip_reply。"
    )
    return _branch_node(
        tree,
        parent_id=parent_id,
        branch_id=branch_id,
        channel="top_level_comment",
        scope="top_level",
        title="视频主评分支",
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )


def _comment_fields(row: dict[str, Any]) -> dict[str, Any]:
    cid = str(row.get("comment_id") or row.get("id") or "").strip()
    root = str(row.get("thread_id") or row.get("root_id") or cid).strip()
    parent = row.get("parent_comment_id") if "parent_comment_id" in row else row.get("parent_id")
    parent_str = str(parent).strip() if parent not in (None, "", 0, "0") else None
    uid = str(row.get("author_uid") or row.get("platform_user_id") or row.get("user_id") or "").strip()
    name = str(row.get("author_name") or row.get("display_name") or row.get("username") or "").strip()
    text = str(row.get("text") if row.get("text") is not None else (row.get("content") or "")).strip()
    return {
        "comment_id": cid,
        "root_id": root,
        "parent_id": parent_str,
        "author_uid": uid,
        "author_name": name or "评论者",
        "text": text,
        "speaker_role": row.get("speaker_role") or row.get("role"),
        "publication": row.get("publication"),
        "is_owner_takeover": bool(row.get("is_owner_takeover")),
    }


def _classify_floor_speaker(
    item: dict[str, Any],
    *,
    self_uid: str | None,
    confirmed_ids: set[str],
    uploader_uid: str | None,
    owner_uid: str | None,
) -> tuple[str, str]:
    cid = item["comment_id"]
    uid = item["author_uid"]
    explicit_role = str(item.get("speaker_role") or "").strip()

    if explicit_role in {"oc", "assistant", "小鲸鱼"} or item.get("publication") == "confirmed" or (
        cid and cid in confirmed_ids
    ):
        return "assistant", "小鲸鱼"
    if (
        item.get("is_owner_takeover")
        or explicit_role in {"operator", "号主人工接管", "人工接管"}
        or (self_uid and uid == str(self_uid))
    ):
        return "user", "人工接管"
    if uploader_uid and uid == str(uploader_uid):
        return "user", "UP主"
    if owner_uid and uid == str(owner_uid):
        return "user", "账号所有者"
    return "user", "评论者"


def fork_comment_floor_branch(
    tree: ContextTree,
    *,
    root_id: str,
    timeline: Sequence[dict[str, Any]],
    reply_target: dict[str, Any] | None = None,
    self_uid: str | None = None,
    confirmed_bot_comment_ids: Iterable[str] = (),
    uploader_uid: str | None = None,
    owner_uid: str | None = None,
    parent_id: str | None = None,
    branch_id: str | None = None,
    objective: str = "",
    extra_context: dict[str, Any] | None = None,
) -> ContextNode:
    """Fork a per-root_id comment floor branch including owner manual takeover turns."""
    root_str = str(root_id).strip()
    confirmed = {str(cid) for cid in (confirmed_bot_comment_ids or ()) if str(cid).strip()}
    normalized_items = [_comment_fields(row) for row in timeline]
    target = _comment_fields(reply_target) if reply_target else (normalized_items[-1] if normalized_items else None)
    by_id = {item["comment_id"]: item for item in normalized_items if item["comment_id"]}

    messages: list[dict[str, Any]] = []
    header = [f"【当前分支：评论楼内对话 (root_id={root_str})】"]
    if extra_context:
        header.append("楼层上下文：" + json.dumps(extra_context, ensure_ascii=False, separators=(",", ":")))
    header.append(
        "说明：下面按时间顺序列出本楼对话。你本人此前确认发出的回复会作为 assistant 轮次；"
        "同一账号下由维护者手动代回的发言会标记为 [人工接管]，不要把它当成你自己说过的话，也不要重复已经答过的内容。"
    )
    messages.append({"role": "user", "content": "\n".join(header)})

    seen_ids: set[str] = set()
    for item in normalized_items:
        if item["comment_id"]:
            seen_ids.add(item["comment_id"])
        role, role_label = _classify_floor_speaker(
            item,
            self_uid=self_uid,
            confirmed_ids=confirmed,
            uploader_uid=uploader_uid,
            owner_uid=owner_uid,
        )
        if role == "assistant":
            messages.append({"role": "assistant", "content": item["text"]})
            continue
        reply_hint = ""
        if item["parent_id"] and item["parent_id"] != item["comment_id"]:
            parent_row = by_id.get(item["parent_id"])
            if parent_row:
                reply_hint = f"(回复 {parent_row['author_name']}) "
            else:
                reply_hint = f"(回复 #{item['parent_id']}) "
        uid_note = f" (UID:{item['author_uid']})" if item["author_uid"] else ""
        target_flag = " [当前待回应]" if target and item["comment_id"] and item["comment_id"] == target["comment_id"] else ""
        messages.append(
            {
                "role": "user",
                "content": f"[{role_label} · {item['author_name']}{uid_note}]{target_flag} {reply_hint}{item['text']}",
            }
        )

    if target and (not target["comment_id"] or target["comment_id"] not in seen_ids):
        _, target_label = _classify_floor_speaker(
            target,
            self_uid=self_uid,
            confirmed_ids=confirmed,
            uploader_uid=uploader_uid,
            owner_uid=owner_uid,
        )
        uid_note = f" (UID:{target['author_uid']})" if target["author_uid"] else ""
        messages.append(
            {
                "role": "user",
                "content": f"[{target_label} · {target['author_name']}{uid_note}] [当前待回应] {target['text']}",
            }
        )

    cue_lines = []
    if target:
        cue_lines.append(
            f"请针对 {target['author_name']}"
            + (f" (UID:{target['author_uid']})" if target["author_uid"] else "")
            + f" 的这句原话：「{target['text']}」"
        )
    cue_lines.append(
        objective.strip()
        or "决定下一步行动：若要公开回复，直接输出自然第一人称正文（不要输出 JSON 或引用编号）；"
        "若需查证或跳过，调用对应工具（如 search_web、read_video_segment、inspect_video_frames、skip_reply、queue_dm_handoff）。"
    )
    messages.append({"role": "user", "content": "\n".join(cue_lines)})

    return _branch_node(
        tree,
        parent_id=parent_id,
        branch_id=branch_id,
        channel="comment_floor",
        scope=f"floor:{root_str}",
        title=f"评论楼 #{root_str}",
        messages=messages,
        metadata={"root_id": root_str, "target_comment_id": target["comment_id"] if target else ""},
    )


def fork_dm_session_branch(
    tree: ContextTree,
    *,
    peer_uid: str,
    peer_name: str = "",
    conversation: Sequence[dict[str, Any]] = (),
    open_messages: Sequence[dict[str, Any]] = (),
    source_comment: dict[str, Any] | None = None,
    parent_id: str | None = None,
    branch_id: str | None = None,
    objective: str = "",
) -> ContextNode:
    """Fork a DM session branch including owner manual takeover turns and pending inbox items."""
    peer_uid_str = str(peer_uid).strip()
    peer_label = str(peer_name or "").strip() or "对方"
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"【当前分支：平台私信会话 · 对方 {peer_label} (UID:{peer_uid_str})】\n"
                "按时间顺序给出私信来往记录；标记为 [人工接管回复] 的消息由账号维护者手动代回，"
                "你只需顺着最新语境自然承接标有 [待回复] 的来信，不要重复已经回答过的内容。"
            ),
        }
    ]
    if source_comment:
        src_bits = ["[触发本次私信的公开评论来源]"]
        if bvid := source_comment.get("bvid") or source_comment.get("video_id"):
            src_bits.append(f"视频={bvid}")
        if root := source_comment.get("root_id") or source_comment.get("thread_id"):
            src_bits.append(f"评论楼=#{root}")
        if quote := source_comment.get("quote") or source_comment.get("text"):
            src_bits.append(f"原话：「{quote}」")
        if purpose := source_comment.get("purpose"):
            src_bits.append(f"约定/事由：{purpose}")
        messages.append({"role": "user", "content": " ".join(src_bits)})

    seen_ids: set[str] = set()
    for item in conversation:
        msg_id = str(item.get("message_id") or item.get("id") or "").strip()
        if msg_id:
            seen_ids.add(msg_id)
        speaker = str(item.get("origin") or item.get("speaker") or item.get("role") or "viewer").strip().lower()
        text = str(item.get("text") if item.get("text") is not None else (item.get("content") or "")).strip()
        if not text:
            continue
        if speaker in {"oc", "assistant"}:
            messages.append({"role": "assistant", "content": text})
        elif speaker == "operator":
            messages.append({"role": "user", "content": f"[人工接管回复] {text}"})
        else:
            open_flag = "[待回复] " if item.get("open") else ""
            messages.append({"role": "user", "content": f"[私信 · {peer_label}] {open_flag}{text}"})

    for item in open_messages:
        msg_id = str(item.get("message_id") or item.get("id") or "").strip()
        if msg_id and msg_id in seen_ids:
            continue
        text = str(item.get("text") if item.get("text") is not None else (item.get("content") or "")).strip()
        if text:
            messages.append({"role": "user", "content": f"[私信 · {peer_label}] [待回复] {text}"})

    messages.append(
        {
            "role": "user",
            "content": objective.strip()
            or "请结合以上会话，直接输出自然第一人称私信正文（不要输出 JSON 或内部标签）；"
            "若来信已由人工接管完整回答或无需回复，请调用 skip_reply。",
        }
    )
    return _branch_node(
        tree,
        parent_id=parent_id,
        branch_id=branch_id,
        channel="dm_session",
        scope=f"dm:{peer_uid_str}",
        title=f"私信会话 {peer_label}",
        messages=messages,
        metadata={"peer_uid": peer_uid_str, "peer_name": peer_label},
    )


def fork_owner_chat_branch(
    tree: ContextTree,
    *,
    history: Sequence[dict[str, Any]] = (),
    current_message: str = "",
    extra_context: dict[str, Any] | None = None,
    parent_id: str | None = None,
    branch_id: str | None = None,
) -> ContextNode:
    """Fork an owner chat branch from a Base node while keeping Base prefix byte-identical."""
    messages: list[dict[str, Any]] = []
    for item in history:
        role = str(item.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            continue
        copied = deepcopy(dict(item))
        messages.append(copied)
    if extra_context:
        messages.append(
            {
                "role": "user",
                "content": "补充上下文：" + json.dumps(extra_context, ensure_ascii=False, separators=(",", ":")),
            }
        )
    if current_message:
        messages.append({"role": "user", "content": current_message})
    return _branch_node(
        tree,
        parent_id=parent_id,
        branch_id=branch_id,
        channel="owner_chat",
        scope="owner_chat",
        title="日常私聊分支",
        messages=messages,
    )


def append_branch_turn(tree: ContextTree, branch_id: str, message: dict[str, Any]) -> ContextNode:
    """Append a user, assistant, or tool message to a branch without mutating its Base parent."""
    node = tree.get(branch_id)
    if node is None:
        raise KeyError(f"Unknown branch {branch_id!r}")
    if node.node_id == tree.root_id:
        raise ValueError("Base node is immutable; fork a child branch before appending turns")
    role = str(message.get("role") or "").strip()
    if role not in {"user", "assistant", "tool"}:
        raise ValueError(f"Branch turns only allow user/assistant/tool roles, got {role!r}")
    updated_messages = (*node.messages, deepcopy(dict(message)))
    updated = ContextNode(
        node_id=node.node_id,
        kind=node.kind,
        parent_id=node.parent_id,
        scope=node.scope,
        channel=node.channel,
        title=node.title,
        messages=updated_messages,
        metadata=node.metadata,
        token_estimate=estimate_messages_tokens(updated_messages),
        degraded=node.degraded,
        created_at=node.created_at,
    )
    return tree.replace_branch(updated)


def _normalize_tool_call(call: Any) -> dict[str, Any]:
    if hasattr(call, "call_id") and hasattr(call, "name") and hasattr(call, "arguments"):
        call_id = str(call.call_id)
        name = str(call.name)
        args = call.arguments
    elif isinstance(call, dict):
        if "function" in call and isinstance(call["function"], dict):
            call_id = str(call.get("id") or call.get("call_id") or f"call_{uuid4().hex[:12]}")
            name = str(call["function"].get("name") or "")
            args = call["function"].get("arguments", "{}")
        else:
            call_id = str(call.get("call_id") or call.get("id") or f"call_{uuid4().hex[:12]}")
            name = str(call.get("name") or "")
            args = call.get("arguments", "{}")
    else:
        raise ValueError("Unsupported tool call structure")
    if not name:
        raise ValueError("Tool call missing function name")
    args_str = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, allow_nan=False)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


def append_assistant_tool_calls(
    tree: ContextTree,
    branch_id: str,
    tool_calls: Sequence[Any],
    *,
    content: str = "",
) -> ContextNode:
    """Append an OpenAI assistant tool_calls turn onto a branch."""
    normalized = [_normalize_tool_call(call) for call in tool_calls]
    if not normalized:
        raise ValueError("tool_calls cannot be empty")
    return append_branch_turn(
        tree,
        branch_id,
        {"role": "assistant", "content": content or "", "tool_calls": normalized},
    )


def append_tool_result(
    tree: ContextTree,
    branch_id: str,
    *,
    tool_call_id: str,
    name: str,
    result: Any,
) -> ContextNode:
    """Append an OpenAI role='tool' result turn onto a branch."""
    serialized = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, allow_nan=False)
    return append_branch_turn(
        tree,
        branch_id,
        {"role": "tool", "tool_call_id": str(tool_call_id), "name": str(name), "content": serialized},
    )


def render_openai_messages(tree: ContextTree, node_id: str | None = None) -> list[dict[str, Any]]:
    """Render standard OpenAI Chat Completions multi-turn messages (system/user/assistant/tool)."""
    return tree.render_messages(node_id)


def to_responses_input(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard OpenAI Chat messages (including tool_calls/tool) into Responses API input items."""
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(msg.get("tool_call_id") or ""),
                    "output": msg.get("content")
                    if isinstance(msg.get("content"), str)
                    else json.dumps(msg.get("content"), ensure_ascii=False),
                }
            )
            continue
        if role == "assistant" and msg.get("tool_calls"):
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                items.append({"role": "assistant", "content": text})
            for call in msg["tool_calls"]:
                fn = call.get("function") or {}
                args = fn.get("arguments", "{}")
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or call.get("call_id") or ""),
                        "name": str(fn.get("name") or ""),
                        "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                    }
                )
            continue
        items.append({"role": role, "content": msg.get("content", "")})
    return items


__all__ = [
    "DEFAULT_SEGMENT_SLICE_MAX_CHARS",
    "DEFAULT_VIDEO_BASE_TOKEN_LIMIT",
    "VideoChapterDigest",
    "VideoSegmentRef",
    "append_assistant_tool_calls",
    "append_branch_turn",
    "append_tool_result",
    "build_base_node",
    "build_video_chapter_digests",
    "create_context_tree",
    "estimate_messages_tokens",
    "estimate_tokens",
    "fork_comment_floor_branch",
    "fork_dm_session_branch",
    "fork_owner_chat_branch",
    "fork_top_level_comment_branch",
    "format_video_base_text",
    "normalize_video_segments",
    "render_openai_messages",
    "slice_video_segments",
    "to_responses_input",
]
