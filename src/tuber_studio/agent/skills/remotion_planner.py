"""Two-stage cinematic video script planner, 6-mode fallback director, and idea pitch generator."""

from __future__ import annotations

import asyncio
import copy
import inspect
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field

from ...core.editorial import (
    DEFAULT_DURATION_BY_MODE,
    DurationPreset,
    VideoIdeaPitch,
    VideoMode,
    assess_editorial_channel,
)
from ...core.identity import without_model_identity
from ...core.models import StrictModel, content_hash, resident_character_profile
from ...core.store import dump
from . import remotion_assets as _ra

ALL_TEMPLATES: tuple[str, ...] = (
    "TitleCard", "CodeWalkthrough", "ChartCompare", "MemeCutaway", "QuoteSpotlight",
    "TimelineSummary", "ChapterBumper", "ArchitectureFlow", "UIShowcase", "KineticStatement",
)
SceneTheme = Literal["dark_tech", "aqua_light", "cinema"]
SceneTransition = Literal["cut", "fade", "slide_left", "wipe_right", "zoom_punch", "flash"]
AvatarLayout = Literal["corner_float", "compact", "featured", "hidden"]
CameraMotion = Literal["static", "push_in", "pull_out", "pan_right", "focus_target"]
LineSfx = Literal["none", "pop", "whoosh", "chime", "chapter_hit"]

DURATION_PRESETS: dict[DurationPreset, dict[str, Any]] = {
    "short": {"min_scenes": 4, "max_scenes": 6, "default_scenes": 5, "approx_seconds": (50, 75), "lines_per_scene": (3, 4)},
    "standard": {"min_scenes": 8, "max_scenes": 12, "default_scenes": 9, "approx_seconds": (120, 180), "lines_per_scene": (4, 5)},
    "deep_dive": {"min_scenes": 12, "max_scenes": 18, "default_scenes": 14, "approx_seconds": (210, 300), "lines_per_scene": (4, 6)},
}
_THEME_BY_TEMPLATE: dict[str, SceneTheme] = {
    "TitleCard": "cinema", "ChapterBumper": "cinema", "KineticStatement": "cinema",
    "ArchitectureFlow": "dark_tech", "CodeWalkthrough": "dark_tech", "UIShowcase": "dark_tech",
    "ChartCompare": "aqua_light", "QuoteSpotlight": "aqua_light", "MemeCutaway": "aqua_light", "TimelineSummary": "aqua_light",
}
_TRANSITION_CYCLE: tuple[SceneTransition, ...] = ("fade", "slide_left", "wipe_right", "zoom_punch")
_CAMERA_BY_TEMPLATE: dict[str, CameraMotion] = {
    "TitleCard": "push_in", "ChapterBumper": "push_in", "KineticStatement": "push_in", "MemeCutaway": "push_in",
    "ArchitectureFlow": "focus_target", "CodeWalkthrough": "focus_target", "UIShowcase": "focus_target",
    "TimelineSummary": "pull_out", "ChartCompare": "pan_right", "QuoteSpotlight": "static",
}
_AVATAR_BY_TEMPLATE: dict[str, AvatarLayout] = {
    "ChapterBumper": "hidden", "KineticStatement": "hidden",
    "CodeWalkthrough": "compact", "ArchitectureFlow": "compact", "UIShowcase": "compact",
}


class ChapterOutlineSpec(StrictModel):
    chapter_number: int = Field(ge=1, le=8)
    chapter_title: str = Field(min_length=1, max_length=60)
    theme: SceneTheme = "dark_tech"
    scene_templates: list[str] = Field(min_length=1, max_length=5)
    key_points: list[str] = Field(default_factory=list, max_length=6)


class VideoOutlinePlan(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1000)
    tags: list[str] = Field(min_length=1, max_length=10)
    chapters: list[ChapterOutlineSpec] = Field(min_length=2, max_length=6)
    target_scene_count: int = Field(ge=2, le=20)


class ChapterScenesBatch(StrictModel):
    scenes: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


def resolve_duration_and_scenes(
    video_mode: VideoMode, duration_preset: DurationPreset | None = None, max_scenes: int | None = None
) -> tuple[DurationPreset, int]:
    """Resolve effective duration preset and scene target while respecting explicit max_scenes."""
    preset: DurationPreset = duration_preset or DEFAULT_DURATION_BY_MODE.get(video_mode, "standard")
    if max_scenes is not None:
        return preset, max(1, min(20, int(max_scenes)))
    return preset, int(DURATION_PRESETS[preset]["default_scenes"])


def _build_planner_system_message(char: dict[str, Any], intent: str) -> dict[str, str]:
    profile = resident_character_profile(char or {})
    name = str(profile.get("name") or "Aqua")
    desc = str(profile.get("self_description") or "白发鲸鱼娘，日常轻傲娇吐槽与本地开发者搭档")
    style = str(profile.get("speaking_style") or "自然口语短句，节奏明快")
    return {
        "role": "system",
        "content": (
            f"角色：{name}（{desc}）。口吻：{style}。\n"
            "仅使用传入的公开资料、brief_text、公开链接摘要、已看视频与全渠道表情包；绝不编造私有经历或私信内容。\n"
            f"{intent}"
        ),
    }


