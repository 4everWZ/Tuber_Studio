"""Read the source a task named, including a specific part of a Bilibili submission."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..infra.platforms.bilibili.browsing import BrowsingAdapter
from ..infra.platforms.bilibili.client import PlatformReadError
from ..infra.web_client.fetch import MAX_EXCERPT, source_url


def published_on(value, day, timezone, *, now=None):
    if not isinstance(value, str):
        return False
    try:
        published = datetime.fromisoformat(value)
    except ValueError:
        return False
    if published.tzinfo:
        published = published.astimezone(ZoneInfo(timezone))
        if published > (now or datetime.now(published.tzinfo)):
            return False
    return published.date().isoformat() == day


def video_read_restriction(error, bvid, part, stage):
    """Only a classified platform restriction is exposed, never the transport URL or body."""
    if not isinstance(error, PlatformReadError) or error.kind != "restricted":
        return {}
    cause = "平台限制读取"
    if error.http_status:
        cause += f"（HTTP {error.http_status}）"
    source = f"{bvid} P{part}" if part else bvid
    options = ("请补充可核对该分 P 投稿信息的来源链接" if stage == "投稿页" else
               "请补充可读取的原片来源，或指定并授权使用已有真实素材（注明分 P 和对应时段）")
    message = f"{source} 的{stage}遇到{cause}。{options}，也可等平台恢复后在本任务继续。"
    return {"read_restriction": {"bvid": bvid, "part": part, "stage": stage, "http_status": error.http_status},
            "message": message}


def restriction_messages(steps, *, blocking_only=False):
    messages = []
    for step in steps:
        result = step.get("result") or {}
        if (step["tool"] in {"inspect_video", "web_page"} and result.get("read_restriction")
                and (not blocking_only or result.get("error")) and result.get("message")):
            messages.append(result["message"])
    return "\n".join(dict.fromkeys(messages))


async def fetch_task_source(value, fetch_page):
    url = source_url(value)
    video = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", url.path)
    if (url.scheme != "https" or url.host != "www.bilibili.com" or url.port is not None
            or url.userinfo or not video or set(url.params) - {"p"}):
        return await fetch_page(value)
    pages = url.params.get_list("p") or ["1"]
    if len(pages) != 1 or not re.fullmatch(r"[1-9]\d{0,3}", pages[0]):
        raise ValueError("投稿页的分 P 编号无效")
    adapter = BrowsingAdapter()
    adapter.read_video_tags = False
    try:
        metadata = await adapter.video_metadata(video[1])
    except PlatformReadError as error:
        restriction = video_read_restriction(error, video[1], int(pages[0]), "投稿页")
        if not restriction:
            raise
        return restriction | {"url": str(url), "error": restriction["message"]}
    finally:
        await adapter.close()
    part = next((row for row in metadata["parts"] if row["page"] == int(pages[0])), None)
    if not part or metadata["bvid"] != video[1]:
        raise ValueError("公开投稿资料无法核对指定分 P")
    text = (f"投稿《{metadata['title']}》，UP 主：{metadata['owner']}。"
            f"合集 P{part['page']}《{part['title']}》，CID {part['cid']}，时长 {part['duration']} 秒。"
            f"投稿简介：{metadata['description']}")
    return {"url": str(url), "title": metadata["title"], "excerpt": text[:MAX_EXCERPT],
            "truncated": len(text) > MAX_EXCERPT, "source_kind": "bilibili_public_metadata"}
