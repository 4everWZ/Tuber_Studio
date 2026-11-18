"""Mint a path-scoped browser ticket, never a program or Avatar session.

TUBER_PRIVATE_REALTIME_ORIGIN is the browser-reachable gateway origin (HTTPS,
or HTTP loopback for local development). TUBER_PRIVATE_REALTIME_TOKEN_FILE
must hold a dedicated ordinary application token, not a media/admin token.
There is deliberately no fallback to the Studio login or gateway admin secret.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import validate_api_key
from ..http_api import endpoint
from ..operation_continuations import content_hash
from ..store import dump
from .owner_memory import citation, correctable_preference, private_rows, read_stamp


class CallOptions(BaseModel):
    temporary: bool = False
    topic: str = Field(default="", max_length=500)


def private_service():
    try:
        origin = endpoint(os.environ["TUBER_PRIVATE_REALTIME_ORIGIN"])
        url = urlsplit(origin)
        if url.path or (url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}):
            return None
        token = Path(os.environ["TUBER_PRIVATE_REALTIME_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        validate_api_key(token)
        if token:
            return origin, token
    except (KeyError, OSError, ValueError):
        pass
    return None


async def mint_ticket(origin, token):
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
        response = await client.post(
            origin + "/v1/browser-tickets", headers={"Authorization": "Bearer " + token},
            json={"path": "/v1/realtime"},
        )
        response.raise_for_status()
        data = response.json()
    ticket = data.get("ticket") if isinstance(data, dict) else None
    if not isinstance(ticket, str) or ticket == token or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", ticket):
        raise ValueError("invalid ticket")
    return ticket


def call_avatar(settings, character):
    images = {}
    if character.get("avatar_manifest"):
        try:
            entries = settings.assets(character)
        except (TypeError, AttributeError):
            entries = []
        for entry in entries:
            path = entry.get("path", "")
            if not path.startswith("assets/") or Path(path).suffix.lower() not in {".png", ".gif", ".webp", ".jpg", ".jpeg"}:
                continue
            try:
                file = settings.asset_path(path)
            except (OSError, ValueError):
                continue
            images[entry.get("tag")] = "/" + file.relative_to(settings.root).as_posix()
    pair = character["id"] == "whale" and (
        images.get("sing_closed"), images.get("sing_open")
    ) == ("/assets/whale/sing_closed.png", "/assets/whale/sing_open.png")
    return {"still": images.get("sing_closed") if pair else images.get("idle") or next(iter(images.values()), None),
            "speaking": images["sing_open"] if pair else None, "fallback": images.get("idle")}


def register_private_call(app, runtime, require_admin):
    @app.get("/api/chat/private-call", dependencies=[Depends(require_admin)])
    async def availability():
        return {"configured": private_service() is not None}

    @app.post("/api/chat/private-call/ticket", dependencies=[Depends(require_admin)])
    async def create_ticket(options: CallOptions = CallOptions()):
        service = private_service()
        if service is None:
            raise HTTPException(503, "尚未配置独立的私聊语音服务")
        origin, token = service
        grant = runtime.tasks.scope()
        try:
            ticket = await mint_ticket(origin, token)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429:
                raise HTTPException(429, "语音服务繁忙，请稍后重试") from None
            raise HTTPException(502, "语音服务未签发私聊票据，请核对应用凭据与服务地址") from None
        except (httpx.RequestError, ValueError):
            raise HTTPException(502, "私聊语音服务不可用") from None
        if runtime.tasks.scope() != grant:
            raise HTTPException(409, "角色或授权已变化，请重新开始通话")
        character = runtime.character
        rows = private_rows(runtime, options.topic) if options.topic and not options.temporary else []
        # A retained headline or source quote may contain the superseded preference.
        notes = [row["anecdote"] for row in rows]
        return {
            "socket": origin.replace("http", "ws", 1) + "/v1/realtime",
            "ticket": ticket,
            "call_scope": content_hash(grant),
            "character": character["name"],
            "avatar": call_avatar(runtime.settings, character),
            "memory_citations": [citation(row) for row in rows],
            "correction_targets": [row["id"] for row in rows if correctable_preference(row, runtime.daily_memory.private_owner)],
            "memory_use": {"ids": [row["id"] for row in rows], "stamp": read_stamp(runtime, rows)} if rows else None,
            "instructions": "\n".join(text for text in (
                f"你是{character['name']}，正在和号主私下通话。",
                character.get("core", {}).get("identity"), character.get("style", {}).get("instructions"),
            ) if isinstance(text, str))[:6000] + ("\n与本次私聊话题有关的记忆资料：" + dump(notes) if notes else ""),
        }
