import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tuber_studio.agent.reply_operations import SelectedReply
from tuber_studio.app import create_app
from tuber_studio.operations_control import OperationsControl
from tuber_studio.platforms.bilibili import PublishUncertain
from tuber_studio.store import dump


class Transport:
    def __init__(self):
        self.sent, self.reads, self.items = [], [], []
        self.disconnect = self.visible = False
        self.before = lambda: None

    async def self_uid(self):
        return "900"

    async def read_video_thread(self, target):
        return {"source": {"text": "原评论", "author_uid": "123"}, "items": self.items}

    async def send(self, uid, text, *, authorized):
        self.before()
        assert authorized()
        self.sent.append((uid, text))
        if self.disconnect:
            raise PublishUncertain("fixture")
        return {"msg_key": "901"}

    async def publish(self, video, parent, text, **options):
        self.sent.append((parent, text))
        if self.disconnect:
            raise PublishUncertain("fixture")
        return {"rpid": 901}

    async def reconcile_publication(self, target, **kwargs):
        self.reads.append((target, kwargs))
        if self.visible:
            receipt = {"rpid": 901} if "root" in target else {"msg_key": "901"}
            return {"status": "visible", "receipt": receipt}
        return {"status": "ambiguous"}


@pytest.fixture
async def studio(settings, monkeypatch):
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "test-owner")
    async def deny_network(*args, **kwargs):
        pytest.fail("限定回复测试只能使用 ASGI 和注入的平台传输")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)
    transport = Transport()
    app = create_app(settings, adapter=transport, message_adapter=transport)
    runtime, store = app.state.runtime, app.state.store
    tasks, comments, messages = runtime.tasks, app.state.comments, app.state.messages
    comments.credentials = {"sessdata": "fixture", "csrf": "fixture"}
    await comments.account_id()
    app.state.browsing.discovery.remember_account("900")
    store.set("message_policy", {"enabled": True})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as owner:
        path = "/api/agent/reply-selections"
        assert (await owner.get(path)).status_code == 401
        owner.headers["Authorization"] = "Bearer test-owner"
        scope = (await owner.get(path)).json()
        yield SimpleNamespace(**locals())
    store.close()


def incoming(s, key, uid="123"):
    return s.messages.ingest({"message_id": str(key), "platform_user_id": uid, "account_id": "900",
                              "display_name": "观众", "text": f"来信 {key}", "timestamp": time.time()})[0]


def prepare(s, channel, *, key=10, uid="123"):
    if channel == "comments":
        s.comments.import_items("BV1d8pU6xEZX", [{"comment_id": "1234", "platform_user_id": "123", "content": "原评论"}])
        source = s.store.one("SELECT id FROM comments")["id"]
        identity = s.store.execute(
            "INSERT INTO drafts(comment_id,status,text,character_id,character_version,account_id,updated_at) "
            "VALUES(?,'pending_review','已核对的回复',?,?,'900',1)", (source, s.scope["character"], s.scope["version"]),
        ).lastrowid
    else:
        source = incoming(s, key, uid)
        identity = s.messages.enqueue("message", source, schedule=False)
        row = s.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        window = s.messages._reply_window(row, s.store.one("SELECT * FROM dm_inbox WHERE id=?", (source,)))
        audit = {"generation_identity": s.runtime.identity_snapshot(s.runtime.character, "900"),
                 "conversation_sha256": window.fingerprint, "reply_incoming_ids": [source]}
        s.store.execute("UPDATE dm_drafts SET status='pending_review',text='已核对的回复',answer_check=? WHERE id=?",
                        (dump(audit), identity))
    return {"channel": channel, "source_ids": [source], "draft_id": identity}


