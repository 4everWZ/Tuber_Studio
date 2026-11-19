import base64
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.providers import VoiceService


def wav_bytes(sample):
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "PCM"))
        output.writeframes(sample * 2400)
    return wav.getvalue()


@pytest.fixture
def gateway(settings, monkeypatch):
    base = "https://omni.example/v1"
    settings.data["omni"].update(base_url=base, model="shared-model")
    settings.provider_keys = {"omni": "test-omni-key"}
    calls = []
    state = {"audio": wav_bytes(b"\x01\x00")}

    def render(request):
        assert str(request.url) == base + "/audio/speech"
        assert request.headers["authorization"] == "Bearer test-omni-key"
        body = json.loads(request.content)
        assert set(body) == {"model", "input", "reference_audio", "response_format"}
        assert body["model"] == "shared-model" and body["response_format"] == "wav"
        assert body["reference_audio"]["format"] == "wav"
        calls.append((body["input"], base64.b64decode(body["reference_audio"]["data"], validate=True)))
        transcript = state.get("transcript", body["input"])
        return httpx.Response(
            state.get("status", 200), content=state["audio"],
            headers={"X-Omni-Transcript": base64.b64encode(transcript.encode()).decode()},
        )

    monkeypatch.setattr("tuber_studio.http_api.inference_transport", lambda _: httpx.MockTransport(render))
    return calls, state


async def test_live_uses_selected_reference_for_each_sentence_and_cache(settings, gateway):
    calls, _ = gateway
    yaoyao = settings.runtime / "tts/ref/yaoyao-reference32k.wav"
    yaoyao.parent.mkdir(parents=True)
    yaoyao.write_bytes(wav_bytes(b"\x02\x00"))
    whale = settings.asset_path("assets/whale/ref.wav")
    service = VoiceService(settings)
    try:
        words = "到了！请打开窗户。"
        first = await service.synthesize_live(words, "yaoyao_sovits_v2")
        assert first.duration == 0.2 and first.text == words
        assert first.path == (await service.synthesize_live(words, "yaoyao_reference")).path
        assert calls == [(part, yaoyao.read_bytes()) for part in ["到了！", "请打开窗户。"]]

        yaoyao.write_bytes(wav_bytes(b"\x03\x00"))
        assert first.path != (await service.synthesize_live(words, "yaoyao_reference")).path
        assert calls[-1][1] == yaoyao.read_bytes()

        other = await service.synthesize_live(words, "whale_sovits_v2")
        assert other.path == (await service.synthesize_live(words, "whale_reference")).path
        assert calls[-2:] == [(part, whale.read_bytes()) for part in ["到了！", "请打开窗户。"]]
        assert len(calls) == 6
        assert len(list(settings.audio.glob("*.wav"))) == 3
    finally:
        await service.close()


@pytest.mark.parametrize("failure", ["transcript", "silence", "truncated"])
async def test_failed_readback_is_not_cached_and_can_be_retried(settings, gateway, failure):
    calls, state = gateway
    good_audio = state["audio"]
    if failure == "transcript":
        state["transcript"] = "另一句。"
    elif failure == "silence":
        state["audio"] = wav_bytes(b"\x00\x00")
    else:
        state["audio"] = good_audio[:-1]
    service = VoiceService(settings)
    try:
        with pytest.raises(ValueError, match="指定台词|全静音|截断"):
            await service.synthesize_live("请开窗。", "whale_reference")
        assert not list(settings.audio.iterdir())
        state.pop("transcript", None)
        state["audio"] = good_audio
        actual = await service.synthesize_live("请开窗。", "whale_reference")
        assert len(calls) == 2 and actual.duration == 0.1
    finally:
        await service.close()


async def test_external_selection_and_sample_never_impersonate_reference(settings, gateway):
    calls, _ = gateway
    service = VoiceService(settings)
    try:
        for voice_id in ("external_api", "whale_sample_v1"):
            assert settings.voice(voice_id)["id"] == voice_id
            with pytest.raises(ValueError, match="参考录音"):
                await service.synthesize_live("这是新台词。", voice_id)
        with pytest.raises(ValueError, match="参考录音"):
            await service.preview("指定台词。", settings.voice("external_api"))
        assert calls == [] and not list(settings.audio.iterdir())
    finally:
        await service.close()


def test_preview_returns_real_sample_or_verified_omni_audio_and_redacts_errors(settings, gateway):
    calls, state = gateway
    with TestClient(create_app(settings), headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        route = "/api/tts/preview"
        sample = settings.voice("whale_sample_v1")
        for text in ("播放指定样音。", ""):
            resp = client.post(route, json={"voice_id": sample["id"], "text": text})
            assert resp.status_code == 200
            result = resp.json()
            assert result["text"] == sample["sample_text"]
            output = settings.audio / result["audio_ref"].split("/")[-1]
            assert output.read_bytes() == settings.asset_path(sample["sample_asset"]).read_bytes()
        assert not calls

        resp = client.post(route, json={"voice_id": "yaoyao_reference", "text": "瑶瑶试听。"})
        assert resp.status_code == 400 and "参考录音缺失" in resp.json()["detail"]
        assert not calls

        assert settings.data["voice_id"] == "whale_reference"
        resp = client.post(route, json={"text": "语音试听。"})
        assert resp.status_code == 200 and resp.json()["duration"] == 0.1
        assert calls == [("语音试听。", settings.asset_path("assets/whale/ref.wav").read_bytes())]
        state.update(status=401, audio=b"private-upstream-details")
        resp = client.post(route, json={"voice_id": "whale_reference", "text": "新的试听。"})
        assert resp.status_code == 502 and "HTTP 401" in resp.json()["detail"]
        assert "private-upstream-details" not in resp.text and "test-omni-key" not in resp.text
