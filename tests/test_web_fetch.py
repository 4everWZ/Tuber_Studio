import asyncio
import gzip
import re
import socket
import zlib

import httpx
import pytest
from pydantic import ValidationError

from tuber_studio import web_fetch


@pytest.mark.parametrize(("encoding", "compress"), [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_compressed_page_is_decoded_and_truncated_by_decoded_size(monkeypatch, encoding, compress):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            payload = compress(b"<html><title>Video</title><body>" + b"visible " * 20000 + b"</body></html>")
            for offset in range(0, len(payload), 32):
                yield payload[offset:offset + 32]

    client = httpx.AsyncClient

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]

    def response(request):
        assert encoding in request.headers["accept-encoding"]
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": encoding}, stream=Stream())

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(response), **kwargs))
    result = await web_fetch.fetch_excerpt("https://example.org/")
    assert result["title"] == "Video"
    assert result["excerpt"].startswith("visible visible")
    assert len(result["excerpt"]) == web_fetch.MAX_EXCERPT and result["truncated"]


def test_fetch_wire_and_backend_share_the_same_bounded_single_url_field():
    pattern = web_fetch.WEB_FETCH_SCHEMA["properties"]["web_fetch"]["pattern"]
    longest = "https://example.org/" + "x" * (web_fetch.MAX_URL - len("https://example.org/"))
    assert re.fullmatch(pattern, longest)
    assert web_fetch.WebFetchRequest(web_fetch=longest).web_fetch == longest
    runaway = "https://example.org/#" + ",https://example.org/#section=1-10" * 300
    assert not re.fullmatch(pattern, runaway)
    with pytest.raises(ValidationError):
        web_fetch.WebFetchRequest(web_fetch=runaway)
    assert not re.fullmatch(pattern, "https://example.org/ https://other.org/")
    # The deployed native parser silently drops grammars with any individual
    # finite repeat above 2000; a single {1,2040} is therefore not equivalent
    # in deployment even though Python's regex engine accepts it.
    assert all(int(high) <= 2000 for _, high in re.findall(r"\{(\d+),(\d+)\}", pattern))


async def test_public_fetch_pins_address_and_rejects_private_redirect(monkeypatch):
    requests = []
    client = httpx.AsyncClient

    async def resolve(host, port, **kwargs):
        address = "100.95.31.18" if host == "private.example.org" else "93.184.215.14"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def response(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://private.example.org/"})

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(
        web_fetch.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(response), **kwargs),
    )
    tool = web_fetch.OperationWebFetch(lambda: None)
    assert await tool._read("https://example.org/") == {"status": "unavailable"}
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.215.14"
    assert requests[0].headers["host"] == requests[0].extensions["sni_hostname"] == "example.org"


async def test_web_timeout_still_delivers_a_final_model_turn(monkeypatch):
    requests = []
    schema = {"type": "object"}

    async def slow_dns(*args, **kwargs):
        await asyncio.sleep(5)

    async def invoke(messages, wire_schema):
        requests.append(messages)
        if len(requests) == 1:
            assert wire_schema["oneOf"][-1]["required"] == ["web_fetch"]
            return {"web_fetch": "https://example.org"}
        assert wire_schema == schema
        assert any("本轮未读到网页正文。" in message["content"] for message in messages)
        return {"reply": "鱼鱼这次没读到网页。"}

    monkeypatch.setattr(web_fetch, "FETCH_TIMEOUT", 0.05)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", slow_dns)
    async with asyncio.timeout(2):
        result = await web_fetch.OperationWebFetch(lambda: None).complete([], schema, invoke)
    assert result["reply"] == "鱼鱼这次没读到网页。" and len(requests) == 2


async def test_model_selected_public_page_does_not_require_a_user_supplied_url(monkeypatch):
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return {"url": url, "excerpt": "官方公开说明"}

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    result = await web_fetch.OperationWebFetch(lambda: None)._read("https://example.org/docs")
    assert result["status"] == "ok" and result["excerpt"] == "官方公开说明"
    assert fetched == ["https://example.org/docs"]
