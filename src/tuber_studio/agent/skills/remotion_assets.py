"""Visual asset staging, procedural shallow-sea BGM/SFX, and 80/20 Aqua script prompt helpers."""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import struct
import wave
from pathlib import Path
from typing import Annotated, Any, Literal

from PIL import Image, ImageSequence
from pydantic import Field, StringConstraints, field_validator

from ...core.models import StrictModel

TemplateId = Literal["TitleCard", "CodeWalkthrough", "ChartCompare", "MemeCutaway", "QuoteSpotlight", "TimelineSummary"]
AvatarCue = Literal["idle", "cheerful", "curious", "proud", "thinking", "surprised", "calm"]


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
    "hum": "story-hum.gif",
    "meal": "story-meal.gif",
    "tail": "story-tail.gif",
    "token": "story-token.gif",
    "turn": "motion-turn.gif",
    "dance": "motion-dance.gif",
    "jump": "motion-jump.gif",
    "eat": "motion-eat.gif",
    "sleep": "motion-sleep.gif",
}
DEFAULT_MEME_BY_CUE: dict[str, str] = {
    "idle": "archive-flat-whale",
    "cheerful": "archive-megaphone",
    "curious": "archive-thinking-whale",
    "thinking": "story-token",
    "surprised": "archive-laptop-rage",
    "proud": "story-tail",
    "calm": "story-meal",
    "angry": "archive-annoyed-poke",
}
DEFAULT_LINE_PAUSE_SECONDS = 0.45
DEFAULT_SCENE_PAUSE_SECONDS = 0.75
DEFAULT_OUTRO_HOLD_SECONDS = 0.60


def load_whale_meme_catalog(assets_root: Path) -> list[dict[str, Any]]:
    """Load public whale meme metadata from `assets/whale/memes/manifest.json`."""
    memes_dir = Path(assets_root) / "assets/whale/memes"
    manifest_path = memes_dir / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    catalog: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        rel_path = str(raw.get("path") or "").strip()
        if not item_id or not rel_path or "/" in rel_path or "\\" in rel_path or ".." in rel_path:
            continue
        file_path = memes_dir / rel_path
        if not file_path.is_file():
            continue
        catalog.append(
            {
                "id": item_id,
                "title": str(raw.get("title") or item_id),
                "tags": [str(t) for t in (raw.get("tags") or [])[:5]],
                "emotions": [str(e) for e in (raw.get("emotions") or [])[:4]],
                "path": rel_path,
                "description": str(raw.get("description") or "")[:120],
            }
        )
    return catalog


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
    cue: str,
    used_ids: set[str],
    available_ids: set[str],
    preferred_id: str | None = None,
) -> str | None:
    """Pick a distinct whale sticker ID per scene so adjacent cards never repeat the same art."""
    if preferred_id and preferred_id in available_ids and preferred_id not in used_ids:
        return preferred_id
    ordered = [
        DEFAULT_MEME_BY_CUE.get(str(cue or "").strip(), "archive-megaphone"),
        "archive-megaphone",
        "story-token",
        "archive-laptop-rage",
        "story-meal",
        "archive-flat-whale",
        "story-tail",
        "motion-turn",
        "story-hum",
        "archive-craving-cake",
        "archive-thinking-whale",
        *sorted(available_ids),
    ]
    for candidate in ordered:
        if candidate in available_ids and candidate not in used_ids:
            return candidate
    if preferred_id and preferred_id in available_ids:
        return preferred_id
    return next(iter(sorted(available_ids)), None)


