"""Discover models from the configured Omni gateway using its bound credential."""

import asyncio

import httpx

from ...core.config import responses_profile
from .http_api import api_client, inference_model_card
from .providers import public_error


async def check_provider_diagnostics(settings, runtime):
    omni = settings.data["omni"]
    key = settings.provider_key("omni")

    async def read_card(profile):
        try:
            async with api_client(profile, key) as client:
                card = await inference_model_card(client, profile["model"])
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            return {"available": False, "error": public_error(error)}
        return {"available": True, "context_tokens": card.get("max_model_len")}

    foreground, background = await asyncio.gather(
        runtime.check_foreground_connection(), read_card(responses_profile(omni))
    )
    return {"foreground": foreground, "background": background}


async def saved_provider_models(settings):
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
    return {
        "models": sorted(
            {row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
        )
    }
