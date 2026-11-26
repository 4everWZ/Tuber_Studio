"""Operator-owned Bilibili credentials and bounded, read-only account status."""

import asyncio
import hashlib
import io
import json
import math
import re
import time
from secrets import token_urlsafe
from urllib.parse import urlsplit

import httpx
import qrcode

from ....core.config import atomic_json
from ....core.models import BilibiliOwner
from ....core.validation import linked
from ...inference.validation import validate_image
from .client import BilibiliAccountExpired
from .login import BilibiliPassport, credential_value, validate_credentials

AVATAR_HOSTS = {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}
PROFILE_CACHE_KEY = "bilibili_account_last_profile"
COMMENT_CAPABILITIES_PREFIX = "bilibili_comment_capabilities:"
ACCOUNT_CHECK_SECONDS = 15 * 60
QR_SECONDS = 180
QR_POLL_SECONDS = 3


def account_comment_capabilities(store, account_id, profile=None):
    """Return conservative comment-sending capabilities (text/image/GIF) for a Bilibili account."""
    uid = str(account_id or "").strip()
    resolved_profile = profile if isinstance(profile, dict) else None
    if resolved_profile is None and store is not None and uid:
        cached = store.get(PROFILE_CACHE_KEY, {})
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("profile"), dict)
            and str(cached["profile"].get("uid") or "") == uid
        ):
            resolved_profile = cached["profile"]
    vip_active = (
        resolved_profile.get("vip_active")
        if isinstance(resolved_profile, dict) and isinstance(resolved_profile.get("vip_active"), bool)
        else None
    )
    level = (
        resolved_profile.get("level")
        if isinstance(resolved_profile, dict) and type(resolved_profile.get("level")) is int
        else None
    )
    override = store.get(COMMENT_CAPABILITIES_PREFIX + uid, {}) if (store is not None and uid) else {}
    if not isinstance(override, dict):
        override = {}
    comment_allowed = False if level == 0 else True
    comment_image_allowed = False if override.get("image_allowed") is False else True
    gif_blocked_by_vip = vip_active is False
    gif_blocked_by_runtime = override.get("gif_allowed") is False
    comment_gif_allowed = bool(
        comment_allowed and comment_image_allowed and not gif_blocked_by_vip and not gif_blocked_by_runtime
    )
    if not comment_allowed:
        reason = "当前 B 站账号等级不足（Lv0），暂无法发送评论"
    elif not comment_image_allowed:
        reason = str(override.get("reason") or "当前账号暂无评论配图发送权限")
    elif not comment_gif_allowed:
        reason = (
            "当前账号未开通大会员，无评论动图（GIF）发送权限，仅支持静态图或纯文字"
            if gif_blocked_by_vip
            else str(override.get("reason") or "当前账号暂无评论动图（GIF）发送权限，仅支持静态图或纯文字")
        )
    else:
        reason = ""
    return {
        "vip_active": vip_active,
        "level": level,
        "comment_allowed": comment_allowed,
        "comment_image_allowed": comment_image_allowed,
        "comment_gif_allowed": comment_gif_allowed,
        "reason": reason,
    }


def record_comment_capability_restriction(store, account_id, *, gif_allowed=None, image_allowed=None, reason=None):
    """Persist runtime-observed platform restrictions (e.g. code 49044 / 49045) per Bilibili UID."""
    uid = str(account_id or "").strip()
    if store is None or not uid:
        return None
    key = COMMENT_CAPABILITIES_PREFIX + uid
    prev = store.get(key, {})
    updated = dict(prev) if isinstance(prev, dict) else {}
    if gif_allowed is not None:
        updated["gif_allowed"] = bool(gif_allowed)
    if image_allowed is not None:
        updated["image_allowed"] = bool(image_allowed)
    if isinstance(reason, str) and reason.strip():
        updated["reason"] = reason.strip()[:240]
    updated["updated_at"] = time.time()
    store.set(key, updated)
    return updated


def signature(credentials):
    cookies = {key: credentials[key] for key in ("sessdata", "csrf", "uid") if key in credentials}
    return hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).hexdigest()


