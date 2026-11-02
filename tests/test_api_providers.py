import json
import wave
from io import BytesIO

import httpx
import pytest

from tuber_studio.api_providers import APIProvider, VisionService, create_llm
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.compaction import ContextWindowExceeded, IncompleteCompaction
from tuber_studio.generation_meter import GenerationMeter
from tuber_studio.providers import VoiceService


@pytest.fixture
def transport(monkeypatch):
    real = httpx.AsyncClient
    captured = []
    state = {"finish": "stop", "count": 120, "silent": False}

    def respond(request):
        captured.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "remote-model"}]})
        body = json.loads(request.content)
        if request.url.path.endswith("/audio/speech"):
            stream = BytesIO()
            with wave.open(stream, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(32000)
                wav.writeframes((b"\0\0" if state["silent"] else b"\x01\0") * 3200)
            return httpx.Response(200, content=stream.getvalue())
        probe = body["max_tokens"] == 1
        reply = json.dumps(
            {
                "spoken_text": "海底的小灯亮起来啦，先喝口热茶，我们再慢慢聊。",
                "emotion": "cheerful",
                "animation_id": "talk_happy",
                "reply_to": [],
            },
            ensure_ascii=False,
        )
        reply = state.get("text", reply)
        usage = {"prompt_tokens": state["count"] if probe else state.get("actual_count", state["count"])}
        if "completion" in state:
            usage["completion_tokens"] = 1 if probe else state["completion"]
        return httpx.Response(
            200,
            json={
                "usage": usage,
                "choices": [
                    {
                        "finish_reason": "length" if probe else state["finish"],
                        "message": {"content": None if probe else reply}
                        | ({} if probe else state.get("extra", {})),
                    }
                ],
            },
        )

    def client(**options):
        options.setdefault("transport", httpx.MockTransport(respond))
        return real(**options)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return captured, state


@pytest.mark.asyncio
async def test_external_provider_uses_bearer_and_real_usage_without_broker_headers(
    settings, store, transport
):
    captured, _ = transport
    settings.data["llm"].update(
        kind="openai_compatible", base_url="https://api.test/v1", model="remote-model", context_tokens=8192
    )
    settings.inference_token = "internal-must-not-leak"
    settings.provider_keys["llm"] = "external-test-key"
    provider = create_llm(settings)
    messages = [{"role": "system", "content": "只返回JSON"}, {"role": "user", "content": "你好"}]
    try:
        assert (await provider.health())["capacity_source"] == "operator_declared"
        assert await provider.count_tokens(messages) == 120
        assert await provider.count_tokens(messages) == 120
        assert await provider.prefill(messages)
        reply = await provider.generate(messages, store.character(), "current")
        assert reply.emotion == "cheerful"
        posts = [r for r in captured if r.method == "POST"]
        assert len(posts) == 2
        assert json.loads(posts[0].content)["max_tokens"] == 1
        assert json.loads(posts[1].content)["response_format"] == {"type": "json_object"}
        for request in captured:
            assert request.url.path.startswith("/v1/")
            assert request.headers["authorization"] == "Bearer external-test-key"
            assert "x-tuber-inference-key" not in request.headers
            assert b"internal-must-not-leak" not in request.content
    finally:
        await provider.close()


@pytest.mark.parametrize("adapter", ["openai_compatible", "llama_cpp"])
async def test_structured_schema_is_sent_to_llama_cpp_without_changing_generic_apis(
    settings, transport, adapter
):
    captured, _ = transport
    provider = APIProvider(
        settings.data["llm"] | {"base_url": "https://api.test/v1", "model": "remote-model"},
        adapter=adapter,
    )
    schema = {
        "type": "object",
        "properties": {"spoken_text": {"type": "string"}},
        "required": ["spoken_text"],
    }
    try:
        result = await provider.generate_json(
            [{"role": "user", "content": "鱼鱼，接着聊吧"}], schema, max_tokens=512
        )
        assert isinstance(result["spoken_text"], str)
    finally:
        await provider.close()
    posts = [json.loads(request.content) for request in captured if request.method == "POST"]
    assert "response_format" not in posts[0]
    expected = {"type": "json_object"}
    if adapter == "llama_cpp":
        expected["schema"] = schema
    assert posts[-1]["response_format"] == expected


@pytest.mark.parametrize("adapter", ["openai_compatible", "llama_cpp"])
@pytest.mark.parametrize("thinking", [None, True, False])
async def test_thinking_override_is_explicit_and_only_sent_by_llama_adapter(
    settings, store, transport, adapter, thinking
):
    captured, _ = transport
    config = settings.data["llm"] | {
        "base_url": "https://api.test/v1",
        "model": "remote-model",
        "enable_thinking": thinking,
    }
    provider = APIProvider(config, adapter=adapter)
    messages = [{"role": "user", "content": "聊聊海底的小灯吧"}]
    try:
        await provider.count_tokens(messages)
        await provider.generate(messages, store.character(), "current")
    finally:
        await provider.close()
    posts = [json.loads(request.content) for request in captured if request.method == "POST"]
    assert len(posts) == 2 and posts[0]["max_tokens"] == 1
    for body in posts:
        if adapter == "llama_cpp" and thinking is not None:
            assert body["chat_template_kwargs"] == {"enable_thinking": thinking}
        else:
            assert "chat_template_kwargs" not in body


@pytest.mark.parametrize("adapter", ["openai_compatible", "llama_cpp"])
async def test_explicit_llama_sampling_is_forwarded_without_polluting_generic_apis(
    settings, store, transport, adapter
):
    captured, _ = transport
    sampling = {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.0,
        "seed": 42,
        "reasoning_budget_tokens": 1024,
    }
    config = (
        settings.data["llm"]
        | sampling
        | {
            "base_url": "https://api.test/v1",
            "model": "remote-model",
            "enable_thinking": True,
        }
    )
    provider = APIProvider(config, adapter=adapter)
    try:
        await provider.generate([{"role": "user", "content": "海底的小灯"}], store.character(), "current")
    finally:
        await provider.close()
    posts = [json.loads(request.content) for request in captured if request.method == "POST"]
    assert len(posts) == 2 and posts[0]["max_tokens"] == 1
    for body in posts:
        assert "tools" not in body
        if adapter == "llama_cpp":
            for key in sampling.keys() - {"temperature"}:
                assert body[key] == sampling[key]
            assert body["chat_template_kwargs"] == {"enable_thinking": True}
            assert "reasoning_budget_tokens" not in body["chat_template_kwargs"]
        else:
            assert not (sampling.keys() - {"temperature"}) & body.keys()
            assert "chat_template_kwargs" not in body
    assert posts[-1]["temperature"] == 1.0
    assert "temperature" not in posts[0]


async def test_invalid_explicit_llama_sampling_is_rejected_before_http(settings, transport):
    captured, _ = transport
    with pytest.raises(ValueError):
        APIProvider(settings.data["llm"] | {"seed": True}, adapter="llama_cpp")
    assert not captured


async def test_public_text_preserves_natural_reply_normal_sampling_and_real_usage(
    settings, runtime, transport
):
    captured, state = transport
    text = "哼，红杯还在桌上。\n小女孩在看另一只手操作。"
    state.update(text=text, completion=12, extra={"reasoning_content": "private-reasoning-sentinel"})
    provider = APIProvider(
        settings.data["llm"] | {"enable_thinking": True, "reasoning_budget_tokens": 1024},
        adapter="llama_cpp",
    )
    row, _ = runtime.browsing_repository.create_round(
        "text-test", runtime.character, BrowsingPolicy().model_dump(), 0, True
    )
    messages = [{"role": "user", "content": "观察画面"}]
    try:
        with GenerationMeter(runtime.browsing_repository, row["id"]):
            actual = await provider.generate_text(messages, max_tokens=128)
        assert actual == text and "private-reasoning" not in actual
        assert provider.last_usage == {"prompt_tokens": 120, "completion_tokens": 12}
        assert runtime.browsing_repository.round(row["id"])["budget"] == {
            "reads": 0,
            "model_calls": 2,
            "input_tokens": 240,
            "output_tokens": 13,
            "top_levels": 0,
        }
        posts = [json.loads(request.content) for request in captured]
        assert [post["max_tokens"] for post in posts] == [1, 128]
        assert posts[-1]["messages"] == messages
        assert posts[-1]["temperature"] == settings.data["llm"]["temperature"]
        for post in posts:
            assert "response_format" not in post and "tools" not in post
            assert post["reasoning_budget_tokens"] == 1024
            assert post["chat_template_kwargs"] == {"enable_thinking": True}
    finally:
        await provider.close()


@pytest.mark.parametrize("budget", [True, 0, -1, "128"])
async def test_public_text_invalid_budget_never_reaches_http(settings, transport, budget):
    captured, _ = transport
    provider = APIProvider(settings.data["llm"])
    try:
        with pytest.raises(ValueError):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=budget)
        assert not captured
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"finish": "length"},
        {"text": ""},
        {"text": "  "},
        {"extra": {"tool_calls": [{"id": "external"}]}},
        {"extra": {"function_call": {"name": "external"}}},
    ],
)
async def test_public_text_incomplete_empty_or_tool_output_is_rejected(settings, transport, patch):
    _, state = transport
    state.update(patch)
    provider = APIProvider(settings.data["llm"])
    try:
        with pytest.raises(ValueError):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=128)
    finally:
        await provider.close()


