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

from ...core.models import StrictModel
from ...core.validation import linked
from ...infra.inference.validation import validate_image
from ...infra.web_client.fetch import MAX_URL, fetch_excerpt, public_https_address, public_ip

TemplateId = Literal["TitleCard", "CodeWalkthrough", "ChartCompare", "MemeCutaway", "QuoteSpotlight", "TimelineSummary"]
AvatarCue = Literal["idle", "cheerful", "curious", "proud", "thinking", "surprised", "calm"]
VideoMode = Literal["commentary", "self_intro", "project_showcase", "explainer"]


class ChartPoint(StrictModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    value: float = Field(ge=-1e9, le=1e9)
    unit: str = Field(default="", max_length=16)


class SceneVisualProps(StrictModel):
    subtitle: str = Field(default="", max_length=200)
    bullets: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]] = Field(default_factory=list, max_length=6)
    code_snippet: str = Field(default="", max_length=1500)
    language: Literal["python", "typescript", "json", "bash", "markdown", "text"] = "text"
    chart_items: list[ChartPoint] = Field(default_factory=list, max_length=8)
    meme_asset_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    citation_label: str = Field(default="", max_length=120)
    uploaded_image_file: str | None = Field(default=None, pattern=r"^uploads/[a-zA-Z0-9._-]{1,80}$")

    @field_validator("uploaded_image_file")
    @classmethod
    def check_uploaded_image_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if ".." in value:
            raise ValueError("上传图片相对路径不能包含上级目录跳转")
        return value


class VideoLineSpec(StrictModel):
    line_id: str = Field(default="", max_length=40, pattern=r"^(?:[a-zA-Z0-9_-]{1,40})?$")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    avatar_cue: AvatarCue = "idle"
    highlight_code: str | None = Field(default=None, max_length=200)
    emphasis: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24)]] = Field(default_factory=list, max_length=3)

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
    visual_props: SceneVisualProps = Field(default_factory=SceneVisualProps)
    lines: list[VideoLineSpec] = Field(min_length=1, max_length=12)
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
        if value is None or not value.strip():
            return None
        cleaned = value.strip()
        if not cleaned.startswith("https://") or any(ch.isspace() for ch in cleaned):
            raise ValueError("公开来源链接必须为有效的 HTTPS URL")
        return cleaned

AVATAR_STATES: tuple[str, ...] = ("idle", "talking", "cheerful", "confused", "angry", "gift")
AVATAR_MEME_STATES: dict[str, str] = {
    "hum": "story-hum.gif", "meal": "story-meal.gif", "tail": "story-tail.gif",
    "token": "story-token.gif", "turn": "motion-turn.gif", "dance": "motion-dance.gif",
    "jump": "motion-jump.gif", "eat": "motion-eat.gif", "sleep": "motion-sleep.gif",
}
DEFAULT_MEME_BY_CUE: dict[str, str] = {
    "idle": "archive-flat-whale", "cheerful": "archive-megaphone", "curious": "archive-thinking-whale",
    "thinking": "story-token", "surprised": "archive-laptop-rage", "proud": "story-tail",
    "calm": "story-meal", "angry": "archive-annoyed-poke",
}
DEFAULT_LINE_PAUSE_SECONDS = 0.45
DEFAULT_SCENE_PAUSE_SECONDS = 0.75
DEFAULT_OUTRO_HOLD_SECONDS = 0.60


def _blocked_bundled_memes(meme_catalog) -> tuple[set[str], set[str]]:
    store = getattr(meme_catalog, "store", None)
    if store is None or not hasattr(store, "all"):
        return set(), set()
    try:
        cols = {col["name"] for col in store.all("PRAGMA table_info(meme_assets)")}
        cond = "(enabled=0 OR usage_scope='interaction_only')" if "usage_scope" in cols else "enabled=0"
        rows = store.all(f"SELECT blob_name, sha256 FROM meme_assets WHERE storage_kind='existing_user_assets' AND deleted=0 AND {cond}")
        return (
            {str(r["blob_name"]) for r in rows if r.get("blob_name")},
            {str(r["sha256"]) for r in rows if r.get("sha256")},
        )
    except Exception:
        return set(), set()


def _resolve_catalog_item_path(meme_catalog, item: dict[str, Any], asset_id: str, char_key: str) -> Path | None:
    raw_path = item.get("file_path") or item.get("path")
    if raw_path is None and hasattr(meme_catalog, "resolve"):
        try:
            raw_path = meme_catalog.resolve(asset_id, char_key, allow_disabled=False).get("path")
        except (ValueError, OSError):
            return None
    if raw_path is None and hasattr(meme_catalog, "_row") and hasattr(meme_catalog, "_path"):
        try:
            raw_path = meme_catalog._path(meme_catalog._row(asset_id))
        except (ValueError, OSError):
            return None
    if raw_path is None:
        return None
    resolved = Path(raw_path)
    return resolved if resolved.is_file() else None


