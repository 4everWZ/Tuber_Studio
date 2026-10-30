"""Music and scene contracts use real PCM files and the actual live worker."""

import array
import asyncio
import hashlib
import io
import math
import wave

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.live_media import default_scene, inspect_wav
from tuber_studio.models import Event


def pcm(seconds=0.4, silence=False):
    target = io.BytesIO()
    samples = array.array(
        "h", [0 if silence else int(6000 * math.sin(i * 0.1)) for i in range(int(16000 * seconds))]
    )
    with wave.open(target, "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(samples.tobytes())
    return target.getvalue()


async def add_song(runtime, **changes):
    data = pcm()
    identity = hashlib.sha256(data).hexdigest()
    (runtime.media.directory / (identity + ".wav")).write_bytes(data)
    return await runtime.media.save_song(
        {
            "id": "test-song",
            "title": "测试曲目",
            "audio": identity,
            "live_allowed": True,
            "license_note": "程序生成的测试波形",
            **changes,
        }
    )


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


class Player:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


async def test_song_is_real_playback_and_never_enters_dialogue_memory(runtime, store):
    song = await add_song(runtime)
    player = Player()
    owner = await runtime.attach_overlay(player)
    await runtime.control("start")
    result = await runtime.submit(Event(type="song", text=song["title"]))
    await wait_for(lambda: any(m["type"] == "play" for m in player.messages))
    message = next(m for m in player.messages if m["type"] == "play")
    assert message["audio_ref"] == "/media/music/" + song["audio"]
    assert message["media"]["mode"] == "direct"
    ack = {"job_id": result["job_id"], "playback_token": message["playback_token"]}
    assert not await runtime.acknowledge(owner, ack | {"type": "ended"})
    assert await runtime.acknowledge(owner, ack | {"type": "started"})
    assert await runtime.acknowledge(
        owner, ack | {"type": "progress", "position": 0.1, "paused": False, "mouth": 0.05}
    )
    assert runtime.playback["position"] == 0.1
    await runtime.control("media-pause")
    assert player.messages[-1]["type"] == "media-control"
    assert player.messages[-1]["paused"] is True
    assert await runtime.acknowledge(
        owner, ack | {"type": "progress", "position": 0.1, "paused": True, "mouth": 0}
    )
    assert await runtime.acknowledge(owner, ack | {"type": "ended"})
    assert store.job(result["job_id"])["status"] == "completed"
    assert not store.all("SELECT * FROM conversations")
    assert not store.all("SELECT * FROM context_windows")


async def test_song_snapshot_dedup_permissions_and_unsupported_singing(runtime):
    await add_song(runtime)
    await runtime.control("start")
    await runtime.control("pause")
    first = await runtime.submit(Event(type="song", text="测试曲目"))
    assert first["status"] == "queued"
    duplicate = await runtime.submit(Event(type="song", text="测试曲目"))
    assert duplicate["status"] == "filtered" and "排队" in duplicate["reason"]
    runtime.media.store.set("music_settings", {"default_mode": "sing"})
    snapshot = runtime.store.job(first["job_id"])["media"]
    assert '"mode": "direct"' in snapshot
    await runtime.control("clear")
    unsupported = await runtime.submit(Event(type="song", text="测试曲目"))
    assert unsupported["status"] == "filtered" and "模型" in unsupported["reason"]
    await add_song(runtime, live_allowed=False)
    refused = await runtime.submit(Event(type="song", text="测试曲目", metadata={"mode": "direct"}))
    assert refused["status"] == "filtered" and "许可" in refused["reason"]


async def test_vocal_activity_has_silence_and_media_integrity_is_checked(runtime):
    song = await add_song(runtime)
    path = runtime.media.blob(song["audio"])
    activity = inspect_wav(path, envelope=True)
    assert len(activity["envelope"]) == 8 and max(activity["envelope"]) > 0.01
    path.write_bytes(pcm(silence=True))
    with pytest.raises(ValueError, match="内容已改变"):
        runtime.media.prepare({"song": song})
    with pytest.raises(ValueError, match="静音"):
        inspect_wav(path)


@pytest.mark.parametrize("mode", [[], {}])
async def test_untrusted_song_mode_is_filtered_without_crashing_worker(runtime, mode):
    await add_song(runtime)
    await runtime.control("start")
    result = await runtime.submit(Event(type="song", text="测试曲目", metadata={"mode": mode}))
    assert result["status"] == "filtered"
    assert result["reason"] == "未知点歌模式"


def test_scene_roundtrip_and_bounds(runtime):
    scene = default_scene()
    scene["items"][0]["x"] += 5
    assert runtime.media.save_scene(scene) == scene
    assert runtime.media.scene() == scene
    scene["items"].append(scene["items"][0])
    with pytest.raises(ValueError, match="重复"):
        runtime.media.save_scene(scene)
    scene = default_scene()
    scene["items"][0]["x"] = 1800
    with pytest.raises(ValueError, match="画布"):
        runtime.media.save_scene(scene)


def test_live_api_scopes_streaming_upload_and_scene_persistence(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        assert client.get("/api/live/view").status_code == 401
        assert client.post("/api/live/uploads", content=pcm()).status_code == 401
        client.post("/api/overlay/auth", json={"token": settings.overlay_token})
        assert client.get("/api/live/view").status_code == 200
        assert client.put("/api/live/scene", json=default_scene()).status_code == 401
        assert client.get("/api/live/music").status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        uploaded = client.post("/api/live/uploads", content=pcm()).json()
        assert uploaded["id"] == hashlib.sha256(pcm()).hexdigest()
        assert client.get("/media/music/" + uploaded["id"]).content == pcm()
        assert client.post("/api/live/uploads", content=b"not a wave").status_code == 400
        assert not list(app.state.runtime.media.directory.glob(".upload-*"))
        scene = default_scene()
        scene["title"] = "保存后的标题"
        assert client.put("/api/live/scene", json=scene).status_code == 200
        assert client.get("/api/live/view").json()["scene"]["title"] == "保存后的标题"
        assert (
            client.put("/api/live/scene", json=scene, headers={"Origin": "https://other.example"}).status_code
            == 403
        )
        assert client.put("/api/live/queue", json=["missing"]).status_code == 400


@pytest.mark.parametrize("action", ["mute", "skip", "stop"])
async def test_song_cancellation_stops_player_and_rejects_late_completion(runtime, action):
    await add_song(runtime)
    player = Player()
    owner = await runtime.attach_overlay(player)
    await runtime.control("start")
    result = await runtime.submit(Event(type="song", text="测试曲目"))
    await wait_for(lambda: any(m["type"] == "play" for m in player.messages))
    message = next(m for m in player.messages if m["type"] == "play")
    ack = {"job_id": result["job_id"], "playback_token": message["playback_token"]}
    assert await runtime.acknowledge(owner, ack | {"type": "started"})
    await runtime.control(action)
    assert not await runtime.acknowledge(owner, ack | {"type": "ended"})
    assert runtime.store.job(result["job_id"])["status"] == "cancelled"
    assert runtime.playback is None
    assert player.messages[-1]["type"] == "stop"