@pytest.mark.parametrize("counts,requests", [({"count": 3800}, 1), ({"actual_count": 3800}, 2)])
async def test_public_text_checks_estimated_and_actual_context(settings, transport, counts, requests):
    captured, state = transport
    state.update(counts)
    provider = APIProvider(settings.data["llm"] | {"context_tokens": 4096})
    try:
        with pytest.raises(ContextWindowExceeded):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=128)
        assert len(captured) == requests
    finally:
        await provider.close()


async def test_public_text_parent_budget_denies_completion_before_request(settings, runtime, transport):
    captured, state = transport
    state["completion"] = 12
    row, _ = runtime.browsing_repository.create_round(
        "text-test", runtime.character, BrowsingPolicy(max_model_calls=1).model_dump(), 0, True
    )
    provider = APIProvider(settings.data["llm"])
    try:
        with GenerationMeter(runtime.browsing_repository, row["id"]), pytest.raises(ValueError, match="预算"):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=128)
        assert len(captured) == 1 and json.loads(captured[0].content)["max_tokens"] == 1
        assert runtime.browsing_repository.round(row["id"])["budget"]["model_calls"] == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_incomplete_external_summary_and_missing_usage_are_rejected(settings, transport):
    _, state = transport
    config = settings.data["llm"] | {"base_url": "https://api.test/v1", "model": "remote-model"}
    provider = APIProvider(config)
    try:
        state["finish"] = "length"
        with pytest.raises(IncompleteCompaction):
            await provider.summarize([{"role": "user", "content": "请总结"}])
        state["count"] = None
        with pytest.raises(ValueError, match="prompt_tokens"):
            await provider.count_tokens([{"role": "user", "content": "新输入"}])
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_api_speech_is_wav_cached_and_scoped_by_actual_api_voice(settings, transport):
    captured, state = transport
    settings.data["tts"].update(
        kind="openai_compatible", base_url="https://api.test/v1", model="remote-model", voice="voice-a"
    )
    settings.provider_keys["tts"] = "tts-test-key"
    service = VoiceService(settings)
    try:
        voice = settings.voice("external_api")
        first = await service.synthesize("这是一段接口测试。", voice)
        assert first.duration == 0.1
        assert (await service.synthesize("这是一段接口测试。", voice)).path == first.path
        assert len(captured) == 1
        assert json.loads(captured[0].content)["response_format"] == "wav"
        assert "x-tuber-inference-key" not in captured[0].headers
        settings.data["tts"]["voice"] = "voice-b"
        assert (await service.synthesize("这是一段接口测试。", voice)).path != first.path
        state["silent"] = True
        with pytest.raises(ValueError, match="静音"):
            await service.synthesize("新的静音错误测试。", voice)
        assert not list(settings.audio.glob("*.part"))
        with pytest.raises(ValueError, match="本地模型"):
            await service.synthesize("不能冒充原角色声线。", settings.voice("whale_sovits_v2"))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reserved_vision_accepts_only_inline_images_and_is_disabled_by_default(transport):
    service = VisionService({"kind": "disabled"})
    with pytest.raises(ValueError, match="仅支持文本"):
        await service.describe("图片是什么", b"x", "image/png")
    service = VisionService(
        {
            "kind": "openai_compatible",
            "base_url": "https://api.test/v1",
            "model": "remote-model",
            "location": "local",
        }
    )
    try:
        with pytest.raises(ValueError, match="MIME"):
            await service.describe("看图", b"file:///etc/passwd", "image/png")
        await service.describe("看图", b"\x89PNG\r\n\x1a\nimage", "image/png")
        request = transport[0][-1]
        content = json.loads(request.content)["messages"][0]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "x-tuber-inference-key" not in request.headers
    finally:
        await service.close()
