import json

import httpx
import pytest

from tuber_studio.http_api import InferenceTransport, inference_transport
from tuber_studio.providers import LlamaCppProvider, reuse_turn_token_counts, validate_reply


@pytest.mark.parametrize(
    "method,path,failures,expected_calls",
    [
        ("GET", "/llm/props", [httpx.ReadError, 502], 3),
        ("POST", "/tokenize", [httpx.RemoteProtocolError], 2),
        ("POST", "/v1/chat/completions", [httpx.ConnectError, httpx.ConnectTimeout], 3),
        ("POST", "/v1/chat/completions", [httpx.ReadError], 1),
        ("POST", "/mode", [httpx.RemoteProtocolError], 1),
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
    provider = LlamaCppProvider({"base_url": "https://agx.example.ts.net"})
    chunks = []
    try:
        with pytest.raises(httpx.ReadError):
            await provider._stream_json({}, httpx.Timeout(5), chunks.append)
    finally:
        await provider.close()
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


async def test_exact_token_count_reuse_is_bound_to_the_current_wire_and_turn(settings, monkeypatch):
    provider = LlamaCppProvider(settings.data["llm"])
    calls = []

    async def tokenize(messages):
        calls.append(messages)
        return len(calls)

    monkeypatch.setattr(provider, "_token_count", tokenize)
    messages = [{"role": "user", "content": "你好吗"}]

    @reuse_turn_token_counts
    async def turn():
        count = await provider.count_tokens(messages)
        assert count == await provider.count_tokens([dict(message) for message in messages])
        assert count != await provider.count_tokens([{"role": "user", "content": "你在吗"}])

    try:
        await turn()
        await turn()
        assert len(calls) == 4
    finally:
        await provider.close()


async def test_live_thinking_budget_is_separate_from_spoken_and_ordinary_output(settings, store):
    config = settings.data["llm"] | {"kind": "llama_cpp", "max_tokens": 160, "enable_thinking": True}
    provider = LlamaCppProvider(config)
    await provider.client.aclose()
    requests = []
    build = "b1-d749821"

    def handler(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={
                "build_info": build, "default_generation_settings": {"n_ctx": config["context_tokens"]},
            })
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "compiled"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {
                "content": json.dumps({"spoken_text": "本鱼听见啦。", "reply_to": ["mock:1"]}),
                "reasoning_content": "这段只属于思考",
            },
        }]})

    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(handler))
    args = ([{"role": "user", "content": "鱼鱼好"}], store.character(), "mock:1")
    try:
        assert (await provider.generate_live(*args)).spoken_text == "本鱼听见啦。"
        await provider.generate(*args)
        config["enable_thinking"] = False
        await provider.generate_live(*args)
        config["enable_thinking"] = True
        build = "unverified"
        await provider.generate_live(*args)
    finally:
        await provider.close()
    assert [body["max_tokens"] for body in requests] == [1184, 160, 160, 160]
    assert [body.get("thinking_budget_tokens") for body in requests] == [1024, None, None, None]
    assert requests[2]["chat_template_kwargs"]["enable_thinking"] is False
    assert provider.live_thinking_budget_supported is False


@pytest.mark.parametrize(
    "properties,reason",
    [
        ({"default_generation_settings": {"n_ctx": 3072}}, "小于配置"),
        ({"default_generation_settings": {"n_ctx": 131072}, "model_alias": "other-model"}, "别名不一致"),
    ],
)
async def test_wrong_server_capacity_or_model_is_rejected_before_generation(
    settings, store, properties, reason
):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=properties)

    provider = LlamaCppProvider(settings.data["llm"])
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError, match=reason):
            await provider.generate([], store.character(), "event")
        assert requests == ["/props"]
        assert provider.profile_error
    finally:
        await provider.close()


@pytest.mark.parametrize("thinking", [False, True])
async def test_structured_decoder_constrains_reply_and_keeps_reasoning_separate(settings, store, thinking):
    settings.data["llm"]["enable_thinking"] = thinking
    settings.data["llm"].update(top_p=1.0, min_p=0.0, top_k=20, presence_penalty=2.0, repeat_penalty=1.0)

    def respond(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        data = json.loads(request.content)
        assert data["chat_template_kwargs"]["enable_thinking"] is thinking
        assert data.get("reasoning_format") == ("deepseek" if thinking else None)
        assert {
            key: data[key] for key in ("top_p", "min_p", "top_k", "presence_penalty", "repeat_penalty")
        } == {
            "top_p": 1.0,
            "min_p": 0.0,
            "top_k": 20,
            "presence_penalty": 2.0,
            "repeat_penalty": 1.0,
        }
        if "response_format" not in data:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "已生成草稿，尚待人工审核。",
                                "reasoning_content": "私有推理不得进入摘要。",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        schema = data["response_format"]["schema"]
        assert schema["properties"]["reply_to"]["items"]["enum"] == ["comment:exact"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "内部推理内容，不应播报或进入对话历史。",
                            "content": json.dumps(
                                {
                                    "spoken_text": "今天当然要来一碗白饭。",
                                    "emotion": "cheerful",
                                    "animation_id": "talk_happy",
                                    "reply_to": ["comment:exact"],
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = LlamaCppProvider(settings.data["llm"])
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    try:
        reply = await provider.generate([], store.character(), "comment:exact")
        assert reply.reply_to == ["comment:exact"]
        assert provider.last_tokens == 3
        assert await provider.summarize([]) == "已生成草稿，尚待人工审核。"
        echoed = await provider.generate(
            [], store.character(), "comment:exact", source_text=reply.spoken_text
        )
        assert echoed.spoken_text == reply.spoken_text
    finally:
        await provider.close()
