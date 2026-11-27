"""Tests for timed and time-window autonomous operation schedules and REST routes."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tests.support.operations_helpers import ControlledPlatform
from tests.support.operations_helpers import browsing as browsing
from tuber_studio.app import create_app
from tuber_studio.core.store import dump
from tuber_studio.operations.channels.dynamics import publisher


async def _prepare_ready(svc):
    svc.bilibili_account.credentials = {"sessdata": "sess", "csrf": "csrf"}
    await svc.configure(
        {
            "discovery_enabled": False,
            "inbox_enabled": True,
            "top_level_enabled": True,
            "top_level_auto_publish": False,
            "business_timezone": "Asia/Taipei",
        }
    )


async def test_duration_schedule_activates_and_auto_stops_without_touching_publish_mode(browsing):
    await _prepare_ready(browsing)
    now = 1_764_200_000.0

    created = await browsing.schedules.create(
        {
            "kind": "duration",
            "duration_minutes": 60,
            "label": "Aqua 刷 1 小时",
            "scope_discovery": True,
            "scope_inbox": True,
        },
        now=now,
    )
    assert created["status"] == "active"
    assert created["end_at"] == now + 3600
    assert browsing.operations.policy()["enabled"] is True
    assert browsing.policy()["discovery_enabled"] is True
    assert browsing.policy()["inbox_enabled"] is True
    # Must never escalate channel publish mode
    assert browsing.policy()["top_level_auto_publish"] is False

    status_mid = browsing.status()
    assert status_mid["active_schedule"]["id"] == created["id"]

    # Before deadline: stays active
    await browsing.schedules.tick(now=now + 1800)
    assert browsing.operations.policy()["enabled"] is True
    assert browsing.schedules.active(now=now + 1800) is not None

    # At deadline: auto-stops and marks completed
    tick_end = await browsing.schedules.tick(now=now + 3600)
    assert created["id"] in tick_end["expired"]
    assert tick_end["active_schedule"] is None
    assert browsing.operations.policy()["enabled"] is False
    items = browsing.schedules.list(now=now + 3600)
    assert items[0]["status"] == "completed"
    assert items[0]["enabled"] is False


async def test_once_window_starts_at_boundary_and_stops_at_end(browsing):
    await _prepare_ready(browsing)
    zone = ZoneInfo("Asia/Taipei")
    start_dt = dt.datetime(2026, 11, 27, 20, 0, tzinfo=zone)
    end_dt = dt.datetime(2026, 11, 27, 22, 0, tzinfo=zone)
    before = start_dt.timestamp() - 300

    created = await browsing.schedules.create(
        {
            "kind": "once_window",
            "start_local": "2026-11-27 20:00",
            "end_local": "2026-11-27 22:00",
            "timezone": "Asia/Taipei",
            "scope_discovery": True,
            "scope_inbox": False,
        },
        now=before,
    )
    assert created["status"] == "scheduled"
    assert browsing.operations.policy()["enabled"] is False

    await browsing.schedules.tick(now=before + 60)
    assert browsing.operations.policy()["enabled"] is False

    # Window opens
    await browsing.schedules.tick(now=start_dt.timestamp() + 10)
    assert browsing.operations.policy()["enabled"] is True
    assert browsing.schedules.active(now=start_dt.timestamp() + 10)["id"] == created["id"]

    # Window ends
    await browsing.schedules.tick(now=end_dt.timestamp())
    assert browsing.operations.policy()["enabled"] is False
    assert browsing.schedules.list(now=end_dt.timestamp())[0]["status"] == "completed"


async def test_daily_window_overnight_weekdays_and_manual_stop_respect(browsing):
    await _prepare_ready(browsing)
    zone = ZoneInfo("Asia/Taipei")
    # 2026-11-27 is Friday (weekday() == 4)
    friday_pre = dt.datetime(2026, 11, 27, 22, 50, tzinfo=zone).timestamp()
    friday_in = dt.datetime(2026, 11, 27, 23, 15, tzinfo=zone).timestamp()
    sat_still_in = dt.datetime(2026, 11, 28, 0, 30, tzinfo=zone).timestamp()
    sat_end = dt.datetime(2026, 11, 28, 1, 0, tzinfo=zone).timestamp()
    next_friday_in = dt.datetime(2026, 12, 4, 23, 5, tzinfo=zone).timestamp()

    created = await browsing.schedules.create(
        {
            "kind": "daily_window",
            "start_time": "23:00",
            "end_time": "01:00",
            "weekdays": [4],  # Friday only -> overnight into Saturday 01:00
            "timezone": "Asia/Taipei",
        },
        now=friday_pre,
    )
    assert created["status"] == "scheduled"

    await browsing.schedules.tick(now=friday_in)
    assert browsing.operations.policy()["enabled"] is True
    assert browsing.schedules.active(now=friday_in)["id"] == created["id"]

    # Still active past midnight inside Friday's overnight window
    await browsing.schedules.tick(now=sat_still_in)
    assert browsing.operations.policy()["enabled"] is True

    # Operator manually stops early -> does NOT immediately re-enable on next tick same night
    await browsing.stop(emergency=False)
    assert browsing.operations.policy()["enabled"] is False
    await browsing.schedules.tick(now=sat_still_in + 60)
    assert browsing.operations.policy()["enabled"] is False
    assert browsing.schedules.list(now=sat_still_in + 60)[0]["status"] == "scheduled"

    # Saturday night (weekday 5) is excluded
    sat_night = dt.datetime(2026, 11, 28, 23, 10, tzinfo=zone).timestamp()
    await browsing.schedules.tick(now=sat_night)
    assert browsing.operations.policy()["enabled"] is False

    # Past Friday's end boundary cleanly
    await browsing.schedules.tick(now=sat_end)
    assert browsing.operations.policy()["enabled"] is False

    # Next Friday starts cleanly again
    await browsing.schedules.tick(now=next_friday_in)
    assert browsing.operations.policy()["enabled"] is True


async def test_emergency_stop_blocks_and_cancels_active_duration_schedule(browsing):
    await _prepare_ready(browsing)
    now = 1_764_200_000.0
    created = await browsing.schedules.create({"kind": "duration", "duration_minutes": 30}, now=now)
    assert created["status"] == "active"

    await browsing.stop(emergency=True)
    assert browsing.operations.policy()["emergency"] is True
    items = browsing.schedules.list(now=now + 10)
    assert items[0]["status"] == "cancelled"
    assert items[0]["enabled"] is False

    # Future daily schedule is blocked while emergency=True
    daily = await browsing.schedules.create(
        {
            "kind": "daily_window",
            "start_time": "09:00",
            "end_time": "10:00",
            "timezone": "Asia/Taipei",
        },
        now=dt.datetime(2026, 11, 27, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp(),
    )
    in_win = dt.datetime(2026, 11, 27, 9, 10, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    await browsing.schedules.tick(now=in_win)
    assert browsing.operations.policy()["enabled"] is False
    row = next(item for item in browsing.schedules.list(now=in_win) if item["id"] == daily["id"])
    assert "紧急停止" in (row["last_error"] or "")


async def test_restart_recovery_resumes_unexpired_and_retires_expired_schedule(browsing):
    await _prepare_ready(browsing)
    now = 1_764_200_000.0
    created = await browsing.schedules.create({"kind": "duration", "duration_minutes": 60}, now=now)
    assert browsing.operations.policy()["enabled"] is True

    # Simulate cold container restart where operations_policy.enabled resets to False
    browsing.operations.configure({"enabled": False})
    assert browsing.operations.policy()["enabled"] is False

    # Restart still inside the 60m window -> automatically resumes remaining window
    await browsing.schedules.tick(now=now + 600)
    assert browsing.operations.policy()["enabled"] is True
    assert browsing.schedules.active(now=now + 600)["id"] == created["id"]

    # Simulate container restarting AFTER deadline expired
    browsing.operations.configure({"enabled": False})
    await browsing.schedules.tick(now=now + 4000)
    assert browsing.operations.policy()["enabled"] is False
    assert browsing.schedules.list(now=now + 4000)[0]["status"] == "completed"


def _at(hour, minute):
    return dt.datetime(2026, 11, 27, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()


async def _tick_with_closeout(browsing, now):
    await browsing.schedules.tick(now=now)
    worker = browsing.schedules.closeouts._worker
    if worker:
        await worker


async def test_natural_closeout_publishes_one_dynamic_and_video_in_its_own_window(browsing, monkeypatch):
    await _prepare_ready(browsing)
    store = browsing.store
    store.set("dynamic_policy", {"enabled": True, "auto_publish": True})
    sent = []
    clips = []
    job = {"id": "recap-video", "status": "script_planned"}

    async def dynamic_text(_, text, *, before_send):
        before_send()
        sent.append(text)
        return {"dynamic_id": "100001"}

    def plan(brief, *, job_key):
        clips.append(("plan", brief, job_key))
        return {"job_id": job["id"]}

    async def generate(_):
        job["status"] = "project_ready"

    async def render(_):
        job["status"] = "ready_for_review"
        return {"ok": True}

    async def publish(_, *, scheduled_guard):
        scheduled_guard()
        assert not browsing.operations.policy()["enabled"]
        clips.append(("publish", job["id"]))
        return {"status": "published", "platform_state": "submitted"}

    async def refresh(_):
        return {"status": "published", "platform_state": "public_visible"}

    monkeypatch.setattr(publisher, "publish_text", dynamic_text)
    browsing.runtime.remotion_skill = SimpleNamespace(
        status=dict, get_job=lambda _: job, plan_operations_recap=plan, generate_remotion_project=generate,
        render_remotion_video=render, stage_or_publish_bilibili_video=publish, refresh_video_publication=refresh,
    )
    schedule = await browsing.schedules.create({
        "kind": "duration", "duration_minutes": 60, "scope_discovery": False,
        "closeout": {"dynamic_mode": "automatic", "video_mode": "automatic_publish",
                     "video_start": "22:00", "video_end": "23:00"},
    }, now=_at(20, 0))
    char = browsing.runtime.character
    scope = (browsing.account, char["id"], char["version"])
    store.execute("INSERT INTO browsing_rounds(id,preview,status) VALUES('actual-watch',0,'completed')")
    video_id = "BV1d8pU6xEZX"
    store.execute(
        "INSERT INTO browsing_tasks(id,round_id,bvid,status) VALUES('viewed','actual-watch',?,'selected')", (video_id,),
    )
    store.execute("INSERT INTO browsing_watch_starts VALUES(?,?,'viewed',?,'2026-11-27',?)",
                  (browsing.account, char["id"], video_id, _at(20, 15)))
    store.execute(
        "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state) "
        "VALUES(?,?,?,?,'[]',?,'viewed')",
        (browsing.account, char["id"], video_id, dump({"metadata": {"title": "盐水实验"}}), _at(20, 30)),
    )
    store.execute(
        "INSERT INTO drafts(comment_id,text,status,account_id,character_id,character_version,receipt,visibility,updated_at) "
        "VALUES (1,'公开测试','published',?,?,?,?, 'public_visible', ?)"
        ", (2,'无公开回读','published',?,?,?,?, 'self_only', ?)",
        (*scope, dump({"rpid": "101"}), _at(20, 30), *scope, dump({"rpid": "102"}), _at(20, 30)),
    )
    await _tick_with_closeout(browsing, _at(21, 0))
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["dynamic_status"] == "published", run["last_error"]
    assert run["video_status"] == "scheduled"
    assert sent and "2 条收到回执，其中 1 条" in sent[0]
    assert not clips
    await _tick_with_closeout(browsing, _at(22, 0))
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["video_status"] == "submitted", run["last_error"]
    assert clips[0][1]["public_comments"] == 1
    assert clips[0][1]["accepted_comments"] == 2
    assert clips[0][1]["public_dynamics"] == 0
    assert clips[0][1]["watched_videos"] == 1
    assert clips[0][1]["sources"][0]["title"] == "盐水实验"
    await _tick_with_closeout(browsing, _at(22, 6))
    await _tick_with_closeout(browsing, _at(22, 7))
    assert browsing.schedules.closeouts.list(schedule["id"])[0]["video_status"] == "published"
    assert [event[0] for event in clips] == ["plan", "publish"]
    assert len(sent) == 1


async def test_video_window_rolls_after_overnight_operations_and_does_not_submit_late(browsing):
    await _prepare_ready(browsing)
    schedule = await browsing.schedules.create({
        "kind": "duration", "duration_minutes": 90, "scope_discovery": False,
        "closeout": {"video_mode": "automatic_publish", "video_start": "23:00", "video_end": "00:30"},
    }, now=_at(23, 0))
    await _tick_with_closeout(browsing, _at(0, 30) + 86400)
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["video_start_at"] == _at(23, 0) + 86400
    assert run["video_end_at"] == _at(0, 30) + 2 * 86400
    await _tick_with_closeout(browsing, run["video_end_at"])
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["video_status"] == "window_missed"
    assert run["video_job_id"] is None


def test_operation_schedule_rest_routes_crud(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        res_create = client.post(
            "/api/v1/operations/automation/schedules",
            json={"kind": "duration", "duration_minutes": 60, "label": "Aqua 1h"},
        )
        assert res_create.status_code == 200
        body = res_create.json()
        sid = body["schedule"]["id"]
        assert body["schedule"]["status"] == "active"
        assert body["status"]["active_schedule"]["id"] == sid
        assert body["status"]["policy"]["enabled"] is True

        res_list = client.get("/api/v1/operations/automation/schedules")
        assert res_list.status_code == 200
        assert res_list.json()["active_schedule"]["id"] == sid
        assert len(res_list.json()["items"]) == 1

        res_patch = client.patch(
            f"/api/v1/operations/automation/schedules/{sid}",
            json={"action": "cancel"},
        )
        assert res_patch.status_code == 200
        assert res_patch.json()["schedule"]["status"] == "cancelled"
        assert res_patch.json()["status"]["state"] in {"off", "draining"}
        for _ in range(40):
            cur = client.get("/api/v1/operations/automation").json()
            if cur["state"] == "off":
                break
            import time

            time.sleep(0.05)
        assert cur["policy"]["enabled"] is False

        res_daily = client.post(
            "/api/v1/operations/automation/schedules",
            json={"kind": "daily_window", "start_time": "20:00", "end_time": "22:00"},
        )
        assert res_daily.status_code == 200
        daily_id = res_daily.json()["schedule"]["id"]

        res_del = client.delete(f"/api/v1/operations/automation/schedules/{daily_id}")
        assert res_del.status_code == 200
        assert res_del.json()["deleted"] == daily_id
        assert client.get("/api/v1/operations/automation/schedules").json()["items"][0]["status"] == "cancelled"
