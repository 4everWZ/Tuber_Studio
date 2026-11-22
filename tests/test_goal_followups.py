import asyncio
from datetime import datetime
from itertools import count
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from tuber_studio.agent import goal_followups
from tuber_studio.agent.goal_followups import FollowupGrant, GoalFollowups
from tuber_studio.agent.task_api import register_task_api
from tuber_studio.agent.task_identity import TaskIdentity
from tuber_studio.agent.task_runtime import TaskRuntime, Tool
from tuber_studio.core.models import StrictModel
from tuber_studio.core.store import Store
from tuber_studio.infra.web_client import fetch as web_fetch


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def runtime(store):
    return TaskRuntime(store, TaskIdentity(store, lambda: "whale", lambda: [
        store.get("account", ""), "whale", 1, None,
    ]), None)


def root(tasks):
    identity = str(uuid4())
    task = tasks.create(identity, "私聊原话，不属于周期授权")
    tasks.workspace.promote(task)
    tasks._save(task, status="cancelled")
    return identity


def schedule(tasks, **overrides):
    return FollowupGrant.model_validate({"epoch": tasks.scope()[-1], "cadence": "daily", "local_time": "09:00",
                                         "timezone": "Asia/Taipei", "pages": ["https://example.org/news"]} | overrides)


async def test_restart_coalesces_to_latest_slot_and_stores_private_receipts(tmp_path, monkeypatch):
    path = tmp_path / "followups.db"
    store = Store(path, [])
    tasks = runtime(store)
    identity = root(tasks)
    service = GoalFollowups(tasks)
    start = stamp("2026-11-18T09:00:00+08:00")
    assert service.configure(identity, schedule(tasks), start)["next_due"] == start + 86400
    await service.tick(start)
    store.close()
    store = Store(path, [])
    tasks = runtime(store)
    service = GoalFollowups(tasks)
    requests = []

    async def fetch(page, *, exact_source):
        assert exact_source
        requests.append(page)
        return {"url": page, "excerpt": "来源正文"}

    monkeypatch.setattr(goal_followups, "fetch_excerpt", fetch)
    missed = start + 4 * 86400 + 60
    await service.tick(missed)
    await service.tick(missed)
    child, = [run for run in tasks.workspace.runs(identity) if run["id"] != identity]
    task = tasks.current(child["id"])
    assert task["due"] == missed - 60 and task["status"] == "completed"
    assert requests == ["https://example.org/news"]
    assert "来源正文" in tasks.workspace.latest(task["id"])["body"]
    assert "私聊原话" not in str(task)
    assert service.get(identity)["next_due"] == start + 5 * 86400
    service.disable(identity)
    service.configure(identity, schedule(tasks), missed - 120)
    await service.tick(missed)
    assert len(requests) == 1
    store.close()


@pytest.mark.parametrize("invalidate", ["disable", "close", "epoch", "replace"])
async def test_inflight_revocation_discards_result_and_never_reads_next_source(store, monkeypatch, invalidate):
    tasks = runtime(store)
    identity = root(tasks)
    service = GoalFollowups(tasks)
    started, finish = asyncio.Event(), asyncio.Event()
    requests = []

    async def status(request, args):
        assert request.goal == "" and request.receipts == () and request.approval is None
        requests.append("live_status")
        started.set()
        await finish.wait()
        return {"state": "stopped"}

    async def forbidden(*args, **kwargs):
        pytest.fail("已停用的授权不得再读取其他来源")

    tasks.register("live_status", Tool("status", StrictModel, status))
    monkeypatch.setattr(goal_followups, "fetch_excerpt", forbidden)
    start = stamp("2026-11-18T08:59:00+08:00")
    service.configure(identity, schedule(tasks, states=["live_status"]), start)
    reading = asyncio.create_task(service.tick(start + 60))
    await asyncio.wait_for(started.wait(), 2)
    if invalidate == "close":
        tasks.workspace.close(identity, tasks.executable)
    elif invalidate == "disable":
        service.disable(identity)
    elif invalidate == "epoch":
        store.set("account", "new")
        tasks.scope()
        store.set("account", "")
    else:
        service.configure(identity, schedule(tasks, states=["live_status"], pages=[], local_time="12:00"), start + 61)
    finish.set()
    await reading
    await service.tick(start + 62)
    assert requests == ["live_status"] and len(tasks.workspace.runs(identity)) == 1
    assert service.get(identity)["enabled"] == (invalidate == "replace")


def test_local_weekly_time_skips_nonexistent_slot_and_does_not_repeat_fold(store):
    tasks = runtime(store)
    weekly = schedule(tasks, cadence="weekly", weekday=6, timezone="America/New_York", local_time="02:30")
    assert weekly.slot(stamp("2026-03-07T12:00:00-05:00"), future=True) == stamp("2026-03-15T02:30:00-04:00")
    folded = schedule(tasks, cadence="weekly", weekday=6, timezone="America/New_York", local_time="01:30")
    assert folded.slot(stamp("2026-11-01T01:35:00-05:00"), future=False) == stamp("2026-11-01T01:30:00-04:00")
    assert folded.slot(stamp("2026-11-01T01:35:00-04:00"), future=True) == stamp("2026-11-08T01:30:00-05:00")


