"""Build an attribution-preserving video script from a frozen public operations recap."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from ...core.models import StrictModel, content_hash
from .remotion_assets import (
    DEFAULT_LINE_PAUSE_SECONDS,
    DEFAULT_OUTRO_HOLD_SECONDS,
    DEFAULT_SCENE_PAUSE_SECONDS,
    PublicSourceRef,
    VideoLineSpec,
    VideoSceneSpec,
)
from .remotion_planner import normalize_cinematic_scene_defaults

RecapTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
RecapCount = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
RecapSourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
RecapNarration = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]

RECAP_TARGET_MIN_SECONDS = 90.0
RECAP_TARGET_MAX_SECONDS = 300.0
RECAP_MIN_SCENES = 7
RECAP_MAX_SCENES = 14

_EDITORIAL_STATUS = {
    "watched_video": ("watched",),
    "public_reply": ("public_visible",),
    "public_comment": ("public_visible",),
    "confirmed_like": ("confirmed",),
    "public_dynamic": ("confirmed", "public_visible"),
    "pending_comment": ("pending_review",),
    "pending_dynamic": ("pending_review",),
}
_RECAP_TAGS = ["大肥鱼", "deepseek", "运营复盘", "视频盘点"]
_PUBLIC_LINK = re.compile(r"https?://[^\s<>\"'“”‘’，。！？、；]+", re.IGNORECASE)
_QUOTED_PHRASE_RE = re.compile(r"[「『《“\"]([^」』》”\"\n]{2,16})[」』》”\"]")


class OperationsRecapHighlight(StrictModel):
    title: RecapTitle
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    source_id: RecapSourceId
    credit: str = Field(default="", max_length=80)


class OperationsRecapEditorialItem(StrictModel):
    source_id: RecapSourceId
    kind: Literal[
        "watched_video", "public_reply", "public_comment", "confirmed_like", "public_dynamic",
        "pending_comment", "pending_dynamic",
    ]
    status: Literal["watched", "public_visible", "confirmed", "pending_review"]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    creator: str | None = None
    excerpt: str | None = None
    excerpt_kind: str | None = None
    excerpt_url: str | None = None
    target_excerpt: str | None = None
    target_author: str | None = None
    target_url: str | None = None
    occurred_at: float | None = None
    in_current_run: bool | None = None
    earlier_in_week: bool | None = None
    url: str | None = None
    bvid: str | None = None

    @model_validator(mode="after")
    def validate_attribution(self):
        if self.status not in _EDITORIAL_STATUS[self.kind]:
            raise ValueError("剪辑素材类型与核实状态不符")
        if self.excerpt_url and (self.kind != "watched_video" or not self.url or not re.fullmatch(
            re.escape(self.url) + r"\?p=[1-9]\d*&t=\d+", self.excerpt_url,
        )):
            raise ValueError("字幕时间链接必须指向同一条观看原片")
        return self


class OperationsRecapPassage(StrictModel):
    source_id: RecapSourceId
    section: Literal["deep_dive", "side_cut"] = "deep_dive"
    chapter_title: str = Field(default="", max_length=40)
    template_hint: Literal[
        "QuoteSpotlight",
        "UIShowcase",
        "ChartCompare",
        "MemeCutaway",
        "TimelineSummary",
        "KineticStatement",
    ] | None = None
    narration: RecapNarration
    key_points: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=4,
    )


class OperationsRecapEditorial(StrictModel):
    title: RecapTitle
    opening: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=220)]
    passages: list[OperationsRecapPassage] = Field(min_length=1, max_length=10)
    closing: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=220)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None = None
    dynamic_post: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=450)] | None = None
    dynamic_source_ids: list[RecapSourceId] = Field(default_factory=list, max_length=2)


class OperationsRecapBrief(StrictModel):
    title: RecapTitle
    period_label: RecapTitle
    watched_videos: RecapCount
    public_comments: RecapCount
    accepted_comments: RecapCount | None = None
    public_dynamics: RecapCount
    review_dynamics: RecapCount | None = None
    review_comments: RecapCount | None = None
    confirmed_likes: RecapCount | None = None
    sources: list[PublicSourceRef] = Field(default_factory=list, max_length=8)
    highlights: list[OperationsRecapHighlight] = Field(default_factory=list, max_length=4)
    reflection: str = Field(default="", max_length=120)
    run_period_label: RecapTitle | None = None
    current_run: dict[str, RecapCount] | None = None
    editorial_items: list[OperationsRecapEditorialItem] = Field(default_factory=list)
    editorial: OperationsRecapEditorial | None = None

    @model_validator(mode="after")
    def validate_comment_readback(self):
        if self.accepted_comments is not None and self.public_comments > self.accepted_comments:
            raise ValueError("公开读回评论数不能超过取得回执的评论数")
        return self


def estimate_recap_duration_seconds(
    scenes: list[VideoSceneSpec] | list[dict[str, Any]],
    *,
    line_pause_seconds: float = DEFAULT_LINE_PAUSE_SECONDS,
    scene_pause_seconds: float = DEFAULT_SCENE_PAUSE_SECONDS,
    outro_hold_seconds: float = DEFAULT_OUTRO_HOLD_SECONDS,
) -> float:
    """Estimate rendered recap duration conservatively using dry-run TTS pacing plus pauses."""
    total = 0.0
    num_scenes = len(scenes)
    for s_idx, scene in enumerate(scenes):
        lines = scene.lines if hasattr(scene, "lines") else (scene.get("lines") or [])
        num_lines = len(lines)
        for l_idx, line in enumerate(lines):
            text = line.text if hasattr(line, "text") else str(line.get("text") or "")
            total += max(0.6, min(12.0, len(text.strip()) * 0.16))
            if l_idx < num_lines - 1:
                total += line_pause_seconds
        total += scene_pause_seconds if s_idx < num_scenes - 1 else outro_hold_seconds
    return round(total, 2)


def _display_excerpt(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _line_emphasis(text: str, key_points: list[str] | tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    for kp in key_points:
        clean = kp.strip()
        if 1 <= len(clean) <= 24 and clean in text and clean not in found:
            found.append(clean)
    for match in _QUOTED_PHRASE_RE.findall(text):
        clean = match.strip()
        if 2 <= len(clean) <= 24 and clean in text and clean not in found:
            found.append(clean)
    return found[:2]


def _spoken_lines(
    narration: str,
    cue: Literal["curious", "thinking", "calm", "proud"],
    *,
    key_points: list[str] | tuple[str, ...] = (),
) -> list[VideoLineSpec]:
    """Keep each caption short while retaining the narrator's original phrasing."""
    phrases = re.findall(r"[^，。！？；!?;]+[，。！？；!?;]*|[，。！？；!?;]+", narration)
    chunks: list[str] = []
    current = ""
    for phrase in phrases:
        if current and len(current) + len(phrase) > 38:
            chunks.append(current)
            current = ""
        while len(phrase) > 38:
            chunks.append(phrase[:38])
            phrase = phrase[38:]
        current += phrase
    if current:
        chunks.append(current)
    return [
        VideoLineSpec(text=text, avatar_cue=cue, emphasis=_line_emphasis(text, key_points))
        for text in chunks
        if text.strip()
    ]


