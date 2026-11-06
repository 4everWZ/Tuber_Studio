import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.live.scene import LiveScene
from tuber_studio.models import Event, Reply


class ReadOnlyPlatform:
    async def close(self):
        pass


@pytest.fixture
def client(settings):
    app = create_app(settings, ReadOnlyPlatform())
    with TestClient(app) as test_client:
        # Authenticate
        res = test_client.post("/api/auth", json={"token": settings.admin_token})
        assert res.status_code == 200
        yield test_client


async def test_live_reply_uses_only_current_trusted_scene(settings, monkeypatch):
    current = True

    def snapshot(session_id, event):
        return LiveScene(
            session_id if current else "old-session", time.monotonic(), "角色已经找到钥匙",
        )

    app = create_app(settings, ReadOnlyPlatform(), live_scene=snapshot)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        runtime.session_id = "session-1"
        generator = AsyncMock(return_value=Reply(spoken_text="让我看看。"))
        monkeypatch.setattr(runtime.llm, "generate", generator)
        event = Event(type="chat", display_name="观众", text="进度怎么样？").model_dump(mode="json")
        await runtime.generate_reply("live:session-1", runtime.character, event)
        messages = generator.await_args.args[0]
        assert any("已经找到钥匙" in message["content"] for message in messages)
        current = False
        await runtime.generate_reply("live:session-1", runtime.character, event)
        messages = generator.await_args.args[0]
        assert not any("已经找到钥匙" in message["content"] for message in messages)
        assert generator.await_count == 2


def test_api_interactive_rules(client):
    res = client.get("/api/interactive-rules")
    assert res.status_code == 200
    data = res.json()
    assert "song_request" in data
    assert "draw_and_guess" in data
    assert "game_livestream" in data

    # Update rules
    data["song_request"]["mode"] = "gift_only"
    data["song_request"]["min_coin"] = 25
    put_res = client.put("/api/interactive-rules", json=data)
    assert put_res.status_code == 200
    assert put_res.json()["song_request"]["mode"] == "gift_only"
    assert put_res.json()["song_request"]["min_coin"] == 25


def test_api_games_management(client):
    res = client.get("/api/games/status")
    assert res.status_code == 200
    data = res.json()
    assert data["active_game_id"] == "stardew_valley"
    assert "星露谷物语" in data["active_status"]["summary"]

    # Switch game
    sw_res = client.post("/api/games/switch", json={"game_id": "pvz"})
    assert sw_res.status_code == 200
    assert sw_res.json()["current_game_id"] == "pvz"
    assert "植物大战僵尸" in sw_res.json()["spoken_text"]

    # Execute action
    act_res = client.post("/api/games/action", json={"action": "plant_sunflower"})
    assert act_res.status_code == 200
    assert act_res.json()["success"] is True

    # Step game
    step_res = client.post("/api/games/step")
    assert step_res.status_code == 200


def test_api_draw_and_guess(client):
    res = client.get("/api/draw-and-guess/status")
    assert res.status_code == 200

    # Start round
    start_res = client.post("/api/draw-and-guess/start", json={"trigger_user": "小强", "duration_sec": 45})
    assert start_res.status_code == 200
    assert start_res.json()["status"] == "started"
    assert "小强" in start_res.json()["spoken_text"]

    # Check status
    st_res = client.get("/api/draw-and-guess/status")
    assert st_res.status_code == 200
    assert st_res.json()["active"] is True

    # Stop round
    stop_res = client.post("/api/draw-and-guess/stop")
    assert stop_res.status_code == 200
    assert stop_res.json()["status"] == "stopped"

    # Overlay HTML check
    overlay_res = client.get("/overlay/draw-guess")
    assert overlay_res.status_code == 200
    assert "你画我猜" in overlay_res.text


@pytest.mark.asyncio
async def test_live_mock_event_draw_guess_flow(settings):
    app = create_app(settings, ReadOnlyPlatform())
    runtime = app.state.runtime
    runtime.state = "live"

    # 1. Start Draw & Guess via Gift event
    gift_start = Event(
        type="gift",
        display_name="粉丝小王",
        metadata={"gift_name": "画笔", "price": 10},
    )
    res_start = await runtime.submit(gift_start)
    assert res_start["status"] == "queued"
    assert runtime.draw_and_guess.is_active() is True

    # Force a known word for testing
    runtime.draw_and_guess.current_word = {
        "id": "watermelon",
        "word": "西瓜",
        "category": "水果",
        "aliases": ["大西瓜"],
        "hint": "红瓤黑籽",
        "strokes": [{"type": "circle", "cx": 10, "cy": 10, "r": 5}],
    }

    # 2. Chat with winning guess
    chat_guess = Event(
        type="chat",
        display_name="猜谜高手",
        text="我猜是西瓜！",
    )
    res_guess = await runtime.submit(chat_guess)
    assert res_guess["status"] == "queued"
    assert runtime.draw_and_guess.winner == "猜谜高手"
    assert runtime.draw_and_guess.state == "finished"

    # Verify highlight was recorded to daily_memory
    highlights = runtime.daily_memory.list_highlights(runtime.character["id"], runtime.character["version"])
    assert any("西瓜" in h["headline"] for h in highlights)


@pytest.mark.asyncio
async def test_live_mock_event_game_switch_and_action(settings):
    app = create_app(settings, ReadOnlyPlatform())
    runtime = app.state.runtime
    runtime.state = "live"

    # Switch game via gift
    gift_switch = Event(
        type="gift",
        text="大肥鱼换玩植物大战僵尸！",
        display_name="热心观众",
        metadata={"gift_name": "向日葵", "price": 50},
    )
    res_switch = await runtime.submit(gift_switch)
    assert res_switch["status"] == "queued"
    assert runtime.game_livestream.active_game_id == "pvz"

    # In-game bounty action via gift
    gift_action = Event(
        type="gift",
        text="快放樱桃炸弹救场！",
        display_name="指挥官",
        metadata={"gift_name": "炸弹", "price": 50},
    )
    res_action = await runtime.submit(gift_action)
    assert res_action["status"] == "queued"
    pvz_adapter = runtime.game_livestream.get_active_adapter()
    assert len(pvz_adapter.priority_tasks) == 1
    assert pvz_adapter.priority_tasks[0]["action"] == "use_cherry_bomb"