async def approve(s, item):
    res = await s.owner.post(s.path, json={"epoch": s.scope["epoch"], "items": [item]})
    assert res.status_code == 200, res.text
    selection = res.json()["selection_id"]
    task = s.tasks.create(str(uuid4()), f"处理回复选择 {selection}")
    step = {"tool": "submit_selected_reply", "id": str(uuid4()), "call_id": "reply", "response_id": "turn",
            "arguments": {"selection_id": selection, "item": 1}, "revision": 1}
    s.tasks._save(task, plan={"backend": "responses", "prior_goals": []}, steps=[step])
    await s.tasks._advance(s.tasks.current(task["id"]))
    task = s.tasks.current(task["id"])
    assert task["status"] == "awaiting_approval" and not s.transport.sent
    approval = task["steps"][0]["approval"]
    assert approval["source_ids"] == item["source_ids"] and approval["text"] == "已核对的回复"
    assert "UID 123" in approval["target"] and "账号 900" in approval["target"]
    await s.tasks.control(task["id"], 1, "approve", step_id=step["id"])
    return task["id"], selection


async def advance(s, task_id):
    task = s.tasks.current(task_id)
    s.tasks._save(task, steps=[task["steps"][0] | {"due": 0}])
    await s.tasks._advance(s.tasks.current(task_id))
    return s.tasks.current(task_id)["steps"][0]


@pytest.mark.parametrize("channel", ["comments", "messages"])
async def test_only_selected_receipt_after_disconnect(studio, channel):
    s = studio
    item = prepare(s, channel)
    task_id, _ = await approve(s, item)
    if channel == "messages":
        incoming(s, 11)
        incoming(s, 12, "321")
    s.transport.disconnect = True
    assert (await advance(s, task_id))["checkpoint"]["status"] == "needs_reconciliation"
    assert len(s.transport.sent) == 1 and not s.transport.reads
    # Simulate loss between the admission transaction and persistence of recovery metadata.
    s.store.execute("DELETE FROM operation_continuations WHERE channel=?", (channel,))
    assert "checkpoint" in await advance(s, task_id)
    s.transport.visible = True
    result = (await advance(s, task_id))["result"]
    assert result["status"] == "published"
    assert len(s.transport.sent) == 1 and len(s.transport.reads) == 2
    assert s.store.one("SELECT token FROM account_writes")["token"] == result["write_token"]
    if channel == "messages":
        assert s.store.all("SELECT incoming_id FROM dm_incoming_settlements") == [{"incoming_id": item["source_ids"][0]}]


@pytest.mark.parametrize("change", ["revoke", "edit", "policy", "switch", "transport_revoke", "operator_reply"])
async def test_approval_invalidated_without_a_platform_submission(studio, change):
    s = studio
    channel = "comments" if change == "operator_reply" else "messages"
    item = prepare(s, channel)
    task_id, selection = await approve(s, item)
    if change == "revoke":
        assert (await s.owner.delete(s.path + "/" + selection)).status_code == 200
    elif change == "edit":
        s.messages.review(item["draft_id"], "edit", "另一份正文")
    elif change == "switch":
        s.app.state.browsing.discovery.remember_account("333")
        s.app.state.browsing.discovery.remember_account("900")
    elif change == "policy":
        s.comments.operations.configure({"emergency": True})
    elif change == "operator_reply":
        s.transport.items = [{"comment_id": "888", "thread_id": "1234", "parent_comment_id": "1234", "user_id": "900"}]
    else:
        s.transport.before = lambda: s.store.execute("DELETE FROM agent_reply_selections WHERE id=?", (selection,))
    if change != "switch":
        step = await advance(s, task_id)
        assert step["result"]["status"] == ("operator_replied" if change == "operator_reply" else "not_submitted")
    else:
        assert not s.tasks.can_control(s.tasks.current(task_id))
    assert not s.transport.sent and not s.store.all("SELECT * FROM account_writes")


async def test_workbench_races_agent_without_resending_and_scope_cannot_expand(studio):
    s = studio
    item = prepare(s, "messages")
    extra = incoming(s, 11, "222")
    assert (await s.owner.post(s.path, json={"epoch": s.scope["epoch"], "items": [
        item | {"source_ids": [*item["source_ids"], extra]}]})).status_code == 409
    task, selection = await approve(s, item)
    tool = s.tasks.tools["submit_selected_reply"]
    with pytest.raises(ValueError):
        tool.review(SelectedReply(selection_id=selection, item=2))
    assert not tool.accepts_goal("其他任务", SelectedReply(selection_id=selection, item=1))
    s.messages.review(item["draft_id"], "approve")
    results = await asyncio.gather(advance(s, task), s.messages.send(item["draft_id"]))
    assert results[0]["result"]["status"] == "not_submitted" and results[1]["status"] == "published"
    assert len(s.transport.sent) == 1 and not s.store.all("SELECT token FROM agent_reply_attempts")