def _pick_scene_template(
    item: OperationsRecapEditorialItem,
    passage: OperationsRecapPassage,
    *,
    prev_template: str,
    has_excerpt: bool,
    index: int,
) -> str:
    hint = passage.template_hint
    if hint and hint != prev_template:
        if item.status != "pending_review" or hint in {"MemeCutaway", "QuoteSpotlight"}:
            return hint
    if item.status == "pending_review":
        return "MemeCutaway" if prev_template != "MemeCutaway" else "QuoteSpotlight"
    if passage.section == "side_cut":
        for cand in ("MemeCutaway", "QuoteSpotlight", "TimelineSummary", "UIShowcase"):
            if cand != prev_template:
                return cand
    if item.kind == "watched_video":
        pool = ("QuoteSpotlight", "UIShowcase", "MemeCutaway", "ChartCompare") if has_excerpt else ("UIShowcase", "MemeCutaway", "QuoteSpotlight")
        for cand in pool[index % len(pool):] + pool[:index % len(pool)]:
            if cand != prev_template:
                return cand
    if item.status == "confirmed":
        return "MemeCutaway" if prev_template != "MemeCutaway" else "QuoteSpotlight"
    for cand in ("QuoteSpotlight", "MemeCutaway", "UIShowcase"):
        if cand != prev_template:
            return cand
    return "QuoteSpotlight"


