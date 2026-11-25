"""Authentication dependencies, same-origin checks, upload limits, and security headers."""

from __future__ import annotations

import os
import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from ..core.auth import COOKIE_MAX_AGE, COOKIE_NAME
from ..live.facade import MAX_UPLOAD
from ..persona.assets.memes import MAX_BYTES as MAX_MEME_BYTES
from ..persona.characters.references import MAX_BYTES as MAX_REFERENCE_BYTES
from ..persona.characters.references import PREFIX as REFERENCE_PREFIX
from .errors import error_response
from .web_mount import route_path

MAX_MEME_REQUEST = ((MAX_MEME_BYTES + 2) // 3) * 4 + 65536
MAX_VIDEO_PLAN_REQUEST = 36 * 1024 * 1024
VIDEO_JOB_UPLOAD_PATH = re.compile(r"^/api/v1/operations/videos/[^/]+/uploads$")
DEFAULT_JSON_LIMIT = 2_100_000
AUTH_COOKIE_EXEMPT_ROUTES = frozenset(
    {
        "/api/v1/system/auth/login",
        "/api/v1/system/auth/logout",
        "/api/v1/system/auth/reset-token",
    }
)
CALL_GUARD_EXEMPT_PREFIXES = (
    "/api/v1/agent/private-call",
    "/api/v1/agent/memory/validate",
)


class AuthContext:
    """Shared request/WebSocket authentication and same-origin verification helpers."""

    def __init__(self, settings, sessions):
        self.settings = settings
        self.sessions = sessions

    @staticmethod
    def same_origin(connection) -> bool:
        origin = connection.headers.get("origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == connection.headers.get("host")

    def local_access(self, connection) -> bool:
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
            and self.same_origin(connection)
        )

    def admin_ok(self, connection) -> bool:
        bearer = connection.headers.get("authorization", "")
        return (
            self.local_access(connection)
            or self.sessions.valid(connection.cookies.get(COOKIE_NAME))
            or (bearer.startswith("Bearer ") and self.sessions.authenticate(bearer[7:]))
        )

    def overlay_ok(self, connection) -> bool:
        return secrets.compare_digest(
            connection.cookies.get("tuber_overlay", ""), self.settings.overlay_token
        )

    @staticmethod
    def admin_cookie(response, request, session: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            session,
            httponly=True,
            samesite="strict",
            max_age=COOKIE_MAX_AGE,
            secure=request.url.scheme == "https",
        )

    @staticmethod
    def clear_admin_cookie(response, request) -> None:
        response.delete_cookie(
            COOKIE_NAME, httponly=True, samesite="strict", secure=request.url.scheme == "https"
        )

    async def require_admin(self, request: Request) -> None:
        if not self.admin_ok(request):
            raise HTTPException(401, "请使用本机管理令牌登录")

    async def require_view(self, request: Request) -> None:
        if not (self.admin_ok(request) or self.overlay_ok(request)):
            raise HTTPException(401, "需要管理或播放器凭证")


def register_http_middleware(app, auth: AuthContext, sessions, *, call_guards=None) -> None:
    """Install HTTP security, upload limit, cookie refresh, and call-guard wakeup middleware."""

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        path = route_path(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not auth.same_origin(request):
                return error_response(403, code="CROSS_ORIGIN_FORBIDDEN", message="跨站写入被拒绝")
            uploading = path == "/api/v1/live/uploads" and request.method == "POST"
            meme_upload = path == "/api/v1/persona/memes" and request.method == "POST"
            video_job_upload = request.method == "POST" and bool(VIDEO_JOB_UPLOAD_PATH.fullmatch(path))
            video_plan_upload = request.method == "POST" and path == "/api/v1/operations/videos/plan"
            limit = (
                MAX_UPLOAD
                if uploading
                else MAX_VIDEO_PLAN_REQUEST
                if video_plan_upload
                else MAX_MEME_REQUEST
                if (meme_upload or video_job_upload)
                else DEFAULT_JSON_LIMIT
            )
            reference_upload = request.method == "PUT" and path.startswith(REFERENCE_PREFIX + "/")
            if reference_upload:
                limit = MAX_REFERENCE_BYTES
            if int(request.headers.get("content-length", "0")) > limit:
                return error_response(413, code="PAYLOAD_TOO_LARGE", message="请求过大")
            if not uploading and not reference_upload:
                body = await request.body()
                if len(body) > limit:
                    return error_response(413, code="PAYLOAD_TOO_LARGE", message="请求过大")
        response = await call_next(request)
        if (
            call_guards
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not path.startswith(CALL_GUARD_EXEMPT_PREFIXES)
        ):
            for guard in call_guards.values():
                guard.wake.set()
        cookie = request.cookies.get(COOKIE_NAME)
        if (
            cookie
            and response.status_code < 400
            and path not in AUTH_COOKIE_EXEMPT_ROUTES
            and sessions.needs_cookie_refresh(cookie)
        ):
            auth.admin_cookie(response, request, cookie)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        return response
