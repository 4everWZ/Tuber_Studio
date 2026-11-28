"""Tests for OvertimeTheater, LiveSchedules, Phase 2 InteractiveRules, and Live API routes."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.core.models import Event
from tuber_studio.live.facade import live_facade
from tuber_studio.live.games.interactive_rules import InteractiveRulesService
from tuber_studio.live.session.overtime_theater import OvertimeTheater

FORBIDDEN_BEGGING_TERMS = (
    "怕死",
    "会死",
    "要死了",
    "停机会死",
    "余额不足",
    "没钱吃饭",
    "饿死",
    "求求",
    "可怜",
    "救救",
    "弃养",
)


def _assert_clean_copy(text: str | None) -> None:
    if not text:
        return
    for term in FORBIDDEN_BEGGING_TERMS:
        assert term not in text, f"Unexpected forbidden copy {term!r} in {text!r}"


def test_overtime_theater_gift_chat_cooldown_decay_and_cues():
    theater = OvertimeTheater(threshold=100.0, chat_cooldown_seconds=15.0, decay_per_minute=4.0)
    t0 = 1_764_300_000.0
    st = theater.start_window(
        base_duration_minutes=60,
        max_overtime_minutes=30,
        extension_step_minutes=15,
        now=t0,
    )
    assert st["active"] is True
    assert st["stage"] == "working"
    assert st["remaining_seconds"] == 3600
    _assert_clean_copy(st["last_event_line"])

    # Same spammer inside 15s cooldown only counts once
    r1 = theater.on_chat(user_id="spammer", display_name="水友A", text="好好好", now=t0 + 10)
    assert r1 is not None and r1["extended"] is False
    energy_after_first = r1["energy"]
    assert theater.on_chat(user_id="spammer", display_name="水友A", text="刷屏", now=t0 + 12) is None
    assert theater.on_chat(user_id="spammer", display_name="水友A", text="继续", now=t0 + 26) is not None
    assert theater.energy > energy_after_first

    # High-weight gift crosses threshold and extends by 15 minutes
    gift_res = theater.on_gift(
        user_id="whale-fan",
        display_name="热心船长",
        gift_name="小礼物",
        coins=70,
        now=t0 + 30,
    )
    assert gift_res is not None
    assert gift_res["extended"] is True
    assert gift_res["added_minutes"] == 15
    assert gift_res["overtime_minutes"] == 15
    assert gift_res["extensions_count"] == 1
    assert gift_res["status"]["scheduled_end_at"] == t0 + 3600 + 900
    _assert_clean_copy(gift_res["spoken_text"])

    # Lively multi-user chat can tip energy over threshold when close
    theater.energy = 95.0
    chat_tip = theater.on_chat(
        user_id="lively-viewer",
        display_name="气氛组",
        text="今晚聊太嗨了别下播！",
        now=t0 + 40,
    )
    assert chat_tip is not None
    assert chat_tip["extended"] is True
    assert theater.overtime_minutes == 30
    assert theater.extensions_count == 2
    _assert_clean_copy(chat_tip["spoken_text"])

    # Further gifts cannot exceed max_overtime_minutes=30
    capped = theater.on_gift(
        user_id="whale-fan",
        display_name="热心船长",
        gift_name="大火箭",
        coins=200,
        now=t0 + 50,
    )
    assert capped is not None
    assert capped["extended"] is False
    assert capped["capped"] is True
    assert theater.overtime_minutes == 30
    _assert_clean_copy(capped["spoken_text"])

    # Decay over 10 minutes + closing soon cue inside last 5 minutes
    closing = theater.tick(now=t0 + 3600 + 1800 - 180)
    assert closing is not None
    assert closing["kind"] == "closing_soon"
    assert theater.energy < 100.0
    _assert_clean_copy(closing["spoken_text"])

    # Window end cue
    ended = theater.tick(now=t0 + 3600 + 1800)
    assert ended is not None
    assert ended["kind"] == "window_ended"
    assert theater.active is False
    assert theater.stage == "ended"
    _assert_clean_copy(ended["spoken_text"])


@pytest.mark.asyncio
async def test_live_schedules_respects_feature_flag_and_staggers_operations(runtime):
    now = 1_764_300_000.0
    # 1. Respects features.live_enabled=False
    runtime.settings.data["features"]["live_enabled"] = False
    with pytest.raises(ValueError, match="live_enabled"):
        await runtime.live.live_schedules.create(
            {"kind": "duration", "duration_minutes": 30},
            now=now,
        )

    # 2. Enable live and start autonomous operations beforehand
    runtime.settings.data["features"]["live_enabled"] = True
    runtime.operations.configure({"enabled": True}, account_id="900", character=runtime.character)
    assert runtime.operations.policy()["enabled"] is True

    created = await runtime.live.live_schedules.create(
        {
            "kind": "duration",
            "duration_minutes": 30,
            "max_overtime_minutes": 60,
            "extension_step_minutes": 15,
            "auto_pause_operations": True,
            "label": "晚间直播档",
        },
        now=now,
    )
    assert created["status"] == "active"
    assert created["paused_operations"] is True
    assert runtime.live.state == "live"
    assert runtime.live.overtime_theater.active is True
    assert runtime.operations.policy()["enabled"] is False

    # 3. OvertimeTheater gift extension postpones live schedule end & keeps operations paused
    ext = runtime.live.overtime_theater.on_gift(
        user_id="fan1",
        display_name="观众1",
        gift_name="星光",
        coins=100,
        now=now + 60,
    )
    assert ext is not None and ext["extended"] is True
    mid_tick = await runtime.live.live_schedules.tick(now=now + 1800 + 10)
    assert mid_tick["active_schedule"] is not None
    assert mid_tick["active_schedule"]["end_at"] == now + 1800 + 900
    assert runtime.live.state == "live"
    assert runtime.operations.policy()["enabled"] is False

    # 4. Once extended deadline passes, live stops and operations resumes cleanly
    end_tick = await runtime.live.live_schedules.tick(now=now + 1800 + 900)
    assert created["id"] in end_tick["expired"]
    assert end_tick["active_schedule"] is None
    assert runtime.live.state == "stopped"
    assert runtime.live.overtime_theater.active is False
    assert runtime.operations.policy()["enabled"] is True


@pytest.mark.asyncio
async def test_live_schedules_daily_window_and_browsing_stagger(runtime):
    zone = ZoneInfo("Asia/Taipei")
    pre = dt.datetime(2026, 11, 27, 19, 50, tzinfo=zone).timestamp()  # Friday
    in_win = dt.datetime(2026, 11, 27, 20, 10, tzinfo=zone).timestamp()
    after_win = dt.datetime(2026, 11, 27, 21, 0, tzinfo=zone).timestamp()

    mock_browsing = SimpleNamespace(
        stop=AsyncMock(side_effect=lambda **_: runtime.operations.configure({"enabled": False})),
        set_enabled=AsyncMock(
            side_effect=lambda enabled: runtime.operations.configure(
                {"enabled": enabled}, account_id="900", character=runtime.character
            )
        ),
    )
    runtime.browsing = mock_browsing
    runtime.operations.configure({"enabled": True}, account_id="900", character=runtime.character)

    created = await runtime.live.live_schedules.create(
        {
            "kind": "daily_window",
            "start_local": "20:00",
            "end_local": "21:00",
            "weekdays": [4],
            "timezone": "Asia/Taipei",
            "auto_pause_operations": True,
        },
        now=pre,
    )
    assert created["status"] == "scheduled"
    assert created["start_time"] == "20:00"
    assert created["end_time"] == "21:00"
    assert runtime.operations.policy()["enabled"] is True

    await runtime.live.live_schedules.tick(now=in_win)
    assert runtime.live.state == "live"
    assert runtime.operations.policy()["enabled"] is False
    mock_browsing.stop.assert_awaited_once()

    await runtime.live.live_schedules.tick(now=after_win)
    assert runtime.live.state == "stopped"
    assert runtime.operations.policy()["enabled"] is True
    mock_browsing.set_enabled.assert_awaited_once_with(True)


def test_interactive_rules_phase2_triggers(store):
    svc = InteractiveRulesService(store)
    rules = svc.get_rules()
    assert rules.turtle_soup.danmaku_command == "#海龟汤"
    assert rules.roast_radio.debate_command == "#开辩论"
    assert rules.roast_radio.roast_command == "#吐槽"

    ok_ts, genre = svc.match_turtle_soup_start(Event(type="chat", text="#海龟汤 科幻"))
    assert ok_ts is True and genre == "科幻"
    assert svc.match_turtle_soup_hint(Event(type="chat", text="#提示")) is True

    ok_deb, deb = svc.match_roast_radio_debate(Event(type="chat", text="#开辩论 白饭 vs 面条"))
    assert ok_deb is True and deb is not None
    assert deb["side_a"] == "白饭" and deb["side_b"] == "面条"

    ok_roast, topic = svc.match_roast_radio_roast(Event(type="chat", text="#吐槽 深夜加班"))
    assert ok_roast is True and topic == "深夜加班"

    rules.draw_and_guess.trigger_mode = "danmaku_or_gift"
    svc.save_rules(rules)
    ok_dg, theme = svc.match_draw_and_guess_start(Event(type="chat", text="#你画我猜 美食"))
    assert ok_dg is True and theme == "美食"


@pytest.mark.asyncio
async def test_barrage_wiring_turtle_soup_roast_radio_and_overtime(runtime):
    runtime.settings.data["queue_limit"] = 10
    await runtime.control("start")
    runtime.live.overtime_theater.start_window(base_duration_minutes=60, max_overtime_minutes=60)

    # 1. Start Turtle Soup via danmaku and solve/QA
    ts_ev = await runtime.submit(Event(type="chat", display_name="汤友", text="#海龟汤"))
    assert ts_ev["status"] == "queued"
    assert runtime.live.turtle_soup.is_active() is True
    hint_ev = await runtime.submit(Event(type="chat", display_name="汤友", text="#提示"))
    assert hint_ev["status"] == "queued"
    assert runtime.live.turtle_soup.hint_index == 1
    runtime.live.turtle_soup.reveal_and_stop()

    # 2. Start Roast Radio debate via danmaku and vote with single-char "1"
    deb_ev = await runtime.submit(Event(type="chat", display_name="辩手", text="#开辩论 白饭 vs 泡面"))
    assert deb_ev["status"] == "queued"
    assert runtime.live.roast_radio.is_active() is True
    vote_res = await runtime.submit(
        Event(type="chat", platform_user_id="u1", display_name="投票员", text="1")
    )
    assert vote_res["status"] == "recorded"
    assert runtime.live.roast_radio.votes_a == 1

    # 3. Overtime gift extension through barrage submission
    ot_ev = await runtime.submit(
        Event(
            type="gift",
            platform_user_id="boss",
            display_name="大老板",
            text="加钟",
            metadata={"gift_name": "火箭", "price": 100},
        )
    )
    assert ot_ev["status"] == "queued"
    assert runtime.live.overtime_theater.overtime_minutes == 15
    assert live_facade(runtime).overtime_status()["extensions_count"] == 1


def test_live_phase2_rest_routes_contract(settings):
    with TestClient(create_app(settings)) as client:
        # Public overlay-safe shows status requires no admin auth
        pub_shows = client.get("/api/v1/live/shows/status")
        assert pub_shows.status_code == 200
        shows_body = pub_shows.json()
        assert set(shows_body) >= {
            "character",
            "draw_and_guess",
            "turtle_soup",
            "roast_radio",
            "cold_director",
            "overtime",
        }

        # Admin routes enforce auth
        assert client.get("/api/v1/live/schedules").status_code == 401
        client.post("/api/v1/system/auth/login", json={"token": settings.admin_token}).raise_for_status()

        # Draw & Guess 2.0 extended start
        dg_res = client.post(
            "/api/v1/live/draw-and-guess/start",
            json={"trigger_user": "测试主播", "duration_sec": 60, "theme": "美食", "use_llm": True, "mode": "aqua_draws"},
        )
        assert dg_res.status_code == 200
        assert dg_res.json()["status"] == "started"

        # Turtle Soup start / hint / reveal
        ts_start = client.post("/api/v1/live/shows/turtle-soup/start", json={"genre": "daily", "use_llm": False})
        assert ts_start.status_code == 200 and ts_start.json()["status"] == "started"
        ts_hint = client.post("/api/v1/live/shows/turtle-soup/hint")
        assert ts_hint.status_code == 200 and ts_hint.json()["hint_index"] == 1
        ts_reveal = client.post("/api/v1/live/shows/turtle-soup/reveal", json={})
        assert ts_reveal.status_code == 200 and ts_reveal.json()["status"] == "finished"

        # Roast Radio start-debate / roast / stop
        rr_deb = client.post(
            "/api/v1/live/shows/roast-radio/start-debate",
            json={"topic": "白饭配什么", "side_a": "海苔", "side_b": "老干妈"},
        )
        assert rr_deb.status_code == 200 and rr_deb.json()["mode"] == "debate"
        rr_roast = client.post("/api/v1/live/shows/roast-radio/roast", json={"topic": "测试锐评"})
        assert rr_roast.status_code == 200 and rr_roast.json()["spoken_text"]
        rr_stop = client.post("/api/v1/live/shows/roast-radio/stop")
        assert rr_stop.status_code == 200 and rr_stop.json()["status"] == "stopped"

        # Cold Director status & manual trigger
        cd_status = client.get("/api/v1/live/cold-director/status")
        assert cd_status.status_code == 200
        assert "idle_seconds" in cd_status.json()
        cd_trig = client.post("/api/v1/live/cold-director/trigger", json={"tier": 2})
        assert cd_trig.status_code == 200
        assert cd_trig.json()["spoken_text"]

        # Overtime start / status / stop
        ot_start = client.post(
            "/api/v1/live/overtime/start",
            json={"base_duration_minutes": 45, "max_overtime_minutes": 90, "extension_step_minutes": 15},
        )
        assert ot_start.status_code == 200 and ot_start.json()["active"] is True
        assert client.get("/api/v1/live/overtime").json()["base_duration_minutes"] == 45
        assert client.post("/api/v1/live/overtime/stop").json()["active"] is False

        # Live schedules CRUD (also verify POST /schedules isn't shadowed by /{action})
        sched_create = client.post(
            "/api/v1/live/schedules",
            json={
                "kind": "duration",
                "duration_minutes": 30,
                "label": "API排班测试",
                "auto_pause_operations": True,
            },
        )
        assert sched_create.status_code == 200
        sched_id = sched_create.json()["schedule"]["id"]
        sched_list = client.get("/api/v1/live/schedules").json()
        assert len(sched_list["schedules"]) == 1 and sched_list["active_schedule"]["id"] == sched_id

        sched_patch = client.patch(
            f"/api/v1/live/schedules/{sched_id}",
            json={"label": "更新后排班", "enabled": False},
        )
        assert sched_patch.status_code == 200
        assert sched_patch.json()["schedule"]["label"] == "更新后排班"

        sched_del = client.delete(f"/api/v1/live/schedules/{sched_id}")
        assert sched_del.status_code == 200
        assert sched_del.json()["deleted"] == sched_id