def _make_evidence_scene(
    item: OperationsRecapEditorialItem,
    passage: OperationsRecapPassage,
    index: int,
    *,
    prev_template: str = "",
) -> dict[str, Any]:
    heading = _display_excerpt(item.title, 54)
    key_points = [_display_excerpt(kp, 80) for kp in passage.key_points[:4] if kp.strip()]
    base: dict[str, Any] = {"scene_id": f"recap_editorial_{index}", "heading": heading}
    if item.status == "pending_review":
        tpl = _pick_scene_template(item, passage, prev_template=prev_template, has_excerpt=False, index=index)
        vp: dict[str, Any] = {"citation_label": "待审核 · 尚未发布"}
        if key_points:
            vp["bullets"] = key_points
        return base | {
            "template": tpl,
            "chapter_title": _display_excerpt(passage.chapter_title, 40) or ("评论选题" if item.kind == "pending_comment" else "动态选题"),
            "avatar_cue": "thinking",
            "visual_props": vp,
            "lines": [line.model_dump(mode="json") for line in _spoken_lines(passage.narration, "thinking", key_points=key_points)],
        }
    if item.kind == "watched_video":
        creator = _display_excerpt(item.creator, 45)
        excerpt = _display_excerpt(item.excerpt, 88)
        label = {"video_description": "原片简介", "subtitle": "原片字幕", "automatic_subtitle": "自动字幕"}.get(item.excerpt_kind)
        subtitle = f"{label} · {excerpt}" if excerpt and label else excerpt
        tpl = _pick_scene_template(item, passage, prev_template=prev_template, has_excerpt=bool(subtitle), index=index)
        vp = {
            "subtitle": subtitle or (key_points[0] if key_points else heading),
            "citation_label": f"UP 主 {creator}" if creator else "B 站投稿",
        }
        if key_points:
            vp["bullets"] = key_points
        elif tpl == "UIShowcase":
            vp["bullets"] = [heading[:48], vp["citation_label"]]
        if tpl == "UIShowcase":
            vp["ui_focus_regions"] = [
                {"x": 0.28, "y": 0.30, "zoom": 1.32, "label": (key_points[0] if key_points else "核心看点")[:40], "line_index": 0},
                {"x": 0.68, "y": 0.56, "zoom": 1.42, "label": (key_points[1] if len(key_points) > 1 else "细节对照")[:40], "line_index": 1},
            ]
        return base | {
            "template": tpl,
            "chapter_title": _display_excerpt(passage.chapter_title, 40) or ("本轮观看" if item.in_current_run else "本周观看"),
            "avatar_cue": "curious",
            "visual_props": vp,
            "lines": [line.model_dump(mode="json") for line in _spoken_lines(passage.narration, "curious", key_points=key_points)],
        }
    if item.status == "confirmed":
        liked = item.kind == "confirmed_like"
        tpl = _pick_scene_template(item, passage, prev_template=prev_template, has_excerpt=liked, index=index)
        vp = {"subtitle": _display_excerpt(item.target_excerpt, 88) if liked else ""}
        if key_points and liked:
            vp["bullets"] = key_points
        return base | {
            "template": tpl,
            "chapter_title": _display_excerpt(passage.chapter_title, 40) or ("点赞回执" if liked else "动态发布回执"),
            "avatar_cue": "calm",
            "visual_props": vp,
            "lines": [line.model_dump(mode="json") for line in _spoken_lines(passage.narration, "calm", key_points=key_points)],
        }
    credit = {
        "public_reply": "本账号的公开回复",
        "public_comment": "本账号的公开评论",
        "public_dynamic": "本账号的公开动态",
    }[item.kind]
    target = _display_excerpt(item.target_excerpt, 72)
    excerpt = _display_excerpt(item.excerpt, 100)
    vp = {"subtitle": excerpt, "citation_label": credit}
    bullets: list[str] = []
    if target and item.kind == "public_reply":
        label = f"原楼留言（{_display_excerpt(item.target_author, 24)}）" if item.target_author else "原楼留言"
        bullets.append(f"{label} · {target}")
    bullets.extend(kp for kp in key_points if kp not in bullets)
    if bullets:
        vp["bullets"] = bullets[:4]
    tpl = _pick_scene_template(item, passage, prev_template=prev_template, has_excerpt=bool(excerpt), index=index)
    if tpl == "UIShowcase":
        vp["ui_focus_regions"] = [
            {"x": 0.30, "y": 0.32, "zoom": 1.32, "label": "原楼观点", "line_index": 0},
            {"x": 0.66, "y": 0.58, "zoom": 1.40, "label": "公开回复", "line_index": 1},
        ]
    return base | {
        "template": tpl,
        "chapter_title": _display_excerpt(passage.chapter_title, 40) or "公开页已核实",
        "avatar_cue": "thinking",
        "visual_props": vp,
        "lines": [line.model_dump(mode="json") for line in _spoken_lines(passage.narration, "thinking", key_points=key_points)],
    }


