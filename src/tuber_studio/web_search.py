"""Bounded public search discovery. Answers use opened pages, not snippets."""

import asyncio
from html.parser import HTMLParser
from xml.etree import ElementTree

import httpx

SEARCH_URL = "https://www.bing.com/search"
SEARCH_HOSTS = {"www.bing.com", "cn.bing.com", "global.bing.com"}
SEARCH_TIMEOUT = 4.0
MAX_SEARCH_BYTES = 128 * 1024


class _ProgrammeHits(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag != "a" or attributes.get("id") != "tit_flag":
            return
        try:
            url = httpx.URL(attributes.get("href") or "")
        except httpx.InvalidURL:
            return
        if url.scheme in {"http", "https"} and url.host == "tv.cctv.com" and not url.userinfo and url.port is None:
            address = str(url.copy_with(scheme="https"))
            if address not in self.links:
                self.links.append(address)


async def search_broadcast_programmes(query):
    """Discover official programme pages by title. Only the subsequently opened page is citable."""
    parser = _ProgrammeHits()
    async with asyncio.timeout(SEARCH_TIMEOUT), httpx.AsyncClient(trust_env=False) as client:
        async with client.stream("GET", "https://search.cctv.com/search.php", params={"type": "web", "qtext": query},
                                 headers={"User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if len(body) + len(chunk) > MAX_SEARCH_BYTES:
                    raise ValueError("节目目录结果超出读取上限")
                body.extend(chunk)
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser.links


async def search_public_web(query):
    url = httpx.URL(SEARCH_URL, params={"q": query, "format": "rss"})
    headers = {"Accept": "application/rss+xml", "User-Agent": "Mozilla/5.0"}
    async with asyncio.timeout(SEARCH_TIMEOUT), httpx.AsyncClient(trust_env=False) as client:
        for _ in range(3):
            async with client.stream("GET", url, headers=headers) as response:
                if response.has_redirect_location:
                    url = url.join(response.headers["location"])
                    if url.scheme != "https" or url.host not in SEARCH_HOSTS or url.port is not None or url.userinfo:
                        raise ValueError("搜索服务跳转到非预期地址")
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(body) + len(chunk) > MAX_SEARCH_BYTES:
                        raise ValueError("搜索结果超出读取上限")
                    body.extend(chunk)
                break
        else:
            raise ValueError("搜索服务重定向过多")
    if b"<!" in body:
        raise ValueError("搜索服务未返回可读取的 RSS")
    root = ElementTree.fromstring(body)
    hits = []
    for item in root.findall("./channel/item"):
        url = item.findtext("link", "").strip()
        if url.startswith("https://") and url not in hits:
            hits.append(url)
        if len(hits) == 2:
            break
    return hits
