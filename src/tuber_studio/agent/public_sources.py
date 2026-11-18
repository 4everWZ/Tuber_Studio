"""Read the source a task named, including a specific part of a Bilibili submission."""

import re

from ..platforms.bilibili_browsing import BrowsingAdapter
from ..web_fetch import MAX_EXCERPT, source_url


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
    try:
        metadata = await adapter.video_metadata(video[1])
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
