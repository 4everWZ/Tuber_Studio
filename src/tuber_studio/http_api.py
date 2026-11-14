"""Operator-configured HTTP endpoints; credentials never become model input."""

import asyncio
import ipaddress
import json
from urllib.parse import urlsplit

import httpx

from .compaction import ContextWindowExceeded

_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


class InferenceTransport(httpx.AsyncHTTPTransport):
    """Reconnect before delivery, and replay safe inference reads before headers."""

    retry_delays = (0.5, 1.5, 3.0)

    async def handle_async_request(self, request):
        replayable = request.method in {"GET", "HEAD"} or (
            request.method == "POST" and request.url.path.rsplit("/", 1)[-1] in {"apply-template", "tokenize"}
        )
        for delay in (*self.retry_delays, None):
            try:
                response = await super().handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if delay is None:
                    raise
            except (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout, httpx.RemoteProtocolError):
                if delay is None or not replayable:
                    raise
            else:
                if delay is None or not replayable or response.status_code not in {502, 504}:
                    return response
                await response.aclose()
            await asyncio.sleep(delay)


def inference_transport(base_url, *, remote=False):
    host = urlsplit(base_url).hostname or ""
    try:
        address = ipaddress.ip_address(host)
        tailnet = any(address in network for network in _TAILSCALE_NETWORKS)
    except ValueError:
        tailnet = host.rstrip(".").endswith(".ts.net")
    if remote or tailnet:
        return InferenceTransport(trust_env=False)
    return None


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("API Base URL无效")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ValueError("API Base URL端口或地址无效") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 for char in value)
    ):
        raise ValueError("API Base URL须为HTTP(S)，凭证须单独填写，不接受查询串或用户名密码")
    if parsed.scheme == "http":
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
            local = (
                address.is_private or address.is_loopback or any(address in network for network in _TAILSCALE_NETWORKS)
            )
        except ValueError:
            local = host in {"localhost", "host.docker.internal", "jetson"} or host.endswith(".local")
        if not local:
            raise ValueError("公网API须使用HTTPS；本地服务可使用HTTP")
    return value.rstrip("/")


def api_client(config, api_key=None):
    base_url = endpoint(config["base_url"])
    transport = inference_transport(base_url)
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": "Bearer " + api_key} if api_key else {},
        timeout=httpx.Timeout(config.get("timeout_seconds", 40), connect=5),
        trust_env=False,
        follow_redirects=False,
        **({"transport": transport} if transport else {}),
    )


async def response_bytes(client, method, path, *, payload=None, timeout=None, limit=2_000_000):
    options = {"json": payload} if payload is not None else {}
    if timeout is not None:
        options["timeout"] = timeout
    async with client.stream(method, path, **options) as response:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("API响应超过允许大小")
        if response.status_code == 400:
            try:
                error = json.loads(data).get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if isinstance(error, dict) and error.get("code") in {
                "context_length_exceeded",
                "context_window_exceeded",
            }:
                raise ContextWindowExceeded("API报告上下文超窗；保留原始记录")
        response.raise_for_status()
        return bytes(data)


async def response_json(client, method, path, *, payload=None, timeout=None):
    raw = await response_bytes(client, method, path, payload=payload, timeout=timeout)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("API未返回有效JSON") from None
    if not isinstance(data, dict):
        raise ValueError("API响应须为JSON对象")
    return data


async def models_health(client, model):
    data = await response_json(client, "GET", "models", timeout=5)
    rows = data.get("data")
    if not isinstance(rows, list) or not any(
        isinstance(row, dict) and row.get("id") == model for row in rows
    ):
        raise ValueError("API模型列表未包含配置的模型ID")
    return {"available": True, "detail": "API模型可达；未执行生成或容量实测"}
