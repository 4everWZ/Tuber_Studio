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
from tuber_studio.live.media import default_scene, inspect_wav
from tuber_studio.live.studio import room_feed
from tuber_studio.models import Event, Reply
from tuber_studio.providers import Audio


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
    (runtime.live.media.directory / (identity + ".wav")).write_bytes(data)
    return await runtime.live.media.save_song(
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


@pytest.mark.parametrize("cancel", [False, True])
async def test_genie_live_plays_first_clause_while_next_is_synthesizing(runtime, store, monkeypatch, cancel):
    text = "鱼鱼刚看完这一段，下一轮我们慢一点试试。"
    runtime.settings.data["tts"].update(kind="gpt_sovits", local_backend="genie_onnx_int8")
    monkeypatch.setattr(runtime.settings, "voice", lambda _: {"provider": "gpt_sovits"})
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    second_cancelled = asyncio.Event()
    calls = []

    async def reply(*_):
        return Reply(spoken_text=text), None

    async def synthesize(part, _voice):
        calls.append(part)
        if len(calls) == 2:
            second_started.set()
            try:
                await second_release.wait()
            except asyncio.CancelledError:
                second_cancelled.set()
                raise
        actual = part if len(calls) == 1 else "下一轮我们慢一点。"
        return Audio(runtime.settings.audio / f"segment-{len(calls)}.wav", 0.4, actual)

    monkeypatch.setattr(runtime, "generate_reply", reply)
    monkeypatch.setattr(runtime.voices, "synthesize_live", synthesize)
    player = Player()
    owner = await runtime.live.attach_overlay(player)
    await runtime.control("start")
    result = await runtime.submit(Event(type="chat", text="下一轮怎么打？"))
    job_id = result["job_id"]

    def plays():
        return [message for message in player.messages if message["type"] == "play"]

    await wait_for(lambda: plays() and second_started.is_set())
    assert [message["text"] for message in plays()] == ["鱼鱼刚看完这一段，"]
    first = {"job_id": job_id, "playback_token": plays()[0]["playback_token"]}
    assert not await runtime.live.acknowledge(owner, first | {"type": "ended"})
    assert await runtime.live.acknowledge(owner, first | {"type": "started"})
    first_audio_ms = runtime.live.timings["first_audio_ms"]
    assert first_audio_ms is not None
    assert await runtime.live.acknowledge(owner, first | {"type": "ended"})
    assert not await runtime.live.acknowledge(owner, first | {"type": "ended"})
    assert store.job(job_id)["status"] == "synthesizing"
    assert not store.all("SELECT * FROM conversations")
    assert room_feed(runtime.live)["last_spoken"] is None

    if cancel:
        await runtime.live.cancel_current()
        assert second_cancelled.is_set()
        assert store.job(job_id)["status"] == "cancelled"
        assert len(plays()) == 1
        assert not store.all("SELECT * FROM conversations")
        return

    second_release.set()
    await wait_for(lambda: len(plays()) == 2)
    second = {"job_id": job_id, "playback_token": plays()[1]["playback_token"]}
    assert plays()[1]["text"] == "下一轮我们慢一点。"
    assert second != first
    assert not await runtime.live.acknowledge(owner, first | {"type": "ended"})
    assert await runtime.live.acknowledge(owner, second | {"type": "started"})
    assert runtime.live.timings["first_audio_ms"] == first_audio_ms
    assert await runtime.live.acknowledge(owner, second | {"type": "ended"})
    assert store.job(job_id)["status"] == "completed"
    spoken = "鱼鱼刚看完这一段，下一轮我们慢一点。"
    assert [row["reply_text"] for row in store.all("SELECT * FROM conversations")] == [spoken]
    assert room_feed(runtime.live)["last_spoken"]["text"] == spoken


async def test_song_is_real_playback_and_never_enters_dialogue_memory(runtime, store):
    song = await add_song(runtime)
    player = Player()
    owner = await runtime.live.attach_overlay(player)
    await runtime.control("start")
    result = await runtime.submit(Event(type="song", text=song["title"]))
    await wait_for(lambda: any(m["type"] == "play" for m in player.messages))
    message = next(m for m in player.messages if m["type"] == "play")
    assert message["audio_ref"] == "/media/music/" + song["audio"]
    assert message["media"]["mode"] == "direct"
    ack = {"job_id": result["job_id"], "playback_token": message["playback_token"]}
    assert not await runtime.live.acknowledge(owner, ack | {"type": "ended"})
    assert await runtime.live.acknowledge(owner, ack | {"type": "started"})
    assert await runtime.live.acknowledge(
        owner, ack | {"type": "progress", "position": 0.1, "paused": False, "mouth": 0.05}
    )
    assert runtime.live.playback["position"] == 0.1
    await runtime.control("media-pause")
    assert player.messages[-1]["type"] == "media-control"
    assert player.messages[-1]["paused"] is True
    assert await runtime.live.acknowledge(
        owner, ack | {"type": "progress", "position": 0.1, "paused": True, "mouth": 0}
    )
    assert await runtime.live.acknowledge(owner, ack | {"type": "ended"})
    assert store.job(result["job_id"])["status"] == "completed"
    assert not store.all("SELECT * FROM conversations")
    assert not store.all("SELECT * FROM context_windows")


async def test_live_studio_alert_tracks_actual_playback_recovery(runtime):
    song = await add_song(runtime)
    player = Player()
    owner = await runtime.live.attach_overlay(player)
    await runtime.control("start")

    async def request_song():
        result = await runtime.submit(Event(type="song", text=song["title"]))
        identity = result["job_id"]
        await wait_for(lambda: any(message.get("job_id") == identity for message in player.messages))
        play = next(message for message in player.messages if message.get("job_id") == identity)
        return {"job_id": identity, "playback_token": play["playback_token"]}

    failed = await request_song()
    assert await runtime.live.acknowledge(owner, failed | {"type": "failed"})
    assert "浏览器播放失败" in room_feed(runtime.live)["error"]
    await wait_for(lambda: runtime.live.current_id is None)

    recovered = await request_song()
    assert await runtime.live.acknowledge(owner, recovered | {"type": "started"})
    assert await runtime.live.acknowledge(owner, recovered | {"type": "ended"})
    assert room_feed(runtime.live)["error"] is None


async def test_song_snapshot_dedup_permissions_and_unsupported_singing(runtime):
    await add_song(runtime)
    await runtime.control("start")
    await runtime.control("pause")
    first = await runtime.submit(Event(type="song", text="测试曲目"))
    assert first["status"] == "queued"
    duplicate = await runtime.submit(Event(type="song", text="测试曲目"))
    assert duplicate["status"] == "filtered" and "排队" in duplicate["reason"]
    runtime.live.media.store.set("music_settings", {"default_mode": "sing"})
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
    path = runtime.live.media.blob(song["audio"])
    activity = inspect_wav(path, envelope=True)
    assert len(activity["envelope"]) == 8 and max(activity["envelope"]) > 0.01
    path.write_bytes(pcm(silence=True))
    with pytest.raises(ValueError, match="内容已改变"):
        runtime.live.media.prepare({"song": song})
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
    assert runtime.live.media.save_scene(scene) == scene
    assert runtime.live.media.scene() == scene
    scene["items"].append(scene["items"][0])
    with pytest.raises(ValueError, match="重复"):
        runtime.live.media.save_scene(scene)
    scene = default_scene()
    scene["items"][0]["x"] = 1800
    with pytest.raises(ValueError, match="画布"):
        runtime.live.media.save_scene(scene)


def test_live_api_scopes_streaming_upload_and_scene_persistence(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        assert client.get("/api/live/view").status_code == 401
        assert client.post("/api/live/uploads", content=pcm()).status_code == 401
        client.post("/api/overlay/auth", json={"token": settings.overlay_token})
        assert client.get("/api/live/view").status_code == 200
        assert client.get("/api/live/studio").status_code == 401
        assert client.put("/api/live/scene", json=default_scene()).status_code == 401
        assert client.get("/api/live/music").status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        uploaded = client.post("/api/live/uploads", content=pcm()).json()
        assert uploaded["id"] == hashlib.sha256(pcm()).hexdigest()
        assert client.get("/media/music/" + uploaded["id"]).content == pcm()
        assert client.post("/api/live/uploads", content=b"not a wave").status_code == 400
        assert not list(app.state.runtime.live.media.directory.glob(".upload-*"))
        scene = default_scene()
        scene["title"] = "保存后的标题"
        assert client.put("/api/live/scene", json=scene).status_code == 200
        assert client.get("/api/live/view").json()["scene"]["title"] == "保存后的标题"
        assert (
            client.put("/api/live/scene", json=scene, headers={"Origin": "https://other.example"}).status_code
            == 403
        )
        assert client.put("/api/live/queue", json=["missing"]).status_code == 400


def test_live_studio_limits_chat_and_dialogue_to_this_session(settings):
    with TestClient(create_app(settings)) as client:
        client.post("/api/overlay/auth", json={"token": settings.overlay_token}).raise_for_status()
        client.headers["Authorization"] = f"Bearer {settings.admin_token}"
        client.post("/api/live/start").raise_for_status()
        client.post("/api/live/pause").raise_for_status()
        client.post("/api/live/poll/start", json={"topic": "晚饭", "options": ["白饭", "海带"]}).raise_for_status()
        for text, uid in [("1", "first-viewer"), ("刚刚聊到哪了？", "second-viewer")]:
            client.post("/api/live/mock-event", json={"text": text, "platform_user_id": uid}).raise_for_status()
        desk = client.get("/api/live/studio").raise_for_status().json()
        assert [row["text"] for row in desk["chat"]] == ["刚刚聊到哪了？"]
        assert [row["status"] for row in desk["dialogue"]] == ["queued"]
        assert desk["dialogue"][0]["text"] is None and desk["last_spoken"] is None
        output = client.get("/api/live/view", headers={"Authorization": ""}).raise_for_status().json()
        assert output["queue"] == []
        assert set(output["events"][0]) == {"type", "display_name", "text"}
        client.post("/api/live/stop").raise_for_status()
        client.post("/api/live/start").raise_for_status()
        next_desk = client.get("/api/live/studio").raise_for_status().json()
        assert desk["session_id"] != next_desk["session_id"]
        assert not next_desk["chat"] and not next_desk["dialogue"]


@pytest.mark.parametrize("action", ["mute", "skip", "stop"])
async def test_song_cancellation_stops_player_and_rejects_late_completion(runtime, action):
    await add_song(runtime)
    player = Player()
    owner = await runtime.live.attach_overlay(player)
    await runtime.control("start")
    result = await runtime.submit(Event(type="song", text="测试曲目"))
    await wait_for(lambda: any(m["type"] == "play" for m in player.messages))
    message = next(m for m in player.messages if m["type"] == "play")
    ack = {"job_id": result["job_id"], "playback_token": message["playback_token"]}
    assert await runtime.live.acknowledge(owner, ack | {"type": "started"})
    await runtime.control(action)
    assert not await runtime.live.acknowledge(owner, ack | {"type": "ended"})
    assert runtime.store.job(result["job_id"])["status"] == "cancelled"
    assert runtime.live.playback is None
    assert player.messages[-1]["type"] == "stop"
