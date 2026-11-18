import asyncio
import sqlite3
from uuid import uuid4

import httpx
import pytest

from tuber_studio.agent.goal_events import BATCH, EventGrant, GoalEvents
from tuber_studio.agent.task_identity import TaskIdentity
from tuber_studio.agent.task_runtime import TaskRuntime
from tuber_studio.app import create_app
from tuber_studio.store import Store


@pytest.fixture
def studio(settings):
    app = create_app(settings)
    app.state.browsing.discovery.remember_account("100")
    tasks = app.state.runtime.tasks
    goal = str(uuid4())
    task = tasks.create(goal, "私有原话不应附带给事件消费")
    tasks.workspace.promote(task)
    tasks._save(task, status="cancelled")
    yield app, tasks, goal
    app.state.store.close()


def incoming(app, key, *, sender="200", account="100", outgoing=False):
    row = {"msg_key": key, "msg_seqno": key, "msg_type": 1, "sender_uid": account if outgoing else sender,
           "receiver_id": sender if outgoing else account, "timestamp": 1700000000,
           "content": {"content": "只有收件箱可以读到的正文"}}
    return app.state.messages._ingest_history([row], sender, account, "不作为授权依据的昵称")


def grant(tasks, goal, sender="200"):
    state = tasks.goal_events.get(goal)
    return EventGrant(source="bilibili_inbox", epoch=state["epoch"], revision=state["revision"], sender_uid=sender)


async def test_inbox_event_worker_readback_filters_and_never_replies(studio, monkeypatch):
    app, tasks, goal = studio
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture-only")
    incoming(app, 1)
    url = f"/api/agent/tasks/{goal}/goal/events"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as owner:
        assert (await owner.get(url)).status_code == 401
        owner.headers["Authorization"] = "Bearer fixture-only"
        payload = grant(tasks, goal).model_dump()
        assert (await owner.put(url, json=payload | {"sender_uid": "*"})).status_code == 422
        assert (await owner.put(url, json=payload | {"source": "shell"})).status_code == 422
        assert (await owner.put(url, json=payload | {"sender_uid": "100"})).status_code == 409
        assert (await owner.put(url, json=payload)).json()["enabled"]
        assert incoming(app, 2, outgoing=True) == 0
        incoming(app, 3, sender="300")
        incoming(app, 4, account="999")
        incoming(app, 7)
        tasks.store.execute("UPDATE dm_inbox SET character_id='another-role' WHERE message_id='7'")
        incoming(app, 5)
        assert incoming(app, 5) == 0
        active = (await owner.get(url)).json()
        assert (await owner.put(url, json=payload)).status_code == 409
        repeated = await owner.put(url, json=payload | {"revision": active["revision"]})
        assert repeated.json()["cursor"] == active["cursor"]
        assert (await owner.delete(url, params={"revision": 0})).status_code == 409
        tasks.start()
        try:
            async with asyncio.timeout(3):
                while not (await owner.get("/api/agent/attention")).json()["count"]:
                    await asyncio.sleep(0.02)
        finally:
            await tasks.close()
        listing = (await owner.get("/api/agent/tasks")).json()
        notice, = listing["attention"]
        task_url = "/api/agent/tasks/" + notice["id"]
        detail = (await owner.get(task_url)).json()
        receipt = detail["steps"][0]["result"]
        assert receipt["message_id"] == "5" and receipt["sender_uid"] == "200" and receipt["account_id"] == "100"
        assert detail["status"] == "completed" and "消息 ID：5" in detail["message"]
        assert "只有收件箱" not in str(detail) and "私有原话" not in str(detail["steps"])
        assert (await owner.delete(task_url + "/attention", params={"updated": detail["attention"]})).status_code == 200
        assert (await owner.get("/api/agent/attention")).json() == {"count": 0}
        state = (await owner.get(url)).json()
        assert (await owner.delete(url, params={"revision": state["revision"]})).json()["enabled"] is False
        incoming(app, 6)
        tasks.goal_events.consume()
        assert len(tasks.workspace.runs(goal)) == 2
        assert not tasks.store.all("SELECT id FROM dm_attempts") and not tasks.store.all("SELECT id FROM dm_drafts")


def test_cursor_and_notices_commit_together_resume_after_process_loss(studio, settings, monkeypatch):
    app, tasks, goal = studio
    service = tasks.goal_events
    service.configure(goal, grant(tasks, goal))
    for key in reversed(range(1, BATCH + 4)):
        incoming(app, key)
    original = tasks.workspace.notify
    with monkeypatch.context() as patch:
        def fail(db, identity, updated):
            original(db, identity, updated)
            raise sqlite3.IntegrityError("fixture")

        patch.setattr(tasks.workspace, "notify", fail)
        with pytest.raises(sqlite3.IntegrityError):
            service.consume()
    assert service.get(goal)["cursor"] == 0 and not tasks.store.all("SELECT * FROM creative_attention")
    service.consume()
    assert len(tasks.workspace.runs(goal)) == 1 + BATCH
    tasks.store.close()
    reopened = Store(settings.runtime / "studio.sqlite3", [])
    try:
        restored = TaskRuntime(reopened, TaskIdentity(reopened, lambda: "whale", lambda: ["100", "whale", 1, None]), None)
        consumer = GoalEvents(restored)
        consumer.consume()
        assert len(reopened.all("SELECT * FROM creative_attention")) == BATCH + 3
        reopened.execute("UPDATE goal_event_watches SET cursor=0")
        consumer.consume()
        consumer.consume()
        assert len(restored.workspace.runs(goal)) == BATCH + 4
        assert consumer.get(goal)["cursor"] == BATCH + 3
    finally:
        reopened.close()


@pytest.mark.parametrize("invalidate", ["switch_back", "edit_role", "close", "replace"])
def test_pending_events_not_delivered_under_changed_grant(studio, invalidate):
    app, tasks, goal = studio
    service = tasks.goal_events
    service.configure(goal, grant(tasks, goal))
    incoming(app, 1)
    if invalidate == "switch_back":
        app.state.browsing.discovery.remember_account("300")
        app.state.browsing.discovery.remember_account("100")
    elif invalidate == "edit_role":
        tasks.store.replace_character(tasks.store.character() | {"name": "修订"})
    elif invalidate == "close":
        tasks.workspace.close(goal, tasks.executable)
    else:
        service.configure(goal, grant(tasks, goal, sender="300"))
        incoming(app, 2, sender="200")
    service.consume()
    assert service.get(goal)["enabled"] == (invalidate == "replace")
    assert not tasks.store.all("SELECT task_id FROM creative_attention")
