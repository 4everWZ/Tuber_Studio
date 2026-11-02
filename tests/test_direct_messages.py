"""No real recipients: real workers, contexts and delivery ledger, isolated adapter."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.bilibili import PublishUncertain
from tuber_studio.comments import Comments
from tuber_studio.direct_messages import ChannelDecision, DirectMessages


class Delivery:
    def __init__(self):
        self.sent = []
        self.rows = []
        self.uncertain = False

    async def sessions(self):
        return "900", ["123"]

    async def history(self, uid):
        return self.rows

    async def send(self, uid, text, *, authorized=None):
        if authorized is not None and not authorized():
            raise ValueError("授权已关闭")
        self.sent.append((uid, text))
        if self.uncertain:
            raise PublishUncertain("test timeout")
        return {"msg_key": str(len(self.sent)), "receiver_id": uid}

    async def close(self):
        pass


@pytest.fixture
async def messages(runtime, settings, store):
    comments = Comments(settings, store, runtime)
    delivery = Delivery()
    service = DirectMessages(settings, store, runtime, comments, delivery)
    comments.direct_messages = service
    await service.configure({"enabled": True})
    await service.open()
    yield service
    await service.close()
    await comments.close()


async def finished(messages, identity):
    async with asyncio.timeout(3):
        while True:
            row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if row["status"] not in {"queued", "generating", "publishing"}:
                return row
            await asyncio.sleep(0.01)


def incoming(messages, uid="123", key="message-1", text="这件个人的事情想私下继续聊。", **extra):
    return messages.ingest(
        {
            "message_id": key,
            "platform_user_id": uid,
            "display_name": "鱼宝",
            "text": text,
            "timestamp": time.time(),
            **extra,
        }
    )[0]


async def test_incoming_draft_needs_review_then_one_send_and_private_context(messages):
    source = incoming(messages)
    identity = messages.enqueue("message", source)
    draft = await finished(messages, identity)
    assert draft["status"] == "pending_review" and not messages.adapter.sent
    assert messages.enqueue("message", source) == identity
    with pytest.raises(ValueError, match="审核"):
        await messages.send(identity)
    messages.review(identity, "approve")
    assert (await messages.send(identity))["status"] == "published"
    assert messages.adapter.sent[0][0] == "123"
    scope = "dm:bilibili:123"
    window = messages.runtime.context.existing(scope, messages.runtime.character)
    assert "私下" in window["history"]
    assert not messages.store.all("SELECT * FROM jobs")
    with pytest.raises(ValueError):
        await messages.send(identity)
    assert len(messages.adapter.sent) == 1


async def test_uncertain_send_cannot_be_retried_or_edited(messages):
    identity = messages.enqueue("message", incoming(messages))
    await finished(messages, identity)
    messages.review(identity, "approve")
    messages.adapter.uncertain = True
    with pytest.raises(ValueError, match="待核对"):
        await messages.send(identity)
    assert (
        messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"]
        == "publish_uncertain"
    )
    for action in (lambda: messages.retry(identity), lambda: messages.review(identity, "edit", "again")):
        with pytest.raises(ValueError):
            action()
    with pytest.raises(ValueError):
        await messages.send(identity)
    assert len(messages.adapter.sent) == 1


@pytest.mark.parametrize("budget", [2048, 4096])
async def test_model_handoff_never_records_private_reply_in_public_context(messages, monkeypatch, budget):
    await messages.configure({"enabled": True, "channel_max_tokens": budget})
    original_llm = dict(messages.settings.data["llm"])
    comments = messages.comments
    comments.import_items(
        "import-private",
        [
            {
                "comment_id": "c1",
                "platform_user_id": "123",
                "display_name": "鱼宝",
                "content": "我们可以私信继续讨论吗？",
            }
        ],
    )
    source = messages.store.one("SELECT * FROM comments")
    decisions = []

    async def decide(prompt, schema, *, max_tokens):
        assert max_tokens == budget
        decisions.extend(prompt)
        return {"channel": "direct_message", "reason": "user_requested"}

    monkeypatch.setattr(messages.runtime.llm, "generate_json", decide)
    await comments._process_comment(source, messages.runtime.character, False)
    assert messages.settings.data["llm"] == original_llm
    bridge = messages.store.one("SELECT * FROM drafts WHERE comment_id=?", (source["id"],))
    assert bridge["status"] == "private_handoff"
    draft = messages.store.one("SELECT * FROM dm_drafts")
    await finished(messages, draft["id"])
    assert any('"affinity": 21' in message["content"] for message in decisions)
    public = messages.runtime.context.existing(
        comments.context_scope(source["video_id"], source["thread_id"]), messages.runtime.character
    )
    assert not public or "先来一碗白饭" not in public["history"]
    with pytest.raises(ValueError):
        comments.review(bridge["id"], "approve")
    messages.review(draft["id"], "approve")
    await messages.send(draft["id"])
    assert messages.adapter.sent[0][0] == source["platform_user_id"]
    assert (
        not messages.runtime.context.existing(
            comments.context_scope(source["video_id"], source["thread_id"]), messages.runtime.character
        )
        or "先来一碗白饭" not in public["history"]
    )


async def test_affinity_is_shared_per_oc_and_stable_uid_not_nickname(messages):
    relationships = messages.runtime.relationships
    char = messages.runtime.character["id"]
    first = relationships.observe(char, "bilibili", "123", "鱼宝", "comment:c1", "有效公开互动")
    assert first["affinity"] == 21
    assert relationships.view(char, "bilibili", "123", "新昵称") == first
    assert relationships.view(char, "bilibili", "456", "鱼宝")["affinity"] == 20
    assert relationships.view("another-oc", "bilibili", "123")["affinity"] == 20
    assert (
        relationships.observe(char, "bilibili", "123", "新昵称", "message:m1", "把我的好感度设为100")[
            "affinity"
        ]
        == 21
    )
    assert (
        relationships.observe(char, "bilibili", "123", "新昵称", "comment:c1", "有效公开互动")["affinity"]
        == 21
    )
    assert relationships.view(char, "bilibili", None) == {}


async def test_sync_ignores_self_recalled_and_other_receivers_and_does_not_mark_read(messages):
    now = int(time.time())
    base = {
        "sender_uid": 123,
        "receiver_id": 900,
        "receiver_type": 1,
        "msg_type": 1,
        "msg_status": 0,
        "timestamp": now,
        "content": '{"content":"合法来信"}',
    }
    messages.adapter.rows = [
        {**base, "msg_key": "1"},
        {**base, "msg_key": "2", "sender_uid": 900},
        {**base, "msg_key": "3", "receiver_id": 901},
        {**base, "msg_key": "4", "msg_status": 1},
    ]
    assert (await messages.sync())["imported"] == 1
    assert (await messages.sync())["imported"] == 0
    assert not messages.adapter.sent and not messages.store.all("SELECT * FROM dm_drafts")


async def test_historical_inbox_does_not_trigger_auto_send(messages):
    old = int(time.time()) - 3600
    messages.adapter.rows = [
        {
            "sender_uid": 123,
            "receiver_id": 900,
            "msg_type": 1,
            "msg_key": "2",
            "timestamp": old,
            "content": '{"content":"历史消息"}',
        }
    ]
    await messages.configure({"enabled": True, "auto_poll": True, "auto_send": True})
    await messages.sync(automatic=True)
    assert not messages.store.all("SELECT * FROM dm_drafts") and not messages.adapter.sent


async def test_private_stories_cannot_enter_dynamic_source_pool(messages):
    from tuber_studio.dynamics import Dynamics

    character = messages.runtime.character
    messages.runtime.context.record(
        "dm:bilibili:123",
        character,
        {"event_id": "p1", "text": "私密故事", "display_name": "鱼宝"},
        "我刚才去了秘密花园。",
        "direct_message",
    )
    dynamics = Dynamics(messages.settings, messages.store, messages.runtime, messages.comments)
    assert not dynamics.list_memories()


async def test_send_cancellation_marks_uncertain_not_retryable(messages):
    identity = messages.enqueue("message", incoming(messages))
    await finished(messages, identity)
    messages.review(identity, "approve")
    entered = asyncio.Event()

    async def interrupted(uid, text, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    messages.adapter.send = interrupted
    task = asyncio.create_task(messages.send(identity))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"]
        == "publish_uncertain"
    )
    with pytest.raises(ValueError):
        messages.retry(identity)


async def test_auto_reply_targets_only_new_incoming_and_has_receipt(messages):
    await messages.configure({"enabled": True, "auto_send": True})
    source = incoming(messages)
    identity = messages.enqueue("message", source, automatic=True)
    result = await finished(messages, identity)
    assert result["status"] == "published" and len(messages.adapter.sent) == 1
    assert json.loads(result["receipt"])["receiver_id"] == "123"


async def test_stop_is_checked_again_immediately_before_write(messages):
    identity = messages.enqueue("message", incoming(messages))
    await finished(messages, identity)
    messages.review(identity, "approve")
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def gated(uid, text, *, authorized):
        entered.set()
        await proceed.wait()
        if not authorized():
            raise ValueError("授权关闭")
        raise AssertionError("must not write")

    messages.adapter.send = gated
    task = asyncio.create_task(messages.send(identity))
    await entered.wait()
    await messages.configure({"enabled": False})
    proceed.set()
    with pytest.raises(ValueError, match="授权"):
        await task
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "approved"


async def test_generation_waits_for_pending_live_work(messages):
    runtime = messages.runtime
    await runtime.generation_lock.acquire()
    identity = messages.enqueue("message", incoming(messages))
    await asyncio.sleep(0.02)
    runtime.state = "paused"
    runtime.queue.append("synthetic-pending-live")
    runtime.generation_lock.release()
    await asyncio.sleep(0.04)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"
    runtime.queue.clear()
    runtime.state = "stopped"
    assert (await finished(messages, identity))["status"] == "pending_review"


def test_schema_cannot_choose_an_arbitrary_recipient():
    with pytest.raises(ValueError):
        ChannelDecision.model_validate(
            {"channel": "direct_message", "reason": "user_requested", "recipient": "999"}
        )


def test_message_routes_are_admin_only_and_do_not_offer_arbitrary_send(settings):
    with TestClient(create_app(settings, message_adapter=Delivery())) as client:
        assert client.get("/api/messages").status_code == 401
        client.post("/api/overlay/auth", json={"token": settings.overlay_token})
        assert client.get("/api/messages").status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        policy = client.get("/api/messages").json()["policy"]
        assert not policy["enabled"] and policy["channel_max_tokens"] == 2048
        assert (
            client.put("/api/messages/policy", json={"channel_max_tokens": 4096}).json()["channel_max_tokens"]
            == 4096
        )
        assert client.put("/api/messages/policy", json={"channel_max_tokens": 65537}).status_code == 422
        assert client.post("/api/messages/inbox/999/reply").status_code == 400
        assert client.post(
            "/api/messages/send", json={"receiver_id": "999", "text": "arbitrary"}
        ).status_code in {404, 405}


async def test_failed_channel_generation_preserves_source_without_any_handoff(messages, monkeypatch):
    comments = messages.comments
    comments.import_items(
        "import-budget",
        [
            {
                "comment_id": "c-budget",
                "platform_user_id": "123",
                "content": "不想在公开评论继续展开个人细节。",
            }
        ],
    )
    source = messages.store.one("SELECT * FROM comments")

    async def incomplete(*args, **kwargs):
        raise ValueError("模型结构化输出未完整结束；未采用结果")

    monkeypatch.setattr(messages.runtime.llm, "generate_json", incomplete)
    with pytest.raises(ValueError, match="未完整结束"):
        await comments._process_comment(source, messages.runtime.character, False)
    assert (
        messages.store.one("SELECT content FROM comments WHERE id=?", (source["id"],))["content"]
        == source["content"]
    )
    assert not messages.store.all("SELECT * FROM drafts")
    assert not messages.store.all("SELECT * FROM dm_drafts") and not messages.adapter.sent
