"""Tests for dual OBS scene layouts, live poll enhancements, Rundown engine, and fine-grained settings."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.core.models import Event
from tuber_studio.live.activity_filter import LiveActivityFilter, LivePoll
from tuber_studio.live.facade import live_facade
from tuber_studio.live.media import default_game_scene, default_scene
from tuber_studio.live.session.cold_director import ColdRoomDirector
from tuber_studio.live.session.overtime_theater import OvertimeTheater
from tuber_studio.live.session.rundown import GAME_CATALOG, RundownPlan, build_presets


class _OverlayRecorder:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


def test_dual_scene_layouts_and_profile_persistence(runtime):
    chat_def = default_scene()
    game_def = default_game_scene()
    assert chat_def["background"] == "ocean"
    assert game_def["background"] == "transparent"

    avatar = next(item for item in game_def["items"] if item["kind"] == "avatar")
    assert (avatar["x"], avatar["y"], avatar["width"], avatar["height"]) == (1440, 620, 440, 420)
    assert all(
        item["x"] + item["width"] <= game_def["width"] and item["y"] + item["height"] <= game_def["height"]
        for item in game_def["items"]
    )

    media = runtime.live.media
    assert media.layout_state() == {"active_layout": "chat", "layout_locked": False}
    assert media.scene() == chat_def
    assert media.scene_for("game") == game_def

    custom_game = default_game_scene()
    custom_game["title"] = "星露谷游戏布局"
    assert media.save_scene_for("game", custom_game)["title"] == "星露谷游戏布局"
    # Saving game profile does not overwrite chat profile
    assert media.scene_for("chat")["title"] == chat_def["title"]

    media.set_layout(layout="game", locked=True)
    assert media.layout_state() == {"active_layout": "game", "layout_locked": True}
    assert media.scene()["title"] == "星露谷游戏布局"

    media.set_layout(layout="chat", locked=False)
    assert media.scene()["title"] == chat_def["title"]


def test_realtime_poll_wildcard_and_chinese_aliases():
    filt = LiveActivityFilter()
    assert filt.public_poll() is None

    poll = filt.start_poll(
        LivePoll(
            platform="*",
            room_id="*",
            topic="下一环节玩什么？",
            options=["你画我猜 2.0", "灵魂海龟汤", "继续闲聊"],
            duration_seconds=90,
            fallback_label="无人投票将按默认节目单推进",
        )
    )
    assert poll["fallback_label"] == "无人投票将按默认节目单推进"

    # Real Bilibili danmaku + local mock danmaku both accepted via wildcard
    e1 = Event(platform="bilibili", room_id="223344", platform_user_id="bili-1", text="你画我猜")
    e2 = Event(platform="mock", room_id="debug", platform_user_id="mock-1", text="海龟汤")
    e3 = Event(platform="mock", room_id="debug", platform_user_id="mock-2", text="继续聊")
    e4 = Event(platform="bilibili", room_id="999", platform_user_id="bili-2", text="1")

    assert filt.check_event(e1) == ("filtered", "已计票")
    assert filt.check_event(e2) == ("filtered", "已计票")
    assert filt.check_event(e3) == ("filtered", "已计票")
    assert filt.check_event(e4) == ("filtered", "已计票")

    pub = filt.public_poll()
    assert pub is not None
    assert pub["total_votes"] == 4
    assert pub["counts"] == {"你画我猜 2.0": 2, "灵魂海龟汤": 1, "继续闲聊": 1}
    assert 0 < pub["remaining_seconds"] <= 90.0
    assert pub["phase"] == "started"


@pytest.mark.asyncio
async def test_rundown_opening_vote_fallback_continue_chat_and_graceful_finish(runtime):
    assert len(GAME_CATALOG) == 6
    presets = build_presets()
    assert [p["id"] for p in presets] == ["multi_game_chat", "device_and_shows", "late_night_llm"]

    player = _OverlayRecorder()
    await runtime.live.attach_overlay(player)
    await runtime.control("start")

    # 1. Start rundown -> 15m chat opening in chat layout
    t0 = time.time()
    await runtime.live.rundown.start(now=t0)
    st = runtime.live.rundown.state(now=t0)
    assert st["active"] is True
    assert st["current_segment"]["kind"] == "chat"
    assert st["current_segment"]["obs_layout"] == "chat"
    assert st["remaining_seconds"] == pytest.approx(900.0, abs=1.0)
    assert runtime.live.media.layout_state()["active_layout"] == "chat"

    # 2. Opening timer expires -> automatically opens game-pool ballot
    trans = await runtime.live.rundown.tick(now=t0 + 901.0)
    assert trans is not None and trans["transition"] == "vote_opened"
    assert runtime.live.activity_filter.is_poll_active() is True
    assert runtime.live.rundown.state()["status"] == "voting"

    # 3. Vote for "你画我猜" and settle -> launches show & switches OBS layout to "game"
    await runtime.submit(Event(platform="mock", room_id="debug", platform_user_id="v1", text="你画我猜"))
    settled = await runtime.live.rundown.settle_vote(now=t0 + 920.0)
    assert settled["fallback"] is False
    assert settled["chosen_kind"] == "draw_and_guess"
    assert settled["scene_changed"] is True
    assert runtime.live.media.layout_state()["active_layout"] == "game"
    assert runtime.live.draw_and_guess.is_active() is True

    # 4. Graceful finish waits while draw_and_guess is still active
    seg_end = runtime.live.rundown.segment_end_at
    assert seg_end is not None
    waiting = await runtime.live.rundown.tick(now=seg_end + 1.0)
    assert waiting is None
    assert runtime.live.rundown.state()["waiting_graceful_finish"] is True
    assert runtime.live.rundown.state()["current_segment"]["kind"] == "draw_and_guess"

    # Once active round finishes, next tick advances cleanly
    runtime.live.draw_and_guess.stop_round()
    advanced = await runtime.live.rundown.tick(now=seg_end + 2.0)
    assert advanced is not None
    assert runtime.live.rundown.state()["waiting_graceful_finish"] is False

    # 5. Empty vote falls back cleanly into next default plan segment
    await runtime.live.rundown.start(now=t0 + 3000.0)
    await runtime.live.rundown.open_game_vote(now=t0 + 3010.0)
    empty_settled = await runtime.live.rundown.settle_vote(now=t0 + 3110.0)
    assert empty_settled["fallback"] is True
    assert runtime.live.rundown.state()["current_segment"]["kind"] == "draw_and_guess"
    assert runtime.live.media.layout_state()["active_layout"] == "game"

    # 6. "继续闲聊" winning extends chat (+15m, chat layout)
    await runtime.live.rundown.open_game_vote(now=t0 + 3200.0)
    await runtime.submit(Event(platform="bilibili", room_id="999", platform_user_id="v2", text="继续聊"))
    chat_won = await runtime.live.rundown.settle_vote(now=t0 + 3210.0)
    assert chat_won["fallback"] is False
    assert chat_won["chosen_kind"] == "chat"
    assert runtime.live.media.layout_state()["active_layout"] == "chat"
    assert runtime.live.rundown.state(now=t0 + 3210.0)["remaining_seconds"] == pytest.approx(900.0, abs=1.0)

    # 7. Layout lock blocks auto layout switching
    runtime.live.media.set_layout(layout="chat", locked=True)
    await runtime.live.rundown.jump(index=1, now=t0 + 3300.0)
    assert runtime.live.media.layout_state()["active_layout"] == "chat"


@pytest.mark.asyncio
async def test_barrage_rundown_commands_and_runner_scene_push(runtime):
    runtime.settings.data["queue_limit"] = 20
    player = _OverlayRecorder()
    await runtime.live.attach_overlay(player)
    await runtime.control("start")

    # Configure vote_trigger_threshold = 2
    plan = runtime.live.rundown.plan()
    plan = RundownPlan.model_validate(
        plan.model_dump()
        | {
            "chat_entry": plan.chat_entry.model_dump() | {"vote_trigger_threshold": 2},
        }
    )
    runtime.live.rundown.save_plan(plan)
    await runtime.live.rundown.start()

    # First #投票选游戏 reaches 1/2 -> does not open poll yet
    r1 = await runtime.submit(Event(type="chat", platform_user_id="u1", display_name="水友1", text="#投票选游戏"))
    assert r1["status"] == "queued"
    assert runtime.live.activity_filter.is_poll_active() is False

    # Second unique viewer #换游戏 reaches 2/2 -> opens game vote
    r2 = await runtime.submit(Event(type="chat", platform_user_id="u2", display_name="水友2", text="#换游戏"))
    assert r2["status"] == "queued"
    assert runtime.live.activity_filter.is_poll_active() is True

    # Settle into draw_and_guess (game layout), then test #继续聊 switching back to chat + pushing WS scene
    await runtime.submit(Event(type="chat", platform_user_id="u3", display_name="水友3", text="1"))
    await runtime.live.rundown.settle_vote()
    assert runtime.live.media.layout_state()["active_layout"] == "game"
    player.messages.clear()

    r3 = await runtime.submit(Event(type="chat", platform_user_id="u4", display_name="水友4", text="#继续聊"))
    assert r3["status"] == "queued"
    assert runtime.live.media.layout_state()["active_layout"] == "chat"
    assert any(m.get("type") == "scene" and m["scene"]["background"] == "ocean" for m in player.messages)


def test_cold_director_and_overtime_fine_config_methods():
    director = ColdRoomDirector(now=100.0)
    cfg = director.configure(
        {
            "tier1_delay_sec": 30,
            "tier2_delay_sec": 120,
            "tier3_delay_sec": 480,
            "tier1_cooldown_sec": 45,
            "tier2_cooldown_sec": 90,
            "tier3_cadence_sec": 180,
        }
    )
    assert cfg["tier1_delay_sec"] == 30.0
    assert director.current_tier(135.0) == 1
    assert director.current_tier(225.0) == 2

    with pytest.raises(ValueError):
        director.configure({"tier1_delay_sec": 500, "tier2_delay_sec": 100})

    ot = OvertimeTheater()
    ot.configure({"gift_coin_multiplier": 2.5, "chat_base_energy": 8.0, "default_base_duration_minutes": 45})
    st = ot.start_window(now=1000.0)
    assert st["base_duration_minutes"] == 45
    res = ot.on_gift(user_id="u1", display_name="粉丝", gift_name="灯牌", coins=40, now=1010.0)
    assert res is not None and res["extended"] is True


def test_live_rundown_scene_profiles_and_fine_settings_routes(settings):
    with TestClient(create_app(settings)) as client:
        # Unauthenticated check
        assert client.get("/api/v1/live/scene/profiles").status_code == 401
        assert client.get("/api/v1/live/rundown").status_code == 401
        assert client.get("/api/v1/live/fine-settings").status_code == 401

        client.post("/api/v1/system/auth/overlay", json={"token": settings.overlay_token}).raise_for_status()
        client.post("/api/v1/system/auth/login", json={"token": settings.admin_token}).raise_for_status()

        # Scene profiles + dual layout PUT/GET/default/view
        profiles = client.get("/api/v1/live/scene/profiles").raise_for_status().json()
        assert profiles["active_layout"] == "chat"
        assert profiles["profiles"]["chat"]["background"] == "ocean"
        assert profiles["profiles"]["game"]["background"] == "transparent"

        game_scene = client.get("/api/v1/live/scene/default?layout=game").raise_for_status().json()
        game_scene["title"] = "定制游戏HUD"
        client.put("/api/v1/live/scene?layout=game", json=game_scene).raise_for_status()

        # View respects ?layout=game even while active_layout is chat
        view_game = client.get("/api/v1/live/view?layout=game", headers={"Authorization": ""}).raise_for_status().json()
        assert view_game["scene"]["title"] == "定制游戏HUD"
        assert view_game["scene_layout"]["active_layout"] == "chat"

        # Toggle layout & lock
        layout_res = client.post("/api/v1/live/scene/layout", json={"layout": "game", "locked": True}).raise_for_status().json()
        assert layout_res["active_layout"] == "game" and layout_res["layout_locked"] is True
        client.post("/api/v1/live/scene/layout", json={"layout": "chat", "locked": False}).raise_for_status()

        # Fine settings GET/PUT
        fine = client.get("/api/v1/live/fine-settings").raise_for_status().json()
        assert set(fine) == {"cold_director", "overtime", "draw_and_guess", "turtle_soup"}
        updated_fine = client.put(
            "/api/v1/live/fine-settings",
            json={
                "cold_director": {"tier1_delay_sec": 60},
                "overtime": {"gift_coin_multiplier": 2.0},
                "draw_and_guess": {"hint_l2_ratio": 0.4, "duration_sec": 90},
                "turtle_soup": {"max_questions": 25},
            },
        ).raise_for_status().json()
        assert updated_fine["cold_director"]["tier1_delay_sec"] == 60.0
        assert updated_fine["overtime"]["gift_coin_multiplier"] == 2.0
        assert updated_fine["draw_and_guess"]["duration_sec"] == 90
        assert updated_fine["turtle_soup"]["max_questions"] == 25

        # Rundown CRUD + live control + vote flow
        client.post("/api/v1/live/start").raise_for_status()
        rd = client.get("/api/v1/live/rundown").raise_for_status().json()
        assert len(rd["presets"]) == 3 and len(rd["game_catalog"]) == 6

        # Apply preset & save custom plan
        preset_plan = rd["presets"][1]["plan"]
        saved_rd = client.put("/api/v1/live/rundown", json=preset_plan).raise_for_status().json()
        assert saved_rd["plan"]["name"] == "游戏实机+互动→闲聊"

        started_rd = client.post("/api/v1/live/rundown/start", json={}).raise_for_status().json()
        assert started_rd["state"]["active"] is True
        assert started_rd["state"]["current_segment"]["obs_layout"] == "chat"

        paused_rd = client.post("/api/v1/live/rundown/pause").raise_for_status().json()
        assert paused_rd["state"]["paused"] is True
        resumed_rd = client.post("/api/v1/live/rundown/resume").raise_for_status().json()
        assert resumed_rd["state"]["paused"] is False

        # Open vote -> verify shows/status exposes live poll counts -> cast vote -> settle
        voted_rd = client.post("/api/v1/live/rundown/vote-now").raise_for_status().json()
        assert voted_rd["poll"]["phase"] == "started"
        client.post("/api/v1/live/mock-event", json={"text": "1", "platform_user_id": "voter-1"}).raise_for_status()

        shows = client.get("/api/v1/live/shows/status", headers={"Authorization": ""}).raise_for_status().json()
        assert set(shows) >= {
            "character",
            "draw_and_guess",
            "turtle_soup",
            "roast_radio",
            "cold_director",
            "overtime",
            "poll",
            "rundown",
            "scene_layout",
        }
        assert shows["poll"]["total_votes"] == 1
        assert shows["rundown"]["active"] is True

        settled = client.post("/api/v1/live/rundown/settle-vote").raise_for_status().json()
        assert settled["fallback"] is False
        assert settled["state"]["current_segment"]["obs_layout"] == "game"

        # Jump, extend-chat, next, stop
        jumped = client.post("/api/v1/live/rundown/jump", json={"index": 0}).raise_for_status().json()
        assert jumped["state"]["current_index"] == 0
        extended = client.post("/api/v1/live/rundown/extend-chat", json={"minutes": 15}).raise_for_status().json()
        assert extended["state"]["remaining_seconds"] > 1500
        nxt = client.post("/api/v1/live/rundown/next", json={"force": True}).raise_for_status().json()
        assert nxt["state"]["current_index"] == 1
        stopped = client.post("/api/v1/live/rundown/stop").raise_for_status().json()
        assert stopped["state"]["active"] is False

        # Facade smoke check
        assert set(live_facade(client.app.state.runtime).shows_status()) >= {
            "poll",
            "rundown",
            "scene_layout",
        }


def test_rundown_custom_times_adjust_and_overrides(settings):
    with TestClient(create_app(settings)) as client:
        client.post("/api/v1/system/auth/login", json={"token": settings.admin_token}).raise_for_status()
        client.post("/api/v1/live/start").raise_for_status()

        # Cannot adjust time before rundown starts
        inactive_adj = client.post("/api/v1/live/rundown/adjust-time", json={"delta_minutes": 5})
        assert inactive_adj.status_code == 400

        rd = client.get("/api/v1/live/rundown").raise_for_status().json()
        custom_plan = rd["plan"]
        custom_plan["enabled_games"] = ["roast_radio", "song_party"]
        custom_plan["chat_entry"]["opening_chat_minutes"] = 1
        custom_plan["chat_entry"]["poll_duration_seconds"] = 20
        custom_plan["chat_entry"]["extend_chat_minutes"] = 8
        custom_plan["chat_entry"]["default_game_duration_minutes"] = 32
        custom_plan["segments"] = [
            {
                "id": "seg-1m",
                "title": "极速开场",
                "kind": "chat",
                "duration_minutes": 1,
                "obs_layout": "chat",
                "auto_start_show": False,
                "graceful_finish": False,
                "settings": {},
            },
            {
                "id": "seg-360m",
                "title": "超长耐久",
                "kind": "draw_and_guess",
                "duration_minutes": 360,
                "obs_layout": "game",
                "auto_start_show": False,
                "graceful_finish": False,
                "settings": {},
            },
        ]
        saved = client.put("/api/v1/live/rundown", json=custom_plan).raise_for_status().json()
        assert saved["plan"]["chat_entry"]["opening_chat_minutes"] == 1
        assert saved["plan"]["chat_entry"]["extend_chat_minutes"] == 8
        assert saved["plan"]["chat_entry"]["default_game_duration_minutes"] == 32
        assert saved["plan"]["segments"][1]["duration_minutes"] == 360

        started = client.post("/api/v1/live/rundown/start", json={}).raise_for_status().json()
        assert 45 <= started["state"]["remaining_seconds"] <= 65

        # Adjust +5m, -2m, and set_remaining_minutes=12 (also while paused)
        plus5 = client.post("/api/v1/live/rundown/adjust-time", json={"delta_minutes": 5}).raise_for_status().json()
        assert 340 <= plus5["state"]["remaining_seconds"] <= 365
        minus2 = client.post("/api/v1/live/rundown/adjust-time", json={"delta_minutes": -2}).raise_for_status().json()
        assert 220 <= minus2["state"]["remaining_seconds"] <= 245

        client.post("/api/v1/live/rundown/pause").raise_for_status()
        paused_set = client.post(
            "/api/v1/live/rundown/adjust-time", json={"set_remaining_minutes": 12}
        ).raise_for_status().json()
        assert paused_set["state"]["paused"] is True
        assert paused_set["state"]["remaining_seconds"] == pytest.approx(720.0, abs=1.0)
        client.post("/api/v1/live/rundown/resume").raise_for_status()

        # Custom extend-chat default (uses chat_entry.extend_chat_minutes=8 when omitted) & explicit override
        ext_default = client.post("/api/v1/live/rundown/extend-chat", json={}).raise_for_status().json()
        assert ext_default["state"]["remaining_seconds"] >= 720 + 8 * 60 - 5
        ext_explicit = client.post("/api/v1/live/rundown/extend-chat", json={"minutes": 3}).raise_for_status().json()
        assert ext_explicit["state"]["remaining_seconds"] >= 720 + 11 * 60 - 10

        # Custom vote-now duration_seconds override & voted game not in plan.segments uses default_game_duration_minutes (32m)
        voted = client.post("/api/v1/live/rundown/vote-now", json={"duration_seconds": 30}).raise_for_status().json()
        assert voted["poll"]["duration"] == pytest.approx(30.0)
        client.post("/api/v1/live/mock-event", json={"text": "1", "platform_user_id": "v-custom-1"}).raise_for_status()
        settled = client.post("/api/v1/live/rundown/settle-vote").raise_for_status().json()
        assert settled["state"]["current_segment"]["kind"] == "roast_radio"
        assert settled["state"]["current_segment"]["duration_minutes"] == 32

        # Chat vote winning uses extend_chat_minutes=8
        client.post("/api/v1/live/rundown/vote-now", json={"duration_seconds": 25}).raise_for_status()
        client.post("/api/v1/live/mock-event", json={"text": "继续聊", "platform_user_id": "v-custom-2"}).raise_for_status()
        settled_chat = client.post("/api/v1/live/rundown/settle-vote").raise_for_status().json()
        assert settled_chat["state"]["current_segment"]["kind"] == "chat"
        assert 7 * 60 + 50 <= settled_chat["state"]["remaining_seconds"] <= 8 * 60 + 2

        # #继续聊 barrage command uses extend_chat_minutes=8 (reset cooldown to simulate separate interval)
        client.app.state.runtime.live.rundown._last_chat_extend_at = 0.0
        client.post("/api/v1/live/mock-event", json={"text": "#继续聊", "platform_user_id": "v-custom-3"}).raise_for_status()
        after_barrage = client.get("/api/v1/live/rundown").raise_for_status().json()
        assert after_barrage["state"]["remaining_seconds"] >= 15 * 60 + 45
