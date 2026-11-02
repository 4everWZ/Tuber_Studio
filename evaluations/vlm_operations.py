"""Read-only VLM operations comparisons, with actual bounded model/tool exchanges.

Example (same flags/cases for each candidate):
  uv run --locked python evaluations/vlm_operations.py --base-url http://127.0.0.1:18080 \
    --model candidate --quantization Q4_K_M --context-tokens 131072 --thinking on \
    --tools-mode native --output evaluations/results/candidate.json

Optional capacity admission is separate from ordinary cases and recall:
  --context-probe-tokens 120000 --context-probe-output-tokens 8192
Optional paired formatting diagnostic (never changes baseline score/history):
  --format-repair
Explicit caller-protocol condition (old discard/text-first defaults remain):
  --tool-reasoning-history ephemeral --image-order image_first
Tool reasoning is replayed in memory within the current real user/tool loop.
It is absent from persisted histories/reports and cleared across real user turns.
Optional client image condition: --image-preprocessing gemma4_image_280
  Run with: uv run --locked --with pillow==13.0.0 python evaluations/vlm_operations.py ...
No application configuration, model residency or platform write endpoint is used.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import socket
import struct
import time
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from tuber_studio.bilibili_browsing import BrowsingAdapter

if __package__:
    from . import vlm_images
else:
    _image_spec = importlib.util.spec_from_file_location(
        "vlm_images", Path(__file__).with_name("vlm_images.py")
    )
    vlm_images = importlib.util.module_from_spec(_image_spec)
    _image_spec.loader.exec_module(vlm_images)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evaluations/cases/vlm-operations.json"
PROTOCOL_VERSION = "vlm-operations-v4-oc-paths"
SECRET_KEYS = re.compile(r"api.?key|password|secret|authorization|cookie|csrf|sessdata|access.?token", re.I)
TOOL_NAMES = {"web_search", "web_fetch", "bilibili_read"}
FINAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "channel", "citations", "evidence_refs"],
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "channel": {"type": "string", "enum": ["public", "private"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "quote"],
                "properties": {"url": {"type": "string"}, "quote": {"type": "string"}},
            },
        },
        "evidence_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索公开网页，返回来源URL、摘要及证据ID。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "读取公开http(s)网页正文，返回正文、来源URL及证据ID。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url"],
                "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 2048}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bilibili_read",
            "description": "只读固定B站接口，读取指定BV的真实元信息及至多两个分P的CC；无字幕如实报告。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bvid"],
                "properties": {"bvid": {"type": "string", "pattern": "^BV1[0-9A-Za-z]{9}$"}},
            },
        },
    },
]


class EvaluationError(ValueError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


class Redactor:
    def __init__(self, values=()):
        self.secrets = tuple(value for value in values if value)

    def __call__(self, value):
        if isinstance(value, dict):
            return {
                key: "[redacted]" if SECRET_KEYS.search(key) else self(item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
        return value


def strip_thought_prefix(text):
    removed = 0
    pairs = (
        ("<think>", "</think>"),
        ("<analysis>", "</analysis>"),
        ("<|analysis_start|>", "<|analysis_end|>"),
    )
    while True:
        trimmed = text.lstrip()
        match = next(((start, end) for start, end in pairs if trimmed.startswith(start)), None)
        if match is None:
            return text, removed
        text = trimmed
        start, end = match
        position = text.find(end, len(start))
        if position < 0:
            raise EvaluationError(
                "incomplete_reasoning_markup", "Unclosed thought prefix; no draft tool calls are executable"
            )
        removed += position - len(start)
        text = text[position + len(end) :]


def error_record(error):
    if isinstance(error, EvaluationError):
        return {"kind": error.kind, "message": str(error)}
    if isinstance(error, ValueError) and isinstance(getattr(error, "kind", None), str):
        return {"kind": error.kind, "message": str(error)}
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return {"kind": "timeout", "message": "The bounded request or case deadline expired"}
    if isinstance(error, httpx.HTTPStatusError):
        return {"kind": "http_status", "status_code": error.response.status_code}
    # Never stringify a remote exception containing request headers/body/secrets.
    return {"kind": type(error).__name__, "message": "Request or response processing failed"}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.in_title = False
        self.parts, self.title = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip())
            if self.in_title:
                self.title.append(data.strip())


class _SNIStream(httpcore.AsyncNetworkStream):
    """Preserve the original TLS name when CONNECT targets a validated pinned IP.

    httpcore's HTTPS proxy tunnel does not consume request.sni_hostname, whereas
    direct HTTP does. Wrapping its network stream covers both without disabling
    certificate checks or allowing the proxy to resolve an unpinned target.
    """

    def __init__(self, stream, address, hostname):
        self.stream, self.address, self.hostname = stream, address, hostname

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        return await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        name = self.hostname if server_hostname == self.address else server_hostname
        stream = await self.stream.start_tls(ssl_context, name, timeout)
        return _SNIStream(stream, self.address, self.hostname)


class _SNIBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend, address, hostname):
        self.backend, self.address, self.hostname = backend, address, hostname

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self.backend.connect_tcp(host, port, timeout, local_address, socket_options)
        return _SNIStream(stream, self.address, self.hostname)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise EvaluationError("unix_socket_denied", "Web tools cannot access Unix sockets")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


async def public_addresses(host, port):
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise EvaluationError("dns_failed", "Public hostname resolution failed") from None
    return list(dict.fromkeys(row[4][0] for row in rows))


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    mapped = getattr(address, "ipv4_mapped", None) or getattr(address, "sixtofour", None)
    return not mapped or mapped.is_global


class PublicWeb:
    def __init__(self, *, timeout=20, maximum=2 * 1024 * 1024, proxy=None, resolver=None, client=None):
        self.timeout, self.maximum, self.proxy = timeout, maximum, proxy
        self.resolver, self.client = resolver or public_addresses, client

    async def validate(self, url):
        if not isinstance(url, str) or not 1 <= len(url) <= 2048 or re.search(r"[\x00-\x20\\]", url):
            raise EvaluationError("url_denied", "A bounded public HTTP(S) URL is required")
        try:
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            host = (parsed.hostname or "").encode("idna").decode("ascii")
        except (ValueError, UnicodeError):
            raise EvaluationError("url_denied", "URL authority is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise EvaluationError("url_denied", "Only credential-free public HTTP(S) URLs are permitted")
        if host.rstrip(".") == "localhost" or host.rstrip(".").endswith(
            (".localhost", ".local", ".internal")
        ):
            raise EvaluationError("private_address", "Local hostnames cannot be fetched")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses = (
            [str(literal)] if literal else await asyncio.wait_for(self.resolver(host, port), self.timeout)
        )
        if not addresses or not all(public_ip(address) for address in addresses):
            raise EvaluationError("private_address", "Every resolved address must be globally routable")
        # Pin the socket destination as well as validating DNS: a second lookup
        # cannot rebind a model-controlled host to loopback/private infrastructure.
        clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        return clean, host, port, addresses[0]

    @asynccontextmanager
    async def connection(self, address, hostname):
        if self.client is not None:
            yield self.client
            return
        transport = httpx.AsyncHTTPTransport(proxy=self.proxy, retries=0)
        if self.proxy:
            # HTTPX 0.28's pool/backend is inspected by the dedicated adapter
            # tests. No application transport or installed dependency is changed.
            transport._pool._network_backend = _SNIBackend(
                transport._pool._network_backend, address, hostname
            )
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=self.timeout
        ) as client:
            yield client

    async def get(self, url):
        started, redirects = time.perf_counter(), []
        for _ in range(5):
            clean, host, port, address = await self.validate(url)
            pinned = httpx.URL(clean).copy_with(host=address)
            authority = f"[{host}]" if ":" in host else host
            if port != (443 if pinned.scheme == "https" else 80):
                authority += f":{port}"
            async with self.connection(address, host) as client:
                async with client.stream(
                    "GET",
                    pinned,
                    headers={"Host": authority, "User-Agent": "TuberStudio-ReadOnly-Evaluation/1"},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise EvaluationError("invalid_redirect", "Redirect has no destination")
                        redirects.append(clean)
                        url = urljoin(clean, location)
                        continue
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.maximum:
                            raise EvaluationError(
                                "download_limit", "Document exceeds the decoded download limit"
                            )
                    return {
                        "url": clean,
                        "redirects": redirects,
                        "raw": bytes(raw),
                        "content_type": response.headers.get("content-type", "").split(";", 1)[0].lower(),
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                    }
        raise EvaluationError("redirect_limit", "Too many redirects; no destination was assumed safe")

    async def fetch(self, url, *, text_limit=18000):
        document = await self.get(url)
        media_type = document["content_type"]
        if not (
            media_type.startswith("text/")
            or media_type in {"application/json", "application/xml", "application/xhtml+xml"}
        ):
            raise EvaluationError("unsupported_media", "Web tool only reads textual documents")
        text = document.pop("raw").decode("utf-8", errors="replace")
        title = ""
        if "html" in media_type:
            parser = TextExtractor()
            parser.feed(text)
            text, title = "\n".join(parser.parts), " ".join(parser.title)
        if not text.strip():
            raise EvaluationError("no_readable_text", "The actual response contained no readable text")
        return document | {"title": title, "text": text[:text_limit], "truncated": len(text) > text_limit}


class SearchHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.current, self.text = [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", "").split():
            self.current, self.text = attrs.get("href"), []

    def handle_data(self, data):
        if self.current:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            url = urljoin("https://duckduckgo.com", self.current)
            parameters = parse_qs(urlsplit(url).query)
            url = parameters.get("uddg", [url])[0]
            self.items.append({"title": " ".join(self.text).strip(), "url": url, "snippet": ""})
            self.current = None


class Search:
    def __init__(self, web, provider="bing-rss", endpoint=None):
        self.web, self.provider, self.endpoint = web, provider, endpoint

    async def run(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise EvaluationError("invalid_query", "Search query must contain 1–200 characters")
        if self.provider == "searxng":
            if not self.endpoint:
                raise EvaluationError("search_not_configured", "SearXNG provider needs --search-endpoint")
            separator = "&" if urlsplit(self.endpoint).query else "?"
            url = self.endpoint + separator + urlencode({"q": query, "format": "json", "language": "zh-CN"})
        elif self.provider == "duckduckgo":
            url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
        else:
            url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss", "count": 5})
        document = await self.web.get(url)
        raw = document["raw"]
        try:
            if self.provider == "bing-rss":
                items = [
                    {
                        "title": item.findtext("title", ""),
                        "url": item.findtext("link", ""),
                        "snippet": item.findtext("description", ""),
                    }
                    for item in ET.fromstring(raw).findall("./channel/item")
                ]
            elif self.provider == "searxng":
                payload = json.loads(raw)
                if not isinstance(payload.get("results"), list):
                    raise ValueError
                items = [
                    {"title": item.get("title", ""), "url": item["url"], "snippet": item.get("content", "")}
                    for item in payload["results"]
                ]
            else:
                parser = SearchHTML()
                parser.feed(raw.decode("utf-8", errors="replace"))
                items = parser.items
        except (ValueError, ET.ParseError, KeyError, TypeError):
            raise EvaluationError(
                "search_unreadable", "Actual search response was not the selected provider's result format"
            ) from None
        if not items:
            raise EvaluationError(
                "search_no_results",
                "Actual search returned no readable results, possibly a challenge or restriction",
            )
        selected = []
        for item in items[:5]:
            try:
                clean, *_ = await self.web.validate(item["url"])
            except EvaluationError:
                continue
            selected.append(
                {"url": clean, "title": str(item["title"])[:300], "snippet": str(item["snippet"])[:1500]}
            )
        if not selected:
            raise EvaluationError("search_no_public_results", "No result had a validated public URL")
        return {
            "provider": self.provider,
            "query": query,
            "results": selected,
            "request_url": document["url"],
            "response_sha256": document["sha256"],
        }


class ReadTools:
    def __init__(self, web, search, *, bilibili=None, redactor=None):
        self.web, self.search, self.bilibili = web, search, bilibili
        self.redact = redactor or Redactor()
        self.receipts = []

    async def execute(self, name, arguments, *, private_data=False):
        reference = f"tool:{len(self.receipts) + 1}"
        started = time.perf_counter()
        receipt = {"ref": reference, "name": name, "arguments": self.redact(arguments)}
        try:
            if name not in TOOL_NAMES:
                raise EvaluationError(
                    "tool_not_allowed", "The requested tool is not in the read-only whitelist"
                )
            if private_data:
                raise EvaluationError(
                    "private_context_network_denied", "Private case material cannot be sent to external tools"
                )
            if not isinstance(arguments, dict):
                raise EvaluationError("invalid_arguments", "Tool arguments must be an object")
            keys = {"web_search": {"query"}, "web_fetch": {"url"}, "bilibili_read": {"bvid"}}[name]
            if set(arguments) != keys:
                raise EvaluationError("invalid_arguments", "Missing or unexpected tool arguments")
            if name == "web_search":
                value = await self.search.run(arguments["query"])
            elif name == "web_fetch":
                value = await self.web.fetch(arguments["url"])
            else:
                bvid = arguments["bvid"]
                if not isinstance(bvid, str) or not re.fullmatch(r"BV1[0-9A-Za-z]{9}", bvid):
                    raise EvaluationError("invalid_bvid", "Only validated BV identifiers are permitted")
                if self.bilibili is None:
                    raise EvaluationError(
                        "bilibili_not_configured", "Read-only Bilibili adapter is unavailable"
                    )
                metadata = await self.bilibili.video_metadata(bvid)
                parts, errors = [], []
                for part in metadata["parts"][:2]:
                    try:
                        subtitle = await self.bilibili.subtitles(
                            bvid, part["cid"], ["zh-CN", "zh-Hans", "zh"]
                        )
                        parts.append(subtitle)
                    except ValueError as error:
                        errors.append({"cid": part["cid"], "error": getattr(error, "kind", "read_failed")})
                value = {
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "metadata": metadata,
                    "subtitles": parts,
                    "subtitle_errors": errors,
                    "coverage": "at_most_two_parts",
                }
            receipt.update(ok=True, result=self.redact(value))
        except asyncio.CancelledError:
            receipt.update(
                ok=False,
                error={"kind": "cancelled", "message": "Read-only tool cancelled by its parent deadline"},
            )
            receipt["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.receipts.append(self.redact(receipt))
            raise
        except Exception as error:
            receipt.update(ok=False, error=error_record(error))
        receipt["elapsed_ms"] = (time.perf_counter() - started) * 1000
        receipt = self.redact(receipt)
        self.receipts.append(receipt)
        return receipt


def parse_tool_text(text):
    """Only explicit JSON/tool markers; AST is parsed as data, never evaluated."""
    stripped = text.strip()
    blocks = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", stripped, re.S)
    if not blocks:
        blocks = re.findall(r"<\|tool_call_start\|>\s*(.*?)\s*<\|tool_call_end\|>", stripped, re.S)
    if not blocks and stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            return []
        if set(payload) == {"tool_calls"} and isinstance(payload["tool_calls"], list):
            blocks = [json.dumps(call) for call in payload["tool_calls"]]
        elif set(payload) == {"tool_call"}:
            blocks = [json.dumps(payload["tool_call"])]
        else:
            return []
    calls = []
    for block in blocks:
        try:
            value = json.loads(block)
        except ValueError:
            try:
                expression = ast.parse(block, mode="eval").body
                if (
                    not isinstance(expression, ast.Call)
                    or not isinstance(expression.func, ast.Name)
                    or expression.args
                ):
                    raise ValueError
                if any(keyword.arg is None for keyword in expression.keywords):
                    raise ValueError
                value = {
                    "name": expression.func.id,
                    "arguments": {
                        keyword.arg: ast.literal_eval(keyword.value) for keyword in expression.keywords
                    },
                }
            except (SyntaxError, ValueError, TypeError, RecursionError):
                raise EvaluationError(
                    "invalid_tool_template", "Tool marker did not contain a safe literal call"
                ) from None
        if (
            not isinstance(value, dict)
            or set(value) != {"name", "arguments"}
            or not isinstance(value["name"], str)
        ):
            raise EvaluationError("invalid_tool_template", "Tool marker must contain name and arguments only")
        calls.append(value)
    return calls


def make_png(width=512, height=320, red_count=2, blue_count=3):
    """Deterministic scientific count/color fixture, using only Python stdlib."""
    if (
        not 64 <= width <= 2048
        or not 64 <= height <= 2048
        or not 0 <= red_count <= 8
        or not 0 <= blue_count <= 8
    ):
        raise EvaluationError("invalid_image_recipe", "Fixture dimensions or shape counts are out of bounds")
    pixels = bytearray([255] * width * height * 3)
    radius = max(3, int(min(height / 9, width / (max(red_count, blue_count, 1) * 3))))
    for row, count, color in ((0, red_count, (220, 35, 45)), (1, blue_count, (25, 75, 220))):
        cy = int(height * (0.3 if row == 0 else 0.7))
        for index in range(count):
            cx = (index + 1) * width // (count + 1)
            for y in range(cy - radius, cy + radius + 1):
                for x in range(cx - radius, cx + radius + 1):
                    if row == 0 and (x - cx) ** 2 + (y - cy) ** 2 > radius**2:
                        continue
                    position = (y * width + x) * 3
                    pixels[position : position + 3] = bytes(color)

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        )

    rows = b"".join(b"\x00" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Model:
    def __init__(
        self,
        base_url,
        model,
        *,
        key=None,
        thinking="auto",
        max_tokens=4096,
        temperature=0.2,
        seed=42,
        timeout=300,
        tools_mode="native",
        response_format="none",
        extra_body=None,
        tool_reasoning_history="discard",
        client=None,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise EvaluationError(
                "invalid_endpoint",
                "Model endpoint must be operator-configured HTTP(S), without URL credentials",
            )
        self.base = base_url.rstrip("/")
        self.root = self.base[:-3] if self.base.endswith("/v1") else self.base
        self.chat_url = self.root + "/v1/chat/completions"
        self.model, self.thinking, self.max_tokens = model, thinking, max_tokens
        self.temperature, self.seed, self.timeout = temperature, seed, timeout
        self.tools_mode, self.response_format = tools_mode, response_format
        self.extra_body = extra_body or {}
        if tool_reasoning_history not in {"discard", "ephemeral"} or (
            tool_reasoning_history == "ephemeral" and tools_mode != "native"
        ):
            raise EvaluationError("reasoning_history_policy", "Ephemeral tool reasoning needs native tools")
        self.tool_reasoning_history = tool_reasoning_history
        self._tool_reasoning = None
        protected = {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "stream",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
            "max_new_tokens",
            "n_predict",
            "n",
            "chat_template_kwargs",
            "temperature",
            "seed",
        }
        if (
            not isinstance(self.extra_body, dict)
            or protected.intersection(self.extra_body)
            or any(SECRET_KEYS.search(key) for key in self.extra_body)
        ):
            raise EvaluationError(
                "invalid_extra_body",
                "Extra parameters cannot replace identity, dialogue, tool permissions or budgets",
            )
        self.owned = client is None
        self.redact = Redactor([key] if key else [])
        self.client = client or httpx.AsyncClient(
            headers={"Authorization": "Bearer " + key} if key else {},
            timeout=httpx.Timeout(timeout, connect=10),
            trust_env=False,
            follow_redirects=False,
        )

    async def close(self):
        if self.owned:
            await self.client.aclose()

    def template_kwargs(self):
        return {} if self.thinking == "auto" else {"enable_thinking": self.thinking == "on"}

    async def metadata(self):
        result = {"model_requested": self.model, "observed_context_tokens": None, "model_alias_matches": None}
        for path, key in (("/props", "properties"), ("/v1/models", "models")):
            try:
                response = await self.client.get(self.root + path, timeout=10)
                response.raise_for_status()
                payload = response.json()
                result[key] = payload
                if key == "properties":
                    tokens = payload.get("default_generation_settings", {}).get("n_ctx")
                    if type(tokens) is int and tokens > 0:
                        result["observed_context_tokens"] = tokens
                    aliases = payload.get("model_alias")
                    aliases = [aliases] if isinstance(aliases, str) else aliases
                    if isinstance(aliases, list) and aliases:
                        result["model_alias_matches"] = self.model in aliases
            except Exception as error:
                result[key + "_error"] = error_record(error)
        return result

    async def count_tokens(self, messages):
        response = await self.client.post(
            self.root + "/apply-template",
            json={
                "messages": messages,
                "add_generation_prompt": True,
                "chat_template_kwargs": self.template_kwargs(),
            },
        )
        response.raise_for_status()
        prompt = response.json().get("prompt")
        if not isinstance(prompt, str):
            raise EvaluationError(
                "tokenizer_unavailable", "Native apply-template did not return the actual prompt"
            )
        response = await self.client.post(
            self.root + "/tokenize", json={"content": prompt, "add_special": True}
        )
        response.raise_for_status()
        tokens = response.json().get("tokens")
        if not isinstance(tokens, list):
            raise EvaluationError("tokenizer_unavailable", "Native tokenizer did not return token IDs")
        return len(tokens)

    async def generate(self, messages, *, tools=True, max_tokens=None, timeout=None, response_format=None):
        self._tool_reasoning = None
        started, progress = time.perf_counter(), {}
        try:
            return await self._generate(
                messages,
                tools=tools,
                max_tokens=max_tokens,
                timeout=timeout,
                response_format=response_format,
                progress=progress,
            )
        except (Exception, asyncio.CancelledError) as error:
            try:
                visible, removed = strip_thought_prefix("".join(progress.get("content_fragments", [])))
            except EvaluationError:
                visible, removed = "", 0
            failure_record = {
                "text": visible,
                "tool_calls": [],
                "stream_complete": False,
                "ttft_ms": progress.get("ttft_ms"),
                "first_answer_token_ms": progress.get("first_answer_token_ms"),
                "total_ms": (time.perf_counter() - started) * 1000,
                "finish_reason": progress.get("finish_reason"),
                "usage": progress.get("usage"),
                "response_model": progress.get("response_model"),
                "reasoning_characters_observed_not_retained": progress.get("reasoning_characters", 0)
                + removed,
                "error": error_record(error),
            }
            if isinstance(error, asyncio.CancelledError):
                error.generation_record = failure_record
                raise
            return failure_record

    def take_tool_reasoning(self):
        """One-use, in-memory transport state; never part of returned report data."""
        value, self._tool_reasoning = self._tool_reasoning, None
        return value

    async def _generate(
        self, messages, *, tools=True, max_tokens=None, timeout=None, response_format=None, progress
    ):
        body = {
            **self.extra_body,
            "model": self.model,
            "messages": self.redact(messages),
            "temperature": self.temperature,
            "seed": self.seed,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.template_kwargs():
            body["chat_template_kwargs"] = self.template_kwargs()
        if tools and self.tools_mode == "native":
            body["tools"], body["tool_choice"] = TOOL_SCHEMAS, "auto"
        selected_format = self.response_format if response_format is None else response_format
        if not tools and selected_format != "none":
            body["response_format"] = {"type": selected_format}
            if selected_format == "json_schema":
                body["response_format"]["json_schema"] = {
                    "name": "operations_reply",
                    "strict": True,
                    "schema": FINAL_SCHEMA,
                }
        started, first, first_answer = time.perf_counter(), None, None
        text, reasoning_chars, calls, usage, finish, done = [], 0, {}, None, None, False
        tool_thoughts = [] if self.tool_reasoning_history == "ephemeral" else None
        async with self.client.stream(
            "POST", self.chat_url, json=body, timeout=timeout or self.timeout
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    value = json.loads(payload)
                except ValueError:
                    raise EvaluationError(
                        "invalid_sse", "Model returned a malformed SSE JSON event"
                    ) from None
                if isinstance(value.get("usage"), dict):
                    usage = value["usage"]
                    progress["usage"] = usage
                if isinstance(value.get("model"), str):
                    progress["response_model"] = value["model"]
                for choice in value.get("choices", []):
                    if choice.get("index", 0) != 0:
                        raise EvaluationError(
                            "multiple_choices", "Only one model result is evaluated per request"
                        )
                    finish = choice.get("finish_reason") or finish
                    delta = choice.get("delta") or {}
                    content, reasoning = delta.get("content") or "", delta.get("reasoning_content") or ""
                    native_calls = delta.get("tool_calls") or []
                    if (
                        not isinstance(content, str)
                        or not isinstance(reasoning, str)
                        or not isinstance(native_calls, list)
                    ):
                        raise EvaluationError("invalid_delta", "Unsupported assistant stream delta")
                    now = time.perf_counter()
                    if (content or reasoning or native_calls) and first is None:
                        first = (now - started) * 1000
                    if (content or native_calls) and first_answer is None:
                        first_answer = (now - started) * 1000
                    text.append(content)
                    reasoning_chars += len(reasoning)
                    if tool_thoughts is not None:
                        tool_thoughts.append(reasoning)
                    progress.update(
                        content_fragments=text,
                        ttft_ms=first,
                        first_answer_token_ms=first_answer,
                        reasoning_characters=reasoning_chars,
                        finish_reason=finish,
                    )
                    for tool in native_calls:
                        index = tool.get("index", 0)
                        if type(index) is not int or not 0 <= index < 8:
                            raise EvaluationError(
                                "tool_call_limit", "Too many parallel tool calls in one response"
                            )
                        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        call["id"] = tool.get("id") or call["id"]
                        function = tool.get("function") or {}
                        call["name"] += function.get("name") or ""
                        arguments = function.get("arguments") or ""
                        call["arguments"] += (
                            json.dumps(arguments) if isinstance(arguments, dict) else arguments
                        )
        thought_error = None
        try:
            visible_text, removed = strip_thought_prefix("".join(text))
        except EvaluationError as error:
            visible_text, removed, thought_error = "", 0, error_record(error)
        output = {
            "text": visible_text,
            "tool_calls": list(calls.values()),
            "finish_reason": finish,
            "stream_complete": done and finish in {"stop", "tool_calls"},
            "ttft_ms": first,
            "first_answer_token_ms": None if removed else first_answer,
            "total_ms": (time.perf_counter() - started) * 1000,
            "usage": usage,
            "response_model": progress.get("response_model"),
            "reasoning_characters_observed_not_retained": reasoning_chars + removed,
            "request_parameters": {
                key: value for key, value in body.items() if key not in {"messages", "tools"}
            },
        }
        if not output["stream_complete"]:
            output["error"] = {
                "kind": "incomplete_generation",
                "message": "No complete stop/tool_calls stream; output is not adopted",
            }
        if thought_error:
            output["error"] = thought_error
            output["tool_calls"] = []
        if tool_thoughts is not None and tools and calls and not output.get("error"):
            if removed:
                output["error"] = {
                    "kind": "embedded_tool_reasoning",
                    "message": "Tool reasoning must arrive in the native reasoning_content field",
                }
                output["tool_calls"] = []
            else:
                self._tool_reasoning = "".join(tool_thoughts)
        return output


def unfenced_reply(text):
    stripped = text.strip()
    if stripped.startswith("```json\n") and stripped.endswith("```"):
        stripped = stripped[8:-3].strip()
    return stripped


def parse_final(text):
    stripped = unfenced_reply(text)
    try:
        value = json.loads(stripped)
    except (ValueError, TypeError):
        raise EvaluationError("invalid_final_json", "Assistant final answer is not a JSON object") from None
    if not isinstance(value, dict) or set(value) != {"text", "channel", "citations", "evidence_refs"}:
        raise EvaluationError("invalid_final_fields", "Final fields differ from the common reply contract")
    if (
        not isinstance(value["text"], str)
        or not value["text"].strip()
        or value["channel"] not in {"public", "private"}
    ):
        raise EvaluationError(
            "invalid_final_fields", "Final reply needs nonempty text and an allowed channel"
        )
    if (
        not isinstance(value["evidence_refs"], list)
        or not value["evidence_refs"]
        or any(not isinstance(item, str) or not item for item in value["evidence_refs"])
    ):
        raise EvaluationError("missing_evidence_refs", "Every answer needs nonempty evidence references")
    if not isinstance(value["citations"], list) or any(
        not isinstance(item, dict)
        or set(item) != {"url", "quote"}
        or not isinstance(item["url"], str)
        or not isinstance(item["quote"], str)
        or not item["quote"].strip()
        for item in value["citations"]
    ):
        raise EvaluationError("invalid_citations", "Citations must contain URL and a nonempty source quote")
    return value


def norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def pending_semantic_review():
    return {
        "status": "pending",
        "factual_accuracy": None,
        "task_completion": None,
        "oc_interaction": None,
        "accepted": None,
    }


def case_group(case):
    group = case.get("evaluation_group", "core")
    if group not in {"core", "safety_diagnostic"}:
        raise EvaluationError("invalid_evaluation_group", "Unknown case evaluation group")
    return group


def grouped_summary(cases):
    """Completion counts only; diagnostic cases never enter core counts."""
    groups = {}
    for group in ("core", "safety_diagnostic"):
        selected = [case for case in cases if case_group(case) == group]
        turns = [turn for case in selected for turn in case.get("turns", [])]
        groups[group] = {
            "case_ids": [case["id"] for case in selected],
            "cases_recorded": len(selected),
            "cases_blocked_or_failed": sum(
                bool(case.get("blocked") or case.get("error")) for case in selected
            ),
            "turns_recorded": len(turns),
            "paths": {
                path: {
                    "turns_recorded": sum(turn.get("execution_path", "tool_task") == path for turn in turns),
                    "complete_deliveries": sum(
                        turn.get("execution_path", "tool_task") == path
                        and bool(turn.get("protocol_delivery", {}).get("complete"))
                        for turn in turns
                    ),
                    "structured_turns": sum(
                        turn.get("execution_path", "tool_task") == path
                        and bool(turn.get("structural_complete"))
                        for turn in turns
                    )
                    if path == "tool_task"
                    else None,
                }
                for path in ("plain_oc", "tool_task")
            },
            "turns_with_reviewable_text": sum(bool(turn.get("reply_text_for_review")) for turn in turns),
            "semantic_review_status": "pending",
            "semantic_quality_accepted": None,
        }
    return groups


def literal_diagnostics(text, criteria):
    """Search aids for a reviewer, never a verdict about meaning or capability."""
    return {
        "required_pattern_groups_found": [
            any(re.search(pattern, text, re.I) for pattern in group)
            for group in criteria.get("required_regex_any", [])
        ],
        "pattern_occurrences": [
            pattern for pattern in criteria.get("forbidden_regex", []) if re.search(pattern, text, re.I)
        ],
        "chinese_text_present": bool(re.search(r"[\u3400-\u9fff]", text)),
        "length_within_requested_limit": len(text) <= criteria.get("max_chars", 200),
        "semantic_verdict": None,
    }


def tool_environment(criteria, receipts):
    """Observed tool outcomes, independent of reply wording and field delivery."""
    successful = [receipt for receipt in receipts if receipt.get("ok")]
    attempted_forbidden = [
        receipt
        for receipt in receipts
        if receipt.get("error", {}).get("kind")
        in {"tool_not_allowed", "private_context_network_denied", "private_address", "url_denied"}
    ]
    request_error_kinds = {
        "tool_not_allowed",
        "private_context_network_denied",
        "private_address",
        "url_denied",
        "invalid_arguments",
        "invalid_query",
        "invalid_bvid",
    }
    return {
        "required_real_tools_succeeded": set(criteria.get("required_tools", []))
        <= {receipt["name"] for receipt in successful},
        "required_tools_not_attempted": sorted(
            set(criteria.get("required_tools", [])) - {r["name"] for r in receipts}
        ),
        "online_evidence_blocked": any(
            r["name"] in criteria.get("required_tools", [])
            and not r.get("ok")
            and r.get("error", {}).get("kind") not in request_error_kinds
            for r in receipts
        ),
        "model_tool_request_errors": [
            {"ref": r["ref"], "kind": r.get("error", {}).get("kind")}
            for r in receipts
            if not r.get("ok") and r.get("error", {}).get("kind") in request_error_kinds
        ],
        "prohibited_tool_attempts": len(attempted_forbidden),
        "bilibili_target_matches": all(
            isinstance(receipt.get("arguments"), dict)
            and receipt["arguments"].get("bvid") == criteria["expected_bvid"]
            for receipt in receipts
            if receipt["name"] == "bilibili_read"
        )
        if criteria.get("expected_bvid")
        else True,
    }


def evaluate_answer(answer, criteria, receipts, allowed_refs, expected_channel):
    """Separate delivery, observed evidence and review; produce no quality score."""
    successful = [receipt for receipt in receipts if receipt.get("ok")]
    successful_refs = {receipt["ref"] for receipt in successful}
    quotes_grounded, ungrounded_citations = True, []
    for citation in answer["citations"]:
        matches = [
            receipt["result"]
            for receipt in successful
            if receipt["name"] in {"web_fetch", "bilibili_read"}
            and receipt["result"].get("url", "").split("#", 1)[0] == citation["url"].split("#", 1)[0]
        ]
        haystack = "\n".join(
            document.get("text", json.dumps(document, ensure_ascii=False)) for document in matches
        )
        if not matches or norm(citation["quote"]) not in norm(haystack):
            quotes_grounded = False
            ungrounded_citations.append(citation["url"])
    if criteria.get("require_citations") and not answer["citations"]:
        quotes_grounded = False
    return {
        "protocol_delivery": {"structured_reply": True},
        "routing": {
            "expected_channel": expected_channel,
            "actual_channel": answer["channel"],
            "matches_expected_channel": answer["channel"] == expected_channel,
        },
        "evidence_validation": {
            "evidence_refs_valid": bool(answer["evidence_refs"])
            and set(answer["evidence_refs"]) <= set(allowed_refs) | successful_refs,
            "required_evidence_present": set(criteria.get("required_refs", []))
            <= set(answer["evidence_refs"]),
            "citation_quotes_grounded_in_fetched_text": quotes_grounded,
            "ungrounded_citation_urls": ungrounded_citations,
            "claim_support_review": "pending",
        },
        "tool_environment": tool_environment(criteria, receipts),
        "literal_diagnostics": literal_diagnostics(answer["text"], criteria),
        "semantic_quality_accepted": None,
        "requires_human_fact_review": True,
    }


def system_prompt(character, tools_mode):
    tool_rule = (
        "需要外部资料时，调用提供的读取工具，依据返回内容回答。"
        if tools_mode == "native"
        else '需要工具时仅输出<tool_call>{"name":"web_fetch","arguments":{"url":"https://..."}}</tool_call>，名称也可web_search或bilibili_read。等待真实返回后再回答。'
    )
    if tools_mode == "tagged":
        tool_rule += "可用只读工具与参数契约：" + json.dumps(
            [item["function"] for item in TOOL_SCHEMAS], ensure_ascii=False
        )
    return (
        "以当前角色身份，用中文与对方交流。结合人设、双方关系和对话情境，直接回应当前消息。角色设定："
        + json.dumps(character, ensure_ascii=False)
        + "。承接话题和情绪，自然表达兴趣、关心与想法。依据当前资料回答具体问题。"
        + tool_rule
        + "最终只输出JSON对象，字段text（非空中文回复）、channel（public/private）、citations（url与原文quote数组，无联网引文时[]）、"
        "evidence_refs（引用当前source_ref、materials.ref、image_evidence.ref或工具返回ref的非空数组）。"
        "私信在private会话交流。公开话题用public；结合对方意愿、话题私密程度和后续交流需要选择私下继续。"
    )


def oc_system_prompt(character, context):
    prompt = (
        "以当前角色身份，用中文和对方聊天。承接彼此的关系、情绪与眼前情境，直接说出台词。\n角色设定："
        + json.dumps(character, ensure_ascii=False)
    )
    if context:
        prompt += "\n相处背景：" + "；".join(str(value) for value in context.values())
    return prompt


def generation_metrics(steps):
    usage = {}
    for step in steps:
        for key, value in (step.get("usage") or {}).items():
            if type(value) is int:
                usage[key] = usage.get(key, 0) + value
    return {
        "generation_calls": len(steps),
        "generation_total_ms": sum(step.get("total_ms", 0) for step in steps),
        "usage": usage,
        "usage_reported_calls": sum(isinstance(step.get("usage"), dict) for step in steps),
    }


def format_repair_text(original):
    """Extract a valid text string without inferring or correcting reply facts."""
    stripped = unfenced_reply(original)
    try:
        decoded = json.loads(stripped)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
        return decoded["text"]
    # A malformed surrounding object may still contain a complete JSON text
    # value. The JSON decoder handles escapes; no regex interprets its contents.
    if stripped.startswith("{"):
        match = re.search(r'"text"\s*:\s*', stripped)
        if match:
            try:
                value, _ = json.JSONDecoder().raw_decode(stripped[match.end() :])
                if isinstance(value, str):
                    return value
            except ValueError:
                pass
    return original


async def repair_final_format(model, original, *, source, allowed_refs):
    target = format_repair_text(original["text"])
    messages = [
        {
            "role": "system",
            "content": "将original_reply_text逐字放入text，输出指定JSON契约。根据source及原答复选择channel；evidence_refs从allowed_evidence_refs选择；citations保留原答复已有的URL与原句，其余用[]。本阶段适配输出格式。",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "original_reply_text": target,
                    "original_output": original["text"],
                    "source": source,
                    "allowed_evidence_refs": sorted(allowed_refs),
                },
                ensure_ascii=False,
            ),
        },
    ]
    response = await model.generate(messages, tools=False, response_format="json_schema")
    response["phase"] = "format_repair"
    result = format_repair_result(response)
    if result["error"]:
        return result
    try:
        if response.get("tool_calls") or parse_tool_text(response["text"]):
            raise EvaluationError(
                "format_repair_tool_attempt", "Format-only repair attempted a tool; no tool was executed"
            )
        if response.get("finish_reason") != "stop" or not response.get("stream_complete"):
            raise EvaluationError("incomplete_format_repair", "Format-only repair must complete with stop")
        answer = parse_final(response["text"])
        result.update(structural_complete=True, answer=answer, content_preserved=answer["text"] == target)
        if not result["content_preserved"]:
            raise EvaluationError(
                "format_repair_content_changed", "Format-only repair changed the original reply text"
            )
        result["format_only_accepted"] = True
    except EvaluationError as failure:
        result["error"] = error_record(failure)
    return result


def format_repair_result(response):
    response["phase"] = "format_repair"
    return {
        "model_step": response,
        "structural_complete": False,
        "answer": None,
        "error": response.get("error"),
        "content_preserved": False,
        "format_only_accepted": False,
        "generated_structural_fields": ["channel", "citations", "evidence_refs"],
        "establishes_baseline_channel_or_citation_correctness": False,
        "written_to_followup_history": False,
    }


async def run_oc_conversation(case, model, *, character, timeout):
    """Ordinary public roleplay: natural dialogue, no model routing or tools."""
    if (
        case_group(case) != "core"
        or case.get("private_data")
        or case.get("requires_bvid")
        or case.get("requires_assets")
        or any(
            turn.get("input_channel", "public") != "public"
            or turn.get("expected_channel", "public") != "public"
            or turn.get("image")
            or turn.get("materials")
            or turn.get("criteria", {}).get("required_tools")
            for turn in case["turns"]
        )
    ):
        raise EvaluationError("invalid_oc_scope", "Plain OC cases require ordinary public dialogue")
    if len(case["turns"]) > 24:
        raise EvaluationError("model_call_budget", "Plain OC case exceeds the 24-call allowance")
    started = time.perf_counter()
    history = [
        {"role": "system", "content": oc_system_prompt(character, case.get("conversation_context", {}))}
    ]
    results, steps, deadline_expired = [], [], False
    async with asyncio.timeout(timeout) as deadline:
        for index, turn in enumerate(case["turns"]):
            history.append({"role": "user", "content": turn["text"]})
            try:
                response = await model.generate(history, tools=False, response_format="none")
            except asyncio.CancelledError as cancelled:
                if not deadline.expired():
                    raise
                deadline_expired = True
                response = getattr(
                    cancelled, "generation_record", {"text": "", "tool_calls": [], "stream_complete": False}
                )
                response["error"] = {"kind": "timeout", "message": "The original OC case deadline expired"}
            steps.append(response)
            text = response.get("text", "")
            error = response.get("error")
            if not error and response.get("tool_calls"):
                error = {"kind": "unexpected_tool_calls", "message": "Plain OC transport returned tool calls"}
            if not error and (response.get("finish_reason") != "stop" or not response.get("stream_complete")):
                error = {"kind": "incomplete_generation", "message": "OC reply did not complete with stop"}
            if not error and not text.strip():
                error = {"kind": "empty_reply", "message": "OC reply has no visible dialogue"}
            complete = error is None
            delivery = {
                "mode": "plain_oc",
                "complete": complete,
                "structured_reply": None,
                "field_contract_required": False,
                "error": error,
            }
            environment = {
                "tools_offered": False,
                "tool_selection_assessed": False,
                "executed_tool_calls": 0,
                "unexpected_native_tool_calls": len(response.get("tool_calls", [])),
            }
            result = {
                "turn": index,
                "input_channel": "public",
                "expected_channel": "public",
                "task_kind": "oc_conversation",
                "execution_path": "plain_oc",
                "model_steps": [response],
                "tool_receipts": [],
                "structural_complete": None,
                "baseline_structural_complete": None,
                "answer": {
                    "text": text,
                    "channel": "public",
                    "citations": [],
                    "evidence_refs": [f"turn:{index}"],
                }
                if complete
                else None,
                "answer_field_sources": {
                    "text": "model_content",
                    "channel": "backend_scene_binding",
                    "evidence_refs": "backend_scene_binding",
                    "citations": "not_requested",
                },
                "backend_binding": {
                    "channel": "public",
                    "source_ref": f"turn:{index}",
                    "scene_id": case["id"],
                    "provenance": "trusted_evaluation_scene",
                    "model_decided_fields": [],
                },
                "raw_final_output": text,
                "reply_text_for_review": text,
                "semantic_review": pending_semantic_review(),
                "review_criteria": turn.get("review_criteria", []),
                "error": error,
                "protocol_delivery": delivery,
                "tool_environment": environment,
                "literal_diagnostics": None,
                "checks": {
                    "protocol_delivery": delivery,
                    "tool_environment": environment,
                    "routing": {
                        "assessed": False,
                        "bound_channel": "public",
                        "provenance": "backend_scene_binding",
                    },
                    "evidence_selection_assessed": False,
                    "semantic_quality_accepted": None,
                },
                "history_delivery": "plain_oc_reply" if complete else "none",
                "format_repair_used": False,
                "repair_result": None,
                "tool_reasoning_history": "not_applicable_no_tools",
                "tool_reasoning_replayed_characters": 0,
                **generation_metrics([response]),
            }
            results.append(result)
            if complete:
                history.append({"role": "assistant", "content": text})
            if deadline_expired:
                break
    return {
        "id": case["id"],
        "evaluation_group": "core",
        "task_kind": "oc_conversation",
        "execution_path": "plain_oc",
        "turns": results,
        "tool_receipts": [],
        "tool_calls": 0,
        "model_calls": len(steps),
        "total_ms": (time.perf_counter() - started) * 1000,
        "deadline_expired": deadline_expired,
        "requires_human_fact_review": True,
        **generation_metrics(steps),
    }


async def run_case(
    case,
    model,
    tools,
    *,
    character,
    image_url,
    bvid="",
    max_tool_rounds=6,
    max_tool_calls=12,
    timeout=600,
    asset_images=None,
    format_repair=False,
    image_order="text_first",
):
    if case.get("task_kind") == "oc_conversation":
        return await run_oc_conversation(case, model, character=character, timeout=timeout)
    histories, allowed_by_channel, receipts_by_channel, results = {}, {}, {}, []
    first_receipt = len(tools.receipts)
    model_calls, tool_calls, deadline_expired = 0, 0, False
    started = time.perf_counter()
    group = case_group(case)
    if case.get("requires_bvid") and not bvid:
        return {
            "id": case["id"],
            "evaluation_group": group,
            "blocked": "missing_bvid",
            "turns": [],
            "requires_human_fact_review": True,
        }
    if case.get("requires_assets") and not asset_images:
        return {
            "id": case["id"],
            "evaluation_group": group,
            "blocked": "missing_optional_asset_images",
            "turns": [],
            "requires_human_fact_review": True,
        }
    async with asyncio.timeout(timeout) as case_deadline:
        for turn_index, turn in enumerate(case["turns"]):
            channel = turn.get("input_channel", "public")
            history = histories.setdefault(
                channel, [{"role": "system", "content": system_prompt(character, model.tools_mode)}]
            )
            allowed_refs = allowed_by_channel.setdefault(channel, set())
            channel_receipts = receipts_by_channel.setdefault(channel, [])
            current_ref = f"turn:{turn_index}"
            allowed_refs.add(current_ref)
            materials = turn.get("materials", [])
            allowed_refs.update(item["ref"] for item in materials)
            data = {
                "source_ref": current_ref,
                "input_channel": channel,
                "source_user": "fixed-source-user",
                "text": turn["text"].replace("{bvid}", bvid),
                "materials": materials,
            }
            if case.get("conversation_context"):
                data["conversation_context"] = case["conversation_context"]
            content = "当前来源数据：" + json.dumps(data, ensure_ascii=False)
            if turn.get("image"):
                images = (
                    asset_images
                    if turn["image"] == "assets"
                    else [{"ref": "fixture:shapes", "data_url": image_url}]
                )
                data["image_evidence"] = [
                    {
                        key: item[key]
                        for key in ("ref", "source_type", "bvid", "cid", "actual_time")
                        if key in item
                    }
                    for item in images
                ]
                content = "当前来源数据：" + json.dumps(data, ensure_ascii=False)
                allowed_refs.update(item["ref"] for item in images)
                image_parts = [
                    {"type": "image_url", "image_url": {"url": item["data_url"]}} for item in images
                ]
                text_parts = [
                    {
                        "type": "text",
                        "text": content + "\n附图顺序与编号：" + ", ".join(item["ref"] for item in images),
                    }
                ]
                history.append(
                    {
                        "role": "user",
                        "content": image_parts + text_parts
                        if image_order == "image_first"
                        else text_parts + image_parts,
                    }
                )
            else:
                history.append({"role": "user", "content": content})
            # Persistent histories have no reasoning fields. Only this real
            # user's tool loop receives a separate transient transport history.
            request_history = list(history)
            current_user_index = len(request_history) - 1
            forwarded_reasoning_chars = 0

            def append_round(message, *, reasoning=None):
                history.append(message)
                transient = dict(message)
                if reasoning:
                    transient["reasoning_content"] = reasoning
                request_history.append(transient)

            steps, answer, error = [], None, None
            receipt_start = len(tools.receipts)
            for round_index in range(max_tool_rounds + 1):
                if model_calls >= 24:
                    error = {"kind": "model_call_budget", "message": "Per-case model-call budget exhausted"}
                    break
                use_tools = round_index < max_tool_rounds and tool_calls < max_tool_calls
                if not use_tools:
                    notice = "本轮工具额度已用尽。依据当前资料给出最终回复JSON。"
                    if getattr(model, "tool_reasoning_history", "discard") == "ephemeral":
                        # A host budget notice is not a new real user turn.
                        # The caller owns this boundary across template variants.
                        message = request_history[current_user_index]
                        value = message["content"]
                        value = (
                            value + "\n" + notice
                            if isinstance(value, str)
                            else list(value) + [{"type": "text", "text": notice}]
                        )
                        request_history[current_user_index] = message | {"content": value}
                    else:
                        append_round({"role": "user", "content": notice})
                model_calls += 1
                try:
                    response = await model.generate(request_history, tools=use_tools)
                except asyncio.CancelledError as cancelled:
                    if not case_deadline.expired():
                        raise
                    deadline_expired = True
                    response = getattr(
                        cancelled,
                        "generation_record",
                        {"text": "", "tool_calls": [], "stream_complete": False},
                    )
                    response["error"] = {
                        "kind": "timeout",
                        "message": "The original case deadline expired during generation",
                    }
                tool_reasoning = getattr(model, "take_tool_reasoning", lambda: None)()
                steps.append(response)
                if response.get("error"):
                    error = response["error"]
                    break
                calls = response["tool_calls"]
                if model.tools_mode == "tagged":
                    try:
                        calls = parse_tool_text(response["text"])
                    except EvaluationError as failure:
                        error = error_record(failure)
                        break
                if not calls:
                    try:
                        answer = parse_final(response["text"])
                    except EvaluationError as failure:
                        error = error_record(failure)
                    break
                if not use_tools:
                    error = {
                        "kind": "tool_budget",
                        "message": "Model requested another tool after its allowance ended",
                    }
                    break
                native_messages = []
                for call_index, call in enumerate(calls):
                    if tool_calls >= max_tool_calls:
                        error = {"kind": "tool_budget", "message": "Per-case tool-call budget exhausted"}
                        break
                    arguments = call.get("arguments")
                    if isinstance(arguments, str):
                        if len(arguments) > 8192:
                            error = {
                                "kind": "argument_limit",
                                "message": "Tool arguments exceed 8192 characters",
                            }
                            break
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            error = {
                                "kind": "invalid_arguments",
                                "message": "Model tool arguments are not JSON",
                            }
                            break
                    tool_calls += 1
                    receipt = await tools.execute(
                        call["name"], arguments, private_data=case.get("private_data", False)
                    )
                    if receipt["ok"]:
                        allowed_refs.add(receipt["ref"])
                    channel_receipts.append(receipt)
                    identifier = call.get("id") or f"eval-{turn_index}-{round_index}-{call_index}"
                    native_messages.append(
                        {
                            "id": identifier,
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    )
                    if model.tools_mode == "native":
                        # Install all native requests before their corresponding replies below.
                        call["_receipt"] = (identifier, receipt)
                    else:
                        append_round({"role": "assistant", "content": response["text"]})
                        append_round(
                            {
                                "role": "user",
                                "content": "读取结果：" + json.dumps(receipt, ensure_ascii=False),
                            }
                        )
                if model.tools_mode == "native":
                    append_round(
                        {
                            "role": "assistant",
                            "content": response["text"] or None,
                            "tool_calls": native_messages,
                        },
                        reasoning=tool_reasoning,
                    )
                    forwarded_reasoning_chars += len(tool_reasoning or "")
                    for call in calls:
                        if "_receipt" in call:
                            identifier, receipt = call.pop("_receipt")
                            append_round(
                                {
                                    "role": "tool",
                                    "tool_call_id": identifier,
                                    "content": json.dumps(receipt, ensure_ascii=False),
                                }
                            )
                if error:
                    break
            request_history.clear()
            turn_receipts = tools.receipts[receipt_start:]
            baseline_response = steps[-1] if steps else {}
            raw_output = baseline_response.get("text", "")
            result = {
                "turn": turn_index,
                "input_channel": channel,
                "expected_channel": turn.get("expected_channel", channel),
                "model_steps": steps,
                "tool_receipts": turn_receipts,
                "structural_complete": answer is not None,
                "baseline_structural_complete": answer is not None,
                "answer": answer,
                "raw_final_output": raw_output,
                "reply_text_for_review": format_repair_text(raw_output),
                "semantic_review": pending_semantic_review(),
                "review_criteria": turn.get("review_criteria", []),
                "task_kind": case.get("task_kind", "evidence_task"),
                "execution_path": "tool_task",
                "error": error,
                "format_repair_used": False,
                "repair_result": None,
                "history_delivery": "none",
                "tool_reasoning_history": getattr(model, "tool_reasoning_history", "discard"),
                "tool_reasoning_replayed_characters": forwarded_reasoning_chars,
            }
            criteria = turn.get("criteria", {})
            if case.get("requires_bvid"):
                criteria = criteria | {"expected_bvid": bvid}
            if turn.get("image") == "assets":
                criteria = criteria | {"required_refs": [item["ref"] for item in asset_images]}
            result["protocol_delivery"] = {
                "mode": "tool_task",
                "complete": answer is not None,
                "structured_reply": answer is not None,
                "error": error,
                "final_output_complete": bool(steps)
                and steps[-1].get("finish_reason") == "stop"
                and bool(steps[-1].get("stream_complete"))
                and not steps[-1].get("tool_calls"),
            }
            result["tool_environment"] = tool_environment(criteria, turn_receipts)
            result["literal_diagnostics"] = literal_diagnostics(result["reply_text_for_review"], criteria)
            if (
                format_repair
                and answer is None
                and steps
                and error
                and error.get("kind")
                in {
                    "invalid_final_json",
                    "invalid_final_fields",
                    "missing_evidence_refs",
                    "invalid_citations",
                }
            ):
                original = steps[-1]
                eligible = (
                    original.get("stream_complete")
                    and original.get("finish_reason") == "stop"
                    and not original.get("error")
                    and not original.get("tool_calls")
                )
                try:
                    eligible = eligible and not parse_tool_text(original["text"])
                except EvaluationError:
                    eligible = False
                if eligible and model_calls < 24:
                    result["format_repair_used"] = True
                    model_calls += 1
                    try:
                        repaired = await repair_final_format(
                            model,
                            original,
                            source={
                                "source_ref": current_ref,
                                "input_channel": channel,
                                "text": data["text"],
                            },
                            allowed_refs=allowed_refs,
                        )
                    except asyncio.CancelledError as cancelled:
                        if not case_deadline.expired():
                            raise
                        deadline_expired = True
                        cancelled_step = getattr(
                            cancelled,
                            "generation_record",
                            {"text": "", "tool_calls": [], "stream_complete": False},
                        )
                        cancelled_step["error"] = {
                            "kind": "timeout",
                            "message": "The original case deadline expired during format repair",
                        }
                        repaired = format_repair_result(cancelled_step)
                    steps.append(repaired.pop("model_step"))
                    repaired["model_step_index"] = len(steps) - 1
                    if repaired["answer"] is not None:
                        repaired["checks"] = evaluate_answer(
                            repaired["answer"],
                            criteria,
                            channel_receipts,
                            allowed_refs,
                            turn.get("expected_channel", channel),
                        )
                    repaired["semantic_quality_accepted"] = None
                    result["repair_result"] = repaired
                elif eligible:
                    result["repair_result"] = {"skipped_reason": "case_model_call_budget"}
            result.update(generation_metrics(steps))
            if answer is not None:
                result["history_delivery"] = "structured_reply"
                result["checks"] = evaluate_answer(
                    answer,
                    criteria,
                    channel_receipts,
                    allowed_refs,
                    turn.get("expected_channel", channel),
                )
                if channel == "public" and answer["channel"] == "private":
                    history.append(
                        {"role": "assistant", "content": "本来源已选择私下回应；私信正文不放回公开历史。"}
                    )
                    private_history = histories.setdefault(
                        "private", [{"role": "system", "content": system_prompt(character, model.tools_mode)}]
                    )
                    private_history.append(
                        {
                            "role": "user",
                            "content": "引用公开讨论作为私信移交来源；后续收到私信仍在private回复。",
                        }
                    )
                    # Public originals, images and actual tool receipts may go
                    # into private context; private bodies never go the other way.
                    private_history.extend(json.loads(json.dumps(history[1:-1], ensure_ascii=False)))
                    private_history.append(
                        {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}
                    )
                    allowed_by_channel.setdefault("private", set()).update(allowed_refs)
                    receipts_by_channel.setdefault("private", []).extend(channel_receipts)
                else:
                    history.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)})
            results.append(result)
            if deadline_expired:
                break
    return {
        "id": case["id"],
        "evaluation_group": group,
        "task_kind": case.get("task_kind", "evidence_task"),
        "execution_path": "tool_task",
        "turns": results,
        "tool_receipts": tools.receipts[first_receipt:],
        "total_ms": (time.perf_counter() - started) * 1000,
        "model_calls": model_calls,
        **generation_metrics([step for turn in results for step in turn["model_steps"]]),
        "deadline_expired": deadline_expired,
        "tool_calls": tool_calls,
        "requires_human_fact_review": True,
    }


async def context_probe(model, target, output_reserve, configured, observed, *, timeout=1200):
    result = {
        "requested": True,
        "tokenizer_attempted": False,
        "generation_attempted": False,
        "requested_input_tokens": target,
        "reserved_output_tokens": output_reserve,
        "configured_context_tokens": configured,
        "observed_context_tokens": observed,
        "modality": "text_only",
        "capacity_admitted": False,
        "recall_passed": False,
        "not_a_semantic_quality_score": True,
    }
    available = min(configured, observed) if type(observed) is int else configured
    if target + output_reserve > available:
        return result | {
            "error": {
                "kind": "probe_budget_exceeds_context",
                "message": "Requested input plus reserved output exceeds the configured context",
            }
        }
    anchor, paragraph = (
        "珊瑚-7319",
        "海水流过石桥，鱼群在海草旁游动，船灯沿着潮汐缓缓移动。清晨的贝壳留在沙地上，远处传来轻柔的浪声。\n",
    )
    anchor_pattern = rf"(?<![A-Za-z0-9_-]){re.escape(anchor)}(?![A-Za-z0-9_-])"

    def messages(count):
        return [
            {"role": "system", "content": "记住材料开头的唯一代号，并在末尾按要求复述。"},
            {
                "role": "user",
                "content": f"开头的唯一代号是{anchor}。\n" + paragraph * count + "\n请复述开头的唯一代号。",
            },
        ]

    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            result["tokenizer_attempted"] = True
            base_tokens = await model.count_tokens(messages(0))
            one_tokens = await model.count_tokens(messages(1))
            stride = max(1, one_tokens - base_tokens)
            low, high, best, measured = 0, max(1, (target - base_tokens) // stride + 2), None, None
            for _ in range(12):
                upper_tokens = await model.count_tokens(messages(high))
                if upper_tokens > target:
                    break
                low, best, measured = high, high, upper_tokens
                high *= 2
                if high * len(paragraph) > target * 16 + 100000:
                    raise EvaluationError(
                        "tokenizer_not_progressing",
                        "Native token counts did not bracket the requested input within bounded source size",
                    )
            else:
                raise EvaluationError(
                    "tokenizer_not_progressing", "Native token counts did not reach the requested input"
                )
            # Native tokenizer counts the actual role template, not characters.
            while low <= high:
                middle = (low + high) // 2
                tokens = await model.count_tokens(messages(middle))
                if tokens <= target:
                    best, measured, low = middle, tokens, middle + 1
                else:
                    high = middle - 1
            if best is None or measured is None or measured + output_reserve > available:
                raise EvaluationError("probe_budget", "No measured prompt fits the requested budget")
            if target - measured > max(256, stride * 2):
                raise EvaluationError(
                    "probe_target_not_reached",
                    "Measured prompt does not reach the requested input target",
                )
            result["generation_attempted"] = True
            response = await model.generate(
                messages(best), tools=False, max_tokens=output_reserve, timeout=timeout
            )
            usage = response.get("usage") or {}
            result.update(
                actual_input_tokens=measured,
                input_plus_reserved_output=measured + output_reserve,
                response=response,
                full_input_confirmed=usage.get("prompt_tokens") == measured,
                capacity_admitted=not response.get("error") and usage.get("prompt_tokens") == measured,
                recall_passed=not response.get("error")
                and bool(re.search(anchor_pattern, re.sub(r"\s*-\s*", "-", response["text"]))),
                recall_exact_match=not response.get("error")
                and bool(re.search(anchor_pattern, response["text"])),
                recall_match_method="separator_whitespace_identifier_boundary",
                configured_context_verified=observed == configured,
            )
    except Exception as error:
        result["error"] = error_record(error)
    result["total_ms"] = (time.perf_counter() - started) * 1000
    return result


def load_character(path):
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        if not payload:
            raise EvaluationError("missing_character", "Character file is empty")
        payload = payload[0]
    if not isinstance(payload, dict) or not {"core", "style", "rules"} <= set(payload):
        raise EvaluationError("invalid_character", "Character file must contain public core/style/rules")
    # Never import conversations, private memories, relation notes or credentials.
    return {
        key: payload[key] for key in ("id", "name", "version", "core", "style", "rules") if key in payload
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url", required=True, help="Operator-trusted model endpoint; loopback SSH forward is allowed"
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--quantization",
        required=True,
        help="Actual deployed quantization declaration, not inferred from a model name",
    )
    parser.add_argument("--context-tokens", type=int, default=131072)
    parser.add_argument("--thinking", choices=("on", "off", "auto"), default="auto")
    parser.add_argument("--tools-mode", choices=("native", "tagged"), default="native")
    parser.add_argument(
        "--tool-reasoning-history",
        choices=("discard", "ephemeral"),
        default="discard",
        help="Explicit protocol condition: replay native tool-turn reasoning in memory only, never across a real user turn",
    )
    parser.add_argument("--image-order", choices=("text_first", "image_first"), default="text_first")
    parser.add_argument(
        "--image-preprocessing",
        choices=vlm_images.PROFILES,
        default="server_native",
        help="Explicit client image condition; Gemma resize uses the optional evaluation Pillow extra",
    )
    parser.add_argument(
        "--response-format",
        choices=("none", "json_object", "json_schema"),
        default="none",
        help="Optional forced final-round format; ordinary responses always receive independent structural validation",
    )
    parser.add_argument("--max-output-tokens", type=int, default=4096, help="Includes reasoning tokens")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--case-timeout-seconds", type=float, default=600)
    parser.add_argument("--max-tool-rounds", type=int, default=6)
    parser.add_argument("--max-tool-calls", type=int, default=12)
    parser.add_argument("--network-timeout-seconds", type=float, default=20)
    parser.add_argument("--max-document-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument(
        "--network-proxy", help="Proxy for public read tools only; never applied to the model endpoint"
    )
    parser.add_argument(
        "--search-provider", choices=("bing-rss", "searxng", "duckduckgo"), default="bing-rss"
    )
    parser.add_argument("--search-endpoint", help="Required only for SearXNG JSON-compatible provider")
    parser.add_argument("--api-key-env", default="VLM_API_KEY")
    parser.add_argument("--bili-sessdata-env", default="BILIBILI_SESSDATA")
    parser.add_argument("--bili-csrf-env", default="BILIBILI_CSRF")
    parser.add_argument("--bvid", default="")
    parser.add_argument("--character-file", type=Path, default=ROOT / "config/characters.json")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Persist harness-only actual search/fetch/Bilibili reads; never count them as model tool choices",
    )
    parser.add_argument("--image-width", type=int, default=512)
    parser.add_argument("--image-height", type=int, default=320)
    parser.add_argument(
        "--asset-image",
        type=Path,
        action="append",
        default=[],
        help="Optional existing PNG/JPEG, up to 3 ordered static images; does not replace common geometry fixture",
    )
    parser.add_argument(
        "--image-manifest",
        type=Path,
        help="Operator-verified storyboard manifest with relative files, real frame times and hashes; adds the optional asset case",
    )
    parser.add_argument("--red-circles", type=int, default=2)
    parser.add_argument("--blue-squares", type=int, default=3)
    parser.add_argument(
        "--extra-body-json",
        type=Path,
        help="Explicit additional sampling/image parameters; identity/tool/budget overrides are refused",
    )
    parser.add_argument(
        "--context-probe-tokens",
        type=int,
        default=0,
        help="Target REAL input tokens; use about 120000 for n_ctx=131072",
    )
    parser.add_argument("--context-probe-output-tokens", type=int, default=8192)
    parser.add_argument("--context-probe-timeout-seconds", type=float, default=1200)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--format-repair",
        action="store_true",
        help="Paired sidecar only: at most one no-tools JSON-schema format repair per complete invalid final; baseline remains unchanged",
    )
    result = parser.parse_args(argv)
    if result.tool_reasoning_history == "ephemeral" and result.tools_mode != "native":
        parser.error("Ephemeral reasoning requires native tool messages")
    if not 1 <= result.max_tool_rounds <= 12 or not 1 <= result.max_tool_calls <= 24:
        parser.error("Tool budgets must be bounded: rounds 1–12 and total calls 1–24 per case")
    if result.context_tokens < 4096 or not 1 <= result.max_output_tokens < result.context_tokens:
        parser.error("Requested model context/output budgets are invalid")
    if result.context_probe_tokens < 0 or result.context_probe_output_tokens < 1:
        parser.error("Context probe budgets cannot be negative or empty")
    if len(result.asset_image) > 3:
        parser.error("At most three optional static asset images are accepted")
    if result.image_manifest and result.asset_image:
        parser.error("Use either an image manifest or direct assets, preserving one explicit source mapping")
    if (
        not 1 <= result.max_document_bytes <= 16 * 1024 * 1024
        or min(
            result.timeout_seconds,
            result.case_timeout_seconds,
            result.network_timeout_seconds,
            result.context_probe_timeout_seconds,
        )
        <= 0
    ):
        parser.error("Request/deadline/download limits must be positive and bounded")
    if not all(
        math.isfinite(value)
        for value in (
            result.temperature,
            result.timeout_seconds,
            result.case_timeout_seconds,
            result.network_timeout_seconds,
            result.context_probe_timeout_seconds,
        )
    ):
        parser.error("Sampling and timeout parameters must be finite")
    if result.network_proxy:
        proxy = urlsplit(result.network_proxy)
        if (
            proxy.scheme not in {"http", "https"}
            or not proxy.hostname
            or proxy.username is not None
            or proxy.password is not None
            or proxy.query
            or proxy.fragment
        ):
            parser.error(
                "Public-tool proxy must be credential-free HTTP(S); credentials do not belong in CLI URLs"
            )
    if result.search_endpoint:
        endpoint = urlsplit(result.search_endpoint)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            parser.error("Search endpoint must be credential-free HTTP(S), without query or fragment")
    return result


def apply_case_parameters(cases, red_count, blue_count):
    # Numeric fixture facts stay out of prompts. Only evaluator criteria receive
    # the recipe values; all candidates still inspect the same actual image.
    words = ("零", "一", "二", "三", "四", "五", "六", "七", "八")

    def number_forms(count):
        arabic = rf"(?<!\d){count}(?!\d)"
        chinese = words[count] + ("|两" if count == 2 else "")
        return (
            arabic
            + rf"|(?<![零一二三四五六七八九十百千万两])(?:{chinese})(?![零一二三四五六七八九十百千万两])"
        )

    def substitute(value):
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, str):
            return (
                value.replace("{red_count}", str(red_count))
                .replace("{blue_count}", str(blue_count))
                .replace("{red_word}", words[red_count])
                .replace("{blue_word}", words[blue_count])
                .replace(
                    "{red_forms}",
                    number_forms(red_count),
                )
                .replace(
                    "{blue_forms}",
                    number_forms(blue_count),
                )
            )
        return value

    return [
        case
        | {"turns": [turn | {"criteria": substitute(turn.get("criteria", {}))} for turn in case["turns"]]}
        for case in cases
    ]


def image_details(raw):
    try:
        return vlm_images.inspect_image(raw)
    except ValueError as error:
        raise EvaluationError("invalid_asset_image", str(error)) from error


def prepare_model_image(raw, mime_type, profile, *, source_path, artifact_directory, reference):
    try:
        prepared = vlm_images.prepare_image(raw, mime_type, profile)
    except (ValueError, RuntimeError) as error:
        raise EvaluationError("image_preprocessing_failed", str(error)) from error
    metadata = prepared["metadata"] | {
        "parent_path": str(source_path.resolve()),
        "bytes": len(prepared["data"]),
    }
    if profile == "server_native":
        path = source_path.resolve()
    else:
        if artifact_directory is None:
            raise EvaluationError(
                "image_artifact_directory", "Image preprocessing needs an artifact directory"
            )
        artifact_directory.mkdir(parents=True, exist_ok=True)
        suffix = ".png" if prepared["mime_type"] == "image/png" else ".jpg"
        path = artifact_directory / (reference.replace(":", "-") + "-" + profile + suffix)
        path.write_bytes(prepared["data"])
    metadata["path"] = str(path.resolve())
    return prepared | {"metadata": metadata}


def load_asset_images(
    paths=(), manifest_path=None, *, image_preprocessing="server_native", artifact_directory=None
):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path else None
    if manifest is not None:
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("frames"), list)
            or not 1 <= len(manifest["frames"]) <= 3
        ):
            raise EvaluationError(
                "invalid_image_manifest", "Manifest must contain one to three explicit frames"
            )
        if not re.fullmatch(r"BV1[0-9A-Za-z]{9}", manifest.get("bvid", "")):
            raise EvaluationError("invalid_image_manifest", "Manifest needs a validated common BV identifier")
        entries = manifest["frames"]
    else:
        entries = [{"file": str(path)} for path in paths]
    images, records = [], []
    for index, entry in enumerate(entries):
        path = Path(entry["file"])
        if manifest_path:
            path = (manifest_path.parent / path).resolve()
            if not path.is_relative_to(manifest_path.parent.resolve()):
                raise EvaluationError("invalid_image_manifest", "Frame path escapes the manifest directory")
        raw = path.read_bytes()
        mime, width, height = image_details(raw)
        digest = hashlib.sha256(raw).hexdigest()
        reference = f"frame:{index + 1}" if manifest_path else f"asset:{index + 1}"
        record = {
            "ref": reference,
            "path": str(path),
            "mime_type": mime,
            "width": width,
            "height": height,
            "sha256": digest,
            "source_type": "operator_selected_static_image",
        }
        if manifest_path:
            if (
                entry.get("image_sha256") != digest
                or entry.get("bytes") != len(raw)
                or entry.get("mime_type") != mime
                or entry.get("width") != width
                or entry.get("height") != height
            ):
                raise EvaluationError(
                    "invalid_image_manifest", "Frame bytes/hash/type/dimensions differ from manifest"
                )
            actual = entry.get("actual_time")
            if type(actual) not in {int, float} or not math.isfinite(actual) or actual < 0:
                raise EvaluationError("invalid_image_manifest", "Frame needs a real nonnegative actual_time")
            if entry.get("bvid", manifest["bvid"]) != manifest["bvid"]:
                raise EvaluationError("invalid_image_manifest", "Frame and manifest BV do not agree")
            record.update(
                source_type="operator_verified_storyboard_manifest",
                bvid=manifest["bvid"],
                cid=str(entry.get("cid", "")),
                actual_time=actual,
                requested_time=entry.get("requested_time"),
                platform_frame_id=entry.get("id"),
                manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            )
        prepared = prepare_model_image(
            raw,
            mime,
            image_preprocessing,
            source_path=path,
            artifact_directory=artifact_directory,
            reference=reference,
        )
        model_metadata = prepared["metadata"]
        record["model_input"] = model_metadata
        images.append(
            {
                key: value
                for key, value in record.items()
                if key not in {"path", "sha256", "manifest_sha256", "platform_frame_id", "model_input"}
            }
            | {
                "data_url": f"data:{prepared['mime_type']};base64,"
                + base64.b64encode(prepared["data"]).decode("ascii"),
                "mime_type": prepared["mime_type"],
                "width": model_metadata["derived_size"]["width"],
                "height": model_metadata["derived_size"]["height"],
            }
        )
        records.append(record)
    return images, records


async def evaluate(args):
    api_key = os.getenv(args.api_key_env, "")
    sessdata, csrf = os.getenv(args.bili_sessdata_env, ""), os.getenv(args.bili_csrf_env, "")
    redact = Redactor([api_key, sessdata, csrf])
    character = redact(load_character(args.character_file))
    raw_cases = args.cases.read_bytes()
    corpus = json.loads(raw_cases)
    cases = corpus["cases"] if isinstance(corpus, dict) else corpus
    for case in cases:
        case_group(case)
    image = make_png(args.image_width, args.image_height, args.red_circles, args.blue_squares)
    cases = apply_case_parameters(cases, args.red_circles, args.blue_squares)
    if args.case_id:
        unknown = set(args.case_id) - {case["id"] for case in cases}
        if unknown:
            raise EvaluationError("unknown_case", "Requested case IDs are not in this corpus")
        cases = [case for case in cases if case["id"] in args.case_id]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fixture_path = args.output.with_suffix(".fixture.png")
    fixture_path.write_bytes(image)
    image_artifacts = args.output.parent / (args.output.stem + ".images")
    prepared_fixture = prepare_model_image(
        image,
        "image/png",
        args.image_preprocessing,
        source_path=fixture_path,
        artifact_directory=image_artifacts,
        reference="fixture:shapes",
    )
    image_url = f"data:{prepared_fixture['mime_type']};base64," + base64.b64encode(
        prepared_fixture["data"]
    ).decode("ascii")
    asset_images, asset_records = load_asset_images(
        args.asset_image,
        args.image_manifest,
        image_preprocessing=args.image_preprocessing,
        artifact_directory=image_artifacts,
    )
    extra = json.loads(args.extra_body_json.read_text(encoding="utf-8-sig")) if args.extra_body_json else {}
    web = PublicWeb(
        timeout=args.network_timeout_seconds, maximum=args.max_document_bytes, proxy=args.network_proxy
    )
    search = Search(web, args.search_provider, args.search_endpoint)
    bilibili = BrowsingAdapter({"sessdata": sessdata, "csrf": csrf})
    previous_bilibili_client = bilibili.client
    bilibili.client = httpx.AsyncClient(
        base_url=str(previous_bilibili_client.base_url),
        headers=dict(previous_bilibili_client.headers),
        cookies=previous_bilibili_client.cookies,
        timeout=args.network_timeout_seconds,
        proxy=args.network_proxy,
        trust_env=False,
        follow_redirects=False,
    )
    await previous_bilibili_client.aclose()
    tools = ReadTools(web, search, bilibili=bilibili, redactor=redact)
    model = Model(
        args.base_url,
        args.model,
        key=api_key or None,
        thinking=args.thinking,
        max_tokens=args.max_output_tokens,
        temperature=args.temperature,
        seed=args.seed,
        timeout=args.timeout_seconds,
        tools_mode=args.tools_mode,
        response_format=args.response_format,
        extra_body=extra,
        tool_reasoning_history=args.tool_reasoning_history,
    )
    model.redact = redact
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_VERSION,
        "evaluation_profile": "oc_separate_paths_v1",
        "declarations": {
            "model": args.model,
            "quantization": args.quantization,
            "context_tokens": args.context_tokens,
            "thinking": args.thinking,
            "tools_mode": args.tools_mode,
            "tool_reasoning_history": args.tool_reasoning_history,
            "image_order": args.image_order,
            "image_preprocessing": args.image_preprocessing,
            "format_repair": args.format_repair,
            "max_output_tokens_including_reasoning": args.max_output_tokens,
            "temperature": args.temperature,
            "seed": args.seed,
            "additional_image_sampling_parameters": extra,
        },
        "execution": {
            "bvid": args.bvid,
            "host": socket.gethostname(),
            "model_endpoint": args.base_url,
            "network_proxy": args.network_proxy,
            "search_provider": args.search_provider,
            "search_endpoint": args.search_endpoint,
            "max_tool_rounds": args.max_tool_rounds,
            "max_tool_calls_per_case": args.max_tool_calls,
            "max_model_calls_per_case": 24,
            "model_timeout_seconds": args.timeout_seconds,
            "case_deadline_seconds": args.case_timeout_seconds,
            "network_timeout_seconds": args.network_timeout_seconds,
            "max_document_bytes": args.max_document_bytes,
        },
        "materials": {
            "harness_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "image_preprocessor_source_sha256": hashlib.sha256(
                Path(vlm_images.__file__).read_bytes()
            ).hexdigest(),
            "tool_task_system_prompt_sha256": hashlib.sha256(
                system_prompt(character, args.tools_mode).encode()
            ).hexdigest(),
            "oc_system_prompt_sha256_by_case": {
                case["id"]: hashlib.sha256(
                    oc_system_prompt(character, case.get("conversation_context", {})).encode()
                ).hexdigest()
                for case in cases
                if case.get("task_kind") == "oc_conversation"
            },
            "image_manifest_file": str(args.image_manifest) if args.image_manifest else None,
            "optional_asset_images": asset_records,
            "cases_sha256": hashlib.sha256(raw_cases).hexdigest(),
            "character_public_sha256": hashlib.sha256(
                json.dumps(character, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "image": {
                "path": str(fixture_path),
                "sha256": hashlib.sha256(image).hexdigest(),
                "width": args.image_width,
                "height": args.image_height,
                "red_circles": args.red_circles,
                "blue_squares": args.blue_squares,
                "model_input": prepared_fixture["metadata"],
            },
        },
        "cases": [],
        "context_configuration_verified": False,
        "context_probe": {
            "requested": bool(args.context_probe_tokens),
            "tokenizer_attempted": False,
            "generation_attempted": False,
        },
        "scope": {
            "production_configuration_modified": False,
            "platform_write_tools_available": False,
            "reasoning_text_retained": False,
            "full_semantic_acceptance_claimed": False,
            "history_policy": "Separate public/private histories; public evidence may transfer into private replies; private bodies never return to public history",
            "score_interpretation": "Core functions and safety diagnostics are separate groups. Raw replies are reviewed for factual accuracy, task completion and OC interaction. Delivery, routing, tool outcomes and literal search aids are reported separately; unreviewed semantics are pending/null. No aggregate capability score is computed.",
            "oc_path_policy": "Plain OC uses natural user text, plain dialogue and no offered tools or response-format schema. Channel/source fields are backend scene bindings, not model choices. Tool selection and model evidence selection are not assessed on this path; evidence/tool tasks retain their separate transport.",
            "quantization_provenance": "Operator declaration plus observed server properties; no remote tensor inspection",
            "thinking_provenance": "Requested template parameter plus actual returned reasoning counters; no assumption that every backend supports thinking",
            "format_repair_policy": "Tool-task path only: optional paired sidecar, preserving baseline output/checks/history. Exact original reply text is required; generated channel/citations/refs do not establish baseline decisions. One retry within unchanged call/output/deadline limits. Plain OC never uses format repair.",
        },
    }

    def save():
        report["group_summary"] = grouped_summary(report["cases"])
        args.output.write_text(
            json.dumps(redact(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    try:
        if args.preflight_only:
            await tools.execute("web_search", {"query": "W3C PNG specification lossless"})
            await tools.execute("web_fetch", {"url": "https://www.w3.org/TR/png-3/"})
            if args.bvid:
                await tools.execute("bilibili_read", {"bvid": args.bvid})
            report["tool_preflight"] = {
                "source": "harness_only_not_model_tool_selection",
                "model_requests_made": 0,
                "receipts": tools.receipts,
            }
            save()
            return report
        report["observed_runtime"] = await model.metadata()
        observed = report["observed_runtime"].get("observed_context_tokens")
        report["context_configuration_verified"] = observed == args.context_tokens
        runtime_error = (
            "runtime_model_alias_mismatch"
            if report["observed_runtime"].get("model_alias_matches") is False
            else "runtime_context_mismatch"
            if type(observed) is int and observed != args.context_tokens
            else None
        )
        report["runtime_configuration_error"] = runtime_error
        save()
        for case in cases:
            if runtime_error:
                report["cases"].append(
                    {
                        "id": case["id"],
                        "evaluation_group": case_group(case),
                        "task_kind": case.get("task_kind", "evidence_task"),
                        "execution_path": "plain_oc"
                        if case.get("task_kind") == "oc_conversation"
                        else "tool_task",
                        "blocked": runtime_error,
                        "turns": [],
                        "requires_human_fact_review": True,
                    }
                )
                save()
                continue
            receipt_start = len(tools.receipts)
            try:
                result = await run_case(
                    case,
                    model,
                    tools,
                    character=character,
                    image_url=image_url,
                    bvid=args.bvid,
                    max_tool_rounds=args.max_tool_rounds,
                    max_tool_calls=args.max_tool_calls,
                    timeout=args.case_timeout_seconds,
                    asset_images=asset_images,
                    format_repair=args.format_repair,
                    image_order=args.image_order,
                )
            except Exception as error:
                result = {
                    "id": case["id"],
                    "evaluation_group": case_group(case),
                    "task_kind": case.get("task_kind", "evidence_task"),
                    "execution_path": "plain_oc"
                    if case.get("task_kind") == "oc_conversation"
                    else "tool_task",
                    "error": error_record(error),
                    "tool_receipts": tools.receipts[receipt_start:],
                    "requires_human_fact_review": True,
                }
            report["cases"].append(result)
            save()
        if args.context_probe_tokens and not runtime_error:
            report["context_probe"] = await context_probe(
                model,
                args.context_probe_tokens,
                args.context_probe_output_tokens,
                args.context_tokens,
                observed,
                timeout=args.context_probe_timeout_seconds,
            )
            save()
    finally:
        await model.close()
        await bilibili.close()
    return report


def main(argv=None):
    args = arguments(argv)
    try:
        report = asyncio.run(evaluate(args))
    except Exception as error:
        print(json.dumps({"error": error_record(error)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "output": str(args.output),
                "case_count": len(report["cases"]),
                "context_configuration_verified": report["context_configuration_verified"],
                "semantic_acceptance": "requires_human_review",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