def cached_account_actor(store, credentials, account_id):
    """Identify the speaking account without a network read or persona-name guess."""
    actor = {"platform": "bilibili", "account_uid": str(account_id) if account_id else None,
             "account_display_name": None, "account_bio": None}
    cached = store.get(PROFILE_CACHE_KEY, {})
    if not isinstance(cached, dict):
        return actor
    profile = cached.get("profile")
    checked_at = cached.get("checked_at")
    if (
        cached.get("format") == 1
        and cached.get("credential_signature") == signature(credentials)
        and isinstance(profile, dict)
        and isinstance(profile.get("uid"), str) and profile["uid"]
        and (actor["account_uid"] is None or profile["uid"] == actor["account_uid"])
        and isinstance(profile.get("name"), str)
        and 0 < len(profile["name"].strip()) <= 100
        and type(checked_at) in {int, float}
        and math.isfinite(checked_at)
        and checked_at > 0
    ):
        actor["account_uid"] = profile["uid"]
        actor["account_display_name"] = profile["name"].strip()
        actor["account_bio"] = profile.get("bio") if isinstance(profile.get("bio"), str) else None
    return actor


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


def space_uid(value):
    value = value.strip()
    if re.fullmatch(r"[1-9][0-9]{0,19}", value):
        return value
    if value.startswith("space.bilibili.com/"):
        value = "https://" + value
    try:
        url = urlsplit(value)
        if url.scheme in {"http", "https"} and url.netloc.lower() == "space.bilibili.com":
            match = re.fullmatch(r"/([1-9][0-9]{0,19})/?", url.path)
            if match:
                return match[1]
    except ValueError:
        pass
    raise ValueError("请输入 B 站 UID 或个人空间链接")


