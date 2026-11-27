"""Build an attribution-preserving video script from a frozen public operations recap."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from ...core.models import StrictModel, content_hash
from .remotion_assets import PublicSourceRef, VideoLineSpec, VideoSceneSpec

RecapTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
RecapCount = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]


class OperationsRecapHighlight(StrictModel):
    title: RecapTitle
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    credit: str = Field(default="", max_length=80)


class OperationsRecapBrief(StrictModel):
    title: RecapTitle
    period_label: RecapTitle
    watched_videos: RecapCount
    public_comments: RecapCount
    accepted_comments: RecapCount | None = None
    public_dynamics: RecapCount
    sources: list[PublicSourceRef] = Field(default_factory=list, max_length=8)
    highlights: list[OperationsRecapHighlight] = Field(default_factory=list, max_length=4)
    reflection: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_comment_readback(self):
        if self.accepted_comments is not None and self.public_comments > self.accepted_comments:
            raise ValueError("公开读回评论数不能超过取得回执的评论数")
        return self


def recap_plan_fields(brief: OperationsRecapBrief) -> dict:
    """Map verified counts and attributed excerpts to the existing scene schema."""
    sources = {source.source_id: source for source in brief.sources}
    if len(sources) != len(brief.sources):
        raise ValueError("运营复盘素材编号须唯一")
    metrics = [("观看记录", brief.watched_videos, "个视频", "实际观看了 {count} 个视频。")]
    if brief.accepted_comments is not None:
        metrics.append(("评论提交回执", brief.accepted_comments, "条评论", "评论拿到了 {count} 条提交回执。"))
    metrics.extend((
        ("公开页读回", brief.public_comments, "条评论", "从公开页读回的评论有 {count} 条。"),
        ("动态发布回执", brief.public_dynamics, "条动态", "发了 {count} 条动态。"),
    ))
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
    for index, highlight in enumerate(brief.highlights, 1):
        if (source := sources.get(highlight.source_id)) is None:
            raise ValueError("复盘片段缺少对应的公开来源")
        label = highlight.credit.strip() or source.title[:120] or source.url or source.source_id
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
        "tags": ["运营复盘", "视频盘点"],
        "video_mode": "watched_roundup",
        "duration_preset": "short",
        "scenes": scenes,
        "sources": [aggregate, *brief.sources],
    }
