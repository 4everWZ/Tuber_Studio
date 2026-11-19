import base64
import io
import json
import wave

import httpx
import pytest

from tuber_studio.providers import VoiceService


def prepare(settings):
    base = "https://omni.example/v1"
    settings.data["llm"].update(kind="openai_compatible", base_url=base)
    settings.data["tts"].update(kind="omni_reference", base_url=base, model="omni-default")
    settings.provider_keys = {"llm": "ordinary-app-test-key"}


def test_omni_speech_key_is_bound_to_full_omni_route(settings):
    prepare(settings)
    assert settings.provider_key("tts") == settings.provider_key("llm")
    settings.data["tts"]["base_url"] += "/other"
    assert settings.provider_key("tts") is None


async def test_omni_renders_each_sentence_with_the_selected_reference(settings):
    prepare(settings)
    voice = settings.voice("yaoyao_sovits_v2")
    reference = settings.runtime / "tts/ref/yaoyao-reference32k.wav"
    reference.parent.mkdir(parents=True)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "PCM"))
        output.writeframes(b"\x01\x00" * 2400)
    reference.write_bytes(wav.getvalue())
    words = []
    references = []
    returned_text = None

    def render(request):
        assert request.url.path == "/v1/audio/speech"
        data = json.loads(request.content)
        assert set(data) == {"model", "input", "reference_audio", "response_format"}
        text = data["input"]
        words.append(text)
        references.append(base64.b64decode(data["reference_audio"]["data"]))
        transcript = base64.b64encode((returned_text or text).encode()).decode()
        return httpx.Response(200, content=wav.getvalue(), headers={"X-Omni-Transcript": transcript})

    service = VoiceService(settings)
    await service.client.aclose()
    service.client = httpx.AsyncClient(base_url=settings.data["tts"]["base_url"], transport=httpx.MockTransport(render))
    try:
        text = "到了！请打开窗户。"
        first = await service.synthesize(text, voice)
        assert words == service.live_parts(text, voice["id"]) == ["到了！", "请打开窗户。"]
        assert first.duration == 0.2 and first.text == text
        assert first.path == (await service.synthesize(text, voice)).path and len(words) == 2
        assert references == [reference.read_bytes()] * 2

        reference.write_bytes(wav.getvalue().replace(b"\x01\x00" * 20, b"\x02\x00" * 20))
        assert first.path != (await service.synthesize(text, voice)).path
        assert references[-1] == reference.read_bytes()

        returned_text = "好的。"
        with pytest.raises(ValueError, match="指定台词"):
            await service.synthesize("把蓝色雨伞放在窗边。", voice)
        with pytest.raises(ValueError, match="参考录音"):
            await service.synthesize_live("读新台词。", "whale_sample_v1")
        assert len(list(settings.audio.glob("*.wav"))) == 2
    finally:
        await service.close()
