"""Account-bound private context and shared public/private relationship semantics."""

import json
import sqlite3
import time

import pytest

from tuber_studio.comments import Comments
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.dynamics import Dynamics
from tuber_studio.models import Reply
from tuber_studio.operations_control import canonical_intent
from tuber_studio.relationships import Relationships


class Delivery:
    def __init__(self):
        self.sent = []

    async def close(self):
        pass

    async def self_uid(self):
        return "901"

    async def send(self, uid, text, *, authorized):
        if not authorized():
            raise ValueError("已停止")
        self.sent.append((uid, text))
        return {"msg_key": "8"}


@pytest.fixture
async def service(settings, store, runtime):
    comments = Comments(settings, store, runtime)
    value = DirectMessages(settings, store, runtime, comments, Delivery())
    await value.configure({"enabled": True})
    yield value
    await value.close()
    await comments.close()


def incoming(service, account="900", *, key="77", text="私下继续聊", uid="123"):
    return service.ingest(
        {
            "message_id": key,
            "account_id": account,
            "platform_user_id": uid,
            "display_name": "鱼宝",
            "text": text,
            "timestamp": time.time(),
        }
    )[0]


def test_relationship_preserves_legacy_range_and_isolates_accounts_and_ocs(store):
    relations, character = Relationships(store), store.character()
    args = (character["id"], "bilibili", "123", "鱼宝")
    old = relations.observe(*args, "legacy-comment", "你好鱼鱼，今天吃什么")
    assert old["affinity"] == 21
    assert relations.view(*args, account_id="900")["affinity"] == 20
    source = canonical_intent("bilibili", "BV1d8pU6xEZX", "42", character["id"])
    public = relations.observe(*args, source, "我们私信继续聊吧", account_id="900")
    private = relations.observe(*args, source, "我们私信继续聊吧", account_id="900")
    assert public == private and public["affinity"] == 21
    assert relations.view(character["id"], "bilibili", "123", "改名", account_id="900") == public
    assert relations.view(*args, account_id="901")["affinity"] == 20
    assert relations.view("another-oc", "bilibili", "123", account_id="900")["affinity"] == 20
    assert relations.view(character["id"], "bilibili", "456", "鱼宝", account_id="900")["affinity"] == 20
    assert relations.view(*args) == old
    assert store.one("SELECT COUNT(*) AS n FROM account_relation_events")["n"] == 1


async def test_same_platform_message_id_is_distinct_per_account(service):
    first = incoming(service, "900", key="same", text="账号一的来信")
    second = incoming(service, "901", key="same", text="账号二的来信")
    assert first != second
    assert incoming(service, "900", key="same", text="账号一的来信") == first
    first_draft = service.enqueue("message", first)
    second_draft = service.enqueue("message", second)
    assert first_draft != second_draft
    rows = service.store.all("SELECT * FROM dm_drafts ORDER BY id")
    assert [row["account_id"] for row in rows] == ["900", "901"]
    assert service.scope(rows[0]) != service.scope(rows[1])
    assert all(service.scope(row).startswith("dm:") for row in rows)


async def test_private_event_recent_history_and_operator_view_are_account_scoped(service):
    incoming(service, "900", key="old-a", text="只属于甲账号的私密正文")
    incoming(service, "901", key="old-b", text="只属于乙账号的私密正文")
    incoming(service, "", key="legacy", text="没有账号归属的旧私信")
    source = incoming(service, "901", key="new-b", text="继续刚才的事")
    identity = service.enqueue("message", source)
    draft = service.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    event = service._event(draft, service.runtime.character)
    encoded = json.dumps(event, ensure_ascii=False)
    assert "只属于乙账号" in encoded and "只属于甲账号" not in encoded and "没有账号归属" not in encoded
    service.active_account_id, service.active_credential = "901", dict(service.comments.credentials)
    view = service.conversation("bilibili", "123")
    assert {row["account_id"] for row in view["messages"]} == {"901"}
    assert {row["account_id"] for row in service.list()["drafts"]} == {"901"}


async def test_operator_view_without_verified_account_keeps_legacy_private_data_hidden(service):
    incoming(service, "", key="old-unbound", text="未绑定账号的旧私信")
    assert not service.list()["users"]
    assert not service.conversation("bilibili", "123")["messages"]
    assert service.store.one("SELECT content FROM dm_inbox")["content"] == "未绑定账号的旧私信"


