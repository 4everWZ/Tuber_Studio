"""One bounded BFS image upload using bilibili-api-python 17.4.2's protocol.

Uploads are preparation, not proof of a delivered message. The caller performs
one final publication and rechecks its original authorization after this upload.
"""

import asyncio
import json
import math
from urllib.parse import urlsplit

import httpx

from .bilibili import _data
from .memes import MAX_BYTES

UPLOAD_URL = "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"
FORMATS = {"image/gif": "gif", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


async def upload_image(adapter, image, *, authorized):
    if not adapter._sessdata or not adapter._csrf:
        raise ValueError("上传图片需要SESSDATA和bili_jct")
    mime, content = image.get("mime_type"), image.get("content")
    if mime not in FORMATS or not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
        raise ValueError("图片必须来自已验证的本地表情图库")
    if authorized is not None and not authorized():
        raise ValueError("图片发送授权已停止，保留草稿")
    try:
        async with asyncio.timeout(30):
            async with adapter.client.stream(
                "POST",
                UPLOAD_URL,
                data={
                    "biz": "new_dyn",
                    "category": "daily",
                    "csrf": adapter._csrf,
                    "csrf_token": adapter._csrf,
                },
                files={"file_up": ("meme." + FORMATS[mime], content, mime)},
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > 65536:
                        raise ValueError("图片上传响应过大")
                data = _data(json.loads(result))
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise ValueError("表情上传失败，尚未提交消息／动态；可重新尝试") from None
    url = data.get("image_url")
    if not isinstance(url, str):
        raise ValueError("平台未返回有效图片地址，尚未提交消息／动态")
    url = "https:" + url if url.startswith("//") else url.replace("http://", "https://", 1)
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and parsed.path.startswith("/bfs/")
            and ".." not in parsed.path
            and "%" not in parsed.path
            and not parsed.query
            and not parsed.fragment
            and len(url) <= 2048
            and not any(ord(c) < 33 for c in url)
        )
    except ValueError:
        valid = False
    width, height, size = data.get("image_width"), data.get("image_height"), data.get("img_size")
    if (
        not valid
        or any(type(n) is not int or not 1 <= n <= 4096 for n in (width, height))
        or (type(size) not in {int, float} or not math.isfinite(size) or not 0 < size <= MAX_BYTES / 1024)
    ):
        raise ValueError("平台图片回执无效，尚未提交消息／动态")
    extension = parsed.path.rsplit(".", 1)[-1].lower()
    if extension not in {"png", "jpg", "jpeg", "gif", "webp"}:
        raise ValueError("平台图片格式无效，尚未提交消息／动态")
    return {"url": url, "height": height, "width": width, "imageType": extension, "original": 1, "size": size}
