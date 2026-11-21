import asyncio
import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from tuber_studio.agent.goal_events import BATCH, EventGrant, GoalEvents
from tuber_studio.agent.task_delivery import PrivateDelivery, evidence_index
from tuber_studio.agent.task_identity import TaskIdentity
from tuber_studio.agent.task_runtime import TaskRuntime
from tuber_studio.app import create_app
from tuber_studio.operations.browsing.comment_timeline import observe_thread
from tuber_studio.responses_provider import FunctionCall, ResponseTurn
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


async def test_next_authorized_reply_starts_private_work_and_delivers_to_original_conversation(studio, monkeypatch):
    app, tasks, goal = studio
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture-only")
    scope = tasks.scope()
    tasks.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
        goal, scope[1], scope[2], "到回信以后帮我整理确认邮件",
        json.dumps({"card": {"type": "agent_task", "data": {"id": goal}}, "account_id": scope[0],
                    "owner_source": scope[-2], "conversation_id": scope[1]}), 1,
    ))
    service = tasks.goal_events
    service.configure(goal, grant(tasks, goal).model_copy(update={"after_reply": "整理一份私有确认邮件"}))
    incoming(app, 1, sender="300")
    service.consume()
    assert len(tasks.workspace.runs(goal)) == 1
    incoming(app, 2)
    incoming(app, 3)
    service.consume()
    service.consume()
    _, queued = tasks.workspace.runs(goal)
    assert queued["status"] == "queued"

    async def planner(*, input, tools, **kwargs):
        allowed = {tool["name"] for tool in tools}
        assert allowed == {"delegate_text", "submit_materials", "ask_owner"}
        payload = json.loads(input[1]["content"])
        source, = [key for key, content in payload["saved_evidence"].items() if "只有收件箱" in content]
        output = next((item for item in input if item.get("type") == "function_call_output"), None)
        if output:
            assert json.loads(output["output"])["receipt"]["draft"] == "确认邮件草稿"
            call = FunctionCall("submit", "submit_materials", {
                "items": [{"title": "确认邮件草稿", "rationale": "来信已经入库", "sources": [source]}], "gaps": [],
            })
        else:
            call = FunctionCall("write", "delegate_text", {"objective": "拟一份确认邮件", "sources": [source]})
        return ResponseTurn("planner", (call,), "", {})

    async def write(request, args):
        assert "只有收件箱可以读到的正文" in evidence_index(request.receipts)[args.sources[0]]
        return {"draft": "确认邮件草稿", "sources": args.sources, "response_id": "writer"}

    async def edit(task, evidence):
        return PrivateDelivery.model_validate(task["plan"]["candidate"])

    tasks.responses_client = lambda: planner
    tasks.tools["delegate_text"] = replace(tasks.tools["delegate_text"], run=write)
    tasks.edit_delivery = edit
    for _ in range(5):
        task = tasks.current(queued["id"])
        if task["status"] in {"completed", "failed"}:
            break
        await tasks._advance(task)
    assert tasks.current(queued["id"])["status"] == "completed"
    assert tasks.workspace.latest(queued["id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer fixture-only"}) as client:
        turns = (await client.get("/api/chat/intelligent")).raise_for_status().json()["turns"]
    delivered, = [turn for turn in turns if turn.get("task_delivery")]
    assert delivered["task_delivery"]["task_id"] == queued["id"]
    assert "确认邮件草稿" in delivered["reply_text"]
    assert not tasks.store.all("SELECT id FROM dm_attempts")


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


def observe(tasks, comment, *, resource="BV1d8pU6xEZX", root="42", uid="200", account="100", via="visit", **extra):
    row = {"account_id": account, "character_id": "whale", "id": via, **extra}
    with tasks.store.db:
        observe_thread(tasks.store, row, resource, [{"comment_id": str(comment), "thread_id": root,
                       "user_id": uid, "text": "只保留在评论原出处"}], via=via)


