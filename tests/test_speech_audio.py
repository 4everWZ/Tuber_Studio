import io
import wave

import httpx
import pytest

from tuber_studio.providers import VoiceService


def audio_bytes(audible):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 32000, 32000, "NONE", "PCM"))
        stream.writeframes((b"\x01\x01" if audible else b"\x00\x00") * 32000)
    return output.getvalue()


async def test_dynamic_silent_http_200_is_never_installed_as_success(settings):
    settings.data["tts"]["kind"] = "gpt_sovits"
    voice = settings.voice("whale_sovits_v2")
    service = VoiceService(settings)
    await service.client.aclose()
    payload = audio_bytes(False)
    service.client = httpx.AsyncClient(
        base_url="http://tts",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=payload, headers={"content-type": "audio/wav"})
        ),
    )
    try:
        with pytest.raises(ValueError, match="全静音"):
            await service.synthesize("这次应该生成真实的声音。", voice)
        assert not list(settings.audio.iterdir())
        payload = audio_bytes(True)
        actual = await service.synthesize("这次应该生成真实的声音。", voice)
        assert actual.duration == 1 and actual.path.is_file()
    finally:
        await service.close()