def stage_whale_visual_assets(
    assets_root: Path,
    public_dir: Path,
    *,
    include_avatar: bool,
    requested_meme_ids: list[str],
    scene_cues: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy real Aqua avatar GIFs and whale meme stickers into `public/` and extract GIF frames."""
    whale_dir = Path(assets_root) / "assets/whale"
    memes_src_dir = whale_dir / "memes"
    avatar_assets: dict[str, Any] = {}
    if include_avatar and whale_dir.is_dir():
        avatar_pub = public_dir / "avatar"
        for state in AVATAR_STATES:
            src_gif = whale_dir / f"{state}.gif"
            if not src_gif.is_file():
                continue
            rel_file, frames, avg_ms = _stage_gif_or_file_entry(src_gif, avatar_pub, state, "avatar")
            avatar_assets[state] = {"file": rel_file, "frames": frames, "frameDurationMs": avg_ms}
        for state, fname in AVATAR_MEME_STATES.items():
            src_gif = memes_src_dir / fname
            if not src_gif.is_file():
                continue
            rel_file, frames, avg_ms = _stage_gif_or_file_entry(src_gif, avatar_pub, state, "avatar")
            avatar_assets[state] = {"file": rel_file, "frames": frames, "frameDurationMs": avg_ms}

    catalog = load_whale_meme_catalog(assets_root)
    by_id = {entry["id"]: entry for entry in catalog}
    wanted_ids: list[str] = []
    for raw_id in requested_meme_ids:
        clean = str(raw_id or "").strip()
        if clean and clean in by_id and clean not in wanted_ids:
            wanted_ids.append(clean)
    for cue in scene_cues:
        fallback_id = DEFAULT_MEME_BY_CUE.get(str(cue or "").strip(), "archive-thinking-whale")
        if fallback_id in by_id and fallback_id not in wanted_ids:
            wanted_ids.append(fallback_id)
    for extra in ("archive-megaphone", "archive-laptop-rage", "story-meal", "story-tail", "story-token", "archive-flat-whale"):
        if extra in by_id and extra not in wanted_ids:
            wanted_ids.append(extra)

    meme_assets: dict[str, Any] = {}
    if wanted_ids:
        memes_pub = public_dir / "memes"
        for meme_id in wanted_ids[:14]:
            entry = by_id[meme_id]
            src_file = memes_src_dir / entry["path"]
            if not src_file.is_file():
                continue
            rel_file, frames, avg_ms = _stage_gif_or_file_entry(src_file, memes_pub, meme_id, "memes")
            meme_assets[meme_id] = {
                "id": meme_id,
                "title": entry["title"],
                "file": rel_file,
                "frames": frames,
                "frameDurationMs": avg_ms,
            }
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


def build_aqua_video_planner_intent() -> str:
    """Return the 80/20 Aqua Bilibili short-commentary video script planner instruction."""
    return (
        "只基于传入的公开来源生成结构化 VideoScriptPlan JSON。"
        "视觉风格是「浅海手账 / 清爽二次元杂谈桌 + Aqua 右下角虚拟主播小窗」，"
        "人设遵循 80% 鲸鱼娘 OC（灵动吐槽、轻傲娇、偶尔白饭/摸鱼梗，自称「我」「Aqua」「本鲸」但不自称「大肥鱼」）、20% 本地开发者底色。\n"
        "严格去掉「总-分-总」科普播音腔与「哈喽大家好今天带大家梳理」套话，改用 B 站轻快短吐槽结构：\n"
        "1) 第 1 镜（TitleCard）：3 秒钩子开场，一针见血抛出痛点或反差吐槽；\n"
        "2) 中间 1~2 镜（CodeWalkthrough / ChartCompare / QuoteSpotlight / MemeCutaway）：抓 2 个最戳人的具体细节、代码对比或反转槽点展开；\n"
        "3) 末镜（MemeCutaway 或 TimelineSummary）：用 1 句轻傲娇、干白饭或「低功耗保鲜摸鱼」式俏皮收尾。\n"
        "配音节奏要求（为 TTS 合成优化，程序会自动在句间插入 0.45s、切镜插入 0.75s 气口停顿）：\n"
        "- 每条口播台词 lines[].text 控制在 10~22 个字左右的短促口语，一句只讲一个爆点，少用生僻英文缩写堆砌；\n"
        "- 关键台词可在 emphasis 填入 1~2 个必须直接出自该句 text 原文的亮眼关键词（如「先跑起来」「一锅粥」「低功耗保鲜」），用于 B 站综艺花字高亮；\n"
        "- 请为每一镜的 visual_props.meme_asset_id 从 available_memes 中挑选【互不重复】的鲸鱼表情包/动图（如 archive-megaphone、archive-laptop-rage、story-meal、story-hum、story-tail、story-token、motion-turn、archive-flat-whale 等），并给每句分配丰富的 avatar_cue（curious/thinking/surprised/cheerful/proud/calm）；\n"
        "- 可用模板严格限于 TitleCard、CodeWalkthrough、ChartCompare、MemeCutaway、QuoteSpotlight、TimelineSummary；严禁写可执行代码或引用私人记忆与私信。"
    )


def build_fallback_aqua_scenes(
    *,
    topic: str,
    style_note: str,
    sources: list[Any],
    max_scenes: int,
    available_memes: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Construct lively 80/20 Aqua fallback scenes when LLM planner is unavailable."""
    lead = topic[:48]
    meme_ids = {m["id"] for m in available_memes}
    hook_meme = "archive-megaphone" if "archive-megaphone" in meme_ids else ("archive-thinking-whale" if "archive-thinking-whale" in meme_ids else None)
    outro_meme = "story-meal" if "story-meal" in meme_ids else ("archive-flat-whale" if "archive-flat-whale" in meme_ids else None)
    excerpt = (sources[0].excerpt[:60] if sources else "") or topic[:60]
    rows = [
        {
            "scene_id": "scene_hook",
            "template": "TitleCard",
            "heading": lead,
            "visual_props": {
                "subtitle": style_note or "本鲸今天不绕弯，直接开聊正题",
                "bullets": ["3 秒抓重点", "拒绝长篇说教"],
                "meme_asset_id": hook_meme,
                "citation_label": (sources[0].title[:40] if sources else "公开观察"),
            },
            "lines": [
                {"line_id": "l1", "text": f"等等，{lead[:14]}这事也太真实了。", "avatar_cue": "surprised", "emphasis": ["太真实了"]},
                {"line_id": "l2", "text": "先别急着点头，本鲸直接给你划重点。", "avatar_cue": "cheerful", "emphasis": ["划重点"]},
            ],
            "avatar_cue": "cheerful",
        },
        {
            "scene_id": "scene_detail",
            "template": "QuoteSpotlight",
            "heading": "最戳人的细节在这儿",
            "visual_props": {
                "subtitle": excerpt,
                "bullets": [s.title[:48] for s in sources[:3] if getattr(s, "title", "")],
                "citation_label": (sources[0].url or sources[0].source_id) if sources else "公开来源",
            },
            "lines": [
                {"line_id": "l3", "text": "表面看着风平浪静，细节一扒全露馅。", "avatar_cue": "curious", "emphasis": ["全露馅"]},
                {"line_id": "l4", "text": "把关键线索拎出来看，是不是瞬间懂了？", "avatar_cue": "thinking", "emphasis": ["瞬间懂了"]},
            ],
            "avatar_cue": "curious",
        },
        {
            "scene_id": "scene_outro",
            "template": "MemeCutaway",
            "heading": "讲完收工，去干一碗热白饭",
            "visual_props": {
                "subtitle": "听完别光点赞，评论区交出你的看法",
                "bullets": ["本鲸低功耗保鲜中", "欢迎评论区接梗"],
                "meme_asset_id": outro_meme,
            },
            "lines": [
                {"line_id": "l5", "text": "行了，本鲸要切回低功耗模式摸鱼了。", "avatar_cue": "proud", "emphasis": ["低功耗模式"]},
                {"line_id": "l6", "text": "有更好玩的槽点，记得在评论区叫我。", "avatar_cue": "cheerful", "emphasis": ["评论区"]},
            ],
            "avatar_cue": "proud",
        },
    ]
    return lead, rows[:max_scenes]


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
