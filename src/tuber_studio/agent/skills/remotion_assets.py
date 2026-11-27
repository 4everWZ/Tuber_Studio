"""Visual asset staging, procedural shallow-sea BGM/SFX, and 80/20 Aqua script prompt helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import inspect
import io
import ipaddress
import json
import math
import os
import re
import shutil
import struct
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urldefrag

import httpx
from PIL import Image, ImageSequence
from pydantic import Field, StringConstraints, field_validator

from ...core.editorial import DurationPreset as DurationPreset
from ...core.editorial import VideoMode as VideoMode
from ...core.models import StrictModel
from ...core.validation import linked
from ...infra.inference.validation import validate_image
from ...infra.web_client.fetch import MAX_URL, fetch_excerpt, public_https_address, public_ip

TemplateId = Literal[
    "TitleCard", "CodeWalkthrough", "ChartCompare", "MemeCutaway", "QuoteSpotlight",
    "TimelineSummary", "ChapterBumper", "ArchitectureFlow", "UIShowcase", "KineticStatement",
]
AvatarCue = Literal["idle", "cheerful", "curious", "proud", "thinking", "surprised", "calm"]
SceneTheme = Literal["dark_tech", "aqua_light", "cinema"]
SceneTransition = Literal["cut", "fade", "slide_left", "wipe_right", "zoom_punch", "flash"]
AvatarLayout = Literal["corner_float", "compact", "featured", "hidden"]
CameraMotion = Literal["static", "push_in", "pull_out", "pan_right", "focus_target"]
LineSfx = Literal["none", "pop", "whoosh", "chime", "chapter_hit"]


class ChartPoint(StrictModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    value: float = Field(ge=-1e9, le=1e9)
    unit: str = Field(default="", max_length=16)


class DiagramNode(StrictModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    sublabel: str = Field(default="", max_length=120)
    lane: str = Field(default="", max_length=40)
    tone: Literal["sky", "mint", "amber", "coral", "slate"] = "sky"

    @field_validator("lane", mode="before")
    @classmethod
    def _coerce_lane(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:40]


class DiagramEdge(StrictModel):
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
    target: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
    label: str = Field(default="", max_length=60)


class CodeStep(StrictModel):
    title: str = Field(default="", max_length=80)
    code_snippet: Annotated[str, StringConstraints(min_length=1, max_length=3500)]
    highlight_lines: list[Annotated[int, Field(ge=1, le=1000)]] = Field(default_factory=list, max_length=40)


class UiFocusRegion(StrictModel):
    x: float = Field(ge=0.0, le=100.0)
    y: float = Field(ge=0.0, le=100.0)
    zoom: float = Field(default=1.6, ge=1.0, le=4.0)
    label: str = Field(default="", max_length=80)
    line_index: int | None = Field(default=None, ge=0, le=16)


class SceneVisualProps(StrictModel):
    subtitle: str = Field(default="", max_length=200)
    bullets: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]] = Field(default_factory=list, max_length=8)
    code_snippet: str = Field(default="", max_length=3500)
    language: Literal["python", "typescript", "javascript", "json", "bash", "markdown", "text"] = "text"
    chart_items: list[ChartPoint] = Field(default_factory=list, max_length=8)
    meme_asset_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    citation_label: str = Field(default="", max_length=120)
    uploaded_image_file: str | None = Field(default=None, pattern=r"^uploads/[a-zA-Z0-9._-]{1,80}$")
    uploaded_video_file: str | None = Field(default=None, pattern=r"^uploads/[a-zA-Z0-9._-]{1,80}$")
    chapter_number: int | str | None = None
    stat_callout: str = Field(default="", max_length=80)
    diagram_nodes: list[DiagramNode] = Field(default_factory=list, max_length=12)
    diagram_edges: list[DiagramEdge] = Field(default_factory=list, max_length=20)
    code_steps: list[CodeStep] = Field(default_factory=list, max_length=8)
    ui_focus_regions: list[UiFocusRegion] = Field(default_factory=list, max_length=8)

    @field_validator("uploaded_image_file", "uploaded_video_file")
    @classmethod
    def check_uploaded_media_file(cls, value: str | None) -> str | None:
        if value is not None and ".." in value:
            raise ValueError("上传媒体相对路径不能包含上级目录跳转")
        return value

    @field_validator("chapter_number")
    @classmethod
    def check_chapter_number(cls, value: int | str | None) -> int | str | None:
        if value is None:
            return None
        if isinstance(value, str) and len(value.strip()) > 16:
            raise ValueError("章节编号过长")
        if isinstance(value, int) and not 0 <= value <= 999:
            raise ValueError("章节编号超出范围")
        return value


class VideoLineSpec(StrictModel):
    line_id: str = Field(default="", max_length=40, pattern=r"^(?:[a-zA-Z0-9_-]{1,40})?$")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    avatar_cue: AvatarCue = "idle"
    highlight_code: str | None = Field(default=None, max_length=200)
    emphasis: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24)]] = Field(default_factory=list, max_length=3)
    reveal_step: int | None = Field(default=None, ge=0, le=16)
    visual_focus: str | None = Field(default=None, max_length=80)
    sfx: LineSfx = "none"

    @field_validator("text")
    @classmethod
    def clean_line_text(cls, value: str) -> str:
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("台词包含非法控制字符")
        return value


class VideoSceneSpec(StrictModel):
    scene_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    template: TemplateId
    heading: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    chapter_title: str = Field(default="", max_length=60)
    theme: SceneTheme = "dark_tech"
    transition: SceneTransition = "fade"
    camera_motion: CameraMotion = "static"
    avatar_layout: AvatarLayout | None = None
    visual_props: SceneVisualProps = Field(default_factory=SceneVisualProps)
    lines: list[VideoLineSpec] = Field(min_length=1, max_length=16)
    avatar_cue: AvatarCue = "idle"


class PublicSourceRef(StrictModel):
    source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    kind: Literal["watched_video", "public_incident", "operator_topic", "web_research", "public_memory", "public_comment"]
    scope: Literal["public"] = "public"
    title: str = Field(default="", max_length=160)
    url: str | None = Field(default=None, max_length=512)
    bvid: str | None = Field(default=None, pattern=r"^BV[0-9A-Za-z]{10}$")
    excerpt: str = Field(default="", max_length=500)
    fetch_status: Literal["fetched", "fallback", "manual"] | None = None
    fetch_error: str | None = Field(default=None, max_length=160)

    @field_validator("url")
    @classmethod
    def require_https_url(cls, value: str | None) -> str | None:
        if value is None or not (cleaned := value.strip()):
            return None
        if not cleaned.startswith("https://") or any(ch.isspace() for ch in cleaned):
            raise ValueError("公开来源链接必须为有效的 HTTPS URL")
        return cleaned

AVATAR_STATES: tuple[str, ...] = ("idle", "talking", "cheerful", "confused", "angry", "gift")
AVATAR_MEME_STATES: dict[str, str] = {"hum": "story-hum.gif", "meal": "story-meal.gif", "tail": "story-tail.gif", "token": "story-token.gif", "turn": "motion-turn.gif", "dance": "motion-dance.gif", "jump": "motion-jump.gif", "eat": "motion-eat.gif", "sleep": "motion-sleep.gif"}
DEFAULT_MEME_BY_CUE: dict[str, str] = {"idle": "archive-flat-whale", "cheerful": "archive-megaphone", "curious": "archive-thinking-whale", "thinking": "story-token", "surprised": "archive-laptop-rage", "proud": "story-tail", "calm": "story-meal", "angry": "archive-annoyed-poke"}
DEFAULT_LINE_PAUSE_SECONDS, DEFAULT_SCENE_PAUSE_SECONDS, DEFAULT_OUTRO_HOLD_SECONDS = 0.45, 0.75, 0.60


def _blocked_bundled_memes(meme_catalog) -> tuple[set[str], set[str]]:
    store = getattr(meme_catalog, "store", None)
    if store is None or not hasattr(store, "all"):
        return set(), set()
    try:
        cols = {col["name"] for col in store.all("PRAGMA table_info(meme_assets)")}
        cond = "(enabled=0 OR usage_scope='interaction_only')" if "usage_scope" in cols else "enabled=0"
        rows = store.all(f"SELECT blob_name, sha256 FROM meme_assets WHERE storage_kind='existing_user_assets' AND deleted=0 AND {cond}")
        return ({str(r["blob_name"]) for r in rows if r.get("blob_name")}, {str(r["sha256"]) for r in rows if r.get("sha256")})
    except Exception:
        return set(), set()


def _resolve_catalog_item_path(meme_catalog, item: dict[str, Any], asset_id: str, char_key: str) -> Path | None:
    raw_path = item.get("file_path") or item.get("path")
    for fn in (
        lambda: meme_catalog.resolve(asset_id, char_key, allow_disabled=False).get("path") if hasattr(meme_catalog, "resolve") else None,
        lambda: meme_catalog._path(meme_catalog._row(asset_id)) if hasattr(meme_catalog, "_row") and hasattr(meme_catalog, "_path") else None,
    ):
        if raw_path is not None:
            break
        try:
            raw_path = fn()
        except (ValueError, OSError):
            return None
    resolved = Path(raw_path) if raw_path is not None else None
    return resolved if resolved and resolved.is_file() else None


def load_whale_meme_catalog(assets_root: Path, *, meme_catalog=None, character_id: str = "whale") -> list[dict[str, Any]]:
    """Load public whale meme metadata from `assets/whale/memes/manifest.json` merged with MemeCatalog."""
    memes_dir, manifest_path = Path(assets_root) / "assets/whale/memes", Path(assets_root) / "assets/whale/memes/manifest.json"
    catalog: list[dict[str, Any]] = []
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            payload = None
        for raw in (payload.get("entries") if isinstance(payload, dict) and isinstance(payload.get("entries"), list) else ()):
            if not isinstance(raw, dict):
                continue
            item_id, rel_path = str(raw.get("id") or "").strip(), str(raw.get("path") or "").strip()
            if not item_id or not rel_path or "/" in rel_path or "\\" in rel_path or ".." in rel_path or not (file_path := memes_dir / rel_path).is_file():
                continue
            catalog.append({
                "id": item_id, "title": str(raw.get("title") or item_id),
                "tags": [str(t) for t in (raw.get("tags") or [])[:8]], "emotions": [str(e) for e in (raw.get("emotions") or [])[:4]],
                "path": rel_path, "source_path": file_path, "file_path": file_path,
                "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(), "alias_ids": [item_id],
                "description": str(raw.get("description") or "")[:120],
            })
    if meme_catalog is None:
        return catalog
    blocked_blobs, blocked_shas = _blocked_bundled_memes(meme_catalog)
    if blocked_blobs or blocked_shas:
        catalog = [e for e in catalog if e["path"] not in blocked_blobs and e.get("sha256") not in blocked_shas]
    by_rel_path, by_sha256 = {e["path"]: e for e in catalog}, {e["sha256"]: e for e in catalog if e.get("sha256")}
    char_key = str(character_id or "whale").strip() or "whale"
    for item in meme_catalog.list(char_key, include_disabled=False, usage_scope="all_channels"):
        if not isinstance(item, dict) or item.get("missing") or not item.get("enabled", True) or item.get("usage_scope") == "interaction_only":
            continue
        asset_id = str(item.get("asset_id") or "").strip()
        if not asset_id or (resolved_path := _resolve_catalog_item_path(meme_catalog, item, asset_id, char_key)) is None:
            continue
        blob_name, item_sha = str(item.get("blob_name") or resolved_path.name), str(item.get("sha256") or "")
        bundled = (by_rel_path.get(blob_name) or (by_sha256.get(item_sha) if item_sha else None)) if item.get("storage_kind") == "existing_user_assets" else None
        if bundled is not None:
            if asset_id not in bundled["alias_ids"]:
                bundled["alias_ids"].append(asset_id)
            if item.get("title"):
                bundled["title"] = str(item["title"])
            for k, lim in (("tags", 8), ("emotions", 4)):
                if isinstance(item.get(k), list):
                    bundled[k] = [str(x) for x in item[k][:lim]]
            if item.get("description") is not None:
                bundled["description"] = str(item["description"])[:120]
            if item_sha and not bundled.get("sha256"):
                bundled["sha256"] = item_sha
        else:
            catalog.append({
                "id": asset_id, "title": str(item.get("title") or asset_id),
                "tags": [str(t) for t in (item.get("tags") or [])[:8]], "emotions": [str(e) for e in (item.get("emotions") or [])[:4]],
                "path": blob_name, "source_path": resolved_path, "file_path": resolved_path,
                "sha256": item_sha, "alias_ids": [asset_id], "description": str(item.get("description") or "")[:120],
                "storage_kind": str(item.get("storage_kind") or "library"),
            })
    return catalog


def resolve_catalog_meme_id(catalog: Sequence[dict[str, Any]], meme_id: str | None) -> str | None:
    clean = str(meme_id or "").strip()
    if not clean:
        return None
    for entry in catalog:
        canonical = str(entry.get("id") or "").strip()
        if canonical and (clean == canonical or clean in (entry.get("alias_ids") or ())):
            return canonical
    return None


def _extract_gif_frames(src_path: Path, frames_dir: Path, rel_prefix: str) -> tuple[list[str], int]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_paths, durations = [], []
    with Image.open(src_path) as img:
        for idx, frame in enumerate(ImageSequence.Iterator(img)):
            if idx >= 48:
                break
            fname = f"{idx:02d}.png"
            frame.convert("RGBA").save(frames_dir / fname, format="PNG")
            frame_paths.append(f"{rel_prefix}/{fname}")
            durations.append(max(40, min(800, int(frame.info.get("duration") or 120))))
    return frame_paths, max(40, int(round(sum(durations) / len(durations))) if durations else 120)


def _stage_gif_or_file_entry(src_file: Path, pub_dir: Path, state_or_id: str, rel_folder: str) -> tuple[str, list[str], int]:
    pub_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, pub_dir / src_file.name)
    frames, avg_ms = ([], 110)
    if src_file.suffix.lower() == ".gif":
        frames, avg_ms = _extract_gif_frames(src_file, pub_dir / "frames" / state_or_id, f"{rel_folder}/frames/{state_or_id}")
    return f"{rel_folder}/{src_file.name}", frames, avg_ms


def pick_distinct_scene_meme_id(cue: str, used_ids: set[str], available_ids: set[str], preferred_id: str | None = None) -> str | None:
    if preferred_id and preferred_id in available_ids and preferred_id not in used_ids:
        return preferred_id
    ordered = [
        DEFAULT_MEME_BY_CUE.get(str(cue or "").strip(), "archive-megaphone"),
        "archive-megaphone", "story-token", "archive-laptop-rage", "story-meal", "archive-flat-whale",
        "story-tail", "motion-turn", "story-hum", "archive-craving-cake", "archive-thinking-whale", *sorted(available_ids),
    ]
    for candidate in ordered:
        if candidate in available_ids and candidate not in used_ids:
            return candidate
    return preferred_id if (preferred_id and preferred_id in available_ids) else next(iter(sorted(available_ids)), None)


def stage_whale_visual_assets(
    assets_root: Path, public_dir: Path, *, include_avatar: bool, requested_meme_ids: list[str],
    scene_cues: list[str], meme_catalog=None, character_id: str = "whale",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy real Aqua avatar GIFs and whale meme stickers into `public/` and extract GIF frames."""
    whale_dir, memes_src_dir, avatar_assets = Path(assets_root) / "assets/whale", Path(assets_root) / "assets/whale/memes", {}
    if include_avatar and whale_dir.is_dir():
        avatar_pub = public_dir / "avatar"
        for state, src_gif in [(s, whale_dir / f"{s}.gif") for s in AVATAR_STATES] + [(s, memes_src_dir / fn) for s, fn in AVATAR_MEME_STATES.items()]:
            if src_gif.is_file():
                rel_file, frames, avg_ms = _stage_gif_or_file_entry(src_gif, avatar_pub, state, "avatar")
                avatar_assets[state] = {"file": rel_file, "frames": frames, "frameDurationMs": avg_ms}
    catalog = load_whale_meme_catalog(assets_root, meme_catalog=meme_catalog, character_id=character_id)
    by_id = {entry["id"]: entry for entry in catalog}
    alias_to_canonical = {str(alias): entry["id"] for entry in catalog for alias in (entry.get("alias_ids") or (entry["id"],))}
    wanted_ids: list[str] = []
    candidates = [alias_to_canonical.get(str(r or "").strip()) for r in requested_meme_ids]
    candidates += [DEFAULT_MEME_BY_CUE.get(str(c or "").strip(), "archive-thinking-whale") for c in scene_cues]
    candidates += ["archive-megaphone", "archive-laptop-rage", "story-meal", "story-tail", "story-token", "archive-flat-whale"]
    for cid in candidates:
        if cid and cid in by_id and cid not in wanted_ids:
            wanted_ids.append(cid)
    meme_assets: dict[str, Any] = {}
    if wanted_ids:
        memes_pub = public_dir / "memes"
        for meme_id in wanted_ids[:14]:
            entry = by_id[meme_id]
            src_file = Path(entry.get("file_path") or entry.get("source_path") or (memes_src_dir / entry["path"]))
            if src_file.is_file():
                rel_file, frames, avg_ms = _stage_gif_or_file_entry(src_file, memes_pub, meme_id, "memes")
                meme_assets[meme_id] = {"id": meme_id, "title": entry["title"], "file": rel_file, "frames": frames, "frameDurationMs": avg_ms}
    return avatar_assets, meme_assets


