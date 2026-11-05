"""Read-only probes for operator-configured model services."""

import asyncio
import time

import httpx
from fastapi import Body, Depends

from .config import validate_api_key
from .http_api import endpoint


def register_provider_diagnostics(app, settings, require_admin):
    def headers_for(name, url, key):
        if key:
            validate_api_key(key)
        broker_key = settings.broker_key_for(url)
        if broker_key:
            return {"X-Tuber-Inference-Key": key or broker_key}
        if url == settings.data.get(name, {}).get("base_url", "").rstrip("/"):
            key = key or settings.provider_key(name)
        return {"Authorization": f"Bearer {key}"} if key else {}

    def probe_base(name, url):
        paired_base = settings.data.get("inference_broker", {}).get("base_url", "").rstrip("/")
        return url + ("/llm" if name == "llm" else "/tts") if url == paired_base else url

    def models_url(url):
        return url + ("/models" if url.endswith("/v1") else "/v1/models")

    async def list_models(base_url, key=None):
        url = endpoint(base_url)
        headers = headers_for("llm", url, key)
        url = probe_base("llm", url)
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            for target in dict.fromkeys(
                (models_url(url), url + "/models", url.removesuffix("/v1") + "/api/tags")
            ):
                try:
                    response = await client.get(target, headers=headers)
                    if response.status_code in (401, 403):
                        return {"models": [], "error": "服务拒绝访问，请检查凭证"}
                    if not response.is_success:
                        continue
                    data = response.json()
                except httpx.RequestError:
                    return {"models": [], "error": "无法连接模型服务，请检查地址"}
                except ValueError:
                    continue
                rows = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
                if isinstance(rows, list):
                    names = (
                        row.get("id") or row.get("name") or row.get("model")
                        for row in rows
                        if isinstance(row, dict)
                    )
                    models = sorted({name for name in names if isinstance(name, str) and name})
                    if models:
                        return {"models": models, "endpoint": target}
        return {"models": [], "error": "服务未提供可用的模型列表"}

    @app.get("/api/providers/models", dependencies=[Depends(require_admin)])
    async def saved_models(base_url: str):
        return await list_models(base_url)

    @app.post("/api/providers/models", dependencies=[Depends(require_admin)])
    async def candidate_models(payload: dict = Body(...)):
        return await list_models(payload.get("base_url"), payload.get("api_key"))

    @app.post("/api/providers/ping", dependencies=[Depends(require_admin)])
    async def ping_endpoints(payload: dict = Body(...)):
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:

            async def probe(name):
                raw_url = payload.get(f"{name}_base_url") or payload.get(f"{name}_url")
                result = {"ok": False, "url": "", "latency_ms": None, "status_code": None}
                if not raw_url:
                    return result | {"detail": "未配置"}
                try:
                    url = endpoint(raw_url)
                    headers = headers_for(name, url, payload.get(f"{name}_key"))
                except ValueError as error:
                    return result | {"detail": str(error)}
                result["url"] = url
                url = probe_base(name, url)
                target = models_url(url) if name == "llm" else url + "/docs" if url.endswith("/tts") else url
                started = time.perf_counter()
                try:
                    response = await client.get(target, headers=headers)
                except httpx.TimeoutException:
                    return result | {"detail": "连接超时"}
                except httpx.RequestError:
                    return result | {"detail": "连接被拒绝或服务不可达"}
                code = response.status_code
                detail = (
                    "连接成功"
                    if response.is_success
                    else "服务拒绝访问，请检查凭证"
                    if code in (401, 403)
                    else f"HTTP {code}"
                )
                return result | {
                    "ok": response.is_success,
                    "status_code": code,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "detail": detail,
                }

            llm, tts = await asyncio.gather(probe("llm"), probe("tts"))
        return {"llm": llm, "tts": tts}
