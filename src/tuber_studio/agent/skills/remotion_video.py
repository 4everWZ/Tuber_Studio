"""Remotion Code-as-Video production skill, strict schemas, TTS alignment, QC, and Bilibili staging."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import wave
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, ValidationError, field_validator

from ...core.identity import without_model_identity
from ...core.models import StrictModel, content_hash, tool_function
from ...core.store import dump
from ...core.validation import linked
from ...operations.facade import (
    canonical_intent,
    control_policy,
    find_write_attempt_by_intent,
    finish_write_attempt,
    heartbeat_pulse_snapshot,
    reserve_write_attempt,
    select_video_candidates,
)
from ...persona.memory.daily.models import contains_memory_credential
from ...persona.self_model import build_self_model_system_message

VIDEO_PRODUCTION_POLICY_KEY = "video_production_policy"
DEFAULT_VIDEO_PRODUCTION_POLICY = {
    "publish_mode": "preview_review",
    "auto_publish_enabled": False,
    "default_fps": 30,
    "default_width": 1920,
    "default_height": 1080,
    "max_render_concurrency": 2,
    "render_timeout_seconds": 300.0,
    "upload_timeout_seconds": 600.0,
}
MAX_RENDERED_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION_DRIFT_SECONDS = 0.5
_ALLOWED_PUBLIC_KINDS = frozenset({"watched_video", "public_incident", "operator_topic", "web_research", "public_memory", "public_comment"})
_FORBIDDEN_SOURCE_PREFIXES = ("owner:", "private:", "dm:", "dm_", "chat:", "turn:", "daily-chat:")
_JOB_COLUMNS = frozenset(
    {"status", "publish_mode", "title", "content_hash", "project_hash", "intent", "write_token", "plan_json", "manifest_json", "qc_json", "receipt_json", "project_dir", "output_path", "error"}
)

TemplateId = Literal["TitleCard", "CodeWalkthrough", "ChartCompare", "MemeCutaway", "QuoteSpotlight", "TimelineSummary"]
AvatarCue = Literal["idle", "cheerful", "curious", "proud", "thinking", "surprised", "calm"]

# Fixed static TSX/package templates — NEVER interpolated with LLM code strings.
_REMOTION_PACKAGE_JSON = {"name": "aqua-remotion-video", "version": "1.0.0", "private": True, "type": "module", "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1", "remotion": "^4.0.0"}}
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
    '      {activeCue ? (\n        <div style={{ position: "absolute", bottom: 56, left: 80, right: 80, textAlign: "center", fontSize: 34, backgroundColor: "rgba(15,23,42,0.78)", padding: "16px 24px", borderRadius: 12 }}>{activeCue.text}</div>\n      ) : null}\n'
    "    </AbsoluteFill>\n  );\n};\n"
)
_REMOTION_INDEX_TS = 'import { registerRoot } from "remotion";\nimport { RemotionRoot } from "./Root";\nregisterRoot(RemotionRoot);\n'


class ChartPoint(StrictModel):
    """Single data point rendered inside ChartCompare Remotion scenes."""

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    value: float = Field(ge=-1e9, le=1e9)
    unit: str = Field(default="", max_length=16)


class SceneVisualProps(StrictModel):
    """Schema-validated visual properties passed strictly via JSON props."""

    subtitle: str = Field(default="", max_length=200)
    bullets: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]] = Field(default_factory=list, max_length=6)
    code_snippet: str = Field(default="", max_length=1500)
    language: Literal["python", "typescript", "json", "bash", "markdown", "text"] = "text"
    chart_items: list[ChartPoint] = Field(default_factory=list, max_length=8)
    meme_asset_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    citation_label: str = Field(default="", max_length=120)


class VideoLineSpec(StrictModel):
    """Spoken subtitle line synchronized to WAV audio and optional avatar cue."""

    line_id: str = Field(default="", max_length=40, pattern=r"^(?:[a-zA-Z0-9_-]{1,40})?$")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    avatar_cue: AvatarCue = "idle"
    highlight_code: str | None = Field(default=None, max_length=200)

    @field_validator("text")
    @classmethod
    def clean_line_text(cls, value: str) -> str:
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("台词包含非法控制字符")
        return value


class VideoSceneSpec(StrictModel):
    """Single Remotion template scene and its spoken narration lines."""

    scene_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    template: TemplateId
    heading: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    visual_props: SceneVisualProps = Field(default_factory=SceneVisualProps)
    lines: list[VideoLineSpec] = Field(min_length=1, max_length=12)
    avatar_cue: AvatarCue = "idle"


class PublicSourceRef(StrictModel):
    """Verified public grounding reference for generated video scripts."""

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


class VideoScriptPlan(StrictModel):
    """Complete structured storyboard generated from public sources."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]] = Field(min_length=1, max_length=10)
    category_tid: int = Field(default=174, ge=1, le=9999)
    fps: Literal[30, 60] = 30
    width: Literal[1920, 1280] = 1920
    height: Literal[1080, 720] = 1080
    include_avatar: bool = False
    scenes: list[VideoSceneSpec] = Field(min_length=1, max_length=16)
    sources: list[PublicSourceRef] = Field(min_length=1, max_length=12)


class VideoProductionPolicy(StrictModel):
    """Runtime policy governing preview staging vs gated auto_publish and render limits."""

    publish_mode: Literal["preview_review", "auto_publish"] = "preview_review"
    auto_publish_enabled: bool = False
    default_fps: Literal[30, 60] = 30
    default_width: Literal[1920, 1280] = 1920
    default_height: Literal[1080, 720] = 1080
    max_render_concurrency: int = Field(default=2, ge=1, le=4)
    render_timeout_seconds: float = Field(default=300.0, ge=5.0, le=3600.0)
    upload_timeout_seconds: float = Field(default=600.0, ge=5.0, le=3600.0)