def _write_mono_wav(path: Path, samples: list[int], *, sample_rate: int = 24000) -> None:
    buf = struct.pack(f"<{len(samples)}h", *(max(-32767, min(32767, s)) for s in samples))
    with wave.open(str(path), "wb") as wf:
        wf.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        wf.writeframes(buf)


def synthesize_bgm_and_sfx(audio_dir: Path, *, duration_seconds: float, sample_rate: int = 24000) -> tuple[str, str]:
    """Synthesize a gentle shallow-sea marimba/pad BGM loop and a crisp transition ding WAV."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    total_samples = int(round(max(4.0, float(duration_seconds) + 0.5) * sample_rate))
    chords = ((261.63, 329.63, 392.00, 493.88), (293.66, 349.23, 440.00, 523.25), (329.63, 392.00, 493.88, 587.33), (349.23, 440.00, 523.25, 659.25))
    arp_notes = (523.25, 587.33, 659.25, 783.99, 659.25, 587.33, 523.25, 440.00)
    beat_samples, fade_in, fade_out = int(0.5 * sample_rate), int(0.8 * sample_rate), int(1.2 * sample_rate)
    bgm_samples = []
    for i in range(total_samples):
        t, bp = i / sample_rate, (i % beat_samples) / sample_rate
        pad = sum(math.sin(2.0 * math.pi * f * t) for f in chords[int(t // 4.0) % 4]) * 0.22
        af = arp_notes[(i // beat_samples) % 8]
        pluck = (math.sin(2.0 * math.pi * af * bp) + 0.25 * math.sin(4.0 * math.pi * af * bp)) * math.exp(-bp * 5.2) * (1.0 - math.exp(-bp * 90.0)) * 0.45
        env = min(1.0, i / max(1, fade_in)) * (max(0.0, (total_samples - i) / max(1, fade_out)) if i > total_samples - fade_out else 1.0)
        bgm_samples.append(int((pad + pluck) * 1500.0 * env))
    _write_mono_wav(audio_dir / "bgm.wav", bgm_samples, sample_rate=sample_rate)
    sfx_samples = [
        int(math.sin(2.0 * math.pi * (783.99 if i / sample_rate < 0.08 else 1046.50) * (i / sample_rate)) * 4200.0
            * math.exp(-((i / sample_rate) if i / sample_rate < 0.08 else (i / sample_rate - 0.08)) * 22.0)
            * (1.0 - math.exp(-((i / sample_rate) if i / sample_rate < 0.08 else (i / sample_rate - 0.08)) * 180.0)))
        for i in range(int(0.22 * sample_rate))
    ]
    _write_mono_wav(audio_dir / "transition.wav", sfx_samples, sample_rate=sample_rate)
    return "audio/bgm.wav", "audio/transition.wav"


_MODE_PLANNER_STRUCTURES: dict[str, str] = {
    "commentary": (
        "当前视频类型为「热点吐槽 / 杂谈（commentary）」，去掉「总-分-总」科普播音腔，改用 B 站轻快短吐槽结构：\n"
        "1) 第 1 镜（TitleCard）：3 秒钩子开场，一针见血抛出痛点或反差吐槽；\n"
        "2) 中间 1~N 镜（CodeWalkthrough / ChartCompare / QuoteSpotlight / KineticStatement / MemeCutaway）：抓最戳人的细节对比或反转槽点展开；\n"
        "3) 末镜（MemeCutaway 或 TimelineSummary）：用 1 句轻傲娇、干白饭或「低功耗保鲜摸鱼」式俏皮收尾。"
    ),
    "daily_vlog": (
        "当前视频类型为「工坊实验 / 微调日记（daily_vlog）」，用真实折腾日志的节奏讲述：\n"
        "1) 第 1 镜（TitleCard）：抛出本次实验动机或折腾前后反差（如 LoRA 微调前后变化）；\n"
        "2) 中间多镜（ChapterBumper / UIShowcase / ChartCompare / CodeWalkthrough / KineticStatement）：按实验配置、效果对比与踩坑复盘推进；\n"
        "3) 末镜（TimelineSummary 或 MemeCutaway）：总结结论与下一步计划，欢迎评论区交流。"
    ),
    "watched_roundup": (
        "当前视频类型为「近期浏览 / 热点盘点（watched_roundup）」，串讲多条看过的好内容：\n"
        "1) 第 1 镜（TitleCard）：给出本期盘点主题与爽点概览；\n"
        "2) 中间多镜（ChapterBumper / QuoteSpotlight / ChartCompare / KineticStatement / MemeCutaway）：逐条点评亮点与槽点；\n"
        "3) 末镜（TimelineSummary）：汇总榜单要点并引导互动。"
    ),
    "self_intro": (
        "当前视频类型为「角色与工坊自我介绍（self_intro）」，结合角色卡设定与传入的 `brief_text` 资料自然表达：\n"
        "1) 第 1 镜（TitleCard）：鲜明身份与反差感钩子开场（80% 鲸鱼娘吐槽感 + 20% 本地开发者底色）；\n"
        "2) 中间 1~N 镜（ArchitectureFlow / UIShowcase / QuoteSpotlight / MemeCutaway / CodeWalkthrough / TimelineSummary）：展开核心能力、工坊日常工作流与陪伴生态；\n"
        "3) 末镜（MemeCutaway 或 TimelineSummary）：友好社区互动号召收尾（欢迎三连/评论区搭话）。"
    ),
    "project_showcase": (
        "当前视频类型为「开源项目 / 作品展示（project_showcase）」，结合 `brief_text` 与抓取到的 GitHub README / 项目主页内容：\n"
        "1) 第 1 镜（TitleCard）：直击真实痛点或最亮眼项目卖点开场；\n"
        "2) 中间 1~N 镜（ChapterBumper / ArchitectureFlow / UIShowcase / CodeWalkthrough / ChartCompare / QuoteSpotlight）：拆解架构流程、核心特性、代码或界面亮点；\n"
        "3) 末镜（TimelineSummary 或 MemeCutaway）：说明适用人群与开源地址，并把公开仓库/项目 URL 原样保留在 summary 与末镜 visual_props.citation_label。"
    ),
    "explainer": (
        "当前视频类型为「原理拆解 / 知识科普（explainer）」，保持轻快口语化但不失条理：\n"
        "1) 第 1 镜（TitleCard）：用反常识疑问或具体卡点抛出问题钩子；\n"
        "2) 中间 1~N 镜（ChapterBumper / ArchitectureFlow / CodeWalkthrough / ChartCompare / KineticStatement / QuoteSpotlight / TimelineSummary）：分步骤或对比把原理讲透；\n"
        "3) 末镜（TimelineSummary 或 MemeCutaway）：用一句好记的话完成核心结论复盘。"
    ),
}


def build_aqua_video_planner_intent(
    video_mode: VideoMode = "commentary",
    pinned_meme_ids: Sequence[str] = (),
    uploaded_image_files: Sequence[str] = (),
) -> str:
    """Return the 80/20 Aqua Bilibili video script planner instruction parameterized by mode."""
    mode_block = _MODE_PLANNER_STRUCTURES.get(video_mode, _MODE_PLANNER_STRUCTURES["commentary"])
    extra_lines: list[str] = []
    clean_pins = [str(p).strip() for p in pinned_meme_ids if str(p).strip()]
    clean_uploads = [str(u).strip() for u in uploaded_image_files if str(u).strip()]
    if clean_pins:
        extra_lines.append(f"- 用户已置顶图库表情 {clean_pins}，请优先将它们分配到不同分镜的 visual_props.meme_asset_id（相邻分镜禁止重复同一表情）；\n")
    if clean_uploads:
        extra_lines.append(f"- 用户上传了本次专属公开配图/截图/片段 {clean_uploads}，请依次绑定到 UIShowcase / QuoteSpotlight / MemeCutaway / CodeWalkthrough 分镜的 visual_props.uploaded_image_file 或 uploaded_video_file；\n")
    extra_block = "".join(extra_lines)
    return (
        "只基于传入的公开来源、brief_text 与 reference_pages 生成结构化 VideoScriptPlan JSON，并将 video_mode 原样设为传入模式。"
        "视觉风格是「浅海手账 / 清爽二次元杂谈桌 + Aqua 浮动虚拟主播」，"
        "人设遵循 80% 鲸鱼娘 OC（灵动吐槽、轻傲娇、偶尔白饭/摸鱼梗，自称「我」「Aqua」「本鲸」但不自称「大肥鱼」）、20% 本地开发者底色。\n"
        f"{mode_block}\n"
        "配音节奏要求（为 TTS 合成优化，程序会自动在句间插入 0.45s、切镜插入 0.75s 气口停顿）：\n"
        "- 每条口播台词 lines[].text 控制在 10~22 个字左右的短促口语，一句只讲一个爆点，少用生僻英文缩写堆砌；\n"
        "- 关键台词可在 emphasis 填入 1~2 个必须直接出自该句 text 原文的亮眼关键词，用于 B 站综艺花字高亮；\n"
        "- 请为每一镜的 visual_props.meme_asset_id 从 available_memes 中挑选【互不重复】的鲸鱼表情包/动图，并给每句分配丰富的 avatar_cue（curious/thinking/surprised/cheerful/proud/calm）；\n"
        f"{extra_block}"
        "- 可用模板严格限于 TitleCard、CodeWalkthrough、ChartCompare、MemeCutaway、QuoteSpotlight、TimelineSummary、ChapterBumper、ArchitectureFlow、UIShowcase、KineticStatement；严禁写可执行代码或引用私人记忆与私信。"
    )


def _extract_brief_points(brief_text: str, sources: list[Any], topic: str) -> list[str]:
    points = [clean[:110] for raw in re.split(r"[\r\n。！？；;]+", brief_text or "") if len(clean := re.sub(r"^[#>*\-\d.\s]+", "", raw).strip()) >= 4]
    for src in sources:
        for cand in (getattr(src, "title", ""), getattr(src, "excerpt", "")):
            if len(clean := " ".join(str(cand or "").split()).strip()) >= 4:
                points.append(clean[:110])
    return list(dict.fromkeys(points)) or [topic[:80]]


def _preferred_showcase_url(sources: list[Any]) -> str:
    urls = [str(getattr(s, "url", "") or "").strip() for s in sources if getattr(s, "url", None)]
    return next((u for u in urls if "github.com/" in u.lower()), urls[0] if urls else "")


def build_fallback_aqua_scenes(
    *, topic: str, style_note: str, sources: list[Any], max_scenes: int, available_memes: list[dict[str, Any]],
    video_mode: VideoMode = "commentary", brief_text: str = "", pinned_meme_ids: Sequence[str] = (), uploaded_image_files: Sequence[str] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """Construct lively 80/20 Aqua fallback scenes supporting 4 video modes and 2-10 scenes."""
    lead, meme_ids, used_memes = topic[:48], {m["id"] for m in available_memes}, set()
    pinned_queue = list(dict.fromkeys(
        c for pid in pinned_meme_ids if (c := resolve_catalog_meme_id(available_memes, pid)) and c in meme_ids
    ))
    points = _extract_brief_points(brief_text, sources, topic)
    excerpt = (sources[0].excerpt[:90] if sources and getattr(sources[0], "excerpt", "") else "") or points[0][:90]
    showcase_url = _preferred_showcase_url(sources)

    def _take_meme(cue: str, preferred: str | None = None) -> str | None:
        if pinned_queue:
            preferred = pinned_queue.pop(0)
        picked = pick_distinct_scene_meme_id(cue, used_memes, meme_ids, preferred_id=preferred)
        if picked:
            used_memes.add(picked)
        return picked

    hook_copy = {
        "self_intro": ("你好呀，我是 Aqua，你的本地鲸鱼搭档！", "自我介绍来啦，今天不端架子直接聊！", ["Aqua", "自我介绍"]),
        "daily_vlog": (f"今天记录一次{lead[:12]}实测折腾！", "微调前后到底差在哪，直接上实测结果。", ["实测", "折腾"]),
        "watched_roundup": ("最近刷到几条超有意思的内容，一期盘完！", "挑重点拆给你看，省得自己一条条翻。", ["盘完", "重点"]),
        "project_showcase": (f"{lead[:14]}到底解决了什么痛点？", "先看痛点与亮点，再看开源结构！", ["痛点", "开源"]),
        "explainer": (f"为什么{lead[:12]}总让人一头雾水？", "别背定义啦，本鲸拆成几步讲透。", ["一头雾水", "讲透"]),
        "commentary": (f"等等，{lead[:14]}这事也太真实了。", "先别急着点头，本鲸直接给你划重点。", ["太真实了", "划重点"]),
    }.get(video_mode, (f"聊聊{lead[:14]}这件有意思的事。", "本鲸直接给你划重点。", ["划重点", "有意思"]))
    outro_copy = {
        "self_intro": ("想一起折腾创作和代码，记得在评论区叫我！", "以后工坊日常和直播都靠大家多多关照啦。", ["评论区", "关照"]),
        "daily_vlog": ("这次实验记录就到这，下轮调优继续汇报！", "想看哪组对照实验，评论区告诉我。", ["实验记录", "评论区"]),
        "watched_roundup": ("这几条看完是不是还挺解馋？", "还有想让本鲸顺手点评的，评论区丢链接。", ["解馋", "评论区"]),
        "project_showcase": ("仓库地址放在卡片上，欢迎亲自上手试试看！", "觉得顺手就点个 Star，有想法评论区直接开聊。", ["仓库地址", "评论区"]),
        "explainer": ("记住这一条主线，原理其实一点都不玄学。", "还有想拆解的技术点，评论区随时点单！", ["一点都不玄学", "评论区"]),
        "commentary": ("行了，本鲸要切回低功耗模式摸鱼了。", "有更好玩的槽点，记得在评论区叫我。", ["低功耗模式", "评论区"]),
    }.get(video_mode, ("总结先收到这里啦。", "欢迎评论区继续聊。", ["总结", "评论区"]))

    hook_scene = {
        "scene_id": "scene_hook", "template": "TitleCard", "heading": lead, "avatar_cue": "cheerful",
        "visual_props": {
            "subtitle": style_note or points[0][:140], "bullets": [p[:48] for p in points[:3]],
            "meme_asset_id": _take_meme("cheerful", "archive-megaphone"),
            "citation_label": (sources[0].title[:60] if sources and getattr(sources[0], "title", "") else "公开观察"),
        },
        "lines": [{"line_id": "l1", "text": hook_copy[0], "avatar_cue": "surprised", "emphasis": [hook_copy[2][0]]}, {"line_id": "l2", "text": hook_copy[1], "avatar_cue": "cheerful", "emphasis": [hook_copy[2][1]]}],
    }
    target_count = max(1, min(20, int(max_scenes)))
    if target_count == 1:
        return lead, [hook_scene]
    middle_templates_by_mode = {
        "commentary": ("QuoteSpotlight", "MemeCutaway", "ChartCompare", "CodeWalkthrough", "TimelineSummary"),
        "daily_vlog": ("QuoteSpotlight", "ChartCompare", "CodeWalkthrough", "MemeCutaway", "TimelineSummary"),
        "watched_roundup": ("QuoteSpotlight", "ChartCompare", "MemeCutaway", "TimelineSummary", "CodeWalkthrough"),
        "self_intro": ("QuoteSpotlight", "TimelineSummary", "MemeCutaway", "CodeWalkthrough", "ChartCompare"),
        "project_showcase": ("CodeWalkthrough", "QuoteSpotlight", "TimelineSummary", "ChartCompare", "MemeCutaway"),
        "explainer": ("TimelineSummary", "QuoteSpotlight", "CodeWalkthrough", "ChartCompare", "MemeCutaway"),
    }
    middle_templates, middle_scenes, line_idx = middle_templates_by_mode.get(video_mode, middle_templates_by_mode["commentary"]), [], 3
    for idx in range(max(0, target_count - 2)):
        tpl, point, next_point = middle_templates[idx % len(middle_templates)], points[idx % len(points)], points[(idx + 1) % len(points)]
        src = sources[idx % len(sources)] if sources else None
        citation = (getattr(src, "url", None) or getattr(src, "title", None) or "公开来源") if src else "公开来源"
        cue = ("curious", "thinking", "proud", "surprised")[idx % 4]
        vp: dict[str, Any] = {"subtitle": point[:180], "bullets": [point[:48], *([next_point[:48]] if next_point != point else [])], "meme_asset_id": _take_meme(cue), "citation_label": str(citation)[:120]}
        if tpl == "CodeWalkthrough":
            vp |= {"language": "text", "code_snippet": "\n".join(points[:4])[:1200]}
        middle_scenes.append({
            "scene_id": f"scene_detail_{idx + 1}", "template": tpl, "heading": f"第 {idx + 1} 块重点：{point[:20]}", "visual_props": vp, "avatar_cue": cue,
            "lines": [{"line_id": f"l{line_idx}", "text": f"先看第{idx + 1}点，重点其实非常直接。", "avatar_cue": cue, "emphasis": ["非常直接"]}, {"line_id": f"l{line_idx + 1}", "text": f"把{point[:10]}拎出来看就全明白。", "avatar_cue": "thinking", "emphasis": ["全明白"]}],
        })
        line_idx += 2
    outro_citation = showcase_url[:120] if video_mode == "project_showcase" and showcase_url else str((sources[0].url or sources[0].title) if sources else "Aqua 浅海手账")[:120]
    outro_scene = {
        "scene_id": "scene_outro", "template": "TimelineSummary" if video_mode in {"project_showcase", "explainer"} else "MemeCutaway",
        "heading": "开源地址与适用场景" if video_mode == "project_showcase" else ("一句话带走核心结论" if video_mode == "explainer" else "讲完收工，去干一碗热白饭"),
        "visual_props": {"subtitle": showcase_url or excerpt or "听完别光点赞，评论区交出你的看法", "bullets": [p[:48] for p in points[:2]] or ["本鲸低功耗保鲜中", "欢迎评论区接梗"], "meme_asset_id": _take_meme("proud", "story-meal"), "citation_label": outro_citation},
        "lines": [{"line_id": f"l{line_idx}", "text": outro_copy[0], "avatar_cue": "proud", "emphasis": [outro_copy[2][0]]}, {"line_id": f"l{line_idx + 1}", "text": outro_copy[1], "avatar_cue": "cheerful", "emphasis": [outro_copy[2][1]]}],
        "avatar_cue": "proud",
    }
    if upload_queue := [str(u).strip() for u in uploaded_image_files if str(u).strip()]:
        image_templates, candidates = {"QuoteSpotlight", "MemeCutaway", "CodeWalkthrough"}, [*middle_scenes, outro_scene]
        picked = [s for s in candidates if s["template"] in image_templates]
        for s in candidates:
            if len(picked) < len(upload_queue) and s not in picked:
                s["template"] = "MemeCutaway" if s is outro_scene else "QuoteSpotlight"
                s["visual_props"].pop("chart_items", None)
                picked.append(s)
        chosen = {id(s) for s in picked[:len(upload_queue)]}
        for s in candidates:
            if id(s) in chosen and upload_queue:
                s["visual_props"]["uploaded_image_file"] = upload_queue.pop(0)
    return lead, [hook_scene, *middle_scenes, outro_scene]


_REMOTION_PACKAGE_JSON = {
    "name": "aqua-remotion-video",
    "version": "1.0.0",
    "private": True,
    "type": "module",
    "dependencies": {
        "@remotion/cli": "4.0.242",
        "react": "^18.3.1",
        "react-dom": "^18.3.1",
        "remotion": "4.0.242",
    },
}
_REMOTION_ROOT_TSX = (
    'import React from "react";\nimport { Composition } from "remotion";\nimport { AquaVideo, AquaVideoProps } from "./AquaVideo";\n'
    'export const RemotionRoot: React.FC = () => (<Composition id="AquaVideo" component={AquaVideo} durationInFrames={300} fps={30} width={1920} height={1080} '
    'defaultProps={{ title: "Aqua Video", fps: 30, totalFrames: 300, scenes: [], cues: [] }} '
    "calculateMetadata={({ props }: { props: AquaVideoProps }) => ({ durationInFrames: Math.max(1, props.totalFrames || 300), fps: props.fps || 30 })} />);\n"
)
_REMOTION_AQUA_VIDEO_TSX = (
    'import React from "react";\nimport { AbsoluteFill, Audio, Sequence, staticFile, useCurrentFrame } from "remotion";\n'
    "export interface AquaSceneProp { scene_id: string; template: string; heading: string; start_frame: number; duration_frames: number; visual_props: Record<string, unknown>; }\n"
    "export interface AquaCueProp { line_id: string; text: string; start_frame: number; duration_frames: number; avatar_cue: string; }\n"
    "export interface AquaVideoProps { title: string; fps: number; totalFrames: number; audioFile?: string; scenes: AquaSceneProp[]; cues: AquaCueProp[]; }\n"
    "export const AquaVideo: React.FC<AquaVideoProps> = ({ title, audioFile, scenes, cues }) => {\n  const frame = useCurrentFrame();\n"
    "  const activeCue = cues.find((c) => frame >= c.start_frame && frame < c.start_frame + c.duration_frames);\n"
    '  return (<AbsoluteFill style={{ backgroundColor: "#0f172a", color: "#f8fafc", fontFamily: "sans-serif", padding: 64 }}>'
    '{audioFile ? <Audio src={staticFile(audioFile)} /> : null}<div style={{ fontSize: 28, opacity: 0.72, marginBottom: 16 }}>{title}</div>'
    '{scenes.map((scene) => (<Sequence key={scene.scene_id} from={scene.start_frame} durationInFrames={scene.duration_frames}><div style={{ display: "flex", flexDirection: "column", gap: 20 }}>'
    '<div style={{ fontSize: 52, fontWeight: 700 }}>{scene.heading}</div><div style={{ fontSize: 24, opacity: 0.85 }}>{String(scene.visual_props?.subtitle || "")}</div></div></Sequence>))}'
    '{activeCue ? (<div style={{ position: "absolute", bottom: 56, left: 80, right: 80, textAlign: "center", fontSize: 34, backgroundColor: "rgba(15,23,42,0.78)", padding: "16px 24px", borderRadius: 12 }}>{activeCue.text}</div>) : null}</AbsoluteFill>);\n};\n'
)
_REMOTION_INDEX_TS = 'import { registerRoot } from "remotion";\nimport { RemotionRoot } from "./Root";\nregisterRoot(RemotionRoot);\n'


def _format_srt_timestamp(seconds: float) -> str:
    h, rem = divmod(max(0, int(round(seconds * 1000))), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    return _format_srt_timestamp(seconds).replace(",", ".")


def _detect_browser_executable() -> tuple[str | None, str | None]:
    if (env_bin := str(os.environ.get("REMOTION_BROWSER_EXECUTABLE") or "").strip()) and Path(env_bin).is_file():
        return env_bin, "env"
    pf, pf86 = os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")
    for kind, cands in (
        ("chrome", (Path(pf) / "Google/Chrome/Application/chrome.exe", Path(pf86) / "Google/Chrome/Application/chrome.exe",
                    Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser"))),
        ("edge", (Path(pf86) / "Microsoft/Edge/Application/msedge.exe", Path(pf) / "Microsoft/Edge/Application/msedge.exe")),
    ):
        for cand in cands:
            if str(cand) and cand.is_file():
                return str(cand), kind
    for pw_root in (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright", Path.home() / ".cache/ms-playwright"):
        if pw_root.is_dir():
            for pat in ("chromium-*/chrome-win*/chrome.exe", "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
                        "chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell"):
                if (matches := sorted(pw_root.glob(pat), reverse=True)) and matches[0].is_file():
                    return str(matches[0]), "playwright-chromium"
    return None, None


def _build_dry_run_wav_bytes(text: str, *, sample_rate: int = 16000) -> bytes:
    nframes = max(1600, int(round(max(0.6, min(12.0, len(text.strip()) * 0.16)) * sample_rate)))
    samples = b"".join(struct.pack("<h", int(1200 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))) for i in range(nframes))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        wf.writeframes(samples)
    return buf.getvalue()


def _read_wav_pcm(wav_bytes: bytes) -> tuple[tuple[int, int, int], bytes, float]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        p, pcm = r.getparams(), r.readframes(r.getnframes())
        if p.nframes <= 0 or len(pcm) != p.nframes * p.nchannels * p.sampwidth:
            raise ValueError("合成音频帧长度无效或被截断")
        return (p.nchannels, p.sampwidth, p.framerate), pcm, p.nframes / float(p.framerate)


def _concat_wav_pcm(spec: tuple[int, int, int], chunks: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams((spec[0], spec[1], spec[2], 0, "NONE", "not compressed"))
        for pcm in chunks:
            w.writeframes(pcm)
    return buf.getvalue()


def _silence_pcm_for_frames(spec: tuple[int, int, int], frames: int, fps: int) -> bytes:
    return b"" if (frames <= 0 or fps <= 0) else b"\x00" * (max(0, int(round((frames / float(fps)) * spec[2]))) * spec[0] * spec[1])


def _pad_pcm_to_frames(spec: tuple[int, int, int], pcm: bytes, target_frames: int, fps: int) -> bytes:
    if target_frames <= 0 or fps <= 0:
        return pcm
    target_bytes = int(round((target_frames / float(fps)) * spec[2])) * spec[0] * spec[1]
    return pcm + b"\x00" * (target_bytes - len(pcm)) if len(pcm) < target_bytes else pcm


def _resolve_studio_dir(runtime, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    root = getattr(getattr(runtime, "settings", None), "root", None)
    return Path(root) / "remotion-studio" if root else Path(__file__).resolve().parents[4] / "remotion-studio"


def _sanitize_diagnostic(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", "[URL]", str(text or ""))).strip()[:240]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def stage_remotion_scaffold(studio_dir: Path, project_dir: Path) -> None:
    """Export the template source tree and the same dependency versions as the renderer."""
    studio_sources = studio_dir / "src"
    destination = project_dir / "src"
    destination.mkdir(parents=True, exist_ok=True)
    if (studio_sources / "index.ts").is_file() and (studio_dir / "package.json").is_file():
        shutil.copytree(studio_sources, destination, dirs_exist_ok=True)
        for filename in ("package.json", "package-lock.json", "remotion.config.ts", "tsconfig.json"):
            if (source := studio_dir / filename).is_file():
                shutil.copyfile(source, project_dir / filename)
        return
    _write_json(project_dir / "package.json", _REMOTION_PACKAGE_JSON)
    for filename, content in (
        ("index.ts", _REMOTION_INDEX_TS),
        ("Root.tsx", _REMOTION_ROOT_TSX),
        ("AquaVideo.tsx", _REMOTION_AQUA_VIDEO_TSX),
    ):
        (destination / filename).write_text(content, encoding="utf-8")


def _parse_biliup_receipt(stdout: str, stderr: str) -> dict[str, Any]:
    combined = f"{stdout}\n{stderr}"
    bv, av = re.search(r"\b(BV[0-9A-Za-z]{10})\b", combined), re.search(r"\b(?:aid|av)[=: ]+([1-9][0-9]{0,19})\b", combined, re.IGNORECASE)
    return {k: v for k, v in (("bvid", bv.group(1) if bv else None), ("aid", av.group(1) if av else None)) if v}


def _scrub_secrets(text: str, credentials: dict[str, Any]) -> str:
    cleaned = str(text or "")
    for key in ("sessdata", "csrf", "bili_jct", "refresh_token"):
        if len(val := str(credentials.get(key) or "").strip()) >= 4:
            cleaned = cleaned.replace(val, "[REDACTED]")
    return cleaned[:400]


def safe_child_path(root: Path, child: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / child).resolve()
    if not target.is_relative_to(resolved_root) or linked(target):
        raise ValueError("路径超出受控工作目录或包含符号链接")
    return target


def normalize_reference_urls(values: Any) -> list[str]:
    """Strip empties, dedupe preserving order, and enforce max 6 reference URLs."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("reference_urls 须为字符串列表")
    cleaned = list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))
    if len(cleaned) > 6:
        raise ValueError("公开参考链接最多填写 6 条")
    return cleaned


