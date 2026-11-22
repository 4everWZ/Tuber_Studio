"""Shared service fixtures and test workflow builders across channels and features."""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tuber_studio.infra.platforms.bilibili.account import BilibiliAccount
from tuber_studio.operations.channels.comments import Comments
from tuber_studio.operations.channels.direct_messages import DirectMessages
from tuber_studio.operations.channels.dynamics import Dynamics
from tuber_studio.operations.channels.dynamics import publisher as dynamics_module
from tuber_studio.operations.kernel.models import BrowsingPolicy
from tuber_studio.operations.service import BrowsingService
from tuber_studio.persona.memory.core_memory import GroundedMemory

from .fake_bilibili import BV, CommentPlatform, ControlledPlatform, DMDelivery
from .fake_providers import ControlledModel, DynamicModel


def direct_request(text, *, mode="read", target=None, followup=False):
    return {
        "action": "direct_tool",
        "operation": {
            "request": text,
            "target": target or text,
            "mode": mode,
            "followup": followup,
        },
    }


@pytest.fixture
async def browsing(settings, store, runtime):
    adapter = ControlledPlatform()
    account = BilibiliAccount(settings, store, adapter)
    comments = Comments(settings, store, runtime, account)

    class MessageTransport:
        async def sessions(self):
            return "100", []

        async def close(self):
            pass

    messages = DirectMessages(settings, store, runtime, comments, MessageTransport(), account=account)
    dynamics = Dynamics(settings, store, runtime, comments, account=account)
    comments.direct_messages = messages
    service = BrowsingService(
        settings, store, runtime, comments, messages, dynamics, read_interval=0, bilibili_account=account
    )
    runtime.browsing = service
    previous = runtime.operation_llm
    runtime.operation_llm = ControlledModel()
    settings.data["omni"]["model"] = "omni-default"
    service.store.set("browsing_policy", BrowsingPolicy(top_level_enabled=True).model_dump())
    yield service
    await service.close()
    await comments.close()
    await account.close()
    runtime.operation_llm = previous


async def authorize_automation(service):
    """Explicit test permissions; starting the parent never adds capabilities."""
    service.store.set(
        "browsing_policy",
        service.store.get("browsing_policy", {})
        | {
            "discovery_enabled": True,
            "top_level_enabled": True,
            "top_level_auto_publish": True,
            "reply_enabled": True,
            "reply_auto_publish": True,
        },
    )
    await service.set_enabled(True)


async def start_automation(service):
    await service.open()
    await authorize_automation(service)
    return service.status()


async def browsing_finished(service, **args):
    if args.get("automatic") and not service.operations.policy()["enabled"]:
        await authorize_automation(service)
    result = await service.run(**args)
    task = service.active.get(result["id"])
    if task:
        await asyncio.wait_for(task, 3)
    return service.repo.round(result["id"])


async def wait_for_sends(service):
    async with asyncio.timeout(2):
        while service.sends.active or service.store.one(
            "SELECT 1 FROM operation_send_queue WHERE wake_at<=? LIMIT 1", (time.time(),)
        ):
            await asyncio.sleep(0.005)


@pytest.fixture
async def comments(settings, store, runtime, monkeypatch):
    async def generate(scope, character, event, relationship=None, mode="live", *, context=None):
        messages = await (context or runtime.context).prepare(scope, character, event, relationship, mode)
        reply = await runtime.operation_llm.generate(messages, character, event["event_id"], event["text"])
        return reply, {"sources": [], "missing": [], "retried": False, "memory_kind": "role_story"}

    monkeypatch.setattr(runtime, "generate_reply", generate, raising=False)
    account = BilibiliAccount(settings, store, CommentPlatform())
    service = Comments(settings, store, runtime, account)
    service.worker = asyncio.create_task(service._worker())
    yield service
    await service.close()
    await account.close()


async def draft(comments, store, automatic=False):
    comments.import_items(BV, [{"comment_id": "123", "content": "鱼鱼今天吃什么？"}])
    cid = store.one("SELECT id FROM comments")["id"]
    comments.enqueue([cid], automatic=automatic)
    await asyncio.wait_for(comments.queue.join(), 3)
    return store.one("SELECT * FROM drafts")


@pytest.fixture
async def messages(runtime, settings, store):
    account = BilibiliAccount(settings, store, None)
    comments = Comments(settings, store, runtime, account)
    delivery = DMDelivery()
    service = DirectMessages(settings, store, runtime, comments, delivery, account=account)
    comments.direct_messages = service
    await service.configure({"enabled": True})
    await service.open()
    yield service
    await service.close()
    await comments.close()
    await account.close()


async def dm_finished(messages, identity):
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


@pytest.fixture
def service(settings, store, monkeypatch):
    account = SimpleNamespace(
        adapter=object(),
        credential_lock=asyncio.Lock(),
        credentials={},
        credentials_configured=True,
        display_account_id=lambda: "",
    )
    comments = SimpleNamespace(account=account)
    runtime = SimpleNamespace(operation_llm=DynamicModel(), generation_lock=asyncio.Lock())
    runtime.offline_generation = lambda lane: runtime.generation_lock
    service = Dynamics(settings, store, runtime, comments, account=account)
    service.operations.configure(
        {"enabled": True}, account_id="injected-test-account", character=store.character()
    )
    service.sent = []

    async def send(adapter, text, *, before_send=None):
        if before_send:
            before_send()
        service.sent.append((adapter, text))
        return {"platform": "bilibili", "code": 0, "dynamic_id": "12345"}

    monkeypatch.setattr(dynamics_module, "publish_text", send)
    return service


def public_memory(service, text="在虚构的海底厨房，我又盛了一碗饭。", *, role="assistant", kind=None):
    GroundedMemory(service.store).record(
        "test", service.store.character(), str(uuid4()), role, text, kind=kind
    )
    identifier = service.store.one("SELECT MAX(id) AS id FROM grounded_memory")["id"]
    service.mark_memory(identifier, True)
    return identifier


def published_reply(service):
    char = service.store.character()
    cid = service.store.execute(
        "INSERT INTO comments(video_id,comment_id,content) VALUES(?,?,?)",
        (BV, "42", "鱼鱼的海底厨房是什么样？"),
    ).lastrowid
    return service.store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,receipt) VALUES(?,?,?,?,?,?)",
        (cid, "想象中的海底厨房有热饭。", "published", char["id"], char["version"], '{"rpid":"77"}'),
    ).lastrowid
