"""Untouched web entry HTML pages, healthz, and shared audio media routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..web_mount import web_page


def register_entry_routes(app, settings, store, auth, *, web_dir: Path | None = None) -> None:
    """Register `/`, `/live`, `/overlay`, `/overlay/draw-guess`, `/settings`, `/healthz`, and `/media/audio/{name}`."""
    web = web_dir or (Path(__file__).resolve().parents[2] / "web")

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

    @app.get("/live")
    async def workbench(request: Request):
        html = (web / "live.html").read_text(encoding="utf-8")
        return web_page(request, html) if web_page else HTMLResponse(html)

    @app.get("/overlay")
    async def overlay(request: Request):
        return web_page(request, (web / "overlay.html").read_text(encoding="utf-8"))

    @app.get("/overlay/draw-guess")
    async def draw_guess_overlay(request: Request):
        return web_page(request, (web / "draw-guess-overlay.html").read_text(encoding="utf-8"))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        store.one("SELECT 1")
        return {"status": "ok"}

    @app.get("/media/audio/{name}")
    async def audio(name: str, request: Request):
        if not (auth.admin_ok(request) or auth.overlay_ok(request)):
            raise HTTPException(401, "音频读取需要播放或管理凭证")
        path = (settings.audio / name).resolve()
        if path.parent != settings.audio or path.suffix != ".wav" or not path.is_file():
            raise HTTPException(404, "音频不存在")
        return FileResponse(path, media_type="audio/wav")
