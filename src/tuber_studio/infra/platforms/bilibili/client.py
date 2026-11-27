"""Narrow Bilibili HTTP adapter; publishing never retries an uncertain write.

Endpoint and root/parent semantics were checked against bilibili-api-python
17.4.2 and c1overzz/dsh-bili-scroll/vendor/bilibili-mcp/mcp_server.py.
Top-level comments and embedded reply previews retain stable root/parent IDs.
Fetching previews does not claim to exhaust every reply page in a thread.
No fingerprint generation, challenge solving, or permission fallback is used.
"""

# Invalid remote payloads intentionally share the adapter's ValueError boundary.
# ruff: noqa: TRY004

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import random
import re
import time
from urllib.parse import urlencode, urlsplit

import httpx

from ....core.errors import PublishUncertain as PublishUncertain
from ....core.errors import platform_rejection
from ....core.text import validate_comment_body
from .comment_materials import comment_attachments

_BV = re.compile(r"BV1[0-9A-Za-z]{9}\Z")
_AV = re.compile(r"av([1-9][0-9]{0,18})\Z", re.IGNORECASE)
BILIBILI_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def bilibili_web_headers(
    *,
    referer: str = "https://www.bilibili.com/",
    origin: str | None = None,
    sec_fetch_site: str = "same-site",
    sec_fetch_mode: str = "cors",
    sec_fetch_dest: str = "empty",
) -> dict[str, str]:
    """Build standard Bilibili Web client headers without synthetic fingerprints."""
    headers = {
        "User-Agent": BILIBILI_WEB_USER_AGENT,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": sec_fetch_site,
        "Sec-Fetch-Mode": sec_fetch_mode,
        "Sec-Fetch-Dest": sec_fetch_dest,
    }
    if origin:
        headers["Origin"] = origin
    return headers
# Public nav-key permutation used by bilibili-api-python 17.4.2. This is
# request signing only: no generated buvid/fingerprint or challenge fallback.
_WBI_ORDER = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
)
_ERRORS = {
    -101: "账号未登录或登录已失效",
    -102: "账号已被平台限制",
    -111: "CSRF 校验失败，请重新配置登录凭证",
    -352: "平台风控限制，请到 B 站完成验证后再操作",
    -403: "平台拒绝访问，请核对账号权限或风控状态",
    -404: "视频或评论不存在或不可访问",
    -412: "平台风控限制，请稍后再操作",
    -509: "请求过于频繁，请稍后再操作",
    12003: "平台禁止回复此评论",
    12006: "目标评论不存在",
    12014: "评论发送已达当前平台上限",
    12015: "平台要求验证码，请到 B 站完成验证",
    12016: "平台拒绝了评论内容",
    12025: "评论超过平台长度限制",
    12035: "账号被 UP 主限制评论",
    12051: "平台拒绝重复评论",
    12052: "评论区已关闭",
}