class PlanVideoScriptArgs(StrictModel):
    """Tool schema arguments for `plan_video_script`."""

    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)] = Field(description="公开视频选题、热点话题或 BV 号说明。")
    candidate_bvids: list[Annotated[str, StringConstraints(pattern=r"^BV[0-9A-Za-z]{10}$")]] = Field(default_factory=list, max_length=6, description="可选关联的公开视频 BV 号。")
    style_note: str = Field(default="", max_length=200, description="可选节奏或风格提示。")
    fps: Literal[30, 60] = Field(default=30, description="视频帧率（30 或 60）。")
    include_avatar: bool = Field(default=False, description="是否附带虚拟形象口型/动作时间轴。")
    max_scenes: int = Field(default=4, ge=1, le=10, description="目标分镜数量。")


class GenerateRemotionProjectArgs(StrictModel):
    """Tool schema arguments for `generate_remotion_project`."""

    job_id: str | None = Field(default=None, max_length=80, description="已规划的视频制作任务 ID。")
    plan: dict[str, Any] | None = Field(default=None, description="可选显式传入的 VideoScriptPlan 对象。")
    tts_mode: Literal["omni_speech", "dry_run"] = Field(default="omni_speech", description="配音模式：omni_speech 或 dry_run。")
    reference_voice_id: str | None = Field(default=None, max_length=80, description="可选指定参考音色 ID。")


class RenderRemotionVideoArgs(StrictModel):
    """Tool schema arguments for `render_remotion_video`."""

    job_id: str = Field(min_length=1, max_length=80, description="已生成 Remotion 工程的任务 ID。")
    composition_id: str = Field(default="AquaVideo", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,40}$", description="Remotion 合成组件 ID。")
    concurrency: int = Field(default=2, ge=1, le=4, description="Remotion 渲染并发线程数。")
    dry_run: bool = Field(default=False, description="跳过真实 npx/ffmpeg 子进程，输出干跑校验结果。")


class StageOrPublishVideoArgs(StrictModel):
    """Tool schema arguments for `stage_or_publish_bilibili_video`."""

    job_id: str = Field(min_length=1, max_length=80, description="已通过成片质检的视频任务 ID。")
    publish_mode: Literal["preview_review", "auto_publish"] | None = Field(default=None, description="发布模式：preview_review（默认暂存审核）或 auto_publish。")
    approved: bool = Field(default=False, description="是否已经过人工明确批准发布。")


def assert_public_video_sources(
    plan: VideoScriptPlan | dict[str, Any], *, store=None, unified_memory=None, character_id: str = ""
) -> VideoScriptPlan:
    """Strictly validate that video plan & sources contain ZERO owner-private memories or DMs."""
    validated = plan if isinstance(plan, VideoScriptPlan) else VideoScriptPlan.model_validate(plan)
    for src in validated.sources:
        if src.scope != "public" or src.kind not in _ALLOWED_PUBLIC_KINDS:
            raise ValueError("公开视频只能使用 public 作用域来源")
        if src.source_id.casefold().startswith(_FORBIDDEN_SOURCE_PREFIXES):
            raise ValueError("公开视频制作禁止引用私有对话或私信来源")

    texts = [validated.title, validated.summary, *validated.tags]
    for src in validated.sources:
        texts.extend([src.source_id, src.title, src.excerpt])
    for scene in validated.scenes:
        vp = scene.visual_props
        texts.extend([scene.heading, vp.subtitle, vp.code_snippet, vp.citation_label, *vp.bullets])
        texts.extend(line.text for line in scene.lines)
    if any(contains_memory_credential(fragment) for fragment in texts):
        raise ValueError("公开视频文案禁止包含凭据或敏感密钥")

    corpus = "\n".join(texts).casefold()
    if unified_memory is not None:
        owner_items = unified_memory.list_memories(scope="owner", character_id=character_id or None, include_inactive=True, limit=200)
        owner_ids = {str(item["id"]) for item in owner_items}
        if any(src.source_id in owner_ids for src in validated.sources):
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
    return validated


