"""Operator-owned Bilibili credentials and bounded, read-only account status."""

import asyncio
import hashlib
import json
import math
import time
from urllib.parse import urlsplit

import httpx

from .api_providers import validate_image

AVATAR_HOSTS = {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}


def signature(credentials):
    return hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()


def avatar_url(value):
    if not isinstance(value, str):
        return None
    value = "https:" + value if value.startswith("//") else value
    if value.startswith("http://"):
        value = "https://" + value[7:]
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in AVATAR_HOSTS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path.startswith("/bfs/")
            and ".." not in parsed.path
            and "%" not in parsed.path
            and not any(ord(c) < 33 for c in value)
            and len(value) <= 2048
        )
    except ValueError:
        return None
    return value if valid else None


class BilibiliAccount:
    def __init__(self, comments):
        self.comments = comments
        self.lock = asyncio.Lock()
        self.checked_signature = None
        self.observed = None
        self.avatar = None

    def status(self):
        credential = self.comments.credentials
        configured = {key: bool(credential.get(key)) for key in ("sessdata", "csrf")}
        fresh = self.checked_signature == signature(credential)
        result = (
            dict(self.observed)
            if fresh and self.observed
            else {
                "state": "unverified" if configured["sessdata"] else "not_configured",
                "profile": None,
                "checked_at": None,
                "error": None,
                "warnings": [],
            }
        )
        if result["state"] == "logged_in" and time.time() - result["checked_at"] > 300:
            result["state"] = "unverified"
        return result | {"configured": configured}

    async def configure(self, body):
        if not isinstance(body, dict) or set(body) - {"sessdata", "csrf", "bili_jct", "clear"}:
            raise ValueError("未知账号设置字段")
        if "clear" in body and type(body["clear"]) is not bool:
            raise ValueError("clear须为布尔值")
        if "bili_jct" in body and "csrf" in body and body["bili_jct"] != body["csrf"]:
            raise ValueError("CSRF与bili_jct是同一凭证，请只填写一个值")
        if body.get("clear") and any(body.get(k) for k in ("sessdata", "csrf", "bili_jct")):
            raise ValueError("清除登录与写入凭证不能同时进行")
        async with self.lock:
            before = dict(self.comments.credentials)
            credential = {} if body.get("clear") else dict(before)
            for field in ("sessdata", "csrf"):
                value = body.get(field, body.get("bili_jct") if field == "csrf" else None)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("登录凭证须为字符串")
                    if value.strip():
                        credential[field] = value.strip()
            await self.comments.replace_credentials(credential, expected=before)
            self.observed = self.checked_signature = self.avatar = None
            return self.status()

    async def refresh(self):
        async with self.lock:
            credential = dict(self.comments.credentials)
            if not credential.get("sessdata"):
                return self.status()
            adapter = self.comments.adapter
            if not hasattr(adapter, "account_profile"):
                return self.status() | {"error": "当前平台适配器尚未提供账号资料读取"}
            result = {"profile": None, "checked_at": time.time(), "error": None, "warnings": []}
            try:
                async with asyncio.timeout(15):
                    profile = await adapter.account_profile()
                uid, name = profile.get("uid"), profile.get("name")
                if (
                    not isinstance(uid, str)
                    or not uid.isdecimal()
                    or int(uid) <= 0
                    or not isinstance(name, str)
                    or not 0 < len(name) <= 100
                ):
                    raise ValueError("平台账号资料无效")
                coins, fans = profile.get("coins"), profile.get("fans")
                if coins is not None and (
                    type(coins) not in {int, float} or not math.isfinite(coins) or coins < 0
                ):
                    raise ValueError("平台硬币数量无效")
                if fans is not None and (type(fans) is not int or fans < 0):
                    raise ValueError("平台粉丝数量无效")
                result.update(
                    state="logged_in",
                    profile={
                        "uid": uid,
                        "name": name,
                        "avatar_url": avatar_url(profile.get("avatar_url")),
                        "coins": coins,
                        "fans": fans,
                    },
                )
                if fans is None:
                    result["warnings"].append("粉丝数暂时未取得")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                kind = getattr(error, "kind", "")
                state = (
                    "expired" if kind == "permission" else "restricted" if kind == "restricted" else "error"
                )
                result.update(
                    state=state,
                    error={
                        "expired": "B站登录已失效，请在设置中更新凭证",
                        "restricted": "B站限制读取，请稍后重试",
                        "error": "账号资料读取失败，请稍后重试",
                    }[state],
                )
            if self.comments.credentials != credential or self.comments.adapter is not adapter:
                return self.status()
            self.checked_signature, self.observed, self.avatar = signature(credential), result, None
            if result["state"] == "logged_in":
                self.comments.verified_account_id = result["profile"]["uid"]
                self.comments.verified_credential = credential
            return self.status()

    async def avatar_image(self):
        current = self.status()
        url = avatar_url((current.get("profile") or {}).get("avatar_url"))
        if not url:
            raise ValueError("当前账号没有可用头像")
        key = (signature(self.comments.credentials), url)
        if self.avatar and self.avatar[0] == key:
            return self.avatar[1:]
        # Never reuse the platform client: its login cookies must not reach a CDN.
        async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError("账号头像超过大小上限")
                mime = response.headers.get("content-type", "").split(";")[0]
                if mime == "image/gif" and bytes(data).startswith((b"GIF87a", b"GIF89a")):
                    pass
                else:
                    validate_image(bytes(data), mime)
        if key != (
            signature(self.comments.credentials),
            avatar_url((self.status().get("profile") or {}).get("avatar_url")),
        ):
            raise ValueError("账号已变更，请刷新头像")
        self.avatar = (key, bytes(data), mime)
        return bytes(data), mime