def load_whale_meme_catalog(assets_root: Path, *, meme_catalog=None, character_id: str = "whale") -> list[dict[str, Any]]:
    """Load public whale meme metadata from `assets/whale/memes/manifest.json` merged with MemeCatalog."""
    memes_dir = Path(assets_root) / "assets/whale/memes"
    manifest_path = memes_dir / "manifest.json"
    catalog: list[dict[str, Any]] = []
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            payload = None
        entries = payload.get("entries") if isinstance(payload, dict) else None
        for raw in (entries if isinstance(entries, list) else ()):
            if not isinstance(raw, dict):
                continue
            item_id, rel_path = str(raw.get("id") or "").strip(), str(raw.get("path") or "").strip()
            if not item_id or not rel_path or "/" in rel_path or "\\" in rel_path or ".." in rel_path:
                continue
            file_path = memes_dir / rel_path
            if not file_path.is_file():
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
            if isinstance(item.get("tags"), list):
                bundled["tags"] = [str(t) for t in item["tags"][:8]]
            if isinstance(item.get("emotions"), list):
                bundled["emotions"] = [str(e) for e in item["emotions"][:4]]
            if "description" in item and item["description"] is not None:
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
    """Resolve a canonical short ID or MemeCatalog `asset_id` (via `alias_ids`) to canonical ID."""
    clean = str(meme_id or "").strip()
    if not clean:
        return None
    for entry in catalog:
        canonical = str(entry.get("id") or "").strip()
        if not canonical:
            continue
        if clean == canonical or clean in (entry.get("alias_ids") or ()):
            return canonical
    return None


def _extract_gif_frames(src_path: Path, frames_dir: Path, rel_prefix: str) -> tuple[list[str], int]:
    """Extract animated GIF frames to RGBA PNGs so Remotion advances frames deterministically."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_paths: list[str] = []
    durations: list[int] = []
    with Image.open(src_path) as img:
        for idx, frame in enumerate(ImageSequence.Iterator(img)):
            if idx >= 48:
                break
            rgba = frame.convert("RGBA")
            fname = f"{idx:02d}.png"
            rgba.save(frames_dir / fname, format="PNG")
            frame_paths.append(f"{rel_prefix}/{fname}")
            dur = int(frame.info.get("duration") or 120)
            durations.append(max(40, min(800, dur)))
    avg_ms = int(round(sum(durations) / len(durations))) if durations else 120
    return frame_paths, max(40, avg_ms)


def _stage_gif_or_file_entry(src_file: Path, pub_dir: Path, state_or_id: str, rel_folder: str) -> tuple[str, list[str], int]:
    """Copy a raster or animated GIF asset into public/ and extract deterministic frames for GIFs."""
    pub_dir.mkdir(parents=True, exist_ok=True)
    dst_file = pub_dir / src_file.name
    shutil.copy2(src_file, dst_file)
    frames: list[str] = []
    avg_ms = 110
    if src_file.suffix.lower() == ".gif":
        frames, avg_ms = _extract_gif_frames(
            src_file,
            pub_dir / "frames" / state_or_id,
            f"{rel_folder}/frames/{state_or_id}",
        )
    return f"{rel_folder}/{src_file.name}", frames, avg_ms


def pick_distinct_scene_meme_id(
    cue: str, used_ids: set[str], available_ids: set[str], preferred_id: str | None = None
) -> str | None:
    """Pick a distinct whale sticker ID per scene so adjacent cards never repeat the same art."""
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
    if preferred_id and preferred_id in available_ids:
        return preferred_id
    return next(iter(sorted(available_ids)), None)


def stage_whale_visual_assets(
    assets_root: Path, public_dir: Path, *, include_avatar: bool, requested_meme_ids: list[str],
    scene_cues: list[str], meme_catalog=None, character_id: str = "whale",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy real Aqua avatar GIFs and whale meme stickers into `public/` and extract GIF frames."""
    whale_dir = Path(assets_root) / "assets/whale"
    memes_src_dir = whale_dir / "memes"
    avatar_assets: dict[str, Any] = {}
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
    for raw_id in requested_meme_ids:
        if (canonical := alias_to_canonical.get(str(raw_id or "").strip())) and canonical in by_id and canonical not in wanted_ids:
            wanted_ids.append(canonical)
    for cue in scene_cues:
        if (fid := DEFAULT_MEME_BY_CUE.get(str(cue or "").strip(), "archive-thinking-whale")) in by_id and fid not in wanted_ids:
            wanted_ids.append(fid)
    for extra in ("archive-megaphone", "archive-laptop-rage", "story-meal", "story-tail", "story-token", "archive-flat-whale"):
        if extra in by_id and extra not in wanted_ids:
            wanted_ids.append(extra)

    meme_assets: dict[str, Any] = {}
    if wanted_ids:
        memes_pub = public_dir / "memes"
        for meme_id in wanted_ids[:14]:
            entry = by_id[meme_id]
            src_file = Path(entry.get("file_path") or entry.get("source_path") or (memes_src_dir / entry["path"]))
            if not src_file.is_file():
                continue
            rel_file, frames, avg_ms = _stage_gif_or_file_entry(src_file, memes_pub, meme_id, "memes")
            meme_assets[meme_id] = {"id": meme_id, "title": entry["title"], "file": rel_file, "frames": frames, "frameDurationMs": avg_ms}
    return avatar_assets, meme_assets


