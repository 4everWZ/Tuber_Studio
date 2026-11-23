"""Read a bounded excerpt from a public HTTPS page for the current turn."""

import asyncio
import ipaddress
import json
import re
import socket
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Annotated
from xml.etree.ElementTree import ParseError

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ...core.text import verified_comment_quote
from .search import search_public_web

FETCH_TIMEOUT = 3.0
MAX_DOWNLOAD = 128 * 1024
MAX_EXCERPT = 720
MAX_URL = 2048
_URL_PART = (MAX_URL - len("https://")) // 2
HTTPS_URL_PATTERN = (
    rf'^https://[^"\\ \t\r\n]{{1,{_URL_PART}}}[^"\\ \t\r\n]{{0,{_URL_PART}}}$'
)
REDIRECTS = {301, 302, 303, 307, 308}
HIDDEN_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer"}
GITHUB_REPOSITORY = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")
TECHNICAL_TERM = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


def technical_terms(text):
    return set(TECHNICAL_TERM.findall(text.casefold()))
WEB_FETCH_INSTRUCTION = (
    '按当前材料回应。需要补查网页时，用 {"web_fetch":"完整HTTPS链接"} 读取摘录。'
)


class WebFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web_fetch: str = Field(min_length=1, max_length=MAX_URL, pattern=HTTPS_URL_PATTERN)


class CommentWebFetchRequest(WebFetchRequest):
    source_quote: str = Field(min_length=2, max_length=240)


class WebSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    web_search: list[Annotated[str, Field(min_length=2, max_length=160)]] = Field(min_length=1, max_length=2)
    source_quote: str = Field(default="", max_length=240)


WEB_SEARCH_SCHEMA = WebSearchRequest.model_json_schema()


WEB_FETCH_SCHEMA = WebFetchRequest.model_json_schema()
# Put the existing URL resource limit in the native pattern itself. An
# unbounded pattern lets a small model enumerate links until the reply budget
# is exhausted, before the post-generation length validator can run.
# Split the bound: the deployed native engine rejects single finite repeats
# above 2000 and can otherwise fall back to unconstrained generation.
WEB_FETCH_SCHEMA["properties"]["web_fetch"] = {"type": "string", "pattern": HTTPS_URL_PATTERN}
COMMENT_WEB_FETCH_SCHEMA = CommentWebFetchRequest.model_json_schema()
COMMENT_WEB_FETCH_SCHEMA["properties"]["web_fetch"] = WEB_FETCH_SCHEMA["properties"]["web_fetch"]


def operations_fetch_schema(schema):
    # Decode a bounded string. The nested finite-repeat URL regexp crashes
    # XGrammar when composed with comment/search alternatives. Validate the
    # full HTTPS pattern with WebFetchRequest before any outbound read.
    field = {"type": "string", "minLength": 1, "maxLength": MAX_URL,
             "description": "完整公开 HTTPS 链接"}
    return schema | {"properties": schema["properties"] | {"web_fetch": field}}


def source_url(value):
    url = httpx.URL(value)
    return url.copy_with(raw_path=url.raw_path, fragment=None)


