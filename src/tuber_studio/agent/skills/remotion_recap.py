"""Build an attribution-preserving video script from a frozen public operations recap."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ...core.models import StrictModel, content_hash
from .remotion_assets import PublicSourceRef, VideoLineSpec, VideoSceneSpec

RecapTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
RecapCount = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
RecapSourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
RecapNarration = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]

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
    narration: RecapNarration


class OperationsRecapEditorial(StrictModel):
    title: RecapTitle
    opening: RecapNarration
    passages: list[OperationsRecapPassage] = Field(min_length=1, max_length=4)
    closing: RecapNarration
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)] | None = None


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


def _display_excerpt(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _spoken_lines(narration: str, cue: Literal["curious", "thinking", "calm", "proud"]) -> list[VideoLineSpec]:
    """Keep each caption short while retaining the narrator's original phrasing."""
    phrases = re.findall(r"[^，。！？；!?;]+[，。！？；!?;]*|[，。！？；!?;]+", narration)
    chunks: list[str] = []
    current = ""
    for phrase in phrases:
        if current and len(current) + len(phrase) > 42:
            chunks.append(current)
            current = ""
        while len(phrase) > 42:
            chunks.append(phrase[:42])
            phrase = phrase[42:]
        current += phrase
    if current:
        chunks.append(current)
    return [VideoLineSpec(text=text, avatar_cue=cue) for text in chunks if text.strip()]


def _make_evidence_scene(item: OperationsRecapEditorialItem, narration: str, index: int) -> VideoSceneSpec:
    heading = _display_excerpt(item.title, 54)
    base = {"scene_id": f"recap_editorial_{index}", "heading": heading, "avatar_layout": "compact"}
    if item.status == "pending_review":
        return VideoSceneSpec(
            **base, template="TitleCard", chapter_title="评论选题" if item.kind == "pending_comment" else "动态选题",
            theme="dark_tech", transition="fade", camera_motion="pull_out", avatar_cue="thinking",
            visual_props={"citation_label": "待审核 · 尚未发布"}, lines=_spoken_lines(narration, "thinking"),
        )
    if item.kind == "watched_video":
        creator = _display_excerpt(item.creator, 45)
        excerpt = _display_excerpt(item.excerpt, 88)
        label = {"video_description": "原片简介", "subtitle": "原片字幕", "automatic_subtitle": "自动字幕"}.get(item.excerpt_kind)
        return VideoSceneSpec(
            **base, template="TitleCard", chapter_title="本轮观看" if item.in_current_run else "本周观看",
            theme="aqua_light", transition="wipe_right", camera_motion="static", avatar_cue="curious",
            visual_props={
                "subtitle": f"{label} · {excerpt}" if excerpt and label else excerpt,
                "citation_label": f"UP 主 {creator}" if creator else "",
            },
            lines=_spoken_lines(narration, "curious"),
        )
    if item.status == "confirmed":
        liked = item.kind == "confirmed_like"
        return VideoSceneSpec(
            **base, template="TitleCard", chapter_title="点赞回执" if liked else "动态发布回执",
            theme="aqua_light", transition="fade", avatar_cue="calm",
            visual_props={"subtitle": _display_excerpt(item.target_excerpt, 88) if liked else ""},
            lines=_spoken_lines(narration, "calm"),
        )
    credit = {
        "public_reply": "本账号的公开回复",
        "public_comment": "本账号的公开评论",
        "public_dynamic": "本账号的公开动态",
    }[item.kind]
    target = _display_excerpt(item.target_excerpt, 72)
    excerpt = _display_excerpt(item.excerpt, 100)
    visual_props = {"subtitle": excerpt, "citation_label": credit}
    if target and item.kind == "public_reply":
        label = f"原楼留言（{_display_excerpt(item.target_author, 24)}）" if item.target_author else "原楼留言"
        visual_props["bullets"] = [f"{label} · {target}"]
    return VideoSceneSpec(
        **base, template="QuoteSpotlight" if excerpt else "TitleCard", chapter_title="公开页已核实",
        theme="dark_tech", transition="fade", camera_motion="static", avatar_cue="thinking",
        visual_props=visual_props, lines=_spoken_lines(narration, "thinking"),
    )


def _editorial_plan_fields(brief: OperationsRecapBrief) -> dict:
    editorial = brief.editorial
    assert editorial is not None
    items = {item.source_id: item for item in brief.editorial_items}
    if len(items) != len(brief.editorial_items):
        raise ValueError("剪辑素材编号须唯一")
    source_ids = [passage.source_id for passage in editorial.passages]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("剪辑不能重复引用同一素材")

    run_period = brief.run_period_label if brief.run_period_label != brief.period_label else None
    scenes = [VideoSceneSpec(
        scene_id="recap_editorial_open", template="TitleCard", heading=editorial.title,
        chapter_title=_display_excerpt(brief.period_label, 60), theme="cinema", camera_motion="push_in",
        avatar_layout="compact", avatar_cue="curious",
        visual_props={"subtitle": f"本轮 {run_period}" if run_period else ""},
        lines=_spoken_lines(editorial.opening, "curious"),
    )]
    references = []
    for index, passage in enumerate(editorial.passages, 1):
        if (item := items.get(passage.source_id)) is None:
            raise ValueError("剪辑片段缺少对应的运营素材")
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
        scenes.append(_make_evidence_scene(item, passage.narration, index))

    description = editorial.description or editorial.opening
    selected_urls = {url for source_id in source_ids if items[source_id].status != "pending_review"
                     for url in (items[source_id].url, items[source_id].excerpt_url) if url}
    description_urls = {url.rstrip(".,!?;)]】）") for url in _PUBLIC_LINK.findall(description)}
    if not description_urls.issubset(selected_urls):
        raise ValueError("视频简介只能使用所选公开素材的原始链接")
    closing_lines = _spoken_lines(editorial.closing, "calm")
    scenes.append(VideoSceneSpec(
        scene_id="recap_editorial_close", template="KineticStatement",
        heading=_display_excerpt(closing_lines[-1].text, 42), theme="cinema",
        transition="fade", camera_motion="pull_out", avatar_layout="compact", avatar_cue="calm",
        lines=closing_lines,
    ))
    return {
        "title": editorial.title,
        "summary": description,
        "tags": list(_RECAP_TAGS),
        "video_mode": "watched_roundup",
        "duration_preset": "short",
        "scenes": scenes,
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
        "duration_preset": "short",
        "scenes": scenes,
        "sources": [aggregate, *brief.sources],
    }