@pytest.mark.parametrize("channel", ["comments", "messages"])
async def test_uncertain_turn_blocks_only_same_destination(studio, channel):
    s = studio
    task_id, _ = await approve(s, prepare(s, channel))
    s.transport.disconnect = True
    await advance(s, task_id)
    control = s.comments.operations
    character = s.runtime.character
    def reserve(recipient, root, source):
        return control.reserve("900", character, source, channel, recipient=recipient, automatic=False,
                               binding={"target": {"root": root, "resource_type": "video"}})
    with pytest.raises(ValueError, match="待核对"):
        reserve("123", "1234", "second")
    assert reserve("222", "2222", "independent")
    assert len(s.transport.sent) == 1


async def test_reconcile_stopped_step_uses_only_original_attempt(studio):
    s = studio
    task_id, _ = await approve(s, prepare(s, "messages"))
    s.transport.disconnect = True
    await advance(s, task_id)
    s.transport.visible = True
    stopped = await s.tasks.control(task_id, 1, "cancel")
    assert stopped["steps"][0]["result"]["receipt"] == {"msg_key": "901"}
    assert len(s.transport.sent) == len(s.transport.reads) == 1


@pytest.mark.parametrize("channel", ["comments", "messages"])
async def test_restart_after_domain_receipt_before_ledger_finish(studio, monkeypatch, channel):
    s = studio
    task_id, _ = await approve(s, prepare(s, channel))
    control = s.comments.operations
    def interrupt(token, status, **kwargs):
        assert status == "published"
        raise asyncio.CancelledError
    with monkeypatch.context() as patch:
        patch.setattr(control, "finish", interrupt)
        task = s.tasks.current(task_id)
        with pytest.raises(asyncio.CancelledError):
            await s.tasks._invoke(task, task["steps"][0], [], task["goal"])
    assert s.store.one("SELECT status FROM account_writes")["status"] == "sending"
    OperationsControl(s.store)
    assert s.store.one("SELECT status FROM account_writes")["status"] == "uncertain"
    result = (await advance(s, task_id))["result"]
    assert result["status"] == "published" and control.get_attempt(result["write_token"])["resolved_status"] == "published"
    assert len(s.transport.sent) == 1 and not s.transport.reads


async def test_foreign_domain_attempt_cannot_be_reconciled(studio):
    s = studio
    item = prepare(s, "messages")
    task_id, _ = await approve(s, item)
    s.transport.disconnect = s.transport.visible = True
    await advance(s, task_id)
    s.store.execute("INSERT INTO dm_attempts(draft_id,started_at,status,platform_user_id) VALUES(?,?,'uncertain','123')",
                    (item["draft_id"], time.time()))
    step = await advance(s, task_id)
    assert "checkpoint" in step and not s.transport.reads and len(s.transport.sent) == 1


async def test_separate_conversations_can_submit_while_selected_inbox_waits(studio, monkeypatch):
    s = studio
    selected = prepare(s, "messages")
    task_id, _ = await approve(s, selected)
    other = prepare(s, "messages", key=11, uid="222")
    s.messages.review(other["draft_id"], "approve")
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(uid, text, *, authorized):
        assert authorized()
        if uid == "123":
            entered.set()
            await release.wait()
        s.transport.sent.append((uid, text))
        return {"msg_key": uid}
    monkeypatch.setattr(s.transport, "send", send)
    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        first = group.create_task(advance(s, task_id))
        await entered.wait()
        try:
            assert (await s.messages.send(other["draft_id"]))["status"] == "published"
            assert not first.done()
        finally:
            release.set()
    assert first.result()["result"]["receipt"] == {"msg_key": "123"}
    assert [uid for uid, _ in s.transport.sent] == ["222", "123"]