class OperationWebFetch:
    """Offer one model-selected page before generating the task result."""

    def __init__(
        self,
        guard,
        *,
        context=None,
        record=None,
        allow_url=None,
        search_source=None,
        enable_web_search=False,
        source_links=(),
    ):
        self.guard = guard
        self.context = context
        self.record = record
        self.allow_url = allow_url
        self.search_source = search_source
        self.enable_web_search = bool(enable_web_search or search_source)
        self.source_links = source_links

    async def complete(self, messages, schema, invoke):
        if self.context is None:
            instruction = WEB_FETCH_INSTRUCTION
            tools = [operations_fetch_schema(WEB_FETCH_SCHEMA)]
            if self.enable_web_search:
                fetch_schema = operations_fetch_schema(
                    COMMENT_WEB_FETCH_SCHEMA if self.search_source else WEB_FETCH_SCHEMA
                )
                tools = [fetch_schema, WEB_SEARCH_SCHEMA]
                instruction += (
                    ' 遇到不熟悉的术语、热点新闻、人物事件、具体工具能力或需外部事实时，先查证。'
                    ' 用 {"web_search":["检索词"]} 搜索公开来源，关键词简洁准确，可拆两条短查询。'
                    + (
                        ' 已知具体网址才用web_fetch并填source_quote。'
                        ' 楼里其他人的话用来理解来龙去脉；这一条只是分享近况时，直接接话。'
                        if self.search_source
                        else ' 已知具体HTTPS网址也可用web_fetch读取。'
                    )
                )
            choices = schema.get("oneOf")
            if choices:
                wire_schema = schema | {"oneOf": [*choices, *tools]}
            else:
                # A local JSON Pointer resolves from the document root, even
                # when the reply schema is placed inside a choice branch.
                reply = {key: value for key, value in schema.items() if key != "$defs"}
                wire_schema = {"type": "object", "oneOf": [reply, *tools]}
                if "$defs" in schema:
                    wire_schema["$defs"] = schema["$defs"]
            choice = await invoke(
                [*messages, {"role": "system", "content": instruction}],
                wire_schema,
            )
            if "web_fetch" not in choice and "web_search" not in choice:
                return choice
            self.guard()
            started = time.monotonic()
            if "web_search" in choice:
                request = WebSearchRequest.model_validate(choice)
                receipt, content = await self._search(request)
            else:
                if self.search_source:
                    request = CommentWebFetchRequest.model_validate(choice)
                    request.source_quote = self._check_quote(request.source_quote)
                else:
                    request = WebFetchRequest.model_validate(choice)
                result = await self._read(request.web_fetch)
                receipt = {k: v for k, v in result.items() if k != "excerpt"}
                if result["status"] == "ok":
                    origin = "" if result["url"] == str(source_url(request.web_fetch)) else f"\n来源 {result['url']}"
                    content = f"网页摘录：{result['excerpt']}{origin}"
                else:
                    content = "本轮未读到网页正文。结合现有材料回应当前问题。"
            receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            selection = request.model_dump_json().replace("<", "\\u003c").replace(">", "\\u003e")
            self.context = [
                {"role": "assistant", "content": selection},
                {"role": "user", "content": content.replace("<", "\\u003c").replace(">", "\\u003e")},
            ]
            if self.record is not None:
                self.record(receipt, self.context)
            self.guard()
        return await invoke([*messages, *self.context], schema)

    def _check_quote(self, quote):
        matched = verified_comment_quote(self.search_source or "", quote)
        if not matched:
            raise ValueError("网页取材须定位当前评论原话")
        return matched

    async def _search(self, request):
        async def discover(query):
            try:
                return await search_public_web(query)
            except (TimeoutError, httpx.HTTPError, ValueError, ParseError):
                return []

        async def discover_and_read():
            hits = await asyncio.gather(*(discover(query) for query in dict.fromkeys(request.web_search)))
            urls = list(dict.fromkeys(url for group in hits for url in group if url != linked))
            return await asyncio.gather(*(self._read(url) for url in urls))

        # A project link supplied on the source itself is a better lead than a
        # ranking for an ambiguous acronym. Only use it when the quote names it.
        terms = technical_terms(" ".join(request.web_search) + " " + (self.search_source or request.source_quote or ""))
        linked = next((url for url in self.source_links if terms & technical_terms(url)), None)
        if linked:
            direct, found = await asyncio.gather(self._read(linked), discover_and_read())
            if direct["status"] == "ok":
                # Once the named project has its own source, add search hits
                # for other named subjects rather than competing acronym matches.
                other_terms = terms - technical_terms(linked)
                found = [page for page in found if page["status"] == "ok"
                         and other_terms & technical_terms(page["url"] + " " + page["title"])]
            pages = [direct, *found]
        else:
            pages = await discover_and_read()
        sources = [page for page in pages if page["status"] == "ok" and page["excerpt"]]
        receipt = {"tool": "web_search", "status": "ok" if sources else "unavailable",
                   "sources": [{k: page[k] for k in ("url", "title", "truncated")} for page in sources]}
        content = (
            "公开网页取材。只把确实读到且能回答原问题的部分作为依据。"
            "能确认一部分时先回答这一部分，再说明还有哪一点未查到。"
        )
        for page in sources:
            content += f"\n\n来源 {page['url']}\n{page['title']}\n{page['excerpt']}"
        if not sources:
            content += " 本次没有读到可用来源。可以说明查不到、询问缺少的信息或略过。"
        return receipt, content

    async def _read(self, value):
        try:
            options = {"allow_url": self.allow_url} if self.allow_url is not None else {}
            return {"status": "ok", **await fetch_excerpt(value, **options)}
        except TimeoutError:
            return {"status": "timeout"}
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, LookupError):
            return {"status": "unavailable"}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = []
        self.title = []
        self.body = []
        self.in_title = False
        self.publication_dates = []
        self.article_data = None

    @property
    def published_at(self):
        # Use publication metadata, excluding modification timestamps and navigation dates.
        dates = set()
        for raw in self.publication_dates:
            try:
                dates.add(datetime.fromisoformat(raw.strip()).isoformat())
            except (ValueError, AttributeError):
                continue
        return next(iter(dates)) if len(dates) == 1 else None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and any((attributes.get(key) or "").lower() in {
            "article:published_time", "datepublished", "parsely-pub-date"
        } for key in ("property", "itemprop", "name")):
            self.publication_dates.append(attributes.get("content"))
        if tag == "time" and (attributes.get("itemprop") or "").lower() == "datepublished":
            self.publication_dates.append(attributes.get("datetime"))
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self.article_data = []
        if tag in HIDDEN_TAGS:
            self.hidden.append(tag)
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self.article_data is not None:
            try:
                value = json.loads("".join(self.article_data))
                entries = value.get("@graph", [value]) if isinstance(value, dict) else value
                for entry in entries if isinstance(entries, list) else ():
                    if isinstance(entry, dict) and any(
                        name in {"Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting"}
                        for name in ([entry.get("@type")] if isinstance(entry.get("@type"), str) else
                                     entry.get("@type") or [])
                        if isinstance(name, str)
                    ):
                        self.publication_dates.append(entry.get("datePublished"))
            except (ValueError, RecursionError, TypeError):
                pass
            self.article_data = None
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.article_data is not None:
            self.article_data.append(data)
        if not self.hidden and data.strip():
            (self.title if self.in_title else self.body).append(data.strip())


