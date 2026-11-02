"""Bounded platform evidence and account notification adapter.

Only fixed platform API paths and verified resource identifiers reach requests.
Endpoint/signing semantics were checked against the PyPI bilibili-api-python
17.4.2 source distribution. The workflow reference is dsh-bili-scroll 398e4724,
not its permissions, prompts, ASR fallback, or host. Reads never mark read.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx

from .bilibili import _BV, BilibiliAdapter, PublishUncertain, _positive_id, _video_params
from .operation_text import validate_operation_text
from .publication_errors import platform_rejection

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_API_BYTES = 4 * 1024 * 1024
SUBTITLE_HOSTS = {"aisubtitle.hdslb.com", "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}
CREDIT = re.compile(r"^(?:字幕(?:制作|提供|来源|组|by)|(?:翻译|听译|校对|时间轴|轴师|字幕员)[:：\s])", re.I)


class PlatformReadError(ValueError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


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

    def __init__(self, credential=None):
        super().__init__(credential)
        self.request_gate = None
        self._read_spacing = 0.1
        self._read_clock = 0.0
        self._read_gate = asyncio.Lock()
        self._frame_sampler = None

    async def video_frames(self, metadata, count=3, *, cid=None):
        from .bilibili_frames import FrameSampler

        if self._frame_sampler is None:
            self._frame_sampler = FrameSampler(self)
        return await self._frame_sampler.frames(metadata, count, cid=cid)

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

    async def _get_payload(self, path, params):
        await self._pace()
        try:
            async with self.client.stream("GET", path, params=params, follow_redirects=False) as response:
                if response.status_code in {401, 403}:
                    raise PlatformReadError(
                        "permission", f"平台拒绝读取（HTTP {response.status_code}），请检查登录与权限"
                    )
                if response.status_code in {412, 429}:
                    raise PlatformReadError(
                        "restricted", f"平台限制读取（HTTP {response.status_code}），请稍后重试"
                    )
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
        anonymous_nav = code == -101 and path == "/x/web-interface/nav" and not self._sessdata
        if code in {-101, -102, -111, -403} and not anonymous_nav:
            raise PlatformReadError("permission", "平台账号未登录、失效或无读取权限")
        if code in {-352, -412, -509}:
            raise PlatformReadError("restricted", "平台风控或频率限制")
        if code in {-404, 12006}:
            raise PlatformReadError("not_found", "平台来源不存在或已删除")
        if code != 0 and not anonymous_nav:
            raise PlatformReadError("platform", f"平台拒绝读取（code {code}）")
        return payload

    async def _read(self, path, params):
        payload = await self._get_payload(path, params)
        # Only anonymous WBI nav is allowed through _get_payload with a
        # nonzero code. It supplies signing keys, not authenticated content.
        if payload["code"] != 0:
            raise PlatformReadError("permission", "平台账号未登录、失效或无读取权限")
        if not isinstance(payload.get("data"), (dict, list)):
            raise PlatformReadError("invalid", "平台返回无效数据")
        return payload["data"]

    async def _get(self, path, params):
        data = await self._read(path, params)
        if not isinstance(data, dict):
            raise PlatformReadError("invalid", "平台返回对象类型错误")
        return data

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
        )
        if not isinstance(tags, list):
            raise PlatformReadError("invalid", "平台标签格式无效")
        return {
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
                    raise PlatformReadError(
                        "restricted", f"字幕下载受平台限制（HTTP {response.status_code}）"
                    )
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
        return {
            **{key: track[key] for key in ("bvid", "cid", "track_id", "language", "is_ai")},
            "segments": segments,
            "content_hash": hashlib.sha256(buffer).hexdigest(),
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
            resource = {1: "video", 17: "dynamic"}.get(item.get("business_id"))
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
        resource = {1: "video", 17: "dynamic"}.get(resource, resource)
        if resource not in {"video", "dynamic"}:
            raise PlatformReadError("unsupported", "暂不支持该通知资源的回复")
        oid = _identifier(target.get("oid") or target.get("aid"))
        root, parent = "0", "0"
        if not top_level:
            root = _identifier(target.get("root"))
            parent = _identifier(target.get("parent") or target.get("comment_id"))
        elif target.get("root") not in {None, 0, "0"} or target.get("parent") not in {None, 0, "0"}:
            raise ValueError("顶层留言不能携带回复楼或父评论")
        return {
            "resource_type": resource,
            "type": 1 if resource == "video" else 17,
            "oid": oid,
            "root": root,
            "parent": parent,
            "bvid": target.get("bvid"),
        }

    async def read_public_thread(self, event):
        account = await self.self_uid()
        if event.get("account_id") != account or event.get("kind") not in {"mention", "reply"}:
            raise PlatformReadError("permission", "通知不属于当前账号或来源类型不受支持")
        target = self._target(event)
        data = await self._get(
            "/x/v2/reply/reply",
            {
                "oid": target["oid"],
                "type": target["type"],
                "root": target["root"],
                "pn": 1,
                "ps": 20,
            },
        )
        root = data.get("root")
        replies = data.get("replies")
        if replies is not None and not isinstance(replies, list):
            raise PlatformReadError("invalid", "原始楼回复结构无效")
        raw = ([root] if root else []) + (replies or [])
        if not raw:
            raise PlatformReadError("not_found", "原始楼不可读或已删除")
        try:
            items = [self._comment(row) for row in raw]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PlatformReadError("invalid", "原始楼关系无效") from None
        source = next((row for row in items if row["comment_id"] == event["comment_id"]), None)
        if source is None or source["platform_user_id"] != event["sender_uid"]:
            raise PlatformReadError("not_found", "当前页未核验到通知原文与作者，不猜测回复对象")
        if source["thread_id"] != target["root"]:
            raise PlatformReadError("invalid", "通知与原始楼不一致")
        if event.get("source_text") and source["content"] != event["source_text"]:
            raise PlatformReadError("invalid", "通知原文已变化，需要重新核验")
        if event["kind"] == "reply":
            original_id = source["parent_comment_id"] or target["root"]
            original = next((row for row in items if row["comment_id"] == original_id), None)
            if original is None or original["platform_user_id"] != account:
                raise PlatformReadError("permission", "收到回复未关联当前账号的真实评论")
        public_text = ""
        if target["resource_type"] == "video":
            info = await self._video("av" + target["oid"])
            if str(info["aid"]) != target["oid"]:
                raise PlatformReadError("invalid", "通知视频对象不一致")
            target["bvid"] = info["bvid"]
        else:
            signed = await self.wbi_params({"id": target["oid"], "timezone_offset": -480})
            dynamic = await self._get("/x/polymer/web-dynamic/v1/detail", signed)
            item = dynamic.get("item")
            if not isinstance(item, dict) or str(item.get("id_str")) != target["oid"]:
                raise PlatformReadError("not_found", "原始文字动态不可读或对象不一致")
            if item.get("type") != "DYNAMIC_TYPE_WORD":
                raise PlatformReadError("unsupported", "仅支持文字动态评论，其他动态类型需单独适配")
            modules = item.get("modules") or {}
            public_text = _text(((modules.get("module_dynamic") or {}).get("desc") or {}).get("text"))
            if not public_text.strip():
                raise PlatformReadError("empty", "原始文字动态没有有效正文")
        return {
            "account_id": account,
            "resource_type": target["resource_type"],
            "target": target,
            "source": {
                "comment_id": source["comment_id"],
                "author_uid": source["platform_user_id"],
                "text": source["content"],
                "display_name": source["display_name"],
            },
            "items": items,
            "public_text": public_text,
            "coverage": {"complete": False, "page": 1, "reason": "bounded_thread_page"},
        }

    async def _publish_target(self, target, text, before_send):
        if not self._sessdata or not self._csrf:
            raise ValueError("发布需要当前账号SESSDATA及CSRF")
        text = validate_operation_text(text, label="评论正文")
        params = await self.wbi_params()
        if before_send is not None:
            before_send()
        try:
            response = await self.client.post(
                "/x/v2/reply/add",
                params=params,
                data={
                    "oid": target["oid"],
                    "type": target["type"],
                    "root": target["root"],
                    "parent": target["parent"],
                    "message": text.strip(),
                    "plat": 1,
                    "csrf": self._csrf,
                    "csrf_token": self._csrf,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishUncertain("评论提交结果不确定，禁止自动重试") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("评论回执无效，禁止自动重试")
        if payload["code"] != 0:
            raise platform_rejection(payload, fallback="平台明确拒绝了本次评论")
        try:
            data = payload["data"]
            rpid = _identifier(data.get("rpid_str") or data.get("rpid"))
        except (KeyError, TypeError, AttributeError, ValueError):
            raise PublishUncertain("评论回执缺少有效ID，禁止自动重试") from None
        return {
            "platform": "bilibili",
            "code": 0,
            "rpid": rpid,
            **{key: target[key] for key in ("oid", "type", "root", "parent")},
        }

    async def publish_top_level(self, target, text, before_send=None):
        target = self._target(target, top_level=True)
        return await self._publish_target(target, text, before_send)

    async def publish_resource_reply(self, target, text, before_send=None):
        target = self._target(target)
        return await self._publish_target(target, text, before_send)

    async def verify_publication(self, target, receipt):
        try:
            rpid = _identifier(receipt.get("rpid"))
            root = target.get("root")
            normalized = self._target(target, top_level=root in (None, 0, "0"))
            params = {"oid": normalized["oid"], "type": normalized["type"], "pn": 1, "ps": 20}
            if normalized["root"] != "0":
                params["root"] = normalized["root"]
                data = await self._get("/x/v2/reply/reply", params)
                rows = ([data["root"]] if data.get("root") else []) + (data.get("replies") or [])
            else:
                data = await self._get("/x/v2/reply", params)
                rows = data.get("replies") or []
            if not isinstance(rows, list):
                raise PlatformReadError("invalid", "可见性响应无效")
            for row in rows:
                if not isinstance(row, dict):
                    raise PlatformReadError("invalid", "可见性响应无效")
                if str(row.get("rpid_str") or row.get("rpid")) == rpid:
                    hidden = row.get("invisible") is True or row.get("invisible") == 1
                    return {"status": "not_visible" if hidden else "visible", "checked_at": time.time()}
            return {"status": "not_found", "checked_at": time.time(), "coverage": "first_page_only"}
        except (ValueError, TypeError, AttributeError):
            return {"status": "check_failed", "checked_at": time.time(), "error": "平台可见性核验失败"}
