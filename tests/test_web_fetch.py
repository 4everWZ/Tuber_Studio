import asyncio
import socket

import httpx

from tuber_studio import web_fetch


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


async def test_private_reply_can_read_only_the_supplied_url(monkeypatch):
    async def fail_fetch(url):
        raise AssertionError(f"Model-added URL escaped to the web: {url}")

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fail_fetch)
    tool = web_fetch.OperationWebFetch(
        lambda: None, web_fetch.supplied_https_urls("读读 https://example.org/public。")
    )
    assert await tool._read("https://example.org/public?private=conversation") == {"status": "unavailable"}
