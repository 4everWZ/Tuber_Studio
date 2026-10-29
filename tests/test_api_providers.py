import json
import wave
from io import BytesIO

import httpx
import pytest

from tuber_studio.api_providers import APIProvider, VisionService, create_llm
from tuber_studio.compaction import IncompleteCompaction
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
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": state["count"]},
                "choices": [
                    {
                        "finish_reason": "length" if probe else state["finish"],
                        "message": {"content": None if probe else reply},
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
