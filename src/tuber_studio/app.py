"""Local workbench, authenticated remote management, and scoped OBS playback."""

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import time
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

from .agent.api import register_agent_api
from .agent.operator_chat import register_operator_chat
from .agent.private_avatar import register_private_avatar
from .agent.private_call import register_private_call
from .agent.private_call_work import register_call_work
from .agent.reply_operations import register_reply_operations
from .agent.task_api import register_task_api
from .agent.task_tools import create_task_runtime
from .core.auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    AuthSessions,
    EnvironmentManagedToken,
    InvalidAdminCredential,
)
from .core.authority import acquire_business_writer
from .core.config import Settings, atomic_bytes
from .core.store import Store
from .infra.inference.diagnostics import check_provider_diagnostics, saved_provider_models
from .infra.inference.providers import public_error
from .infra.inference.video_vision_service import configured_video_provider
from .infra.platforms.bilibili.account import BilibiliAccount
from .live.api import register_live_api
from .live.media import MAX_UPLOAD
from .meme_api import MAX_REQUEST as MAX_MEME_REQUEST
from .meme_api import register_meme_api
from .message_api import register_message_api
from .model_runtime.api import register_model_api
from .operation_recovery_api import register_operation_recovery_api
from .operations.api import register_browsing_api, register_operations_api
from .operations.channels.comments import Comments
from .operations.channels.direct_messages import DirectMessages
from .operations.channels.dynamics import Dynamics
from .operations.service import BrowsingService
from .persona.assets.illustration_policy import (
    IllustrationPolicy,
    illustration_policy,
    migrate_illustration_policy,
    save_illustration_policy,
)
from .persona.characters.performance import Performance, performance, save_performance
from .persona.characters.pets import PetSelection
from .persona.characters.presets import upgrade_bundled_characters
from .persona.characters.references import DEFAULTS as REFERENCE_DEFAULTS
from .persona.characters.references import MAX_BYTES as MAX_REFERENCE_BYTES
from .persona.characters.references import PREFIX as REFERENCE_PREFIX
from .persona.characters.references import Variant as ReferenceVariant
from .persona.characters.references import (
    _location as reference_location,
)
from .persona.characters.references import (
    _selection as reference_selection,
)
from .persona.characters.references import (
    _validate_upload as validate_reference_upload,
)
from .persona.characters.references import (
    resolve_reference,
)
from .runtime import Runtime
from .web_entry import WebMount, route_path, web_page


def register_provider_diagnostics(app, settings, runtime, require_admin):
    @app.post("/api/providers/check", dependencies=[Depends(require_admin)])
    async def check():
        return await check_provider_diagnostics(settings, runtime)

    @app.get("/api/providers/models", dependencies=[Depends(require_admin)])
    async def saved_models():
        return await saved_provider_models(settings)


def register_performance(app, runtime, require_admin):
    @app.get("/api/settings/performance", dependencies=[Depends(require_admin)])
    async def read():
        return performance(runtime.store)

    @app.put("/api/settings/performance", dependencies=[Depends(require_admin)])
    async def update(body: Performance):
        return save_performance(runtime.store, body)


def register_pet_api(app, pets, require_admin):
    path = "/api/pets/{character_id}"
    auth = [Depends(require_admin)]

    @app.get(path, dependencies=auth)
    async def options(character_id: str):
        return await pets.options(character_id)

    @app.put(path, dependencies=auth)
    async def select(character_id: str, body: PetSelection):
        return await pets.select(character_id, body.asset_id)


def register_illustration_policy(app, store, require_admin):
    path = "/api/settings/illustrations"

    @app.get(path, dependencies=[Depends(require_admin)])
    async def read():
        return illustration_policy(store)

    @app.patch(path, dependencies=[Depends(require_admin)])
    async def update(value: IllustrationPolicy):
        changes = value.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "请选择要修改的配图方式")
        return save_illustration_policy(store, illustration_policy(store).model_dump() | changes)


