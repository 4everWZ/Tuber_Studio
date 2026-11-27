"""Bounded platform evidence and account notification adapter.

Only fixed platform API paths and verified resource identifiers reach requests.
Endpoint/signing semantics were checked against the PyPI bilibili-api-python
17.4.2 source distribution. The workflow reference is dsh-bili-scroll 398e4724,
not its permissions, prompts, ASR fallback, or host. Reads never mark read.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from ....core.errors import platform_rejection
from ....core.text import validate_comment_body
from .client import (
    _BV,
    BilibiliAccountExpired,
    BilibiliAdapter,
    PlatformReadError,
    PublishUncertain,
    _positive_id,
    _video_params,
    bilibili_web_headers,
)
from .comment_materials import MAX_IMAGE_BYTES, article_excerpt, comment_image_url
from .subtitles import deduplicate_subtitles

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_API_BYTES = 4 * 1024 * 1024
DYNAMIC_COMMENT_TYPES = {11, 17}
SUBTITLE_HOSTS = {"aisubtitle.hdslb.com", "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}
CREDIT = re.compile(r"^(?:字幕(?:制作|提供|来源|组|by)|(?:翻译|听译|校对|时间轴|轴师|字幕员)[:：\s])", re.I)


def http_read_detail(error):
    """Retain transport diagnostics without echoing request or response data."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return f"超时（{type(error).__name__}）"
    if isinstance(error, httpx.ConnectError):
        return "连接失败（ConnectError）"
    return f"请求失败（{type(error).__name__}）"