def parse_public_https_url(raw: str) -> httpx.URL:
    """Synchronously validate URL syntax and reject non-HTTPS, credentials, custom ports, localhost, or private IPs."""
    clean = str(raw or "").strip()
    if not 1 <= len(clean) <= MAX_URL or any(ch.isspace() or ch in "\\\x00" for ch in clean):
        raise ValueError("公开参考链接格式无效")
    try:
        url = httpx.URL(urldefrag(clean).url)
    except (ValueError, httpx.InvalidURL):
        raise ValueError("公开参考链接格式无效") from None
    host = (url.host or "").rstrip(".").lower()
    if url.scheme != "https" or not host or url.userinfo or url.port is not None or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".ts.net")) or ("." not in host and ":" not in host):
        raise ValueError("仅可读取公开 HTTPS 网页（不支持 HTTP、凭据、自定义端口或本地地址）")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not public_ip(str(addr)):
        raise ValueError("公开参考链接不能指向内网、回环或链路本地 IP")
    return url


async def fetch_reference_urls(urls: Sequence[str], *, fetch_fn=fetch_excerpt) -> tuple[list[PublicSourceRef], list[dict[str, Any]], list[str]]:
    """Validate reference URLs with `public_https_address` and fetch public excerpts with per-URL fallback."""
    validated: list[tuple[str, httpx.URL, str | None]] = []
    for raw in urls:
        parsed = parse_public_https_url(raw)
        clean, dns_err = str(parsed), None
        try:
            await public_https_address(parsed)
        except ValueError:
            raise
        except OSError as exc:
            if fetch_fn is fetch_excerpt:
                dns_err = _sanitize_diagnostic(f"{type(exc).__name__}: {exc}")[:160] or "DNS 解析失败"
        validated.append((clean, parsed, dns_err))
    sources, pages, diagnostics = [], [], []
    for clean, parsed, dns_err in validated:
        host_or_url = (parsed.host or clean)[:160]
        src_id = f"web:{hashlib.sha256(clean.encode('utf-8')).hexdigest()[:12]}"
        if dns_err is not None:
            sources.append(PublicSourceRef(source_id=src_id, kind="web_research", scope="public", title=host_or_url, url=clean, excerpt="公开链接抓取失败，已保留原地址供参考。", fetch_status="fallback", fetch_error=dns_err))
            pages.append({"url": clean, "title": host_or_url, "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": dns_err})
            diagnostics.append(f"{host_or_url}: {dns_err}")
            continue
        try:
            res = fetch_fn(clean)
            res = await res if inspect.isawaitable(res) else res
            page_url = str((res.get("url") if isinstance(res, dict) else None) or clean).strip()
            page_url = page_url if page_url.startswith("https://") else clean
            title = (str((res.get("title") if isinstance(res, dict) else None) or "").strip()[:160]) or host_or_url
            raw_excerpt = str((res.get("excerpt") if isinstance(res, dict) else None) or "").strip()
            sources.append(PublicSourceRef(source_id=f"web:{hashlib.sha256(page_url.encode('utf-8')).hexdigest()[:12]}", kind="web_research", scope="public", title=title, url=page_url, excerpt=raw_excerpt[:480] or title[:480], fetch_status="fetched"))
            pages.append({"url": page_url, "title": title, "excerpt": raw_excerpt[:720], "truncated": bool(isinstance(res, dict) and res.get("truncated")), "fetch_status": "fetched"})
        except Exception as exc:
            err_msg = _sanitize_diagnostic(f"{type(exc).__name__}: {exc}")[:160] or "抓取失败"
            sources.append(PublicSourceRef(source_id=src_id, kind="web_research", scope="public", title=host_or_url, url=clean, excerpt="公开链接抓取失败，已保留原地址供参考。", fetch_status="fallback", fetch_error=err_msg))
            pages.append({"url": clean, "title": host_or_url, "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": err_msg})
            diagnostics.append(f"{host_or_url}: {err_msg}")
    return sources, pages, diagnostics


