"""Owner-only control plane for accepted private-call avatars."""

import asyncio
import hashlib
import hmac
import json
import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .private_call import private_service
from .private_call_guard import register_call_guards

PREFIX = "/api/chat/private-call/avatars"
ID = r"[A-Za-z0-9_-]{16,80}"


class Attach(BaseModel):
    profile: Literal["original", "q"]
    selection: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_session_id: str = Field(pattern="^" + ID + "$")
    guard: str | None = Field(default=None, pattern="^" + ID + "$")


def accepted_pair(runtime, origin):
    try:
        data = json.loads((runtime.settings.runtime / "private-call-avatars.json").read_text(encoding="utf-8"))
        if data["character_id"] != runtime.character["id"] or data["origin"] != origin:
            return {}
        profiles = data["profiles"]
        pair = {}
        for key in ("original", "q"):
            profile = profiles[key]
            identity = profile["avatar_id"]
            driver = profile.get("driver", "ditto")
            if (profile["accepted"] is not True or driver not in ("ditto", "inochi2d")
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identity)):
                return {}
            pair[key] = (identity, driver)
        if pair["original"][0] != pair["q"][0]:
            return pair
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return {}


async def upstream(service, method, path, **options):
    origin, token = service
    headers = options.pop("headers", {}) | {"Authorization": "Bearer " + token}
    try:
        async with httpx.AsyncClient(timeout=22, trust_env=False) as client:
            response = await client.request(method, origin + path, headers=headers, **options)
    except httpx.RequestError:
        raise HTTPException(503, "Avatar 服务不可用") from None
    if not response.is_success:
        raise HTTPException(response.status_code if response.status_code in (404, 409, 410, 429) else 502,
                            "Avatar 会话不可用或服务繁忙")
    return response


def record(response):
    try:
        data = response.json()
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    raise HTTPException(502, "Avatar 服务回包无效")


async def ready(service, pair):
    async def check(key, driver):
        data = record(await upstream(service, "GET", "/v1/avatar/avatars/" + key))
        return data.get("id") == key and data.get("state") == "ready" and data.get("driver") == driver

    return all(await asyncio.gather(*(check(identity, driver) for identity, driver in pair.values())))


def register_private_avatar(app, runtime, require_admin):
    router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_admin)])
    guards = register_call_guards(app, runtime, require_admin)

    def context():
        service = private_service()
        pair = accepted_pair(runtime, service[0]) if service else {}
        if not pair:
            raise HTTPException(409, "两套通话形象尚未就绪")
        scope = json.dumps([service[0], pair, runtime.tasks.scope()], sort_keys=True).encode()
        key = hmac.digest(service[1].encode(), b"Studio private avatar\0" + scope, hashlib.sha256)
        return service, pair, key

    def sign(key, identity):
        return hmac.digest(key, identity.encode(), hashlib.sha256).hex()

    def owned(handle):
        identity, _, tag = handle.partition(".")
        service, _, key = context()
        if not re.fullmatch(ID, identity) or not re.fullmatch(r"[a-f0-9]{64}", tag) or not hmac.compare_digest(
                sign(key, identity), tag):
            raise HTTPException(404, "未知通话")
        return service, "/v1/avatar/sessions/" + identity

    @router.get("")
    async def choices():
        try:
            service, pair, key = context()
            if await ready(service, pair) and context()[2] == key:
                return {"profiles": list(pair), "selection": sign(key, "select")}
        except HTTPException:
            pass
        return {"profiles": []}

    @router.post("")
    async def attach(body: Attach):
        service, pair, key = context()
        guard = guards.get(body.guard)
        if body.guard and (not guard or guard.state() != "valid" or guard.close_media):
            raise HTTPException(409, "通话记忆订阅已失效")
        if not hmac.compare_digest(body.selection, sign(key, "select")) or not await ready(service, pair):
            raise HTTPException(409, "请重新选择已验收的通话形象")
        if context()[2] != key:
            raise HTTPException(409, "角色或授权已变化")
        avatar, driver = pair[body.profile]
        data = record(await upstream(service, "POST", "/v1/avatar/sessions", json={
            "avatar_id": avatar, "source": {"type": "realtime", "session_id": body.source_session_id}}))
        identity = data.get("id")
        if not isinstance(identity, str) or not re.fullmatch(ID, identity):
            raise HTTPException(502, "Avatar 会话标识无效")
        path = "/v1/avatar/sessions/" + identity
        async def release_media():
            try:
                await upstream(service, "DELETE", path)
            except HTTPException:
                pass

        try:
            if (data.get("source_session_id") != body.source_session_id or data.get("avatar_id") != avatar
                    or data.get("state") != "active" or data.get("format", {}).get("transport") != "whep"
                    or data["format"].get("driver") != driver):
                raise HTTPException(502, "Avatar 未绑定本次实时通话")
            ticket = record(await upstream(service, "POST", "/v1/browser-tickets",
                                           json={"path": path + "/events"})).get("ticket")
            if (not isinstance(ticket, str) or not re.fullmatch(ID, ticket) or ticket == service[1]
                    or context()[2] != key or (guard and (guard.state() != "valid" or guard.close_media))):
                raise HTTPException(409, "无法订阅本次通话")
            if guard:
                guard.close_media = release_media
            return {"handle": identity + "." + sign(key, identity), "generation": data["generation"],
                    "events": service[0].replace("http", "ws", 1) + path + "/events?ticket=" + ticket}
        except BaseException:
            await upstream(service, "DELETE", path)
            raise

    @router.get("/{handle}")
    async def heartbeat(handle: str):
        service, path = owned(handle)
        return {"state": record(await upstream(service, "GET", path)).get("state")}

    @router.delete("/{handle}", status_code=204)
    async def close(handle: str):
        service, path = owned(handle)
        await upstream(service, "DELETE", path)
        return Response(status_code=204)

    @router.post("/{handle}/whep")
    async def offer(handle: str, request: Request):
        service, path = owned(handle)
        body = await request.body()
        if request.headers.get("content-type") != "application/sdp" or not 0 < len(body) <= 131072:
            raise HTTPException(400, "无效的 WHEP offer")
        reply = await upstream(service, "POST", path + "/whep", content=body,
                               headers={"Content-Type": "application/sdp"})
        location = reply.headers.get("Location", "")
        peer = location.rsplit("/", 1)[-1]
        origin = reply.headers.get("X-Avatar-Timeline-Origin-Ms", "")
        if not re.fullmatch(ID, peer) or location != path + "/whep/" + peer or not re.fullmatch(r"[0-9]{1,15}", origin):
            raise HTTPException(502, "WHEP 时间线或订阅标识无效")
        return Response(reply.content, status_code=201, media_type="application/sdp", headers={
            "Location": f"{PREFIX}/{handle}/whep/{peer}", "X-Avatar-Timeline-Origin-Ms": origin})

    @router.delete("/{handle}/whep/{peer}", status_code=204)
    async def unsubscribe(handle: str, peer: str):
        service, path = owned(handle)
        if not re.fullmatch(ID, peer):
            raise HTTPException(404)
        await upstream(service, "DELETE", path + "/whep/" + peer)
        return Response(status_code=204)

    app.include_router(router)