def _group_chapters(
    deep_passages: list[OperationsRecapPassage],
    items: dict[str, OperationsRecapEditorialItem],
) -> list[tuple[str, list[OperationsRecapPassage]]]:
    groups: list[tuple[str, list[OperationsRecapPassage]]] = []
    for idx, passage in enumerate(deep_passages, 1):
        item = items[passage.source_id]
        chap_title = _display_excerpt(passage.chapter_title, 40) or _display_excerpt(item.title, 28) or f"话题 {idx}"
        if groups and groups[-1][0] == chap_title:
            groups[-1][1].append(passage)
        else:
            groups.append((chap_title, [passage]))
    return groups


def _editorial_plan_fields(brief: OperationsRecapBrief) -> dict:
    editorial = brief.editorial
    assert editorial is not None
    items = {item.source_id: item for item in brief.editorial_items}
    if len(items) != len(brief.editorial_items):
        raise ValueError("剪辑素材编号须唯一")
    source_ids = [passage.source_id for passage in editorial.passages]
    passage_keys = [(p.source_id, p.section, p.chapter_title) for p in editorial.passages]
    if len(passage_keys) != len(set(passage_keys)):
        raise ValueError("剪辑不能重复引用同一素材")

    references: list[PublicSourceRef] = []
    seen_refs: set[str] = set()
    selected_public_items: list[OperationsRecapEditorialItem] = []
    for passage in editorial.passages:
        if (item := items.get(passage.source_id)) is None:
            raise ValueError("剪辑片段缺少对应的运营素材")
        if passage.source_id in seen_refs:
            continue
        seen_refs.add(passage.source_id)
        pending = item.status == "pending_review"
        kind = {
            "watched_video": "watched_video", "public_reply": "public_comment", "public_comment": "public_comment",
            "confirmed_like": "operator_topic", "public_dynamic": "public_incident",
            "pending_comment": "operator_topic", "pending_dynamic": "operator_topic",
        }[item.kind]
        has_excerpt = not pending and (item.kind != "public_dynamic" or item.status == "public_visible")
        references.append(PublicSourceRef(
            source_id=item.source_id, kind=kind, title=item.title,
            url=None if pending else item.excerpt_url or item.url, bvid=None if pending else item.bvid,
            excerpt=_display_excerpt(item.excerpt, 500) if has_excerpt else "", fetch_status="manual",
        ))
        if not pending:
            selected_public_items.append(item)

    description = editorial.description or editorial.opening
    selected_urls = {url for source_id in set(source_ids) if items[source_id].status != "pending_review"
                     for url in (items[source_id].url, items[source_id].excerpt_url) if url}
    description_urls = {url.rstrip(".,!?;)]】）") for url in _PUBLIC_LINK.findall(description)}
    if not description_urls.issubset(selected_urls):
        raise ValueError("视频简介只能使用所选公开素材的原始链接")

    run_period = brief.run_period_label if brief.run_period_label != brief.period_label else None
    deep_passages = [p for p in editorial.passages if p.section == "deep_dive"] or list(editorial.passages)
    side_passages = [p for p in editorial.passages if p.section == "side_cut"] if any(
        p.section == "deep_dive" for p in editorial.passages
    ) else []
    chapters = _group_chapters(deep_passages, items)

    opening_bullets = [_display_excerpt(it.title, 48) for it in selected_public_items[:3]]
    scenes: list[dict[str, Any]] = [{
        "scene_id": "recap_editorial_open",
        "template": "TitleCard",
        "heading": editorial.title,
        "chapter_title": _display_excerpt(brief.period_label, 60),
        "theme": "cinema",
        "camera_motion": "push_in",
        "avatar_layout": "compact",
        "avatar_cue": "curious",
        "visual_props": {
            "subtitle": f"本轮 {run_period}" if run_period else _display_excerpt(brief.period_label, 60),
            **({"bullets": opening_bullets} if opening_bullets else {}),
        },
        "lines": [line.model_dump(mode="json") for line in _spoken_lines(editorial.opening, "curious")],
    }]

    prev_template = "TitleCard"
    evidence_idx = 0
    for chap_idx, (chap_title, chap_passages) in enumerate(chapters, 1):
        # Insert ChapterBumper for multi-chapter structure while keeping scene count within RECAP_MAX_SCENES.
        projected_remaining = len(chap_passages) + sum(len(cp) for _, cp in chapters[chap_idx:]) + len(side_passages) + 1
        if len(scenes) + projected_remaining + 1 <= RECAP_MAX_SCENES and (len(chapters) >= 2 or len(editorial.passages) < 5):
            first_item = items[chap_passages[0].source_id]
            chap_subtitle = "" if first_item.status == "pending_review" else _display_excerpt(first_item.title, 72)
            scenes.append({
                "scene_id": f"recap_chapter_{chap_idx}",
                "template": "ChapterBumper",
                "heading": chap_title,
                "chapter_title": f"PART {chap_idx:02d}",
                "theme": "cinema",
                "avatar_layout": "hidden",
                "avatar_cue": "curious",
                "visual_props": {
                    "chapter_number": chap_idx,
                    **({"subtitle": chap_subtitle} if chap_subtitle else {}),
                },
                "lines": [
                    line.model_dump(mode="json")
                    for line in _spoken_lines(f"接下来进入第{chap_idx}部分，聊聊{chap_title}。", "curious")
                ],
            })
            prev_template = "ChapterBumper"

        for passage in chap_passages:
            evidence_idx += 1
            scene = _make_evidence_scene(items[passage.source_id], passage, evidence_idx, prev_template=prev_template)
            scenes.append(scene)
            prev_template = scene["template"]

    if side_passages:
        if len(scenes) + len(side_passages) + 2 <= RECAP_MAX_SCENES and (len(chapters) >= 1 and len(side_passages) >= 2 or len(scenes) + len(side_passages) + 1 < RECAP_MIN_SCENES):
            chap_num = len(chapters) + 1
            scenes.append({
                "scene_id": f"recap_chapter_{chap_num}",
                "template": "ChapterBumper",
                "heading": "顺带本周切片",
                "chapter_title": f"PART {chap_num:02d}",
                "theme": "cinema",
                "avatar_layout": "hidden",
                "avatar_cue": "cheerful",
                "visual_props": {"chapter_number": chap_num, "subtitle": "顺带翻两张这周的轻量记录"},
                "lines": [line.model_dump(mode="json") for line in _spoken_lines("深聊先告一段落，顺带翻两张这周的切片。", "curious")],
            })
            prev_template = "ChapterBumper"
        for passage in side_passages:
            evidence_idx += 1
            scene = _make_evidence_scene(items[passage.source_id], passage, evidence_idx, prev_template=prev_template)
            scenes.append(scene)
            prev_template = scene["template"]

    optional_scene_ids: list[str] = []
    if len(scenes) + 2 <= RECAP_MAX_SCENES and selected_public_items and prev_template != "TimelineSummary":
        bullets = [_display_excerpt(it.title, 48) for it in selected_public_items[:4]]
        if bullets:
            scenes.append({
                "scene_id": "recap_side_timeline",
                "template": "TimelineSummary",
                "heading": "本周话题切片脉络",
                "chapter_title": "顺带本周切片",
                "avatar_cue": "curious",
                "visual_props": {"subtitle": _display_excerpt(brief.period_label, 60), "bullets": bullets},
                "lines": [
                    line.model_dump(mode="json")
                    for line in _spoken_lines("把这几条挑出来的公开脉络顺一遍，看看哪块最戳你。", "curious")
                ],
            })
            prev_template = "TimelineSummary"
            optional_scene_ids.append("recap_side_timeline")

    chart_metrics = [
        {"label": label, "value": count, "unit": "条"}
        for label, count in (
            ("观看投稿", brief.watched_videos),
            ("公开评论", brief.public_comments),
            ("公开动态", brief.public_dynamics),
            ("确认点赞", brief.confirmed_likes or 0),
        )
        if count > 0
    ]
    if len(scenes) + 2 <= RECAP_MAX_SCENES and len(chart_metrics) >= 2 and prev_template != "ChartCompare":
        scenes.append({
            "scene_id": "recap_weekly_metrics",
            "template": "ChartCompare",
            "heading": "本周足迹速览",
            "chapter_title": "顺带本周切片",
            "avatar_cue": "calm",
            "visual_props": {"chart_items": chart_metrics, "citation_label": _display_excerpt(brief.period_label, 60)},
            "lines": [
                line.model_dump(mode="json")
                for line in _spoken_lines(
                    f"顺便对一下这周的公开足迹，一共看了 {brief.watched_videos} 个投稿，公开评论和动态也都留了档。",
                    "calm",
                )
            ],
        })
        prev_template = "ChartCompare"
        optional_scene_ids.append("recap_weekly_metrics")

    closing_lines = _spoken_lines(editorial.closing, "calm")
    closing_bullets = [_display_excerpt(it.title, 42) for it in selected_public_items[:3]]
    scenes.append({
        "scene_id": "recap_editorial_close",
        "template": "MemeCutaway" if prev_template == "KineticStatement" else "KineticStatement",
        "heading": _display_excerpt(closing_lines[-1].text, 42),
        "chapter_title": "评论区继续聊",
        "theme": "cinema",
        "transition": "fade",
        "camera_motion": "pull_out",
        "avatar_layout": "hidden",
        "avatar_cue": "calm",
        "visual_props": {
            "subtitle": _display_excerpt(editorial.title, 80),
            **({"bullets": closing_bullets} if closing_bullets else {}),
        },
        "lines": [line.model_dump(mode="json") for line in closing_lines],
    })

    raw_plan = {
        "title": editorial.title,
        "summary": description,
        "tags": list(_RECAP_TAGS),
        "video_mode": "watched_roundup",
        "duration_preset": "standard",
        "scenes": scenes,
        "sources": [ref.model_dump(mode="json") for ref in references],
    }
    normalized = normalize_cinematic_scene_defaults(raw_plan)
    validated_scenes = [VideoSceneSpec.model_validate(sc) for sc in normalized["scenes"]]
    estimated = estimate_recap_duration_seconds(validated_scenes)

    while estimated > RECAP_TARGET_MAX_SECONDS and optional_scene_ids and len(validated_scenes) > RECAP_MIN_SCENES:
        drop_id = optional_scene_ids.pop()
        validated_scenes = [sc for sc in validated_scenes if sc.scene_id != drop_id]
        estimated = estimate_recap_duration_seconds(validated_scenes)

    if not RECAP_MIN_SCENES <= len(validated_scenes) <= RECAP_MAX_SCENES:
        raise ValueError(f"复盘视频分镜数量须在 {RECAP_MIN_SCENES} 至 {RECAP_MAX_SCENES} 镜之间（当前 {len(validated_scenes)} 镜）")
    if estimated < RECAP_TARGET_MIN_SECONDS:
        raise ValueError(f"复盘视频预估时长 {estimated:.1f}s 低于 {RECAP_TARGET_MIN_SECONDS:.0f}s 下限")
    if estimated > RECAP_TARGET_MAX_SECONDS:
        raise ValueError(f"复盘视频预估时长 {estimated:.1f}s 超过 {RECAP_TARGET_MAX_SECONDS:.0f}s 上限")

    preset = "deep_dive" if (len(validated_scenes) >= 11 or estimated >= 180.0) else "standard"
    return {
        "title": editorial.title,
        "summary": description,
        "tags": list(_RECAP_TAGS),
        "video_mode": "watched_roundup",
        "duration_preset": preset,
        "scenes": validated_scenes,
        "sources": references,
    }