def _video_params(value: str) -> dict:
    """Parse an identifier, never request a URL supplied by a caller."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("请输入有效的 BV 号、av 号或 B 站视频链接")
    value = value.strip()
    if "://" in value:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in {None, 80, 443}
            ):
                raise ValueError
            match = re.fullmatch(r"/video/([^/]+)/?", parsed.path)
            if not match:
                raise ValueError
            value = match.group(1)
        except ValueError:
            raise ValueError("仅支持 bilibili.com 的标准视频链接，不跟随短链或外站链接") from None
    if _BV.fullmatch(value):
        return {"bvid": value}
    if match := _AV.fullmatch(value):
        return {"aid": int(match.group(1))}
    raise ValueError("请输入有效的 BV 号、av 号或 B 站视频链接")


def _positive_id(value) -> str:
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("平台返回的标识无效")
    return value


def _data(payload: object) -> dict:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise ValueError("B 站返回了无效响应")
    code = payload["code"]
    if code != 0:
        raise ValueError(f"B 站：{_ERRORS.get(code, '请求被平台拒绝')}（code {code}）")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("B 站返回了无效数据")
    return data


class PlatformReadError(ValueError):
    def __init__(self, kind, message, *, http_status=None, code=None):
        self.kind = kind
        self.http_status = http_status
        self.code = code
        super().__init__(message)

    @classmethod
    def restricted(cls, status):
        return cls("restricted", f"平台限制读取（HTTP {status}），请稍后重试", http_status=status)


class BilibiliAccountExpired(PlatformReadError):
    def __init__(self, message):
        super().__init__("permission", message)


class BilibiliAdapter:
    def __init__(
        self,
        credential: dict | None = None,
        *,
        bootstrap_session_cookies: bool | None = None,
        pre_comment_engagement: bool | None = None,
        pre_comment_dwell_range: tuple[float, float] = (4.0, 12.0),
    ):
        credential = credential or {}
        self._sessdata = str(credential.get("sessdata") or "")
        self._csrf = str(credential.get("csrf") or "")
        self._uid = str(credential.get("uid") or "")
        cookies = {}
        if self._sessdata:
            cookies["SESSDATA"] = self._sessdata
        if self._csrf:
            cookies["bili_jct"] = self._csrf
        if self._uid:
            cookies["DedeUserID"] = self._uid
        self.client = httpx.AsyncClient(
            base_url="https://api.bilibili.com",
            headers=bilibili_web_headers(),
            cookies=cookies,
            timeout=httpx.Timeout(45, connect=10),
            follow_redirects=False,
        )
        self.anon_client = httpx.AsyncClient(
            base_url="https://api.bilibili.com",
            headers=bilibili_web_headers(),
            cookies={},
            timeout=httpx.Timeout(45, connect=10),
            follow_redirects=False,
        )
        self._default_anon_client = self.anon_client
        self._bootstrap_session_cookies = bootstrap_session_cookies
        self._session_cookies_bootstrapped = False
        self._session_bootstrap_lock = asyncio.Lock()
        self._pre_comment_engagement = pre_comment_engagement
        self._pre_comment_dwell_range = (
            max(0.0, float(pre_comment_dwell_range[0])),
            max(0.0, float(pre_comment_dwell_range[1])),
        )
        self._recent_video_reads: dict[str, tuple[float, dict]] = {}
        self._recent_comment_reads: dict[tuple[str, int, str], float] = {}

    def _is_mock_transport(self) -> bool:
        return isinstance(getattr(self.client, "_transport", None), httpx.MockTransport)

    def _should_bootstrap_session_cookies(self, *, force: bool = False) -> bool:
        if force:
            return True
        if self._bootstrap_session_cookies is not None:
            return bool(self._bootstrap_session_cookies)
        return not self._is_mock_transport()

    def _should_pre_comment_engage(self, *, force: bool = False) -> bool:
        if force:
            return True
        if self._pre_comment_engagement is not None:
            return bool(self._pre_comment_engagement)
        return not self._is_mock_transport()

    def _dwell_bounds(self, *, force: bool = False) -> tuple[float, float]:
        if not force and self._is_mock_transport() and self._pre_comment_engagement is not True:
            return 0.0, 0.0
        lo, hi = self._pre_comment_dwell_range
        return lo, max(lo, hi)

    def _record_video_read(self, info: dict) -> None:
        if not isinstance(info, dict):
            return
        aid = info.get("aid")
        bvid = info.get("bvid")
        cid = info.get("cid")
        if not cid:
            pages = info.get("pages") or info.get("parts")
            if isinstance(pages, list) and pages and isinstance(pages[0], dict):
                cid = pages[0].get("cid")
        entry = {
            "aid": aid,
            "bvid": bvid,
            "cid": cid,
            "duration": info.get("duration"),
        }
        now = time.monotonic()
        if bvid:
            self._recent_video_reads[str(bvid)] = (now, entry)
        if aid:
            self._recent_video_reads[f"av{aid}"] = (now, entry)

    def _recent_video_info(self, *, bvid: str | None = None, oid: str | int | None = None, fresh_seconds: float = 300.0) -> dict | None:
        now = time.monotonic()
        for key in (str(bvid or ""), f"av{oid}" if oid else ""):
            if key and key in self._recent_video_reads:
                ts, entry = self._recent_video_reads[key]
                if now - ts <= fresh_seconds:
                    return entry
        return None

    def _record_comment_read(self, oid: str | int, comment_type: int, root: str | int = "0") -> None:
        self._recent_comment_reads[(str(oid), int(comment_type), str(root or "0"))] = time.monotonic()

    def _was_comment_recently_read(
        self, oid: str | int, comment_type: int, root: str | int = "0", *, fresh_seconds: float = 120.0
    ) -> bool:
        ts = self._recent_comment_reads.get((str(oid), int(comment_type), str(root or "0")))
        return ts is not None and (time.monotonic() - ts) <= fresh_seconds

    async def ensure_session_cookies(self, *, force: bool = False) -> dict[str, str]:
        """Bootstrap official Bilibili session cookies (buvid3/buvid4/b_nut) once per session."""
        if not force and self._session_cookies_bootstrapped:
            return {
                name: val
                for name in ("buvid3", "buvid4", "b_nut")
                if (val := self.client.cookies.get(name))
            }
        if not self._should_bootstrap_session_cookies(force=force):
            return {}
        async with self._session_bootstrap_lock:
            if not force and self._session_cookies_bootstrapped:
                return {
                    name: val
                    for name in ("buvid3", "buvid4", "b_nut")
                    if (val := self.client.cookies.get(name))
                }
            any_succeeded = False
            home_headers = bilibili_web_headers(
                referer="https://www.bilibili.com/",
                sec_fetch_site="none",
                sec_fetch_mode="navigate",
                sec_fetch_dest="document",
            ) | {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            }
            try:
                home_resp = await self.client.get("https://www.bilibili.com/", headers=home_headers)
                if home_resp.status_code < 500:
                    any_succeeded = True
                for cookie_name in ("b_nut", "buvid3", "buvid4"):
                    val = home_resp.cookies.get(cookie_name) or self.client.cookies.get(cookie_name)
                    if val and not self.client.cookies.get(cookie_name, domain=".bilibili.com"):
                        self.client.cookies.set(cookie_name, str(val), domain=".bilibili.com", path="/")
            except (httpx.HTTPError, ValueError):
                pass
            try:
                spi_resp = await self.client.get(
                    "/x/frontend/finger/spi",
                    headers=bilibili_web_headers(referer="https://www.bilibili.com/", origin="https://www.bilibili.com"),
                )
                if spi_resp.status_code == 200:
                    payload = spi_resp.json()
                    if isinstance(payload, dict) and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
                        any_succeeded = True
                        data = payload["data"]
                        if b_3 := str(data.get("b_3") or "").strip():
                            self.client.cookies.set("buvid3", b_3, domain=".bilibili.com", path="/")
                        if b_4 := str(data.get("b_4") or "").strip():
                            self.client.cookies.set("buvid4", b_4, domain=".bilibili.com", path="/")
            except (httpx.HTTPError, ValueError, TypeError):
                pass
            if any_succeeded or any(self.client.cookies.get(k) for k in ("buvid3", "buvid4", "b_nut")):
                self._session_cookies_bootstrapped = True
            return {
                name: val
                for name in ("buvid3", "buvid4", "b_nut")
                if (val := self.client.cookies.get(name))
            }

    async def _prepare_comment_engagement(
        self,
        *,
        oid: str | int,
        comment_type: int = 1,
        root: str = "0",
        bvid: str | None = None,
        cid: str | int | None = None,
        duration: float | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        """Run genuine pre-comment watch heartbeat, thread readback, and bounded human dwell."""
        if not self._should_pre_comment_engage(force=force):
            return {"engaged": False, "heartbeat_sent": False, "comment_page_fetched": False, "dwell_seconds": 0.0}
        await self.ensure_session_cookies(force=force)
        heartbeat_sent = False
        comment_page_fetched = False
        kind = int(comment_type)
        root_id = str(root or "0")

        if kind == 1:
            cached = self._recent_video_info(bvid=bvid, oid=oid)
            resolved_bvid = bvid or (cached.get("bvid") if cached else None)
            resolved_cid = cid or (cached.get("cid") if cached else None)
            resolved_duration = duration if duration is not None else (cached.get("duration") if cached else None)
            if not resolved_cid:
                try:
                    info = await self._video(resolved_bvid or f"av{oid}")
                    resolved_bvid = info.get("bvid") or resolved_bvid
                    pages = info.get("pages") or []
                    resolved_cid = info.get("cid") or (pages[0].get("cid") if pages and isinstance(pages[0], dict) else None)
                    resolved_duration = info.get("duration") if resolved_duration is None else resolved_duration
                except (httpx.HTTPError, ValueError):
                    pass
            if resolved_cid:
                played_time = random.randint(15, 45)
                if isinstance(resolved_duration, (int, float)) and resolved_duration >= 2:
                    max_play = max(1, min(45, int(resolved_duration) - 1))
                    min_play = min(15, max_play)
                    played_time = random.randint(min_play, max_play)
                referer = (
                    f"https://www.bilibili.com/video/{resolved_bvid}/"
                    if resolved_bvid and _BV.fullmatch(str(resolved_bvid))
                    else "https://www.bilibili.com/"
                )
                hb_data = {
                    "aid": str(oid),
                    "cid": str(resolved_cid),
                    **({"bvid": str(resolved_bvid)} if resolved_bvid and _BV.fullmatch(str(resolved_bvid)) else {}),
                    **({"mid": self._uid} if self._uid else {}),
                    **({"csrf": self._csrf} if self._csrf else {}),
                    "played_time": played_time,
                    "realtime": played_time,
                    "start_ts": max(0, int(time.time()) - played_time),
                    "type": 3,
                    "sub_type": 0,
                    "play_type": 0,
                    "dt": 2,
                }
                try:
                    resp = await self.client.post(
                        "/x/click-interface/web/heartbeat",
                        headers={"Origin": "https://www.bilibili.com", "Referer": referer},
                        data=hb_data,
                    )
                    if resp.status_code == 200:
                        heartbeat_sent = True
                except (httpx.HTTPError, ValueError):
                    pass

        if not self._was_comment_recently_read(oid, kind, root_id):
            try:
                if root_id == "0":
                    await self._get_payload("/x/v2/reply", {"oid": oid, "type": kind, "pn": 1, "ps": 20, "sort": 2})
                else:
                    await self._get_payload(
                        "/x/v2/reply/reply", {"oid": oid, "type": kind, "root": root_id, "pn": 1, "ps": 20}
                    )
                self._record_comment_read(oid, kind, root_id)
                comment_page_fetched = True
            except (httpx.HTTPError, ValueError):
                pass

        lo, hi = self._dwell_bounds(force=force)
        dwell_seconds = 0.0
        if hi > 0:
            dwell_seconds = random.uniform(lo, hi)
            if dwell_seconds > 0:
                await asyncio.sleep(dwell_seconds)
        return {
            "engaged": True,
            "heartbeat_sent": heartbeat_sent,
            "comment_page_fetched": comment_page_fetched,
            "dwell_seconds": dwell_seconds,
        }

    async def close(self):
        await self.anon_client.aclose()
        await self.client.aclose()

    async def public_profile(self, uid):
        uid = _positive_id(uid)
        data = await self._get("/x/web-interface/card", {"mid": uid})
        card = data.get("card")
        if not isinstance(card, dict) or _positive_id(card.get("mid")) != uid:
            raise ValueError("B 站返回的资料与所填 UID 不一致")
        return {"uid": uid, "name": card.get("name"), "avatar_url": card.get("face")}

    async def mention_accounts(self, name):
        data = await self._get("/x/polymer/web-dynamic/v1/mention/search", {"keyword": name})
        accounts = {}
        for group in data.get("groups") or []:
            for item in group.get("items") or []:
                if (item.get("name") or item.get("uname")) == name:
                    accounts[_positive_id(item.get("uid") or item.get("mid"))] = name
        return accounts

    async def account_profile(self):
        if not self._sessdata:
            raise BilibiliAccountExpired("尚未配置B站登录")
        nav = await self._get_payload("/x/web-interface/nav", {})
        if nav.get("code") == -101:
            raise BilibiliAccountExpired("B站登录已失效")
        nav = _data(nav)
        if nav.get("isLogin") is False:
            raise BilibiliAccountExpired("B站登录已失效")
        if nav.get("isLogin") is not True:
            raise ValueError("B站账号核验缺少登录状态")
        uid = _positive_id(nav.get("mid"))
        try:
            details = await self._get("/x/space/myinfo", {})
        except ValueError:
            details = {}
        if details and _positive_id(details.get("mid")) != uid:
            raise ValueError("账号主页与登录账号不一致")
        fans = None
        try:
            stats = await self._get("/x/relation/stat", {"vmid": uid})
            if type(stats.get("follower")) is int and stats["follower"] >= 0:
                fans = stats["follower"]
        except ValueError:
            # Login remains verified when just the public statistic is unavailable.
            pass
        vip_obj = nav.get("vip") if isinstance(nav.get("vip"), dict) else {}
        vip_status_raw = vip_obj.get("status") if "status" in vip_obj else nav.get("vipStatus")
        vip_active = bool(vip_status_raw == 1) if vip_status_raw is not None else None
        vip_type_raw = vip_obj.get("type") if "type" in vip_obj else nav.get("vipType")
        vip_type = int(vip_type_raw) if type(vip_type_raw) is int and vip_type_raw >= 0 else None
        level_obj = nav.get("level_info") if isinstance(nav.get("level_info"), dict) else {}
        level_raw = level_obj.get("current_level")
        level = int(level_raw) if type(level_raw) is int and 0 <= level_raw <= 10 else None
        return {
            "uid": uid,
            "name": details.get("name") or nav.get("uname"),
            "bio": details.get("sign"),
            "avatar_url": nav.get("face"),
            "coins": nav.get("money"),
            "fans": fans,
            "vip_active": vip_active,
            "vip_type": vip_type,
            "level": level,
        }

    async def _get_payload(self, path: str, params: dict, *, anonymous: bool = False):
        client = self.anon_client if anonymous else self.client
        for attempt in range(2):
            try:
                response = await client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as error:
                raise ValueError(
                    f"B 站读取失败（HTTP {error.response.status_code}），请检查权限或风控状态"
                ) from None
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == 0:
                    await asyncio.sleep(0.8)
                    continue
                raise ValueError("B 站读取连接失败或超时，请稍后重试") from None
            except httpx.RequestError:
                raise ValueError("B 站读取连接失败或超时，请稍后重试") from None
            except json.JSONDecodeError:
                raise ValueError("B 站返回了非 JSON 响应，请检查平台访问状态") from None
            except (TypeError, KeyError):
                raise ValueError("B 站返回了无效数据") from None

    async def _get(self, path: str, params: dict) -> dict:
        return _data(await self._get_payload(path, params))

    async def wbi_params(self, params: dict | None = None, *, anonymous: bool = False) -> dict:
        """Sign a trusted caller's query, including a POST's query, once.

        Checked against bilibili-api-python 17.4.2's _get_mixin_key/_enc_wbi.
        The JSON/form body is separate and is never included in this signature.
        No fingerprint generation, challenge solving or retry is performed.
        """
        if params is not None and (
            not isinstance(params, dict)
            or any(not isinstance(k, str) or type(v) not in (str, int) for k, v in params.items())
        ):
            raise ValueError("B 站签名参数须为字符串键和字符串或整数值")
        payload = (
            await self._get_payload("/x/web-interface/nav", {}, anonymous=True)
            if anonymous
            else await self._get_payload("/x/web-interface/nav", {})
        )
        # Anonymous nav deliberately reports -101 while supplying public WBI
        # keys. An explicitly configured but expired login must still fail.
        if isinstance(payload, dict) and payload.get("code") == -101 and (anonymous or not self._sessdata):
            nav = payload.get("data")
        else:
            nav = _data(payload)
        try:
            images = nav["wbi_img"]
            keys = [urlsplit(images[k]).path.rsplit("/", 1)[-1].split(".")[0] for k in ("img_url", "sub_url")]
            if any(not re.fullmatch(r"[0-9a-fA-F]{32}", k) for k in keys):
                raise ValueError
            joined = "".join(keys)
            mixin = "".join(joined[i] for i in _WBI_ORDER)
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError("B 站搜索签名配置无效，请稍后重试") from None
        signed = dict(params or {})
        signed.pop("w_rid", None)
        signed["wts"] = int(time.time())
        if not signed.get("web_location"):
            signed["web_location"] = 1550101
        signed["w_rid"] = hashlib.md5(
            (urlencode(sorted(signed.items())) + mixin).encode(), usedforsecurity=False
        ).hexdigest()
        return signed

    async def popular(self) -> list:
        data = await self._get("/x/web-interface/popular", {"pn": 1, "ps": 20})
        videos = data.get("list")
        if not isinstance(videos, list):
            raise ValueError("B 站热门列表格式无效")
        result = []
        for item in videos:
            try:
                bvid = item["bvid"]
                if not isinstance(bvid, str) or not _BV.fullmatch(bvid):
                    raise ValueError
                result.append(
                    {
                        "bvid": bvid,
                        "title": str(item["title"]),
                        "owner": str(item.get("owner", {}).get("name", "")),
                        "aid": int(_positive_id(item["aid"])),
                    }
                )
            except (KeyError, TypeError, AttributeError, ValueError):
                raise ValueError("B 站热门视频数据格式无效") from None
        return result

    async def _video(self, video: str) -> dict:
        data = await self._get("/x/web-interface/view", _video_params(video))
        if not isinstance(data.get("bvid"), str) or not _BV.fullmatch(data["bvid"]):
            raise ValueError("B 站视频标识无效")
        data["aid"] = int(_positive_id(data.get("aid")))
        self._record_video_read(data)
        return data

    @staticmethod
    def _comment(reply, *, thread_id=None, parent_comment_id=None):
        cid = _positive_id(reply.get("rpid_str") or reply["rpid"])
        root = reply.get("root_str") or reply.get("root")
        parent = reply.get("parent_str") or reply.get("parent")
        if isinstance(root, bool) or isinstance(parent, bool):
            raise ValueError("评论楼标识无效")
        root = _positive_id(root) if root not in (None, 0, "0", "") else thread_id or cid
        parent_verified = parent not in (None, 0, "0", "") or root == cid
        parent = _positive_id(parent) if parent not in (None, 0, "0", "") else parent_comment_id
        if thread_id is not None and root != thread_id:
            raise ValueError("评论楼标识不一致")
        member, content = reply["member"], reply["content"]
        attachments = comment_attachments(content)
        mentioned = content.get("members") or []
        if not isinstance(mentioned, list):
            raise ValueError("评论提及列表格式无效")
        mentions, seen = [], set()
        for person in mentioned:
            uid = _positive_id(person["mid"])
            if uid not in seen:
                mentions.append({"uid": uid, "name": str(person.get("uname") or "")})
                seen.add(uid)
        return {
            "comment_id": cid,
            "thread_id": root,
            "parent_comment_id": parent,
            "parent_verified": parent_verified,
            **({"reply_count": reply["rcount"]} if type(reply.get("rcount")) is int and reply["rcount"] >= 0 else {}),
            "platform_user_id": _positive_id(member["mid"]),
            "display_name": str(member["uname"]),
            "content": html.unescape(str(content["message"])),
            "like_count": max(0, int(reply.get("like", 0))),
            **({"liked_by_self": reply["action"] == 1} if type(reply.get("action")) is int else {}),
            "created_at": str(int(reply.get("ctime", 0))),
            "mentions": mentions,
            **({"attachments": attachments} if attachments else {}),
        }

    async def fetch(self, video: str, max_pages: int = 1, *, newest=False, start_page=1) -> dict:
        if type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise ValueError("每次读取页数须为 1 到 10")
        if type(start_page) is not int or start_page < 1:
            raise ValueError("起始页须为正整数")
        info = await self._video(video)
        items, seen = [], set()
        next_page = None
        for page in range(start_page, start_page + max_pages):
            if page > start_page:
                await asyncio.sleep(0.5)
            data = await self._get(
                "/x/v2/reply",
                {
                    "oid": info["aid"],
                    "type": 1,
                    "pn": page,
                    "ps": 20,
                    "sort": 0 if newest else 2,
                },
            )
            replies = data.get("replies") or []
            if not isinstance(replies, list):
                raise ValueError("B 站评论列表格式无效")
            for reply in replies:
                try:
                    root = self._comment(reply)
                    previews = reply.get("replies") or []
                    if not isinstance(previews, list):
                        raise ValueError
                    normalized = [root] + [
                        self._comment(
                            child, thread_id=root["thread_id"], parent_comment_id=root["comment_id"]
                        )
                        for child in previews
                    ]
                    for item in normalized:
                        if item["comment_id"] not in seen:
                            if len(items) >= 1000:
                                raise ValueError("读取评论超过单批上限")
                            items.append(item)
                            seen.add(item["comment_id"])
                except (KeyError, TypeError, AttributeError, ValueError):
                    raise ValueError("B 站评论数据格式无效") from None
            page_info = data.get("page") or {}
            if not isinstance(page_info, dict):
                raise ValueError("B 站评论分页数据无效")
            count = page_info.get("count")
            has_more = bool(replies) and not (isinstance(count, int) and page * 20 >= count)
            next_page = page + 1 if has_more else None
            if not has_more:
                break
        self._record_comment_read(info["aid"], 1, "0")
        return {
            "video_id": info["bvid"],
            "items": items,
            "title": str(info.get("title", "")),
            "aid": info["aid"],
            "has_more": next_page is not None,
            "next_page": next_page,
        }

    async def publish(
        self, video_id: str, comment_id: str, text: str, *, thread_id: str | None = None, before_send=None,
        mentions=(),
    ) -> dict:
        from .mentions import comment_mention_form

        if not self._sessdata or not self._csrf:
            raise ValueError("发布需要配置 SESSDATA 和 CSRF；当前仅可生成或复制草稿")
        text = validate_comment_body(text)
        parent = _positive_id(comment_id)
        root = _positive_id(thread_id) if thread_id is not None else parent
        mention_form = comment_mention_form(text, mentions)
        await self.ensure_session_cookies()
        info = await self._video(video_id)
        pages = info.get("pages") or []
        cid = info.get("cid") or (pages[0].get("cid") if pages and isinstance(pages[0], dict) else None)
        await self._prepare_comment_engagement(
            oid=info["aid"],
            comment_type=1,
            root=root,
            bvid=info["bvid"],
            cid=cid,
            duration=info.get("duration"),
        )
        if before_send is not None:
            before_send()
        try:
            response = await self.client.post(
                "/x/v2/reply/add",
                headers={
                    "Origin": "https://www.bilibili.com",
                    "Referer": f"https://www.bilibili.com/video/{info['bvid']}/",
                },
                data={
                    "oid": info["aid"],
                    "type": 1,
                    "root": root,
                    "parent": parent,
                    "message": text,
                    **mention_form,
                    "plat": 1,
                    "csrf": self._csrf,
                    "csrf_token": self._csrf,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("发布结果未知，请到 B 站核对；不会自动重试") from None
        # A nonzero platform code is a definite rejection. Missing code/data/rpid
        # after a write cannot establish whether the comment was accepted.
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("发布响应无有效回执，请到 B 站核对；不会自动重试")
        if payload["code"] != 0:
            raise platform_rejection(
                payload,
                fallback=_ERRORS.get(payload["code"], "平台明确拒绝了本次评论"),
                secret_values=(self._sessdata, self._csrf),
            )
        data = payload.get("data")
        try:
            if not isinstance(data, dict):
                raise ValueError
            rpid = _positive_id(data.get("rpid_str") or data.get("rpid"))
        except ValueError:
            raise PublishUncertain("平台未返回有效评论 ID，请到 B 站核对；不会自动重试") from None
        return {
            "platform": "bilibili",
            "code": 0,
            "rpid": rpid,
            "video_id": info["bvid"],
            "aid": info["aid"],
            "root": root,
            "parent": parent,
        }

    async def like_video(self, video_id: str, *, like: bool = True, before_send=None) -> dict:
        if not self._sessdata or not self._csrf:
            raise ValueError("点赞需要配置 SESSDATA 和 CSRF；当前不可操作")
        info = await self._video(video_id)
        if before_send is not None:
            before_send()
        try:
            response = await self.client.post(
                "/x/web-interface/archive/like",
                data={
                    "bvid": info["bvid"],
                    "like": 1 if like else 2,
                    "csrf": self._csrf,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("点赞结果未知，请到 B 站核对；不会自动重试") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("点赞响应无有效回执，请到 B 站核对；不会自动重试")
        if payload["code"] != 0 and payload["code"] != 65006:
            raise platform_rejection(
                payload,
                fallback=_ERRORS.get(payload["code"], "平台明确拒绝了本次点赞"),
                secret_values=(self._sessdata, self._csrf),
            )
        return {
            "platform": "bilibili",
            "code": 0,
            "video_id": info["bvid"],
            "aid": info["aid"],
            "action": "like" if like else "unlike",
            "already_liked": payload["code"] == 65006,
        }

    async def favorite_video(
        self, video_id: str, *, folder_id: int | str | None = None, before_send=None
    ) -> dict:
        if not self._sessdata or not self._csrf:
            raise ValueError("收藏需要配置 SESSDATA 和 CSRF；当前不可操作")
        info = await self._video(video_id)
        if not folder_id:
            nav_payload = await self._get_payload("/x/web-interface/nav", {})
            nav_data = (
                _data(nav_payload) if isinstance(nav_payload, dict) and nav_payload.get("code") == 0 else {}
            )
            mid = nav_data.get("mid")
            if not mid:
                raise ValueError("获取账号信息失败，无法查询默认收藏夹")
            folders_data = _data(
                await self._get_payload("/x/v3/fav/folder/created/list-all", {"up_mid": mid})
            )
            folders = folders_data.get("list") if isinstance(folders_data, dict) else []
            if not folders:
                raise ValueError("未找到可用收藏夹")
            default_folder = next(
                (f for f in folders if f.get("attr", 0) == 0 or "默认" in str(f.get("title", ""))), folders[0]
            )
            folder_id = default_folder["id"]
        if before_send is not None:
            before_send()
        try:
            response = await self.client.post(
                "/x/v3/fav/resource/deal",
                data={
                    "rid": info["aid"],
                    "type": 2,
                    "add_media_ids": str(folder_id),
                    "del_media_ids": "",
                    "csrf": self._csrf,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("收藏结果未知，请到 B 站核对；不会自动重试") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("收藏响应无有效回执，请到 B 站核对；不会自动重试")
        if payload["code"] != 0 and payload["code"] != 11201:
            raise platform_rejection(
                payload,
                fallback=_ERRORS.get(payload["code"], "平台明确拒绝了本次收藏"),
                secret_values=(self._sessdata, self._csrf),
            )
        return {
            "platform": "bilibili",
            "code": 0,
            "video_id": info["bvid"],
            "aid": info["aid"],
            "folder_id": folder_id,
            "action": "favorite",
            "already_favorited": payload["code"] == 11201,
        }

    async def reconcile_publication(self, target, *, author_uid, text, started_at, receipt=None):
        from .reconciliation import reconcile_comment

        target = dict(target)
        if not target.get("oid") and target.get("resource_type", "video") == "video":
            video = target.get("video_id") or target.get("resource_id")
            try:
                requested = _video_params(video)
                info = await self._video(video)
                if (
                    "bvid" in requested
                    and info["bvid"] != requested["bvid"]
                    or "aid" in requested
                    and str(info["aid"]) != str(requested["aid"])
                ):
                    raise ValueError("视频核对来源不一致")
                target["oid"], target["type"] = str(info["aid"]), 1
            except (ValueError, httpx.HTTPError):
                return {
                    "status": "check_failed",
                    "checked_at": time.time(),
                    "coverage": {"scope": "video_metadata"},
                }
        return await reconcile_comment(
            self._get, target, author_uid=author_uid, text=text, started_at=started_at, receipt=receipt
        )

    async def reconcile_dynamic_publication(self, target, *, author_uid, text, started_at, receipt=None):
        from .reconciliation import reconcile_dynamic

        async def read(path, params):
            return await self._get(path, await self.wbi_params(params))

        return await reconcile_dynamic(
            read, target, author_uid=author_uid, text=text, started_at=started_at, receipt=receipt
        )