class CCTVProgramPage(PageText):
    """The programme page fills its visible credits from these literal fields."""

    fields = {"video_album_title": "节目名", "play_director": "导演"}

    def __init__(self):
        super().__init__()
        self.credits = {}

    def handle_data(self, data):
        if self.hidden == ["script"]:
            for key, value in re.findall(
                r"(?m)^\s*var[ \t]+(video_album_title|play_director)[ \t]*=[ \t]*'([^'\\\r\n<>]{1,160})'[ \t]*;",
                data,
            ):
                if value.strip():
                    self.credits[key] = value.strip()
                    self.body.append(f"{self.fields[key]}：{value.strip()}")
        super().handle_data(data)


def is_cctv_archive(url):
    return (url.scheme == "https" and url.host == "tv.cctv.com" and not url.userinfo and url.port is None
            and bool(re.fullmatch(r"/\d{4}/\d{2}/\d{2}/VIDA[A-Za-z0-9]+\.shtml", url.path)))


def archive_title_fields(title):
    match = re.fullmatch(r"([^；;<>]{1,120}?)_CCTV节目官网-([^_；;<>]{1,24})_央视网\(cctv\.com\)", title)
    return {"节目名": match[1], "节目类型": match[2]} if match else {}


def programme_credit_text(receipt):
    if receipt.get("programme_credits"):
        return receipt["programme_credits"]
    # Earlier read receipts retained the catalogue title even without parsed credits.
    if receipt.get("excerpt") and not receipt.get("error") and is_cctv_archive(httpx.URL(receipt.get("url") or "")):
        fields = archive_title_fields(receipt.get("title") or "")
        return "；".join(f"{k}：{v}" for k, v in fields.items())
    return ""


class CCTVArchivePage(PageText):
    """Read the dated album template's heading and labelled credits together."""

    def __init__(self):
        super().__init__()
        self.detail_depth = 0
        self.field_tag = None
        self.parts = []
        self.heading = ""
        self.directors = {}

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "div":
            if self.detail_depth:
                self.detail_depth += 1
            elif "text_mod" in (dict(attrs).get("class") or "").split():
                self.detail_depth = 1
                self.heading = ""
        if self.detail_depth and tag in ("h3", "p"):
            self.field_tag, self.parts = tag, []

    def handle_data(self, data):
        if self.field_tag and not self.hidden:
            self.parts.append(data)
        super().handle_data(data)

    def handle_endtag(self, tag):
        if self.detail_depth and tag == self.field_tag:
            text = "".join(self.parts).strip()
            if tag == "h3":
                self.heading = text
            elif self.heading and (match := re.fullmatch(r"导演\s*[：:]\s*([^；;<>\n]{1,100})", text)):
                self.directors[self.heading] = match[1].strip()
            self.field_tag = None
        if tag == "div" and self.detail_depth:
            self.detail_depth -= 1
        super().handle_endtag(tag)

    def credits(self, title):
        fields = archive_title_fields(title)
        if not fields:
            return ""
        if director := self.directors.get(fields["节目名"]):
            fields["导演"] = director
        return "；".join(f"{k}：{v}" for k, v in fields.items())


