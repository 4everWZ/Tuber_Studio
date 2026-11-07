"""Business-side model, voice, and inference node routes."""

import asyncio
import base64
import binascii
from typing import Literal

import httpx
from fastapi import Body, Depends, HTTPException

from ..inference_node_client import pair_node, refresh_capabilities
from ..inference_node_client import status as inference_node_status
from ..inference_profiles import DAFEIYU_KEY, GROUNDED_DAFEIYU_KEY, LLM_MODELS, VISION_ROUTES
from ..provider_diagnostics import register_provider_diagnostics
from ..provider_settings import ProviderConfigurationError, apply_settings
from ..providers import public_error
from ..video_vision_service import probe_visual


def register_model_api(app, settings, runtime, require_admin):
    @app.post("/api/inference/mode", dependencies=[Depends(require_admin)])
    async def work_mode(mode: Literal["text", "live"] = Body(embed=True)):
        return await runtime.inference.set_mode(mode)

    @app.get("/api/inference/node", dependencies=[Depends(require_admin)])
    async def inference_node():
        return inference_node_status(runtime)

    @app.post("/api/inference/node/check", dependencies=[Depends(require_admin)])
    async def check_inference_node():
        await refresh_capabilities(runtime)
        return inference_node_status(runtime)

    @app.put("/api/inference/node", dependencies=[Depends(require_admin)])
    async def configure_inference_node(data: dict = Body(...)):
        return await pair_node(runtime, data)

    @app.post("/api/inference/node/vision-probe", dependencies=[Depends(require_admin)])
    async def probe_inference_vision():
        return await probe_visual(runtime)

    @app.post("/api/inference/node/idle", dependencies=[Depends(require_admin)])
    async def release_inference_cache():
        if not runtime.inference.client:
            raise ValueError("尚未配置推理节点")
        if (
            runtime.live.state != "stopped"
            or runtime.generation_lock.locked()
            or runtime.inference.mode_lock.locked()
        ):
            raise ValueError("请先停止直播并等待当前任务结束")
        if runtime.inference.status.get("mode") != "text":
            raise ValueError("请先切到运营模式")
        async with runtime.inference.mode_lock, runtime.generation_lock:
            response = await runtime.inference.client.post("/idle", json={})
            response.raise_for_status()
            runtime.inference.status = response.json()
            cleared = await runtime.inference.client.post("/cache/clear", json={})
            cleared.raise_for_status()
            result = cleared.json()
        return {"cache_result": result, "capabilities": await refresh_capabilities(runtime)}

    @app.get("/api/voices", dependencies=[Depends(require_admin)])
    async def voices():
        return settings.voices()

    @app.post("/api/tts/preview", dependencies=[Depends(require_admin)])
    async def preview(data: dict = Body(...)):
        text = data.get("text", "")
        if not isinstance(text, str) or not 1 <= len(text) <= 120:
            raise ValueError("试听台词为1到120字")
        if runtime.live.busy:
            raise ValueError("直播生成／播放繁忙，请先暂停并等待当前任务结束")
        async with runtime.generation_lock:
            try:
                artifact = await runtime.voices.synthesize(text, settings.voice(data.get("voice_id")))
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
            "model": settings.data["vision"].get("model"),
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

    @app.get("/api/providers/local-models", dependencies=[Depends(require_admin)])
    async def local_models():
        """Known node profiles; installation comes from the observed node inventory."""
        capabilities = runtime.inference.capabilities or {}
        profiles = {} if capabilities.get("monitoring_error") else capabilities.get("profiles", {})
        result = {"fine_tuned": [], "base": []}
        for key, model in LLM_MODELS.items():
            if key == DAFEIYU_KEY:
                continue
            fine_tuned = key == GROUNDED_DAFEIYU_KEY
            result["fine_tuned" if fine_tuned else "base"].append(
                {
                    "id": model,
                    "name": f"{model} (大肥鱼 DoRA)" if fine_tuned else model,
                    "installed": profiles.get(key, {}).get("installed"),
                    "multimodal": key in VISION_ROUTES.values(),
                    "tag": "微调人设" if fine_tuned else "通用模型",
                }
            )
        return result

    register_provider_diagnostics(app, settings, require_admin)