async def test_legacy_private_context_is_not_inherited_by_new_account_generation(service, monkeypatch):
    character, context = service.runtime.character, service.runtime.context
    context.record(
        "dm:bilibili:123",
        character,
        {"event_id": "legacy", "text": "旧账号秘密"},
        "秘密地址是火星基地99号。",
        "direct_message",
    )
    context.record(
        'dm:["bilibili","900","123"]',
        character,
        {"event_id": "other", "text": "甲账号秘密"},
        "秘密口令是海底珍珠。",
        "direct_message",
    )
    prompts = []

    async def generate(messages, character, event_id, source_text=None):
        prompts.extend(messages)
        return Reply(spoken_text="鱼宝，我们慢慢聊。", reply_to=[event_id])

    monkeypatch.setattr(service.runtime.llm, "generate", generate)
    identity = service.enqueue("message", incoming(service, "901"))
    await service._generate(identity, False)
    prompt = json.dumps(prompts, ensure_ascii=False)
    assert "火星基地" not in prompt and "海底珍珠" not in prompt
    assert context.existing("dm:bilibili:123", character)
    assert context.existing('dm:["bilibili","900","123"]', character)


async def test_account_scoped_private_stories_cannot_enter_public_dynamic_sources(service):
    scope = 'dm:["bilibili","900","123"]'
    service.runtime.context.record(
        scope,
        service.runtime.character,
        {"event_id": "secret", "text": "私密话题"},
        "在虚构的私密花园里，我又盛了一碗饭。",
        "direct_message",
    )
    dynamic = Dynamics(service.settings, service.store, service.runtime, service.comments)
    assert not dynamic.list_memories()


async def test_public_handoff_context_and_account_binding_are_retained(service):
    service.comments.import_items(
        "BV1d8pU6xEZX", [{"comment_id": "42", "content": "私信继续聊吧", "platform_user_id": "123"}]
    )
    source = service.store.one("SELECT id FROM comments")["id"]
    evidence = {
        "material_keys": ["material-a"],
        "public_source_thread": {"public_text": "来自真实公开楼的原文"},
    }
    first = service.enqueue("comment", source, account_id="900", video_evidence=evidence)
    second = service.enqueue("comment", source, account_id="901", video_evidence=evidence)
    assert first != second
    draft = service.store.one("SELECT * FROM dm_drafts WHERE id=?", (first,))
    event = service._event(draft, service.runtime.character)
    assert event["video_evidence"] == evidence
    assert event["public_source_thread"] == evidence["public_source_thread"]


async def test_account_switch_refuses_private_generation_and_write(service, monkeypatch):
    identity = service.enqueue("message", incoming(service, "900"))
    service.injected = False
    # The fixed adapter replaces _adapter(), including its credential binding.
    service.credential_signature = (
        service.comments.credentials.get("sessdata"),
        service.comments.credentials.get("csrf"),
    )

    async def adapter():
        return service.adapter

    monkeypatch.setattr(service, "_adapter", adapter)
    with pytest.raises(ValueError, match="其他账号"):
        await service._generate(identity, False)
    service.store.execute(
        "UPDATE dm_drafts SET status='approved',text='不能发给其他账号的用户。' WHERE id=?", (identity,)
    )
    with pytest.raises(ValueError, match="其他账号"):
        await service.send(identity)
    assert service.adapter.sent == []


@pytest.mark.parametrize("corrupt", [False, True])
async def test_inbox_account_migration_preserves_ids_and_rolls_back_on_copy_error(
    settings, store, runtime, corrupt
):
    comments = Comments(settings, store, runtime)
    store.execute(
        "CREATE TABLE dm_inbox(id INTEGER PRIMARY KEY,platform TEXT,message_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,content TEXT NOT NULL,sent_at REAL,character_id TEXT,received_at REAL,UNIQUE(platform,message_id))"
    )
    store.execute(
        "INSERT INTO dm_inbox VALUES(55,?,'legacy','123','旧用户','未绑定账号的旧原文',?,?,?)",
        (None if corrupt else "bilibili", time.time(), runtime.character["id"], time.time()),
    )
    try:
        if corrupt:
            with pytest.raises(sqlite3.IntegrityError):
                DirectMessages(settings, store, runtime, comments, Delivery())
            assert not store.one("SELECT name FROM sqlite_master WHERE name='dm_inbox_scoped'")
        else:
            service = DirectMessages(settings, store, runtime, comments, Delivery())
            assert store.one("SELECT account_id FROM dm_inbox WHERE id=55")["account_id"] == ""
            assert incoming(service, "900", key="legacy", text="新账号原文") != 55
            reopened = DirectMessages(settings, store, runtime, comments, Delivery())
            assert store.one("SELECT COUNT(*) AS n FROM dm_inbox")["n"] == 2
            await reopened.close()
            await service.close()
        assert store.one("SELECT content FROM dm_inbox WHERE id=55")["content"] == "未绑定账号的旧原文"
    finally:
        await comments.close()