def public_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    mapped = getattr(address, "ipv4_mapped", None) or getattr(address, "sixtofour", None)
    return mapped is None or public_ip(str(mapped))


async def public_https_address(url):
    host = url.raw_host.decode("ascii").rstrip(".")
    if (
        url.scheme != "https"
        or len(str(url)) > MAX_URL
        or not host
        or url.userinfo
        or url.port is not None
        or host.endswith((".localhost", ".local", ".internal", ".ts.net"))
        or ("." not in host and ":" not in host)
    ):
        raise ValueError("仅可读取公开 HTTPS 网页")
    rows = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not rows or not all(public_ip(row[4][0]) for row in rows):
        raise ValueError("网页解析到了非公网地址")
    # Prefer IPv4 on nodes with intermittent or absent IPv6 routes.
    address = min(rows, key=lambda row: row[0] != socket.AF_INET)[4][0]
    return host, address


async def fetch_excerpt(value, *, allow_url=None, exact_source=False):
    if not 1 <= len(value) <= MAX_URL or any(c.isspace() or c == "\\" for c in value):
        raise ValueError("网页地址格式无效")
    url = source_url(value)
    try_readme = not exact_source
    async with (
        asyncio.timeout(FETCH_TIMEOUT),
        httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client,
    ):
        for _ in range(3):
            if allow_url is not None and not allow_url(url):
                raise ValueError("网页已由当前任务的内容来源管理")
            resource, accept = url, "text/html, text/plain, application/xhtml+xml, application/json"
            if (try_readme and url.host == "github.com" and url.scheme == "https"
                    and not url.userinfo and url.port is None
                    and (repo := GITHUB_REPOSITORY.fullmatch(url.path))):
                owner, name = repo.groups()
                resource = httpx.URL(f"https://api.github.com/repos/{owner}/{name.removesuffix('.git')}/readme")
                accept = "application/vnd.github.raw+json"
            host, address = await public_https_address(resource)
            # Preserve the original Host and TLS verification while connecting only to the checked IP.
            target = resource.copy_with(host=address)
            client.cookies.clear()
            async with client.stream(
                "GET",
                target,
                headers={
                    "Host": f"[{host}]" if ":" in host else host,
                    "Accept": accept,
                    "Accept-Encoding": "gzip, deflate, identity",
                    "User-Agent": "TuberStudio-WebFetch/1.0",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if resource != url and response.status_code in {403, 404, 429}:
                    try_readme = False
                    continue
                if response.status_code in REDIRECTS and (location := response.headers.get("location")):
                    if exact_source:
                        raise ValueError("跟进授权不包含重定向目标")
                    url = source_url(resource.join(location))
                    continue
                response.raise_for_status()
                return await read_excerpt(url, response)
        raise ValueError("网页重定向过多")


async def read_excerpt(url, response):
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not (media.startswith("text/") or media in {"application/xhtml+xml", "application/json", "application/vnd.github.raw+json"}):
        raise ValueError("网页没有返回可读文本")
    encodings = response.headers.get("content-encoding", "identity").lower().split(",")
    if any(encoding.strip() not in {"identity", "gzip", "deflate"} for encoding in encodings):
        raise ValueError("网页返回了不支持的编码")
    raw = bytearray()
    truncated = False
    # HTTPX decodes the advertised standard encodings while streaming; the
    # existing limit applies to decoded text, including compressed responses.
    async for chunk in response.aiter_bytes(chunk_size=8192):
        remaining = MAX_DOWNLOAD - len(raw)
        raw.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    content = raw.decode(response.encoding or "utf-8", errors="replace")
    title = ""
    metadata = {}
    if "html" in media:
        if url.host == "tv.cctv.com" and url.path.startswith("/v/"):
            page = CCTVProgramPage()
        elif is_cctv_archive(url):
            page = CCTVArchivePage()
        else:
            page = PageText()
        page.feed(content)
        page.close()
        title, content = " ".join(page.title), " ".join(page.body)
        if page.published_at:
            metadata["published_at"] = page.published_at
        if isinstance(page, CCTVProgramPage) and page.credits:
            metadata["programme_credits"] = "；".join(
                f"{label}：{page.credits[key]}" for key, label in page.fields.items() if key in page.credits)
        if isinstance(page, CCTVArchivePage) and (credits := page.credits(title)):
            metadata["programme_credits"] = credits
    content = " ".join(content.split())
    if not content:
        raise ValueError("网页没有返回可读文本")
    return {
        **metadata,
        "url": str(url),
        "title": title[:120],
        "excerpt": content[:MAX_EXCERPT],
        "truncated": truncated or len(content) > MAX_EXCERPT,
    }
