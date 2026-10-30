"""Future-model boundary tests; the fixture is a PCM producer, not a singing model."""

import array
import asyncio
import hashlib
import shutil
import wave

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.live_media import LiveMedia
from tuber_studio.models import Event
from tuber_studio.singing import SingingResult


class FixtureProvider:
    id = "test-pcm-producer"
    version = "v1"
    timeout_seconds = 1

    def __init__(self):
        self.calls = 0
        self.request = None
        self.closed = False
        self.wrong_voice = False
        self.delay = 0
        self.cancelled = False

    async def prepare(self, request):
        self.calls += 1
        self.request = request
        try:
            await asyncio.sleep(self.delay)
            output = request.output_directory / "prepared.wav"
            shutil.copyfile(request.audio, output)
            return SingingResult(
                output, "other" if self.wrong_voice else request.voice["id"], self.id, self.version, output
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        self.closed = True


async def prepared_case(settings, store):
    provider = FixtureProvider()
    media = LiveMedia(settings, store, singing_provider=provider)
    source = media.directory / "fixture.wav"
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(array.array("h", [1000, -1000] * 1600).tobytes())
    blob = hashlib.sha256(source.read_bytes()).hexdigest()
    source.rename(media.directory / (blob + ".wav"))
    await media.save_song(
        {
            "id": "fixture",
            "title": "接口测试",
            "audio": blob,
            "live_allowed": True,
            "license_note": "自有测试波形",
        }
    )
    character = store.character()
    snapshot = media.request(Event(type="song", text="接口测试", metadata={"mode": "sing"}), character)
    return media, provider, snapshot, character


async def test_mounted_provider_receives_validated_inputs_and_reuses_own_cache(settings, store):
    media, provider, snapshot, character = await prepared_case(settings, store)
    audio, activity = await media.singing.prepare(snapshot, character)
    cached, cached_activity = await media.singing.prepare(snapshot, character)
    assert audio.path == cached.path and activity == cached_activity
    assert provider.calls == 1
    assert provider.request.voice["id"] == character["voice_profile"]
    assert provider.request.audio.is_file() and provider.request.reference is None
    assert audio.path.parent == media.directory
    provider.version = "v2"
    with pytest.raises(ValueError, match="身份已改变"):
        await media.singing.prepare(snapshot, character)
    await media.singing.close()
    assert provider.closed


async def test_wrong_voice_output_is_not_published(settings, store):
    media, provider, snapshot, character = await prepared_case(settings, store)
    provider.wrong_voice = True
    with pytest.raises(ValueError, match="声音或模型身份不匹配"):
        await media.singing.prepare(snapshot, character)
    assert not list((media.singing.root / "cache").glob("*.json"))


async def test_model_preparation_timeout_cancels_provider(settings, store):
    media, provider, snapshot, character = await prepared_case(settings, store)
    provider.delay = 10
    provider.timeout_seconds = 0.02
    with pytest.raises(ValueError, match="演唱模型准备超时"):
        await media.singing.prepare(snapshot, character)
    assert provider.cancelled
    assert not list((media.singing.root / "cache").glob("*.json"))


def test_default_app_keeps_singing_unavailable_and_rejects_singing_default(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth", json={"token": settings.admin_token})
        music = client.get("/api/live/music").json()
        assert music["settings"]["default_mode"] == "direct"
        assert music["singing"]["available"] is False
        assert "模型待接入" in music["singing"]["reason"]
        response = client.put("/api/live/music", json=music["settings"] | {"default_mode": "sing"})
        assert response.status_code == 400
        assert client.get("/api/live/music").json()["settings"]["default_mode"] == "direct"
