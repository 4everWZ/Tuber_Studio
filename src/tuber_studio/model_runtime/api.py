"""Business-side inference and voice clients; the service owns model management."""

import asyncio
import base64
import binascii

import httpx
from fastapi import Body, Depends, HTTPException

from ..provider_diagnostics import register_provider_diagnostics
from ..provider_settings import ProviderConfigurationError, apply_settings
from ..providers import public_error


def register_model_api(app, settings, runtime, require_admin):
    @app.get("/api/voices", dependencies=[Depends(require_admin)])
    async def voices():
        return settings.voices()

    @app.post("/api/tts/preview", dependencies=[Depends(require_admin)])
    async def preview(data: dict = Body(...)):
        text = data.get("text", "")
        if runtime.live.busy:
            raise ValueError("直播生成／播放繁忙，请先暂停并等待当前任务结束")
        async with runtime.generation_lock:
            try:
                voice = settings.voice(data.get("voice_id", settings.data["voice_id"]))
                artifact = await runtime.voices.preview(text, voice)
            except httpx.HTTPError as error:
                raise HTTPException(502, public_error(error)) from None
        return {
            "audio_ref": artifact.ref,
            "duration": artifact.duration,
            "text": artifact.text,
            "warning": artifact.warning,
        }

    @app.put("/api/settings/providers", dependencies=[Depends(require_admin)])
    async def provider_settings(data: dict = Body(...)):
        try:
            return await apply_settings(runtime, data)
        except ProviderConfigurationError as error:
            raise HTTPException(409, str(error)) from None
        except RuntimeError as error:
            raise HTTPException(409, public_error(error)) from None
        except httpx.HTTPError as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else 503
            raise HTTPException(status if status in {409, 422} else 503, public_error(error)) from None

    @app.post("/api/vision/describe", dependencies=[Depends(require_admin)])
    async def vision_description(data: dict = Body(...)):
        if set(data) != {"prompt", "image_base64", "mime_type"} or not isinstance(data["image_base64"], str):
            raise ValueError("视觉接口只接受prompt、image_base64和mime_type，不接受路径或URL")
        try:
            image = base64.b64decode(data["image_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片base64无效") from None
        async with runtime.generation_lock:
            try:
                description = await runtime.vision.describe(data["prompt"], image, data["mime_type"])
            except httpx.HTTPError as error:
                raise HTTPException(502, public_error(error)) from None
        return {
            "description": description,
            "model": settings.data["omni"]["model"],
            "context_installed": False,
        }

    @app.post("/api/providers/check", dependencies=[Depends(require_admin)])
    async def check():
        async def one(provider):
            try:
                return await provider.health()
            except Exception as error:
                return {"available": False, "error": public_error(error)}

        llm, tts, vision = await asyncio.gather(one(runtime.llm), one(runtime.voices), one(runtime.vision))
        return {"llm": llm, "tts": tts, "vision": vision}

    register_provider_diagnostics(app, settings, require_admin)
