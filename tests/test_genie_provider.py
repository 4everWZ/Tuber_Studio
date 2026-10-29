import io
import wave

import httpx
import pytest

from tuber_studio.providers import VoiceService


def speech_wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(32000)
        audio.writeframes(b"\x03\x00" * 3200)
    return stream.getvalue()


async def test_genie_cache_binds_ready_bundle_and_cannot_bypass_changed_assets(settings):
    settings.data["tts"].update(kind="gpt_sovits", base_url="http://genie", local_backend="genie_onnx_int8")
    service = VoiceService(settings)
    await service.client.aclose()
    calls, state = [], {"identity": "a" * 64, "ready": True}

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/docs":
            return httpx.Response(
                200 if state["ready"] else 503,
                json={
                    "status": "ready" if state["ready"] else "unavailable",
                    "backend": "genie_onnx_int8",
                    "voice_id": "whale_sovits_v2",
                    "bundle_identity": state["identity"],
                },
            )
        return httpx.Response(200, content=speech_wav())

    service.client = httpx.AsyncClient(base_url="http://genie", transport=httpx.MockTransport(respond))
    try:
        voice = settings.voice("whale_sovits_v2")
        first = await service.synthesize("海底的小灯亮起来了。", voice)
        assert (await service.synthesize("海底的小灯亮起来了。", voice)).path == first.path
        assert calls.count(("POST", "/tts")) == 1
        state["identity"] = "b" * 64
        assert (await service.synthesize("海底的小灯亮起来了。", voice)).path != first.path
        assert calls.count(("POST", "/tts")) == 2
        state["ready"] = False
        with pytest.raises(httpx.HTTPStatusError):
            await service.synthesize("海底的小灯亮起来了。", voice)
        assert calls.count(("POST", "/tts")) == 2
    finally:
        await service.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"backend": "gpt_sovits_v2_compact"},
        {"voice_id": "other_oc"},
        {"bundle_identity": "unverified"},
    ],
)
async def test_genie_rejects_other_backend_voice_or_unverified_bundle_before_speech(settings, changed):
    settings.data["tts"].update(kind="gpt_sovits", base_url="http://genie", local_backend="genie_onnx_int8")
    service = VoiceService(settings)
    await service.client.aclose()
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "backend": "genie_onnx_int8",
                "voice_id": "whale_sovits_v2",
                "bundle_identity": "a" * 64,
                **changed,
            },
        )

    service.client = httpx.AsyncClient(base_url="http://genie", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError, match="身份未就绪"):
            await service.synthesize("海底的小灯亮起来了。", settings.voice("whale_sovits_v2"))
        assert calls == ["GET"]
        assert not list(settings.audio.glob("*.wav"))
    finally:
        await service.close()


def test_unknown_local_speech_engine_is_not_a_command_or_module_setting(settings):
    settings.data["tts"]["local_backend"] = "../arbitrary.py"
    with pytest.raises(ValueError, match="固定本地语音引擎"):
        settings._validate()
