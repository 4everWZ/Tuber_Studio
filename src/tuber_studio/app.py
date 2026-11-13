"""Local workbench, authenticated remote management, and scoped OBS playback."""

import asyncio
import json
import os
import re
import secrets
import time
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent.api import register_agent_api
from .agent.operator_chat import register_operator_chat
from .auth_sessions import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    AuthSessions,
    EnvironmentManagedToken,
    InvalidAdminCredential,
)
from .bilibili_account import BilibiliAccount
from .comments import Comments
from .config import Settings
from .direct_messages import DirectMessages
from .dynamics import Dynamics
from .live.api import register_live_api
from .live.media import MAX_UPLOAD
from .meme_api import MAX_REQUEST as MAX_MEME_REQUEST
from .meme_api import register_meme_api
from .memes import MemeCatalog
from .message_api import register_message_api
from .model_runtime.api import register_model_api
from .operation_recovery_api import register_operation_recovery_api
from .operations.api import register_operations_api
from .operations.browsing.api import register_browsing_api
from .operations.browsing.service import BrowsingService
from .providers import public_error
from .runtime import Runtime
from .runtime_authority import acquire_business_writer
from .store import Store
from .video_vision_service import configured_video_provider


def create_app(
    settings=None,
    adapter=None,
    *,
    singing_provider=None,
    message_adapter=None,
    video_evidence_factory=None,
    live_scene=None,
):
    settings = settings or Settings()
    writer = acquire_business_writer(settings.runtime)
    writer.__enter__()
    try:
        AuthSessions.prepare_runtime(settings)
        seeds = json.loads((settings.root / "config/characters.json").read_text(encoding="utf-8"))
        store = Store(settings.runtime / "studio.sqlite3", seeds)
    except BaseException:
        writer.__exit__(None, None, None)
        raise
    sessions = AuthSessions(settings, store)
    runtime = Runtime(settings, store, singing_provider=singing_provider, live_scene=live_scene)
    comments = Comments(settings, store, runtime, adapter)
    account = BilibiliAccount(comments)
    runtime.account = account
    memes = MemeCatalog(settings, store)
    dynamics = Dynamics(settings, store, runtime, comments, memes=memes)
    messages = DirectMessages(settings, store, runtime, comments, message_adapter, memes=memes)
    comments.direct_messages = messages
    visual = (
        video_evidence_factory(runtime)
        if video_evidence_factory is not None
        else configured_video_provider(runtime)
    )
    browsing = BrowsingService(settings, store, runtime, comments, messages, dynamics, visual=visual)
    runtime.browsing = browsing
    login_attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.open()
            await comments.open()
            await account.open()
            await dynamics.open()
            await messages.open()
            await browsing.open()
            runtime.memory_organizer.start()
            yield
        finally:
            try:
                await runtime.memory_organizer.close()
                await browsing.close()
            finally:
                try:
                    await messages.close()
                finally:
                    try:
                        await dynamics.close()
                    finally:
                        try:
                            try:
                                await account.close()
                            finally:
                                await comments.close()
                        finally:
                            try:
                                await runtime.close()
                            finally:
                                try:
                                    store.close()
                                finally:
                                    release_writer()

    app = FastAPI(title="Tuber Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
    release_writer = weakref.finalize(app, writer.__exit__, None, None, None)
    app.state.runtime, app.state.comments, app.state.store = runtime, comments, store
    app.state.dynamics = dynamics
    app.state.messages = messages
    app.state.browsing = browsing
    app.state.bilibili_account = account
    app.state.memes = memes
    app.state.auth_sessions = sessions

    def local_access(connection):
        loopback = {"127.0.0.1", "::1"}
        return (
            not os.getenv("TUBER_ADMIN_TOKEN")
            and connection.client is not None
            and connection.client.host in loopback
            and "host" in connection.headers
            and connection.url.hostname in loopback | {"localhost"}
            and connection.headers.get("sec-fetch-site") != "cross-site"
            and not any(
                header in connection.headers
                for header in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
            )
            and same_origin(connection)
        )

    def admin_ok(connection):
        bearer = connection.headers.get("authorization", "")
        return (
            local_access(connection)
            or sessions.valid(connection.cookies.get(COOKIE_NAME))
            or (bearer.startswith("Bearer ") and sessions.authenticate(bearer[7:]))
        )

    def admin_cookie(response, request, session):
        response.set_cookie(
            COOKIE_NAME,
            session,
            httponly=True,
            samesite="strict",
            max_age=COOKIE_MAX_AGE,
            secure=request.url.scheme == "https",
        )

    def clear_admin_cookie(response, request):
        response.delete_cookie(
            COOKIE_NAME, httponly=True, samesite="strict", secure=request.url.scheme == "https"
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
            meme_upload = request.url.path == "/api/memes" and request.method == "POST"
            limit = MAX_UPLOAD if uploading else MAX_MEME_REQUEST if meme_upload else 2_100_000
            if int(request.headers.get("content-length", "0")) > limit:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
            if not uploading:
                body = await request.body()
                if len(body) > limit:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        cookie = request.cookies.get(COOKIE_NAME)
        if (
            cookie
            and response.status_code < 400
            and request.url.path not in {"/api/auth", "/api/logout", "/api/auth/reset-token"}
            and sessions.needs_cookie_refresh(cookie)
        ):
            admin_cookie(response, request, cookie)
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
    register_live_api(app, settings, runtime, require_admin, admin_ok, overlay_ok, same_origin)
    register_agent_api(app, settings, runtime, store, comments, require_admin)
    register_operator_chat(app, runtime, store, browsing, dynamics, require_admin)
    register_model_api(app, settings, runtime, require_admin)
    register_operations_api(app, store, account, comments, browsing, dynamics, require_admin)
    register_message_api(app, messages, require_admin)
    register_meme_api(app, memes, runtime, require_admin)
    register_browsing_api(app, browsing, require_admin)
    register_operation_recovery_api(
        app,
        runtime,
        {"comments": comments, "messages": messages, "dynamics": dynamics, "browsing": browsing},
        require_admin,
    )

    @app.get("/")
    async def index():
        shell = (web / "index.html").read_text(encoding="utf-8")
        content = re.sub(
            r"<!-- page: ([a-z-]+) -->",
            lambda match: (web / "pages" / f"{match[1]}.html").read_text(encoding="utf-8"),
            shell,
        )
        return HTMLResponse(content, headers={"Cache-Control": "no-cache, must-revalidate"})

    @app.get("/overlay")
    async def overlay():
        return FileResponse(web / "overlay.html")

    @app.get("/overlay/draw-guess")
    async def draw_guess_overlay():
        return FileResponse(web / "draw-guess-overlay.html")

    @app.post("/api/auth")
    async def login(request: Request, data: dict = Body(...)):
        ip = request.client.host if request.client else "local"
        attempts = [t for t in login_attempts.get(ip, []) if time.time() - t < 60]
        if len(attempts) >= 10:
            raise HTTPException(429, "登录尝试过多，请稍后再试")
        token = data.get("token", "")
        try:
            session = sessions.issue(token, request.cookies.get(COOKIE_NAME))
        except InvalidAdminCredential:
            login_attempts[ip] = attempts + [time.time()]
            raise HTTPException(401, "管理令牌无效")
        login_attempts.pop(ip, None)
        response = JSONResponse({"authenticated": True})
        admin_cookie(response, request, session)
        return response

    @app.post("/api/logout", dependencies=[Depends(require_admin)])
    async def logout(request: Request):
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"authenticated": False})
        clear_admin_cookie(response, request)
        return response

    @app.post("/api/auth/reset-token", dependencies=[Depends(require_admin)])
    async def reset_admin_token(request: Request):
        authorization = request.headers.get("authorization", "")
        try:
            token = sessions.reset_token(
                cookie=request.cookies.get(COOKIE_NAME),
                bearer=authorization[7:] if authorization.startswith("Bearer ") else None,
            )
        except InvalidAdminCredential as error:
            raise HTTPException(401, str(error)) from None
        except EnvironmentManagedToken as error:
            raise HTTPException(409, str(error)) from None
        response = JSONResponse({"token": token, "authenticated": False})
        clear_admin_cookie(response, request)
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

    @app.get("/api/settings", dependencies=[Depends(require_admin)])
    async def config(request: Request):
        return settings.public_data() | {
            "local_access": local_access(request),
            "config_path": str(settings.path),
            "overlay_url": str(request.base_url).rstrip("/") + "/overlay#token=" + settings.overlay_token,
            "platform": "Mock；B站评论已接入，B站／抖音直播后置",
            "changes": "模型／API／上下文设置可在此保存；直播后置，默认审核与自动关闭",
        }

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
        if runtime.live.busy or runtime.generation_lock.locked():
            raise HTTPException(409, "正在生成或播放，请暂停并等待当前任务结束后清理")
        return await runtime.cleanup_retention(before)

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
            await websocket.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app
