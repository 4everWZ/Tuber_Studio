"""Discover models from the configured Omni gateway using its bound credential."""

import httpx
from fastapi import Depends

from .http_api import api_client


def register_provider_diagnostics(app, settings, require_admin):
    @app.get("/api/providers/models", dependencies=[Depends(require_admin)])
    async def saved_models():
        async with api_client(settings.data["omni"], settings.provider_key("omni")) as client:
            try:
                response = await client.get("models", timeout=5)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                return {"models": [], "error": "无法从 Omni 读取模型列表，请检查地址与凭据"}
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {"models": [], "error": "Omni 返回的模型列表无效"}
        return {"models": sorted({row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)})}
