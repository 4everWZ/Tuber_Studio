import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.live import options
from tuber_studio.live.avatar import SDK
from tuber_studio.live.options import LiveOptions, WeatherLocation
from tuber_studio.live.session import IDLE_AFTER
from tuber_studio.models import Event


class Overlay:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not (result := predicate()):
            await asyncio.sleep(0.01)
        return result


async def test_idle_yields_to_real_chat_before_play_and_audio_envelope(runtime, store):
    await runtime.control("start")
    overlay = Overlay()
    owner = await runtime.live.attach_overlay(overlay)
    runtime.live.last_activity = time.monotonic() - IDLE_AFTER
    runtime.live.set_options(LiveOptions(idle=True))
    first = await wait_for(lambda: next((m for m in overlay.messages if m["type"] == "play"), None))
    assert store.event(store.job(first["job_id"])["event_id"])["payload"]["type"] == "idle"
    assert any(value > 0 for value in first["activity"]["envelope"])
    ordinary = Event(text="普通观众先聊", platform_user_id="123", platform="bilibili")
    chat = await runtime.live.submit(ordinary, verified_account_id="900")
    second = await wait_for(lambda: next(
        (m for m in overlay.messages if m["type"] == "play" and m["job_id"] == chat["job_id"]), None,
    ))
    assert store.job(first["job_id"])["status"] == "cancelled"
    ack = {"job_id": second["job_id"], "playback_token": second["playback_token"]}
    score_scope = (runtime.live.session_id, runtime.character["id"], "bilibili", "123")
    assert runtime.relationships.live_view(*score_scope, account_id="900")["affinity"] == 0
    assert await runtime.live.acknowledge(owner, ack | {"type": "started"})
    assert await runtime.live.acknowledge(owner, ack | {"type": "ended"})
    assert runtime.relationships.live_view(*score_scope, account_id="900")["affinity"] == 1
    assert not store.all("SELECT * FROM account_relations")
    assert not store.all("SELECT * FROM users")
    assert len(store.all("SELECT * FROM conversations")) == 1


async def test_mock_gift_cannot_promote_real_relationship(runtime, store):
    await runtime.control("start")
    await runtime.control("pause")
    data = {"type": "gift", "platform": "bilibili", "text": "小礼物", "metadata": {
        "_live_account": "900", "paid_support": True, "price": 100,
    }}
    await runtime.submit(Event(**data, platform_user_id="mock"))
    assert not store.all("SELECT * FROM account_relations")
    await runtime.live.submit(Event(**data, platform_user_id="real"), verified_account_id="900")
    char = runtime.character["id"]
    assert len(store.all("SELECT * FROM account_relations")) == 1
    assert runtime.relationships.view(char, "bilibili", "real", account_id="900")["affinity"] == 0


async def test_lookup_once_only_and_timeout_still_allows_one_final(runtime, monkeypatch):
    reads, calls = [], []

    async def fetch(url):
        reads.append(url)
        await asyncio.sleep(1)

    async def generate(messages, schema, *, max_tokens):
        calls.append((messages, schema, max_tokens))
        if len(calls) == 1:
            return {"web_fetch": "https://public.example/page"}
        return {"spoken_text": "这次没读到。", "reply_to": ["event"]}

    runtime.llm.supports_operation_web_fetch = True
    runtime.llm.config = {"max_tokens": 180}
    runtime.llm.generate_json = generate
    monkeypatch.setattr(options, "fetch_excerpt", fetch)
    monkeypatch.setattr(options, "LOOKUP_TIMEOUT", 0.01)
    runtime.live.set_options(LiveOptions(timezone="UTC", web_fetch=True))
    await runtime.control("start")
    reply, _ = await runtime.generate_reply(
        "live:" + runtime.live.session_id, runtime.character, Event(event_id="event", text="看看这个网页").model_dump(mode="json"),
    )
    assert reply.spoken_text == "这次没读到。"
    assert len(calls) == 2 and reads == ["https://public.example/page"]
    assert len(calls[0][1]["oneOf"]) == 2 and "oneOf" not in calls[1][1]
    assert any("（UTC）" in msg["content"] for msg in calls[0][0])
    assert any("没有查到" in msg["content"] for msg in calls[1][0])
    assert all(call[2] == 180 for call in calls)
    assert not runtime.store.all("SELECT * FROM conversations")


