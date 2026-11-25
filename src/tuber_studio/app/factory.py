"""L4 FastAPI application factory and shared route composition."""

from __future__ import annotations

import json
import os
import weakref
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..agent.api import create_workbench_task_runtime as create_task_runtime
from ..agent.tasks.tools import register_device_tools
from ..core.auth import AuthSessions
from ..core.authority import acquire_business_writer
from ..core.config import Settings
from ..core.store import Store
from ..infra.inference.video_vision_service import configured_video_provider
from ..infra.platforms.bilibili.account import BilibiliAccount
from ..operations.channels.comments import Comments
from ..operations.channels.direct_messages import DirectMessages
from ..operations.channels.dynamics import Dynamics
from ..operations.service import BrowsingService
from ..persona.assets.illustration_policy import migrate_illustration_policy
from ..persona.characters.presets import upgrade_bundled_characters
from .errors import install_error_handlers
from .lifespan import build_lifespan
from .middleware import AuthContext, register_http_middleware
from .routes import (
    register_agent_routes,
    register_entry_routes,
    register_live_routes,
    register_operations_channel_routes,
    register_operations_routes,
    register_persona_routes,
    register_system_routes,
    register_video_production_routes,
)
from .runtime import Runtime
from .web_mount import WebMount


def _default_web_dist() -> Path:
    return Path(__file__).resolve().parents[1] / "web_dist"


def register_all_routes(
    app: FastAPI,
    *,
    settings: Settings,
    runtime: Runtime,
    store: Store,
    account: BilibiliAccount,
    comments: Comments,
    dynamics: Dynamics,
    messages: DirectMessages,
    browsing: BrowsingService,
    memes,
    sessions: AuthSessions,
    call_guards: dict,
    login_attempts: dict | None = None,
    mount_static: bool = True,
) -> None:
    """Mount error handlers, static assets, middleware, and all L4 route modules onto FastAPI."""
    install_error_handlers(app)
    auth = AuthContext(settings, sessions)

    web_dist = _default_web_dist()
    if mount_static:
        if web_dist.is_dir():
            app.mount("/static", StaticFiles(directory=web_dist), name="static")
        assets_dir = settings.root / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    register_live_routes(
        app,
        settings,
        runtime,
        auth,
        on_stardew_device=lambda device: (
            runtime.live_facade.attach_stardew_device(device),
            register_device_tools(runtime.tasks, runtime.live_facade),
        ),
    )
    register_agent_routes(
        app,
        runtime,
        store,
        browsing,
        dynamics,
        comments,
        messages,
        auth.require_admin,
        call_guards=call_guards,
    )
    register_system_routes(app, settings, runtime, comments, dynamics, auth, login_attempts=login_attempts)
    register_persona_routes(app, settings, runtime, store, comments, memes, auth.require_admin)
    register_operations_routes(
        app, runtime, store, account, comments, browsing, dynamics, messages, auth.require_admin
    )
    register_operations_channel_routes(
        app, store, account, comments, browsing, dynamics, messages, auth.require_admin
    )
    register_video_production_routes(app, runtime, store, browsing, auth.require_admin)
    register_entry_routes(app, settings, store, auth, web_dir=web_dist)
    register_http_middleware(app, auth, sessions, call_guards=call_guards)
    if prefix := os.getenv("TUBER_WEB_PREFIX"):
        app.add_middleware(WebMount, prefix=prefix)


def create_app(
    settings=None,
    adapter=None,
    *,
    singing_provider=None,
    message_adapter=None,
    video_evidence_factory=None,
    live_scene=None,
) -> FastAPI:
    """Create and configure the Tuber Studio FastAPI application."""
    settings = settings or Settings()
    writer = acquire_business_writer(settings.runtime)
    writer.__enter__()
    try:
        settings.migrate_connection()
        AuthSessions.prepare_runtime(settings)
        seeds = json.loads((settings.root / "config/characters.json").read_text(encoding="utf-8"))
        store = Store(
            settings.runtime / "studio.sqlite3",
            seeds,
            migrations=(migrate_illustration_policy, upgrade_bundled_characters),
        )
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
    runtime.cache.meme_inbox_evicted_callback = runtime.meme_harvester.on_cache_evicted
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
    runtime.visual_provider = visual
    runtime.tasks = create_task_runtime(runtime, store, browsing)

    release_handle: weakref.finalize | None = None
    lifespan = build_lifespan(
        runtime, comments, account, dynamics, messages, browsing, store, lambda: release_handle() if release_handle else None
    )
    app = FastAPI(title="Tuber Studio", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    release_handle = weakref.finalize(app, writer.__exit__, None, None, None)

    app.state.runtime = runtime
    app.state.comments = comments
    app.state.store = store
    app.state.dynamics = dynamics
    app.state.messages = messages
    app.state.browsing = browsing
    app.state.bilibili_account = account
    app.state.memes = memes
    app.state.auth_sessions = sessions

    call_guards: dict = {}
    login_attempts: dict = {}
    register_all_routes(
        app,
        settings=settings,
        runtime=runtime,
        store=store,
        account=account,
        comments=comments,
        dynamics=dynamics,
        messages=messages,
        browsing=browsing,
        memes=memes,
        sessions=sessions,
        call_guards=call_guards,
        login_attempts=login_attempts,
    )
    return app
