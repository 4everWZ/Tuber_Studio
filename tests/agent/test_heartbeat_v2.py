"""Acceptance tests for Heartbeat v2: SQLite dedup, edge-triggered signals, unread cap, quiet hours, and policy API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from tests.support.operations_helpers import BV, ControlledPlatform
from tuber_studio.agent.heartbeat import HeartbeatService, is_in_quiet_hours
from tuber_studio.app import create_app
from tuber_studio.core.store import dump
from tuber_studio.operations.facade import heartbeat_pulse_snapshot


def _ts_taipei(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()


def _make_snapshot(
    *,
    account_id: str = "",
    control_state: str = "running",
    account_error: str | None = None,
    disc_error: str | None = None,
    read_cooldown_until: float | None = None,
    inbox_state: str = "running",
    inbox_pending: int = 0,
    dm_pending: int = 0,
    dm_pending_ids: list[str] | None = None,
    dm_latest_created_at: float = 0.0,
    dyn_pending: int = 0,
    dyn_pending_ids: list[str] | None = None,
    dyn_latest_created_at: float = 0.0,
    dyn_uncertain: int = 0,
    writes_uncertain: int = 0,
    hot_topics: list[dict] | None = None,
) -> dict:
    return {
        "account_id": account_id,
        "control_state": control_state,
        "account_error": account_error,
        "credentials_configured": True,
        "discovery": {
            "state": "running",
            "enabled": True,
            "error": disc_error,
            "read_cooldown_until": read_cooldown_until,
        },
        "inbox": {
            "state": inbox_state,
            "enabled": True,
            "pending_events": inbox_pending,
            "dm_enabled": True,
            "dm_error": None,
            "dm_pending": dm_pending,
            "dm_pending_ids": dm_pending_ids or [],
            "dm_latest_created_at": dm_latest_created_at,
        },
        "dynamics": {
            "enabled": True,
            "auto_publish": False,
            "pending_review": dyn_pending,
            "pending_ids": dyn_pending_ids or [],
            "pending_latest_created_at": dyn_latest_created_at,
            "uncertain": dyn_uncertain,
            "error": None,
        },
        "writes": {
            "uncertain_count": writes_uncertain,
            "uncertain_ids": [],
        },
        "hot_topics": hot_topics or [],
    }


def test_heartbeat_v2_restart_persistent_7d_dedup_and_zero_duplicate_research(settings):
    async def scenario():
        clock_now = [_ts_taipei(2026, 11, 25, 14, 0)]
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        store = runtime.store

        search_calls: list[str] = []

        async def fake_search(query: str):
            search_calls.append(query)
            return ["https://docs.example.com/topic"]

        async def fake_fetch(url: str):
            return {"url": url, "title": "本地 AI 吐槽实测", "excerpt": "关于本地 Jetson 视频吐槽的公开实测资料。"}

        hot = [{"bvid": BV, "title": "本地 Jetson 视频吐槽实测", "tags": ["Jetson"], "popular": True}]
        snap = _make_snapshot(hot_topics=hot)

        hb1 = HeartbeatService(
            runtime,
            store,
            clock=lambda: clock_now[0],
            web_search_fn=fake_search,
            web_fetch_fn=fake_fetch,
        )
        hb1.configure({"quiet_hours_start": "", "quiet_hours_end": "", "max_unreplied_pulses": 0})
        res1 = await hb1.tick(snapshot=snap)
        assert res1["pulse"] is not None
        assert len(search_calls) == 1
        assert "心跳巡检" not in res1["pulse"]["brief"]
        assert "刚完成一轮巡检" not in res1["pulse"]["brief"]

        # Simulate process restart with a fresh HeartbeatService on the same SQLite store.
        clock_now[0] += 4 * 3600.0
        hb2 = HeartbeatService(
            runtime,
            store,
            clock=lambda: clock_now[0],
            web_search_fn=fake_search,
            web_fetch_fn=fake_fetch,
        )
        res2 = await hb2.tick(snapshot=snap)
        assert res2["pulse"] is None
        assert res2["emitted"] == 0
        assert len(search_calls) == 1, "persisted 7d dedup must skip web_search_fn across restarts"
        assert any(item["reason"] == "persisted_dedup" for item in res2["suppressed"])
        assert any(s["reason"] == "persisted_dedup" for s in hb2.status()["recent_suppressions"])

        # After 7 days (604800s), the same topic becomes eligible again.
        clock_now[0] += 604800.0 + 10.0
        res3 = await hb2.tick(snapshot=snap)
        assert res3["pulse"] is not None
        assert len(search_calls) == 2

    asyncio.run(scenario())


def test_heartbeat_v2_edge_triggered_drafts_snapshot_metadata_and_fault_recurrence(settings):
    async def scenario():
        clock_now = [_ts_taipei(2026, 11, 25, 14, 0)]
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        store = runtime.store
        browsing = runtime.browsing
        char_id = store.character()["id"]

        # Seed real rows in dm_drafts, dynamic_posts, and account_writes (view operation_write_attempts).
        with store.db:
            store.db.execute(
                "INSERT INTO dm_drafts(id,platform,platform_user_id,display_name,source_kind,source_id,"
                "character_id,character_version,status,text,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (101, "bilibili", "u1", "观众A", "dm", 1, char_id, 1, "pending_review", "你好", 1000.0, 1000.0),
            )
            store.db.execute(
                "INSERT INTO dynamic_posts(id,decision_id,character_id,character_version,content,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (201, 1, char_id, 1, "一条动态草稿", "pending_review", 2000.0, 2000.0),
            )
            store.db.execute(
                "INSERT INTO account_writes(token,account_id,character_id,character_version,intent,channel,"
                "recipient,automatic,epoch,status,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("tok_uncertain_1", "", char_id, 1, "intent_1", "comments", "BV1xx", 1, 1, "uncertain", 3000.0),
            )

        real_snap = heartbeat_pulse_snapshot(browsing, store=store)
        assert real_snap["inbox"]["dm_pending_ids"] == ["101"]
        assert real_snap["inbox"]["dm_latest_created_at"] == 1000.0
        assert real_snap["dynamics"]["pending_ids"] == ["201"]
        assert real_snap["dynamics"]["pending_latest_created_at"] == 2000.0
        assert real_snap["writes"]["uncertain_ids"] == ["tok_uncertain_1"]
        assert "account_error" in real_snap and "credentials_configured" in real_snap

        hb = HeartbeatService(runtime, store, clock=lambda: clock_now[0])
        hb.configure({
            "pulse_cooldown_seconds": 0.0,
            "max_pulses_per_day": 0,
            "max_unreplied_pulses": 0,
            "quiet_hours_start": "",
            "quiet_hours_end": "",
        })

        # Tick 1: initial pending draft + uncertain write emit.
        snap1 = _make_snapshot(
            dyn_pending=1,
            dyn_pending_ids=["201"],
            dyn_latest_created_at=2000.0,
            dm_pending=1,
            dm_pending_ids=["101"],
            dm_latest_created_at=1000.0,
        )
        res1 = await hb.tick(snapshot=snap1)
        assert res1["pulse"] is not None
        assert "心跳巡检" not in res1["pulse"]["brief"]
        assert hb.status()["pending_summary"]["dynamics_pending_review"] == 1
        assert hb.status()["pending_summary"]["dm_pending"] == 1

        # Tick 2: unchanged stock suppresses with no_state_delta, never spamming chat.
        clock_now[0] += 600.0
        res2 = await hb.tick(snapshot=snap1)
        assert res2["pulse"] is None
        assert any(s["reason"] == "no_state_delta" for s in res2["suppressed"])

        # Tick 3: new dynamic draft arrives -> edge triggers!
        clock_now[0] += 600.0
        snap3 = _make_snapshot(
            dyn_pending=2,
            dyn_pending_ids=["201", "202"],
            dyn_latest_created_at=2500.0,
            dm_pending=1,
            dm_pending_ids=["101"],
            dm_latest_created_at=1000.0,
        )
        res3 = await hb.tick(snapshot=snap3)
        assert res3["pulse"] is not None

        # Tick 4: drafts cleared -> state saved cleanly with no pulse, badges drop to 0.
        clock_now[0] += 600.0
        snap4 = _make_snapshot()
        res4 = await hb.tick(snapshot=snap4)
        assert res4["pulse"] is None
        assert hb.status()["pending_summary"]["dynamics_pending_review"] == 0

        # Tick 5: urgent fault edge-triggers, stays silent while active, clears on recovery, fires again on recurrence.
        clock_now[0] += 60.0
        fault_snap = _make_snapshot(inbox_state="frozen")
        r_fault1 = await hb.tick(snapshot=fault_snap)
        assert r_fault1["pulse"] is not None and r_fault1["pulse"]["severity"] == "urgent"
        r_fault_repeat = await hb.tick(snapshot=fault_snap)
        assert r_fault_repeat["pulse"] is None
        await hb.tick(snapshot=_make_snapshot())  # recover
        r_fault_recur = await hb.tick(snapshot=fault_snap)
        assert r_fault_recur["pulse"] is not None and r_fault_recur["pulse"]["severity"] == "urgent"

    asyncio.run(scenario())


def test_heartbeat_v2_unread_cap_urgent_bypass_and_owner_reply_reset(settings):
    async def scenario():
        clock_now = [_ts_taipei(2026, 11, 25, 15, 0)]
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        store = runtime.store
        char = store.character()

        # Ensure operator_chat_turns table exists (chat transport creates it on chat setup).
        with store.db:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS operator_chat_turns ("
                "request_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, character_version INTEGER NOT NULL, "
                "message TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL)"
            )

        search_calls: list[str] = []

        async def fake_search(query: str):
            search_calls.append(query)
            return ["https://docs.example.com/ok"]

        async def fake_fetch(url: str):
            return {"url": url, "title": "测试热点资料", "excerpt": "一条公开热点资料摘要。"}

        hb = HeartbeatService(
            runtime,
            store,
            clock=lambda: clock_now[0],
            web_search_fn=fake_search,
            web_fetch_fn=fake_fetch,
        )
        hb.configure({
            "pulse_cooldown_seconds": 60.0,
            "max_pulses_per_day": 10,
            "max_unreplied_pulses": 1,
            "quiet_hours_start": "",
            "quiet_hours_end": "",
        })

        # Pulse 1 (non-urgent) brings unreplied_pulses to 1.
        r1 = await hb.tick(snapshot=_make_snapshot(dyn_pending=1, dyn_pending_ids=["d1"], dyn_latest_created_at=clock_now[0]))
        assert r1["pulse"] is not None
        assert hb.status()["unreplied_pulses"] == 1

        # Past cooldown, a hot_topic is blocked by unread_cap BEFORE research_topic (0 search calls, not deduped).
        clock_now[0] += 120.0
        hot_snap = _make_snapshot(
            dyn_pending=1,
            dyn_pending_ids=["d1"],
            dyn_latest_created_at=1000.0,
            hot_topics=[{"bvid": "BV1deferred", "title": "延期热点视频测试", "tags": ["测试"], "popular": True}],
        )
        r2 = await hb.tick(snapshot=hot_snap)
        assert r2["rate_limited"] == "unread_cap"
        assert r2["pulse"] is None
        assert len(search_calls) == 0

        # Urgent fault (account_error) penetrates unread_cap!
        clock_now[0] += 30.0
        r3 = await hb.tick(snapshot=_make_snapshot(account_error="401 登录过期"))
        assert r3["pulse"] is not None
        assert r3["pulse"]["severity"] == "urgent"

        # An assistant_only turn must NOT reset unreplied_pulses.
        clock_now[0] += 30.0
        with store.db:
            store.db.execute(
                "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                ("assistant-turn-1", char["id"], 1, "", dump({"assistant_only": True}), clock_now[0]),
            )
        assert hb.status()["unreplied_pulses"] == 1

        # An owner reply (non-empty message, assistant_only=0) resets unreplied_pulses and unblocks deferred hot_topic.
        clock_now[0] += 30.0
        with store.db:
            store.db.execute(
                "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                ("owner-turn-1", char["id"], 1, "收到，我看过了", dump({"assistant_only": False}), clock_now[0]),
            )
        assert hb.status()["unreplied_pulses"] == 0

        clock_now[0] += 120.0
        r4 = await hb.tick(snapshot=hot_snap)
        assert r4["pulse"] is not None
        assert len(search_calls) == 1

    asyncio.run(scenario())


def test_heartbeat_v2_quiet_hours_wraparound_urgent_bypass_and_empty_disable(settings):
    assert is_in_quiet_hours(_ts_taipei(2026, 11, 25, 23, 0), "Asia/Taipei", "23:00", "08:30") is True
    assert is_in_quiet_hours(_ts_taipei(2026, 11, 26, 2, 15), "Asia/Taipei", "23:00", "08:30") is True
    assert is_in_quiet_hours(_ts_taipei(2026, 11, 26, 8, 29), "Asia/Taipei", "23:00", "08:30") is True
    assert is_in_quiet_hours(_ts_taipei(2026, 11, 26, 8, 30), "Asia/Taipei", "23:00", "08:30") is False
    assert is_in_quiet_hours(_ts_taipei(2026, 11, 26, 10, 30), "Asia/Taipei", "09:00", "12:00") is True
    assert is_in_quiet_hours(_ts_taipei(2026, 11, 26, 2, 15), "Asia/Taipei", "", "08:30") is False

    async def scenario():
        clock_now = [_ts_taipei(2026, 11, 25, 23, 30)]
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        store = runtime.store

        search_calls: list[str] = []
        brief_calls: list[int] = []

        async def fake_search(query: str):
            search_calls.append(query)
            return ["https://docs.example.com/night"]

        async def fake_brief(messages, _char):
            brief_calls.append(len(messages))
            return "夜深了先简单跟你说个紧急情况。"

        hb = HeartbeatService(
            runtime,
            store,
            clock=lambda: clock_now[0],
            web_search_fn=fake_search,
            brief_generator=fake_brief,
        )
        assert hb.status()["in_quiet_hours"] is True

        # During 23:30 quiet hours, non-urgent hot_topic is blocked with zero search/LLM calls.
        hot_snap = _make_snapshot(
            hot_topics=[{"bvid": "BV1night", "title": "深夜热点视频", "tags": ["深夜"], "popular": True}]
        )
        r_night = await hb.tick(snapshot=hot_snap)
        assert r_night["rate_limited"] == "quiet_hours"
        assert r_night["pulse"] is None
        assert len(search_calls) == 0
        assert len(brief_calls) == 0

        # At 02:00 during quiet hours, urgent fault penetrates!
        clock_now[0] = _ts_taipei(2026, 11, 26, 2, 0)
        r_urgent = await hb.tick(snapshot=_make_snapshot(control_state="emergency"))
        assert r_urgent["pulse"] is not None
        assert r_urgent["pulse"]["severity"] == "urgent"
        assert len(brief_calls) == 1

        # Empty string disables quiet hours even at 02:15.
        hb.configure({"quiet_hours_start": "", "quiet_hours_end": ""})
        clock_now[0] = _ts_taipei(2026, 11, 26, 2, 15)
        assert hb.status()["in_quiet_hours"] is False
        r_open = await hb.tick(snapshot=hot_snap)
        assert r_open["pulse"] is not None
        assert len(search_calls) == 1

    asyncio.run(scenario())


def test_heartbeat_v2_zero_unlimited_and_http_policy_routes(settings):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        headers = {"Authorization": "Bearer " + settings.admin_token}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
            initial = (await client.get("/api/v1/system/heartbeat", headers=headers)).raise_for_status().json()
            policy = initial["policy"]
            assert policy["pulse_cooldown_seconds"] == 10800.0
            assert policy["max_pulses_per_day"] == 3
            assert policy["max_unreplied_pulses"] == 1
            assert policy["hot_topic_dedup_seconds"] == 604800.0
            assert policy["quiet_hours_start"] == "23:00"
            assert policy["quiet_hours_end"] == "08:30"
            assert {"in_quiet_hours", "unreplied_pulses", "pending_summary", "recent_suppressions"} <= set(
                initial["status"]
            )

            # Reject unknown field (422), invalid HH:MM (422), and empty patch (400).
            assert (await client.put("/api/v1/system/heartbeat", headers=headers, json={"unknown_key": 1})).status_code == 422
            assert (await client.put("/api/v1/system/heartbeat", headers=headers, json={"quiet_hours_start": "25:99"})).status_code == 422
            assert (await client.put("/api/v1/system/heartbeat", headers=headers, json={})).status_code == 400

            # Configure 0 = unlimited and disable quiet hours via HTTP PUT.
            updated = (
                (
                    await client.put(
                        "/api/v1/system/heartbeat",
                        headers=headers,
                        json={
                            "pulse_cooldown_seconds": 0,
                            "max_pulses_per_day": 0,
                            "max_unreplied_pulses": 0,
                            "quiet_hours_start": "",
                            "quiet_hours_end": "",
                        },
                    )
                )
                .raise_for_status()
                .json()
            )
            assert updated["policy"]["max_pulses_per_day"] == 0
            assert updated["policy"]["max_unreplied_pulses"] == 0
            assert updated["status"]["in_quiet_hours"] is False

            # Verify unlimited non-urgent emissions when both caps are 0.
            hb = app.state.runtime.heartbeat
            for idx in range(4):
                res = await hb.tick(
                    snapshot=_make_snapshot(
                        dyn_pending=idx + 1,
                        dyn_pending_ids=[f"dyn-{j}" for j in range(idx + 1)],
                        dyn_latest_created_at=1000.0 + idx,
                    )
                )
                assert res["pulse"] is not None
                assert res["rate_limited"] is None

    asyncio.run(scenario())