def _integer(value, *, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PlatformReadError("invalid", "平台返回的数量或时间无效")
    return value


def _identifier(value):
    try:
        return _positive_id(value)
    except ValueError:
        raise PlatformReadError("invalid", "平台返回的标识无效") from None


def _text(value, maximum=100000):
    if not isinstance(value, str) or len(value) > maximum:
        raise PlatformReadError("invalid", "平台返回的文本无效或过大")
    return value


def _duration(value):
    if isinstance(value, str) and re.fullmatch(r"\d{1,4}(?::\d{1,2}){1,2}", value):
        value = sum(int(n) * 60**i for i, n in enumerate(reversed(value.split(":"))))
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise PlatformReadError("invalid", "平台返回的视频时长无效")
    return float(value)


class BrowsingAdapter(BilibiliAdapter):
    """No browser emulation, generated fingerprints, or platform-write retries."""

    read_video_tags = True

    def __init__(self, credential=None):
        super().__init__(credential)
        self.request_gate = None
        self._read_spacing = 0.1
        self._read_clock = 0.0
        self._read_gate = asyncio.Lock()
        self._frame_sampler = None
        self._stream_sampler = None

    async def video_frames(self, metadata, count=3, *, cid=None, timestamps=None, windows=None, sparse_range=None, known_times=()):
        from .frames import FrameSampler

        if self._frame_sampler is None:
            self._frame_sampler = FrameSampler(self)
        return await self._frame_sampler.frames(metadata, count, cid=cid, timestamps=timestamps, windows=windows,
                                               sparse_range=sparse_range, known_times=known_times)

    def _stream_reader(self):
        from .frames import FrameSampler
        from .stream_frame import StreamFrameSampler

        if self._frame_sampler is None:
            self._frame_sampler = FrameSampler(self)
        if self._stream_sampler is None:
            self._stream_sampler = StreamFrameSampler(self, self._frame_sampler.decoder)
        return self._stream_sampler

    async def video_stream_frame(self, metadata, *, cid, timestamp):
        return await self._stream_reader().frame(metadata, cid, timestamp)

    async def video_stream_clip(self, metadata, *, cid, start, end):
        return await self._stream_reader().clip(metadata, cid, start, end)

    async def video_stream_audio(self, metadata, *, cid, max_duration=600):
        return await self._stream_reader().audio(metadata, cid, max_duration=max_duration)

    async def close(self):
        if self._frame_sampler is not None:
            await self._frame_sampler.close()
        await super().close()

    async def _pace(self):
        if self.request_gate is not None:
            await self.request_gate()
        async with self._read_gate:
            await asyncio.sleep(max(0, self._read_clock + self._read_spacing - time.monotonic()))
            self._read_clock = time.monotonic()

    @asynccontextmanager
    async def _stream_get(self, path, params, *, anonymous: bool = False):
        if not anonymous:
            async with self.client.stream("GET", path, params=params, follow_redirects=False) as response:
                yield response
            return
        if self.anon_client is self._default_anon_client and isinstance(self.client._transport, httpx.MockTransport):
            request = httpx.Request(
                "GET",
                self.client.base_url.join(path),
                params=params,
                headers=bilibili_web_headers(),
            )
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                yield response
            finally:
                await response.aclose()
            return
        async with self.anon_client.stream("GET", path, params=params, follow_redirects=False) as response:
            yield response

    async def _video(self, video: str) -> dict:
        info = await super()._video(video)
        return info

    async def _get_payload(self, path, params, *, anonymous: bool = False):
        await self._pace()
        try:
            async with self._stream_get(path, params, anonymous=anonymous) as response:
                if response.status_code in {401, 403}:
                    raise PlatformReadError(
                        "permission",
                        f"平台拒绝读取（HTTP {response.status_code}），请检查登录与权限",
                        http_status=response.status_code,
                    )
                if response.status_code in {412, 429}:
                    raise PlatformReadError.restricted(response.status_code)
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_API_BYTES:
                        raise PlatformReadError("too_large", "平台读取超过单响应字节上限")
                payload = json.loads(body)
        except httpx.HTTPError as error:
            raise PlatformReadError("network", "平台读取失败：" + http_read_detail(error)) from None
        except (ValueError, TypeError) as error:
            if isinstance(error, PlatformReadError):
                raise
            raise PlatformReadError("invalid", "平台返回无效JSON") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PlatformReadError("invalid", "平台返回无效响应")
        # The inherited WBI signer calls this lower-level method directly.
        # Preserve its explicit anonymous-nav case, but classify expired real
        # logins and risk denials before that signer can erase their category.
        code = payload["code"]
        anonymous_nav = code == -101 and path == "/x/web-interface/nav" and (anonymous or not self._sessdata)
        if code == -101 and path == "/x/web-interface/nav" and not anonymous_nav:
            raise BilibiliAccountExpired("B站登录已失效")
        if code in {-101, -102, -111, -403} and not anonymous_nav:
            raise PlatformReadError("permission", "平台账号未登录、失效或无读取权限", code=code)
        if code in {-352, -412, -509}:
            raise PlatformReadError("restricted", f"平台风控或频率限制（code {code}）", code=code)
        if code in {-404, 12006, 12022, 62002, 62004, 62012}:
            messages = {
                62002: "视频已删除或设为不可见（code 62002）",
                62004: "视频正在审核中暂不可访问（code 62004）",
                62012: "视频仅作者自己可见（code 62012）",
                12022: "原评论已删除（code 12022）",
            }
            raise PlatformReadError("not_found", messages.get(code, "平台来源不存在或已删除"), code=code)
        if code in {12002, 12061}:
            raise PlatformReadError("closed", f"评论区已关闭或仅精选可见（code {code}）", code=code)
        if code != 0 and not anonymous_nav:
            raise PlatformReadError("platform", f"平台拒绝读取（code {code}）", code=code)
        return payload

    async def _read(self, path, params, *, anonymous: bool = False):
        payload = (
            await self._get_payload(path, params, anonymous=True)
            if anonymous
            else await self._get_payload(path, params)
        )
        # Only anonymous WBI nav is allowed through _get_payload with a
        # nonzero code. It supplies signing keys, not authenticated content.
        if payload["code"] != 0:
            raise PlatformReadError("permission", "平台账号未登录、失效或无读取权限", code=payload["code"])
        if not isinstance(payload.get("data"), (dict, list)):
            raise PlatformReadError("invalid", "平台返回无效数据")
        return payload["data"]

    async def _get(self, path, params, *, anonymous: bool = False):
        data = await self._read(path, params, anonymous=True) if anonymous else await self._read(path, params)
        if not isinstance(data, dict):
            raise PlatformReadError("invalid", "平台返回对象类型错误")
        return data

    async def read_comment_article(self, identity):
        identity = _identifier(identity)
        article = await self._get("/x/article/view", {"id": identity})
        return article_excerpt(article, identity)

    async def read_comment_image(self, url):
        url = comment_image_url(url)
        if url is None:
            raise PlatformReadError("invalid", "评论图片来源不属于支持的 B 站 CDN")
        request = httpx.Request("GET", url, headers={"Referer": "https://www.bilibili.com/"})
        await self._pace()
        try:
            response = await self.client.send(request, stream=True, auth=None, follow_redirects=False)
            try:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp", "application/octet-stream"}:
                    raise PlatformReadError("invalid", "评论图片响应类型不受支持")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                        raise PlatformReadError("too_large", "评论图片下载超出字节上限")
                    body.extend(chunk)
                return bytes(body)
            finally:
                await response.aclose()
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            kind = "invalid" if error.response.is_redirect else (
                "restricted" if code in {412, 429} else "permission" if code in {401, 403} else "network"
            )
            raise PlatformReadError(kind, "评论图片下载失败：" + http_read_detail(error)) from None
        except httpx.HTTPError as error:
            raise PlatformReadError("network", "评论图片下载失败：" + http_read_detail(error)) from None

    async def self_uid(self):
        if not self._sessdata:
            raise PlatformReadError("permission", "需要配置当前账号SESSDATA")
        return _identifier((await self._get("/x/web-interface/nav", {})).get("mid"))

    def capabilities(self):
        return {
            name: True
            for name in (
                "recommend",
                "search",
                "popular",
                "specified",
                "subtitles",
                "mentions",
                "replies",
                "top_level",
                "dynamic_comments",
                "publication_visibility",
            )
        }

    async def own_uploads(self, account, limit=3):
        account = _identifier(account)
        signed = await self.wbi_params({"mid": account, "pn": 1, "ps": limit, "order": "pubdate"})
        data = await self._get("/x/space/wbi/arc/search", signed)
        rows = (data.get("list") or {}).get("vlist")
        if not isinstance(rows, list):
            raise PlatformReadError("invalid", "个人投稿列表结构无效")
        return [
            {"bvid": _video_params(row["bvid"])["bvid"], "aid": _identifier(row["aid"])}
            for row in rows[:limit]
            if isinstance(row, dict) and row.get("bvid") and row.get("aid")
        ]

    async def own_dynamics(self, account, limit=3):
        if str(account) != await self.self_uid():
            raise PlatformReadError("permission", "只能扫描当前账号的动态")
        signed = await self.wbi_params({"host_mid": str(account), "timezone_offset": -480})
        data = await self._get("/x/polymer/web-dynamic/v1/feed/space", signed)
        items = data.get("items") or []
        if not isinstance(items, list):
            raise PlatformReadError("invalid", "个人动态列表结构无效")
        targets = []
        for item in items:
            target = self._dynamic_comment_target(item)
            if not target or str(target["author_uid"]) != str(account):
                continue
            targets.append(target)
            if len(targets) == limit:
                break
        return targets

    @staticmethod
    def _dynamic_comment_target(item):
        if not isinstance(item, dict) or item.get("type") not in {"DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW"}:
            return None
        basic = item.get("basic") or {}
        kind = basic.get("comment_type")
        if str(kind) not in {"11", "17"}:
            return None
        modules = item.get("modules") or {}
        try:
            oid = _identifier(basic.get("comment_id_str"))
            dynamic_id = _identifier(item.get("id_str"))
            author = _identifier((modules.get("module_author") or {}).get("mid"))
        except ValueError:
            return None
        return {"resource_type": "dynamic", "type": int(kind), "oid": oid, "dynamic_id": dynamic_id,
                "author_uid": author}

    async def _dynamic_detail(self, dynamic_id):
        from .reconciliation import dynamic_detail_params

        signed = await self.wbi_params(dynamic_detail_params(dynamic_id))
        item = (await self._get("/x/polymer/web-dynamic/v1/detail", signed)).get("item")
        if not isinstance(item, dict) or str(item.get("id_str")) != dynamic_id:
            raise PlatformReadError("not_found", "原始动态不可读或对象不一致")
        return item

    async def own_dynamic_target(self, account, dynamic_id):
        target = self._dynamic_comment_target(await self._dynamic_detail(_identifier(dynamic_id)))
        if not target or str(target["author_uid"]) != str(account):
            raise PlatformReadError("permission", "无法核验当前账号的动态评论区")
        return target

    @staticmethod
    def _dynamic_reaction_source(item):
        from .reconciliation import dynamic_summary

        modules = item.get("modules") or {}
        state = ((modules.get("module_stat") or {}).get("like") or {}).get("status")
        text = _text(dynamic_summary(modules.get("module_dynamic") or {}).get("text"))
        author = (modules.get("module_author") or {}).get("mid")
        if type(state) is not bool or not author or not text:
            return None
        return {"dynamic_id": _identifier(item["id_str"]), "author_uid": _identifier(author), "text": text, "liked": state}

    async def read_dynamic_like(self, dynamic_id):
        return self._dynamic_reaction_source(await self._dynamic_detail(_identifier(dynamic_id)))

    async def like_dynamic(self, target, *, before_send):
        if not self._sessdata or not self._csrf:
            raise ValueError("动态点赞需要当前账号的登录凭证")
        identifier = _identifier(target["dynamic_id"])
        before_send()
        try:
            response = await self.client.post("/x/dynamic/feed/dyn/thumb", params={"csrf": self._csrf},
                                              json={"dyn_id_str": identifier, "up": 1})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("动态点赞结果待核对") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("B 站返回的动态点赞回执格式无效")
        if payload["code"] != 0:
            raise platform_rejection(
                payload,
                fallback="平台明确拒绝了本次动态点赞",
                secret_values=(self._sessdata, self._csrf),
            )
        return {"platform": "bilibili", "code": 0, "action": "dynamic_like", "dynamic_id": identifier}

    async def fetch_dynamic_comments(self, target):
        target = self._target(target, top_level=True)
        data = await self._get("/x/v2/reply", {"oid": target["oid"], "type": target["type"],
                                              "pn": 1, "ps": 20, "sort": 0})
        items = []
        for reply in data.get("replies") or []:
            root = self._comment(reply)
            items.append(root)
            items.extend(self._comment(item, thread_id=root["thread_id"], parent_comment_id=root["comment_id"])
                         for item in reply.get("replies") or [])
        self._record_comment_read(target["oid"], target["type"], "0")
        return items

    async def discover(self, source, keyword="", page=1, limit=20):
        _integer(page, minimum=1, maximum=10)
        _integer(limit, minimum=1, maximum=20)
        keyword = _text(keyword, 100).strip()
        account = None
        if source == "recommend":
            if keyword:
                raise ValueError("首页推荐不接受搜索词")
            account = await self.self_uid()
            signed = await self.wbi_params({"ps": limit, "fresh_idx": page})
            data = await self._get("/x/web-interface/wbi/index/top/feed/rcmd", signed)
            raw = data.get("item")
        elif source == "search":
            if not keyword:
                raise ValueError("搜索必须有真实非空关键词，不能回退热门")
            signed = await self.wbi_params(
                {
                    "search_type": "video",
                    "keyword": keyword,
                    "page": page,
                    "page_size": limit,
                    "order": "totalrank",
                }
            )
            data = await self._get("/x/web-interface/wbi/search/type", signed)
            raw = data.get("result")
        elif source == "popular":
            if keyword:
                raise ValueError("热门来源不接受搜索词")
            data = await self._get("/x/web-interface/popular", {"pn": page, "ps": limit})
            raw = data.get("list")
        elif source == "specified":
            info = await self.video_metadata(keyword)
            return {"items": [info], "source": source, "keyword": keyword, "page": 1, "has_more": False}
        else:
            raise ValueError("不支持的发现来源")
        if not isinstance(raw, list) or len(raw) > 100:
            raise PlatformReadError("invalid", "视频发现列表格式无效")
        result = []
        for row in raw[:limit]:
            if not isinstance(row, dict):
                raise PlatformReadError("invalid", "视频候选格式无效")
            # Recommendation feeds may include ads or live rooms. They are not videos.
            if source == "recommend" and row.get("goto", "av") != "av":
                continue
            # Video search also returns course cards (type="ketang") without a BV.
            if source == "search" and row.get("type", "video") != "video":
                continue
            bvid = row.get("bvid")
            if not isinstance(bvid, str) or not _BV.fullmatch(bvid):
                raise PlatformReadError("invalid", "发现结果的视频标识无效")
            owner = row.get("owner") or {}
            result.append(
                {
                    "bvid": bvid,
                    "aid": int(_identifier(row.get("aid") or row.get("id"))),
                    "title": html.unescape(re.sub(r"</?em[^>]*>", "", _text(row.get("title"), 1000))),
                    "owner_uid": _identifier(owner.get("mid") or row.get("mid")),
                    "owner": _text(owner.get("name") or row.get("author") or "", 100),
                    "duration": _duration(row.get("duration", 0)),
                    "published_at": row.get("pubdate"),
                    "category_id": row.get("tid"),
                    "source": source,
                    "keyword": keyword,
                    "page": page,
                }
            )
        return {
            "items": result,
            "source": source,
            "keyword": keyword,
            "page": page,
            "account_id": account,
            "has_more": bool(raw) and not data.get("no_more", False),
        }

    async def video_metadata(self, video):
        data = await self._get("/x/web-interface/view", _video_params(video))
        bvid = data.get("bvid")
        if not isinstance(bvid, str) or not _BV.fullmatch(bvid):
            raise PlatformReadError("invalid", "视频BV标识无效")
        aid = int(_identifier(data.get("aid")))
        raw_parts = data.get("pages")
        if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= 1000:
            raise PlatformReadError("invalid", "视频分P信息缺失或过大")
        parts = []
        for ordinal, part in enumerate(raw_parts, 1):
            if not isinstance(part, dict):
                raise PlatformReadError("invalid", "视频分P信息无效")
            parts.append(
                {
                    "cid": _identifier(part.get("cid")),
                    "page": ordinal,
                    "title": _text(part.get("part") or "", 1000),
                    "duration": _duration(part.get("duration", 0)),
                }
            )
        tags = await self._read(
            "/x/web-interface/view/detail/tag", {"bvid": bvid, "aid": aid, "cid": int(parts[0]["cid"])}
        ) if self.read_video_tags else []
        if not isinstance(tags, list):
            raise PlatformReadError("invalid", "平台标签格式无效")
        result = {
            "bvid": bvid,
            "aid": aid,
            "title": _text(data.get("title"), 1000),
            "description": _text(data.get("desc", "")),
            "tags": [_text(tag.get("tag_name"), 100) for tag in tags if isinstance(tag, dict)],
            "tags_missing": not tags,
            "owner_uid": _identifier((data.get("owner") or {}).get("mid")),
            "owner": _text((data.get("owner") or {}).get("name", ""), 100),
            "duration": _duration(data.get("duration")),
            "parts": parts,
            "published_at": data.get("pubdate"),
            "category_id": data.get("tid"),
            "fetched_at": time.time(),
            "description_complete": True,
        }
        self._record_video_read(result)
        return result

    @staticmethod
    def _subtitle_url(value):
        value = _text(value, 4096)
        value = "https:" + value if value.startswith("//") else value
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in SUBTITLE_HOSTS
                or parsed.port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError
        except ValueError:
            raise PlatformReadError("invalid", "字幕地址不在允许的HTTPS字幕来源中") from None
        return value

    async def list_subtitles(self, bvid, cid):
        params = _video_params(bvid)
        if "bvid" not in params:
            raise ValueError("字幕需要规范BV标识")
        cid = _identifier(cid)
        # Confirm membership; a remote caller cannot splice another video's CID.
        video = await self._get("/x/web-interface/view", params)
        if video.get("bvid") != bvid or not any(
            str(part.get("cid")) == cid for part in video.get("pages", []) if isinstance(part, dict)
        ):
            raise PlatformReadError("invalid", "字幕CID与当前视频分P不一致")
        signed = await self.wbi_params({"bvid": bvid, "cid": int(cid)})
        data = await self._get("/x/player/wbi/v2", signed)
        if data.get("cid") is not None and str(data["cid"]) != cid:
            raise PlatformReadError("invalid", "平台字幕CID与请求不一致")
        if data.get("need_login_subtitle"):
            raise PlatformReadError("permission", "平台要求登录后才能读取字幕")
        subtitle = data.get("subtitle")
        if subtitle is not None and not isinstance(subtitle, dict):
            raise PlatformReadError("invalid", "字幕轨道对象无效")
        if subtitle and subtitle.get("need_login_subtitle"):
            raise PlatformReadError("permission", "平台要求登录后才能读取字幕")
        raw = (subtitle or {}).get("subtitles")
        if raw is None:
            raise PlatformReadError("no_subtitles", "当前分P没有可读取CC字幕")
        if not isinstance(raw, list) or len(raw) > 100:
            raise PlatformReadError("invalid", "字幕轨道列表无效")
        tracks = []
        for row in raw:
            if not isinstance(row, dict):
                raise PlatformReadError("invalid", "字幕轨道无效")
            language = _text(row.get("lan"), 40)
            tracks.append(
                {
                    "bvid": bvid,
                    "cid": cid,
                    "track_id": _identifier(row.get("id_str") or row.get("id")),
                    "language": language,
                    "is_ai": row.get("type") == 1 or language.startswith("ai-"),
                    "url": self._subtitle_url(row.get("subtitle_url")),
                }
            )
        if not tracks:
            raise PlatformReadError("no_subtitles", "当前分P没有可读取CC字幕")
        return tracks

    async def fetch_subtitle(self, track):
        url = self._subtitle_url(track["url"])
        request = self.client.build_request("GET", url)
        # Cookies in the API client are domainless; never send them to CDN hosts.
        request.headers.pop("cookie", None)
        request.headers.pop("authorization", None)
        await self._pace()
        try:
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code in {401, 403}:
                    raise PlatformReadError("permission", f"字幕下载权限不足（HTTP {response.status_code}）")
                if response.status_code in {412, 429}:
                    raise PlatformReadError.restricted(response.status_code)
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_SUBTITLE_BYTES:
                        raise PlatformReadError("too_large", "字幕下载超出字节上限")
                payload = json.loads(buffer)
            finally:
                await response.aclose()
        except httpx.HTTPStatusError as error:
            kind = "invalid" if error.response.is_redirect else "network"
            raise PlatformReadError(
                kind, "字幕下载失败：" + http_read_detail(error) + "；不跟随重定向"
            ) from None
        except httpx.HTTPError as error:
            raise PlatformReadError("network", "字幕下载失败：" + http_read_detail(error)) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise PlatformReadError("invalid", "字幕正文不是有效JSON") from None
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, list) or len(body) > 20000:
            raise PlatformReadError("invalid", "字幕正文结构无效或过大")
        segments = []
        for index, row in enumerate(body):
            if not isinstance(row, dict):
                raise PlatformReadError("invalid", "字幕片段格式无效")
            start, end = _duration(row.get("from")), _duration(row.get("to"))
            text = _text(row.get("content"), 10000).strip()
            if end <= start or (segments and start < segments[-1]["start"]):
                raise PlatformReadError("invalid", "字幕时间范围无效")
            if text:
                segments.append(
                    {
                        "id": f"{track['cid']}:{track['track_id']}:{index}",
                        "start": start,
                        "end": end,
                        "text": text,
                    }
                )
        if not segments or all(CREDIT.match(row["text"]) for row in segments):
            raise PlatformReadError("empty", "字幕没有有效正文（空白或仅署名）")
        segments = deduplicate_subtitles(segments)
        for clean_idx, seg in enumerate(segments):
            seg["id"] = f"{track['cid']}:{track['track_id']}:{clean_idx}"
        if not segments or all(CREDIT.match(row["text"]) for row in segments):
            raise PlatformReadError("empty", "字幕没有有效正文（空白或仅署名）")
        return {
            **{key: track[key] for key in ("bvid", "cid", "track_id", "language", "is_ai")},
            "segments": segments,
            "coverage": {
                "complete": True,
                "segment_count": len(segments),
                "start": segments[0]["start"],
                "end": max(row["end"] for row in segments),
            },
            "fetched_at": time.time(),
        }

    async def subtitles(self, bvid, cid, languages=("zh-CN", "zh-Hans", "zh")):
        if not isinstance(languages, (tuple, list)) or not 1 <= len(languages) <= 10:
            raise ValueError("字幕语言优先级需要1到10项")
        tracks = await self.list_subtitles(bvid, cid)

        def rank(track):
            language = track["language"].removeprefix("ai-")
            return (
                next((i for i, v in enumerate(languages) if language == v), len(languages)),
                track["is_ai"],
            )

        return await self.fetch_subtitle(min(tracks, key=rank))

    async def notifications(self, kind, cursor=None):
        if kind not in {"mention", "reply"}:
            raise ValueError("只读取真实@和收到回复通知，点赞关注不触发互动")
        account = await self.self_uid()
        params = {}
        time_field = "at_time" if kind == "mention" else "reply_time"
        if cursor is not None:
            if not isinstance(cursor, dict) or not set(cursor) <= {"id", "time", "is_end"}:
                raise ValueError("通知游标格式无效")
            if cursor.get("id") is not None:
                params["id"] = _identifier(cursor["id"])
            if cursor.get("time") is not None:
                params[time_field] = _integer(cursor["time"])
        data = await self._get("/x/msgfeed/at" if kind == "mention" else "/x/msgfeed/reply", params)
        raw = data.get("items")
        if not isinstance(raw, list) or len(raw) > 100:
            raise PlatformReadError("invalid", "账号通知列表无效")
        events = []
        for row in raw:
            if not isinstance(row, dict):
                raise PlatformReadError("invalid", "账号通知格式无效")
            item, user = row.get("item"), row.get("user")
            if not isinstance(item, dict) or not isinstance(user, dict):
                raise PlatformReadError("invalid", "账号通知缺少原始对象或用户")
            business = item.get("business_id")
            resource = {1: "video", 11: "dynamic", 17: "dynamic"}.get(business)
            root = item.get("root_id") or item.get("source_id") or item.get("target_id")
            comment = item.get("source_id") or item.get("target_id")
            # Unknown resource types remain observable and cannot be routed by guessing.
            event = {
                "event_id": _identifier(row.get("id")),
                "kind": kind,
                "account_id": account,
                "sender_uid": _identifier(user.get("mid")),
                "sender_name": _text(user.get("nickname") or user.get("uname") or "", 100),
                "resource_type": resource or "unsupported",
                "business_id": item.get("business_id"),
                "oid": _identifier(item.get("subject_id")),
                "root": _identifier(root) if root else None,
                "parent": _identifier(comment) if comment else None,
                "comment_id": _identifier(comment) if comment else None,
                "timestamp": _integer(row.get(time_field)),
                "source_text": _text(item.get("source_content") or "", 10000),
                "target_id": _identifier(item["target_id"]) if item.get("target_id") else None,
                "bvid": None,
            }
            if resource == "dynamic":
                event["type"] = business
                uri = urlsplit(str(item.get("uri") or ""))
                path = uri.path.strip("/").split("/")
                if uri.hostname == "t.bilibili.com" and len(path) == 1 and path[0].isdigit():
                    event["dynamic_id"] = _identifier(path[0])
                elif uri.hostname == "www.bilibili.com" and len(path) == 2 and path[0] in {"opus", "dynamic"} and path[1].isdigit():
                    event["dynamic_id"] = _identifier(path[1])
            events.append(event)
        next_cursor = data.get("cursor")
        if not isinstance(next_cursor, dict) or type(next_cursor.get("is_end")) not in {bool, int}:
            raise PlatformReadError("invalid", "账号通知分页游标无效，不能越过未保存消息")
        normalized_cursor = {"is_end": bool(next_cursor["is_end"])}
        if next_cursor.get("id") not in (None, 0, "0"):
            normalized_cursor["id"] = _identifier(next_cursor["id"])
        if next_cursor.get("time") is not None:
            normalized_cursor["time"] = _integer(next_cursor["time"])
        return {
            "account_id": account,
            "items": events,
            "cursor": normalized_cursor,
            "has_more": not normalized_cursor["is_end"],
        }

    @staticmethod
    def _target(target, *, top_level=False):
        if not isinstance(target, dict):
            raise ValueError("评论目标必须来自已核验的公开来源")
        resource = target.get("resource_type") or target.get("type")
        resource = {1: "video", 11: "dynamic", 17: "dynamic"}.get(resource, resource)
        if resource not in {"video", "dynamic"}:
            raise PlatformReadError("unsupported", "暂不支持该通知资源的回复")
        kind = target.get("type", 1 if resource == "video" else 17)
        if type(kind) is not int or kind not in ({1} if resource == "video" else DYNAMIC_COMMENT_TYPES):
            raise PlatformReadError("invalid", "评论类型与资源不一致")
        oid = _identifier(target.get("oid") or target.get("aid"))
        root, parent = "0", "0"
        if not top_level:
            root = _identifier(target.get("root"))
            parent = _identifier(target.get("parent") or target.get("comment_id"))
        elif target.get("root") not in {None, 0, "0"} or target.get("parent") not in {None, 0, "0"}:
            raise ValueError("顶层留言不能携带回复楼或父评论")
        return {
            "resource_type": resource,
            "type": kind,
            "oid": oid,
            "root": root,
            "parent": parent,
            "bvid": target.get("bvid"),
        }

    async def _thread_items(self, target, *, resume=None, checkpoint=None):
        root_id = target["root"]
        items, head, page = {}, None, 1
        for _ in range(8):
            data = await self._get("/x/v2/reply/reply", {
                "oid": target["oid"], "type": target["type"], "root": root_id, "pn": page, "ps": 20,
            })
            replies = data.get("replies") or []
            root = data.get("root")
            if not isinstance(replies, list):
                raise PlatformReadError("invalid", "原始楼回复结构无效")
            try:
                normalized = ([self._comment(root, thread_id=root_id)] if root else []) + [
                    self._comment(item, thread_id=root_id) for item in replies
                ]
            except (ValueError, TypeError, KeyError, AttributeError):
                raise PlatformReadError("invalid", "原始楼关系无效") from None
            metadata = data.get("page") or {}
            count = metadata.get("count")
            size = metadata.get("size") or 20
            has_more = bool(replies) and (page * size < count if type(count) is int and type(size) is int
                                           else len(replies) >= 20)
            if page == 1:
                # A shifted head invalidates old offsets. Keep the current page
                # authoritative even when resuming older pages.
                head = [count, [item["comment_id"] for item in normalized]]
                if has_more and resume and resume.get("head") == head:
                    items = {item["comment_id"]: item for item in resume["items"]}
                    page = resume["next_page"] - 1
            items.update((item["comment_id"], item) for item in normalized)
            if not has_more:
                self._record_comment_read(target["oid"], target["type"], root_id)
                return list(items.values()), {"complete": type(count) is int, "page": page,
                                              "reason": "thread_pages"}
            page += 1
            if checkpoint:
                checkpoint({"head": head, "next_page": page, "items": list(items.values())})
        raise PlatformReadError("thread_pending", "讨论楼分页尚未读完，保存游标后继续核对")

    async def video_thread_page(self, target, *, page=1):
        """Read a single page for the ordinary/priority video comment cursor."""
        scoped = self._target(target)
        if scoped["resource_type"] != "video" or type(page) is not int or page < 1:
            raise ValueError("视频评论楼页码无效")
        root_id = scoped["root"]
        data = await self._get("/x/v2/reply/reply", {
            "oid": scoped["oid"], "type": 1, "root": root_id, "pn": page, "ps": 20,
        })
        replies, root = data.get("replies") or [], data.get("root")
        info = data.get("page") or {}
        if not isinstance(replies, list) or not isinstance(info, dict):
            raise PlatformReadError("invalid", "评论楼分页结构无效")
        try:
            items = ([self._comment(root, thread_id=root_id)] if root else []) + [
                self._comment(reply, thread_id=root_id) for reply in replies
            ]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PlatformReadError("invalid", "评论楼关系无效") from None
        count, size = info.get("count"), info.get("size") or 20
        has_more = bool(replies) and (page * size < count if type(count) is int and type(size) is int
                                       else len(replies) >= 20)
        self._record_comment_read(scoped["oid"], 1, root_id)
        return {"items": items, "next_page": page + 1 if has_more else None}

    async def read_public_thread(self, event, *, resume=None, checkpoint=None):
        account = await self.self_uid()
        own_comment = event.get("kind") == "own_comment"
        if event.get("account_id") != account or event.get("kind") not in {"mention", "reply", "own_comment"}:
            raise PlatformReadError("permission", "通知不属于当前账号或来源类型不受支持")
        target = self._target(event)
        dynamic = None
        if target["resource_type"] == "dynamic":
            dynamic_id = _identifier(event.get("dynamic_id") or target["oid"])
            dynamic = await self._dynamic_detail(dynamic_id)
            if dynamic.get("type") not in {"DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW"}:
                raise PlatformReadError("unsupported", "仅支持文字和图文动态评论")
            basic = dynamic.get("basic") or {}
            kind = basic.get("comment_type")
            oid = basic.get("comment_id_str")
            if (target["type"] == 11 or str(kind) == "11") and (kind is None or not oid):
                raise PlatformReadError("invalid", "动态详情缺少图文评论区和类型")
            if str(oid or dynamic_id) != target["oid"]:
                raise PlatformReadError("invalid", "动态评论区与来源不一致")
            if kind is not None:
                if str(kind) not in {"11", "17"} or ("type" in event and int(kind) != target["type"]):
                    raise PlatformReadError("invalid", "动态评论类型与来源不一致")
                target["type"] = int(kind)
            target["dynamic_id"] = dynamic_id
        items, coverage = await self._thread_items(target, resume=resume, checkpoint=checkpoint)
        if not items:
            raise PlatformReadError("not_found", "原始楼不可读或已删除")
        source = next((row for row in items if row["comment_id"] == event["comment_id"]), None)
        if source is None or source["platform_user_id"] != event["sender_uid"]:
            raise PlatformReadError("not_found", "当前页未核验到通知原文与作者，不猜测回复对象")
        if source["thread_id"] != target["root"]:
            raise PlatformReadError("invalid", "通知与原始楼不一致")
        if event.get("source_text") and source["content"] != event["source_text"]:
            raise PlatformReadError("invalid", "通知原文已变化，需要重新核验")
        if event["kind"] == "reply":
            original_id = source["parent_comment_id"]
            original = next((row for row in items if row["comment_id"] == original_id), None)
            if original is None or original["platform_user_id"] != account:
                raise PlatformReadError("permission", "收到回复未关联当前账号的真实评论")
        public_text = ""
        if target["resource_type"] == "video":
            info = await self._video("av" + target["oid"])
            resource_author_uid = (info.get("owner") or {}).get("mid")
            if str(info["aid"]) != target["oid"]:
                raise PlatformReadError("invalid", "通知视频对象不一致")
            if own_comment:
                if str(resource_author_uid) != account:
                    raise PlatformReadError("permission", "该视频不属于当前账号")
                public_text = _text(info.get("title") or "", 1000)
            target["bvid"] = info["bvid"]
        else:
            modules = dynamic.get("modules") or {}
            resource_author_uid = (modules.get("module_author") or {}).get("mid")
            if own_comment and str(resource_author_uid) != account:
                raise PlatformReadError("permission", "该动态不属于当前账号")
            public_text = _text(((modules.get("module_dynamic") or {}).get("desc") or {}).get("text"))
        return {
            "account_id": account,
            "resource_type": target["resource_type"],
            "resource_author_uid": resource_author_uid,
            "target": target,
            "source": {
                "comment_id": source["comment_id"],
                "author_uid": source["platform_user_id"],
                "text": source["content"],
                "display_name": source["display_name"],
            },
            "items": items,
            "public_text": public_text,
            **({"dynamic_source": self._dynamic_reaction_source(dynamic)} if dynamic else {}),
            "coverage": coverage,
        }

    async def read_video_reply(self, target):
        """Read the actual video thread for replies selected during discovery."""
        thread = await self.read_video_thread(target)
        return {key: thread["source"][key] for key in ("author_uid", "text")}

    async def read_video_thread(self, target, *, resume=None, checkpoint=None):
        """Refresh one discussion without moving the video's browsing cursor."""
        if not target.get("oid"):
            video = await self._video(target["bvid"])
            if video["bvid"] != target["bvid"]:
                raise PlatformReadError("invalid", "原始视频与评论来源不一致")
            target = target | {"oid": video["aid"]}
        scoped = self._target(target | {"resource_type": "video"})
        items, coverage = await self._thread_items(scoped, resume=resume, checkpoint=checkpoint)
        parent = scoped["parent"]
        source = next((row for row in items if row["comment_id"] == parent), None)
        if source is None:
            raise PlatformReadError("not_found", "当前楼未找到原评论")
        return {
            "target": dict(target),
            "source": {
                "comment_id": source["comment_id"],
                "author_uid": source["platform_user_id"],
                "author_name": source["display_name"],
                "text": source["content"],
            },
            "items": items,
            "coverage": coverage,
        }

    async def _publish_target(self, target, text, before_send, *, image=None, prepare_authorized=None, on_uploaded=None):
        from .mentions import comment_mention_form

        if not self._sessdata or not self._csrf:
            raise ValueError("发布需要当前账号SESSDATA及CSRF")
        text = validate_comment_body(text)
        mention_form = comment_mention_form(text, target.get("mentions", []))
        await self.ensure_session_cookies()
        params = await self.wbi_params()
        from .images import comment_pictures

        pictures = await comment_pictures(self, image, prepare_authorized, on_uploaded)
        await self._prepare_comment_engagement(
            oid=target["oid"],
            comment_type=target["type"],
            root=target["root"],
            bvid=target.get("bvid"),
            cid=target.get("cid"),
        )
        if before_send is not None:
            before_send()
        bvid_raw = str(target.get("bvid") or "").strip()
        referer = f"https://www.bilibili.com/video/{bvid_raw}/" if _BV.fullmatch(bvid_raw) else "https://www.bilibili.com/"
        try:
            response = await self.client.post(
                "/x/v2/reply/add",
                params=params,
                headers={"Origin": "https://www.bilibili.com", "Referer": referer},
                data={
                    "oid": target["oid"],
                    "type": target["type"],
                    "root": target["root"],
                    "parent": target["parent"],
                    "message": text,
                    **mention_form,
                    **pictures,
                    "plat": 1,
                    "csrf": self._csrf,
                    "csrf_token": self._csrf,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("评论发布结果待核对") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("B 站返回的评论回执格式无效")
        if payload["code"] != 0:
            raise platform_rejection(
                payload,
                fallback="平台明确拒绝了本次评论",
                secret_values=(self._sessdata, self._csrf),
            )
        try:
            data = payload["data"]
            rpid = _identifier(data.get("rpid_str") or data.get("rpid"))
        except (KeyError, TypeError, AttributeError, ValueError):
            raise PublishUncertain("B 站回执缺少评论 ID") from None
        diagnostics = {}
        if type(data.get("success_action")) is int:
            diagnostics["success_action"] = data["success_action"]
        if type(data.get("need_captcha")) is bool:
            diagnostics["need_captcha"] = data["need_captcha"]
        reply_obj = data.get("reply")
        if isinstance(reply_obj, dict) and "invisible" in reply_obj:
            diagnostics["reply_invisible"] = reply_obj.get("invisible") in (True, 1, "1")
        toast = data.get("success_toast")
        if isinstance(toast, str) and toast.strip():
            clean_toast = toast.strip()[:200]
            if not any(secret and secret in clean_toast for secret in (self._sessdata, self._csrf)):
                diagnostics["success_toast"] = clean_toast
        return {
            "platform": "bilibili",
            "code": 0,
            "rpid": rpid,
            **{key: target[key] for key in ("oid", "type", "root", "parent")},
            **diagnostics,
        }

    async def publish_top_level(self, target, text, before_send=None, **images):
        cid = target.get("cid") if isinstance(target, dict) else None
        target = self._target(target, top_level=True) | {"mentions": target.get("mentions", [])}
        if cid:
            target["cid"] = cid
        if images.get("image") is not None and target["type"] != 1:
            raise ValueError("仅视频一级评论提供配图能力")
        return await self._publish_target(target, text, before_send, **images)

    async def publish_resource_reply(self, target, text, before_send=None):
        cid = target.get("cid") if isinstance(target, dict) else None
        target = self._target(target) | {"mentions": target.get("mentions", [])}
        if cid:
            target["cid"] = cid
        return await self._publish_target(target, text, before_send)

    async def like_comment(self, target, *, before_send=None):
        target = self._target(target)
        if not self._sessdata or not self._csrf:
            raise ValueError("评论点赞需要当前账号SESSDATA及CSRF")
        params = await self.wbi_params()
        if before_send is not None:
            before_send()
        try:
            response = await self.client.post("/x/v2/reply/action", params=params, data={
                "oid": target["oid"], "type": target["type"], "rpid": target["parent"],
                "action": 1, "csrf": self._csrf, "csrf_token": self._csrf,
            })
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("评论点赞结果待核对") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("B 站返回的点赞回执格式无效")
        if payload["code"] != 0:
            raise platform_rejection(
                payload,
                fallback="平台明确拒绝了本次评论点赞",
                secret_values=(self._sessdata, self._csrf),
            )
        return {"platform": "bilibili", "code": 0, "action": "comment_like", "oid": target["oid"],
                "type": target["type"], "rpid": target["parent"]}

    async def like_video(self, target, *, like: bool = True, before_send=None):
        video_id = target["bvid"] if isinstance(target, dict) else str(target)
        return await super().like_video(video_id, like=like, before_send=before_send)

    async def favorite_video(self, target, *, folder_id=None, before_send=None):
        video_id = target["bvid"] if isinstance(target, dict) else str(target)
        return await super().favorite_video(video_id, folder_id=folder_id, before_send=before_send)

    async def _anonymous_wbi_params(self, params: dict) -> dict:
        try:
            return await self.wbi_params(params, anonymous=True)
        except PlatformReadError as error:
            if error.kind in {"restricted", "network", "permission"}:
                raise
            return dict(params)
        except ValueError:
            return dict(params)

    @staticmethod
    def _find_reply_row(rows, rpid):
        if not isinstance(rows, list):
            raise PlatformReadError("invalid", "可见性响应无效")
        ctimes = []
        matched = None
        for row in rows:
            if not isinstance(row, dict):
                raise PlatformReadError("invalid", "可见性响应无效")
            ctime = row.get("ctime")
            if type(ctime) is int and ctime > 0:
                ctimes.append(ctime)
            if str(row.get("rpid_str") or row.get("rpid")) == rpid:
                matched = row
        oldest_ctime = min(ctimes) if ctimes else None
        if matched is None:
            return False, False, None, oldest_ctime
        hidden = matched.get("invisible") in (True, 1, "1")
        matched_ctime = matched.get("ctime") if type(matched.get("ctime")) is int else None
        return True, hidden, matched_ctime, oldest_ctime

    async def _seek_publication(self, target, rpid, *, anonymous: bool):
        base = {
            "oid": target["oid"], "type": target["type"], "mode": 3, "plat": 1,
            "pagination_str": '{"offset":""}', "seek_rpid": rpid, "web_location": "1315875",
        }
        sign = self._anonymous_wbi_params if anonymous else self.wbi_params
        data = await self._get("/x/v2/reply/wbi/main", await sign(base), anonymous=anonymous)
        roots = data.get("replies") or []
        rows = list(roots) + (data.get("top_replies") or [])
        for row in rows:
            rows.extend(row.get("replies") or [])
        matches = [row for row in rows if all(
            str(row.get(key + "_str") or row.get(key, target[key])) == target[key]
            for key in ("root", "parent")
        )]
        found, invisible, ctime, oldest_ctime = self._find_reply_row(matches, rpid)
        cursor = data.get("cursor") if isinstance(data.get("cursor"), dict) else {}
        return {
            "found": found, "invisible": invisible, "ctime": ctime,
            "covered": target["root"] == "0" and cursor.get("is_end") is True and cursor.get("all_count") == len(roots),
            "records": len(roots), "oldest_ctime": oldest_ctime, "checked_page": 1,
            "seek_attempted": True, "seek_found": found,
        }

    async def _inspect_publication_view(self, normalized, rpid, *, anonymous: bool):
        is_reply = normalized["root"] != "0"
        if not is_reply and normalized["resource_type"] == "dynamic":
            return await self._seek_publication(normalized, rpid, anonymous=anonymous)
        if not is_reply:
            base = {"oid": normalized["oid"], "type": normalized["type"], "pn": 1, "ps": 20, "sort": 0}
            params = await self._anonymous_wbi_params(base) if anonymous else base
            data = await self._get("/x/v2/reply", params, anonymous=anonymous)
            rows = data.get("replies") or []
            found, invisible, ctime, oldest_ctime = self._find_reply_row(rows, rpid)
            page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
            count = page_info.get("count") if type(page_info.get("count")) is int else None
            return {
                "found": found,
                "invisible": invisible,
                "ctime": ctime,
                "covered": len(rows) < 20,
                "records": len(rows),
                "oldest_ctime": oldest_ctime,
                "page_count": count,
                "checked_page": 1,
            }
        base = {
            "oid": normalized["oid"],
            "type": normalized["type"],
            "root": normalized["root"],
            "pn": 1,
            "ps": 20,
        }
        params = await self._anonymous_wbi_params(base) if anonymous else base
        data = await self._get("/x/v2/reply/reply", params, anonymous=anonymous)
        page_replies = data.get("replies") or []
        roots = [data["root"]] if data.get("root") else []
        rows = roots + page_replies
        found, invisible, ctime, oldest_ctime = self._find_reply_row(rows, rpid)
        page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
        count = page_info.get("count") if type(page_info.get("count")) is int else None
        size = page_info.get("size") if type(page_info.get("size")) is int and page_info.get("size") > 0 else 20
        checked_page = 1
        records = len(page_replies)
        covered = records < size or (count is not None and count <= size)
        if not found and count is not None and count > size:
            last_page = math.ceil(count / size)
            if last_page > 1:
                tail_base = {**base, "pn": last_page, "ps": size}
                tail_params = await self._anonymous_wbi_params(tail_base) if anonymous else tail_base
                tail_data = await self._get("/x/v2/reply/reply", tail_params, anonymous=anonymous)
                tail_replies = tail_data.get("replies") or []
                if tail_data.get("root"):
                    roots.append(tail_data["root"])
                    rows.append(tail_data["root"])
                rows.extend(tail_replies)
                found, invisible, ctime, tail_oldest = self._find_reply_row(tail_replies, rpid)
                oldest_ctime = tail_oldest if tail_oldest is not None else oldest_ctime
                checked_page = last_page
                records = len(tail_replies)
                covered = last_page == 2 and page_info.get("count") == (
                    tail_data.get("page") or {}
                ).get("count")
        root_found, root_hidden, _, _ = self._find_reply_row(roots, normalized["root"])
        parent_found, parent_hidden, _, _ = self._find_reply_row(rows, normalized["parent"])
        return {
            "found": found,
            "invisible": invisible,
            "ctime": ctime,
            "covered": covered,
            "records": records,
            "oldest_ctime": oldest_ctime,
            "page_count": count,
            "checked_page": checked_page,
            "root_visible": root_found and not root_hidden,
            "parent_visible": parent_found and not parent_hidden,
        }

    async def _read_publication_view(self, target, rpid, *, anonymous: bool):
        listed = await self._inspect_publication_view(target, rpid, anonymous=anonymous)
        if listed["found"] or listed.get("seek_attempted"):
            return listed
        exact = await self._seek_publication(target, rpid, anonymous=anonymous)
        if exact["found"]:
            return listed | {key: exact[key] for key in ("found", "invisible", "ctime", "seek_attempted", "seek_found")}
        return listed | {"covered": listed["covered"] or exact["covered"], "seek_attempted": True, "seek_found": False}

    async def verify_publication(self, target, receipt):
        checked_at = time.time()
        try:
            rpid = _identifier(receipt.get("rpid"))
            root = target.get("root")
            normalized = self._target(target, top_level=root in (None, 0, "0"))
        except (ValueError, TypeError, AttributeError):
            return {
                "status": "check_failed",
                "checked_at": checked_at,
                "reason": "平台可见性核验参数无效",
                "error": "平台可见性核验参数无效",
            }
        is_reply = normalized["root"] != "0"
        auth_view = None
        try:
            auth_view = await self._read_publication_view(normalized, rpid, anonymous=False)
            anon_view = await self._read_publication_view(normalized, rpid, anonymous=True)
        except PlatformReadError as error:
            return {
                "status": "check_failed",
                "checked_at": time.time(),
                "error_code": error.code if error.code is not None else error.http_status,
                "error_kind": error.kind,
                "reason": str(error),
                "error": str(error),
                "auth_visible": (
                    (auth_view["found"] and not auth_view["invisible"]) if auth_view is not None else None
                ),
                "anon_visible": None,
            }
        except (ValueError, TypeError, AttributeError):
            return {
                "status": "check_failed",
                "checked_at": time.time(),
                "reason": "平台可见性核验失败",
                "error": "平台可见性核验失败",
            }

        published_at = receipt.get("published_at")
        if not isinstance(published_at, (int, float)) or not math.isfinite(published_at):
            published_at = None
        ref_time = auth_view["ctime"] or published_at
        auth_covered = auth_view["covered"]
        anon_covered = anon_view["covered"]
        dynamic_top_level = not is_reply and normalized["resource_type"] == "dynamic"
        if not is_reply and not dynamic_top_level:
            if not auth_covered and published_at is not None and auth_view["oldest_ctime"] is not None:
                auth_covered = auth_view["oldest_ctime"] <= float(published_at) + 2
            if not anon_covered and ref_time is not None and anon_view["oldest_ctime"] is not None:
                anon_covered = anon_view["oldest_ctime"] <= float(ref_time) + 2

        auth_visible = auth_view["found"] and not auth_view["invisible"]
        anon_visible = anon_view["found"] and not anon_view["invisible"]
        anon_target_public = not is_reply or (anon_view["root_visible"] and anon_view["parent_visible"])
        if anon_visible:
            status = "public_visible"
        elif auth_view["found"] and auth_view["invisible"]:
            status = "shadow_deleted"
        elif auth_visible and ((anon_view["found"] and anon_view["invisible"]) or (anon_covered and anon_target_public)):
            status = "self_only"
        else:
            status = "not_found"

        hints = {
            key: receipt[key]
            for key in ("success_action", "need_captcha", "reply_invisible", "success_toast")
            if key in receipt
        }
        return {
            "status": status,
            "checked_at": time.time(),
            "auth_visible": auth_visible,
            "anon_visible": anon_visible,
            "auth_found": auth_view["found"],
            "anon_found": anon_view["found"],
            "auth_invisible": auth_view["invisible"],
            "anon_invisible": anon_view["invisible"],
            "coverage": {
                "scope": "thread_tail" if is_reply else "dynamic_seek" if dynamic_top_level else "first_page_newest",
                "auth_covered": auth_covered,
                "anon_covered": anon_covered,
                "auth_page": auth_view["checked_page"],
                "anon_page": anon_view["checked_page"],
                "auth_records": auth_view["records"],
                "anon_records": anon_view["records"],
                "anon_oldest_ctime": anon_view["oldest_ctime"],
                "auth_seek_found": auth_view.get("seek_found"),
                "anon_seek_found": anon_view.get("seek_found"),
                **({"anon_root_visible": anon_view["root_visible"],
                    "anon_parent_visible": anon_view["parent_visible"]} if is_reply else {}),
            },
            **({"receipt_hints": hints} if hints else {}),
            **({"reason": "匿名读回未核实原评论楼和父评论均可见"}
               if status == "not_found" and anon_covered and not anon_target_public else {}),
        }