MAX_JOB_UPLOADS = 12
MAX_VIDEO_UPLOAD_BYTES = 30 * 1024 * 1024
IMAGE_MIME_EXTS: dict[str, str] = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
VIDEO_MIME_EXTS: dict[str, str] = {"video/mp4": ".mp4", "video/webm": ".webm"}
UPLOAD_MIME_EXTS: dict[str, str] = {**IMAGE_MIME_EXTS, **VIDEO_MIME_EXTS}
UPLOAD_EXT_MIMES: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp4": "video/mp4", ".webm": "video/webm",
}


def _validate_video_clip(raw_bytes: bytes, mime_type: str) -> None:
    if not isinstance(raw_bytes, bytes) or not 0 < len(raw_bytes) <= MAX_VIDEO_UPLOAD_BYTES:
        raise ValueError("视频片段须为非空字节，最大 30MB")
    ok = (
        (mime_type == "video/mp4" and len(raw_bytes) >= 12 and raw_bytes[4:8] == b"ftyp")
        or (mime_type == "video/webm" and raw_bytes.startswith(b"\x1a\x45\xdf\xa3"))
    )
    if not ok:
        raise ValueError("仅接受与 MIME 匹配的 MP4 或 WebM 视频字节")


def sanitize_upload_filename(filename: str, mime_type: str) -> str:
    """Sanitize uploaded image/video filename and enforce MIME-aligned extension."""
    expected_ext = UPLOAD_MIME_EXTS.get(mime_type)
    if expected_ext is None:
        raise ValueError("仅支持 PNG/JPEG/WebP 图片或 MP4/WebM 视频片段")
    raw = str(filename or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("媒体文件名无效或包含非法路径字符")
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(raw).stem).strip("._-") or "media"
    cur_ext = Path(raw).suffix.lower()
    ext = cur_ext if UPLOAD_EXT_MIMES.get(cur_ext) == mime_type else expected_ext
    return f"{stem[:80 - len(ext)]}{ext}"


