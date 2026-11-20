"""Bounded public search discovery. Answers use opened pages, not snippets."""

import asyncio
import re
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

import httpx

SEARCH_URL = "https://www.bing.com/search"
NEWS_URL = "https://www.bing.com/news/search"
SEARCH_HOSTS = {"www.bing.com", "cn.bing.com", "global.bing.com"}
NEWS_FEEDS = ("https://feeds.feedburner.com/rsscna/local", "https://feeds.feedburner.com/rsscna/intworld")
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


async def _rss(url, allowed_hosts):
    url = httpx.URL(url)
    headers = {"Accept": "application/rss+xml", "User-Agent": "Mozilla/5.0"}
    async with asyncio.timeout(SEARCH_TIMEOUT), httpx.AsyncClient(trust_env=False) as client:
        for _ in range(3):
            async with client.stream("GET", url, headers=headers) as response:
                if response.has_redirect_location:
                    url = url.join(response.headers["location"])
                    if url.scheme != "https" or url.host not in allowed_hosts or url.port is not None or url.userinfo:
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
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise ValueError("搜索服务未返回可读取的 RSS")
    try:
        return ElementTree.fromstring(body).findall("./channel/item")
    except ElementTree.ParseError as error:
        raise ValueError("搜索服务未返回可读取的 RSS") from error


async def search_public_web(query, *, news=False):
    if news and not query:
        batches = await asyncio.gather(*(_rss(feed, {"feeds.feedburner.com"}) for feed in NEWS_FEEDS),
                                       return_exceptions=True)
        items = [item for batch in batches if isinstance(batch, list) for item in batch[:4]]
        if not items and all(isinstance(batch, Exception) for batch in batches):
            raise ValueError("新闻来源暂时不可读取")
    else:
        params = {"q": query, "format": "rss"}
        if news and re.search(r"[\u3400-\u9fff]", query):
            params |= {"mkt": "zh-TW", "setlang": "zh-TW"}
        items = await _rss(httpx.URL(NEWS_URL if news else SEARCH_URL, params=params), SEARCH_HOSTS)

    def feed_time(item):
        try:
            return parsedate_to_datetime(item.findtext("pubDate", "")).timestamp()
        except (ValueError, OverflowError):
            return 0

    if news:
        items.sort(key=feed_time, reverse=True)
    hits = []
    for item in items:
        url = item.findtext("link", "").strip()
        if news:
            link = httpx.URL(url)
            if link.host in SEARCH_HOSTS and link.path == "/news/apiclick.aspx":
                url = link.params.get("url", "")
        if url.startswith("https://") and url not in hits:
            hits.append(url)
        if len(hits) == (5 if news else 2):
            break
    return hits
