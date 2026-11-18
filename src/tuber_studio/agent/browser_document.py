"""Fetch only an explicitly authorized public document, without browser credentials."""

import asyncio
import html
import ipaddress
from urllib.parse import urldefrag

import httpx

from ..web_fetch import MAX_URL, public_https_address, public_ip

MAX_DOCUMENT = 256 * 1024
MAX_SOURCES = 6


def source_list(values):
    if not 1 <= len(values) <= MAX_SOURCES:
        raise ValueError("请指定 1 到 6 个公开 HTTPS 文档地址")
    result = []
    for value in values:
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL or any(c.isspace() or c in "\\\x00" for c in value):
            raise ValueError("文档地址格式无效")
        try:
            url = httpx.URL(urldefrag(value).url)
        except (ValueError, httpx.InvalidURL):
            raise ValueError("文档地址格式无效") from None
        if (url.scheme != "https" or not url.host or url.userinfo or url.port is not None
                or url.host.endswith((".local", ".localhost", ".internal", ".ts.net"))
                or "." not in url.host and ":" not in url.host):
            raise ValueError("仅可授权公开 HTTPS 文档")
        try:
            address = ipaddress.ip_address(url.host)
        except ValueError:
            address = None
        if address is not None and not public_ip(str(address)):
            raise ValueError("仅可授权公网地址")
        result.append(str(url))
    return list(dict.fromkeys(result))


async def read_document(value, *, guard=None):
    url = httpx.URL(value)
    async with asyncio.timeout(7):
        host, address = await public_https_address(url)
        if guard:
            guard()
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", url.copy_with(host=address), extensions={"sni_hostname": host},
                                     headers={"Host": f"[{host}]" if ":" in host else host,
                                              "Accept": "text/html,text/plain", "Accept-Encoding": "identity"}) as response:
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if (response.status_code != 200 or media not in {"text/html", "text/plain"}
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise ValueError("来源须直接返回未压缩的 HTML 或纯文本文档")
                content = bytearray()
                async for chunk in response.aiter_raw(32768):
                    if guard:
                        guard()
                    if len(content) + len(chunk) > MAX_DOCUMENT:
                        raise ValueError("来源文档超过 256 KiB")
                    content.extend(chunk)
                try:
                    body = content.decode(response.encoding or "utf-8", errors="replace")
                except LookupError:
                    raise ValueError("来源使用了无法识别的字符编码") from None
                return "<pre>" + html.escape(body) + "</pre>" if media == "text/plain" else body
