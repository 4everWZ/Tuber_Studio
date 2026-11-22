"""Unified `/api/v1/*` error envelope `{code, message, details}` and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core.models import StrictModel
from ..infra.inference.providers import public_error
from .web_mount import route_path

STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    412: "PRECONDITION_FAILED",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}

STATUS_MESSAGES: dict[int, str] = {
    400: "请求参数或操作无效",
    401: "需要登录凭证",
    403: "禁止访问",
    404: "资源不存在",
    405: "不支持该请求方法",
    409: "当前状态冲突，请刷新后重试",
    412: "前置状态已变化，请重新读取",
    413: "请求内容过大",
    422: "请求参数校验失败",
    429: "请求过于频繁，请稍后再试",
    500: "服务内部错误",
    502: "上游服务响应异常",
    503: "服务暂不可用",
}


class ErrorEnvelope(StrictModel):
    """Structured error response body emitted on `/api/v1/*` non-2xx responses."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class ApiError(Exception):
    """Domain-safe HTTP error carrying an explicit machine code and optional structured details."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def default_error_code(status_code: int) -> str:
    return STATUS_CODES.get(status_code, f"HTTP_{status_code}")


def default_error_message(status_code: int) -> str:
    return STATUS_MESSAGES.get(status_code, "请求处理失败")


def build_error_payload(
    status_code: int,
    *,
    code: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ErrorEnvelope(
        code=code or default_error_code(status_code),
        message=message or default_error_message(status_code),
        details=details,
    ).model_dump()


def error_response(
    status_code: int,
    *,
    code: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        build_error_payload(status_code, code=code, message=message, details=details),
        status_code=status_code,
        headers=headers,
    )


def _normalize_http_exception(exc: StarletteHTTPException) -> tuple[str, str, dict[str, Any] | None]:
    raw = exc.detail
    fallback_code = default_error_code(exc.status_code)
    fallback_msg = default_error_message(exc.status_code)
    if isinstance(raw, dict):
        code = str(raw.get("code") or fallback_code)
        msg = str(raw.get("message") or raw.get("detail") or fallback_msg)
        raw_details = raw.get("details")
        if raw_details is None:
            extra = {k: v for k, v in raw.items() if k not in {"code", "message", "detail"}}
            details = extra or None
        elif isinstance(raw_details, dict):
            details = raw_details
        else:
            details = {"value": raw_details}
        return code, msg, details
    if isinstance(raw, list):
        parts = [
            str(item.get("message") or item.get("msg"))
            for item in raw
            if isinstance(item, dict) and (item.get("message") or item.get("msg"))
        ]
        msg = "；".join(parts) if parts else fallback_msg
        return fallback_code, msg, {"errors": raw}
    if isinstance(raw, str) and raw:
        return fallback_code, raw, None
    return fallback_code, fallback_msg, None


def _is_api_request(request: Request) -> bool:
    return route_path(request).startswith("/api/")


def install_error_handlers(app) -> None:
    """Install unified `{code, message, details}` handlers for API routes and ValueError."""

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError):
        return error_response(
            error.status_code,
            code=error.code,
            message=error.message,
            details=error.details,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        errors = [
            {
                "field": ".".join(str(part) for part in item.get("loc", ()) if part != "body"),
                "loc": list(item.get("loc", ())),
                "message": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
            for item in error.errors()
        ]
        if not _is_api_request(request):
            return JSONResponse({"detail": errors}, status_code=422)
        summary = "；".join(item["message"] for item in errors[:3] if item["message"]) or default_error_message(422)
        return error_response(
            422,
            code="VALIDATION_ERROR",
            message=summary,
            details={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, error: StarletteHTTPException):
        headers = getattr(error, "headers", None)
        if not _is_api_request(request):
            return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=headers)
        code, message, details = _normalize_http_exception(error)
        return error_response(
            error.status_code,
            code=code,
            message=message,
            details=details,
            headers=headers,
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, error: ValueError):
        message = public_error(error)
        if not _is_api_request(request):
            return JSONResponse({"detail": message}, status_code=400)
        next_at = getattr(error, "next_at", None)
        details = {"next_at": next_at} if next_at is not None else None
        return error_response(400, code="BAD_REQUEST", message=message, details=details)