def save_job_upload_file(uploads_dir: Path, *, filename: str, data_base64: str, mime_type: str) -> tuple[str, int]:
    """Validate base64 image or MP4/WebM clip bytes and save up to 12 files per job."""
    safe_name = sanitize_upload_filename(filename, mime_type)
    payload = str(data_base64 or "").strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw_bytes = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("媒体 Base64 编码无效") from None
    if mime_type in VIDEO_MIME_EXTS:
        _validate_video_clip(raw_bytes, mime_type)
    else:
        validate_image(raw_bytes, mime_type)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    if linked(uploads_dir):
        raise ValueError("上传目录不能使用符号链接")
    existing = [p.name for p in sorted(uploads_dir.iterdir()) if p.is_file() and not linked(p)]
    if safe_name not in existing and len(existing) >= MAX_JOB_UPLOADS:
        raise ValueError(f"每个视频任务最多上传 {MAX_JOB_UPLOADS} 份公开参考媒体")
    target = safe_child_path(uploads_dir, safe_name)
    target.write_bytes(raw_bytes)
    return safe_name, len(raw_bytes)


def save_job_upload_image(uploads_dir: Path, *, filename: str, image_base64: str, mime_type: str) -> tuple[str, int]:
    """Validate base64 image bytes with `validate_image` and save to `public/uploads/`."""
    if mime_type not in IMAGE_MIME_EXTS:
        raise ValueError("仅支持 image/png、image/jpeg 或 image/webp 图片")
    return save_job_upload_file(uploads_dir, filename=filename, data_base64=image_base64, mime_type=mime_type)