def _write_mono_wav(path: Path, samples: list[int], *, sample_rate: int = 24000) -> None:
    buf = struct.pack(f"<{len(samples)}h", *(max(-32767, min(32767, s)) for s in samples))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(buf)


def synthesize_bgm_and_sfx(
    audio_dir: Path,
    *,
    duration_seconds: float,
    sample_rate: int = 24000,
) -> tuple[str, str]:
    """Synthesize a gentle shallow-sea marimba/pad BGM loop and a crisp transition ding WAV."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    total_sec = max(4.0, float(duration_seconds) + 0.5)
    total_samples = int(round(total_sec * sample_rate))
    # Soft C-major / Fmaj7 pentatonic progression cycling every 4 seconds.
    chords = (
        (261.63, 329.63, 392.00, 493.88),  # Cmaj7
        (293.66, 349.23, 440.00, 523.25),  # Dm7
        (329.63, 392.00, 493.88, 587.33),  # Em7
        (349.23, 440.00, 523.25, 659.25),  # Fmaj7
    )
    arp_notes = (523.25, 587.33, 659.25, 783.99, 659.25, 587.33, 523.25, 440.00)
    bgm_samples: list[int] = [0] * total_samples
    beat_sec = 0.5
    beat_samples = int(beat_sec * sample_rate)
    fade_in = int(0.8 * sample_rate)
    fade_out = int(1.2 * sample_rate)

    for i in range(total_samples):
        t = i / sample_rate
        chord = chords[int(t // 4.0) % len(chords)]
        pad = sum(math.sin(2.0 * math.pi * f * t) for f in chord) * 0.22
        beat_idx = i // beat_samples
        beat_pos = (i % beat_samples) / sample_rate
        arp_f = arp_notes[beat_idx % len(arp_notes)]
        pluck_env = math.exp(-beat_pos * 5.2) * (1.0 - math.exp(-beat_pos * 90.0))
        pluck = (
            math.sin(2.0 * math.pi * arp_f * beat_pos)
            + 0.25 * math.sin(4.0 * math.pi * arp_f * beat_pos)
        ) * pluck_env * 0.45
        env = 1.0
        if i < fade_in:
            env *= i / max(1, fade_in)
        if i > total_samples - fade_out:
            env *= max(0.0, (total_samples - i) / max(1, fade_out))
        bgm_samples[i] = int((pad + pluck) * 1500.0 * env)

    bgm_path = audio_dir / "bgm.wav"
    _write_mono_wav(bgm_path, bgm_samples, sample_rate=sample_rate)

    sfx_samples_len = int(0.22 * sample_rate)
    sfx_samples: list[int] = [0] * sfx_samples_len
    for i in range(sfx_samples_len):
        t = i / sample_rate
        f = 783.99 if t < 0.08 else 1046.50
        local_t = t if t < 0.08 else (t - 0.08)
        env = math.exp(-local_t * 22.0) * (1.0 - math.exp(-local_t * 180.0))
        sfx_samples[i] = int(math.sin(2.0 * math.pi * f * t) * 4200.0 * env)

    sfx_path = audio_dir / "transition.wav"
    _write_mono_wav(sfx_path, sfx_samples, sample_rate=sample_rate)
    return "audio/bgm.wav", "audio/transition.wav"


_MODE_PLANNER_STRUCTURES: dict[str, str] = {
    "commentary": (
        "当前视频类型为「热点吐槽 / 杂谈（commentary）」，去掉「总-分-总」科普播音腔与「哈喽大家好今天带大家梳理」套话，改用 B 站轻快短吐槽结构：\n"
        "1) 第 1 镜（TitleCard）：3 秒钩子开场，一针见血抛出痛点或反差吐槽；\n"
        "2) 中间 1~N 镜（CodeWalkthrough / ChartCompare / QuoteSpotlight / MemeCutaway）：抓最戳人的具体细节、代码对比或反转槽点展开；\n"
        "3) 末镜（MemeCutaway 或 TimelineSummary）：用 1 句轻傲娇、干白饭或「低功耗保鲜摸鱼」式俏皮收尾。"
    ),
    "self_intro": (
        "当前视频类型为「角色与工坊自我介绍（self_intro）」，结合角色卡设定与传入的 `brief_text` 资料自然表达：\n"
        "1) 第 1 镜（TitleCard）：鲜明身份与反差感钩子开场（80% 鲸鱼娘吐槽感 + 20% 本地开发者底色）；\n"
        "2) 中间 1~N 镜（QuoteSpotlight / MemeCutaway / CodeWalkthrough / TimelineSummary）：依次展开核心能力、工坊日常工作流与陪伴生态；\n"
        "3) 末镜（MemeCutaway 或 TimelineSummary）：友好社区互动号召收尾（欢迎三连/评论区搭话）。"
    ),
    "project_showcase": (
        "当前视频类型为「开源项目 / 作品展示（project_showcase）」，结合 `brief_text` 与抓取到的 GitHub README / 项目主页内容：\n"
        "1) 第 1 镜（TitleCard）：直击真实痛点或最亮眼项目卖点开场；\n"
        "2) 中间 1~N 镜（CodeWalkthrough / ChartCompare / QuoteSpotlight / TimelineSummary）：拆解核心特性、架构流程、代码或界面亮点；\n"
        "3) 末镜（TimelineSummary 或 MemeCutaway）：说明适用人群与开源地址，并把公开仓库/项目 URL 原样保留在 summary 与末镜 visual_props.citation_label。"
    ),
    "explainer": (
        "当前视频类型为「原理拆解 / 知识科普（explainer）」，保持轻快口语化但不失条理：\n"
        "1) 第 1 镜（TitleCard）：用反常识疑问或具体卡点抛出问题钩子；\n"
        "2) 中间 1~N 镜（CodeWalkthrough / ChartCompare / QuoteSpotlight / TimelineSummary）：分 2~3 个清晰步骤或对比把原理讲透；\n"
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
        extra_lines.append(
            f"- 用户已置顶图库表情 {clean_pins}，请优先将它们分配到不同分镜的 visual_props.meme_asset_id（相邻分镜禁止重复同一表情）；\n"
        )
    if clean_uploads:
        extra_lines.append(
            f"- 用户上传了本次专属公开配图/截图 {clean_uploads}，请依次绑定到 QuoteSpotlight / MemeCutaway / CodeWalkthrough 分镜的 visual_props.uploaded_image_file；\n"
        )
    extra_block = "".join(extra_lines)
    return (
        "只基于传入的公开来源、brief_text 与 reference_pages 生成结构化 VideoScriptPlan JSON，并将 video_mode 原样设为传入模式。"
        "视觉风格是「浅海手账 / 清爽二次元杂谈桌 + Aqua 右下角虚拟主播小窗」，"
        "人设遵循 80% 鲸鱼娘 OC（灵动吐槽、轻傲娇、偶尔白饭/摸鱼梗，自称「我」「Aqua」「本鲸」但不自称「大肥鱼」）、20% 本地开发者底色。\n"
        f"{mode_block}\n"
        "配音节奏要求（为 TTS 合成优化，程序会自动在句间插入 0.45s、切镜插入 0.75s 气口停顿）：\n"
        "- 每条口播台词 lines[].text 控制在 10~22 个字左右的短促口语，一句只讲一个爆点，少用生僻英文缩写堆砌；\n"
        "- 关键台词可在 emphasis 填入 1~2 个必须直接出自该句 text 原文的亮眼关键词（如「先跑起来」「一锅粥」「低功耗保鲜」），用于 B 站综艺花字高亮；\n"
        "- 请为每一镜的 visual_props.meme_asset_id 从 available_memes 中挑选【互不重复】的鲸鱼表情包/动图（如 archive-megaphone、archive-laptop-rage、story-meal、story-hum、story-tail、story-token、motion-turn、archive-flat-whale 等），并给每句分配丰富的 avatar_cue（curious/thinking/surprised/cheerful/proud/calm）；\n"
        f"{extra_block}"
        "- 可用模板严格限于 TitleCard、CodeWalkthrough、ChartCompare、MemeCutaway、QuoteSpotlight、TimelineSummary；严禁写可执行代码或引用私人记忆与私信。"
    )


def _extract_brief_points(brief_text: str, sources: list[Any], topic: str) -> list[str]:
    points: list[str] = []
    for raw_line in re.split(r"[\r\n。！？；;]+", brief_text or ""):
        clean = re.sub(r"^[#>*\-\d.\s]+", "", raw_line).strip()
        if len(clean) >= 4 and clean not in points:
            points.append(clean[:110])
    for src in sources:
        for cand in (getattr(src, "title", ""), getattr(src, "excerpt", "")):
            clean = " ".join(str(cand or "").split()).strip()
            if len(clean) >= 4 and clean[:110] not in points:
                points.append(clean[:110])
    if not points:
        points.append(topic[:80])
    return points


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
        "project_showcase": (f"{lead[:14]}到底解决了什么痛点？", "先看痛点与亮点，再看开源结构！", ["痛点", "开源"]),
        "explainer": (f"为什么{lead[:12]}总让人一头雾水？", "别背定义啦，本鲸拆成几步讲透。", ["一头雾水", "讲透"]),
        "commentary": (f"等等，{lead[:14]}这事也太真实了。", "先别急着点头，本鲸直接给你划重点。", ["太真实了", "划重点"]),
    }[video_mode]
    outro_copy = {
        "self_intro": ("想一起折腾创作和代码，记得在评论区叫我！", "以后工坊日常和直播都靠大家多多关照啦。", ["评论区", "关照"]),
        "project_showcase": ("仓库地址放在卡片上，欢迎亲自上手试试看！", "觉得顺手就点个 Star，有想法评论区直接开聊。", ["仓库地址", "评论区"]),
        "explainer": ("记住这一条主线，原理其实一点都不玄学。", "还有想拆解的技术点，评论区随时点单！", ["一点都不玄学", "评论区"]),
        "commentary": ("行了，本鲸要切回低功耗模式摸鱼了。", "有更好玩的槽点，记得在评论区叫我。", ["低功耗模式", "评论区"]),
    }[video_mode]

    hook_scene = {
        "scene_id": "scene_hook", "template": "TitleCard", "heading": lead, "avatar_cue": "cheerful",
        "visual_props": {
            "subtitle": style_note or points[0][:140], "bullets": [p[:48] for p in points[:3]],
            "meme_asset_id": _take_meme("cheerful", "archive-megaphone"),
            "citation_label": (sources[0].title[:60] if sources and getattr(sources[0], "title", "") else "公开观察"),
        },
        "lines": [
            {"line_id": "l1", "text": hook_copy[0], "avatar_cue": "surprised", "emphasis": [hook_copy[2][0]]},
            {"line_id": "l2", "text": hook_copy[1], "avatar_cue": "cheerful", "emphasis": [hook_copy[2][1]]},
        ],
    }
    target_count = max(1, min(10, int(max_scenes)))
    if target_count == 1:
        return lead, [hook_scene]

    middle_templates_by_mode: dict[str, tuple[str, ...]] = {
        "commentary": ("QuoteSpotlight", "MemeCutaway", "ChartCompare", "CodeWalkthrough", "TimelineSummary"),
        "self_intro": ("QuoteSpotlight", "TimelineSummary", "MemeCutaway", "CodeWalkthrough", "ChartCompare"),
        "project_showcase": ("CodeWalkthrough", "QuoteSpotlight", "TimelineSummary", "ChartCompare", "MemeCutaway"),
        "explainer": ("TimelineSummary", "QuoteSpotlight", "CodeWalkthrough", "ChartCompare", "MemeCutaway"),
    }
    middle_templates = middle_templates_by_mode[video_mode]
    middle_scenes: list[dict[str, Any]] = []
    line_idx = 3
    for idx in range(max(0, target_count - 2)):
        tpl, point, next_point = middle_templates[idx % len(middle_templates)], points[idx % len(points)], points[(idx + 1) % len(points)]
        src = sources[idx % len(sources)] if sources else None
        citation = (getattr(src, "url", None) or getattr(src, "title", None) or "公开来源") if src else "公开来源"
        cue = ("curious", "thinking", "proud", "surprised")[idx % 4]
        vp: dict[str, Any] = {"subtitle": point[:180], "bullets": [point[:48], *([next_point[:48]] if next_point != point else [])], "meme_asset_id": _take_meme(cue), "citation_label": str(citation)[:120]}
        if tpl == "CodeWalkthrough":
            vp |= {"language": "text", "code_snippet": "\n".join(points[:4])[:1200]}
        elif tpl == "ChartCompare":
            vp["chart_items"] = [{"label": "关键点 A", "value": 80 + idx * 5, "unit": "%"}, {"label": "对比项 B", "value": 45 + idx * 4, "unit": "%"}]
        middle_scenes.append({
            "scene_id": f"scene_detail_{idx + 1}", "template": tpl, "heading": f"第 {idx + 1} 块重点：{point[:20]}", "visual_props": vp, "avatar_cue": cue,
            "lines": [
                {"line_id": f"l{line_idx}", "text": f"先看第{idx + 1}点，重点其实非常直接。", "avatar_cue": cue, "emphasis": ["非常直接"]},
                {"line_id": f"l{line_idx + 1}", "text": f"把{point[:10]}拎出来看就全明白。", "avatar_cue": "thinking", "emphasis": ["全明白"]},
            ],
        })
        line_idx += 2

    outro_citation = showcase_url[:120] if video_mode == "project_showcase" and showcase_url else str((sources[0].url or sources[0].title) if sources else "Aqua 浅海手账")[:120]
    outro_scene = {
        "scene_id": "scene_outro", "template": "TimelineSummary" if video_mode in {"project_showcase", "explainer"} else "MemeCutaway",
        "heading": "开源地址与适用场景" if video_mode == "project_showcase" else ("一句话带走核心结论" if video_mode == "explainer" else "讲完收工，去干一碗热白饭"),
        "visual_props": {"subtitle": showcase_url or excerpt or "听完别光点赞，评论区交出你的看法", "bullets": [p[:48] for p in points[:2]] or ["本鲸低功耗保鲜中", "欢迎评论区接梗"], "meme_asset_id": _take_meme("proud", "story-meal"), "citation_label": outro_citation},
        "lines": [
            {"line_id": f"l{line_idx}", "text": outro_copy[0], "avatar_cue": "proud", "emphasis": [outro_copy[2][0]]},
            {"line_id": f"l{line_idx + 1}", "text": outro_copy[1], "avatar_cue": "cheerful", "emphasis": [outro_copy[2][1]]},
        ],
        "avatar_cue": "proud",
    }
    if upload_queue := [str(u).strip() for u in uploaded_image_files if str(u).strip()]:
        image_templates, candidates = {"QuoteSpotlight", "MemeCutaway", "CodeWalkthrough"}, [*middle_scenes, outro_scene]
        picked = [s for s in candidates if s["template"] in image_templates]
        for s in candidates:
            if len(picked) >= len(upload_queue):
                break
            if s not in picked:
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
    "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1", "remotion": "^4.0.0"},
}
_REMOTION_ROOT_TSX = (
    'import React from "react";\nimport { Composition } from "remotion";\nimport { AquaVideo, AquaVideoProps } from "./AquaVideo";\n\n'
    'export const RemotionRoot: React.FC = () => (\n  <Composition id="AquaVideo" component={AquaVideo} durationInFrames={300} fps={30} width={1920} height={1080}\n'
    '    defaultProps={{ title: "Aqua Video", fps: 30, totalFrames: 300, scenes: [], cues: [] }}\n'
    "    calculateMetadata={({ props }: { props: AquaVideoProps }) => ({ durationInFrames: Math.max(1, props.totalFrames || 300), fps: props.fps || 30 })} />\n);\n"
)
_REMOTION_AQUA_VIDEO_TSX = (
    'import React from "react";\nimport { AbsoluteFill, Audio, Sequence, staticFile, useCurrentFrame } from "remotion";\n\n'
    "export interface AquaSceneProp { scene_id: string; template: string; heading: string; start_frame: number; duration_frames: number; visual_props: Record<string, unknown>; }\n"
    "export interface AquaCueProp { line_id: string; text: string; start_frame: number; duration_frames: number; avatar_cue: string; }\n"
    "export interface AquaVideoProps { title: string; fps: number; totalFrames: number; audioFile?: string; scenes: AquaSceneProp[]; cues: AquaCueProp[]; }\n\n"
    "export const AquaVideo: React.FC<AquaVideoProps> = ({ title, audioFile, scenes, cues }) => {\n"
    "  const frame = useCurrentFrame();\n"
    "  const activeCue = cues.find((c) => frame >= c.start_frame && frame < c.start_frame + c.duration_frames);\n"
    '  return (\n    <AbsoluteFill style={{ backgroundColor: "#0f172a", color: "#f8fafc", fontFamily: "sans-serif", padding: 64 }}>\n'
    "      {audioFile ? <Audio src={staticFile(audioFile)} /> : null}\n      <div style={{ fontSize: 28, opacity: 0.72, marginBottom: 16 }}>{title}</div>\n"
    '      {scenes.map((scene) => (\n        <Sequence key={scene.scene_id} from={scene.start_frame} durationInFrames={scene.duration_frames}>\n'
    '          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>\n            <div style={{ fontSize: 52, fontWeight: 700 }}>{scene.heading}</div>\n'
    '            <div style={{ fontSize: 24, opacity: 0.85 }}>{String(scene.visual_props?.subtitle || "")}</div>\n          </div>\n        </Sequence>\n      ))}\n'
    '      {activeCue ? (\n        <div style={{ position: "absolute", bottom: 56, left: 80, right: 80, textAlign: "center", fontSize: 34, backgroundColor: "rgba(15,23,42,0.78)", padding: "16px 24px", borderRadius: 12 }}>{activeCue.text}</div>\n'
    "      ) : null}\n    </AbsoluteFill>\n  );\n};\n"
)
_REMOTION_INDEX_TS = 'import { registerRoot } from "remotion";\nimport { RemotionRoot } from "./Root";\nregisterRoot(RemotionRoot);\n'


def _format_srt_timestamp(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


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
        if not pw_root.is_dir():
            continue
        for pat in ("chromium-*/chrome-win*/chrome.exe", "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
                    "chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell"):
            if (matches := sorted(pw_root.glob(pat), reverse=True)) and matches[0].is_file():
                return str(matches[0]), "playwright-chromium"
    return None, None


def _build_dry_run_wav_bytes(text: str, *, sample_rate: int = 16000) -> bytes:
    duration = max(0.6, min(12.0, len(text.strip()) * 0.16))
    nframes = max(1600, int(round(duration * sample_rate)))
    samples = b"".join(struct.pack("<h", int(1200 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))) for i in range(nframes))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples)
    return buf.getvalue()


def _read_wav_pcm(wav_bytes: bytes) -> tuple[tuple[int, int, int], bytes, float]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        p = r.getparams()
        pcm = r.readframes(p.nframes)
        if p.nframes <= 0 or len(pcm) != p.nframes * p.nchannels * p.sampwidth:
            raise ValueError("合成音频帧长度无效或被截断")
        return (p.nchannels, p.sampwidth, p.framerate), pcm, p.nframes / float(p.framerate)


def _concat_wav_pcm(spec: tuple[int, int, int], chunks: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(spec[0])
        w.setsampwidth(spec[1])
        w.setframerate(spec[2])
        for pcm in chunks:
            w.writeframes(pcm)
    return buf.getvalue()


def _silence_pcm_for_frames(spec: tuple[int, int, int], frames: int, fps: int) -> bytes:
    """Return zeroed PCM frames matching `spec` for exact `frames / fps` video duration."""
    if frames <= 0 or fps <= 0:
        return b""
    nchannels, sampwidth, framerate = spec
    audio_samples = max(0, int(round((frames / float(fps)) * framerate)))
    return b"\x00" * (audio_samples * nchannels * sampwidth)


def _pad_pcm_to_frames(spec: tuple[int, int, int], pcm: bytes, target_frames: int, fps: int) -> bytes:
    """Pad tail of spoken line PCM so total chunk duration equals `target_frames / fps`."""
    if target_frames <= 0 or fps <= 0:
        return pcm
    nchannels, sampwidth, framerate = spec
    frame_bytes = nchannels * sampwidth
    target_bytes = int(round((target_frames / float(fps)) * framerate)) * frame_bytes
    if len(pcm) < target_bytes:
        return pcm + b"\x00" * (target_bytes - len(pcm))
    return pcm


def _resolve_studio_dir(runtime, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    root = getattr(getattr(runtime, "settings", None), "root", None)
    return Path(root) / "remotion-studio" if root else Path(__file__).resolve().parents[4] / "remotion-studio"


def _sanitize_diagnostic(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", "[URL]", str(text or ""))).strip()[:240]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    """Strip empties, dedupe preserving order, and enforce max 4 reference URLs."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("reference_urls 须为字符串列表")
    cleaned = list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))
    if len(cleaned) > 4:
        raise ValueError("公开参考链接最多填写 4 条")
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
    if (
        url.scheme != "https"
        or not host
        or url.userinfo
        or url.port is not None
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal", ".ts.net"))
        or ("." not in host and ":" not in host)
    ):
        raise ValueError("仅可读取公开 HTTPS 网页（不支持 HTTP、凭据、自定义端口或本地地址）")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not public_ip(str(addr)):
        raise ValueError("公开参考链接不能指向内网、回环或链路本地 IP")
    return url


