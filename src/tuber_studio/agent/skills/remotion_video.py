"""Remotion Code-as-Video production skill, strict schemas, TTS alignment, QC, and Bilibili staging."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, ValidationError, field_validator

from ...core.editorial import CHANNEL_TRIAGE_GUIDANCE, DurationPreset, VideoMode, assess_editorial_channel
from ...core.models import StrictModel, content_hash, tool_function
from ...core.store import dump
from ...core.validation import linked
from ...infra.web_client.fetch import fetch_excerpt
from ...operations import facade as _ops
from ...persona.memory.daily.models import contains_memory_credential
from ...persona.self_model import build_self_model_system_message
from . import remotion_assets as _ra
from . import remotion_audio_qc as _raqc
from . import remotion_planner as _rp
from . import remotion_research as _rr
from .remotion_assets import (
    DEFAULT_LINE_PAUSE_SECONDS,
    DEFAULT_OUTRO_HOLD_SECONDS,
    DEFAULT_SCENE_PAUSE_SECONDS,
    AvatarCue,
    ChartPoint,
    CodeStep,
    DiagramEdge,
    DiagramNode,
    PublicSourceRef,
    SceneVisualProps,
    TemplateId,
    UiFocusRegion,
    VideoLineSpec,
    VideoSceneSpec,
)
from .remotion_audio_qc import MAX_DURATION_DRIFT_SECONDS, MAX_RENDERED_VIDEO_BYTES
from .remotion_publication import StageOrPublishVideoArgs, StageVideoToolArgs, VideoPublicationMixin
from .remotion_recap import (
    RECAP_TARGET_MAX_SECONDS,
    RECAP_TARGET_MIN_SECONDS,
    OperationsRecapBrief,
    estimate_recap_duration_seconds,
    recap_plan_fields,
)

__all__ = [
    "DEFAULT_VIDEO_PRODUCTION_POLICY", "MAX_DURATION_DRIFT_SECONDS", "MAX_RENDERED_VIDEO_BYTES", "VIDEO_PRODUCTION_POLICY_KEY",
    "AvatarCue", "ChartPoint", "CodeStep", "DiagramEdge", "DiagramNode", "DurationPreset", "GenerateRemotionProjectArgs",
    "OperationsRecapBrief", "PlanUploadedImage", "PlanVideoScriptArgs", "PublicSourceRef", "RemotionVideoSkill", "RenderRemotionVideoArgs",
    "SceneVisualProps", "StageOrPublishVideoArgs", "TemplateId", "UiFocusRegion", "VideoLineSpec", "VideoMode",
    "VideoProductionPolicy", "VideoSceneSpec", "VideoScriptPlan", "assert_public_video_sources",
    "capture_job_ui_screenshots", "execute_remotion_tool", "generate_remotion_project", "pitch_video_ideas",
    "plan_video_script", "register_remotion_tools", "remotion_tool_schemas", "render_remotion_video",
    "review_job_visuals", "stage_or_publish_bilibili_video",
]

VIDEO_PRODUCTION_POLICY_KEY = "video_production_policy"
DEFAULT_VIDEO_PRODUCTION_POLICY = {
    "publish_mode": "preview_review", "auto_publish_enabled": False, "default_fps": 30, "default_width": 1920,
    "default_height": 1080, "default_include_avatar": True, "default_bgm_enabled": True,
    "default_line_pause_seconds": DEFAULT_LINE_PAUSE_SECONDS, "default_scene_pause_seconds": DEFAULT_SCENE_PAUSE_SECONDS,
    "max_render_concurrency": 4, "render_timeout_seconds": 1800.0, "upload_timeout_seconds": 600.0,
}
_ALLOWED_PUBLIC_KINDS = frozenset({"watched_video", "public_incident", "operator_topic", "web_research", "public_memory", "public_comment"})
_FORBIDDEN_SOURCE_PREFIXES = ("owner:", "private:", "dm:", "dm_", "chat:", "turn:", "daily-chat:")
_JOB_COLUMNS = frozenset({
    "status", "publish_mode", "title", "content_hash", "project_hash", "intent", "write_token", "plan_json",
    "manifest_json", "qc_json", "receipt_json", "project_dir", "output_path", "cover_path", "planner_source", "planner_error", "error",
    "account_id", "platform_state", "platform_checked_at", "public_url",
})

# Scene & script schemas + static Remotion templates live in remotion_assets.py.


class VideoScriptPlan(StrictModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]] = Field(min_length=1, max_length=10)
    category_tid: int = Field(default=231, ge=1, le=9999)
    video_mode: VideoMode = "commentary"
    duration_preset: DurationPreset = "standard"
    fps: Literal[30, 60] = 30
    width: Literal[1920, 1280] = 1920
    height: Literal[1080, 720] = 1080
    include_avatar: bool = True
    bgm_enabled: bool = True
    line_pause_seconds: float = Field(default=DEFAULT_LINE_PAUSE_SECONDS, ge=0.0, le=2.0)
    scene_pause_seconds: float = Field(default=DEFAULT_SCENE_PAUSE_SECONDS, ge=0.0, le=3.0)
    scenes: list[VideoSceneSpec] = Field(min_length=1, max_length=24)
    sources: list[PublicSourceRef] = Field(min_length=1, max_length=12)


class VideoProductionPolicy(StrictModel):
    publish_mode: Literal["preview_review", "auto_publish"] = "preview_review"
    auto_publish_enabled: bool = False
    default_fps: Literal[30, 60] = 30
    default_width: Literal[1920, 1280] = 1920
    default_height: Literal[1080, 720] = 1080
    default_include_avatar: bool = True
    default_bgm_enabled: bool = True
    default_line_pause_seconds: float = Field(default=DEFAULT_LINE_PAUSE_SECONDS, ge=0.0, le=2.0)
    default_scene_pause_seconds: float = Field(default=DEFAULT_SCENE_PAUSE_SECONDS, ge=0.0, le=3.0)
    max_render_concurrency: int = Field(default=4, ge=1, le=4)
    render_timeout_seconds: float = Field(default=1800.0, ge=5.0, le=3600.0)
    upload_timeout_seconds: float = Field(default=600.0, ge=5.0, le=3600.0)


class PlanUploadedImage(StrictModel):
    filename: str = Field(min_length=1, max_length=80)
    image_base64: str = Field(min_length=1, max_length=45_000_000)
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm"]


class PlanVideoScriptArgs(StrictModel):
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)] = Field(description="公开视频选题、热点话题或 BV 号说明。")
    brief_text: str = Field(default="", max_length=30000, description="可选参考资料、产品说明或原始大纲（最多 30000 字）。")
    reference_urls: list[str] = Field(default_factory=list, max_length=6, description="可选公开参考链接（最多 6 条 HTTPS URL，支持 GitHub 仓库深度抓取）。")
    video_mode: VideoMode = Field(default="commentary", description="视频类型：commentary / daily_vlog / watched_roundup / project_showcase / explainer / self_intro。")
    duration_preset: DurationPreset | None = Field(default=None, description="可选时长档位：short / standard / deep_dive。")
    include_workspace_docs: bool = Field(default=False, description="是否读取允许公开的工坊仓库文档与模块结构。")
    candidate_bvids: list[Annotated[str, StringConstraints(pattern=r"^BV[0-9A-Za-z]{10}$")]] = Field(default_factory=list, max_length=6, description="可选关联的公开视频 BV 号。")
    style_note: str = Field(default="", max_length=200, description="可选节奏或风格提示。")
    fps: Literal[30, 60] = Field(default=30, description="视频帧率（30 或 60）。")
    include_avatar: bool = Field(default=True, description="是否附带 Aqua 虚拟主播演出小窗与动作时间轴。")
    bgm_enabled: bool = Field(default=True, description="是否开启浅海轻量 BGM 与转场音效混音。")
    line_pause_seconds: float = Field(default=DEFAULT_LINE_PAUSE_SECONDS, ge=0.0, le=2.0, description="同一镜内相邻台词句间气口停顿（秒）。")
    scene_pause_seconds: float = Field(default=DEFAULT_SCENE_PAUSE_SECONDS, ge=0.0, le=3.0, description="分镜切换时的停顿留白时长（秒）。")
    max_scenes: int | None = Field(default=None, ge=1, le=20, description="目标分镜数量（不填时按视频类型与时长档位自动推导）。")
    pinned_meme_ids: list[str] = Field(default_factory=list, max_length=3, description="可选置顶图库表情 ID（0-3 张，仅限全渠道可用表情）。")
    uploaded_images: list[PlanUploadedImage] = Field(default_factory=list, max_length=12, description="可选本次视频专属公开配图/截图或短视频片段（0-12 份）。")

    @field_validator("reference_urls", mode="before")
    @classmethod
    def _clean_reference_urls(cls, value: Any) -> list[str]:
        return _ra.normalize_reference_urls(value)


class GenerateRemotionProjectArgs(StrictModel):
    job_id: str | None = Field(default=None, max_length=80, description="已规划的视频制作任务 ID。")
    plan: dict[str, Any] | None = Field(default=None, description="可选显式传入的 VideoScriptPlan 对象。")
    tts_mode: Literal["omni_speech", "dry_run"] = Field(default="omni_speech", description="配音模式：omni_speech 或 dry_run。")
    reference_voice_id: str | None = Field(default=None, max_length=80, description="可选指定参考音色 ID。")


class RenderRemotionVideoArgs(StrictModel):
    job_id: str = Field(min_length=1, max_length=80, description="已生成 Remotion 工程的任务 ID。")
    composition_id: str = Field(default="AquaVideo", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,40}$", description="Remotion 合成组件 ID。")
    concurrency: int = Field(default=2, ge=1, le=4, description="Remotion 渲染并发线程数。")
    dry_run: bool = Field(default=False, description="跳过真实 npx/ffmpeg 子进程，输出干跑校验结果。")


def assert_public_video_sources(
    plan: VideoScriptPlan | dict[str, Any], *, store=None, unified_memory=None, character_id: str = "", extra_texts: Sequence[str] = ()
) -> VideoScriptPlan:
    """Strictly validate that video plan, brief_text & sources contain ZERO owner-private memories or DMs."""
    validated = plan if isinstance(plan, VideoScriptPlan) else VideoScriptPlan.model_validate(plan)
    for src in validated.sources:
        if src.scope != "public" or src.kind not in _ALLOWED_PUBLIC_KINDS:
            raise ValueError("公开视频只能使用 public 作用域来源")
        if src.source_id.casefold().startswith(_FORBIDDEN_SOURCE_PREFIXES):
            raise ValueError("公开视频制作禁止引用私有对话或私信来源")

    texts = [validated.title, validated.summary, *validated.tags, *(str(t) for t in extra_texts if t)]
    for src in validated.sources:
        texts.extend((src.source_id, src.title, src.excerpt))
    for scene in validated.scenes:
        texts.extend(_ra.collect_scene_public_texts(scene))
    _ra.assert_public_texts(texts, contains_credential_fn=contains_memory_credential, store=store, unified_memory=unified_memory, character_id=character_id, source_ids=[s.source_id for s in validated.sources])
    return validated


class RemotionVideoSkill(VideoPublicationMixin):
    """Orchestrates the 6-stage Remotion Code-as-Video production & Bilibili publishing pipeline."""

    _render_semaphore: asyncio.Semaphore | None = None

    def __init__(self, runtime, store, *, browsing=None, renders_root: Path | None = None, studio_dir: Path | None = None, planner_fn=None, tts_fn=None, web_fetch_fn=None, subprocess_runner=None, ffprobe_runner=None, biliup_runner=None, render_semaphore: asyncio.Semaphore | None = None) -> None:
        self.runtime, self.store, self._browsing = runtime, store, browsing
        self.renders_root = Path(renders_root or (getattr(getattr(runtime, "settings", None), "runtime", None) or Path.cwd() / "runtime")) / "video_renders"
        self.studio_dir = _ra._resolve_studio_dir(runtime, studio_dir)
        self.planner_fn, self.tts_fn, self.web_fetch_fn = planner_fn, tts_fn, web_fetch_fn
        self.subprocess_runner, self.ffprobe_runner, self.biliup_runner = subprocess_runner, ffprobe_runner, biliup_runner
        self._own_semaphore, self.active_job_ids = render_semaphore, set()
        self.active_publication_ids: set[str] = set()
        self._install_storage()

    def _touch_video_cache(self, project_dir: Path, *, lease_seconds: int = 0, enforce: bool = False) -> None:
        if (cache := getattr(self.runtime, "cache", None)) is None:
            return
        with suppress(OSError, ValueError):
            if project_dir.resolve().is_relative_to(cache.root.resolve()):
                cache.touch(project_dir, seconds=lease_seconds)
                if enforce:
                    cache.enforce("video")

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._own_semaphore is not None:
            return self._own_semaphore
        if RemotionVideoSkill._render_semaphore is None:
            RemotionVideoSkill._render_semaphore = asyncio.Semaphore(1)
        return RemotionVideoSkill._render_semaphore

    def _install_storage(self) -> None:
        with self.store.db:
            self.store.db.executescript(
                "CREATE TABLE IF NOT EXISTS remotion_video_jobs(id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', character_id TEXT NOT NULL DEFAULT '', character_version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, publish_mode TEXT NOT NULL DEFAULT 'preview_review', title TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', project_hash TEXT NOT NULL DEFAULT '', intent TEXT NOT NULL DEFAULT '', write_token TEXT, plan_json TEXT NOT NULL, manifest_json TEXT, qc_json TEXT, receipt_json TEXT, project_dir TEXT NOT NULL DEFAULT '', output_path TEXT NOT NULL DEFAULT '', cover_path TEXT NOT NULL DEFAULT '', planner_source TEXT NOT NULL DEFAULT '', planner_error TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_remotion_jobs_char ON remotion_video_jobs(character_id, updated_at DESC);"
            )
            existing_cols = {row[1] for row in self.store.db.execute("PRAGMA table_info(remotion_video_jobs)")}
            for col_name, col_def in (("cover_path", "TEXT NOT NULL DEFAULT ''"), ("planner_source", "TEXT NOT NULL DEFAULT ''"), ("planner_error", "TEXT"), ("platform_state", "TEXT"), ("platform_checked_at", "REAL"), ("public_url", "TEXT")):
                if col_name not in existing_cols:
                    self.store.db.execute(f"ALTER TABLE remotion_video_jobs ADD COLUMN {col_name} {col_def}")

    def _shared_studio_installed(self) -> bool:
        return (self.studio_dir / "node_modules/@remotion/cli/remotion-cli.js").is_file() and (
            self.studio_dir / "src/index.ts"
        ).is_file()
    def _browsing_service(self): return self._browsing if self._browsing is not None else getattr(self.runtime, "browsing", None)
    def _current_character(self) -> dict[str, Any]: return getattr(self.runtime, "character", None) or self.store.character()
    def _assets_root(self) -> Path: return Path(root) if (root := getattr(getattr(self.runtime, "settings", None), "root", None)) else Path(__file__).resolve().parents[4]
    def _meme_catalog(self): return getattr(getattr(self.runtime, "pets", None), "catalog", None)

    def environment_status(self) -> dict[str, Any]:
        browser_path, browser_kind = _ra._detect_browser_executable()
        settings, op_llm, voices = getattr(self.runtime, "settings", None), getattr(self.runtime, "operation_llm", None), getattr(self.runtime, "voices", None)
        omni_cfg, tts_ref_ok = (getattr(settings, "data", None) or {}).get("omni") or {}, False
        if settings and callable(getattr(settings, "voice", None)) and callable(getattr(settings, "voice_reference_path", None)):
            with suppress(Exception):
                voice = settings.voice(self._current_character().get("voice_profile") or "whale_reference")
                tts_ref_ok = voice.get("provider") == "omni_reference" and settings.voice_reference_path(voice).is_file()
        account = getattr(self.runtime, "account", None)
        creds = dict(getattr(account, "credentials", None) or {})
        web_creds_ok, cli_ok = bool(str(creds.get("sessdata") or "").strip() and str(creds.get("csrf") or "").strip()), bool(shutil.which("biliup"))
        return {
            "node": {"ready": bool(shutil.which("node"))}, "npx": {"ready": bool(shutil.which("npx"))},
            "ffmpeg": {"ready": bool(shutil.which("ffmpeg"))}, "ffprobe": {"ready": bool(shutil.which("ffprobe"))},
            "remotion_studio": {"ready": self._shared_studio_installed(), "dir_exists": self.studio_dir.is_dir(), "label": self.studio_dir.name},
            "browser": {"ready": bool(browser_path), "kind": browser_kind},
            "biliup": {"ready": web_creds_ok and callable(getattr(account, "account_id", None)),
                       "mode": ("biliup_cli" if callable(self.biliup_runner) else "web_api") if web_creds_ok else "missing",
                       "web_credentials": web_creds_ok, "cli_installed": cli_ok,
                       "account_verified": bool(getattr(account, "display_account_id", lambda: "")())},
            "omni_llm": {"ready": bool(op_llm is not None and callable(getattr(op_llm, "generate_json", None)) and omni_cfg.get("base_url"))},
            "omni_tts": {"ready": bool(voices is not None and tts_ref_ok and omni_cfg.get("base_url"))},
        }

    def _account_id(self) -> str:
        acct = getattr(self.runtime, "account", None)
        if acct is not None and hasattr(acct, "display_account_id") and (uid := str(acct.display_account_id() or "")):
            return uid
        return str(b.account) if (b := self._browsing_service()) is not None and getattr(b, "account", None) else ""

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
        if (row := self.store.one("SELECT * FROM remotion_video_jobs WHERE id=?", (str(job_id),))) is None:
            return None
        decoded = dict(row)
        for col, target in (("plan_json", "plan"), ("manifest_json", "manifest"), ("qc_json", "qc_report"), ("receipt_json", "receipt")):
            raw_val = decoded.pop(col, None)
            try:
                decoded[target] = json.loads(raw_val) if raw_val else None
            except ValueError:
                decoded[target] = None
        return decoded

    def require_project_available(self, job: dict[str, Any]) -> None:
        if job["id"] in self.active_job_ids or job["id"] in self.active_publication_ids:
            raise ValueError("该视频任务正在准备、渲染或投稿")
        if job["status"] in {"scheduled", "publishing", "published", "publish_uncertain"} or job.get("receipt"):
            raise ValueError("该视频任务已进入投稿流程，请先核对当前状态")

    def list_jobs(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE status=?", (status,)) if status else ("", ())
        rows = self.store.all(f"SELECT id FROM remotion_video_jobs {where} ORDER BY updated_at DESC LIMIT ?", (*params, max(1, min(int(limit), 100))))
        return [job for row in rows if (job := self.get_job(row["id"])) is not None]

    def _public_candidates_and_memories(self, *, topic: str, candidate_bvids: list[str], character_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        candidates = _ops.select_video_candidates(self._browsing_service(), store=self.store, limit=12, query=None)
        if candidate_bvids:
            by_bv = {item["bvid"]: item for item in candidates}
            candidates = [by_bv.get(bv) or {"bvid": bv, "title": f"视频 {bv}", "url": f"https://www.bilibili.com/video/{bv}", "summary": topic, "source_kind": "watched_video", "scope": "public"} for bv in candidate_bvids]
        unified = getattr(self.runtime, "unified_memory", None)
        return candidates, (unified.for_prompt("public", character_id=character_id, limit=10) if unified is not None else [])

    @staticmethod
    def _validate_and_normalize_scene_memes(plan: VideoScriptPlan, catalog: list[dict[str, Any]]) -> VideoScriptPlan:
        updated_scenes, changed = [], False
        for scene in plan.scenes:
            if (raw_id := scene.visual_props.meme_asset_id) is not None:
                if (canonical := _ra.resolve_catalog_meme_id(catalog, raw_id)) is None:
                    raise ValueError(f"表情 {raw_id} 不存在、未启用或仅限互动使用")
                if canonical != raw_id:
                    scene = scene.model_copy(update={"visual_props": scene.visual_props.model_copy(update={"meme_asset_id": canonical})})
                    changed = True
            updated_scenes.append(scene)
        return plan.model_copy(update={"scenes": updated_scenes}) if changed else plan

    def _stage_plan_uploads(self, job_id: str, uploaded_images: Sequence[PlanUploadedImage]) -> tuple[str, list[str], list[str], Path | None]:
        if not uploaded_images:
            return "", [], [], None
        self.renders_root.mkdir(parents=True, exist_ok=True)
        if linked(self.renders_root):
            raise ValueError("渲染目录不能使用符号链接")
        project_dir = _ra.safe_child_path(self.renders_root, job_id)
        existed_before, uploaded_images_out, uploaded_videos_out = project_dir.exists(), [], []
        try:
            uploads_dir = _ra.safe_child_path(project_dir, "public/uploads")
            for item in uploaded_images:
                safe_name, _ = _ra.save_job_upload_file(uploads_dir, filename=item.filename, data_base64=item.image_base64, mime_type=item.mime_type)
                rel = f"uploads/{safe_name}"
                target_list = uploaded_videos_out if item.mime_type in _ra.VIDEO_MIME_EXTS else uploaded_images_out
                if rel not in target_list:
                    target_list.append(rel)
            self._touch_video_cache(project_dir)
            return str(project_dir), uploaded_images_out, uploaded_videos_out, (None if existed_before else project_dir)
        except BaseException:
            if not existed_before and project_dir.exists() and not linked(project_dir):
                shutil.rmtree(project_dir, ignore_errors=True)
            raise

    async def plan_video_script(self, arguments: PlanVideoScriptArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 1 & 2: Select strictly public sources, research, and plan a cinematic VideoScriptPlan."""
        args = arguments if isinstance(arguments, PlanVideoScriptArgs) else PlanVideoScriptArgs.model_validate(arguments)
        char = self._current_character()
        char_id, unified = str(char.get("id") or ""), getattr(self.runtime, "unified_memory", None)
        _ra.assert_public_texts([args.topic, args.style_note, args.brief_text], contains_credential_fn=contains_memory_credential, store=self.store, unified_memory=unified, character_id=char_id)
        available_memes = _ra.load_whale_meme_catalog(self._assets_root(), meme_catalog=self._meme_catalog(), character_id=char_id or "whale")
        pinned_meme_ids: list[str] = []
        for raw_id in args.pinned_meme_ids:
            if (canonical := _ra.resolve_catalog_meme_id(available_memes, raw_id)) is None:
                raise ValueError(f"置顶表情 {raw_id} 不存在、未启用或仅限互动使用")
            if canonical not in pinned_meme_ids:
                pinned_meme_ids.append(canonical)
        candidates, public_memories = self._public_candidates_and_memories(topic=args.topic, candidate_bvids=args.candidate_bvids, character_id=char_id)
        research_bundle = await _rr.fetch_deep_reference_materials(args.reference_urls, fetch_fn=self.web_fetch_fn or fetch_excerpt, include_workspace_docs=args.include_workspace_docs, workspace_root=self._assets_root(), topic=args.topic, video_mode=args.video_mode, candidates=candidates)
        ref_pages, ref_diagnostics = research_bundle.get("pages") or [], research_bundle.get("diagnostics") or []
        if ref_pages:
            _ra.assert_public_texts([str(p.get("excerpt") or "") for p in ref_pages], contains_credential_fn=contains_memory_credential, store=self.store, unified_memory=unified, character_id=char_id)
        ref_sources = [PublicSourceRef.model_validate(s) for s in (research_bundle.get("sources") or ())]
        editorial_fit = assess_editorial_channel(args.topic, brief_text=args.brief_text, reference_urls=args.reference_urls, candidate_bvids=args.candidate_bvids, uploaded_count=len(args.uploaded_images), include_workspace_docs=args.include_workspace_docs, requested_mode=args.video_mode if "video_mode" in args.model_fields_set else None, requested_duration=args.duration_preset)
        job_id = f"rem_{uuid4().hex[:16]}"
        project_dir_str, uploaded_image_files, uploaded_video_files, created_project_dir = self._stage_plan_uploads(job_id, args.uploaded_images)
        try:
            sys_msg = build_self_model_system_message(char, channel="dynamic", scene="公开视频脚本与分镜策划", task_intent=_ra.build_aqua_video_planner_intent(args.video_mode, pinned_meme_ids=pinned_meme_ids, uploaded_image_files=[*uploaded_image_files, *uploaded_video_files]))
            plan_kw = {
                "args": args, "char": char, "research_bundle": research_bundle, "candidates": candidates, "public_memories": public_memories,
                "available_memes": available_memes, "pinned_meme_ids": pinned_meme_ids, "uploaded_image_files": uploaded_image_files,
                "uploaded_video_files": uploaded_video_files, "runtime": self.runtime,
                "plan_schema": VideoScriptPlan.model_json_schema(), "system_message": sys_msg,
            }
            plan_dict, planner_source, planner_error = await _rp.plan_cinematic_script_two_stage(**plan_kw, op_llm=getattr(self.runtime, "operation_llm", None), custom_planner_fn=self.planner_fn)
            try:
                plan = self._validate_and_normalize_scene_memes(VideoScriptPlan.model_validate(plan_dict), available_memes)
            except Exception as exc:
                if planner_source != "llm":
                    raise
                planner_error = _ra._sanitize_diagnostic(f"{type(exc).__name__}: {exc}")
                plan_dict, planner_source, _ = await _rp.plan_cinematic_script_two_stage(**plan_kw, op_llm=None, custom_planner_fn=None)
                plan = self._validate_and_normalize_scene_memes(VideoScriptPlan.model_validate(plan_dict), available_memes)
            if planner_source == "fallback" and len(uploaded_image_files) > 1:
                bound = [sc for sc in plan.scenes if sc.visual_props.uploaded_image_file]
                if bound:
                    rebound = {id(sc): uploaded_image_files[i] for i, sc in enumerate(bound[:len(uploaded_image_files)])}
                    plan = plan.model_copy(update={"scenes": [sc.model_copy(update={"visual_props": sc.visual_props.model_copy(update={"uploaded_image_file": rebound[id(sc)]})}) if id(sc) in rebound else sc for sc in plan.scenes]})
            plan = _ra.finalize_rich_plan_fields(plan, topic=args.topic, brief_text=args.brief_text, reference_urls=args.reference_urls, video_mode=args.video_mode, ref_sources=ref_sources)
            validated = assert_public_video_sources(plan, store=self.store, unified_memory=unified, character_id=char_id, extra_texts=[args.brief_text, *(str(p.get("excerpt") or "") for p in ref_pages)])
            now, plan_hash = time.time(), content_hash(validated.model_dump(mode="json"))
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO remotion_video_jobs(id,account_id,character_id,character_version,status,publish_mode,title,content_hash,plan_json,project_dir,planner_source,planner_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, self._account_id(), char_id, int(char.get("version") or 1), "script_planned", self.policy().publish_mode, validated.title, plan_hash, validated.model_dump_json(), project_dir_str, planner_source, planner_error, now, now),
                )
        except BaseException:
            if created_project_dir is not None and created_project_dir.exists() and not linked(created_project_dir):
                shutil.rmtree(created_project_dir, ignore_errors=True)
            raise
        return {"ok": True, "job_id": job_id, "status": "script_planned", "content_hash": plan_hash, "planner_source": planner_source, "planner_error": planner_error, "reference_diagnostics": ref_diagnostics, "editorial_fit": editorial_fit.model_dump(mode="json"), "plan": validated.model_dump(mode="json")}

    def plan_operations_recap(
        self, brief: OperationsRecapBrief | dict[str, Any], *, job_key: str | None = None
    ) -> dict[str, Any]:
        """Store the public snapshot once so scheduled work can resume the same draft."""
        args = brief if isinstance(brief, OperationsRecapBrief) else OperationsRecapBrief.model_validate(brief)
        character = self._current_character()
        account_id, character_id = self._account_id(), str(character["id"])
        if job_key is None:
            job_id = f"rem_{uuid4().hex[:16]}"
        else:
            if not isinstance(job_key, str) or not 1 <= len(job_key.strip()) <= 160:
                raise ValueError("运营复盘任务键需要 1 至 160 字")
            job_id = f"rem_recap_{content_hash([account_id, character_id, job_key.strip()])[:24]}"
        if (job := self.get_job(job_id)) is None:
            policy = self.policy()
            plan = assert_public_video_sources(
                recap_plan_fields(args) | {
                    "fps": policy.default_fps,
                    "width": policy.default_width,
                    "height": policy.default_height,
                    "include_avatar": policy.default_include_avatar,
                    "bgm_enabled": policy.default_bgm_enabled,
                    "line_pause_seconds": policy.default_line_pause_seconds,
                    "scene_pause_seconds": policy.default_scene_pause_seconds,
                },
                store=self.store,
                unified_memory=getattr(self.runtime, "unified_memory", None),
                character_id=character_id,
            )
            if args.editorial is not None:
                estimated = estimate_recap_duration_seconds(
                    plan.scenes,
                    line_pause_seconds=plan.line_pause_seconds,
                    scene_pause_seconds=plan.scene_pause_seconds,
                )
                if not RECAP_TARGET_MIN_SECONDS <= estimated <= RECAP_TARGET_MAX_SECONDS:
                    raise ValueError(
                        f"复盘视频预估时长 {estimated:.1f}s 超出 {RECAP_TARGET_MIN_SECONDS:.0f}~{RECAP_TARGET_MAX_SECONDS:.0f}s 允许范围"
                    )
            now = time.time()
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO remotion_video_jobs(id,account_id,character_id,character_version,status,publish_mode,"
                    "title,content_hash,plan_json,planner_source,created_at,updated_at) "
                    "VALUES(?,?,?,?,'script_planned','preview_review',?,?,?,'operations_recap',?,?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (job_id, account_id, character_id, int(character["version"]), plan.title,
                     content_hash(plan.model_dump(mode="json")), plan.model_dump_json(), now, now),
                )
            job = self.get_job(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "status": job["status"],
            "plan": job["plan"],
            "planner_source": job["planner_source"],
            "content_hash": job["content_hash"],
        }

    def _require_editable_job(self, job_id: str, action: str = "修改脚本") -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None or not job.get("plan"):
            raise ValueError("找不到指定视频任务")
        if job.get("status") in {"published", "publish_uncertain", "rendering"}:
            raise ValueError(f"当前状态（{job['status']}）不允许{action}")
        return job

    def list_job_uploads(self, job_or_id: dict[str, Any] | str) -> list[dict[str, Any]]:
        job = self.get_job(job_or_id) if isinstance(job_or_id, str) else job_or_id
        if not job or not self.renders_root.is_dir():
            return []
        try:
            return _ra.list_job_upload_entries(_ra.resolve_controlled_job_dir(self.renders_root, job), str(job["id"]))
        except ValueError:
            return []

    def upload_job_image(self, job_id: str, *, filename: str, image_base64: str, mime_type: str) -> dict[str, Any]:
        job = self._require_editable_job(job_id, "上传媒体")
        project_dir = _ra.resolve_controlled_job_dir(self.renders_root, job, create=True)
        safe_name, size_bytes = _ra.save_job_upload_file(_ra.safe_child_path(project_dir, "public/uploads"), filename=filename, data_base64=image_base64, mime_type=mime_type)
        if not str(job.get("project_dir") or "").strip():
            self._update_job(job_id, project_dir=str(project_dir))
        self._touch_video_cache(project_dir)
        uploads = _ra.list_job_upload_entries(project_dir, job_id)
        entry = next((u for u in uploads if u["filename"] == safe_name), {"filename": safe_name, "rel_path": f"uploads/{safe_name}", "url": f"/api/v1/operations/videos/{job_id}/uploads/{safe_name}", "size_bytes": size_bytes})
        return {"ok": True, "upload": entry, "uploads": uploads}

    def pitch_video_ideas(self, *, brief_hint: str = "", max_ideas: int = 5) -> dict[str, Any]:
        char_id = str(self._current_character().get("id") or "")
        _ra.assert_public_texts([brief_hint], contains_credential_fn=contains_memory_credential, store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None), character_id=char_id)
        candidates, public_memories = self._public_candidates_and_memories(topic=brief_hint, candidate_bvids=[], character_id=char_id)
        return {"ok": True, "ideas": _rp.pitch_video_ideas(candidates=candidates, public_memories=public_memories, brief_hint=brief_hint, max_ideas=max_ideas), "channel_guidance": CHANNEL_TRIAGE_GUIDANCE}

    async def capture_job_ui_screenshots(self, job_id: str, *, base_url: str = "http://127.0.0.1:9020", routes: Sequence[str] | None = None, max_captures: int = 6) -> dict[str, Any]:
        job = self._require_editable_job(job_id, "写入截图")
        project_dir = _ra.resolve_controlled_job_dir(self.renders_root, job, create=True)
        result = await _rr.capture_workbench_screenshots(_ra.safe_child_path(project_dir, "public/uploads"), base_url=base_url, routes=routes, max_captures=max_captures)
        if not str(job.get("project_dir") or "").strip():
            self._update_job(job_id, project_dir=str(project_dir))
        self._touch_video_cache(project_dir)
        return result | {"uploads": _ra.list_job_upload_entries(project_dir, job_id)}

    async def review_job_visuals(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None or not job.get("plan"):
            raise ValueError("找不到指定视频任务")
        raw_dir = str(job.get("project_dir") or "").strip()
        project_dir = _ra.resolve_controlled_job_dir(self.renders_root, job) if raw_dir else None
        sheet_path = (project_dir / "contact_sheet.png") if project_dir is not None else None
        manifest = job.get("manifest") or {}
        scenes = manifest.get("scenes") or job["plan"].get("scenes") or []
        scene_frames: list[dict[str, Any]] = []
        if project_dir is not None and (not sheet_path or not sheet_path.is_file()) and (out_file := Path(job["output_path"]) if job.get("output_path") else project_dir / "output.mp4").is_file():
            sheet_path, scene_frames = await _raqc.extract_scene_contact_sheet(out_file, scenes, fps=int(manifest.get("fps") or job["plan"].get("fps") or 30), project_dir=project_dir, run_exec=self._run_exec)
        if not scene_frames:
            scene_frames = [{"scene_index": i, "template": s.get("template"), "heading": s.get("heading")} for i, s in enumerate(scenes, start=1) if isinstance(s, dict)]
        return await _raqc.run_vlm_visual_review(self.runtime, sheet_path, scene_frames, job["plan"])

    def resolve_job_upload(self, job_id: str, filename: str) -> tuple[Path, str]:
        if (job := self.get_job(job_id)) is None:
            raise ValueError("找不到指定视频任务")
        project_dir = _ra.resolve_controlled_job_dir(self.renders_root, job)
        target, media_type = _ra.resolve_job_upload_file(project_dir, filename)
        self._touch_video_cache(project_dir)
        return target, media_type

    def update_job_plan(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        job = self._require_editable_job(job_id, "修改脚本")
        if not isinstance(patch, dict) or not patch or (set(patch) - set(VideoScriptPlan.model_fields)):
            raise ValueError("脚本更新内容为空或包含未知字段")
        char_id = str(self._current_character().get("id") or "")
        validated = assert_public_video_sources(dict(job["plan"]) | patch, store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None), character_id=char_id)
        validated = self._validate_and_normalize_scene_memes(validated, _ra.load_whale_meme_catalog(self._assets_root(), meme_catalog=self._meme_catalog(), character_id=char_id or "whale"))
        dumped = validated.model_dump(mode="json")
        structural = any(f in patch and dumped.get(f) != job["plan"].get(f) for f in ("scenes", "fps", "width", "height", "include_avatar", "bgm_enabled", "line_pause_seconds", "scene_pause_seconds"))
        updates: dict[str, Any] = {"title": validated.title, "plan_json": validated.model_dump_json(), "status": "script_planned" if structural else job["status"], "error": None}
        if structural:
            updates |= {"content_hash": content_hash(dumped), "qc_json": None}
        self._update_job(job_id, **updates)
        return {"ok": True, "job": self.get_job(job_id), "structural_changed": structural}

    def delete_job(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError("找不到指定视频任务")
        self.require_project_available(job)
        if job.get("status") == "rendering":
            raise ValueError(f"状态为 {job['status']} 的视频任务不能删除")
        if (raw_dir := str(job.get("project_dir") or "").strip()) and (proj := Path(raw_dir)).exists() and not linked(proj) and proj.resolve().is_relative_to(self.renders_root.resolve()):
            shutil.rmtree(proj, ignore_errors=True)
        with self.store.db:
            self.store.db.execute("DELETE FROM remotion_video_jobs WHERE id=?", (str(job_id),))
        return {"ok": True, "deleted": True, "job_id": str(job_id)}

    def resolve_job_media(self, job_id: str, kind: str) -> tuple[Path, str]:
        job = self.get_job(job_id)
        if job is None or not (raw_dir := str(job.get("project_dir") or "").strip()):
            raise ValueError("找不到指定视频任务或尚未生成工程文件")
        project_dir = Path(raw_dir)
        if not project_dir.resolve().is_relative_to(self.renders_root.resolve()) or linked(project_dir):
            raise ValueError("视频工程目录不在受控范围内")
        mapping = {
            "video": (Path(job["output_path"]) if job.get("output_path") else project_dir / "output.mp4", "video/mp4"),
            "audio": (project_dir / "public/narration.wav", "audio/wav"),
            "subtitles": (project_dir / "subtitles.srt", "text/plain; charset=utf-8"),
            "subtitles_vtt": (project_dir / "subtitles.vtt", "text/vtt; charset=utf-8"),
            "cover": (Path(job["cover_path"]) if job.get("cover_path") else project_dir / "cover.png", "image/png"),
            "contact_sheet": (project_dir / "contact_sheet.png", "image/png"), "props": (project_dir / "props.json", "application/json; charset=utf-8"),
            "manifest": (project_dir / "manifest.json", "application/json; charset=utf-8"), "code": (project_dir / "src/AquaVideo.tsx", "text/plain; charset=utf-8"),
        }
        if kind not in mapping:
            raise ValueError(f"不支持的视频产物类型: {kind}")
        target, media_type = mapping[kind]
        resolved = target.resolve()
        if not resolved.is_relative_to(self.renders_root.resolve()) or linked(target) or not resolved.is_file():
            raise FileNotFoundError(f"产物 {kind} 尚未生成或不存在")
        self._touch_video_cache(project_dir)
        return resolved, media_type

    async def _synthesize_line_wav(self, text: str, *, tts_mode: Literal["omni_speech", "dry_run"], voice_id: str | None) -> bytes:
        if callable(self.tts_fn):
            res = self.tts_fn(text, voice_id)
            res = await res if asyncio.iscoroutine(res) else res
            if isinstance(res, Path):
                return res.read_bytes()
            if isinstance(res, (bytes, bytearray)):
                return bytes(res)
            raise ValueError("自定义 TTS 回调必须返回 WAV 字节或路径")
        if tts_mode == "dry_run":
            return _ra._build_dry_run_wav_bytes(text)
        voices, settings = getattr(self.runtime, "voices", None), getattr(self.runtime, "settings", None)
        if voices is None or settings is None:
            raise ValueError("TTS 语音服务未配置，请传入 tts_mode='dry_run' 或配置 Omni 语音")
        audio = await voices.synthesize(text, settings.voice(voice_id or self._current_character().get("voice_profile") or "whale_reference"))
        return audio.path.read_bytes()

    async def generate_remotion_project(self, arguments: GenerateRemotionProjectArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 3 & 4 Prep: Synthesize aligned TTS WAV/SRT/manifest and scaffold safe Remotion TSX project."""
        args = arguments if isinstance(arguments, GenerateRemotionProjectArgs) else GenerateRemotionProjectArgs.model_validate(arguments)
        char = self._current_character()
        char_id = str(char.get("id") or "")
        job = self.get_job(args.job_id) if args.job_id else None
        if job is not None:
            self.require_project_available(job)
        if (raw_plan := args.plan if args.plan is not None else (job.get("plan") if job else None)) is None:
            raise ValueError("需要提供有效的 job_id 或 plan")
        plan = assert_public_video_sources(raw_plan, store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None), character_id=char_id)
        effective_char_id = char_id or "whale"
        catalog = _ra.load_whale_meme_catalog(self._assets_root(), meme_catalog=self._meme_catalog(), character_id=effective_char_id)
        plan = self._validate_and_normalize_scene_memes(plan, catalog)
        job_id = job["id"] if job is not None else f"rem_{uuid4().hex[:16]}"
        self.renders_root.mkdir(parents=True, exist_ok=True)
        if linked(self.renders_root):
            raise ValueError("渲染目录不能使用符号链接")
        project_dir = _ra.safe_child_path(self.renders_root, job_id)
        self.active_job_ids.add(job_id)
        try:
            public_dir, audio_dir = project_dir / "public", project_dir / "public/audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            wav_spec: tuple[int, int, int] | None = None
            pcm_chunks, cues, scene_props, avatar_cues, srt_lines, vtt_lines = [], [], [], [], [], ["WEBVTT\n"]
            cursor_seconds, cursor_frame, srt_index = 0.0, 0, 1
            line_gap_frames = max(0, int(round(plan.line_pause_seconds * plan.fps)))
            scene_gap_frames = max(0, int(round(plan.scene_pause_seconds * plan.fps)))
            lead_in_target = max(0, int(round(min(0.25, plan.scene_pause_seconds * 0.35) * plan.fps))) if scene_gap_frames > 0 else 0
            outro_frames = max(0, int(round(min(DEFAULT_OUTRO_HOLD_SECONDS, max(plan.line_pause_seconds, plan.scene_pause_seconds)) * plan.fps)))
            catalog_ids, used_meme_ids, resolved_scene_memes = {m["id"] for m in catalog}, set(), []
            num_scenes = len(plan.scenes)
            uploaded_clips = [u["rel_path"] for u in _ra.list_job_upload_entries(project_dir, job_id) if u.get("kind") == "video"]
            pending_clip = uploaded_clips[0] if (uploaded_clips and not any(s.visual_props.uploaded_video_file for s in plan.scenes)) else None
            clip_target_idx = next((i for i, s in enumerate(plan.scenes, start=1) if s.template == "UIShowcase"), 2 if num_scenes > 2 else 1) if pending_clip else None
            for s_idx, scene in enumerate(plan.scenes, start=1):
                scene_start_frame, scene_frames = cursor_frame, 0
                if s_idx > 1 and lead_in_target > 0 and wav_spec is not None:
                    pcm_chunks.append(_ra._silence_pcm_for_frames(wav_spec, lead_in_target, plan.fps))
                    cursor_frame += lead_in_target
                    scene_frames += lead_in_target
                num_lines = len(scene.lines)
                for l_idx, line in enumerate(scene.lines, start=1):
                    line_id = line.line_id or f"s{s_idx}_l{l_idx}"
                    wav_bytes = await self._synthesize_line_wav(line.text, tts_mode=args.tts_mode, voice_id=args.reference_voice_id)
                    spec, pcm, duration = _ra._read_wav_pcm(wav_bytes)
                    if wav_spec is None:
                        wav_spec = spec
                    elif spec != wav_spec:
                        raise ValueError("逐句配音音频采样率或声道数不一致")
                    line_frames = max(1, math.ceil(duration * plan.fps))
                    (audio_dir / f"{line_id}.wav").write_bytes(wav_bytes)
                    pcm_chunks.append(_ra._pad_pcm_to_frames(wav_spec, pcm, line_frames, plan.fps))
                    start_sec, end_sec = cursor_frame / float(plan.fps), (cursor_frame + line_frames) / float(plan.fps)
                    cues.append({
                        "line_id": line_id, "scene_id": scene.scene_id, "text": line.text, "start_frame": cursor_frame,
                        "duration_frames": line_frames, "start_seconds": round(start_sec, 4), "end_seconds": round(end_sec, 4),
                        "audio_file": f"audio/{line_id}.wav", "avatar_cue": line.avatar_cue, "highlight_code": line.highlight_code,
                        "emphasis": list(line.emphasis), "reveal_step": line.reveal_step if line.reveal_step is not None else (l_idx - 1),
                        "visual_focus": line.visual_focus, "sfx": line.sfx, "line_index_in_scene": l_idx - 1,
                    })
                    avatar_cues.append({"line_id": line_id, "cue": line.avatar_cue, "start_frame": cursor_frame, "duration_frames": line_frames, "viseme_energy": 0.65 if args.tts_mode != "dry_run" else 0.35})
                    srt_lines.append(f"{srt_index}\n{_ra._format_srt_timestamp(start_sec)} --> {_ra._format_srt_timestamp(end_sec)}\n{line.text}\n")
                    vtt_lines.append(f"{_ra._format_vtt_timestamp(start_sec)} --> {_ra._format_vtt_timestamp(end_sec)}\n{line.text}\n")
                    srt_index += 1
                    cursor_frame += line_frames
                    scene_frames += line_frames
                    if (gap := line_gap_frames if l_idx < num_lines else (max(0, scene_gap_frames - lead_in_target) if s_idx < num_scenes else outro_frames)) > 0:
                        pcm_chunks.append(_ra._silence_pcm_for_frames(wav_spec, gap, plan.fps))
                        cursor_frame += gap
                        scene_frames += gap
                    cursor_seconds = cursor_frame / float(plan.fps)
                vp_dict = scene.visual_props.model_dump(mode="json")
                if pending_clip and s_idx == clip_target_idx and not vp_dict.get("uploaded_video_file"):
                    vp_dict["uploaded_video_file"] = pending_clip
                    pending_clip = None
                picked_meme = _ra.pick_distinct_scene_meme_id(scene.avatar_cue, used_meme_ids, catalog_ids, preferred_id=vp_dict.get("meme_asset_id"))
                if not vp_dict.get("meme_asset_id") and picked_meme and scene.template in {"TitleCard", "CodeWalkthrough", "MemeCutaway", "QuoteSpotlight"}:
                    vp_dict["meme_asset_id"] = picked_meme
                if vp_dict.get("meme_asset_id"):
                    used_meme_ids.add(str(vp_dict["meme_asset_id"]))
                    resolved_scene_memes.append(str(vp_dict["meme_asset_id"]))
                scene_props.append({
                    "scene_id": scene.scene_id, "template": scene.template, "heading": scene.heading,
                    "chapter_title": scene.chapter_title, "theme": scene.theme, "transition": scene.transition,
                    "camera_motion": scene.camera_motion, **({"avatar_layout": scene.avatar_layout} if scene.avatar_layout else {}),
                    "start_frame": scene_start_frame, "duration_frames": max(1, scene_frames), "visual_props": vp_dict, "avatar_cue": scene.avatar_cue,
                })
            assert wav_spec is not None
            narration_path, srt_path, vtt_path, avatar_cues_path = public_dir / "narration.wav", project_dir / "subtitles.srt", project_dir / "subtitles.vtt", project_dir / "avatar_cues.json"
            narration_path.write_bytes(_ra._concat_wav_pcm(wav_spec, pcm_chunks))
            srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
            vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")
            avatar_assets, meme_assets = _ra.stage_whale_visual_assets(self._assets_root(), public_dir, include_avatar=plan.include_avatar, requested_meme_ids=resolved_scene_memes, scene_cues=[s.avatar_cue for s in plan.scenes], meme_catalog=self._meme_catalog(), character_id=effective_char_id)
            audio_envelope = _raqc.extract_audio_envelope(narration_path, fps=plan.fps, total_frames=max(1, cursor_frame))
            bgm_file, sfx_files = _raqc.prepare_bgm_and_sfx_kit(self._assets_root(), audio_dir, duration_seconds=cursor_seconds) if plan.bgm_enabled else (None, {})
            avatar_cues_path.write_text(dump({"include_avatar": plan.include_avatar, "avatar_mode": "gif_frames_ready" if avatar_assets else "2d_fallback", "cues": avatar_cues}), encoding="utf-8")
            props_payload = {
                "title": plan.title, "fps": plan.fps, "width": plan.width, "height": plan.height, "totalFrames": max(1, cursor_frame),
                "audioFile": "narration.wav", "includeAvatar": plan.include_avatar, "bgmEnabled": plan.bgm_enabled, "bgmFile": bgm_file,
                "transitionSfxFile": sfx_files.get("transition") if plan.bgm_enabled else None, "sfxFiles": sfx_files,
                "audioEnvelope": audio_envelope, "duration_preset": plan.duration_preset, "avatarAssets": avatar_assets, "memeAssets": meme_assets,
                "scenes": scene_props, "cues": cues,
            }
            props_path, manifest_path = project_dir / "props.json", project_dir / "manifest.json"
            _ra._write_json(props_path, props_payload)
            manifest = {
                "job_id": job_id, "fps": plan.fps, "width": plan.width, "height": plan.height, "total_frames": max(1, cursor_frame), "total_duration_seconds": round(cursor_seconds, 4),
                "tts_mode": args.tts_mode, "planner_source": (job or {}).get("planner_source") or "explicit_plan", "planner_error": (job or {}).get("planner_error"),
                "audio_file": str(narration_path), "srt_file": str(srt_path), "vtt_file": str(vtt_path), "avatar_cues_file": str(avatar_cues_path),
                "props_file": str(props_path), "scenes": [{"scene_id": s["scene_id"], "template": s["template"], "heading": s["heading"], "start_frame": s["start_frame"], "duration_frames": s["duration_frames"]} for s in scene_props],
                "cues": cues, "sources": [src.model_dump(mode="json") for src in plan.sources],
            }
            _ra._write_json(manifest_path, manifest)
            _ra.stage_remotion_scaffold(self.studio_dir, project_dir)
            project_hash = content_hash({"plan": plan.model_dump(mode="json"), "props": props_payload})
            if job is None:
                now = time.time()
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO remotion_video_jobs(id,account_id,character_id,character_version,status,publish_mode,title,content_hash,project_hash,plan_json,manifest_json,project_dir,planner_source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job_id, self._account_id(), char_id, int(char.get("version") or 1), "project_ready", self.policy().publish_mode, plan.title, content_hash(plan.model_dump(mode="json")), project_hash, plan.model_dump_json(), dump(manifest), str(project_dir), "explicit_plan", now, now),
                    )
            else:
                self._update_job(
                    job_id, status="project_ready", title=plan.title, plan_json=plan.model_dump_json(),
                    project_hash=project_hash, manifest_json=dump(manifest), project_dir=str(project_dir),
                    qc_json=None, output_path="", cover_path="", error=None,
                )
            self._touch_video_cache(project_dir, lease_seconds=1800, enforce=True)
            return {"ok": True, "job_id": job_id, "status": "project_ready", "project_dir": str(project_dir), "props_path": str(props_path), "manifest_path": str(manifest_path), "srt_path": str(srt_path), "vtt_path": str(vtt_path), "total_frames": manifest["total_frames"], "total_duration_seconds": manifest["total_duration_seconds"], "project_hash": project_hash}
        finally:
            self.active_job_ids.discard(job_id)

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

    async def _postprocess_render(
        self, output_path: Path, project_dir: Path, *, expected_duration: float, scenes: Sequence[dict[str, Any]] = (), fps: int = 30
    ) -> tuple[bool, Path | None, Path | None, list[dict[str, Any]]]:
        if not (ffmpeg := shutil.which("ffmpeg")) or not output_path.is_file():
            return False, None, None, []
        loudnorm_applied, norm_tmp = False, project_dir / "output.loudnorm.mp4"
        try:
            code, _, _ = await self._run_exec([ffmpeg, "-y", "-hide_banner", "-i", str(output_path), "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(norm_tmp)], cwd=project_dir, timeout=120.0)
            if code == 0 and norm_tmp.is_file() and norm_tmp.stat().st_size > 0:
                norm_tmp.replace(output_path)
                loudnorm_applied = True
        finally:
            norm_tmp.unlink(missing_ok=True)
        cover_path = project_dir / "cover.png"
        ccode, _, _ = await self._run_exec([ffmpeg, "-y", "-hide_banner", "-ss", f"{min(1.0, max(0.0, expected_duration * 0.35)):.2f}", "-i", str(output_path), "-frames:v", "1", str(cover_path)], cwd=project_dir, timeout=30.0)
        sheet_path, scene_frames = await _raqc.extract_scene_contact_sheet(output_path, scenes, fps=fps, project_dir=project_dir, run_exec=self._run_exec, ffmpeg_bin=ffmpeg)
        return loudnorm_applied, (cover_path if ccode == 0 and cover_path.is_file() else None), sheet_path, scene_frames

    async def render_remotion_video(self, arguments: RenderRemotionVideoArgs | dict[str, Any]) -> dict[str, Any]:
        """Stage 4 & 5: Render Remotion project under concurrency guard and enforce FFmpeg/FFprobe QC."""
        args = arguments if isinstance(arguments, RenderRemotionVideoArgs) else RenderRemotionVideoArgs.model_validate(arguments)
        job = self.get_job(args.job_id)
        if job is None or not job.get("project_dir") or not job.get("manifest"):
            raise ValueError("找不到已完成工程准备的视频任务")
        self.require_project_available(job)
        project_dir = Path(job["project_dir"])
        if not project_dir.resolve().is_relative_to(self.renders_root.resolve()) or linked(project_dir):
            raise ValueError("视频工程目录不在受控范围内")
        if not (props_path := project_dir / "props.json").is_file():
            raise ValueError("视频工程缺少 props.json")
        output_path, policy, public_dir = project_dir / "output.mp4", self.policy(), project_dir / "public"
        cmd = ["npx", "remotion", "render", args.composition_id, str(output_path), f"--props={props_path}", f"--concurrency={min(args.concurrency, policy.max_render_concurrency)}"]
        self.active_job_ids.add(args.job_id)
        try:
            self._update_job(args.job_id, status="rendering", qc_json=None, error=None)
            async with self._get_semaphore():
                if callable(self.subprocess_runner):
                    code, _out, stderr = await self.subprocess_runner(cmd, project_dir, policy.render_timeout_seconds)
                elif args.dry_run:
                    output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
                    code, stderr = 0, ""
                else:
                    studio = (self.studio_dir if self._shared_studio_installed() else project_dir).resolve()
                    cli = studio / "node_modules/@remotion/cli/remotion-cli.js"
                    if not (node_bin := shutil.which("node")) or not cli.is_file():
                        error = "渲染工程缺少本地 Node.js 或 Remotion CLI"
                        self._update_job(args.job_id, status="render_failed", error=error)
                        raise ValueError(error)
                    real_cmd = [
                        node_bin, str(cli), "render", str(studio / "src/index.ts"), *cmd[3:],
                        *([f"--public-dir={public_dir}"] if public_dir.is_dir() else []),
                    ]
                    if (browser_path := _ra._detect_browser_executable()[0]):
                        real_cmd.append(f"--browser-executable={browser_path}")
                    code, _out, stderr = await self._run_exec(real_cmd, cwd=studio, timeout=policy.render_timeout_seconds)
                if code != 0:
                    err_msg = _ra._sanitize_diagnostic(stderr) or "Remotion 渲染失败"
                    self._update_job(args.job_id, status="render_failed", error=err_msg)
                    raise ValueError(f"Remotion 渲染失败: {err_msg[:200]}")
            manifest = job["manifest"]
            expected = float(manifest.get("total_duration_seconds") or 0.0)
            scenes_meta = manifest.get("scenes") or (job.get("plan") or {}).get("scenes") or ()
            if not args.dry_run and not callable(self.subprocess_runner):
                loudnorm_applied, cover_path, sheet_path, scene_frames = await self._postprocess_render(output_path, project_dir, expected_duration=expected, scenes=scenes_meta, fps=int(manifest.get("fps") or 30))
            else:
                loudnorm_applied, cover_path, sheet_path, scene_frames = False, None, None, []
            probe = await _raqc.probe_rendered_video(
                output_path, expected_duration=expected, dry_run=args.dry_run,
                ffprobe_runner=self.ffprobe_runner, run_exec=self._run_exec,
            )
            qc_report = _raqc.validate_qc_probe(probe, output_path, expected)
            qc_report |= {"dry_run": args.dry_run, "loudnorm_applied": loudnorm_applied, "cover_generated": cover_path is not None, "contact_sheet_generated": sheet_path is not None, "scene_frames": len(scene_frames)}
            if not qc_report["passed"]:
                self._update_job(args.job_id, status="qc_failed", qc_json=dump(qc_report), output_path=str(output_path), cover_path=str(cover_path or ""), error="；".join(qc_report["issues"]))
                return {"ok": False, "job_id": args.job_id, "status": "qc_failed", "output_path": str(output_path), "cover_path": str(cover_path or ""), "qc_report": qc_report}
            file_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
            next_status = "ready_for_review" if policy.publish_mode == "preview_review" else "rendered"
            self._update_job(args.job_id, status=next_status, qc_json=dump(qc_report), output_path=str(output_path), cover_path=str(cover_path or ""), content_hash=file_sha256, error=None)
            self._touch_video_cache(project_dir, lease_seconds=1800, enforce=True)
            return {"ok": True, "job_id": args.job_id, "status": next_status, "output_path": str(output_path), "cover_path": str(cover_path or ""), "content_hash": file_sha256, "qc_report": qc_report}
        except (Exception, asyncio.CancelledError) as exc:
            current = self.get_job(args.job_id)
            if current and current["status"] == "rendering":
                error = _ra._sanitize_diagnostic(str(exc)) or ("渲染已取消" if isinstance(exc, asyncio.CancelledError) else "渲染失败")
                self._update_job(args.job_id, status="render_failed", error=error)
            raise
        finally:
            self.active_job_ids.discard(args.job_id)

    def _verified_publication_plan(self, job, character):
        return assert_public_video_sources(
            job["plan"], store=self.store, unified_memory=getattr(self.runtime, "unified_memory", None),
            character_id=character["id"],
        )

    async def stage_video_for_review(self, args: StageVideoToolArgs):
        return await self.stage_or_publish_bilibili_video(
            StageOrPublishVideoArgs(job_id=args.job_id, publish_mode="preview_review")
        )

    def status(self) -> dict[str, Any]:
        try:
            policy, counts = self.policy(), {row["status"]: int(row["n"]) for row in self.store.all("SELECT status, COUNT(*) AS n FROM remotion_video_jobs GROUP BY status")}
        except Exception:
            policy, counts = VideoProductionPolicy(), {}
        return {"publish_mode": policy.publish_mode, "auto_publish_enabled": policy.auto_publish_enabled, "jobs": counts}


async def plan_video_script(skill: RemotionVideoSkill, arguments: PlanVideoScriptArgs | dict[str, Any]) -> dict[str, Any]: return await skill.plan_video_script(arguments)
async def generate_remotion_project(skill: RemotionVideoSkill, arguments: GenerateRemotionProjectArgs | dict[str, Any]) -> dict[str, Any]: return await skill.generate_remotion_project(arguments)
async def render_remotion_video(skill: RemotionVideoSkill, arguments: RenderRemotionVideoArgs | dict[str, Any]) -> dict[str, Any]: return await skill.render_remotion_video(arguments)
async def stage_or_publish_bilibili_video(skill: RemotionVideoSkill, arguments: StageOrPublishVideoArgs | dict[str, Any]) -> dict[str, Any]: return await skill.stage_or_publish_bilibili_video(arguments)
def pitch_video_ideas(skill: RemotionVideoSkill, *, brief_hint: str = "", max_ideas: int = 5) -> dict[str, Any]: return skill.pitch_video_ideas(brief_hint=brief_hint, max_ideas=max_ideas)
async def capture_job_ui_screenshots(skill: RemotionVideoSkill, job_id: str, *, base_url: str = "http://127.0.0.1:9020", routes: Sequence[str] | None = None, max_captures: int = 6) -> dict[str, Any]: return await skill.capture_job_ui_screenshots(job_id, base_url=base_url, routes=routes, max_captures=max_captures)
async def review_job_visuals(skill: RemotionVideoSkill, job_id: str) -> dict[str, Any]: return await skill.review_job_visuals(job_id)

_REMOTION_TOOL_SPECS: tuple[tuple[str, str, type[StrictModel], str], ...] = (
    ("plan_video_script", "基于公开视频或话题规划结构化 Remotion 脚本与分镜 JSON。", PlanVideoScriptArgs, "plan_video_script"),
    ("generate_remotion_project", "生成配音 WAV、字幕 SRT、时间轴 Manifest 与 Remotion TSX 工程。", GenerateRemotionProjectArgs, "generate_remotion_project"),
    ("render_remotion_video", "受并发保护地渲染 Remotion 视频并执行 FFprobe 音画质检。", RenderRemotionVideoArgs, "render_remotion_video"),
    ("stage_or_publish_bilibili_video", "将质检通过的视频放入审核队列。", StageVideoToolArgs, "stage_video_for_review"),
)


def remotion_tool_schemas(*, format: Literal["chat", "responses"] = "responses") -> list[dict[str, Any]]:
    return [
        (spec if format == "responses" else {"type": "function", "function": {k: spec[k] for k in ("name", "description", "parameters", "strict")}})
        for name, desc, model_cls, _m in _REMOTION_TOOL_SPECS
        for spec in (tool_function(name, desc, model_cls, require_all=False),)
    ]


async def execute_remotion_tool(skill: RemotionVideoSkill, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        return {"ok": False, "error": "工具参数须为 JSON 对象"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "工具参数须为 JSON 对象"}
    if (entry := {n: (m, fn) for n, _d, m, fn in _REMOTION_TOOL_SPECS}.get(name)) is None:
        return {"ok": False, "error": f"未知视频工具：{name}"}
    try:
        return await getattr(skill, entry[1])(entry[0].model_validate(parsed))
    except (ValidationError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def register_remotion_tools(tasks, *, runtime=None, store=None, browsing=None, skill: RemotionVideoSkill | None = None) -> RemotionVideoSkill:
    if (target_skill := skill or getattr(runtime, "remotion_skill", None)) is None:
        target_skill = RemotionVideoSkill(runtime, store or tasks.store, browsing=browsing)
        if runtime is not None:
            runtime.remotion_skill = target_skill
    for tool_name, description, model_cls, method_name in _REMOTION_TOOL_SPECS:
        if tool_name not in tasks.tools:
            async def _runner(_req, args, _fn=getattr(target_skill, method_name)):
                return await _fn(args)
            tasks.register(tool_name, tasks.Tool(description, model_cls, _runner, own_deadline=True, responses_only=True))
    return target_skill
