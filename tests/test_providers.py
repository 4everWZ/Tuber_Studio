import json

import httpx
import pytest

from tuber_studio.http_api import InferenceTransport, inference_transport
from tuber_studio.providers import read_chat_stream, validate_reply


@pytest.mark.parametrize(
    "method,path,failures,expected_calls",
    [
        ("GET", "/v1/models", [httpx.ReadError, 502], 3),
        ("POST", "/v1/chat/prompt_tokens", [httpx.RemoteProtocolError], 2),
        ("POST", "/v1/chat/completions", [httpx.ConnectError, httpx.ConnectTimeout], 3),
        ("POST", "/v1/chat/completions", [httpx.ReadError], 1),
        ("POST", "/images/jobs", [httpx.RemoteProtocolError], 1),
        ("POST", "/v1/chat/completions", [502], 1),
        ("GET", "/health", [401], 1),
        ("GET", "/health", [httpx.ConnectError] * 4, 4),
    ],
)
async def test_tailnet_inference_retries_before_delivery_or_for_safe_reads(
    monkeypatch, method, path, failures, expected_calls
):
    calls = []

    async def send(_transport, request):
        calls.append((request.url.path, await request.aread()))
        result = failures[len(calls) - 1] if len(calls) <= len(failures) else 200
        if isinstance(result, type):
            raise result("controlled connection failure", request=request)
        return httpx.Response(result, content=b"response")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(InferenceTransport, "retry_delays", (0, 0, 0))
    transport = inference_transport("http://100.64.0.10:9012")
    async with httpx.AsyncClient(transport=transport, base_url="http://100.64.0.10:9012") as client:
        options = {"json": {"text": "same body"}} if method == "POST" else {}
        if expected_calls <= len(failures) and isinstance(failures[-1], type):
            with pytest.raises(failures[-1]):
                await client.request(method, path, **options)
        else:
            response = await client.request(method, path, **options)
            assert response.status_code == (failures[-1] if expected_calls <= len(failures) else 200)
    assert len(calls) == expected_calls and len(set(calls)) == 1


async def test_tailnet_stream_connection_failure_does_not_replay_started_generation(monkeypatch):
    calls = 0

    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"part"}}]}\n\n'
            raise httpx.ReadError("controlled stream interruption")

    async def send(_transport, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("controlled connect error", request=request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=InterruptedBody())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(InferenceTransport, "retry_delays", (0, 0, 0))
    base = "https://gateway.example.ts.net"
    chunks = []
    async with httpx.AsyncClient(base_url=base, transport=inference_transport(base)) as client:
        with pytest.raises(httpx.ReadError):
            async with client.stream("POST", "/v1/chat/completions", json={}) as response:
                await read_chat_stream(response, chunks.append)
    assert calls == 2 and chunks == ["part"]


def test_oc_wording_and_style_length_are_flexible_but_event_binding_is_checked(store):
    role = store.character()
    for text in ("哼！", "收到", "鱼宝，\n给你留了一碗热饭。"):
        payload = json.dumps({"spoken_text": text, "reply_to": ["viewer:1"]})
        assert validate_reply(payload, role, "viewer:1", source_text=text).spoken_text == text
    with pytest.raises(ValueError, match="其他事件"):
        validate_reply(
            json.dumps({"spoken_text": "给你留了一碗热饭。", "reply_to": ["other"]}), role, "viewer:1"
        )
