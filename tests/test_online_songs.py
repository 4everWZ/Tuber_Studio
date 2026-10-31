"""Selection, lifecycle and raw-media contracts; no upstream network in CI."""

import asyncio
import hashlib
import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.live_media import inspect_audio
from tuber_studio.models import Event
from tuber_studio.online_songs import OnlineSongs, SongRequestSettings, cache_bytes, parse_lrc
from tuber_studio.song_component import SongComponent
from tuber_studio.song_download import public_address

FIXTURES = Path(__file__).parent / "fixtures/audio"
SONG = {"id": "123", "title": "测试歌", "artist": "测试歌手", "artists": ["测试歌手"], "duration": 3.0}


class Component:
    def __init__(self):
        self.running, self.error, self.calls = False, None, []
        self.rows = [SONG]

    def status(self):
        return {"installed": True, "running": self.running, "error": self.error}

    async def start(self):
        self.running, self.error = True, None

    async def stop(self):
        self.running = False

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "search":
            return {"songs": self.rows}
        if method == "detail":
            return {**SONG, "id": params["id"]}
        if method == "lyrics":
            return {"lrc": "[00:00.00]第一行\n[00:01.50]第二行"}
        if method == "resolve":
            return {
                "song": {**SONG, "id": params["id"]},
                "source": params["source"],
                "url": "https://audio.kuwo.cn/test",
            }
        raise AssertionError(method)


async def enabled(runtime, monkeypatch, *, fmt="flac"):
    online = runtime.online_songs
    online.component = Component()

    async def download(url, path, maximum):
        data = (FIXTURES / ("tone." + fmt)).read_bytes()
        if len(data) > maximum:
            raise ValueError("测试下载超过上限")
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)

    monkeypatch.setattr("tuber_studio.online_songs.download", download)
    await online.configure({"enabled": True, "live_allowed": True, "license_note": "自有生成测试波形"})
    await runtime.control("start")
    await runtime.control("pause")
    return online


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("fmt,mime", [("flac", "audio/flac"), ("mp3", "audio/mpeg")])
def test_raw_file_detection_upload_and_range_serving(settings, fmt, mime):
    data = (FIXTURES / ("tone." + fmt)).read_bytes()
    info = inspect_audio(FIXTURES / ("tone." + fmt))
    assert info["format"] == fmt and abs(info["duration"] - 3) < 0.1
    assert info["sample_rate"] == 44100
    if fmt == "mp3":
        assert info["bitrate"] == 320000
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth", json={"token": settings.admin_token})
        result = client.post("/api/live/uploads", content=data).json()
        assert result["format"] == fmt
        response = client.get("/media/music/" + result["id"])
        assert response.content == data and response.headers["content-type"] == mime
        response = client.get("/media/music/" + result["id"], headers={"Range": "bytes=0-99"})
        assert response.status_code == 206 and response.content == data[:100]