def recap_plan_fields(brief: OperationsRecapBrief) -> dict:
    """Map verified counts and attributed excerpts to the existing scene schema."""
    sources = {source.source_id: source for source in brief.sources}
    if len(sources) != len(brief.sources):
        raise ValueError("运营复盘素材编号须唯一")
    if brief.editorial is not None:
        return _editorial_plan_fields(brief)
    metrics = [("观看记录", brief.watched_videos, "个视频", "实际观看了 {count} 个视频。")]
    if brief.accepted_comments is not None:
        metrics.append(("评论提交回执", brief.accepted_comments, "条评论", "评论拿到了 {count} 条提交回执。"))
    metrics.extend((
        ("公开页读回", brief.public_comments, "条评论", "从公开页读回的评论有 {count} 条。"),
        ("动态发布回执", brief.public_dynamics, "条动态", "发了 {count} 条动态。"),
    ))
    if brief.review_comments:
        metrics.append(("评论待审核", brief.review_comments, "条草稿", "还有 {count} 条评论草稿，先留着审核。"))
    if brief.confirmed_likes:
        metrics.append(("点赞回执", brief.confirmed_likes, "个赞", "这轮还点了 {count} 个赞。"))
    counts_text = "，".join(f"{label} {count}" for label, count, _, _ in metrics)
    aggregate = PublicSourceRef(
        source_id=f"operations-recap:{content_hash([brief.period_label, counts_text])[:20]}",
        kind="operator_topic",
        title="工坊观看记录、平台回执与公开读回",
        excerpt=f"{brief.period_label}。{counts_text}。",
        fetch_status="manual",
    )
    scenes = [
        VideoSceneSpec(
            scene_id="recap_title",
            template="TitleCard",
            heading=brief.title,
            theme="cinema",
            camera_motion="push_in",
            avatar_cue="curious",
            visual_props={"subtitle": brief.period_label, "citation_label": aggregate.title},
            lines=[VideoLineSpec(text="来翻翻这段时间的运营记录。", avatar_cue="curious")],
        ),
        VideoSceneSpec(
            scene_id="recap_counts",
            template="ChartCompare",
            heading="这段时间做了什么",
            chapter_title="实际记录",
            theme="aqua_light",
            avatar_layout="compact",
            transition="wipe_right",
            visual_props={
                "chart_items": [{"label": label, "value": count, "unit": unit} for label, count, unit, _ in metrics],
                "citation_label": aggregate.title,
            },
            lines=[
                VideoLineSpec(text=narration.format(count=count), reveal_step=index, avatar_cue="calm")
                for index, (_, count, _, narration) in enumerate(metrics)
            ],
        ),
    ]
    watched_titles = []
    for index, highlight in enumerate(brief.highlights, 1):
        if (source := sources.get(highlight.source_id)) is None:
            raise ValueError("复盘片段缺少对应的公开来源")
        label = highlight.credit.strip() or source.title[:120] or source.url or source.source_id
        if source.kind == "watched_video" and highlight.credit:
            watched_titles.append(f"{highlight.title} · {label}"[:120])
            continue
        scenes.append(VideoSceneSpec(
            scene_id=f"recap_highlight_{index}",
            template="QuoteSpotlight",
            heading=highlight.title,
            chapter_title="投稿来源",
            theme="dark_tech" if index % 2 else "aqua_light",
            avatar_layout="compact",
            transition="fade",
            visual_props={"subtitle": highlight.text, "citation_label": label[:120]},
            lines=[VideoLineSpec(text=highlight.text, avatar_cue="thinking")],
        ))
    if watched_titles:
        scenes.append(VideoSceneSpec(
            scene_id="recap_watched_sources",
            template="TimelineSummary",
            heading="这轮看过的投稿",
            theme="dark_tech",
            avatar_layout="compact",
            transition="fade",
            visual_props={"bullets": watched_titles, "citation_label": "原片链接见简介"},
            lines=[VideoLineSpec(text="放几条我这轮看过的投稿。原片链接在简介里，感兴趣就点进去看看呀。",
                                 avatar_cue="curious")],
        ))
    if reflection := brief.reflection.strip():
        scenes.append(VideoSceneSpec(
            scene_id="recap_reflection",
            template="QuoteSpotlight",
            heading="我的复盘感想",
            theme="aqua_light",
            avatar_layout="compact",
            transition="fade",
            visual_props={"subtitle": reflection},
            lines=[VideoLineSpec(text=reflection, avatar_cue="calm")],
        ))
    summary = f"{brief.period_label}。{counts_text}。"
    for url in dict.fromkeys(source.url for source in brief.sources if source.url):
        if len(summary) + len(url) + 1 <= 1000:
            summary += f"\n{url}"
    return {
        "title": brief.title,
        "summary": summary,
        "tags": list(_RECAP_TAGS),
        "video_mode": "watched_roundup",
        "duration_preset": "standard",
        "scenes": scenes,
        "sources": [aggregate, *brief.sources],
    }