async def fetch_reference_urls(
    urls: Sequence[str], *, fetch_fn=fetch_excerpt
) -> tuple[list[PublicSourceRef], list[dict[str, Any]], list[str]]:
    """Validate reference URLs with `public_https_address` and fetch public excerpts with per-URL fallback."""
    validated: list[tuple[str, httpx.URL, str | None]] = []
    for raw in urls:
        parsed = parse_public_https_url(raw)
        clean = str(parsed)
        dns_err: str | None = None
        try:
            await public_https_address(parsed)
        except ValueError:
            raise
        except OSError as exc:
            if fetch_fn is fetch_excerpt:
                dns_err = _sanitize_diagnostic(f"{type(exc).__name__}: {exc}")[:160] or "DNS 解析失败"
        validated.append((clean, parsed, dns_err))

    sources: list[PublicSourceRef] = []
    pages: list[dict[str, Any]] = []
    diagnostics: list[str] = []
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
            if inspect.isawaitable(res):
                res = await res
            page_url = str((res.get("url") if isinstance(res, dict) else None) or clean).strip()
            if not page_url.startswith("https://"):
                page_url = clean
            title = (str((res.get("title") if isinstance(res, dict) else None) or "").strip()[:160]) or host_or_url
            raw_excerpt = str((res.get("excerpt") if isinstance(res, dict) else None) or "").strip()
            excerpt = raw_excerpt[:480] or title[:480]
            sources.append(PublicSourceRef(source_id=f"web:{hashlib.sha256(page_url.encode('utf-8')).hexdigest()[:12]}", kind="web_research", scope="public", title=title, url=page_url, excerpt=excerpt, fetch_status="fetched"))
            pages.append({"url": page_url, "title": title, "excerpt": raw_excerpt[:720], "truncated": bool(isinstance(res, dict) and res.get("truncated")), "fetch_status": "fetched"})
        except Exception as exc:
            err_msg = _sanitize_diagnostic(f"{type(exc).__name__}: {exc}")[:160] or "抓取失败"
            sources.append(PublicSourceRef(source_id=src_id, kind="web_research", scope="public", title=host_or_url, url=clean, excerpt="公开链接抓取失败，已保留原地址供参考。", fetch_status="fallback", fetch_error=err_msg))
            pages.append({"url": clean, "title": host_or_url, "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": err_msg})
            diagnostics.append(f"{host_or_url}: {err_msg}")
    return sources, pages, diagnostics