async def test_online_loop_uses_actual_player_and_cache_without_tts(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    result = await runtime.submit(Event(type="song", metadata={"online_id": "123"}, text="测试歌"))
    assert result["status"] == "queued"
    snapshot = json.loads(runtime.store.job(result["job_id"])["media"])
    assert snapshot["online"]["id"] == "123" and "audio" not in snapshot["song"]
    prepared = await online.prepared(result["job_id"])
    assert prepared["song"]["quality"]["format"] == "flac"
    assert len(prepared["song"]["lyrics"]) == 2
    messages = []

    class Player:
        async def send_json(self, message):
            messages.append(message)

    owner = await runtime.attach_overlay(Player())
    await runtime.control("resume")
    await wait_for(lambda: any(message["type"] == "play" for message in messages))
    play = next(message for message in messages if message["type"] == "play")
    assert play["audio_ref"].startswith("/media/music/")
    assert play["media"]["song"]["source"] == "bodian"
    ack = {"job_id": result["job_id"], "playback_token": play["playback_token"]}
    assert await runtime.acknowledge(owner, {**ack, "type": "started"})
    assert await runtime.acknowledge(owner, {**ack, "type": "ended"})
    await wait_for(lambda: runtime.current_id is None)
    assert runtime.store.job(result["job_id"])["status"] == "completed"
    assert not runtime.store.all("SELECT * FROM conversations")
    count = len(online.component.calls)
    # A second request reuses verified raw bytes and lyrics, never a stale URL.
    second = await runtime.submit(Event(type="song", metadata={"online_id": "123"}))
    await online.prepared(second["job_id"])
    assert [method for method, _ in online.component.calls[count:]] == ["detail"]


async def test_disable_cancels_online_preparation_and_queue_preserves_chat(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_download(url, path, maximum):
        path.write_bytes(b"partial")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("tuber_studio.online_songs.download", slow_download)
    first = await runtime.submit(Event(type="song", metadata={"online_id": "123"}))
    chat = await runtime.submit(Event(type="chat", text="今天天气不错"))
    await entered.wait()
    await online.configure({**online.config(), "enabled": False})
    assert cancelled.is_set() and not list(online.directory.glob(".download-*"))
    assert not online.tasks and not online.component.running
    assert runtime.queue == [chat["job_id"]] and runtime.state == "paused"
    assert runtime.store.job(first["job_id"])["status"] == "cancelled"
    assert online.config()["license_note"] == "自有生成测试波形"
    messages = []

    class Player:
        async def send_json(self, message):
            messages.append(message)

    owner = await runtime.attach_overlay(Player())
    await runtime.control("resume")
    await wait_for(lambda: any(message["type"] == "play" for message in messages))
    play = next(message for message in messages if message["type"] == "play")
    assert play["job_id"] == chat["job_id"] and play["media"] is None
    assert play["audio_ref"].endswith(".wav")
    ack = {"job_id": chat["job_id"], "playback_token": play["playback_token"]}
    assert await runtime.acknowledge(owner, {**ack, "type": "started"})
    assert await runtime.acknowledge(owner, {**ack, "type": "ended"})
    assert runtime.store.job(chat["job_id"])["status"] == "completed"


async def test_disable_stops_current_online_playback_without_stopping_session(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    result = await runtime.submit(Event(type="song", metadata={"online_id": "123"}))
    messages = []

    class Player:
        async def send_json(self, message):
            messages.append(message)

    await runtime.attach_overlay(Player())
    await runtime.control("resume")
    await wait_for(lambda: runtime.playback is not None)
    await online.configure({**online.config(), "enabled": False})
    assert runtime.store.job(result["job_id"])["status"] == "cancelled"
    assert runtime.playback is None and runtime.state == "live"
    assert any(message["type"] == "stop" and message["job_id"] == result["job_id"] for message in messages)
    assert not online.tasks and not online.component.running


async def test_only_current_and_next_song_prepare_and_reorder_cancels_old_work(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    runtime.settings.data["queue_limit"] = 10
    jobs = []
    for identity in ["1", "2", "3"]:
        result = await runtime.submit(Event(type="song", metadata={"online_id": identity}))
        jobs.append(result["job_id"])
    assert set(online.tasks) == set(jobs[:2])
    runtime.queue[:] = list(reversed(jobs))
    online.sync()
    assert set(online.tasks) == {jobs[2], jobs[1]}


async def test_ambiguous_chat_confirmation_is_scoped_to_viewer_and_room(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    online.component.rows = [SONG, {**SONG, "id": "456", "artist": "另一歌手", "artists": ["另一歌手"]}]
    request = Event(type="chat", text="点歌 测试歌", platform_user_id="viewer", room_id="a")
    result = await runtime.submit(request)
    assert result["status"] == "needs_confirmation" and not runtime.queue
    assert len(online.status()["candidates"]) == 1
    outsider = await runtime.submit(Event(type="chat", text="1", platform_user_id="viewer", room_id="b"))
    assert outsider["status"] == "filtered" and not runtime.queue
    selected = await runtime.submit(Event(type="chat", text="1", platform_user_id="viewer", room_id="a"))
    assert selected["status"] == "queued"
    assert json.loads(runtime.store.job(selected["job_id"])["media"])["song"]["id"] == "online_123"
    assert not online.status()["candidates"]


async def test_disable_during_search_cannot_enqueue_late_result(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = online.component.call

    async def delayed(method, params):
        if method == "search":
            entered.set()
            await release.wait()
        return await original(method, params)

    monkeypatch.setattr(online.component, "call", delayed)
    task = asyncio.create_task(runtime.submit(Event(type="chat", text="点歌 测试歌")))
    await entered.wait()
    await online.configure({**online.config(), "enabled": False})
    release.set()
    assert (await task)["status"] == "filtered" and not runtime.queue


@pytest.mark.parametrize("change", ["session", "duplicate"])
async def test_search_admission_survives_session_changes_and_concurrent_duplicates(
    runtime, monkeypatch, change
):
    online = await enabled(runtime, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = online.component.call

    async def delayed(method, params):
        if method == "search":
            entered.set()
            await release.wait()
        return await original(method, params)

    monkeypatch.setattr(online.component, "call", delayed)
    event = Event(type="chat", text="点歌 测试歌", platform_user_id="viewer")
    first = asyncio.create_task(runtime.submit(event))
    await entered.wait()
    if change == "session":
        await runtime.control("stop")
        await runtime.control("start")
        release.set()
        assert (await first)["status"] == "filtered" and not runtime.queue
        assert not online.choices
    else:
        second = asyncio.create_task(runtime.submit(event))
        await asyncio.sleep(0)
        release.set()
        assert [result["status"] for result in await asyncio.gather(first, second)] == ["queued", "duplicate"]
        assert len(runtime.queue) == 1
        assert [method for method, _ in online.component.calls].count("search") == 1
    assert not runtime.submissions


def test_quality_validation_rejects_wrong_version_duration_and_lower_bitrate():
    config = SongRequestSettings().model_dump()
    info = inspect_audio(FIXTURES / "tone.mp3")
    OnlineSongs.validate_quality(info, {"duration": 3}, config)
    with pytest.raises(ValueError, match="时长"):
        OnlineSongs.validate_quality(info, {"duration": 30}, config)
    with pytest.raises(ValueError, match="码率"):
        OnlineSongs.validate_quality({**info, "bitrate": 128000}, {"duration": 3}, config)
    OnlineSongs.validate_quality(
        {**info, "bitrate": 128000}, {"duration": 3}, {**config, "allow_lower_quality": True}
    )
    assert parse_lrc("[offset:-500]\n[00:01.00][00:02.00]多时间标签\n[99:99]无效") == [
        {"time": 0.5, "text": "多时间标签"},
        {"time": 1.5, "text": "多时间标签"},
    ]


async def test_cache_eviction_accounts_shared_blobs_once_and_preserves_local_music(runtime, monkeypatch):
    online = await enabled(runtime, monkeypatch)
    data = (FIXTURES / "tone.flac").read_bytes()
    identity = hashlib.sha256(data).hexdigest()
    path = online.directory / (identity + ".flac")
    path.write_bytes(data)
    row = {"audio": identity, "bytes": len(data), "used_at": time.time(), "quality": {"format": "flac"}}
    online.store.set("online_music_cache", {"a": row, "b": row})
    assert cache_bytes(online.store.get("online_music_cache")) == len(data)
    # A local catalog reference protects bytes even when they originated online.
    online.store.set("music_catalog", [{"id": "local", "audio": identity}])
    with pytest.raises(ValueError, match="空间不足"):
        online.evict(reserve=online.config()["cache_limit_mib"] * 1024 * 1024)
    assert path.exists()
    online.store.set("music_catalog", [])
    online.evict(reserve=online.config()["cache_limit_mib"] * 1024 * 1024)
    assert not path.exists() and not online.store.get("online_music_cache")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/a",
        "http://music.migu.cn.attacker.test/a",
        "https://x.kuwo.cn:444/a",
        "https://u:p@x.kuwo.cn/a",
    ],
)
async def test_download_rejects_unsupported_hosts_and_schemes(url):
    with pytest.raises(ValueError, match="地址"):
        await public_address(url)


async def test_cdn_dns_must_be_entirely_public(monkeypatch):
    async def addresses(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
    with pytest.raises(ValueError, match="非公网"):
        await public_address("https://audio.kuwo.cn/file.flac")


async def test_missing_component_is_explicit_and_off_does_not_spawn(tmp_path):
    component = SongComponent(tmp_path)
    assert component.status()["running"] is False
    with pytest.raises(ValueError, match="未安装"):
        await component.start()
    assert component.process is None


def test_optional_environment_dependencies_can_be_separate_from_readonly_helper(tmp_path, monkeypatch):
    component = SongComponent(tmp_path / "program")
    component.directory.mkdir(parents=True)
    (component.directory / "helper.cjs").write_text("// mounted program")
    libraries = tmp_path / "image-node-modules"
    for name in ("netease-metadata", "@unblockneteasemusic/server"):
        path = libraries / name / "package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    monkeypatch.setattr("tuber_studio.song_component.shutil.which", lambda name: "/image/node")
    monkeypatch.setenv("NODE_PATH", str(libraries))
    assert component.installed()
    assert not (component.directory / "node_modules").exists()
    (libraries / "netease-metadata/package.json").unlink()
    assert not component.installed()


def test_optional_api_is_admin_only_and_disabled_by_default(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/live/online").status_code == 401
        client.post("/api/overlay/auth", json={"token": settings.overlay_token})
        assert client.get("/api/live/online").status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        value = client.get("/api/live/online").json()
        assert not value["settings"]["enabled"] and not value["component"]["running"]
        assert client.get("/api/live/online/search?q=test").status_code == 400
        assert client.post("/api/live/online/confirm", json={"token": [], "number": True}).status_code == 422
        assert client.put("/api/live/online", json={"sources": ["bodian", "bodian"]}).status_code == 422