class BilibiliAccount:
    def __init__(self, settings, store, adapter=None):
        self.settings, self.store = settings, store
        self.credential_path = settings.runtime / "bilibili-credentials.json"
        if linked(self.credential_path):
            raise ValueError("账号凭证文件不能使用链接")
        credentials = (
            json.loads(self.credential_path.read_text(encoding="utf-8"))
            if self.credential_path.exists()
            else {}
        )
        self.credentials = credentials
        if adapter is None:
            from .browsing import BrowsingAdapter

            adapter = BrowsingAdapter(credentials)
            self.injected = False
        else:
            self.injected = True
        self.adapter = adapter
        self.verified_account_id = ""
        self.verified_credential = None
        self.credential_lock = asyncio.Lock()
        self.credential_changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.checked_signature = None
        self.observed = None
        self.avatar = None
        self.passport = BilibiliPassport()
        self.changed = asyncio.Event()
        self.worker = None
        self.qr = None
        self.qr_expiry = None
        self.renewal = {"state": "idle", "checked_at": None, "renewed_at": None}
        cached = self.store.get(PROFILE_CACHE_KEY, {})
        if (
            isinstance(cached, dict)
            and cached.get("format") == 1
            and cached.get("credential_signature") == signature(self.credentials)
            and isinstance(cached.get("profile"), dict)
            and type(cached.get("checked_at")) in {int, float}
            and math.isfinite(cached["checked_at"])
            and cached["checked_at"] > 0
        ):
            self.checked_signature = cached["credential_signature"]
            self.observed = {
                "state": "logged_in",
                "profile": cached["profile"],
                "checked_at": cached["checked_at"],
                "attempted_at": None,
                "error": None,
                "warnings": [],
            }

    def status(self):
        credential = self.credentials
        configured = {key: bool(credential.get(key)) for key in ("sessdata", "csrf")}
        fresh = self.checked_signature == signature(credential)
        result = (
            dict(self.observed)
            if fresh and self.observed
            else {
                "state": "unverified" if configured["sessdata"] else "not_configured",
                "profile": None,
                "checked_at": None,
                "attempted_at": None,
                "error": None,
                "warnings": [],
            }
        )
        checked = result["checked_at"]
        renewal = dict(self.renewal)
        if credential.get("pending_confirmation"):
            renewal["state"] = "confirmation_pending"
        elif not credential.get("refresh_token"):
            renewal["state"] = "manual" if configured["sessdata"] else "idle"
        elif renewal["state"] == "idle":
            renewal["state"] = "available"
        uid = (result.get("profile") or {}).get("uid") or credential.get("uid") or self.verified_account_id
        return result | {
            "configured": configured,
            "renewal": renewal,
            "comment_capabilities": account_comment_capabilities(self.store, uid, result.get("profile")),
            "verification_stale": (
                checked is None or time.time() - checked > ACCOUNT_CHECK_SECONDS or result["state"] != "logged_in"
            ),
        }

    async def owner_profile(self, account=None):
        status = self.status()
        if account is None:
            if status["verification_stale"] and status["configured"]["sessdata"]:
                status = await self.refresh()
            if status["state"] != "logged_in" or not status["profile"]:
                raise ValueError("请先在账号与登录中核验当前 B 站账号")
            profile = status["profile"]
        else:
            uid = space_uid(account)
            profile = status["profile"] if status["state"] == "logged_in" else None
            if not profile or profile["uid"] != uid:
                lookup = getattr(self.adapter, "public_profile", None)
                if lookup is None:
                    raise ValueError("当前平台适配器无法查询公开账号资料")
                try:
                    async with asyncio.timeout(12):
                        profile = await lookup(uid)
                except TimeoutError:
                    raise ValueError("查询 B 站账号超时，请稍后重试") from None
                if profile.get("uid") != uid:
                    raise ValueError("B 站返回的资料与所填 UID 不一致")
        name = profile.get("name")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= 100 or re.search(r"[\x00-\x1f\x7f]", name):
            raise ValueError("B 站返回的账号昵称无效")
        return BilibiliOwner(uid=profile["uid"], name=name.strip(), avatar_url=avatar_url(profile.get("avatar_url")))

    async def configure(self, body):
        if not isinstance(body, dict) or set(body) - {"sessdata", "csrf", "bili_jct", "refresh_token", "clear"}:
            raise ValueError("未知账号设置字段")
        if "clear" in body and type(body["clear"]) is not bool:
            raise ValueError("clear须为布尔值")
        if "bili_jct" in body and "csrf" in body and body["bili_jct"] != body["csrf"]:
            raise ValueError("CSRF与bili_jct是同一凭证，请只填写一个值")
        if body.get("clear") and any(body.get(k) for k in ("sessdata", "csrf", "bili_jct", "refresh_token")):
            raise ValueError("清除登录与写入凭证不能同时进行")
        async with self.lock:
            before = dict(self.credentials)
            credential = {} if body.get("clear") else dict(before)
            for field in ("sessdata", "csrf", "refresh_token"):
                value = body.get(field, body.get("bili_jct") if field == "csrf" else None)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("登录凭证须为字符串")
                    if value.strip():
                        credential[field] = credential_value(value.strip())
            if credential.get("refresh_token") != before.get("refresh_token"):
                credential.pop("pending_confirmation", None)
            if any(credential.get(key) != before.get(key) for key in ("sessdata", "csrf")):
                credential.pop("uid", None)
                credential.pop("pending_confirmation", None)
                if not (body.get("refresh_token") or "").strip():
                    credential.pop("refresh_token", None)
            if credential.get("refresh_token") and not all(credential.get(key) for key in ("sessdata", "csrf")):
                raise ValueError("续期令牌需与同次登录的 SESSDATA 和 bili_jct 一起保存")
            await self.replace_credentials(credential, expected=before)
            if credential != before or body.get("clear"):
                self.renewal = {"state": "idle", "checked_at": None, "renewed_at": None}
                self._clear_qr()
                if signature(before) != signature(credential) or body.get("clear"):
                    self.observed = self.checked_signature = self.avatar = None
                    self.store.set(PROFILE_CACHE_KEY, None)
                self.changed.set()
            return self.status()

    async def open(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self._maintain())

    @property
    def credentials_configured(self):
        return bool(self.credentials.get("sessdata") and self.credentials.get("csrf"))

    async def replace_credentials(self, credential, *, expected=None, verified_uid=None):
        """Atomically replace the account credential store against its observed version."""
        validate_credentials(credential)
        from .browsing import BrowsingAdapter

        async with self.credential_lock:
            if expected is not None and self.credentials != expected:
                raise ValueError("账号凭证已被另一项设置更新，请刷新后重新保存")
            if credential == self.credentials:
                return
            if all(credential.get(key) == self.credentials.get(key) for key in ("sessdata", "csrf", "uid")):
                atomic_json(self.credential_path, credential, private=True)
                if self.verified_credential == self.credentials:
                    self.verified_credential = dict(credential)
                self.credentials = dict(credential)
                return
            replacement = BrowsingAdapter(credential)
            try:
                atomic_json(self.credential_path, credential, private=True)
            except BaseException:
                await replacement.close()
                raise
            old = self.adapter
            self.credentials, self.adapter, self.injected = dict(credential), replacement, False
            self.verified_account_id = verified_uid or ""
            self.verified_credential = dict(credential) if verified_uid else None
            self.credential_changed.set()
            if hasattr(old, "close"):
                await old.close()

    async def account_id(self):
        adapter = self.adapter
        credential = dict(self.credentials)
        if hasattr(adapter, "self_uid"):
            account_id = str(await adapter.self_uid())
            if self.adapter is not adapter or self.credentials != credential:
                raise ValueError("核验期间账号已变更，请重试")
            self.verified_account_id = account_id
            self.verified_credential = credential
            return account_id
        if self.injected:
            return "injected-test-account"
        raise ValueError("平台适配器不能核验当前账号")

    def display_account_id(self):
        return self.verified_account_id if self.verified_credential == self.credentials else ""

    def context_account_id(self):
        return self.display_account_id() or cached_account_actor(self.store, self.credentials, None).get("account_uid") or ""

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        self._clear_qr()
        await self.passport.close()
        if hasattr(self.adapter, "close"):
            await self.adapter.close()

    def _clear_qr(self):
        if self.qr_expiry is not None:
            self.qr_expiry.cancel()
            self.qr_expiry = None
        self.qr = None

    async def _maintain(self):
        delay = 30
        while True:
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), delay)
            except TimeoutError:
                credential = self.credentials
                if credential.get("sessdata"):
                    try:
                        await self.refresh(maintain=True)
                    except OSError:
                        self.renewal["state"] = "error"
                retry = self.renewal["state"] == "error" and self.status()["state"] != "expired"
                delay = 120 if retry else ACCOUNT_CHECK_SECONDS
            else:
                delay = 30

    async def create_login(self):
        async with self.lock:
            key, url = await self.passport.generate_qr()
            image = io.BytesIO()
            qrcode.make(url, box_size=5).save(image, format="PNG")
            self._clear_qr()
            self.qr = {
                "id": token_urlsafe(24), "key": key, "image": image.getvalue(), "state": "waiting",
                "expires_at": time.monotonic() + QR_SECONDS, "next_poll": 0,
                "before": dict(self.credentials), "credential": None,
            }
            self.qr_expiry = asyncio.get_running_loop().call_later(QR_SECONDS, self._clear_qr)
            return {"id": self.qr["id"], "expires_in": QR_SECONDS}

    async def cancel_login(self, identity):
        async with self.lock:
            if self._login(identity) is not None:
                self._clear_qr()
            return {"state": "cancelled"}

    def _login(self, identity):
        if self.qr is None or self.qr["id"] != identity or self.qr["expires_at"] <= time.monotonic():
            return None
        return self.qr

    def login_image(self, identity):
        login = self._login(identity)
        if login is None or login["state"] in {"complete", "expired"}:
            raise ValueError("扫码登录已结束，请获取新二维码")
        return login["image"]

    async def poll_login(self, identity):
        async with self.lock:
            login = self._login(identity)
            if login is None:
                return {"state": "expired"}
            if login["state"] in {"complete", "expired"}:
                result = {"state": login["state"]}
                return result | {"account": self.status()} if login["state"] == "complete" else result
            if login["next_poll"] > time.monotonic():
                return {"state": login["state"]}
            login["next_poll"] = time.monotonic() + QR_POLL_SECONDS
            if login["credential"] is None:
                state, credential = await self.passport.poll_qr(login["key"])
                if self.qr is not login:
                    return {"state": "expired"}
                if state != "complete":
                    login["state"] = state
                    return {"state": state}
                login["state"] = "scanned"
                login["credential"] = credential
                login["expires_at"] = time.monotonic() + QR_SECONDS
                self.qr_expiry.cancel()
                self.qr_expiry = asyncio.get_running_loop().call_later(QR_SECONDS, self._clear_qr)
            credential = login["credential"]
            uid = await self.passport.verified_uid(credential)
            if self._login(identity) is not login:
                return {"state": "expired"}
            if uid != credential["uid"]:
                self._clear_qr()
                raise ValueError("扫码凭证与平台账号不一致")
            await self.replace_credentials(credential, expected=login["before"], verified_uid=uid)
            login.update(state="complete", credential=None, before=None, key=None, image=None)
            self.observed = self.checked_signature = self.avatar = None
            self.renewal = {"state": "available", "checked_at": None, "renewed_at": None}
            self.store.set(PROFILE_CACHE_KEY, None)
            self.changed.set()
            return {"state": "complete", "account": await self._refresh_profile()}

    async def _renew(self):
        credential = dict(self.credentials)
        if not all(credential.get(field) for field in ("sessdata", "csrf", "refresh_token")):
            return
        previous = self.status()
        self.renewal.update(state="checking", checked_at=time.time())
        try:
            if not credential.get("pending_confirmation"):
                needed, timestamp = await self.passport.refresh_info(credential)
                if not needed:
                    self.renewal["state"] = "available"
                    return
                uid = await self.passport.verified_uid(credential)
                if credential.get("uid") not in {None, uid}:
                    raise ValueError("旧登录凭证与平台账号不一致")
                replacement = await self.passport.renew(credential, timestamp)
                if replacement["uid"] != uid:
                    raise ValueError("续期返回了不同账号，已保留原登录")
                replacement["pending_confirmation"] = credential["refresh_token"]
                await self.replace_credentials(replacement, expected=credential)
                credential = replacement
            uid = await self.passport.verified_uid(credential)
            if uid != credential["uid"]:
                raise ValueError("续期登录账号核验失败")
            await self.passport.confirm(credential)
            confirmed = {key: value for key, value in credential.items() if key != "pending_confirmation"}
            await self.replace_credentials(confirmed, expected=credential)
            self.renewal.update(state="available", renewed_at=time.time())
        except (ValueError, TimeoutError, OSError):
            self.renewal["state"] = "error"
        finally:
            current = self.credentials
            if (
                self.checked_signature != signature(current)
                and previous.get("profile") and previous["profile"]["uid"] == current.get("uid")
            ):
                self.checked_signature, self.observed = signature(current), previous
                self.store.set(PROFILE_CACHE_KEY, {
                    "format": 1, "credential_signature": self.checked_signature,
                    "profile": previous["profile"], "checked_at": previous["checked_at"],
                })

    async def refresh(self, *, maintain=False):
        async with self.lock:
            if maintain:
                await self._renew()
            return await self._refresh_profile()

    async def _refresh_profile(self):
        credential = dict(self.credentials)
        if not credential.get("sessdata"):
            return self.status()
        adapter = self.adapter
        if not hasattr(adapter, "account_profile"):
            return self.status() | {"error": "当前平台适配器尚未提供账号资料读取"}
        previous = self.status()
        result = {
            "profile": None,
            "checked_at": None,
            "attempted_at": time.time(),
            "error": None,
            "warnings": [],
        }
        try:
            async with asyncio.timeout(15):
                profile = await adapter.account_profile()
            uid, name = profile.get("uid"), profile.get("name")
            if (
                not isinstance(uid, str)
                or not uid.isdecimal()
                or int(uid) <= 0
                or credential.get("uid") not in {None, uid}
                or not isinstance(name, str)
                or not 0 < len(name) <= 100
            ):
                raise ValueError("平台账号资料无效")
            coins, fans = profile.get("coins"), profile.get("fans")
            bio = profile.get("bio")
            if bio is not None and (not isinstance(bio, str) or len(bio) > 4000):
                raise ValueError("平台账号简介无效")
            if coins is not None and (
                type(coins) not in {int, float} or not math.isfinite(coins) or coins < 0
            ):
                raise ValueError("平台硬币数量无效")
            if fans is not None and (type(fans) is not int or fans < 0):
                raise ValueError("平台粉丝数量无效")
            vip_active = profile.get("vip_active")
            if vip_active is not None and type(vip_active) is not bool:
                vip_active = None
            vip_type = profile.get("vip_type")
            if type(vip_type) is not int or vip_type < 0:
                vip_type = None
            level = profile.get("level")
            if type(level) is not int or not 0 <= level <= 10:
                level = None
            profile_payload = {
                "uid": uid,
                "name": name,
                "avatar_url": avatar_url(profile.get("avatar_url")),
                "coins": coins,
                "fans": fans,
                "bio": bio,
            }
            if vip_active is not None:
                profile_payload["vip_active"] = vip_active
            if vip_type is not None:
                profile_payload["vip_type"] = vip_type
            if level is not None:
                profile_payload["level"] = level
            result.update(
                state="logged_in",
                checked_at=time.time(),
                profile=profile_payload,
            )
            if vip_active is True:
                prev_cap = self.store.get(COMMENT_CAPABILITIES_PREFIX + uid)
                if isinstance(prev_cap, dict) and prev_cap.get("gif_allowed") is False:
                    updated_cap = dict(prev_cap)
                    updated_cap.pop("gif_allowed", None)
                    updated_cap.pop("reason", None)
                    updated_cap["updated_at"] = time.time()
                    self.store.set(COMMENT_CAPABILITIES_PREFIX + uid, updated_cap)
            if fans is None:
                result["warnings"].append("粉丝数暂时未取得")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state = "expired" if isinstance(error, BilibiliAccountExpired) else getattr(error, "kind", "")
            if state not in {"expired", "restricted"}:
                state = "error"
            result.update(
                state=state,
                error={
                    "expired": "B站登录已失效，请在设置中重新扫码",
                    "restricted": "B站限制读取，请稍后重试",
                    "error": "账号资料读取失败，请稍后重试",
                }[state],
            )
            if state != "expired" and previous.get("profile"):
                result.update(profile=previous["profile"], checked_at=previous["checked_at"])
        if self.credentials != credential or self.adapter is not adapter:
            return self.status()
        self.checked_signature, self.observed = signature(credential), result
        if result["state"] == "logged_in":
            self.verified_account_id = result["profile"]["uid"]
            self.verified_credential = credential
            self.store.set(
                PROFILE_CACHE_KEY,
                {
                    "format": 1,
                    "credential_signature": self.checked_signature,
                    "profile": result["profile"],
                    "checked_at": result["checked_at"],
                },
            )
        elif result["state"] == "expired":
            # Only discard the exact login that the platform rejected. A new scan
            # or pending credential rotation may already own this account.
            await self.replace_credentials({}, expected=credential)
            self.checked_signature = signature({})
            self.avatar = None
            self.verified_account_id = ""
            self.verified_credential = None
            self.store.set(PROFILE_CACHE_KEY, None)
        return self.status()

    async def avatar_image(self):
        current = self.status()
        url = avatar_url((current.get("profile") or {}).get("avatar_url"))
        if not url:
            raise ValueError("当前账号没有可用头像")
        key = (signature(self.credentials), url)
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
            signature(self.credentials),
            avatar_url((self.status().get("profile") or {}).get("avatar_url")),
        ):
            raise ValueError("账号已变更，请刷新头像")
        self.avatar = (key, bytes(data), mime)
        return bytes(data), mime