async def test_only_explicit_authenticated_grant_can_start_a_followup(store):
    tasks = runtime(store)
    identity = root(tasks)
    app = FastAPI()
    authorized = False

    def admin():
        if not authorized:
            raise HTTPException(401)

    register_task_api(app, tasks, admin)
    path = f"/api/agent/tasks/{identity}/goal/followup"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        grant = schedule(tasks).model_dump()
        assert (await client.put(path, json=grant)).status_code == 401
        authorized = True
        assert (await client.get(path)).json()["schedule"] is None
        for invalid in ({"states": ["live_handoff"]}, {"states": ["game_action"]}, {"shell": "not-a-tool"},
                        {"local_time": ""}, {"weekday": 2}, {"timezone": "missing"}, {"pages": []}):
            assert (await client.put(path, json=grant | invalid)).status_code == 422
        assert (await client.put(path, json=grant)).json()["enabled"]
        store.set("account", "other")
        assert (await client.put(path, json=grant)).status_code == 409
        assert not (await client.get(path)).json()["enabled"]
        assert not (await client.delete(path)).json()["enabled"]


async def test_existing_worker_ticks_without_consulting_chat_or_planner(store):
    tasks = runtime(store)
    identity = root(tasks)
    done = asyncio.Event()

    async def game(request, args):
        assert request.goal == "" and not request.receipts
        done.set()
        return {"simulated": True, "state": {"summary": "游戏进行中"}}

    tasks.register("game_status", Tool("模拟状态", StrictModel, game))
    register_task_api(FastAPI(), tasks, lambda: None)
    tasks.followups.configure(identity, schedule(tasks, pages=[], states=["game_status"]), 0)
    tasks.start()
    try:
        await asyncio.wait_for(done.wait(), 3)
        child, = [run for run in tasks.workspace.runs(identity) if run["id"] != identity]
        assert child["status"] == "completed"
        assert "游戏进行中" in tasks.workspace.latest(child["id"])["body"]
    finally:
        await tasks.close()


async def test_attention_compares_last_periodic_receipts_for_this_goal(store, monkeypatch):
    tasks = runtime(store)
    service = GoalFollowups(tasks)
    identity, other_goal = root(tasks), root(tasks)
    day = 86400
    now = stamp("2026-11-18T09:00:00+08:00")
    excerpt = "第一版"
    pages = ["https://example.org/one", "https://example.org/two"]
    checks = count()

    async def read(page, *, exact_source):
        if not excerpt:
            raise ValueError("读取失败")
        return {"url": page, "excerpt": excerpt, "checked_at": next(checks)}

    async def activity(request, args):
        return {"date": str(next(checks)), "events": []}

    tasks.register("today_activity", Tool("今日运营", StrictModel, activity))
    monkeypatch.setattr(goal_followups, "fetch_excerpt", read)
    grant = schedule(tasks, states=["today_activity"], pages=pages)
    service.configure(identity, grant, now - 1)
    await service.tick(now)
    baseline = next(run["id"] for run in tasks.workspace.runs(identity) if run["id"] != identity)
    store.execute("UPDATE agent_tasks SET source_key=NULL WHERE id=?", (baseline,))
    service.configure(other_goal, grant, now)
    excerpt = "另一个目标的观测"
    await service.tick(now + day)
    await service.tick(now + day)
    service.disable(other_goal)
    other_ids = {run["id"] for run in tasks.workspace.runs(other_goal)}
    assert not other_ids.intersection(row["task_id"] for row in store.all("SELECT task_id FROM creative_attention"))
    manual = tasks.create(str(uuid4()), "周期跟进 · 手动交办", creative_goal_id=identity)
    assert tasks._save(manual, status="cancelled")
    service.configure(identity, schedule(tasks, states=["today_activity"], pages=list(reversed(pages))), now + day + 1)
    for offset, excerpt in enumerate(["另一个目标的观测", "", "", "已恢复", "已恢复"], start=2):
        await service.tick(now + offset * day)
    attention = store.all("""
        SELECT due,status FROM agent_tasks JOIN creative_attention ON task_id=id
        WHERE creative_goal_id=? ORDER BY due
    """, (identity,))
    assert attention == [{"due": now + day, "status": "completed"},
                         {"due": now + 3 * day, "status": "failed"},
                         {"due": now + 5 * day, "status": "completed"}]


async def test_failed_attention_write_rolls_back_receipt_delivery_and_cursor(store, monkeypatch):
    tasks = runtime(store)
    service = GoalFollowups(tasks)
    identity = root(tasks)
    start = stamp("2026-11-18T09:00:00+08:00")
    excerpt = "初始观测"

    async def read(page, *, exact_source):
        return {"url": page, "excerpt": excerpt}

    monkeypatch.setattr(goal_followups, "fetch_excerpt", read)
    service.configure(identity, schedule(tasks), start - 1)
    await service.tick(start)
    due = service.get(identity)["next_due"]
    excerpt = "有变化"
    with monkeypatch.context() as patch:
        def reject(db, task_id, updated):
            assert db.in_transaction
            raise RuntimeError("attention fixture")

        patch.setattr(tasks.workspace, "notify", reject)
        with pytest.raises(RuntimeError, match="attention fixture"):
            await service.tick(due)
    assert service.get(identity)["next_due"] == due and len(tasks.workspace.runs(identity)) == 2
    assert len(store.all("SELECT task_id FROM creative_editions")) == 1
    await service.tick(due)
    await service.tick(due)
    assert len(store.all("SELECT task_id FROM creative_attention")) == 1


async def test_exact_page_does_not_read_substitutions_or_redirect_targets(monkeypatch):
    urls = []
    client = httpx.AsyncClient

    def redirected(request):
        urls.append((request.headers["host"], request.url.path))
        return httpx.Response(302, headers={"location": "https://example.org/unapproved"})

    async def public(url):
        return url.host, "93.184.215.14"

    monkeypatch.setattr(web_fetch, "public_https_address", public)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client(transport=httpx.MockTransport(redirected), **options))
    with pytest.raises(ValueError, match="重定向"):
        await web_fetch.fetch_excerpt("https://github.com/example/repo", exact_source=True)
    assert urls == [("github.com", "/example/repo")]