MAX_JOB_UPLOADS = 3
UPLOAD_MIME_EXTS: dict[str, str] = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
UPLOAD_EXT_MIMES: dict[str, str] = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def sanitize_upload_filename(filename: str, mime_type: str) -> str:
    """Sanitize uploaded image filename and enforce MIME-aligned extension."""
    expected_ext = UPLOAD_MIME_EXTS.get(mime_type)
    if expected_ext is None:
        raise ValueError("仅支持 image/png、image/jpeg 或 image/webp 图片")
    raw = str(filename or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("图片文件名无效或包含非法路径字符")
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(raw).stem).strip("._-") or "image"
    cur_ext = Path(raw).suffix.lower()
    ext = cur_ext if UPLOAD_EXT_MIMES.get(cur_ext) == mime_type else expected_ext
    return f"{stem[:80 - len(ext)]}{ext}"


def save_job_upload_image(uploads_dir: Path, *, filename: str, image_base64: str, mime_type: str) -> tuple[str, int]:
    """Validate base64 image bytes with `validate_image` and save up to 3 images per job."""
    safe_name = sanitize_upload_filename(filename, mime_type)
    payload = str(image_base64 or "").strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw_bytes = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("图片 Base64 编码无效") from None
    validate_image(raw_bytes, mime_type)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    if linked(uploads_dir):
        raise ValueError("上传目录不能使用符号链接")
    existing = [p.name for p in sorted(uploads_dir.iterdir()) if p.is_file() and not linked(p)]
    if safe_name not in existing and len(existing) >= MAX_JOB_UPLOADS:
        raise ValueError(f"每个视频任务最多上传 {MAX_JOB_UPLOADS} 张公开参考图片")
    target = safe_child_path(uploads_dir, safe_name)
    target.write_bytes(raw_bytes)
    return safe_name, len(raw_bytes)


