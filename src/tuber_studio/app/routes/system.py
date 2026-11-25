"""L4 system routes (`/api/v1/system/*`) and admin status WebSocket (`/ws/admin`)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
import time
from typing import Any

import httpx
from fastapi import Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ...core.auth import COOKIE_NAME, EnvironmentManagedToken, InvalidAdminCredential
from ...core.models import StrictModel
from ...infra.inference.diagnostics import check_provider_diagnostics, saved_provider_models
from ...infra.inference.providers import public_error
from ...infra.inference.settings import ProviderConfigurationError, apply_settings
from ...infra.inference.validation import validate_image
from ...infra.media.cache import CachePolicy
from ...persona.characters.performance import Performance, performance, save_performance
from ..errors import ErrorEnvelope

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid request parameters"},
    401: {"model": ErrorEnvelope, "description": "Authentication required"},
    403: {"model": ErrorEnvelope, "description": "Cross-origin write forbidden"},
    409: {"model": ErrorEnvelope, "description": "Conflict with active generation or environment"},
    422: {"model": ErrorEnvelope, "description": "Request validation failed"},
    429: {"model": ErrorEnvelope, "description": "Rate limited"},
    502: {"model": ErrorEnvelope, "description": "Upstream inference service error"},
    503: {"model": ErrorEnvelope, "description": "Upstream service unavailable"},
}


class AuthStatusResponse(StrictModel):
    authenticated: bool
    scope: str | None = None


class TokenResetResponse(StrictModel):
    token: str
    authenticated: bool


def register_system_routes(app, settings, runtime, comments, dynamics, auth, login_attempts: dict | None = None) -> None:
    """Register `/api/v1/system/*` REST routes and `WS /ws/admin`."""
    attempts_by_ip = login_attempts if login_attempts is not None else {}
    admin_dep = [Depends(auth.require_admin)]
    prefix = "/api/v1/system"

    @app.post(
        prefix + "/auth/login",
        response_model=AuthStatusResponse,
        response_model_exclude_none=True,
        responses=ERROR_RESPONSES,
        tags=["system"],
    )
    async def login(request: Request, data: dict = Body(...)):
        ip = request.client.host if request.client else "local"
        attempts = [t for t in attempts_by_ip.get(ip, []) if time.time() - t < 60]
        if len(attempts) >= 10:
            raise HTTPException(429, "登录尝试过多，请稍后再试")
        token = data.get("token", "")
        try:
            session = auth.sessions.issue(token, request.cookies.get(COOKIE_NAME))
        except InvalidAdminCredential:
            attempts_by_ip[ip] = attempts + [time.time()]
            raise HTTPException(401, "管理令牌无效")
        attempts_by_ip.pop(ip, None)
        response = JSONResponse({"authenticated": True})
        auth.admin_cookie(response, request, session)
        return response

    @app.post(
        prefix + "/auth/logout",
        dependencies=admin_dep,
        response_model=AuthStatusResponse,
        response_model_exclude_none=True,
        responses=ERROR_RESPONSES,
        tags=["system"],
    )
    async def logout(request: Request):
        auth.sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"authenticated": False})
        auth.clear_admin_cookie(response, request)
        return response

    @app.post(
        prefix + "/auth/reset-token",
        dependencies=admin_dep,
        response_model=TokenResetResponse,
        responses=ERROR_RESPONSES,
        tags=["system"],
    )
    async def reset_admin_token(request: Request):
        authorization = request.headers.get("authorization", "")
        try:
            token = auth.sessions.reset_token(
                cookie=request.cookies.get(COOKIE_NAME),
                bearer=authorization[7:] if authorization.startswith("Bearer ") else None,
            )
        except InvalidAdminCredential as error:
            raise HTTPException(401, str(error)) from None
        except EnvironmentManagedToken as error:
            raise HTTPException(409, str(error)) from None
        response = JSONResponse({"token": token, "authenticated": False})
        auth.clear_admin_cookie(response, request)
        return response

    @app.post(
        prefix + "/auth/overlay",
        response_model=AuthStatusResponse,
        responses=ERROR_RESPONSES,
        tags=["system"],
    )
    async def overlay_auth(request: Request, data: dict = Body(...)):
        token = data.get("token", "")
        if not isinstance(token, str) or not secrets.compare_digest(token, settings.overlay_token):
            raise HTTPException(401, "播放器令牌无效")
        response = JSONResponse({"authenticated": True, "scope": "playback_only"})
        response.set_cookie(
            "tuber_overlay",
            settings.overlay_token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.get(prefix + "/status", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def status():
        return runtime.status() | {"comment_policy": comments.policy(), "dynamic_policy": dynamics.policy()}

    @app.get(prefix + "/settings", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def config(request: Request):
        return settings.public_data() | {
            "local_access": auth.local_access(request),
            "config_path": str(settings.path),
            "overlay_url": str(request.base_url).rstrip("/") + "/overlay#token=" + settings.overlay_token,
            "platform": "Mock；B站评论已接入，B站／抖音直播后置",
            "changes": "模型／API／上下文设置可在此保存；直播后置，默认审核与自动关闭",
        }

    @app.get(prefix + "/retention", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def retention_policy():
        return runtime.retention.policy() | {"error": runtime.retention_error}

    @app.put(prefix + "/retention", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def retention_settings(data: dict = Body(...)):
        if set(data) != {"days"}:
            raise ValueError("仅接受days；null表示不自动清理")
        return runtime.retention.set_policy(data["days"])

    def retention_arguments(data: dict, *, cleanup: bool = False):
        if set(data) - ({"before", "confirm"} if cleanup else {"before"}):
            raise ValueError("未知清理参数")
        if cleanup and data.get("confirm") is not True:
            raise ValueError("清理不可恢复，请先预览并明确确认")
        return data.get("before")

    @app.post(prefix + "/retention/preview", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def retention_preview(data: dict = Body(...)):
        return runtime.retention.preview(retention_arguments(data))

    @app.post(prefix + "/retention/cleanup", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def retention_cleanup(data: dict = Body(...)):
        before = retention_arguments(data, cleanup=True)
        if runtime.live.busy or runtime.generation_active():
            raise HTTPException(409, "正在生成或播放，请暂停并等待当前任务结束后清理")
        return await runtime.cleanup_retention(before)

    @app.put(prefix + "/settings/providers", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def provider_settings(data: dict = Body(...)):
        try:
            return await apply_settings(runtime, data)
        except ProviderConfigurationError as error:
            raise HTTPException(409, str(error)) from None
        except RuntimeError as error:
            raise HTTPException(409, public_error(error)) from None
        except httpx.HTTPError as error:
            http_status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else 503
            raise HTTPException(http_status if http_status in {409, 422} else 503, public_error(error)) from None

    @app.post(prefix + "/vision/describe", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def vision_description(data: dict = Body(...)):
        if set(data) != {"prompt", "image_base64", "mime_type"} or not isinstance(data["image_base64"], str):
            raise ValueError("视觉接口只接受prompt、image_base64和mime_type，不接受路径或URL")
        try:
            image = base64.b64decode(data["image_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片base64无效") from None
        prompt, mime_type = data["prompt"], data["mime_type"]
        if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 8000:
            raise ValueError("图片问题须为1到8000字")
        validate_image(image, mime_type)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data['image_base64']}"}},
                ],
            }
        ]
        schema = {
            "type": "object",
            "properties": {"description": {"type": "string", "minLength": 1}},
            "required": ["description"],
            "additionalProperties": False,
        }
        async with runtime.offline_generation():
            try:
                result = await runtime.operation_llm.generate_json(messages, schema, max_tokens=1024)
            except httpx.HTTPError as error:
                raise HTTPException(502, public_error(error)) from None
        return {
            "description": result["description"],
            "model": settings.responses_config["model"],
            "context_installed": False,
        }

    @app.post(prefix + "/providers/check", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def check():
        return await check_provider_diagnostics(settings, runtime)

    @app.get(prefix + "/providers/models", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def saved_models():
        return await saved_provider_models(settings)

    @app.get(prefix + "/settings/performance", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def read_performance():
        return performance(runtime.store)

    @app.put(prefix + "/settings/performance", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def update_performance(body: Performance):
        return save_performance(runtime.store, body)

    @app.get(prefix + "/cache", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def cache_status():
        return runtime.cache.status()

    @app.put(prefix + "/cache", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def cache_policy(data: CachePolicy):
        return await runtime.cache.configure(data.model_dump(exclude_unset=True))

    @app.post(prefix + "/cache/clear-all", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def clear_all_cache():
        return {"result": await runtime.cache.clear_all(), **runtime.cache.status()}

    @app.post(prefix + "/cache/{category}/clear", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["system"])
    async def clear_cache(category: str):
        return {"result": await runtime.cache.clear(category), **runtime.cache.status()}

    @app.websocket("/ws/admin")
    async def admin_ws(websocket: WebSocket):
        if not auth.same_origin(websocket) or not auth.admin_ok(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while auth.admin_ok(websocket):
                await websocket.send_json({"type": "status", "data": runtime.status()})
                await asyncio.sleep(1)
            await websocket.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
