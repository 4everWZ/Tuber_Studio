"""Bilibili Web passport operations with scoped credentials."""

import re
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .bilibili import BilibiliAccountExpired

PASSPORT = "https://passport.bilibili.com"
WEB = "https://www.bilibili.com"
API = "https://api.bilibili.com"
PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""
COOKIE_FIELDS = {"sessdata": "SESSDATA", "csrf": "bili_jct", "uid": "DedeUserID"}
CREDENTIAL_FIELDS = {*COOKIE_FIELDS, "refresh_token", "pending_confirmation"}


class PassportError(ValueError):
    def __init__(self, message, *, kind="error", code=None):
        self.kind, self.code = kind, code
        super().__init__(message)


def credential_value(value):
    if (
        not isinstance(value, str) or not 0 < len(value) <= 2000
        or not value.isascii() or any(ord(char) < 33 or ord(char) > 126 or char in ';"\\' for char in value)
    ):
        raise ValueError("凭证格式无效")
    return value


def validate_credentials(credential):
    if not isinstance(credential, dict) or set(credential) - CREDENTIAL_FIELDS:
        raise ValueError("未知账号凭证字段")
    for value in credential.values():
        credential_value(value)
    if "uid" in credential and not re.fullmatch(r"[1-9][0-9]{0,19}", credential["uid"]):
        raise ValueError("平台账号标识无效")
    if "pending_confirmation" in credential and not all(
        credential.get(key) for key in ("sessdata", "csrf", "uid", "refresh_token")
    ):
        raise ValueError("待确认续期记录不完整")


def _correspond_path(timestamp):
    # The platform Web client encrypts the current timestamp with RSA-OAEP/SHA-256.
    key = serialization.load_pem_public_key(PUBLIC_KEY)
    return key.encrypt(
        f"refresh_{timestamp}".encode(),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).hex()


class BilibiliPassport:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": WEB + "/", "Origin": WEB},
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, url, credential=None, **kwargs):
        headers = {"Cookie": "; ".join(
            f"{cookie}={credential_value(credential[field])}"
            for field, cookie in COOKIE_FIELDS.items() if credential and credential.get(field)
        )} if credential else {}
        # The request always uses the specified generation of cookies. Do not
        # allow Set-Cookie from an earlier response to alter another operation.
        request = self.client.build_request(method, url, headers=headers, **kwargs)
        if not headers:
            request.headers.pop("cookie", None)
        try:
            response = await self.client.send(request, stream=True)
            try:
                if response.status_code in {403, 412, 429}:
                    raise PassportError("B站限制登录请求，请稍后重试", kind="restricted")
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 512 * 1024:
                        raise PassportError("B站登录响应超出上限")
                headers = response.headers.copy()
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                return httpx.Response(response.status_code, headers=headers, content=bytes(body))
            finally:
                await response.aclose()
        except httpx.HTTPError:
            raise PassportError("B站登录服务连接失败，请稍后重试") from None
        finally:
            self.client.cookies.clear()

    @staticmethod
    def _data(response):
        try:
            payload = response.json()
            code = payload["code"]
        except (ValueError, TypeError, KeyError):
            raise PassportError("B站登录服务返回无效数据") from None
        if type(code) is not int:
            raise PassportError("B站登录服务返回无效数据")
        if code == -101:
            raise BilibiliAccountExpired("B站登录已失效")
        if code != 0:
            kind = "restricted" if code in {-352, -412, -509} else "error"
            raise PassportError(f"B站登录服务拒绝请求（{code}）", kind=kind, code=code)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def generate_qr(self):
        data = self._data(await self._request("GET", PASSPORT + "/x/passport-login/web/qrcode/generate"))
        try:
            url, key = data["url"], credential_value(data["qrcode_key"])
            parsed = urlsplit(url)
            if (
                len(url) > 1200 or parsed.scheme != "https"
                or parsed.port not in {443, None} or parsed.username or parsed.password
                or (parsed.hostname, parsed.path) not in {
                    ("account.bilibili.com", "/h5/account-h5/auth/scan-web"),
                    ("passport.bilibili.com", "/x/passport-login/web/qrcode"),
                }
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise PassportError("B站未返回有效扫码登录地址") from None
        return key, url

    async def poll_qr(self, key):
        response = await self._request(
            "GET", PASSPORT + "/x/passport-login/web/qrcode/poll", params={"qrcode_key": key},
        )
        data = self._data(response)
        code = data.get("code")
        if type(code) is int and code == 0:
            return "complete", self._new_credentials(response, data)
        if type(code) is int and code in {86101, 86090, 86038}:
            return {86101: "waiting", 86090: "scanned", 86038: "expired"}[code], None
        raise PassportError("B站返回未知扫码状态")

    async def verified_uid(self, credential):
        data = self._data(await self._request("GET", API + "/x/web-interface/nav", credential))
        if data.get("isLogin") is not True:
            raise BilibiliAccountExpired("B站登录已失效")
        uid = str(data.get("mid"))
        if not re.fullmatch(r"[1-9][0-9]{0,19}", uid):
            raise PassportError("B站登录账号标识无效")
        return uid

    async def refresh_info(self, credential):
        data = self._data(await self._request(
            "GET", PASSPORT + "/x/passport-login/web/cookie/info", credential,
        ))
        if type(data.get("refresh")) is not bool:
            raise PassportError("B站未返回有效续期状态")
        timestamp = data.get("timestamp")
        if type(timestamp) is not int or not 0 < timestamp < 10**14:
            timestamp = int(time.time() * 1000) - 20000
        return data["refresh"], timestamp

    async def renew(self, credential, timestamp):
        response = await self._request("GET", WEB + "/correspond/1/" + _correspond_path(timestamp), credential)
        matched = re.search(r'<div\s+id=["\']1-name["\']\s*>\s*([A-Za-z0-9_-]{1,256})\s*</div>', response.text)
        if not matched:
            raise PassportError("B站未返回有效续期口令")
        response = await self._request("POST", PASSPORT + "/x/passport-login/web/cookie/refresh", credential, data={
            "csrf": credential["csrf"], "refresh_csrf": matched[1], "source": "main_web",
            "refresh_token": credential["refresh_token"],
        })
        return self._new_credentials(response, self._data(response))

    async def confirm(self, credential):
        response = await self._request("POST", PASSPORT + "/x/passport-login/web/confirm/refresh", credential, data={
            "csrf": credential["csrf"], "refresh_token": credential["pending_confirmation"],
        })
        self._data(response)

    @staticmethod
    def _new_credentials(response, data):
        cookies = SimpleCookie()
        try:
            for value in response.headers.get_list("set-cookie"):
                cookies.load(value)
            result = {field: cookies[cookie].value for field, cookie in COOKIE_FIELDS.items()}
            result["refresh_token"] = data["refresh_token"]
            validate_credentials(result)
        except (ValueError, KeyError, CookieError):
            raise PassportError("B站没有返回完整登录凭证") from None
        return result
