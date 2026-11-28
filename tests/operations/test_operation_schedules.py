"""Tests for timed and time-window autonomous operation schedules and REST routes."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.support.operations_helpers import ControlledPlatform
from tests.support.operations_helpers import browsing as browsing
from tuber_studio.app import create_app
from tuber_studio.core.errors import SendLimitReached
from tuber_studio.core.store import dump
from tuber_studio.operations.channels.dynamics import publisher
from tuber_studio.operations.kernel import closeout_editorial


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
    editorial_inputs = []
    limit_until = _at(22, 3)
    quotas = []
    plan_attempts = []
    job = {"id": "recap-video", "status": "script_planned"}
    video_id, earlier_id = "BV1d8pU6xEZX", "BV1J7sm6YE6K"

    async def editorial_reply(_provider, messages, _schema, *, max_tokens):
        evidence = json.loads(messages[-1]["content"])
        editorial_inputs.append(evidence)
        assert "尚未发出的原稿" not in dump(evidence)
        assert "只在账号可见的旧回复" not in dump(evidence)
        items = {item["kind"]: item for item in reversed(evidence["items"])}
        return {
            "title": "盐水里能浮起什么",
            "opening": "本来我只是想随手点开一个盐水浮鸡蛋的小实验，结果翻进评论区一看，大家居然为了先放盐还是先搅匀吵出了两派。",
            "passages": [
                {
                    "source_id": f"watch:{earlier_id}",
                    "section": "deep_dive",
                    "chapter_title": "周初那场沉浮对比",
                    "template_hint": "UIShowcase",
                    "key_points": ["密度变化", "沉浮对照", "周初实验"],
                    "narration": "先把时间拨回周初我看过的一场沉浮实验。那期 UP 主把不同浓度的液体排成一排对照，只要密度差稍微拉开一点，物体的姿态立刻完全不同。当时我就记了一笔，这种看起来简单的物理直觉，真动手的时候最容易在细节上翻车，结果这周刷到盐水实验果然又对上了。",
                },
                {
                    "source_id": items["watched_video"]["source_id"],
                    "section": "deep_dive",
                    "chapter_title": "盐水实验里的关键一步",
                    "template_hint": "QuoteSpotlight",
                    "key_points": ["盐分变化", "鸡蛋浮起", "搅拌溶解"],
                    "narration": "回到本轮看的这条盐水实验，画面里能明显看到杯底的盐分慢慢化开之后，鸡蛋才一点点抬起头来。很多人只盯着最后浮起来那一刻，却漏掉了溶解速度本身就在决定浮力节奏。我盯着进度条来回拉了两遍，发现镜头其实把这层微妙的过渡拍得很老实。",
                },
                {
                    "source_id": items["public_reply"]["source_id"],
                    "section": "deep_dive",
                    "chapter_title": "评论区的步骤之争",
                    "template_hint": "QuoteSpotlight",
                    "key_points": ["加盐顺序", "楼里讨论", "步骤差异"],
                    "narration": "更好玩的是评论区，我顺手回复了楼里关于加盐顺序的疑问，结果回头再看才发现大家根本不是在争同一个结论，而是各自盯着完全不同的操作步骤。有人先倒水再慢慢补盐，有人干脆先把浓盐水调好再兑清水，难怪互相说服不了对方。",
                },
                {
                    "source_id": items["pending_comment"]["source_id"],
                    "section": "side_cut",
                    "chapter_title": "顺带本周切片",
                    "template_hint": "MemeCutaway",
                    "key_points": ["糖水对比", "待审新坑"],
                    "narration": "顺带记一个还在打磨的衍生坑，有观众提起换成糖水能不能做到差不多的效果，我还特意查了一下两种溶液的饱和度差异，先把这个对比方向留在了待审选题里，等后面有空再细捋成片。",
                },
            ],
            "closing": "其实日常做小实验最怕把顺序弄混，哪怕只差一步，出来的结果都会完全走样。你们平时自己动手的时候，是按步骤一步步试，还是喜欢先把变量一次拉满？评论区聊聊看。",
            "description": "从一个盐水实验翻进评论区，顺手把周初看过的沉浮对比也串了起来。",
            "dynamic_post": "刚又翻完一个盐水浮鸡蛋的小实验，结果评论区里大家为了加盐顺序能聊出两套完全不同的流派，连带着我想起周初看的那场沉浮对照。果然动手细节才是最容易让人上头的地方呀。",
            "dynamic_source_ids": [items["watched_video"]["source_id"], f"watch:{earlier_id}"],
        }

    async def dynamic_text(_, text, *, before_send):
        before_send()
        sent.append(text)
        return {"dynamic_id": "100001"}

    def check_quota(*_):
        quotas.append(True)
        if len(quotas) == 1:
            raise SendLimitReached("动态最小发布间隔", limit_until)

    def plan(brief, *, job_key):
        plan_attempts.append(job_key)
        if len(plan_attempts) == 1:
            raise ValueError("制片入口拒绝素材字段")
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
    monkeypatch.setattr(browsing.dynamics, "_quota", check_quota)
    monkeypatch.setattr(closeout_editorial, "budgeted_json", editorial_reply)
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
    store.execute("INSERT INTO browsing_rounds(id,account_id,character_id,preview,status,config,created_at) VALUES"
                  "('actual-watch',?,?,0,'completed','{}',?),('preview-watch',?,?,1,'completed','{}',?)",
                  (*scope[:2], _at(20, 0), *scope[:2], _at(20, 50)))
    target = {"bvid": video_id, "root": "201", "parent": "201", "recipient": "301",
              "source_discussion": {"source": {"comment_id": "201", "author_uid": "301", "author_name": "砂糖",
                                                "text": "换成糖也能让鸡蛋浮起来吗？"}}}
    store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,source_key,round_id,kind,status,receipt,target,text,created_at,updated_at) "
        "VALUES('reviewed-draft',?,?, 'reviewed-source','actual-watch','reply_public','pending_review',NULL,?,'尚未发出的原稿',?,?), "
        "('liked-video',?,?, 'liked-source','actual-watch','video_like','published',?,?,NULL,?,?)",
        (*scope[:2], dump(target), _at(20, 10), _at(20, 10), *scope[:2],
         dump({"code": 0, "action": "like", "video_id": video_id}), dump({"bvid": video_id}), _at(20, 20), _at(20, 20)),
    )
    store.execute(
        "INSERT INTO browsing_tasks(id,round_id,bvid,status) VALUES('viewed','actual-watch',?,'selected')", (video_id,),
    )
    store.execute("INSERT INTO browsing_watch_starts VALUES(?,?,'viewed',?,'2026-11-27',?)",
                  (browsing.account, char["id"], video_id, _at(20, 15)))
    for identity, title, round_id, task_kind, at in (
        (video_id, "盐水实验", "actual-watch", None, _at(20, 15)),
        (earlier_id, "本周较早的沉浮实验", "actual-watch", "new_video", _at(20, 15) - 3 * 86400),
        ("BV1HVsm6KEWp", "上周旧片", "actual-watch", "new_video", _at(20, 15) - 8 * 86400),
        ("BV1B6s26ZEZf", "本轮仅回访评论", "actual-watch", "comment_visit", _at(20, 40)),
        ("BV1RNs26QEsC", "只有预览的任务", "preview-watch", "new_video", _at(20, 50)),
    ):
        store.execute(
            "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state) "
            "VALUES(?,?,?,?,'[]',?,'viewed')",
            (*scope[:2], identity, dump({"metadata": {"bvid": identity, "title": title, "owner": "实验员"}}), at),
        )
        if task_kind:
            store.execute(
                "INSERT INTO browsing_tasks(id,round_id,bvid,status,payload,material_key,updated_at) "
                "VALUES(?,?,?,'selected',?,'preserved-material',?)",
                (identity, round_id, identity, dump({"task_kind": task_kind}), at),
            )
    store.execute(
        "INSERT INTO comments(id,video_id,comment_id,thread_id,platform_user_id,display_name,content) "
        "VALUES(1,?,'202','202','302','瓶子','加盐的顺序会影响结果吗？')", (video_id,),
    )
    store.execute(
        "INSERT INTO drafts(comment_id,text,status,account_id,character_id,character_version,receipt,visibility,updated_at) "
        "VALUES (1,'公开测试','published',?,?,?,?, 'public_visible', ?)"
        ", (2,'只在账号可见的旧回复','published',?,?,?,?, 'self_only', ?)",
        (*scope, dump({"rpid": "101"}), _at(20, 30), *scope, dump({"rpid": "102"}), _at(20, 30)),
    )
    await _tick_with_closeout(browsing, _at(21, 0))
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["dynamic_status"] == "pending", run["last_error"]
    dynamic_id = run["dynamic_id"]
    assert dynamic_id and browsing.schedules.closeouts._row(run["id"])["dynamic_retry_at"] == limit_until
    assert run["video_status"] == "scheduled"
    assert not sent and not clips
    await _tick_with_closeout(browsing, _at(22, 0))
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["video_status"] == "render_failed" and not run["video_job_id"]
    with pytest.raises(ValueError, match="原自动投稿授权时段"):
        browsing.schedules.closeouts.request_video(schedule["id"], run["id"], automatic=True, now=_at(23, 0))
    browsing.bilibili_account.verified_account_id = ""
    browsing.bilibili_account.verified_credential = None
    browsing.schedules.closeouts.request_video(schedule["id"], run["id"], automatic=True, now=_at(22, 1))
    await browsing.schedules.closeouts._worker
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["video_status"] == "submitted", run["last_error"]
    assert browsing.bilibili_account.display_account_id() == scope[0]
    assert plan_attempts == [run["id"], run["id"]]
    assert run["dynamic_status"] == "pending" and len(quotas) == 1
    assert clips[0][1]["public_comments"] == 1
    assert clips[0][1]["accepted_comments"] == 2
    assert clips[0][1]["public_dynamics"] == 0
    assert len(editorial_inputs) == 1
    assert clips[0][1]["watched_videos"] == 2
    assert clips[0][1]["current_run"]["watched_videos"] == 1
    assert clips[0][1]["review_comments"] == clips[0][1]["confirmed_likes"] == 1
    assert clips[0][1]["sources"][0]["title"] == "盐水实验"
    assert {item["title"] for item in clips[0][1]["editorial_items"]} == {"盐水实验", "本周较早的沉浮实验"}
    reply = next(item for item in clips[0][1]["editorial_items"] if item["kind"] == "public_reply")
    pending = next(item for item in clips[0][1]["editorial_items"] if item["kind"] == "pending_comment")
    assert (reply["status"], reply["target_author"], reply["target_excerpt"]) == (
        "public_visible", "瓶子", "加盐的顺序会影响结果吗？",
    )
    assert (pending["status"], pending["excerpt"], pending["target_excerpt"]) == (
        "pending_review", "", "换成糖也能让鸡蛋浮起来吗？",
    )
    await _tick_with_closeout(browsing, limit_until)
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["dynamic_status"] == "published", run["last_error"]
    assert run["dynamic_id"] == dynamic_id and len(editorial_inputs) == 1
    assert sent
    assert "待审" not in sent[0]
    assert "收到回执" not in sent[0] and "公开页读回" not in sent[0] and "运营收尾" not in sent[0]
    assert "《盐水实验》" in sent[0] and "《本周较早的沉浮实验》" in sent[0]
    assert f"https://www.bilibili.com/video/{video_id}" in sent[0]
    assert f"https://www.bilibili.com/video/{earlier_id}" in sent[0]
    assert f"https://www.bilibili.com/video/{video_id}#reply101" not in sent[0]
    assert "尚未发出的原稿" not in sent[0] and "只在账号可见的旧回复" not in sent[0]
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


async def test_empty_closeout_skips_dynamic_and_rejects_pending_or_receipt_copy(browsing, monkeypatch):
    await _prepare_ready(browsing)
    browsing.store.set("dynamic_policy", {"enabled": True, "auto_publish": True})
    called = []

    async def fail_if_called(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("empty closeout should not compose or publish")

    monkeypatch.setattr(closeout_editorial, "budgeted_json", fail_if_called)
    monkeypatch.setattr(publisher, "publish_text", fail_if_called)

    schedule = await browsing.schedules.create({
        "kind": "duration", "duration_minutes": 30, "scope_discovery": False,
        "closeout": {"dynamic_mode": "automatic", "video_mode": "off"},
    }, now=_at(18, 0))
    await _tick_with_closeout(browsing, _at(18, 30))
    run = browsing.schedules.closeouts.list(schedule["id"])[0]
    assert run["dynamic_status"] == "skipped"
    assert run["dynamic_id"] is None
    assert not called

    # Pending-only drafts without any watched videos or confirmed public interactions also skip.
    schedule2 = await browsing.schedules.create({
        "kind": "duration", "duration_minutes": 30, "scope_discovery": False,
        "closeout": {"dynamic_mode": "automatic", "video_mode": "off"},
    }, now=_at(19, 0))
    char = browsing.runtime.character
    browsing.store.execute(
        "INSERT INTO browsing_rounds(id,account_id,character_id,preview,status,config,created_at) VALUES"
        "('pending-only-round',?,?,0,'completed','{}',?)",
        (browsing.account, char["id"], _at(19, 5)),
    )
    browsing.store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,source_key,round_id,kind,status,receipt,target,text,created_at,updated_at) "
        "VALUES('only-draft',?,?, 'draft-key','pending-only-round','reply_public','pending_review',NULL,?,'未审草稿',?,?)",
        (browsing.account, char["id"], dump({"bvid": "BV1d8pU6xEZX", "parent": "99", "recipient": "1"}), _at(19, 10), _at(19, 10)),
    )
    await _tick_with_closeout(browsing, _at(19, 30))
    run2 = browsing.schedules.closeouts.list(schedule2["id"])[0]
    assert run2["dynamic_status"] == "skipped"
    assert run2["dynamic_id"] is None
    assert not called

    # CloseoutEditorial rejects banned receipt/pending phrases and fallback drafter rejects empty runs.
    with pytest.raises(ValueError, match="内部审核或回执"):
        closeout_editorial.CloseoutEditorial.model_validate({
            "title": "标题",
            "opening": "这是一个足够长的开场白用来验证文案约束。",
            "passages": [
                {"source_id": "s1", "section": "deep_dive", "chapter_title": "第一章", "narration": "这里展开第一段足够长的深聊口播内容，确保满足单段最小字数要求并能顺利通过基础校验。" * 2},
                {"source_id": "s2", "section": "deep_dive", "chapter_title": "第二章", "narration": "这里展开第二段足够长的深聊口播内容，确保满足单段最小字数要求并能顺利通过基础校验。" * 2},
                {"source_id": "s3", "section": "side_cut", "chapter_title": "切片", "narration": "这里是一段足够长的本周切片口播内容，用于凑齐整期复盘所需的字数下限与段落结构要求。" * 2},
            ],
            "closing": "最后留个问题给大家，欢迎在评论区一起慢慢讨论吧。",
            "description": "第一人称简介。",
            "dynamic_post": "待审 · 这条动态今天收到回执啦。",
            "dynamic_source_ids": ["s1"],
        })
    with pytest.raises(ValueError, match="没有可分享"):
        browsing.dynamics.prepare_schedule_closeout(
            {"id": "empty-run", "account_id": browsing.account},
            {"period_label": "11月27日", "watched_videos": 0, "public_comments": 0, "public_dynamics": 0, "sources": []},
            char,
        )


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
