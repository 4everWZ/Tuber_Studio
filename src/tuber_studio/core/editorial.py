"""Channel triage & editorial assessment for comment vs dynamic vs cinematic video."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from .models import StrictModel

ContentChannel = Literal["comment", "dynamic", "video"]
VideoMode = Literal[
    "commentary",
    "daily_vlog",
    "watched_roundup",
    "project_showcase",
    "explainer",
    "self_intro",
]
DurationPreset = Literal["short", "standard", "deep_dive"]

DEFAULT_DURATION_BY_MODE: dict[VideoMode, DurationPreset] = {
    "commentary": "short",
    "daily_vlog": "standard",
    "watched_roundup": "standard",
    "project_showcase": "standard",
    "explainer": "standard",
    "self_intro": "standard",
}

CHANNEL_TRIAGE_GUIDANCE = (
    "内容渠道分流原则（非强制建议，保持各通道定位清晰）：\n"
    "- 评论（comment）：针对单个视频或评论楼层的 1~3 句自然短评/吐槽/接话，无需多镜视觉信息。\n"
    "- 动态（dynamic）：单点感悟、心情碎碎念、一张配图或视频预告等低视觉复杂度轻量分享，1 条动态即可讲清。\n"
    "- 视频（video）：需要多镜头叙事弧或高信息增益视觉表达的内容，例如："
    "LoRA 微调 / 日常实验前后对比日志（daily_vlog）、多部近期看过视频串联盘点（watched_roundup）、"
    "开源项目架构与 UI 演示（project_showcase）、深度原理拆解（explainer）或角色/工坊正式自我介绍（self_intro）。"
)

_DAILY_VLOG_RE = re.compile(
    r"(lora|微调|finetune|fine-tune|训练|调参|实验|日记|日志|vlog|前后对比|before\s*/?\s*after|测试记录|翻车实录|日常折腾)",
    re.I,
)
_ROUNDUP_RE = re.compile(
    r"(盘点|合集|roundup|最近看|刷到|周报|月报|回顾|连看|一期看|片单|榜单)",
    re.I,
)
_SHOWCASE_RE = re.compile(
    r"(开源|项目|架构|repo|github|showcase|演示|demo|工作台|workbench|上线|新功能|tuber\s*studio)",
    re.I,
)
_SELF_INTRO_RE = re.compile(
    r"(自我介绍|认识一下|初来乍到|频道介绍|我是\s*aqua|关于本鲸|self[_\s-]*intro)",
    re.I,
)
_EXPLAINER_RE = re.compile(
    r"(原理|拆解|科普|教程|机制|为什么|怎么实现|工作流|pipeline|architecture|deep\s*dive|explainer|剖析)",
    re.I,
)
_DEEP_DURATION_RE = re.compile(r"(深度|全解析|长视频|完整拆解|源码级|deep[_\s-]*dive|从零到一)", re.I)
_VISUAL_ARC_RE = re.compile(
    r"(截图|录屏|代码|架构|流程|图表|对比|分镜|视频|演示|demo|展示|盘点|教程|拆解|微调|lora|测试)",
    re.I,
)
_DYNAMIC_MOOD_RE = re.compile(
    r"(一句话|随想|心情|干饭|白饭|摸鱼|早安|晚安|冒泡|碎碎念|发条动态|预告|吐槽一句)",
    re.I,
)
_COMMENT_REPLY_RE = re.compile(r"(回复|评论区|楼里|搭话|回一句|短评)", re.I)


def _infer_video_mode(
    text: str,
    *,
    reference_urls: Sequence[str],
    candidate_bvids: Sequence[str],
    include_workspace_docs: bool,
    requested_mode: VideoMode | None,
) -> VideoMode:
    if requested_mode is not None:
        return requested_mode
    if len(candidate_bvids) >= 2 or _ROUNDUP_RE.search(text):
        return "watched_roundup"
    if _DAILY_VLOG_RE.search(text):
        return "daily_vlog"
    if include_workspace_docs or any("github.com/" in str(u).lower() for u in reference_urls) or _SHOWCASE_RE.search(text):
        return "project_showcase"
    if _SELF_INTRO_RE.search(text):
        return "self_intro"
    if _EXPLAINER_RE.search(text):
        return "explainer"
    return "commentary"


def _infer_duration_preset(
    text: str,
    *,
    brief_text: str,
    reference_urls: Sequence[str],
    include_workspace_docs: bool,
    video_mode: VideoMode,
    requested_duration: DurationPreset | None,
) -> DurationPreset:
    if requested_duration is not None:
        return requested_duration
    brief_len = len(brief_text.strip())
    if (
        brief_len >= 1800
        or len(reference_urls) >= 3
        or (include_workspace_docs and brief_len >= 600)
        or _DEEP_DURATION_RE.search(text)
    ):
        return "deep_dive"
    return DEFAULT_DURATION_BY_MODE.get(video_mode, "standard")


def assess_editorial_channel(
    topic: str,
    *,
    brief_text: str = "",
    reference_urls: Sequence[str] = (),
    candidate_bvids: Sequence[str] = (),
    uploaded_count: int = 0,
    include_workspace_docs: bool = False,
    requested_mode: VideoMode | None = None,
    requested_duration: DurationPreset | None = None,
) -> EditorialAssessment:
    """Advise whether an idea fits comment, dynamic post, or multi-scene video without blocking."""
    clean_topic = str(topic or "").strip()
    clean_brief = str(brief_text or "").strip()
    combined = f"{clean_topic}\n{clean_brief}".strip()
    urls = [str(u).strip() for u in reference_urls if str(u).strip()]
    bvids = [str(b).strip() for b in candidate_bvids if str(b).strip()]

    video_mode = _infer_video_mode(
        combined,
        reference_urls=urls,
        candidate_bvids=bvids,
        include_workspace_docs=include_workspace_docs,
        requested_mode=requested_mode,
    )
    duration_preset = _infer_duration_preset(
        combined,
        brief_text=clean_brief,
        reference_urls=urls,
        include_workspace_docs=include_workspace_docs,
        video_mode=video_mode,
        requested_duration=requested_duration,
    )

    signals: list[str] = []
    video_score = 0.25

    if len(clean_brief) >= 160:
        video_score += 0.25
        signals.append(f"参考大纲/资料较充实（{len(clean_brief)} 字）")
    if urls:
        video_score += 0.22
        signals.append(f"附带 {len(urls)} 条外部公开参考链接")
    if uploaded_count > 0:
        video_score += 0.22
        signals.append(f"附带 {uploaded_count} 份专属公开视觉素材")
    if include_workspace_docs:
        video_score += 0.25
        signals.append("已开启工坊公开文档取材")
    if len(bvids) >= 2:
        video_score += 0.25
        signals.append(f"串联 {len(bvids)} 部已看视频做盘点")
    elif len(bvids) == 1:
        signals.append(f"关联单部视频 {bvids[0]}")
    if _VISUAL_ARC_RE.search(combined):
        video_score += 0.18
        signals.append("包含视觉展示、对比、流程或代码拆解诉求")
    if video_mode in {"daily_vlog", "watched_roundup", "project_showcase", "explainer", "self_intro"}:
        video_score += 0.12
        signals.append(f"题材天然匹配 {video_mode} 多镜叙事")

    is_single_bv_reply = (
        len(bvids) == 1
        and len(clean_brief) < 80
        and uploaded_count == 0
        and not urls
        and not include_workspace_docs
        and (len(clean_topic) < 42 or bool(_COMMENT_REPLY_RE.search(combined)))
        and not _VISUAL_ARC_RE.search(combined)
    )
    is_light_dynamic = (
        requested_mode is None
        and len(clean_brief) < 120
        and not urls
        and not include_workspace_docs
        and len(bvids) <= 1
        and uploaded_count <= 1
        and not _VISUAL_ARC_RE.search(combined)
        and (bool(_DYNAMIC_MOOD_RE.search(combined)) or (len(clean_topic) < 48 and video_score < 0.52))
    )

    if is_single_bv_reply:
        channel: ContentChannel = "comment"
        fit_score = 0.78
        reasoning = (
            "当前选题聚焦单条视频且无多镜视觉材料或长大纲，优先建议作为 1~3 句视频评论直接互动；"
            "若仍想做短吐槽视频，可继续走 short 模板。"
        )
        dynamic_alt = "若想面向全站粉丝发表简短观后感，可改为 1 条公开动态附带该视频链接。"
    elif is_light_dynamic:
        channel = "dynamic"
        fit_score = 0.76
        reasoning = (
            "当前选题属于单点心情、轻量随想或简短预告，视觉复杂度低，发一条图文动态通常比做多镜视频更自然轻快；"
            "如准备扩展对比图或拆解脚本，可继续生成短视频。"
        )
        dynamic_alt = "建议直接用 1 条动态讲清这件事，再把后续深度实验攒成一期 daily_vlog。"
    else:
        channel = "video"
        fit_score = round(min(0.98, max(0.55, video_score)), 2)
        reasoning = (
            f"当前选题具备清晰的多镜叙事与视觉信息增益，推荐以「{video_mode} / {duration_preset}」成片呈现。"
        )
        dynamic_alt = "可同步裁出 1 条轻量动态作为预告或文字补充，把核心对比留给视频正片。"

    if not signals:
        signals.append("单点轻量话题，信息密度适合短表达")

    return EditorialAssessment(
        recommended_channel=channel,
        fit_score=fit_score,
        video_mode=video_mode,
        duration_preset=duration_preset,
        reasoning=reasoning,
        dynamic_alternative=dynamic_alt,
        key_signals=signals[:6],
    )


class EditorialAssessment(StrictModel):
    recommended_channel: ContentChannel
    fit_score: float = Field(ge=0.0, le=1.0)
    video_mode: VideoMode
    duration_preset: DurationPreset
    reasoning: str
    dynamic_alternative: str = ""
    key_signals: list[str] = Field(default_factory=list)


class VideoIdeaPitch(StrictModel):
    idea_id: str
    title: str
    topic: str
    channel: ContentChannel
    video_mode: VideoMode
    duration_preset: DurationPreset
    hook: str
    visual_angle: str
    source_refs: list[str] = Field(default_factory=list)
    why_this_channel: str
