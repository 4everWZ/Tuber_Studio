"""L4 video production routes (`/api/v1/operations/videos/*`): Remotion Code-as-Video pipeline."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import Field

from ...agent.skills.remotion_video import (
    DurationPreset,
    GenerateRemotionProjectArgs,
    PlanVideoScriptArgs,
    RenderRemotionVideoArgs,
    StageOrPublishVideoArgs,
    VideoMode,
    VideoProductionPolicy,
)
from ...core.models import StrictModel
from ...operations.facade import select_video_candidates
from .system import ERROR_RESPONSES

__all__ = ["register_video_production_routes"]

MediaKind = Literal["video", "audio", "subtitles", "subtitles_vtt", "cover", "contact_sheet", "props", "manifest", "code"]


class VideoPlanPatch(StrictModel):
    """Editable fields on a planned VideoScriptPlan prior to publishing."""

    title: str | None = Field(default=None, min_length=1, max_length=80)
    summary: str | None = Field(default=None, min_length=1, max_length=1000)
    tags: list[str] | None = Field(default=None, min_length=1, max_length=10)
    category_tid: int | None = Field(default=None, ge=1, le=9999)
    video_mode: VideoMode | None = None
    duration_preset: DurationPreset | None = None
    fps: Literal[30, 60] | None = None
    width: Literal[1920, 1280] | None = None
    height: Literal[1080, 720] | None = None
    include_avatar: bool | None = None
    bgm_enabled: bool | None = None
    line_pause_seconds: float | None = Field(default=None, ge=0.0, le=2.0)
    scene_pause_seconds: float | None = Field(default=None, ge=0.0, le=3.0)
    scenes: list[dict[str, Any]] | None = None
    sources: list[dict[str, Any]] | None = None


class VideoUploadBody(StrictModel):
    """HTTP payload for uploading up to 12 public images/screenshots or MP4/WebM clips for a video job."""

    filename: str = Field(min_length=1, max_length=80)
    image_base64: str = Field(min_length=1, max_length=45_000_000)
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm"]


class VideoPitchIdeasBody(StrictModel):
    """HTTP payload for pitching video vs dynamic content ideas."""

    brief_hint: str = Field(default="", max_length=2000)
    max_ideas: int = Field(default=5, ge=1, le=10)


class VideoCaptureUiBody(StrictModel):
    """HTTP payload for capturing local Tuber Studio workbench screenshots into a video job."""

    base_url: str = Field(default="http://127.0.0.1:9020", max_length=200)
    routes: list[str] | None = Field(default=None, max_length=12)
    max_captures: int = Field(default=6, ge=1, le=12)


class VideoGenerateBody(StrictModel):
    """HTTP payload for generating TTS audio, manifest, and Remotion project."""

    tts_mode: Literal["omni_speech", "dry_run"] = "omni_speech"
    reference_voice_id: str | None = Field(default=None, max_length=80)
    plan: dict[str, Any] | None = None


class VideoRenderBody(StrictModel):
    """HTTP payload for triggering Remotion render + FFprobe QC."""

    composition_id: str = Field(default="AquaVideo", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")
    concurrency: int = Field(default=2, ge=1, le=4)
    dry_run: bool = False
    background: bool = True


class VideoPublishBody(StrictModel):
    """HTTP payload for staging in preview_review or explicitly approved publish."""

    publish_mode: Literal["preview_review", "auto_publish"] | None = None
    approved: bool = False


class VideoPolicyPatch(StrictModel):
    """Partial update to VideoProductionPolicy."""

    publish_mode: Literal["preview_review", "auto_publish"] | None = None
    auto_publish_enabled: bool | None = None
    default_fps: Literal[30, 60] | None = None
    default_width: Literal[1920, 1280] | None = None
    default_height: Literal[1080, 720] | None = None
    default_include_avatar: bool | None = None
    default_bgm_enabled: bool | None = None
    default_line_pause_seconds: float | None = Field(default=None, ge=0.0, le=2.0)
    default_scene_pause_seconds: float | None = Field(default=None, ge=0.0, le=3.0)
    max_render_concurrency: int | None = Field(default=None, ge=1, le=4)
    render_timeout_seconds: float | None = Field(default=None, ge=5.0, le=3600.0)
    upload_timeout_seconds: float | None = Field(default=None, ge=5.0, le=3600.0)


def _media_urls(skill, job: dict[str, Any]) -> dict[str, str]:
    job_id = str(job["id"])
    base = f"/api/v1/operations/videos/{job_id}/media"
    urls: dict[str, str] = {}
    for kind in ("video", "audio", "subtitles", "subtitles_vtt", "cover", "contact_sheet", "props", "manifest", "code"):
        try:
            skill.resolve_job_media(job_id, kind)
        except (ValueError, FileNotFoundError):
            continue
        urls[kind] = f"{base}/{kind}"
    return urls


def register_video_production_routes(app, runtime, store, browsing, require_admin) -> None:
    """Register `/api/v1/operations/videos/*` routes onto FastAPI."""
    deps = [Depends(require_admin)]
    prefix = "/api/v1/operations/videos"
    active_renders: dict[str, asyncio.Task] = {}
    app.state.active_video_renders = active_renders

    def _skill():
        return runtime.remotion_skill

    def _present_job(job: dict[str, Any]) -> dict[str, Any]:
        skill = _skill()
        job_id = str(job["id"])
        running = job_id in active_renders and not active_renders[job_id].done()
        # Strip raw local paths from API view while keeping relative media URLs.
        sanitized = {k: v for k, v in job.items() if k not in {"project_dir", "output_path", "cover_path"}}
        return sanitized | {
            "render_active": running,
            "media": _media_urls(skill, job),
            "uploads": skill.list_job_uploads(job),
        }

    @app.get(prefix, dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def list_video_jobs(
        limit: int = Query(30, ge=1, le=100),
        status: str | None = Query(None, max_length=40),
    ):
        skill = _skill()
        jobs = [_present_job(job) for job in skill.list_jobs(limit=limit, status=status)]
        candidates = select_video_candidates(browsing, store=store, limit=12, query=None)
        return {
            "jobs": jobs,
            "policy": skill.policy().model_dump(),
            "environment": skill.environment_status(),
            "candidates": candidates,
        }

    @app.post(prefix + "/pitch-ideas", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def pitch_ideas(body: VideoPitchIdeasBody = VideoPitchIdeasBody()):
        try:
            return _skill().pitch_video_ideas(brief_hint=body.brief_hint, max_ideas=body.max_ideas)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get(prefix + "/{job_id}", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def get_video_job(job_id: str):
        skill = _skill()
        job = skill.get_job(job_id)
        if job is None:
            raise HTTPException(404, "找不到指定视频任务")
        return {
            "job": _present_job(job),
            "policy": skill.policy().model_dump(),
            "environment": skill.environment_status(),
        }

    @app.put(prefix + "/policy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def update_video_policy(body: VideoPolicyPatch):
        patch = body.model_dump(exclude_none=True)
        if not patch:
            return {"policy": _skill().policy().model_dump()}
        try:
            updated: VideoProductionPolicy = _skill().configure_policy(patch)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"policy": updated.model_dump()}

    @app.post(prefix + "/plan", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def plan_video(body: PlanVideoScriptArgs):
        skill = _skill()
        try:
            result = await skill.plan_video_script(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        job = skill.get_job(result["job_id"])
        return result | ({"job": _present_job(job)} if job else {})

    @app.patch(prefix + "/{job_id}/plan", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def edit_video_plan(job_id: str, body: VideoPlanPatch):
        skill = _skill()
        patch = body.model_dump(exclude_none=True)
        try:
            result = skill.update_job_plan(job_id, patch)
        except ValueError as exc:
            status_code = 404 if "找不到" in str(exc) else 409 if "不允许" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from None
        return {
            "ok": True,
            "structural_changed": result["structural_changed"],
            "job": _present_job(result["job"]),
        }

    @app.post(prefix + "/{job_id}/generate", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def generate_video_project(job_id: str, body: VideoGenerateBody = VideoGenerateBody()):
        skill = _skill()
        if skill.get_job(job_id) is None:
            raise HTTPException(404, "找不到指定视频任务")
        try:
            result = await skill.generate_remotion_project(
                GenerateRemotionProjectArgs(
                    job_id=job_id,
                    plan=body.plan,
                    tts_mode=body.tts_mode,
                    reference_voice_id=body.reference_voice_id,
                )
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        job = skill.get_job(job_id)
        return {
            "ok": result["ok"],
            "job_id": job_id,
            "status": result["status"],
            "total_frames": result["total_frames"],
            "total_duration_seconds": result["total_duration_seconds"],
            "project_hash": result["project_hash"],
            "job": _present_job(job) if job else None,
        }

    @app.post(prefix + "/{job_id}/render", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def render_video(job_id: str, body: VideoRenderBody = VideoRenderBody()):
        skill = _skill()
        job = skill.get_job(job_id)
        if job is None:
            raise HTTPException(404, "找不到指定视频任务")
        if not job.get("manifest"):
            raise HTTPException(409, "请先生成配音与 Remotion 工程后再渲染")
        if job_id in active_renders and not active_renders[job_id].done():
            raise HTTPException(409, "该视频任务正在渲染中，请稍候")

        render_args = RenderRemotionVideoArgs(
            job_id=job_id,
            composition_id=body.composition_id,
            concurrency=body.concurrency,
            dry_run=body.dry_run,
        )
        if not body.background:
            try:
                result = await skill.render_remotion_video(render_args)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            updated = skill.get_job(job_id)
            return {
                "ok": result["ok"],
                "job_id": job_id,
                "status": result["status"],
                "qc_report": result.get("qc_report"),
                "job": _present_job(updated) if updated else None,
            }

        skill._update_job(job_id, status="rendering", error=None)

        async def _run_render() -> None:
            try:
                await skill.render_remotion_video(render_args)
            except Exception as exc:
                current = skill.get_job(job_id)
                if current and current.get("status") == "rendering":
                    skill._update_job(job_id, status="render_failed", error=str(exc)[:240])
            finally:
                active_renders.pop(job_id, None)

        task = asyncio.create_task(_run_render())
        active_renders[job_id] = task
        fresh = skill.get_job(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "status": "rendering",
            "queued": True,
            "job": _present_job(fresh) if fresh else None,
        }

    @app.post(prefix + "/{job_id}/publish", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def publish_video(job_id: str, body: VideoPublishBody = VideoPublishBody()):
        skill = _skill()
        if skill.get_job(job_id) is None:
            raise HTTPException(404, "找不到指定视频任务")
        try:
            result = await skill.stage_or_publish_bilibili_video(
                StageOrPublishVideoArgs(
                    job_id=job_id,
                    publish_mode=body.publish_mode,
                    approved=body.approved,
                )
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        fresh = skill.get_job(job_id)
        return result | ({"job": _present_job(fresh)} if fresh else {})

    @app.delete(prefix + "/{job_id}", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def delete_video_job(job_id: str):
        if job_id in active_renders and not active_renders[job_id].done():
            raise HTTPException(409, "渲染进行中，暂不可删除")
        try:
            return _skill().delete_job(job_id)
        except ValueError as exc:
            status_code = 404 if "找不到" in str(exc) else 409
            raise HTTPException(status_code, str(exc)) from None

    @app.get(prefix + "/{job_id}/media/{kind}", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def get_video_media(job_id: str, kind: MediaKind):
        try:
            path, media_type = _skill().resolve_job_media(job_id, kind)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})

    @app.post(prefix + "/{job_id}/uploads", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def upload_video_image(job_id: str, body: VideoUploadBody):
        skill = _skill()
        try:
            result = skill.upload_job_image(
                job_id,
                filename=body.filename,
                image_base64=body.image_base64,
                mime_type=body.mime_type,
            )
        except ValueError as exc:
            msg = str(exc)
            status_code = 404 if "找不到" in msg else 409 if "不允许" in msg else 400
            raise HTTPException(status_code, msg) from None
        fresh = skill.get_job(job_id)
        return result | ({"job": _present_job(fresh)} if fresh else {})

    @app.get(prefix + "/{job_id}/uploads/{filename}", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def get_video_upload(job_id: str, filename: str):
        try:
            path, media_type = _skill().resolve_job_upload(job_id, filename)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})

    @app.post(prefix + "/{job_id}/capture-ui", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def capture_job_ui(job_id: str, body: VideoCaptureUiBody = VideoCaptureUiBody()):
        skill = _skill()
        try:
            result = await skill.capture_job_ui_screenshots(
                job_id,
                base_url=body.base_url,
                routes=body.routes,
                max_captures=body.max_captures,
            )
        except ValueError as exc:
            msg = str(exc)
            status_code = 404 if "找不到" in msg else 409 if "不允许" in msg else 400
            raise HTTPException(status_code, msg) from None
        fresh = skill.get_job(job_id)
        return result | ({"job": _present_job(fresh)} if fresh else {})

    @app.post(prefix + "/{job_id}/visual-review", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def review_job_visuals(job_id: str):
        skill = _skill()
        try:
            return await skill.review_job_visuals(job_id)
        except ValueError as exc:
            status_code = 404 if "找不到" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from None