def _extract_points(brief_text: str, research_bundle: dict[str, Any], candidates: Sequence[dict[str, Any]], topic: str) -> list[str]:
    points: list[str] = []
    for raw_line in re.split(r"[\r\n。！？；;]+", brief_text or ""):
        clean = re.sub(r"^[#>*\-\d.\s]+", "", raw_line).strip()
        if len(clean) >= 4 and clean not in points:
            points.append(clean[:120])
    for cand in (
        *(research_bundle.get("facts") or ()),
        *(str(s.get("excerpt") or s.get("title") or "") for s in (research_bundle.get("sources") or ())),
        *(str(c.get("summary") or c.get("title") or "") for c in candidates[:4]),
    ):
        clean = " ".join(str(cand or "").split()).strip()
        if len(clean) >= 4 and clean[:120] not in points:
            points.append(clean[:120])
    return points or [topic[:80]]


def normalize_cinematic_scene_defaults(plan_dict: dict[str, Any]) -> dict[str, Any]:
    """Fill sensible theme, avatar_layout, transition, camera_motion, and reveal_step defaults."""
    out = copy.deepcopy(plan_dict)
    mode = str(out.get("video_mode") or "commentary")
    if out.get("duration_preset") not in DURATION_PRESETS:
        out["duration_preset"] = DEFAULT_DURATION_BY_MODE.get(mode, "standard")
    scenes = out.get("scenes")
    if not isinstance(scenes, list):
        return out

    prev_theme: str | None = None
    for s_idx, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        tpl = str(scene.get("template") or "QuoteSpotlight")
        if tpl not in ALL_TEMPLATES:
            tpl = scene["template"] = "QuoteSpotlight"
        vp = scene.setdefault("visual_props", {})
        if not isinstance(vp, dict):
            vp = scene["visual_props"] = {}

        if not scene.get("theme"):
            base_theme = _THEME_BY_TEMPLATE.get(tpl, "aqua_light")
            if base_theme == prev_theme and tpl not in {"TitleCard", "ChapterBumper", "CodeWalkthrough", "ArchitectureFlow"}:
                base_theme = "dark_tech" if prev_theme == "aqua_light" else "aqua_light"
            scene["theme"] = base_theme
        prev_theme = scene["theme"]

        scene.setdefault("avatar_layout", _AVATAR_BY_TEMPLATE.get(tpl, "corner_float"))
        if not scene.get("avatar_layout"):
            scene["avatar_layout"] = _AVATAR_BY_TEMPLATE.get(tpl, "corner_float")
        if not scene.get("transition"):
            scene["transition"] = "fade" if s_idx == 1 else ("zoom_punch" if tpl in {"ChapterBumper", "KineticStatement"} else _TRANSITION_CYCLE[s_idx % len(_TRANSITION_CYCLE)])
        if not scene.get("camera_motion"):
            scene["camera_motion"] = _CAMERA_BY_TEMPLATE.get(tpl, "static")

        if tpl == "ChapterBumper" and not vp.get("chapter_number"):
            vp["chapter_number"] = max(1, (s_idx // 3) + 1)
        if tpl == "ArchitectureFlow" and not vp.get("diagram_nodes"):
            vp["diagram_nodes"] = [
                {"id": "input", "label": "公开素材", "lane": 0, "tone": "sky"},
                {"id": "planner", "label": "导演编排", "lane": 1, "tone": "mint"},
                {"id": "render", "label": "音画合成", "lane": 2, "tone": "amber"},
            ]
            vp["diagram_edges"] = [{"source": "input", "target": "planner", "label": "结构化"}, {"source": "planner", "target": "render", "label": "帧对齐"}]
        if tpl == "KineticStatement" and not vp.get("stat_callout"):
            vp["stat_callout"] = str(vp.get("subtitle") or scene.get("heading") or "100%")[:24]

        lines = scene.get("lines")
        if isinstance(lines, list):
            node_ids = [str(n.get("id")) for n in (vp.get("diagram_nodes") or ()) if isinstance(n, dict) and n.get("id")]
            for l_idx, line in enumerate(lines, start=1):
                if not isinstance(line, dict):
                    continue
                if not line.get("line_id"):
                    line["line_id"] = f"s{s_idx}_l{l_idx}"
                if line.get("reveal_step") is None:
                    line["reveal_step"] = l_idx - 1
                if not line.get("sfx"):
                    line["sfx"] = "chapter_hit" if (tpl == "ChapterBumper" and l_idx == 1) else ("whoosh" if (l_idx == 1 and s_idx > 1) else ("pop" if line.get("emphasis") else "none"))
                if not line.get("visual_focus"):
                    if tpl == "ArchitectureFlow" and node_ids:
                        line["visual_focus"] = node_ids[(l_idx - 1) % len(node_ids)]
                    elif tpl == "UIShowcase":
                        line["visual_focus"] = f"region_{(l_idx - 1) % 3}"
    return out


def _resolve_sources_for_plan(
    *, topic: str, brief_text: str, research_bundle: dict[str, Any], candidates: Sequence[dict[str, Any]], public_memories: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    sources = [dict(s) for s in (research_bundle.get("sources") or ()) if isinstance(s, dict)]
    for item in candidates[:4]:
        if bv := str(item.get("bvid") or "").strip():
            sources.append({
                "source_id": f"video:{bv}", "kind": "watched_video", "scope": "public",
                "title": str(item.get("title") or bv)[:160], "url": item.get("url") or f"https://www.bilibili.com/video/{bv}",
                "bvid": bv, "excerpt": str(item.get("summary") or "")[:300],
            })
    if brief_text.strip():
        sources.append({"source_id": f"brief:{content_hash(brief_text)[:12]}", "kind": "operator_topic", "scope": "public", "title": topic[:120], "excerpt": brief_text.strip()[:480], "fetch_status": "manual"})
    elif not sources:
        sources.append({"source_id": f"topic:{content_hash(topic)[:12]}", "kind": "operator_topic", "scope": "public", "title": topic[:80], "excerpt": topic[:300]})
    for mem in public_memories[:2]:
        sources.append({"source_id": str(mem["id"]), "kind": "public_memory", "scope": "public", "title": "公开印象参考", "excerpt": str(mem.get("content") or "")[:200]})
    return sources[:12]


def _make_lines(scene_idx: int, tpl: str, point: str, next_point: str, cue: str, target_lines: int) -> list[dict[str, Any]]:
    short_p, short_n = point[:14] or "这一块", next_point[:14] or "下一步"
    templates_copy = {
        "ChapterBumper": [(f"接下来进入{short_p}这一章。", "cheerful", [short_p[:6]] if len(short_p) >= 2 else [], "chapter_hit"), ("先抓住最核心的变化再看细节。", "curious", ["最核心"], "none")],
        "ArchitectureFlow": [(f"先顺一遍{short_p}的数据流向。", "curious", ["数据流向"], "whoosh"), ("上游先拿真实公开材料，绝不凭空编造。", "thinking", ["公开材料"], "pop"), ("中间导演层拆好章节与节奏，再交给渲染。", "proud", ["章节与节奏"], "none"), (f"最后把{short_n}对齐到每一帧输出。", "cheerful", ["每一帧"], "chime")],
        "CodeWalkthrough": [(f"直接看{short_p}的关键实现。", "thinking", ["关键实现"], "whoosh"), ("第一段先锁定结构化 Schema 与校验边界。", "curious", ["校验边界"], "pop"), ("第二步再把每句音频实测帧长写进时间轴。", "proud", ["实测帧长"], "none"), ("这样代码和配音就能一步一高亮对上拍。", "cheerful", ["对上拍"], "chime")],
        "UIShowcase": [("来看看实际界面的交互焦点。", "cheerful", ["交互焦点"], "whoosh"), (f"左上角这组状态能一眼看清{short_p}。", "curious", ["一眼看清"], "pop"), (f"点开详情区还能继续联动{short_n}。", "proud", ["联动"], "none")],
        "ChartCompare": [(f"拿一组实测数据对照一下{short_p}。", "curious", ["实测数据"], "whoosh"), ("左边是优化前的耗时与波动，右边是稳态表现。", "thinking", ["稳态表现"], "pop"), ("差距摆在眼前，调整方向就非常明确了。", "proud", ["非常明确"], "chime")],
        "KineticStatement": [(f"记住这个核心结论：{short_p}。", "proud", ["核心结论"], "chapter_hit"), ("复杂工程不靠玄学，靠可复现的每一步对比。", "cheerful", ["可复现"], "pop")],
    }
    pool = templates_copy.get(tpl, [
        (f"先拆开看看{short_p}到底卡在哪。", cue, [short_p[:6]] if len(short_p) >= 2 else [], "whoosh"),
        ("把前因后果放在一起看，脉络一下就清了。", "thinking", ["脉络"], "pop"),
        (f"再对照{short_n}，很多小问题其实早有伏笔。", "curious", ["伏笔"], "none"),
        ("这一步讲透了，后面理解起来就会顺滑很多。", "proud", ["顺滑"], "chime"),
    ])
    count = max(2, min(len(pool), target_lines))
    return [
        {"line_id": f"s{scene_idx}_l{idx + 1}", "text": txt, "avatar_cue": lc, "emphasis": [e for e in emp if e and e in txt][:2], "reveal_step": idx, "sfx": sfx}
        for idx, (txt, lc, emp, sfx) in enumerate(pool[:count])
    ]


def build_cinematic_fallback_scenes(
    *, topic: str, style_note: str, sources: list[dict[str, Any]], target_scenes: int, duration_preset: DurationPreset,
    available_memes: list[dict[str, Any]], video_mode: VideoMode = "commentary", brief_text: str = "",
    research_bundle: dict[str, Any] | None = None, candidates: Sequence[dict[str, Any]] = (),
    pinned_meme_ids: Sequence[str] = (), uploaded_image_files: Sequence[str] = (), uploaded_video_files: Sequence[str] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """Build multi-chapter cinematic scenes across all 6 modes and 10 Remotion templates."""
    lead, bundle = topic[:48], research_bundle or {}
    points = _extract_points(brief_text, bundle, candidates, topic)
    modules = [str(m) for m in (bundle.get("modules") or ())]
    meme_ids, used_memes = {m["id"] for m in available_memes}, set()
    pinned_queue = list(dict.fromkeys(c for pid in pinned_meme_ids if (c := _ra.resolve_catalog_meme_id(available_memes, pid)) and c in meme_ids))

    def _take_meme(cue: str, preferred: str | None = None) -> str | None:
        if pinned_queue:
            preferred = pinned_queue.pop(0)
        if picked := _ra.pick_distinct_scene_meme_id(cue, used_memes, meme_ids, preferred_id=preferred):
            used_memes.add(picked)
        return picked

    showcase_url = next((str(s.get("url")) for s in sources if s.get("url") and "github.com/" in str(s["url"]).lower()), "") or next((str(s.get("url")) for s in sources if s.get("url")), "")
    lines_range = DURATION_PRESETS[duration_preset]["lines_per_scene"]
    lines_per_scene = lines_range[0] if target_scenes <= 6 else lines_range[1]
    hook_lines_map = {
        "daily_vlog": (f"今天把{lead[:12]}的实验日志摊开聊聊！", "从翻车现场到调参对比，一镜一镜看变化。", ["实验日志", "调参对比"]),
        "watched_roundup": (f"最近刷完这波视频，{lead[:12]}最有话聊！", "不水时长，直接按看点串起一期精华盘点。", ["精华盘点", "不水时长"]),
        "project_showcase": (f"{lead[:14]}到底解决了什么痛点？", "先看架构设计，再带大家逛一圈真实界面。", ["架构设计", "真实界面"]),
        "explainer": (f"为什么{lead[:12]}总让人一头雾水？", "别背概念啦，本鲸拆成流程图一步步讲透。", ["流程图", "一步步讲透"]),
        "self_intro": ("你好呀，我是 Aqua，你的本地鲸鱼搭档！", "今天带你逛一圈我的创作工坊与日常工作流。", ["Aqua", "创作工坊"]),
        "commentary": (f"等等，{lead[:14]}这事也太真实了。", "别光跟着点头，本鲸直接把重点拎出来聊。", ["太真实了", "重点"]),
    }
    h0, h1, h_emp = hook_lines_map.get(video_mode, hook_lines_map["commentary"])
    hook_scene: dict[str, Any] = {
        "scene_id": "scene_hook", "template": "TitleCard", "chapter_title": "开场导览", "theme": "cinema",
        "transition": "fade", "camera_motion": "push_in", "avatar_layout": "corner_float", "heading": lead, "avatar_cue": "cheerful",
        "visual_props": {
            "subtitle": style_note or points[0][:140], "bullets": [p[:48] for p in points[:3]],
            "meme_asset_id": _take_meme("cheerful", "archive-megaphone"),
            "citation_label": str((sources[0].get("title") if sources else "") or "公开观察")[:60],
        },
        "lines": [
            {"line_id": "s1_l1", "text": h0, "avatar_cue": "surprised", "emphasis": [h_emp[0]] if h_emp[0] in h0 else [], "reveal_step": 0, "sfx": "whoosh"},
            {"line_id": "s1_l2", "text": h1, "avatar_cue": "cheerful", "emphasis": [h_emp[1]] if h_emp[1] in h1 else [], "reveal_step": 1, "sfx": "none"},
        ],
    }
    count = max(1, min(20, int(target_scenes)))
    if count == 1:
        return lead, [hook_scene]

    mode_middle: dict[str, tuple[str, ...]] = {
        "daily_vlog": ("ChartCompare", "UIShowcase", "KineticStatement", "MemeCutaway", "CodeWalkthrough", "TimelineSummary"),
        "watched_roundup": ("QuoteSpotlight", "ChartCompare", "MemeCutaway", "KineticStatement", "TimelineSummary"),
        "project_showcase": ("ArchitectureFlow", "UIShowcase", "CodeWalkthrough", "ChartCompare", "KineticStatement"),
        "explainer": ("ArchitectureFlow", "CodeWalkthrough", "ChartCompare", "QuoteSpotlight", "KineticStatement"),
        "self_intro": ("QuoteSpotlight", "UIShowcase", "ArchitectureFlow", "MemeCutaway", "TimelineSummary"),
        "commentary": ("QuoteSpotlight", "MemeCutaway", "ChartCompare", "KineticStatement", "CodeWalkthrough"),
    }
    pool = mode_middle.get(video_mode, mode_middle["commentary"])
    use_bumpers, chapter_names = count >= 8, ("核心背景与痛点", "结构拆解与实测", "关键细节与对比", "总结与后续展望")
    middle_scenes: list[dict[str, Any]] = []
    last_tpl, chap_num = "TitleCard", 1
    for idx in range(max(0, count - 2)):
        s_idx, point, next_point = idx + 2, points[idx % len(points)], points[(idx + 1) % len(points)]
        if use_bumpers and idx in {2, 5, 8} and last_tpl != "ChapterBumper":
            tpl, chap_num = "ChapterBumper", chap_num + 1
        else:
            tpl = pool[idx % len(pool)]
            if tpl == last_tpl:
                tpl = pool[(idx + 1) % len(pool)]
        last_tpl = tpl
        cue = ("curious", "thinking", "proud", "surprised")[idx % 4]
        src = sources[idx % len(sources)] if sources else {}
        vp: dict[str, Any] = {
            "subtitle": point[:180], "bullets": [point[:48], *([next_point[:48]] if next_point != point else [])],
            "meme_asset_id": _take_meme(cue), "citation_label": str(src.get("url") or src.get("title") or "公开来源")[:120],
        }
        if tpl == "ChapterBumper":
            vp |= {"chapter_number": chap_num, "subtitle": f"第 {chap_num} 章 · {point[:48]}"}
        elif tpl == "ArchitectureFlow":
            mods, tones = modules[:4] or ["素材输入", "导演编排", "语音对齐", "Remotion 合成"], ("sky", "mint", "amber", "coral")
            vp["diagram_nodes"] = [{"id": f"n{i + 1}", "label": m[:18], "lane": i % 3, "tone": tones[i % 4]} for i, m in enumerate(mods[:4])]
            vp["diagram_edges"] = [{"source": f"n{i + 1}", "target": f"n{i + 2}", "label": "流转"} for i in range(len(vp["diagram_nodes"]) - 1)]
        elif tpl == "CodeWalkthrough":
            snippet = "\n".join(points[:5])[:1600]
            vp |= {
                "language": "python" if "def " in snippet or "import " in snippet else "text", "code_snippet": snippet,
                "code_steps": [{"title": "步骤 1：结构校验", "code_snippet": points[0][:300], "highlight_lines": [1]}, {"title": "步骤 2：节奏对齐", "code_snippet": "\n".join(points[:3])[:500], "highlight_lines": [1, 2]}],
            }
        elif tpl == "UIShowcase":
            vp["ui_focus_regions"] = [{"x": 0.28, "y": 0.30, "zoom": 1.35, "label": "核心控制区", "line_index": 0}, {"x": 0.68, "y": 0.55, "zoom": 1.45, "label": "结果预览区", "line_index": 1}]
        elif tpl == "ChartCompare":
            vp["chart_items"] = [{"label": "基线方案", "value": 46 + idx * 3, "unit": "%"}, {"label": "当前实验", "value": 84 + min(12, idx * 2), "unit": "%"}]
        elif tpl == "KineticStatement":
            vp["stat_callout"] = f"{min(99, 60 + idx * 7)}%" if video_mode != "daily_vlog" else "Before vs After"

        chap_title = chapter_names[min(chap_num - 1, len(chapter_names) - 1)]
        middle_scenes.append({
            "scene_id": f"scene_detail_{idx + 1}", "template": tpl, "chapter_title": chap_title,
            "heading": chap_title if tpl == "ChapterBumper" else point[:36], "visual_props": vp, "avatar_cue": cue,
            "lines": _make_lines(s_idx, tpl, point, next_point, cue, lines_per_scene if count >= 6 else 2),
        })

    outro_tpl = "TimelineSummary" if video_mode in {"project_showcase", "explainer", "watched_roundup"} else "MemeCutaway"
    if last_tpl == outro_tpl:
        outro_tpl = "QuoteSpotlight"
    outro_citation = showcase_url[:120] if video_mode == "project_showcase" and showcase_url else str((sources[0].get("url") or sources[0].get("title")) if sources else "Aqua 浅海手账")[:120]
    outro_scene: dict[str, Any] = {
        "scene_id": "scene_outro", "template": outro_tpl, "chapter_title": "总结与互动",
        "heading": "开源地址与适用场景" if video_mode == "project_showcase" else "总结收工，评论区继续聊",
        "visual_props": {"subtitle": showcase_url or points[0][:120], "bullets": [p[:48] for p in points[:3]], "meme_asset_id": _take_meme("proud", "story-meal"), "citation_label": outro_citation},
        "avatar_cue": "proud",
        "lines": [
            {"line_id": f"s{count}_l1", "text": "好啦，今天这一期的重点都摆在台面上了。", "avatar_cue": "proud", "emphasis": ["重点"], "reveal_step": 0, "sfx": "chime"},
            {"line_id": f"s{count}_l2", "text": "有想继续拆解或对比的内容，欢迎在评论区点单！", "avatar_cue": "cheerful", "emphasis": ["评论区"], "reveal_step": 1, "sfx": "none"},
        ],
    }

    img_queue = [str(u).strip() for u in uploaded_image_files if str(u).strip()]
    vid_queue = [str(v).strip() for v in uploaded_video_files if str(v).strip()]
    bindable = {"UIShowcase", "QuoteSpotlight", "MemeCutaway", "CodeWalkthrough"}
    candidates_media = [*middle_scenes, outro_scene]
    ordered_targets = [s for s in candidates_media if s["template"] in bindable]
    for s in candidates_media:
        if s not in ordered_targets and s is not outro_scene and s["template"] not in {"ChapterBumper", "ArchitectureFlow"}:
            ordered_targets.append(s)
    for s in candidates_media:
        if s not in ordered_targets and s is not outro_scene and s["template"] != "ChapterBumper":
            ordered_targets.append(s)
    for s in ordered_targets:
        if not img_queue and not vid_queue:
            break
        if s["template"] not in bindable:
            s["template"] = "UIShowcase"
            s["visual_props"].pop("chart_items", None)
            s["visual_props"].pop("diagram_nodes", None)
            s["visual_props"].pop("diagram_edges", None)
            s["visual_props"].setdefault("ui_focus_regions", [{"x": 0.5, "y": 0.45, "zoom": 1.35, "label": "重点区域", "line_index": 0}])
        if vid_queue and not s["visual_props"].get("uploaded_video_file"):
            s["visual_props"]["uploaded_video_file"] = vid_queue.pop(0)
        if img_queue and not s["visual_props"].get("uploaded_image_file"):
            s["visual_props"]["uploaded_image_file"] = img_queue.pop(0)
    return lead, [hook_scene, *middle_scenes, outro_scene]


async def plan_cinematic_script_two_stage(
    *, args: Any, char: dict[str, Any], research_bundle: dict[str, Any], candidates: Sequence[dict[str, Any]],
    public_memories: Sequence[dict[str, Any]], available_memes: list[dict[str, Any]], pinned_meme_ids: Sequence[str],
    uploaded_image_files: Sequence[str], uploaded_video_files: Sequence[str] = (), op_llm=None, runtime=None,
    custom_planner_fn=None, plan_schema: dict[str, Any] | None = None, system_message: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Run legacy custom_planner_fn, Two-Stage LLM Director, or 6-mode cinematic fallback."""
    duration_preset, target_scenes = resolve_duration_and_scenes(args.video_mode, getattr(args, "duration_preset", None), getattr(args, "max_scenes", None))
    intent = _ra.build_aqua_video_planner_intent(args.video_mode, pinned_meme_ids=pinned_meme_ids, uploaded_image_files=uploaded_image_files)
    sys_msg = system_message or _build_planner_system_message(char, intent)
    packet = {
        "topic": args.topic, "video_mode": args.video_mode, "duration_preset": duration_preset, "brief_text": args.brief_text,
        "reference_pages": research_bundle.get("pages") or [], "reference_diagnostics": research_bundle.get("diagnostics") or [],
        "research_facts": research_bundle.get("facts") or [], "research_modules": research_bundle.get("modules") or [],
        "watched_digest": research_bundle.get("watched_digest") or [], "style_note": args.style_note, "fps": args.fps,
        "include_avatar": args.include_avatar, "bgm_enabled": args.bgm_enabled, "line_pause_seconds": args.line_pause_seconds,
        "scene_pause_seconds": args.scene_pause_seconds, "max_scenes": target_scenes, "pinned_meme_ids": list(pinned_meme_ids),
        "uploaded_image_files": list(uploaded_image_files), "uploaded_video_files": list(uploaded_video_files),
        "available_templates": list(ALL_TEMPLATES),
        "available_avatar_cues": ["idle", "cheerful", "curious", "proud", "thinking", "surprised", "calm"],
        "available_memes": [{"id": m["id"], "title": m["title"], "emotions": m["emotions"], "tags": m["tags"], "description": m.get("description", "")} for m in available_memes],
        "public_candidates": list(candidates)[:6], "public_memories": [{"id": m["id"], "content": m["content"]} for m in list(public_memories)[:6]],
    }
    messages = [sys_msg, {"role": "user", "content": dump(packet)}]
    schema = plan_schema or {"type": "object", "required": ["title", "summary", "tags", "scenes", "sources"], "properties": {"title": {"type": "string"}, "summary": {"type": "string"}, "tags": {"type": "array"}, "scenes": {"type": "array"}, "sources": {"type": "array"}}}

    if callable(custom_planner_fn):
        raw_plan = custom_planner_fn(messages, schema, packet)
        if inspect.isawaitable(raw_plan):
            raw_plan = await raw_plan
        plan_dict = raw_plan.model_dump(mode="json") if hasattr(raw_plan, "model_dump") else dict(raw_plan)
        plan_dict.setdefault("duration_preset", duration_preset)
        return normalize_cinematic_scene_defaults(plan_dict), "custom_planner", None

    planner_error: str | None = None
    if op_llm is not None and callable(getattr(op_llm, "generate_json", None)):
        try:
            slot = runtime.offline_generation("background") if runtime is not None and callable(getattr(runtime, "offline_generation", None)) else asyncio.Lock()
            async with asyncio.timeout(120), slot:
                with without_model_identity():
                    stage_a_msgs = [sys_msg, {"role": "user", "content": dump(packet | {"director_stage": "A_outline", "director_rules": ["相邻分镜禁止相同 template", "多章节使用 ChapterBumper", "流程用 ArchitectureFlow，截图/视频用 UIShowcase", "交替使用 dark_tech / aqua_light / cinema 主题"]})}]
                    raw_a = await op_llm.generate_json(stage_a_msgs, schema, max_tokens=6144)
                    if isinstance(raw_a, dict) and isinstance(raw_a.get("scenes"), list) and raw_a["scenes"]:
                        raw_a.setdefault("duration_preset", duration_preset)
                        return normalize_cinematic_scene_defaults(raw_a), "llm", None
                    outline = VideoOutlinePlan.model_validate(raw_a)
                    stage_b_msgs = [sys_msg, {"role": "user", "content": dump({"director_stage": "B_scenes", "outline": outline.model_dump(mode="json"), "research_facts": packet["research_facts"], "uploaded_image_files": packet["uploaded_image_files"], "uploaded_video_files": packet["uploaded_video_files"], "pinned_meme_ids": packet["pinned_meme_ids"], "available_memes": packet["available_memes"][:12]})}]
                    raw_b = await op_llm.generate_json(stage_b_msgs, schema, max_tokens=6144)
                    if isinstance(raw_b, dict) and isinstance(raw_b.get("scenes"), list) and raw_b["scenes"]:
                        assembled = {
                            "title": raw_b.get("title") or outline.title, "summary": raw_b.get("summary") or outline.summary,
                            "tags": raw_b.get("tags") or outline.tags, "video_mode": args.video_mode, "duration_preset": duration_preset,
                            "fps": args.fps, "include_avatar": args.include_avatar, "bgm_enabled": args.bgm_enabled,
                            "line_pause_seconds": args.line_pause_seconds, "scene_pause_seconds": args.scene_pause_seconds,
                            "scenes": raw_b["scenes"],
                            "sources": raw_b.get("sources") or _resolve_sources_for_plan(topic=args.topic, brief_text=args.brief_text, research_bundle=research_bundle, candidates=candidates, public_memories=public_memories),
                        }
                        return normalize_cinematic_scene_defaults(assembled), "llm", None
        except Exception as exc:
            planner_error = _ra._sanitize_diagnostic(f"{type(exc).__name__}: {exc}")

    sources = _resolve_sources_for_plan(topic=args.topic, brief_text=args.brief_text, research_bundle=research_bundle, candidates=candidates, public_memories=public_memories)
    lead, raw_scenes = build_cinematic_fallback_scenes(
        topic=args.topic, style_note=args.style_note, sources=sources, target_scenes=target_scenes, duration_preset=duration_preset,
        available_memes=available_memes, video_mode=args.video_mode, brief_text=args.brief_text, research_bundle=research_bundle,
        candidates=candidates, pinned_meme_ids=pinned_meme_ids, uploaded_image_files=uploaded_image_files, uploaded_video_files=uploaded_video_files,
    )
    fallback_plan = {
        "title": lead, "summary": f"Aqua 围绕「{args.topic[:100]}」的浅海手账创作。引用来源：" + "；".join(str(s.get("url") or s.get("title") or "") for s in sources[:3]),
        "tags": ["Aqua", "浅海手账", args.video_mode[:16]], "video_mode": args.video_mode, "duration_preset": duration_preset,
        "fps": args.fps, "include_avatar": args.include_avatar, "bgm_enabled": args.bgm_enabled,
        "line_pause_seconds": args.line_pause_seconds, "scene_pause_seconds": args.scene_pause_seconds, "scenes": raw_scenes, "sources": sources,
    }
    return normalize_cinematic_scene_defaults(fallback_plan), "fallback", planner_error


def pitch_video_ideas(
    *, candidates: Sequence[dict[str, Any]], public_memories: Sequence[dict[str, Any]], brief_hint: str = "", max_ideas: int = 5
) -> list[dict[str, Any]]:
    """Generate concrete VideoIdeaPitch items triaged across video modes and dynamic posts."""
    pitches: list[VideoIdeaPitch] = []
    hint = str(brief_hint or "").strip()
    if hint:
        assessment = assess_editorial_channel(hint, brief_text=hint)
        pitches.append(VideoIdeaPitch(
            idea_id="pitch_hint_focus", title=f"围绕「{hint[:24]}」做一期结构化实录", topic=hint[:160],
            channel=assessment.recommended_channel, video_mode=assessment.video_mode, duration_preset=assessment.duration_preset,
            hook="先把最戳人的前后变化甩出来，再拆开每一步操作细节。", visual_angle="UIShowcase + ChartCompare 前后对比 + Aqua 实况解说",
            source_refs=["operator_hint"], why_this_channel=assessment.reasoning,
        ))
    if len(candidates) >= 2:
        top_bvids = [str(c.get("bvid") or "") for c in candidates[:4] if c.get("bvid")]
        titles = "、".join(str(c.get("title") or c.get("bvid"))[:18] for c in candidates[:3])
        pitches.append(VideoIdeaPitch(
            idea_id="pitch_watched_roundup", title="Aqua 近期看片盘点：这几部视频最值得拎出来聊",
            topic=f"盘点近期看过的 {len(top_bvids)} 部视频（{titles}）", channel="video", video_mode="watched_roundup", duration_preset="standard",
            hook="最近连刷了几部硬核视频，有几处反差细节不吐不快！", visual_angle="QuoteSpotlight 原句摘录 + ChartCompare 看点评分 + 鲸鱼表情包串场",
            source_refs=top_bvids, why_this_channel="多部视频存在共同脉络与对比维度，适合做串联盘点视频。",
        ))
    elif len(candidates) == 1:
        c0 = candidates[0]
        bv = str(c0.get("bvid") or "")
        pitches.append(VideoIdeaPitch(
            idea_id="pitch_single_commentary", title=f"锐评「{str(c0.get('title') or bv)[:24]}」的隐藏槽点",
            topic=f"围绕视频 {bv}（{c0.get('title', '')}）做短平快杂谈吐槽", channel="video", video_mode="commentary", duration_preset="short",
            hook="三秒抛出一针见血的观后反差，顺便抓两个核心片段聊透。", visual_angle="TitleCard 痛点开场 + QuoteSpotlight 亮点拆解",
            source_refs=[bv] if bv else [], why_this_channel="单个高信息量视频做 1 分钟短杂谈最容易完播。",
        ))
    if any(k in hint.lower() for k in ("lora", "微调", "训练", "日常")) or not hint:
        pitches.append(VideoIdeaPitch(
            idea_id="pitch_lora_daily_vlog", title="LoRA 微调与本地实验日志：调参前后到底差在哪",
            topic="记录最近一次 LoRA 微调实验：数据集清理、前后效果对比与踩坑复盘", channel="video", video_mode="daily_vlog", duration_preset="standard",
            hook="同样一段提示词，微调前像背书，微调后终于有点鲸鱼味了？", visual_angle="UIShowcase 样本截图 + ChartCompare 指标对比 + KineticStatement 结论",
            source_refs=["daily_vlog"], why_this_channel="微调过程具备明确前后对比与实验曲线，视觉信息增益高。",
        ))
    pitches.append(VideoIdeaPitch(
        idea_id="pitch_project_showcase", title="Tuber Studio 创作工坊：从脚本策划到音画帧对齐",
        topic="展示 Tuber Studio 本地视频创作工坊架构、多模板 Remotion 引擎与角色协同流程", channel="video", video_mode="project_showcase", duration_preset="standard",
        hook="为什么本地写代码也能做出会卡点、有运镜的二次元杂谈视频？", visual_angle="ArchitectureFlow 数据流动画 + CodeWalkthrough 高亮 + UIShowcase 工作台演示",
        source_refs=["workspace_docs"], why_this_channel="项目架构与工作台界面需要流程图、代码与截图联动讲解。",
    ))
    mem_hint = str(public_memories[0].get("content") or "刚结束一轮高强度调参，先干一碗热白饭保鲜")[:48] if public_memories else "刚把脚本跑通，先吃口白饭低功耗保鲜"
    pitches.append(VideoIdeaPitch(
        idea_id="pitch_light_dynamic", title="轻量动态随想：一句日常冒泡或下期预告",
        topic=f"发一条轻量动态分享：{mem_hint}", channel="dynamic", video_mode="commentary", duration_preset="short",
        hook="一句话交代当下状态与下期预告，配一张鲸鱼表情包直接发。", visual_angle="单图动态卡片（无需做多镜视频）",
        source_refs=["public_memory"] if public_memories else [], why_this_channel="单点生活感或预告无需铺开多镜脚本，发动态更轻快自然。",
    ))
    return [item.model_dump(mode="json") for item in pitches[:max(1, min(10, int(max_ideas)))]]