def _format_srt_timestamp(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


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
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        p = reader.getparams()
        pcm = reader.readframes(p.nframes)
        if p.nframes <= 0 or len(pcm) != p.nframes * p.nchannels * p.sampwidth:
            raise ValueError("合成音频帧长度无效或被截断")
        return (p.nchannels, p.sampwidth, p.framerate), pcm, p.nframes / float(p.framerate)


def _concat_wav_pcm(spec: tuple[int, int, int], chunks: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(spec[0])
        writer.setsampwidth(spec[1])
        writer.setframerate(spec[2])
        for pcm in chunks:
            writer.writeframes(pcm)
    return buf.getvalue()


def _parse_biliup_receipt(stdout: str, stderr: str) -> dict[str, Any]:
    combined = f"{stdout}\n{stderr}"
    bv = re.search(r"\b(BV[0-9A-Za-z]{10})\b", combined)
    av = re.search(r"\b(?:aid|av)[=: ]+([1-9][0-9]{0,19})\b", combined, re.IGNORECASE)
    return {k: v for k, v in (("bvid", bv.group(1) if bv else None), ("aid", av.group(1) if av else None)) if v}


def _scrub_secrets(text: str, credentials: dict[str, Any]) -> str:
    cleaned = str(text or "")
    for key in ("sessdata", "csrf", "bili_jct", "refresh_token"):
        if len(val := str(credentials.get(key) or "").strip()) >= 4:
            cleaned = cleaned.replace(val, "[REDACTED]")
    return cleaned[:400]


def _safe_child_path(root: Path, child: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / child).resolve()
    if not target.is_relative_to(resolved_root) or linked(target):
        raise ValueError("路径超出受控工作目录或包含符号链接")
    return target


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class RemotionVideoSkill:
    """Orchestrates the 6-stage Remotion Code-as-Video production & Bilibili publishing pipeline."""

    _render_semaphore: asyncio.Semaphore | None = None

    def __init__(
        self, runtime, store, *, browsing=None, renders_root: Path | None = None,
        planner_fn=None, tts_fn=None, subprocess_runner=None, ffprobe_runner=None,
        biliup_runner=None, render_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.runtime, self.store, self._browsing = runtime, store, browsing
        base_rt = renders_root or (getattr(getattr(runtime, "settings", None), "runtime", None) or Path.cwd() / "runtime")
        self.renders_root = Path(base_rt) / "video_renders"
        self.planner_fn, self.tts_fn = planner_fn, tts_fn
        self.subprocess_runner, self.ffprobe_runner, self.biliup_runner = subprocess_runner, ffprobe_runner, biliup_runner
        self._own_semaphore = render_semaphore
        self._install_storage()

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._own_semaphore is not None:
            return self._own_semaphore
        if RemotionVideoSkill._render_semaphore is None:
            RemotionVideoSkill._render_semaphore = asyncio.Semaphore(1)
        return RemotionVideoSkill._render_semaphore

    def _install_storage(self) -> None:
        with self.store.db:
            self.store.db.executescript(
                "CREATE TABLE IF NOT EXISTS remotion_video_jobs("
                "id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', character_id TEXT NOT NULL DEFAULT '',"
                "character_version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, publish_mode TEXT NOT NULL DEFAULT 'preview_review',"
                "title TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', project_hash TEXT NOT NULL DEFAULT '',"
                "intent TEXT NOT NULL DEFAULT '', write_token TEXT, plan_json TEXT NOT NULL, manifest_json TEXT,"
                "qc_json TEXT, receipt_json TEXT, project_dir TEXT NOT NULL DEFAULT '', output_path TEXT NOT NULL DEFAULT '',"
                "error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_remotion_jobs_char ON remotion_video_jobs(character_id, updated_at DESC);"
            )

    def _browsing_service(self):
        return self._browsing if self._browsing is not None else getattr(self.runtime, "browsing", None)

    def _current_character(self) -> dict[str, Any]:
        return getattr(self.runtime, "character", None) or self.store.character()

    def _account_id(self) -> str:
        acct = getattr(self.runtime, "account", None)
        if acct is not None and hasattr(acct, "display_account_id") and (uid := str(acct.display_account_id() or "")):
            return uid
        browsing = self._browsing_service()
        return str(browsing.account) if browsing is not None and getattr(browsing, "account", None) else ""

    def policy(self) -> VideoProductionPolicy:
        saved = self.store.get(VIDEO_PRODUCTION_POLICY_KEY) or {}
        return VideoProductionPolicy.model_validate(DEFAULT_VIDEO_PRODUCTION_POLICY | (saved if isinstance(saved, dict) else {}))

    def configure_policy(self, patch: dict[str, Any]) -> VideoProductionPolicy:
        if not isinstance(patch, dict) or set(patch) - set(VideoProductionPolicy.model_fields):
            raise ValueError("未知的视频制作策略字段")
        updated = VideoProductionPolicy.model_validate(self.policy().model_dump() | patch)
        self.store.set(VIDEO_PRODUCTION_POLICY_KEY, updated.model_dump())
        return updated

    def _update_job(self, job_id: str, **fields: Any) -> None:
        if bad := (set(fields) - _JOB_COLUMNS):
            raise ValueError(f"非法任务字段: {sorted(bad)}")
        cols = [f"{k}=?" for k in fields] + ["updated_at=?"]
        with self.store.db:
            self.store.db.execute(f"UPDATE remotion_video_jobs SET {', '.join(cols)} WHERE id=?", [*fields.values(), time.time(), job_id])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT * FROM remotion_video_jobs WHERE id=?", (str(job_id),))
        if row is None:
            return None
        decoded = dict(row)
        for col, target in (("plan_json", "plan"), ("manifest_json", "manifest"), ("qc_json", "qc_report"), ("receipt_json", "receipt")):
            raw_val = decoded.pop(col, None)
            try:
                decoded[target] = json.loads(raw_val) if raw_val else None
            except ValueError:
                decoded[target] = None
        return decoded

    def list_jobs(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE status=?", (status,)) if status else ("", ())
        rows = self.store.all(f"SELECT id FROM remotion_video_jobs {where} ORDER BY updated_at DESC LIMIT ?", (*params, max(1, min(int(limit), 100))))
        return [job for row in rows if (job := self.get_job(row["id"])) is not None]

    def _public_candidates_and_memories(
        self, *, topic: str, candidate_bvids: list[str], character_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        candidates = select_video_candidates(self._browsing_service(), store=self.store, limit=12, query=None)
        if candidate_bvids:
            by_bv = {item["bvid"]: item for item in candidates}
            candidates = [
                by_bv.get(bv) or {"bvid": bv, "title": f"视频 {bv}", "url": f"https://www.bilibili.com/video/{bv}", "summary": topic, "source_kind": "watched_video", "scope": "public"}
                for bv in candidate_bvids
            ]
        unified = getattr(self.runtime, "unified_memory", None)
        return candidates, (unified.for_prompt("public", character_id=character_id, limit=10) if unified is not None else [])

    def _fallback_script_plan(
        self, args: PlanVideoScriptArgs, candidates: list[dict[str, Any]], public_memories: list[dict[str, Any]]
    ) -> VideoScriptPlan:
        sources = [
            PublicSourceRef(
                source_id=f"video:{item['bvid']}", kind="watched_video", scope="public",
                title=str(item.get("title") or item["bvid"])[:160],
                url=item.get("url") or f"https://www.bilibili.com/video/{item['bvid']}",
                bvid=item["bvid"], excerpt=str(item.get("summary") or "")[:300],
            )
            for item in candidates[:3]
        ] or [PublicSourceRef(source_id=f"topic:{content_hash(args.topic)[:12]}", kind="operator_topic", scope="public", title=args.topic[:80], excerpt=args.topic[:300])]
        sources.extend(
            PublicSourceRef(source_id=str(m["id"]), kind="public_memory", scope="public", title="公开印象参考", excerpt=str(m.get("content") or "")[:200])
            for m in public_memories[:2]
        )
        lead = args.topic[:68]
        scene_rows = [
            ("scene_intro", "TitleCard", lead, SceneVisualProps(subtitle=args.style_note or "Aqua 公开观察速览", citation_label=sources[0].title[:60]), f"哈喽，我是 Aqua！今天带大家快速过一遍「{lead[:36]}」的核心看点。", "cheerful"),
            ("scene_main", "QuoteSpotlight", "核心看点梳理", SceneVisualProps(subtitle=sources[0].title[:80], bullets=[s.title[:60] for s in sources[:3] if s.title], citation_label=sources[0].url or sources[0].source_id), f"结合公开信息来看，最关键的一点是：{sources[0].excerpt[:80] or args.topic[:80]}。", "curious"),
            ("scene_outro", "TimelineSummary", "总结与下期预告", SceneVisualProps(subtitle="资料均来自已核验公开来源", bullets=["来源透明可追溯", "欢迎评论区聊聊你的想法"]), "以上就是本鲸整理的重点啦，有想看的话题欢迎留在评论区。", "proud"),
        ]
        scenes = [
            VideoSceneSpec(scene_id=sid, template=tpl, heading=head, visual_props=vp, lines=[VideoLineSpec(line_id=f"l{i}", text=txt, avatar_cue=cue)], avatar_cue=cue)
            for i, (sid, tpl, head, vp, txt, cue) in enumerate(scene_rows[: args.max_scenes], start=1)
        ]
        return VideoScriptPlan(
            title=lead,
            summary=f"围绕「{args.topic[:120]}」整理的公开要点解析。引用来源：" + "；".join(s.url or s.title for s in sources[:3]),
            tags=["Aqua", "科普速览", "公开观察"],
            fps=args.fps,
            include_avatar=args.include_avatar,
            scenes=scenes,
            sources=sources,
        )

    async def plan_video_script(self, arguments: PlanVideoScriptArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 1 & 2: Select strictly public sources and plan a schema-validated VideoScriptPlan."""
        args = arguments if isinstance(arguments, PlanVideoScriptArgs) else PlanVideoScriptArgs.model_validate(arguments)
        char = self._current_character()
        char_id = str(char.get("id") or "")
        candidates, public_memories = self._public_candidates_and_memories(topic=args.topic, candidate_bvids=args.candidate_bvids, character_id=char_id)
        sys_msg = build_self_model_system_message(
            char, channel="dynamic", scene="公开视频脚本与分镜策划",
            task_intent="只基于传入的公开来源生成结构化 VideoScriptPlan JSON。严禁写可执行代码或引用私人记忆与私信。",
        )
        packet = {
            "topic": args.topic, "style_note": args.style_note, "fps": args.fps,
            "include_avatar": args.include_avatar, "max_scenes": args.max_scenes,
            "public_candidates": candidates[:6],
            "public_memories": [{"id": m["id"], "content": m["content"]} for m in public_memories[:6]],
        }
        messages = [sys_msg, {"role": "user", "content": dump(packet)}]
        plan: VideoScriptPlan | None = None
        if callable(self.planner_fn):
            raw_plan = await self.planner_fn(messages, VideoScriptPlan.model_json_schema(), packet)
            plan = raw_plan if isinstance(raw_plan, VideoScriptPlan) else VideoScriptPlan.model_validate(raw_plan)
        else:
            settings, op_llm = getattr(self.runtime, "settings", None), getattr(self.runtime, "operation_llm", None)
            has_key = bool(settings and callable(getattr(settings, "provider_key", None)) and settings.provider_key("omni"))
            if has_key and op_llm is not None and callable(getattr(op_llm, "generate_json", None)):
                try:
                    slot = self.runtime.offline_generation("background") if callable(getattr(self.runtime, "offline_generation", None)) else asyncio.Lock()
                    async with asyncio.timeout(45), slot:
                        with without_model_identity():
                            raw_plan = await op_llm.generate_json(messages, VideoScriptPlan.model_json_schema(), max_tokens=2048)
                    plan = VideoScriptPlan.model_validate(raw_plan)
                except Exception:
                    plan = None
        if plan is None:
            plan = self._fallback_script_plan(args, candidates, public_memories)

        validated = assert_public_video_sources(plan, store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None), character_id=char_id)
        job_id, now = f"rem_{uuid4().hex[:16]}", time.time()
        plan_hash = content_hash(validated.model_dump(mode="json"))
        policy = self.policy()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO remotion_video_jobs(id,account_id,character_id,character_version,status,publish_mode,title,content_hash,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, self._account_id(), char_id, int(char.get("version") or 1), "script_planned", policy.publish_mode, validated.title, plan_hash, validated.model_dump_json(), now, now),
            )
        return {"ok": True, "job_id": job_id, "status": "script_planned", "content_hash": plan_hash, "plan": validated.model_dump(mode="json")}

    async def _synthesize_line_wav(self, text: str, *, tts_mode: Literal["omni_speech", "dry_run"], voice_id: str | None) -> bytes:
        if callable(self.tts_fn):
            result = self.tts_fn(text, voice_id)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, Path):
                return result.read_bytes()
            if isinstance(result, (bytes, bytearray)):
                return bytes(result)
            raise ValueError("自定义 TTS 回调必须返回 WAV 字节或路径")
        if tts_mode == "dry_run":
            return _build_dry_run_wav_bytes(text)
        voices, settings = getattr(self.runtime, "voices", None), getattr(self.runtime, "settings", None)
        if voices is None or settings is None:
            raise ValueError("TTS 语音服务未配置，请传入 tts_mode='dry_run' 或配置 Omni 语音")
        voice = settings.voice(voice_id or self._current_character().get("voice_profile") or "whale_reference")
        audio = await voices.synthesize(text, voice)
        return audio.path.read_bytes()

    async def generate_remotion_project(self, arguments: GenerateRemotionProjectArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 3 & 4 Prep: Synthesize aligned TTS WAV/SRT/manifest and scaffold safe Remotion TSX project."""
        args = arguments if isinstance(arguments, GenerateRemotionProjectArgs) else GenerateRemotionProjectArgs.model_validate(arguments)
        char = self._current_character()
        char_id = str(char.get("id") or "")
        job = self.get_job(args.job_id) if args.job_id else None
        raw_plan = args.plan if args.plan is not None else (job.get("plan") if job else None)
        if raw_plan is None:
            raise ValueError("需要提供有效的 job_id 或 plan")
        plan = assert_public_video_sources(raw_plan, store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None), character_id=char_id)

        job_id = job["id"] if job is not None else f"rem_{uuid4().hex[:16]}"
        self.renders_root.mkdir(parents=True, exist_ok=True)
        if linked(self.renders_root):
            raise ValueError("渲染目录不能使用符号链接")
        project_dir = _safe_child_path(self.renders_root, job_id)
        src_dir, public_dir = project_dir / "src", project_dir / "public"
        audio_dir = public_dir / "audio"
        src_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)

        wav_spec: tuple[int, int, int] | None = None
        pcm_chunks, cues, scene_props, avatar_cues, srt_lines = [], [], [], [], []
        cursor_seconds, cursor_frame, srt_index = 0.0, 0, 1
        for s_idx, scene in enumerate(plan.scenes, start=1):
            scene_start_frame, scene_frames = cursor_frame, 0
            for l_idx, line in enumerate(scene.lines, start=1):
                line_id = line.line_id or f"s{s_idx}_l{l_idx}"
                wav_bytes = await self._synthesize_line_wav(line.text, tts_mode=args.tts_mode, voice_id=args.reference_voice_id)
                spec, pcm, duration = _read_wav_pcm(wav_bytes)
                if wav_spec is None:
                    wav_spec = spec
                elif spec != wav_spec:
                    raise ValueError("逐句配音音频采样率或声道数不一致")
                line_frames = max(1, math.ceil(duration * plan.fps))
                (audio_dir / f"{line_id}.wav").write_bytes(wav_bytes)
                pcm_chunks.append(pcm)
                start_sec, end_sec = cursor_seconds, cursor_seconds + duration
                cues.append(
                    {
                        "line_id": line_id, "scene_id": scene.scene_id, "text": line.text,
                        "start_frame": cursor_frame, "duration_frames": line_frames,
                        "start_seconds": round(start_sec, 4), "end_seconds": round(end_sec, 4),
                        "audio_file": f"audio/{line_id}.wav", "avatar_cue": line.avatar_cue, "highlight_code": line.highlight_code,
                    }
                )
                avatar_cues.append({"line_id": line_id, "cue": line.avatar_cue, "start_frame": cursor_frame, "duration_frames": line_frames, "viseme_energy": 0.65 if args.tts_mode != "dry_run" else 0.35})
                srt_lines.append(f"{srt_index}\n{_format_srt_timestamp(start_sec)} --> {_format_srt_timestamp(end_sec)}\n{line.text}\n")
                srt_index += 1
                cursor_seconds, cursor_frame, scene_frames = end_sec, cursor_frame + line_frames, scene_frames + line_frames
            scene_props.append({"scene_id": scene.scene_id, "template": scene.template, "heading": scene.heading, "start_frame": scene_start_frame, "duration_frames": max(1, scene_frames), "visual_props": scene.visual_props.model_dump(mode="json"), "avatar_cue": scene.avatar_cue})

        assert wav_spec is not None
        narration_path = public_dir / "narration.wav"
        narration_path.write_bytes(_concat_wav_pcm(wav_spec, pcm_chunks))
        srt_path = project_dir / "subtitles.srt"
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
        avatar_cues_path = project_dir / "avatar_cues.json"
        avatar_cues_path.write_text(dump({"include_avatar": plan.include_avatar, "avatar_mode": "viseme_envelope_ready" if plan.include_avatar else "2d_fallback", "cues": avatar_cues}), encoding="utf-8")
        props_payload = {"title": plan.title, "fps": plan.fps, "totalFrames": max(1, cursor_frame), "audioFile": "narration.wav", "scenes": scene_props, "cues": cues}
        props_path, manifest_path = project_dir / "props.json", project_dir / "manifest.json"
        _write_json(props_path, props_payload)
        manifest = {
            "job_id": job_id, "fps": plan.fps, "width": plan.width, "height": plan.height,
            "total_frames": max(1, cursor_frame), "total_duration_seconds": round(cursor_seconds, 4),
            "audio_file": str(narration_path), "srt_file": str(srt_path), "avatar_cues_file": str(avatar_cues_path),
            "props_file": str(props_path), "cues": cues, "sources": [src.model_dump(mode="json") for src in plan.sources],
        }
        _write_json(manifest_path, manifest)

        # Write static schema-safe Remotion template files (never interpolated with LLM code).
        _write_json(project_dir / "package.json", _REMOTION_PACKAGE_JSON)
        (src_dir / "index.ts").write_text(_REMOTION_INDEX_TS, encoding="utf-8")
        (src_dir / "Root.tsx").write_text(_REMOTION_ROOT_TSX, encoding="utf-8")
        (src_dir / "AquaVideo.tsx").write_text(_REMOTION_AQUA_VIDEO_TSX, encoding="utf-8")

        project_hash = content_hash({"plan": plan.model_dump(mode="json"), "props": props_payload})
        if job is None:
            now = time.time()
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO remotion_video_jobs(id,account_id,character_id,character_version,status,publish_mode,title,content_hash,project_hash,plan_json,manifest_json,project_dir,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, self._account_id(), char_id, int(char.get("version") or 1), "project_ready", self.policy().publish_mode, plan.title, content_hash(plan.model_dump(mode="json")), project_hash, plan.model_dump_json(), dump(manifest), str(project_dir), now, now),
                )
        else:
            self._update_job(job_id, status="project_ready", project_hash=project_hash, manifest_json=dump(manifest), project_dir=str(project_dir), error=None)

        return {
            "ok": True, "job_id": job_id, "status": "project_ready", "project_dir": str(project_dir),
            "props_path": str(props_path), "manifest_path": str(manifest_path), "srt_path": str(srt_path),
            "total_frames": manifest["total_frames"], "total_duration_seconds": manifest["total_duration_seconds"], "project_hash": project_hash,
        }

    async def _run_exec(self, cmd: list[str], *, cwd: Path, timeout: float) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except BaseException:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        return (proc.returncode if proc.returncode is not None else -1, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace"))

    async def _probe_rendered_video(self, output_path: Path, *, expected_duration: float, dry_run: bool) -> dict[str, Any]:
        if callable(self.ffprobe_runner):
            probe = self.ffprobe_runner(output_path, expected_duration)
            return dict(await probe if asyncio.iscoroutine(probe) else probe)
        if dry_run:
            return {"has_video": True, "has_audio": True, "silent_audio": False, "duration_seconds": expected_duration, "width": 1920, "height": 1080, "dry_run": True}
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise ValueError("未安装 ffprobe，无法执行成片质检")
        code, stdout, stderr = await self._run_exec(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(output_path)],
            cwd=output_path.parent, timeout=30.0,
        )
        if code != 0:
            raise ValueError(f"ffprobe 质检失败: {stderr[:200]}")
        data = json.loads(stdout or "{}")
        streams = data.get("streams") or []
        v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = data.get("format") or {}
        probe: dict[str, Any] = {
            "has_video": v_stream is not None, "has_audio": a_stream is not None, "silent_audio": False,
            "duration_seconds": float(fmt.get("duration") or (v_stream or {}).get("duration") or 0.0),
            "width": int((v_stream or {}).get("width") or 0), "height": int((v_stream or {}).get("height") or 0),
        }
        if (ffmpeg := shutil.which("ffmpeg")) and a_stream is not None:
            fcode, _, ferr = await self._run_exec([ffmpeg, "-hide_banner", "-i", str(output_path), "-af", "volumedetect", "-f", "null", "-"], cwd=output_path.parent, timeout=30.0)
            if fcode == 0 and (m := re.search(r"mean_volume:\s*([-\d.]+)\s*dB", ferr)):
                probe["mean_volume_db"] = float(m.group(1))
                probe["silent_audio"] = float(m.group(1)) <= -60.0
        return probe

    def _validate_qc_probe(self, probe: dict[str, Any], output_path: Path, expected_duration: float) -> dict[str, Any]:
        size = output_path.stat().st_size if output_path.is_file() else 0
        probed_duration = float(probe.get("duration_seconds") or 0.0)
        drift = abs(probed_duration - float(expected_duration))
        issues: list[str] = []
        if size <= 0 or size > MAX_RENDERED_VIDEO_BYTES:
            issues.append("成片文件大小无效或超出平台限制")
        if not probe.get("has_video"):
            issues.append("成片缺少有效视频轨")
        if not probe.get("has_audio"):
            issues.append("成片缺少音频轨")
        mean_db = probe.get("mean_volume_db")
        if probe.get("silent_audio") or (mean_db is not None and float(mean_db) <= -60.0):
            issues.append("成片音轨为全静音")
        if drift > MAX_DURATION_DRIFT_SECONDS:
            issues.append(f"音画时长偏差 {drift:.2f}s 超过 0.5s 上限")
        return {
            "passed": not issues, "issues": issues, "size_bytes": size,
            "expected_duration_seconds": expected_duration, "probed_duration_seconds": probed_duration,
            "duration_drift_seconds": round(drift, 4), "width": int(probe.get("width") or 0), "height": int(probe.get("height") or 0),
        }

    async def render_remotion_video(self, arguments: RenderRemotionVideoArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 4 & 5: Render Remotion project under concurrency guard and enforce FFmpeg/FFprobe QC."""
        args = arguments if isinstance(arguments, RenderRemotionVideoArgs) else RenderRemotionVideoArgs.model_validate(arguments)
        job = self.get_job(args.job_id)
        if job is None or not job.get("project_dir") or not job.get("manifest"):
            raise ValueError("找不到已完成工程准备的视频任务")
        project_dir = Path(job["project_dir"])
        if not project_dir.resolve().is_relative_to(self.renders_root.resolve()) or linked(project_dir):
            raise ValueError("视频工程目录不在受控范围内")
        props_path = project_dir / "props.json"
        if not props_path.is_file():
            raise ValueError("视频工程缺少 props.json")

        output_path, policy = project_dir / "output.mp4", self.policy()
        cmd = ["npx", "remotion", "render", args.composition_id, str(output_path), f"--props={props_path}", f"--concurrency={min(args.concurrency, policy.max_render_concurrency)}"]

        async with self._get_semaphore():
            if callable(self.subprocess_runner):
                code, _out, stderr = await self.subprocess_runner(cmd, project_dir, policy.render_timeout_seconds)
            elif args.dry_run:
                output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
                code, stderr = 0, ""
            else:
                npx_bin = shutil.which("npx")
                if not npx_bin:
                    raise ValueError("未安装 npx / Remotion CLI，请使用 dry_run 或在渲染节点执行")
                cmd[0] = npx_bin
                code, _out, stderr = await self._run_exec(cmd, cwd=project_dir, timeout=policy.render_timeout_seconds)
            if code != 0:
                self._update_job(args.job_id, status="render_failed", error=(stderr[:240] or "Remotion 渲染失败"))
                raise ValueError(f"Remotion 渲染失败: {stderr[:200]}")

        expected = float(job["manifest"].get("total_duration_seconds") or 0.0)
        probe = await self._probe_rendered_video(output_path, expected_duration=expected, dry_run=args.dry_run)
        qc_report = self._validate_qc_probe(probe, output_path, expected)
        if not qc_report["passed"]:
            self._update_job(args.job_id, status="qc_failed", qc_json=dump(qc_report), output_path=str(output_path), error="；".join(qc_report["issues"]))
            return {"ok": False, "job_id": args.job_id, "status": "qc_failed", "output_path": str(output_path), "qc_report": qc_report}

        file_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
        next_status = "ready_for_review" if policy.publish_mode == "preview_review" else "rendered"
        self._update_job(args.job_id, status=next_status, qc_json=dump(qc_report), output_path=str(output_path), content_hash=file_sha256, error=None)
        return {"ok": True, "job_id": args.job_id, "status": next_status, "output_path": str(output_path), "content_hash": file_sha256, "qc_report": qc_report}

    def _auto_publish_gate_check(self) -> str | None:
        policy = self.policy()
        if policy.publish_mode != "auto_publish" or not policy.auto_publish_enabled:
            return "当前策略未开启自动投稿，保持人工预览审核队列"
        if (browsing := self._browsing_service()) is not None:
            snap = heartbeat_pulse_snapshot(browsing, store=self.store)
            if snap.get("control_state") in {"off", "paused", "emergency", "blocked", "stopping"}:
                return f"运营控制面状态为 {snap['control_state']}，暂不自动投稿"
            if (snap.get("writes") or {}).get("uncertain_count"):
                return "存在待核对写入回执，已阻止自动投稿"
            if (snap.get("discovery") or {}).get("read_cooldown_until"):
                return "当前账号处于平台风控冷却期，已转回人工预览审核"
        return None

    async def _upload_via_biliup(self, *, plan: VideoScriptPlan, output_path: Path, credentials: dict[str, Any], timeout: float) -> dict[str, Any]:
        sessdata, csrf, uid = str(credentials.get("sessdata") or "").strip(), str(credentials.get("csrf") or "").strip(), str(credentials.get("uid") or "").strip()
        if not (sessdata and csrf):
            raise ValueError("缺少有效的 B 站登录 Cookie 凭证，无法执行投稿")
        cookie_payload = {"cookie_info": {"cookies": [{"name": "SESSDATA", "value": sessdata}, {"name": "bili_jct", "value": csrf}, *([{"name": "DedeUserID", "value": uid}] if uid else [])]}}
        with tempfile.TemporaryDirectory(prefix="biliup-cookie-") as tmp_dir:
            cookie_file = Path(tmp_dir) / "cookies.json"
            cookie_file.write_text(json.dumps(cookie_payload, ensure_ascii=False), encoding="utf-8")
            with suppress(OSError):
                os.chmod(cookie_file, 0o600)
            cmd = ["biliup", "upload", "--user-cookie", str(cookie_file), "--title", plan.title, "--desc", plan.summary[:250], "--tid", str(plan.category_tid), "--tag", ",".join(plan.tags[:10]), str(output_path)]
            if callable(self.biliup_runner):
                code, stdout, stderr = await self.biliup_runner(cmd, output_path.parent, timeout)
            else:
                biliup_bin = shutil.which("biliup")
                if not biliup_bin:
                    raise ValueError("未安装 biliup CLI，无法直接投稿")
                cmd[0] = biliup_bin
                code, stdout, stderr = await self._run_exec(cmd, cwd=output_path.parent, timeout=timeout)
        safe_out, safe_err = _scrub_secrets(stdout, credentials), _scrub_secrets(stderr, credentials)
        receipt = _parse_biliup_receipt(safe_out, safe_err)
        if code != 0:
            raise RuntimeError(f"biliup exit={code}: {safe_err or safe_out}")
        if not receipt:
            raise TimeoutError("biliup 返回成功但未解析到 bvid/aid，转为待核对状态")
        return receipt

    def _find_existing_job_by_intent(self, intent: str, current_job_id: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT id, status, receipt_json FROM remotion_video_jobs WHERE intent=? AND id!=? AND status IN ('published','publish_uncertain') ORDER BY updated_at DESC LIMIT 1", (intent, current_job_id))
        if row is not None:
            with suppress(ValueError):
                row["receipt"] = json.loads(row["receipt_json"]) if row.get("receipt_json") else None
        return row

    async def stage_or_publish_bilibili_video(self, arguments: StageOrPublishVideoArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 6: Stage in preview_review queue (default) or idempotently publish via biliup."""
        args = arguments if isinstance(arguments, StageOrPublishVideoArgs) else StageOrPublishVideoArgs.model_validate(arguments)
        job = self.get_job(args.job_id)
        if job is None or not job.get("plan"):
            raise ValueError("找不到指定视频任务")
        char = self._current_character()
        char_id = str(char.get("id") or "")
        plan = assert_public_video_sources(job["plan"], store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None), character_id=char_id)
        if not (job.get("qc_report") or {}).get("passed"):
            raise ValueError("成片尚未通过 FFmpeg/FFprobe 质检，不能投稿")

        policy = self.policy()
        requested_mode = args.publish_mode or policy.publish_mode
        effective_hash = job.get("content_hash") or job.get("project_hash") or content_hash(plan.model_dump(mode="json"))
        intent = canonical_intent("bilibili", "video_publish", effective_hash, char_id)

        if requested_mode == "preview_review" and not args.approved:
            self._update_job(args.job_id, status="ready_for_review", publish_mode="preview_review", intent=intent)
            return {"ok": True, "job_id": args.job_id, "action": "staged_for_review", "status": "ready_for_review", "intent": intent, "output_path": job.get("output_path"), "title": plan.title}
        if not args.approved and (gate_err := self._auto_publish_gate_check()) is not None:
            self._update_job(args.job_id, status="ready_for_review", intent=intent, error=gate_err)
            return {"ok": False, "job_id": args.job_id, "action": "gated_to_review", "status": "ready_for_review", "intent": intent, "reason": gate_err}

        account_id = job.get("account_id") or self._account_id() or "local"
        if job.get("status") == "published" and job.get("receipt"):
            return {"ok": True, "job_id": args.job_id, "action": "already_published", "status": "published", "intent": intent, "receipt": job["receipt"]}
        if job.get("status") == "publish_uncertain":
            return {"ok": False, "job_id": args.job_id, "action": "blocked_uncertain", "status": "publish_uncertain", "intent": intent, "error": "上次投稿回执待核对，已阻止重复上传"}

        ops_host = self._browsing_service() or getattr(self.runtime, "operations", None)
        if (existing := find_write_attempt_by_intent(ops_host, account_id=account_id, character_id=char_id, intent=intent)) is not None:
            if existing.get("resolved_status") == "published":
                return {"ok": True, "job_id": args.job_id, "action": "already_published", "status": "published", "intent": intent, "receipt": existing.get("receipt")}
            if existing.get("resolved_status") in {"sending", "uncertain"}:
                return {"ok": False, "job_id": args.job_id, "action": "blocked_uncertain", "status": "publish_uncertain", "intent": intent, "error": "写入账本已有在途或待核对投稿，禁止重复提交"}
        if (dup_job := self._find_existing_job_by_intent(intent, args.job_id)) is not None:
            if dup_job.get("status") == "published":
                return {"ok": True, "job_id": args.job_id, "action": "already_published", "status": "published", "intent": intent, "receipt": dup_job.get("receipt")}
            return {"ok": False, "job_id": args.job_id, "action": "blocked_uncertain", "status": "publish_uncertain", "intent": intent, "error": "已有同内容待核对投稿任务，禁止重复提交"}

        write_token = None
        ops_ctrl = getattr(ops_host, "operations", ops_host) if ops_host is not None else None
        if ops_ctrl is not None and hasattr(ops_ctrl, "reserve") and hasattr(ops_ctrl, "finish"):
            pol_ctrl = control_policy(ops_host)
            epoch = pol_ctrl.get("epoch") if (not args.approved and isinstance(pol_ctrl, dict)) else None
            write_token = reserve_write_attempt(
                ops_host, account_id=account_id, character=char, intent=intent, channel="video_publish", automatic=not args.approved,
                epoch=epoch,
                binding={"job_id": args.job_id, "title": plan.title, "content_hash": effective_hash}, content_hash=effective_hash,
            )

        credentials = dict(getattr(getattr(self.runtime, "account", None), "credentials", None) or {})
        try:
            receipt = await self._upload_via_biliup(plan=plan, output_path=Path(job["output_path"]), credentials=credentials, timeout=policy.upload_timeout_seconds)
        except TimeoutError as exc:
            if write_token:
                finish_write_attempt(ops_host, write_token, "uncertain", error=str(exc))
            self._update_job(args.job_id, status="publish_uncertain", intent=intent, write_token=write_token, error=str(exc)[:300])
            return {"ok": False, "job_id": args.job_id, "action": "uncertain", "status": "publish_uncertain", "intent": intent, "write_token": write_token, "error": str(exc)}
        except Exception as exc:
            if write_token:
                finish_write_attempt(ops_host, write_token, "rejected", error=str(exc)[:300])
            self._update_job(args.job_id, status="publish_failed", intent=intent, write_token=write_token, error=str(exc)[:300])
            return {"ok": False, "job_id": args.job_id, "action": "rejected", "status": "publish_failed", "intent": intent, "write_token": write_token, "error": str(exc)}

        if write_token:
            finish_write_attempt(ops_host, write_token, "published", receipt=receipt)
        self._update_job(args.job_id, status="published", intent=intent, write_token=write_token, receipt_json=dump(receipt), error=None)
        return {"ok": True, "job_id": args.job_id, "action": "published", "status": "published", "intent": intent, "write_token": write_token, "receipt": receipt}

    def status(self) -> dict[str, Any]:
        try:
            policy = self.policy()
            counts = {row["status"]: int(row["n"]) for row in self.store.all("SELECT status, COUNT(*) AS n FROM remotion_video_jobs GROUP BY status")}
        except Exception:
            policy = VideoProductionPolicy()
            counts = {}
        return {"publish_mode": policy.publish_mode, "auto_publish_enabled": policy.auto_publish_enabled, "jobs": counts}


async def plan_video_script(skill: RemotionVideoSkill, arguments: PlanVideoScriptArgs | dict[str, Any]) -> dict[str, Any]:
    return await skill.plan_video_script(arguments)


async def generate_remotion_project(skill: RemotionVideoSkill, arguments: GenerateRemotionProjectArgs | dict[str, Any]) -> dict[str, Any]:
    return await skill.generate_remotion_project(arguments)


async def render_remotion_video(skill: RemotionVideoSkill, arguments: RenderRemotionVideoArgs | dict[str, Any]) -> dict[str, Any]:
    return await skill.render_remotion_video(arguments)


async def stage_or_publish_bilibili_video(skill: RemotionVideoSkill, arguments: StageOrPublishVideoArgs | dict[str, Any]) -> dict[str, Any]:
    return await skill.stage_or_publish_bilibili_video(arguments)


_REMOTION_TOOL_SPECS: tuple[tuple[str, str, type[StrictModel], str], ...] = (
    ("plan_video_script", "基于公开视频或话题规划结构化 Remotion 脚本与分镜 JSON。", PlanVideoScriptArgs, "plan_video_script"),
    ("generate_remotion_project", "生成配音 WAV、字幕 SRT、时间轴 Manifest 与 Remotion TSX 工程。", GenerateRemotionProjectArgs, "generate_remotion_project"),
    ("render_remotion_video", "受并发保护地渲染 Remotion 视频并执行 FFprobe 音画质检。", RenderRemotionVideoArgs, "render_remotion_video"),
    ("stage_or_publish_bilibili_video", "将质检通过的视频放入审核队列或按幂等意图执行投稿。", StageOrPublishVideoArgs, "stage_or_publish_bilibili_video"),
)


def remotion_tool_schemas(*, format: Literal["chat", "responses"] = "responses") -> list[dict[str, Any]]:
    """Build OpenAI Chat Completions or Responses function tool schemas for Remotion pipeline."""
    tools: list[dict[str, Any]] = []
    for name, description, model_cls, _method in _REMOTION_TOOL_SPECS:
        spec = tool_function(name, description, model_cls, require_all=False)
        tools.append(spec if format == "responses" else {"type": "function", "function": {k: spec[k] for k in ("name", "description", "parameters", "strict")}})
    return tools


async def execute_remotion_tool(skill: RemotionVideoSkill, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    """Parse and execute any of the 4 Remotion skill OpenAI function tools."""
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {"ok": False, "error": "工具参数须为 JSON 对象"}
    else:
        parsed = arguments
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "工具参数须为 JSON 对象"}
    dispatch = {tool_name: (model_cls, method_name) for tool_name, _desc, model_cls, method_name in _REMOTION_TOOL_SPECS}
    if name not in dispatch:
        return {"ok": False, "error": f"未知视频工具：{name}"}
    model_cls, method_name = dispatch[name]
    try:
        return await getattr(skill, method_name)(model_cls.model_validate(parsed))
    except (ValidationError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def register_remotion_tools(tasks, *, runtime=None, store=None, browsing=None, skill: RemotionVideoSkill | None = None) -> RemotionVideoSkill:
    """Register the 4 Remotion Code-as-Video tools onto TaskRuntime (`responses_only=True`)."""
    target_skill = skill or getattr(runtime, "remotion_skill", None)
    if target_skill is None:
        target_skill = RemotionVideoSkill(runtime, store or tasks.store, browsing=browsing)
        if runtime is not None:
            runtime.remotion_skill = target_skill

    tool_cls = tasks.Tool
    for tool_name, description, model_cls, method_name in _REMOTION_TOOL_SPECS:
        if tool_name in tasks.tools:
            continue

        async def _runner(_req, args, _fn=getattr(target_skill, method_name)):
            return await _fn(args)

        tasks.register(tool_name, tool_cls(description, model_cls, _runner, own_deadline=True, responses_only=True))
    return target_skill
