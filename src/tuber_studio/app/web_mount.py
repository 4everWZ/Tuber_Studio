"""Serve the workbench at a deployment's public path and at its direct routes."""

import re
from html import escape

from fastapi.responses import HTMLResponse, RedirectResponse


class WebMount:
    def __init__(self, app, prefix):
        if not re.fullmatch(r"/[A-Za-z0-9_-]+", prefix):
            raise ValueError("工坊入口须是单段路径")
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            root = scope.get("root_path", "").rstrip("/") + self.prefix
            path = scope["path"]
            if scope["type"] == "http" and path == root:
                query = scope.get("query_string", b"").decode("latin-1")
                response = RedirectResponse(root + "/" + ("?" + query if query else ""))
                await response(scope, receive, send)
                return
            if path.startswith(root + "/"):
                scope = dict(scope, root_path=root)
        await self.app(scope, receive, send)


def route_path(request):
    path, root = request.url.path, request.scope.get("root_path", "").rstrip("/")
    return path[len(root) :] if path.startswith(root + "/") else path


def web_page(request, content):
    base = escape(request.scope.get("root_path", "").rstrip("/") + "/", quote=True)
    head = f'<head><base href="{base}"><script src="static/studio-path.js" defer></script>'
    return HTMLResponse(content.replace("<head>", head, 1), headers={"Cache-Control": "no-cache, must-revalidate"})
