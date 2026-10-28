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
import json
import re
from urllib.parse import urlsplit

import httpx


class PublishUncertain(ValueError):
    """A write may have reached Bilibili; the operator must reconcile it."""


_BV = re.compile(r"BV1[0-9A-Za-z]{9}\Z")
_AV = re.compile(r"av([1-9][0-9]{0,18})\Z", re.IGNORECASE)
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


class BilibiliAdapter:
    def __init__(self, credential: dict | None = None):
        credential = credential or {}
        self._sessdata = str(credential.get("sessdata") or "")
        self._csrf = str(credential.get("csrf") or "")
        cookies = {}
        if self._sessdata:
            cookies["SESSDATA"] = self._sessdata
        if self._csrf:
            cookies["bili_jct"] = self._csrf
        self.client = httpx.AsyncClient(
            base_url="https://api.bilibili.com",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            cookies=cookies,
            timeout=httpx.Timeout(20, connect=10),
            follow_redirects=False,
        )

    async def close(self):
        await self.client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        try:
            response = await self.client.get(path, params=params)
            response.raise_for_status()
            return _data(response.json())
        except httpx.HTTPStatusError as error:
            raise ValueError(
                f"B 站读取失败（HTTP {error.response.status_code}），请检查权限或风控状态"
            ) from None
        except httpx.RequestError:
            raise ValueError("B 站读取连接失败或超时，请稍后重试") from None
        except json.JSONDecodeError:
            raise ValueError("B 站返回了非 JSON 响应，请检查平台访问状态") from None
        except (TypeError, KeyError):
            raise ValueError("B 站返回了无效数据") from None

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
        return data

    @staticmethod
    def _comment(reply, *, thread_id=None, parent_comment_id=None):
        cid = _positive_id(reply.get("rpid_str") or reply["rpid"])
        root = reply.get("root_str") or reply.get("root")
        parent = reply.get("parent_str") or reply.get("parent")
        if isinstance(root, bool) or isinstance(parent, bool):
            raise ValueError("评论楼标识无效")
        root = _positive_id(root) if root not in (None, 0, "0", "") else thread_id or cid
        parent = _positive_id(parent) if parent not in (None, 0, "0", "") else parent_comment_id
        if thread_id is not None and root != thread_id:
            raise ValueError("评论楼标识不一致")
        member, content = reply["member"], reply["content"]
        return {
            "comment_id": cid,
            "thread_id": root,
            "parent_comment_id": parent,
            "platform_user_id": _positive_id(member["mid"]),
            "display_name": str(member["uname"]),
            "content": str(content["message"]),
            "like_count": max(0, int(reply.get("like", 0))),
            "created_at": str(int(reply.get("ctime", 0))),
        }

    async def fetch(self, video: str, max_pages: int = 1) -> dict:
        if type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise ValueError("每次读取页数须为 1 到 10")
        info = await self._video(video)
        items, seen = [], set()
        for page in range(1, max_pages + 1):
            if page > 1:
                await asyncio.sleep(0.5)
            data = await self._get(
                "/x/v2/reply",
                {
                    "oid": info["aid"],
                    "type": 1,
                    "pn": page,
                    "ps": 20,
                    "sort": 2,
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
            if not replies or (isinstance(count, int) and page * 20 >= count):
                break
        return {
            "video_id": info["bvid"],
            "items": items,
            "title": str(info.get("title", "")),
            "aid": info["aid"],
        }

    async def publish(
        self, video_id: str, comment_id: str, text: str, *, thread_id: str | None = None
    ) -> dict:
        if not self._sessdata or not self._csrf:
            raise ValueError("发布需要配置 SESSDATA 和 CSRF；当前仅可生成或复制草稿")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError("回复须为 1 到 1000 字")
        parent = _positive_id(comment_id)
        root = _positive_id(thread_id) if thread_id is not None else parent
        info = await self._video(video_id)
        try:
            response = await self.client.post(
                "/x/v2/reply/add",
                data={
                    "oid": info["aid"],
                    "type": 1,
                    "root": root,
                    "parent": parent,
                    "message": text,
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
            _data(payload)
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