def list_job_upload_entries(project_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """List uploaded public reference images inside `<project_dir>/public/uploads`."""
    uploads_dir = project_dir / "public/uploads"
    if not uploads_dir.is_dir() or linked(uploads_dir):
        return []
    entries: list[dict[str, Any]] = []
    for item in sorted(uploads_dir.iterdir(), key=lambda p: p.name):
        if linked(item) or not item.is_file() or ".." in item.name or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", item.name) or item.suffix.lower() not in UPLOAD_EXT_MIMES:
            continue
        entries.append({"filename": item.name, "rel_path": f"uploads/{item.name}", "url": f"/api/v1/operations/videos/{job_id}/uploads/{item.name}", "size_bytes": item.stat().st_size})
    return entries[:MAX_JOB_UPLOADS]


def resolve_job_upload_file(project_dir: Path, filename: str) -> tuple[Path, str]:
    """Safely resolve a single uploaded public image file for HTTP serving."""
    raw = str(filename or "").strip()
    if "/" in raw or "\\" in raw or ".." in raw or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", raw):
        raise ValueError("无效的上传图片文件名")
    uploads_dir = safe_child_path(project_dir, "public/uploads")
    target = safe_child_path(uploads_dir, raw)
    if not target.is_file():
        raise FileNotFoundError("上传图片不存在")
    media_type = UPLOAD_EXT_MIMES.get(target.suffix.lower())
    if media_type is None:
        raise ValueError("不支持的图片格式")
    return target, media_type


def assert_public_texts(
    texts: Sequence[str],
    *,
    contains_credential_fn,
    store=None,
    unified_memory=None,
    character_id: str = "",
    source_ids: Sequence[str] = (),
) -> None:
    """Reject credentials, owner-private memories, and dm_inbox texts across arbitrary-length fragments."""
    non_empty = [str(t) for t in texts if t]
    if any(contains_credential_fn(fragment) for fragment in non_empty):
        raise ValueError("公开视频文案禁止包含凭据或敏感密钥")
    corpus = "\n".join(non_empty).casefold()
    if unified_memory is not None:
        owner_items = unified_memory.list_memories(scope="owner", character_id=character_id or None, include_inactive=True, limit=200)
        owner_ids = {str(item["id"]) for item in owner_items}
        if any(sid in owner_ids for sid in source_ids):
            raise ValueError("公开视频制作禁止引用 owner 私有记忆")
        for item in owner_items:
            content = " ".join(str(item.get("content") or "").split()).casefold()
            if len(content) >= 6 and content in corpus:
                raise ValueError("公开视频文案检测到未授权的 owner 私有记忆内容")
    if store is not None and store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_inbox'") is not None:
        for row in store.all("SELECT content FROM dm_inbox ORDER BY id DESC LIMIT 100"):
            dm_text = " ".join(str(row.get("content") or "").split()).casefold()
            if len(dm_text) >= 8 and dm_text in corpus:
                raise ValueError("公开视频文案检测到私信原文，已阻止外泄")


def finalize_rich_plan_fields(
    plan: Any,
    *,
    topic: str,
    brief_text: str,
    reference_urls: Sequence[str],
    video_mode: VideoMode,
    ref_sources: list[PublicSourceRef],
) -> Any:
    """Enforce requested video_mode, preserve verified external/brief sources, and keep project_showcase repo URL."""
    extra_sources: list[PublicSourceRef] = list(ref_sources)
    if brief_text.strip():
        digest = hashlib.sha256(brief_text.encode("utf-8")).hexdigest()[:12]
        extra_sources.append(PublicSourceRef(source_id=f"brief:{digest}", kind="operator_topic", scope="public", title=topic[:120], excerpt=brief_text.strip()[:480], fetch_status="manual"))
    merged: list[PublicSourceRef] = []
    seen_keys: set[str] = set()
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
        showcase_url = next((u for u in urls if "github.com/" in u.lower()), urls[0] if urls else "")
        if showcase_url:
            if showcase_url not in summary:
                suffix = f" 项目地址：{showcase_url}"
                summary = (summary[: max(1, 1000 - len(suffix))] + suffix)[:1000]
            if scenes and showcase_url not in scenes[-1].visual_props.citation_label:
                vp = scenes[-1].visual_props.model_copy(update={"citation_label": showcase_url[:120]})
                scenes[-1] = scenes[-1].model_copy(update={"visual_props": vp})
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
