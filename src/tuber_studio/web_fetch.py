"""Read a bounded excerpt from a public HTTPS page for the current turn."""

import asyncio
import ipaddress
import socket
import time
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .operation_text import source_alias_literals

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
WEB_FETCH_INSTRUCTION = (
    '先决定怎样处理当前交流。如果要看网页原文再答，第一步只返回 {"web_fetch":"该页完整HTTPS链接"}，'
    "然后会收到真实网页摘录。已有材料足够就直接用原回复格式。"
)


class WebFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web_fetch: str = Field(min_length=1, max_length=MAX_URL, pattern=HTTPS_URL_PATTERN)


WEB_FETCH_SCHEMA = WebFetchRequest.model_json_schema()
# Put the existing URL resource limit in the native pattern itself. An
# unbounded pattern lets a small model enumerate links until the reply budget
# is exhausted, before the post-generation length validator can run.
# Split the bound: the deployed native engine rejects single finite repeats
# above 2000 and can otherwise fall back to unconstrained generation.
WEB_FETCH_SCHEMA["properties"]["web_fetch"] = {"type": "string", "pattern": HTTPS_URL_PATTERN}


def source_url(value):
    url = httpx.URL(value)
    return url.copy_with(raw_path=url.raw_path, fragment=None)


class OperationWebFetch:
    """One model-selected page, reused if this generation needs revision."""

    def __init__(self, guard):
        self.guard = guard
        self.context = None
        self.receipt = None

    async def complete(self, messages, schema, invoke):
        if self.context is None:
            instruction = WEB_FETCH_INSTRUCTION
            choices = schema.get("oneOf")
            wire_schema = (
                schema | {"oneOf": [*choices, WEB_FETCH_SCHEMA]}
                if choices
                else {"type": "object", "oneOf": [schema, WEB_FETCH_SCHEMA]}
            )
            choice = await invoke(
                [*messages, {"role": "system", "content": instruction}],
                wire_schema,
            )
            if "web_fetch" not in choice:
                return choice
            request = WebFetchRequest.model_validate(choice)
            self.guard()
            started = time.monotonic()
            result = await self._read(request.web_fetch)
            self.receipt = {k: v for k, v in result.items() if k != "excerpt"} | {
                "elapsed_ms": round((time.monotonic() - started) * 1000)
            }
            if result["status"] == "ok":
                self.receipt["source_alias_literals"] = sorted(set().union(*(
                    source_alias_literals(result.get(key) or "") for key in ("url", "title", "excerpt")
                )))
            self.guard()
            if result["status"] == "ok":
                excerpt = result["excerpt"].replace("<", "\\u003c").replace(">", "\\u003e")
                origin = (
                    "" if result["url"] == str(source_url(request.web_fetch)) else f"\n来源 {result['url']}"
                )
                content = f"网页已读取。摘录：{excerpt}{origin}\n直接把摘录里与问题相关的内容告诉对方。"
            else:
                content = "本轮未读到网页正文。结合现有材料回应当前问题。"
            selection = request.model_dump_json().replace("<", "\\u003c").replace(">", "\\u003e")
            self.context = [
                {"role": "assistant", "content": selection},
                {"role": "user", "content": content},
            ]
        return await invoke([*messages, *self.context], schema)

    async def _read(self, value):
        try:
            return {"status": "ok", **await fetch_excerpt(value)}
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

    def handle_starttag(self, tag, attrs):
        if tag in HIDDEN_TAGS:
            self.hidden.append(tag)
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden and data.strip():
            (self.title if self.in_title else self.body).append(data.strip())


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


async def fetch_excerpt(value):
    if not 1 <= len(value) <= MAX_URL or any(c.isspace() or c == "\\" for c in value):
        raise ValueError("网页地址格式无效")
    url = source_url(value)
    async with (
        asyncio.timeout(FETCH_TIMEOUT),
        httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client,
    ):
        for _ in range(3):
            host, address = await public_https_address(url)
            # Preserve the original Host and TLS verification while connecting only to the checked IP.
            target = url.copy_with(host=address)
            client.cookies.clear()
            async with client.stream(
                "GET",
                target,
                headers={
                    "Host": f"[{host}]" if ":" in host else host,
                    "Accept": "text/html, text/plain, application/xhtml+xml, application/json",
                    "Accept-Encoding": "gzip, deflate, identity",
                    "User-Agent": "TuberStudio-WebFetch/1.0",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in REDIRECTS and (location := response.headers.get("location")):
                    url = source_url(url.join(location))
                    continue
                response.raise_for_status()
                return await read_excerpt(url, response)
        raise ValueError("网页重定向过多")


async def read_excerpt(url, response):
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not (media.startswith("text/") or media in {"application/xhtml+xml", "application/json"}):
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
    if "html" in media:
        page = PageText()
        page.feed(content)
        page.close()
        title, content = " ".join(page.title), " ".join(page.body)
    content = " ".join(content.split())
    if not content:
        raise ValueError("网页没有返回可读文本")
    return {
        "url": str(url),
        "title": title[:120],
        "excerpt": content[:MAX_EXCERPT],
        "truncated": truncated or len(content) > MAX_EXCERPT,
    }