async def test_configured_weather_reuses_ten_minute_cache(runtime, monkeypatch):
    requests = []

    def result(request):
        requests.append(request)
        return httpx.Response(200, json={
            "current": {"temperature_2m": 24.5, "time": "2026-11-10T03:00", "weather_code": 61},
        })

    client = httpx.AsyncClient
    monkeypatch.setattr(options.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(result), **kwargs,
    ))
    location = WeatherLocation(name="台北", latitude=25.0, longitude=121.5)
    config = LiveOptions(weather=location)
    first = await runtime.live.lookup.read(config, {"weather": True})
    assert first == await runtime.live.lookup.read(config, {"weather": True})
    assert first["地点"] == "台北" and first["天气"] == "雨"
    assert len(requests) == 1 and requests[0].url.params["latitude"] == "25.0"
    assert "没有查到" in (await runtime.live.lookup.read(config, {"weather": True, "web_fetch": "bad"}))["状态"]


def install_avatar(runtime: Path, *, model_refs):
    sdk, model = runtime / "live2d" / "sdk", runtime / "live2d" / "whale"
    sdk.mkdir(parents=True)
    model.mkdir()
    for file in SDK:
        (sdk / file).write_text("// demo file", encoding="utf-8")
    (model / "avatar.json").write_text(json.dumps({"model": "whale.model3.json", "license": "Local fixture"}), encoding="utf-8")
    (model / "whale.model3.json").write_text(json.dumps({"FileReferences": model_refs}), encoding="utf-8")
    (model / "whale.moc3").write_bytes(b"local-fixture")
    (model / "texture.png").write_bytes(b"local-fixture")
    return model


def test_live2d_optional_scoped_and_no_second_audio(settings):
    model = install_avatar(settings.runtime, model_refs={"Moc": "whale.moc3", "Textures": ["texture.png"]})
    (model / "private.txt").write_text("unreferenced fixture", encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as client:
        path = "/media/live2d/model/whale/whale.model3.json"
        assert client.get(path).status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        view = client.get("/api/live/view").raise_for_status().json()
        assert view["live2d"]["model"] == path and view["live2d"]["status"] == "available"
        assert client.get(path).status_code == 200
        assert client.get("/media/live2d/model/whale/private.txt").status_code == 404
        assert client.get("/media/live2d/model/other/whale.model3.json").status_code == 404
        broken = {"FileReferences": {"Moc": "whale.moc3", "Textures": ["texture.png"], "Motions": {
            "Idle": [{"File": "motion.json", "Sound": "second-audio.wav"}],
        }}}
        (model / "whale.model3.json").write_text(json.dumps(broken), encoding="utf-8")
        assert client.get("/api/live/view").json()["live2d"]["status"] == "unavailable"
        assert client.get(path).status_code == 404


def test_live_controls_and_mock_poll_endpoints(settings):
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        assert client.get("/api/live/options").json()["weather"] is None
        assert client.put("/api/live/options", json={"timezone": "InvalidTZ"}).status_code == 422
        assert client.put("/api/live/options", json={"idle": True, "timezone": "UTC"}).json()["idle"]
        client.post("/api/live/start").raise_for_status()
        client.post("/api/live/pause").raise_for_status()
        payload = {"topic": "晚饭", "options": ["白饭", "海带"]}
        started = client.post("/api/live/poll/start", json=payload).raise_for_status().json()
        assert client.get("/api/live/view").json()["poll"]["id"] == started["id"]
        receipt = client.post("/api/live/mock-event", json={"text": "1", "platform_user_id": "voter"}).json()
        assert receipt["reason"] == "已计票"
        final = client.post("/api/live/poll/finish").raise_for_status().json()
        assert final["winner"] == "白饭"
        view = client.get("/api/live/view").raise_for_status().json()
        assert not view["poll"] and not view["events"] and not view["queue"]