async def test_comment_watch_deduplicates_verified_observations_and_restarts(studio, settings, monkeypatch):
    app, tasks, goal = studio
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture-only")
    path = f"/api/agent/tasks/{goal}/goal/comment-events"
    observe(tasks, 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as owner:
        assert (await owner.get(path)).status_code == 401
        owner.headers["Authorization"] = "Bearer fixture-only"
        state = (await owner.get(path)).json()
        body = {"resource": "BV1d8pU6xEZX", "root_id": "42", "epoch": state["epoch"], "revision": state["revision"]}
        assert (await owner.put(path, json=body | {"resource": "*"})).status_code == 422
        watch = (await owner.put(path, json=body)).json()
        assert watch["enabled"] and watch["root_id"] == "42"
        assert (await owner.put(path, json=body)).status_code == 409
        observe(tasks, 1, via="again")
        observe(tasks, 2, uid="100")
        observe(tasks, 3, account="999")
        observe(tasks, 4, root="12")
        observe(tasks, 5, resource="dynamic:987")
        observe(tasks, 6, character_id="other")
        observe(tasks, 7, preview=True)
        observe(tasks, 8)
        observe(tasks, 8, via="different_source")
        assert tasks.store.all("SELECT comment_id FROM goal_pending_comments") == [{"comment_id": "8"}]
        assert (await owner.put(path, json=body | {"revision": watch["revision"]})).json() == watch
    tasks.store.close()
    reopened = Store(settings.runtime / "studio.sqlite3", [])
    try:
        restored = TaskRuntime(reopened, TaskIdentity(reopened, lambda: "whale", lambda: ["100", "whale", 1, None]), None)
        events = GoalEvents(restored)
        with monkeypatch.context() as patch:
            def fail(*args):
                raise sqlite3.IntegrityError("fixture")
            patch.setattr(restored.workspace, "notify", fail)
            with pytest.raises(sqlite3.IntegrityError):
                events.consume()
        assert not reopened.all("SELECT * FROM creative_attention")
        original, = reopened.all("SELECT * FROM goal_pending_comments")
        events.consume()
        notice, = reopened.all("SELECT task_id FROM creative_attention")
        detail = restored.current(notice["task_id"])
        result = detail["steps"][0]["result"]
        assert result["comment_id"] == "8" and result["url"].endswith("#reply8") and "只保留在" not in str(detail)
        assert "42" in detail["goal"] and result["author_uid"] == "200"
        reopened.execute("DELETE FROM creative_attention")
        reopened.execute("INSERT INTO goal_pending_comments(id,goal_id,revision,comment_id,author_uid) VALUES(?,?,?,?,?)",
                         tuple(original[key] for key in ("id", "goal_id", "revision", "comment_id", "author_uid")))
        events.consume()
        assert not reopened.all("SELECT * FROM goal_pending_comments") and not reopened.all("SELECT * FROM creative_attention")
        assert len(restored.workspace.runs(goal)) == 2
        assert not reopened.all("SELECT * FROM account_writes") and not reopened.all("SELECT * FROM drafts")
    finally:
        reopened.close()


@pytest.mark.parametrize("reason", ["replace", "disable", "account", "close"])
def test_queued_comment_event_respects_revocation(studio, reason):
    from tuber_studio.agent.goal_comment_events import CommentWatch
    app, tasks, goal = studio
    events = tasks.goal_events.comments
    grant = CommentWatch(resource="BV1d8pU6xEZX", root_id="42", epoch=events.get(goal)["epoch"], revision=0)
    state = events.configure(goal, grant)
    observe(tasks, 1)
    if reason == "replace":
        events.configure(goal, grant.model_copy(update={"revision": state["revision"], "root_id": "43"}))
    elif reason == "disable":
        events.disable(goal, state["revision"])
    elif reason == "close":
        tasks.workspace.close(goal, tasks.executable)
    else:
        app.state.browsing.discovery.remember_account("300")
        app.state.browsing.discovery.remember_account("100")
    tasks.goal_events.consume()
    assert not tasks.store.all("SELECT * FROM goal_pending_comments")
    assert not tasks.store.all("SELECT * FROM creative_attention")