def register_artwork_references(app, settings, store, require_admin):
    route = REFERENCE_PREFIX + "/{character_id}"
    auth = [Depends(require_admin)]

    @app.get(route, dependencies=auth)
    def list_references(character_id: str):
        store.character(character_id)
        variants = {}
        for variant in REFERENCE_DEFAULTS:
            try:
                _, digest, source = reference_location(settings, character_id, variant)
                url = f"{REFERENCE_PREFIX}/{quote(character_id, safe='')}/{variant}?sha256={digest}"
                variants[variant] = dict(
                    available=True,
                    source=source,
                    sha256=digest,
                    preview_url=url,
                    thumbnail_url=url + "&thumbnail=true",
                )
            except FileNotFoundError:
                variants[variant] = dict(available=False, source=None, sha256=None, preview_url=None)
        return dict(character_id=character_id, variants=variants)

    @app.get(route + "/{variant}", dependencies=auth)
    def preview(character_id: str, variant: ReferenceVariant, sha256: str | None = None, thumbnail: bool = False):
        store.character(character_id)
        try:
            ref = resolve_reference(settings, character_id, variant, sha256=sha256)
        except FileNotFoundError:
            raise HTTPException(404, "该版本参考图不可用") from None
        if thumbnail:
            with Image.open(io.BytesIO(ref.data)) as picture:
                picture.thumbnail((224, 224), Image.Resampling.LANCZOS)
                thumb = ImageOps.exif_transpose(picture).convert("RGBA")
                out = io.BytesIO()
                thumb.save(out, "PNG")
            return Response(out.getvalue(), media_type="image/png")
        mime = "image/png" if ref.data.startswith(b"\x89PNG") else "image/jpeg"
        return Response(ref.data, media_type=mime)

    @app.put(route + "/{variant}", dependencies=auth)
    async def replace(character_id: str, variant: ReferenceVariant, request: Request):
        store.character(character_id)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_REFERENCE_BYTES:
                raise HTTPException(413, "参考图不能超过 4 MiB")
            data.extend(chunk)
        validate_reference_upload(data)
        digest = hashlib.sha256(data).hexdigest()
        folder = reference_selection(settings, character_id, variant)
        atomic_bytes(folder / digest, data, private=True)
        atomic_bytes(folder / "active", digest.encode("ascii"), private=True)
        return list_references(character_id)

    @app.delete(route + "/{variant}", dependencies=auth)
    def restore(character_id: str, variant: ReferenceVariant):
        store.character(character_id)
        (reference_selection(settings, character_id, variant) / "active").unlink(missing_ok=True)
        return list_references(character_id)


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
        settings.migrate_connection()
        AuthSessions.prepare_runtime(settings)
        seeds = json.loads((settings.root / "config/characters.json").read_text(encoding="utf-8"))
        store = Store(settings.runtime / "studio.sqlite3", seeds, migrations=(migrate_illustration_policy, upgrade_bundled_characters))
    except BaseException:
        writer.__exit__(None, None, None)
        raise
    sessions = AuthSessions(settings, store)
    runtime = Runtime(settings, store, singing_provider=singing_provider, live_scene=live_scene)
    account = BilibiliAccount(settings, store, adapter)
    comments = Comments(
        settings,
        store,
        runtime,
        account,
        is_live_busy=lambda: runtime.live.state in {"live", "paused"}
        and bool(runtime.live.queue or runtime.live.current_id),
    )
    runtime.account = account
    memes = runtime.pets.catalog
    runtime.operation_images.catalog = memes
    comments.memes = memes
    dynamics = Dynamics(
        settings,
        store,
        runtime,
        comments,
        account=account,
        memes=memes,
        is_live_busy=lambda: runtime.live.state in {"live", "paused"} and bool(runtime.live.busy),
    )
    messages = DirectMessages(settings, store, runtime, comments, message_adapter, account=account, memes=memes)
    comments.direct_messages = messages
    visual = (
        video_evidence_factory(runtime)
        if video_evidence_factory is not None
        else configured_video_provider(runtime)
    )
    browsing = BrowsingService(
        settings, store, runtime, comments, messages, dynamics, visual=visual, bilibili_account=account
    )
    runtime.browsing = browsing
    runtime.tasks = create_task_runtime(runtime, store, browsing)
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
            runtime.tasks.start()
            yield
        finally:
            try:
                try:
                    await runtime.tasks.close()
                finally:
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
        path = route_path(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not same_origin(request):
                return JSONResponse({"detail": "跨站写入被拒绝"}, status_code=403)
            uploading = path == "/api/live/uploads" and request.method == "POST"
            meme_upload = path == "/api/memes" and request.method == "POST"
            limit = MAX_UPLOAD if uploading else MAX_MEME_REQUEST if meme_upload else 2_100_000
            reference_upload = request.method == "PUT" and path.startswith(REFERENCE_PREFIX + "/")
            if reference_upload:
                limit = MAX_REFERENCE_BYTES
            if int(request.headers.get("content-length", "0")) > limit:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
            if not uploading and not reference_upload:
                body = await request.body()
                if len(body) > limit:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        cookie = request.cookies.get(COOKIE_NAME)
        if (
            cookie
            and response.status_code < 400
            and path not in {"/api/auth", "/api/logout", "/api/auth/reset-token"}
            and sessions.needs_cookie_refresh(cookie)
        ):
            admin_cookie(response, request, cookie)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": public_error(error)}, status_code=400)

    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")
    app.mount("/assets", StaticFiles(directory=settings.root / "assets"), name="assets")
    register_live_api(app, settings, runtime, require_admin, admin_ok, overlay_ok, same_origin)
    register_agent_api(app, settings, runtime, store, comments, require_admin)
    operator_chat = register_operator_chat(app, runtime, store, browsing, dynamics, require_admin)
    register_private_call(app, runtime, require_admin, operator_chat=operator_chat)
    register_call_work(app, runtime, require_admin, operator_chat)
    register_private_avatar(app, runtime, require_admin, operator_chat=operator_chat)
    register_performance(app, runtime, require_admin)
    register_task_api(app, runtime.tasks, require_admin)
    register_reply_operations(app, runtime.tasks, comments, messages, require_admin)
    register_model_api(app, settings, runtime, require_admin)
    register_provider_diagnostics(app, settings, runtime, require_admin)
    register_operations_api(app, store, account, comments, browsing, dynamics, require_admin)
    register_message_api(app, messages, require_admin)
    register_meme_api(app, memes, runtime, require_admin)
    register_pet_api(app, runtime.pets, require_admin)
    register_artwork_references(app, settings, store, require_admin)
    register_illustration_policy(app, store, require_admin)
    register_browsing_api(app, browsing, require_admin)
    register_operation_recovery_api(
        app,
        runtime,
        {"comments": comments, "messages": messages, "dynamics": dynamics, "browsing": browsing},
        require_admin,
    )

    @app.get("/settings", include_in_schema=False)
    async def settings_page(request: Request):
        return RedirectResponse(request.scope.get("root_path", "").rstrip("/") + "/#settings")

    @app.get("/")
    async def index(request: Request):
        shell = (web / "index.html").read_text(encoding="utf-8")
        content = re.sub(
            r"<!-- page: ([a-z-]+) -->",
            lambda match: (web / "pages" / f"{match[1]}.html").read_text(encoding="utf-8"),
            shell,
        )
        return web_page(request, content)

    @app.get("/overlay")
    async def overlay(request: Request):
        return web_page(request, (web / "overlay.html").read_text(encoding="utf-8"))

    @app.get("/overlay/draw-guess")
    async def draw_guess_overlay(request: Request):
        return web_page(request, (web / "draw-guess-overlay.html").read_text(encoding="utf-8"))

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
        if runtime.live.busy or runtime.generation_active():
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

    if prefix := os.getenv("TUBER_WEB_PREFIX"):
        app.add_middleware(WebMount, prefix=prefix)
    return app