def list_job_upload_entries(project_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """List uploaded public reference images/clips inside `<project_dir>/public/uploads`."""
    uploads_dir = project_dir / "public/uploads"
    if not uploads_dir.is_dir() or linked(uploads_dir):
        return []
    entries: list[dict[str, Any]] = []
    for item in sorted(uploads_dir.iterdir(), key=lambda p: p.name):
        ext = item.suffix.lower()
        if linked(item) or not item.is_file() or ".." in item.name or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", item.name) or ext not in UPLOAD_EXT_MIMES:
            continue
        mime = UPLOAD_EXT_MIMES[ext]
        entries.append({
            "filename": item.name, "rel_path": f"uploads/{item.name}",
            "url": f"/api/v1/operations/videos/{job_id}/uploads/{item.name}",
            "size_bytes": item.stat().st_size, "mime_type": mime,
            "kind": "video" if mime in VIDEO_MIME_EXTS else "image",
        })
    return entries[:MAX_JOB_UPLOADS]


def resolve_job_upload_file(project_dir: Path, filename: str) -> tuple[Path, str]:
    """Safely resolve a single uploaded public image or video clip for HTTP serving."""
    raw = str(filename or "").strip()
    if "/" in raw or "\\" in raw or ".." in raw or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", raw):
        raise ValueError("无效的上传媒体文件名")
    uploads_dir = safe_child_path(project_dir, "public/uploads")
    target = safe_child_path(uploads_dir, raw)
    if not target.is_file():
        raise FileNotFoundError("上传媒体不存在")
    media_type = UPLOAD_EXT_MIMES.get(target.suffix.lower())
    if media_type is None:
        raise ValueError("不支持的媒体格式")
    return target, media_type


def collect_scene_public_texts(scene: VideoSceneSpec) -> list[str]:
    """Collect every text fragment inside a scene (including diagram/code/UI/line metadata) for public isolation checks."""
    vp = scene.visual_props
    out: list[str] = [scene.heading, scene.chapter_title, vp.subtitle, vp.code_snippet, vp.citation_label, vp.stat_callout, *vp.bullets]
    for node in vp.diagram_nodes:
        out.extend((node.id, node.label, node.sublabel, node.lane))
    for edge in vp.diagram_edges:
        out.extend((edge.source, edge.target, edge.label))
    for step in vp.code_steps:
        out.extend((step.title, step.code_snippet))
    for reg in vp.ui_focus_regions:
        out.append(reg.label)
    for line in scene.lines:
        out.extend((line.text, *line.emphasis))
        if line.visual_focus:
            out.append(line.visual_focus)
        if line.highlight_code:
            out.append(line.highlight_code)
    return [t for t in out if t]


def assert_public_texts(texts: Sequence[str], *, contains_credential_fn, store=None, unified_memory=None, character_id: str = "", source_ids: Sequence[str] = ()) -> None:
    """Reject credentials, owner-private memories, and dm_inbox texts across arbitrary-length fragments."""
    non_empty = [str(t) for t in texts if t]
    if any(contains_credential_fn(fragment) for fragment in non_empty):
        raise ValueError("公开视频文案禁止包含凭据或敏感密钥")
    corpus = "\n".join(non_empty).casefold()
    if unified_memory is not None:
        owner_items = unified_memory.list_memories(scope="owner", character_id=character_id or None, include_inactive=True, limit=200)
        if any(sid in {str(item["id"]) for item in owner_items} for sid in source_ids):
            raise ValueError("公开视频制作禁止引用 owner 私有记忆")
        for item in owner_items:
            if len(content := " ".join(str(item.get("content") or "").split()).casefold()) >= 6 and content in corpus:
                raise ValueError("公开视频文案检测到未授权的 owner 私有记忆内容")
    if store is not None and store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_inbox'") is not None:
        for row in store.all("SELECT content FROM dm_inbox ORDER BY id DESC LIMIT 100"):
            if len(dm_text := " ".join(str(row.get("content") or "").split()).casefold()) >= 8 and dm_text in corpus:
                raise ValueError("公开视频文案检测到私信原文，已阻止外泄")


def finalize_rich_plan_fields(plan: Any, *, topic: str, brief_text: str, reference_urls: Sequence[str], video_mode: VideoMode, ref_sources: list[PublicSourceRef]) -> Any:
    """Enforce requested video_mode, preserve verified external/brief sources, and keep project_showcase repo URL."""
    extra_sources: list[PublicSourceRef] = list(ref_sources)
    if brief_text.strip():
        extra_sources.append(PublicSourceRef(source_id=f"brief:{hashlib.sha256(brief_text.encode('utf-8')).hexdigest()[:12]}", kind="operator_topic", scope="public", title=topic[:120], excerpt=brief_text.strip()[:480], fetch_status="manual"))
    merged, seen_keys = [], set()
    for src in [*extra_sources, *plan.sources]:
        key = f"url:{src.url}" if src.url else f"id:{src.source_id}"
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(src)
            if len(merged) >= 12:
                break
    summary, scenes = plan.summary, list(plan.scenes)
    if video_mode == "project_showcase":
        urls = [u for u in reference_urls if u] + [s.url for s in merged if s.url]
        if showcase_url := next((u for u in urls if "github.com/" in u.lower()), urls[0] if urls else ""):
            if showcase_url not in summary:
                suffix = f" 项目地址：{showcase_url}"
                summary = (summary[: max(1, 1000 - len(suffix))] + suffix)[:1000]
            if scenes and showcase_url not in scenes[-1].visual_props.citation_label:
                scenes[-1] = scenes[-1].model_copy(update={"visual_props": scenes[-1].visual_props.model_copy(update={"citation_label": showcase_url[:120]})})
    return plan.model_copy(update={"video_mode": video_mode, "summary": summary, "scenes": scenes, "sources": merged})


def resolve_controlled_job_dir(renders_root: Path, job: dict[str, Any], *, create: bool = False) -> Path:
    """Resolve and validate a job directory inside renders_root without symlinks."""
    raw_dir = str(job.get("project_dir") or "").strip()
    if create:
        renders_root.mkdir(parents=True, exist_ok=True)
        if linked(renders_root):
            raise ValueError("渲染目录不能使用符号链接")
    project_dir = Path(raw_dir) if raw_dir else safe_child_path(renders_root, str(job["id"]))
    if not project_dir.resolve().is_relative_to(renders_root.resolve()) or linked(project_dir):
        raise ValueError("视频工程目录不在受控范围内")
    return project_dir
