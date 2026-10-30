"""Authenticated local management API and separately scoped OBS playback channel."""

import asyncio
import base64
import binascii
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .comments import Comments
from .config import Settings
from .direct_messages import DirectMessages
from .dynamics import Dynamics
from .live_api import register_live_api
from .live_media import MAX_UPLOAD
from .message_api import register_message_api
from .models import Character, Event
from .provider_settings import ProviderConfigurationError, apply_settings
from .providers import public_error
from .runtime import Runtime
from .store import Store


def create_app(settings=None, adapter=None, *, singing_provider=None, message_adapter=None):
    settings = settings or Settings()
    seeds = json.loads((settings.root / "config/characters.json").read_text(encoding="utf-8"))
    store = Store(settings.runtime / "studio.sqlite3", seeds)
    runtime = Runtime(settings, store, singing_provider=singing_provider)
    comments = Comments(settings, store, runtime, adapter)
    dynamics = Dynamics(settings, store, runtime, comments)
    messages = DirectMessages(settings, store, runtime, comments, message_adapter)
    comments.direct_messages = messages
    sessions = {}
    login_attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.open()
            await comments.open()
            await dynamics.open()
            await messages.open()
            yield
        finally:
            try:
                await messages.close()
            finally:
                try:
                    await dynamics.close()
                finally:
                    try:
                        await comments.close()
                    finally:
                        try:
                            await runtime.close()
                        finally:
                            store.close()

    app = FastAPI(title="Tuber Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime, app.state.comments, app.state.store = runtime, comments, store
    app.state.dynamics = dynamics
    app.state.messages = messages

    def valid_session(cookie):
        return bool(cookie and sessions.get(cookie, 0) > time.time())

    def admin_ok(connection):
        bearer = connection.headers.get("authorization", "")
        return valid_session(connection.cookies.get("tuber_admin")) or (
            bearer.startswith("Bearer ") and secrets.compare_digest(bearer[7:], settings.admin_token)
        )

    def overlay_ok(connection):
        return secrets.compare_digest(connection.cookies.get("tuber_overlay", ""), settings.overlay_token)

    def same_origin(connection):
        origin = connection.headers.get("origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == connection.headers.get("host")

    async def require_admin(request: Request):
        if not admin_ok(request):
            raise HTTPException(401, "请使用本机管理令牌登录")

    @app.middleware("http")
    async def boundaries(request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not same_origin(request):
                return JSONResponse({"detail": "跨站写入被拒绝"}, status_code=403)
            uploading = request.url.path == "/api/live/uploads" and request.method == "POST"
            if int(request.headers.get("content-length", "0")) > (MAX_UPLOAD if uploading else 2_100_000):
                return JSONResponse({"detail": "请求过大"}, status_code=413)
            if not uploading:
                body = await request.body()
                if len(body) > 2_100_000:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": public_error(error)}, status_code=400)

    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")
    app.mount("/assets", StaticFiles(directory=settings.root / "assets"), name="assets")
    register_live_api(app, settings, runtime, require_admin, admin_ok, overlay_ok)
    register_message_api(app, messages, require_admin)

    @app.get("/")
    async def index():
        return FileResponse(web / "index.html")

    @app.get("/overlay")
    async def overlay():
        return FileResponse(web / "overlay.html")

    @app.post("/api/auth")
    async def login(request: Request, data: dict = Body(...)):
        ip = request.client.host if request.client else "local"
        attempts = [t for t in login_attempts.get(ip, []) if time.time() - t < 60]
        if len(attempts) >= 10:
            raise HTTPException(429, "登录尝试过多，请稍后再试")
        token = data.get("token", "")
        if not isinstance(token, str) or not secrets.compare_digest(token, settings.admin_token):
            login_attempts[ip] = attempts + [time.time()]
            raise HTTPException(401, "管理令牌无效")
        login_attempts.pop(ip, None)
        session = secrets.token_urlsafe(32)
        sessions.update({k: v for k, v in list(sessions.items()) if v > time.time()})
        if len(sessions) >= 32:
            sessions.pop(next(iter(sessions)))
        sessions[session] = time.time() + 12 * 3600
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            "tuber_admin",
            session,
            httponly=True,
            samesite="strict",
            max_age=43200,
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/api/logout", dependencies=[Depends(require_admin)])
    async def logout(request: Request):
        sessions.pop(request.cookies.get("tuber_admin"), None)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("tuber_admin")
        return response

    @app.post("/api/overlay/auth")
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

    @app.get("/media/audio/{name}")
    async def audio(name: str, request: Request):
        if not (admin_ok(request) or overlay_ok(request)):
            raise HTTPException(401, "音频读取需要播放或管理凭证")
        path = (settings.audio / name).resolve()
        if path.parent != settings.audio or path.suffix != ".wav" or not path.is_file():
            raise HTTPException(404, "音频不存在")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/status", dependencies=[Depends(require_admin)])
    async def status():
        return runtime.status() | {"comment_policy": comments.policy(), "dynamic_policy": dynamics.policy()}

    @app.post("/api/inference/mode", dependencies=[Depends(require_admin)])
    async def work_mode(mode: Literal["text", "live"] = Body(embed=True)):
        return await runtime.set_work_mode(mode)

    @app.post("/api/live/mock-event", dependencies=[Depends(require_admin)])
    async def mock_event(event: Event):
        return await runtime.submit(event)

    @app.post("/api/live/{action}", dependencies=[Depends(require_admin)])
    async def control(action: str):
        return await runtime.control(action)

    @app.get("/api/events", dependencies=[Depends(require_admin)])
    async def events(limit: int = Query(50, ge=1, le=200)):
        rows = store.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    @app.get("/api/jobs", dependencies=[Depends(require_admin)])
    async def jobs(limit: int = Query(50, ge=1, le=200)):
        rows = store.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        for row in rows:
            row.pop("playback_token", None)
        return rows

    @app.get("/api/characters", dependencies=[Depends(require_admin)])
    async def characters():
        return store.characters()

    @app.get("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def character(character_id: str):
        return store.character(character_id)

    @app.get("/api/characters/{character_id}/versions", dependencies=[Depends(require_admin)])
    async def versions(character_id: str):
        return store.versions(character_id)

    @app.put("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def save_character(character_id: str, data: Character):
        if data.id != character_id:
            raise ValueError("角色ID不可修改，请复制新建OC")
        settings.voice(data.voice_profile)
        if not settings.assets(data.model_dump()):
            raise ValueError("素材清单无效或为空")
        active = runtime.character["id"] == character_id
        if active:
            runtime.clear_queue("角色配置更新")
            await runtime.cancel_current("角色配置更新")
        result = store.save_character(data.model_dump())
        if active:
            await runtime.activate(character_id, result["version"])
        return result

    @app.post("/api/characters/{character_id}/activate", dependencies=[Depends(require_admin)])
    async def activate(character_id: str, data: dict = Body(default={})):
        return await runtime.activate(character_id, data.get("version"))

    @app.post("/api/characters/{character_id}/clone", dependencies=[Depends(require_admin)])
    async def clone(character_id: str, data: dict = Body(...)):
        if store.one("SELECT id FROM characters WHERE id=?", (data.get("id"),)):
            raise ValueError("角色ID已存在")
        seed = store.character(character_id)
        seed.update(id=data.get("id"), name=data.get("name"))
        return store.save_character(seed)

    @app.get("/api/voices", dependencies=[Depends(require_admin)])
    async def voices():
        return settings.voices()

    @app.post("/api/tts/preview", dependencies=[Depends(require_admin)])
    async def preview(data: dict = Body(...)):
        text = data.get("text", "")
        if not isinstance(text, str) or not 1 <= len(text) <= 120:
            raise ValueError("试听台词为1到120字")
        if runtime.current_id or runtime.queue:
            raise ValueError("直播生成／播放繁忙，请先暂停并等待当前任务结束")
        async with runtime.generation_lock:
            artifact = await runtime.voices.synthesize(text, settings.voice(data.get("voice_id")))
        return {
            "audio_ref": artifact.ref,
            "duration": artifact.duration,
            "text": artifact.text,
            "warning": artifact.warning,
        }

    @app.get("/api/assets", dependencies=[Depends(require_admin)])
    async def assets():
        return settings.assets(runtime.character)

    @app.get("/api/settings", dependencies=[Depends(require_admin)])
    async def config(request: Request):
        return settings.public_data() | {
            "config_path": str(settings.path),
            "overlay_url": str(request.base_url).rstrip("/") + "/overlay#token=" + settings.overlay_token,
            "platform": "Mock；B站评论已接入，B站／抖音直播后置",
            "changes": "模型／API／上下文设置可在此保存；直播后置，默认审核与自动关闭",
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

    @app.get("/api/context/status", dependencies=[Depends(require_admin)])
    async def context_status(video_id: str | None = None, thread_id: str | None = None):
        if video_id is not None or thread_id is not None:
            if not video_id or not thread_id:
                raise ValueError("请同时指定视频与对话楼ID")
            return runtime.context.status(comments.context_scope(video_id, thread_id), runtime.character)
        return runtime.context.status()

    @app.get("/api/retention", dependencies=[Depends(require_admin)])
    async def retention_policy():
        return runtime.retention.policy() | {"error": runtime.retention_error}

    @app.put("/api/retention", dependencies=[Depends(require_admin)])
    async def retention_settings(data: dict = Body(...)):
        if set(data) != {"days"}:
            raise ValueError("仅接受days；null表示不自动清理")
        return runtime.retention.set_policy(data["days"])

    def retention_arguments(data, *, cleanup=False):
        if set(data) - ({"before", "confirm"} if cleanup else {"before"}):
            raise ValueError("未知清理参数")
        if cleanup and data.get("confirm") is not True:
            raise ValueError("清理不可恢复，请先预览并明确确认")
        return data.get("before")

    @app.post("/api/retention/preview", dependencies=[Depends(require_admin)])
    async def retention_preview(data: dict = Body(...)):
        return runtime.retention.preview(retention_arguments(data))

    @app.post("/api/retention/cleanup", dependencies=[Depends(require_admin)])
    async def retention_cleanup(data: dict = Body(...)):
        before = retention_arguments(data, cleanup=True)
        if runtime.current_id or runtime.queue or runtime.generation_lock.locked():
            raise HTTPException(409, "正在生成或播放，请暂停并等待当前任务结束后清理")
        return await runtime.cleanup_retention(before)

    @app.post("/api/context/rebuild", dependencies=[Depends(require_admin)])
    async def rebuild_context(data: dict = Body(...)):
        if set(data) != {"scope", "video_id", "thread_id"} or data.get("scope") != "comments":
            raise ValueError("请指定comments、video_id和thread_id，仅重建单楼")
        video_id, thread_id = data.get("video_id"), data.get("thread_id")
        scope = comments.context_scope(video_id, thread_id)
        if runtime.current_id or runtime.queue or runtime.generation_lock.locked():
            raise HTTPException(409, "正在生成或播放，请等待当前任务结束后重建")
        async with runtime.generation_lock:
            window = store.one(
                "SELECT history FROM context_windows WHERE key=?",
                (runtime.context.key(scope, runtime.character),),
            )
            if window and json.loads(window["history"]):
                raise HTTPException(409, "当前楼已有上下文，无需覆盖重建")
            if not store.one(
                "SELECT id FROM comments WHERE platform='bilibili' AND video_id=? AND thread_id=?",
                (video_id, thread_id),
            ):
                raise HTTPException(404, "本地没有该楼原文，请重新读取或导入评论")
            return await comments.rebuild_context(video_id, thread_id)

    @app.post("/api/context/compact", dependencies=[Depends(require_admin)])
    async def compact(data: dict = Body(...)):
        if runtime.current_id or runtime.queue:
            raise ValueError("直播正在生成或播放，请暂停并等待当前任务结束后压缩")
        if data.get("scope") == "live":
            scope, mode = "live:" + str(runtime.session_id), "live"
        elif data.get("scope") == "comments":
            video_id = data.get("video_id", "")
            if not isinstance(video_id, str) or not 1 <= len(video_id) <= 100:
                raise ValueError("请指定评论视频ID")
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 200:
                raise ValueError("请指定单个对话楼ID，不将整个视频的评论合并压缩")
            scope, mode = comments.context_scope(video_id, thread_id), "comment_draft"
        else:
            raise ValueError("scope应为live或comments")
        async with runtime.generation_lock:
            return await runtime.context.compact(scope, runtime.character, mode)

    @app.get("/api/comments/policy", dependencies=[Depends(require_admin)])
    async def policy():
        return comments.policy()

    @app.put("/api/comments/policy", dependencies=[Depends(require_admin)])
    async def set_policy(data: dict = Body(...)):
        return await comments.set_policy(data)

    @app.get("/api/comments/videos", dependencies=[Depends(require_admin)])
    async def videos():
        try:
            return await comments.adapter.popular()
        except Exception as error:
            raise ValueError(public_error(error)) from None

    @app.post("/api/comments/fetch", dependencies=[Depends(require_admin)])
    async def fetch(data: dict = Body(...)):
        try:
            return await comments.fetch(data.get("video", ""), data.get("max_pages", 1))
        except Exception as error:
            raise ValueError(public_error(error)) from None

    @app.get("/api/dynamics/policy", dependencies=[Depends(require_admin)])
    async def dynamic_policy():
        return dynamics.policy()

    @app.put("/api/dynamics/policy", dependencies=[Depends(require_admin)])
    async def set_dynamic_policy(data: dict = Body(...)):
        return await dynamics.set_policy(data)

    @app.get("/api/dynamics/posts", dependencies=[Depends(require_admin)])
    async def dynamic_posts(status: str | None = None, limit: int = Query(50, ge=1, le=100)):
        return dynamics.list_posts(status=status, limit=limit)

    @app.get("/api/dynamics/decisions", dependencies=[Depends(require_admin)])
    async def dynamic_decisions(limit: int = Query(50, ge=1, le=100)):
        return dynamics.list_decisions(limit=limit)

    @app.get("/api/dynamics/memories", dependencies=[Depends(require_admin)])
    async def dynamic_memories(limit: int = Query(100, ge=1, le=200)):
        return dynamics.list_memories(limit=limit)

    @app.get("/api/dynamics/memory-summary", dependencies=[Depends(require_admin)])
    async def dynamic_memory_summary():
        return dynamics.summary_status()

    @app.put("/api/dynamics/memories/{memory_id}", dependencies=[Depends(require_admin)])
    async def dynamic_memory(memory_id: int, data: dict = Body(...)):
        if set(data) != {"public"} or type(data["public"]) is not bool:
            raise ValueError("请指定 public 布尔值")
        return dynamics.mark_memory(memory_id, data["public"])

    @app.post("/api/dynamics/evaluate", dependencies=[Depends(require_admin)])
    async def evaluate_dynamic():
        return await dynamics.evaluate(manual=True)

    @app.post("/api/dynamics/posts/{post_id}/review", dependencies=[Depends(require_admin)])
    async def review_dynamic(post_id: int, data: dict = Body(...)):
        if set(data) - {"action", "text"}:
            raise ValueError("未知动态审核字段")
        return dynamics.review(post_id, data.get("action"), data.get("text"))

    @app.post("/api/dynamics/posts/{post_id}/publish", dependencies=[Depends(require_admin)])
    async def publish_dynamic(post_id: int):
        return await dynamics.publish(post_id)

    @app.post("/api/comments/import", dependencies=[Depends(require_admin)])
    async def import_comments(data: dict = Body(...)):
        return comments.import_items(
            data.get("video_id", ""), data.get("items"), data.get("csv_text"), data.get("field_mapping")
        )

    @app.post("/api/comments/import/preview", dependencies=[Depends(require_admin)])
    async def preview_import(data: dict = Body(...)):
        prepared = comments.prepare_import(
            data.get("video_id", ""), data.get("items"), data.get("csv_text"), data.get("field_mapping")
        )
        return {"video_id": data.get("video_id"), "total": len(prepared), "items": prepared[:10]}

    @app.put("/api/comments/{comment_id}/category", dependencies=[Depends(require_admin)])
    async def edit_category(comment_id: int, data: dict = Body(...)):
        return comments.set_category(comment_id, data.get("category"))

    @app.get("/api/comments", dependencies=[Depends(require_admin)])
    async def list_comments(
        video_id: str | None = None,
        thread_id: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ):
        return comments.list_comments(video_id, category, status, limit, thread_id=thread_id)

    @app.post("/api/comments/analyze", dependencies=[Depends(require_admin)])
    async def analyze(data: dict = Body(default={})):
        return comments.analyze(data.get("video_id"))

    @app.post("/api/comments/drafts", dependencies=[Depends(require_admin)])
    async def drafts(data: dict = Body(...)):
        return comments.enqueue(data.get("comment_ids", []))

    @app.get("/api/comment-tasks", dependencies=[Depends(require_admin)])
    async def tasks():
        return store.all("SELECT * FROM comment_tasks ORDER BY created_at DESC LIMIT 50")

    @app.post("/api/comments/drafts/{draft_id}/review", dependencies=[Depends(require_admin)])
    async def review(draft_id: int, data: dict = Body(...)):
        return comments.review(draft_id, data.get("action"), data.get("text"))

    @app.post("/api/comments/drafts/{draft_id}/copy", dependencies=[Depends(require_admin)])
    async def copy(draft_id: int):
        return comments.copy(draft_id)

    @app.post("/api/comments/drafts/{draft_id}/publish", dependencies=[Depends(require_admin)])
    async def publish(draft_id: int):
        return await comments.publish(draft_id)

    @app.websocket("/ws/admin")
    async def admin_ws(websocket: WebSocket):
        if not same_origin(websocket) or not admin_ok(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while admin_ok(websocket):
                await websocket.send_json({"type": "status", "data": runtime.status()})
                await asyncio.sleep(1)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.websocket("/ws/overlay")
    async def overlay_ws(websocket: WebSocket):
        if not same_origin(websocket) or not overlay_ok(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        owner = await runtime.attach_overlay(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 4096:
                    await websocket.close(code=1009)
                    break
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        await runtime.acknowledge(owner, data)
                except json.JSONDecodeError:
                    pass
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await runtime.detach_overlay(owner)

    return app
