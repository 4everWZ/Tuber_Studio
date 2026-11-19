import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from test_browsing import BV, finished, wait_for_sends
from test_browsing import browsing as browsing
from test_direct_messages import Delivery, incoming
from tuber_studio import operation_attachments, operation_images
from tuber_studio.operations.browsing.quotas import day_window
from tuber_studio.publication_errors import PublishUncertain


def arrival(browsing):
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('new','100','reply','502',?,'received',?,NULL,?)",
        (time.time(), '{"event_id":"new","kind":"reply","resource_type":"video","oid":"42","root":"501",'
                      '"comment_id":"502","sender_uid":"300"}', time.time()),
    )


async def test_ready_direct_reply_gets_last_shared_send_slot(browsing, monkeypatch):
    await browsing.configure({"max_per_hour": 2, "max_per_day": 2, "recipient_cooldown_seconds": 0,
                              "top_level_auto_publish": True, "reply_enabled": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    await browsing.sends.close()

    # Freeze the sender until both intents are ready, then run the real quota ledger.
    start = browsing.sends.start
    browsing.sends.start = lambda: None
    await finished(browsing, automatic=True, video_only=True)
    top = next(item for item in browsing.intents() if item["kind"] == "top_level")
    arrival(browsing)
    await finished(browsing, automatic=True, interaction_only=True)
    monkeypatch.setattr(browsing.comments, "publication_limits", lambda: {"max_per_hour": 1, "max_per_day": 1})
    browsing.sends.start = start
    start()
    await wait_for_sends(browsing)
    assert len(browsing.adapter.sent) == 1 and browsing.adapter.sent[0][0]["parent"] == "502"
    deferred, = browsing.store.all("SELECT * FROM operation_send_queue")
    assert deferred["source_id"] == top["id"] and deferred["wake_at"] > time.time()
    assert len(browsing.store.all("SELECT * FROM account_writes")) == 1
    assert (await finished(browsing, automatic=True, video_only=True))["id"] != top["round_id"]

    await browsing.stop()
    await browsing.set_enabled(True)
    browsing.store.execute("UPDATE account_writes SET started_at=started_at-90000")
    browsing.store.execute("UPDATE operation_send_queue SET wake_at=?", (time.time(),))
    browsing.sends.changed.set()
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["502", "0"]


@pytest.mark.parametrize("limits", [
    {"max_per_hour": 1, "max_per_day": 1},
    {"max_per_hour": 0, "max_per_day": 0, "max_replies_per_video": 1},
])
async def test_last_comment_slot_covers_generation_queue_and_submission(browsing, monkeypatch, limits):
    await browsing.configure(limits | {"recipient_cooldown_seconds": 0,
                              "top_level_auto_publish": True, "reply_enabled": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    await browsing.sends.close()
    start = browsing.sends.start
    monkeypatch.setattr(browsing.sends, "start", lambda: None)
    entered, release = asyncio.Event(), asyncio.Event()
    original = browsing.decisions._response

    async def response(row, material, context, allowed, kind, metrics, **options):
        assert kind == "top_level", "a reply without a free slot reached drafting"
        entered.set()
        await release.wait()
        return await original(row, material, context, allowed, kind, metrics, **options)

    monkeypatch.setattr(browsing.decisions, "_response", response)
    first = await browsing.run(automatic=True, video_only=True)
    task = browsing.active[first["id"]]
    await asyncio.wait_for(entered.wait(), 2)
    arrival(browsing)
    await finished(browsing, automatic=True, interaction_only=True)
    assert not browsing.intents()
    assert not browsing.store.all("SELECT * FROM operation_image_jobs")
    blocked = browsing.store.one("SELECT status,error FROM browsing_events WHERE id='new'")
    assert blocked["status"] == "received" and "暂不拟稿" in json.loads(blocked["error"])["message"]

    release.set()
    await asyncio.wait_for(task, 2)
    assert len(browsing.store.all("SELECT * FROM operation_send_queue")) == 1
    browsing.store.execute("UPDATE browsing_events SET error=NULL WHERE id='new'")
    await finished(browsing, automatic=True, interaction_only=True)
    assert [row["kind"] for row in browsing.intents()] == ["top_level"]
    assert not browsing.adapter.sent

    transmitting, receipt = asyncio.Event(), asyncio.Event()

    async def publish(target, text, *, before_send):
        before_send()
        transmitting.set()
        await receipt.wait()
        return {"code": 0, "rpid": "900"}

    monkeypatch.setattr(browsing.adapter, "publish_top_level", publish)
    start()
    await asyncio.wait_for(transmitting.wait(), 2)
    assert browsing.store.one("SELECT resolved_status FROM operation_write_attempts")["resolved_status"] == "sending"
    browsing.store.execute("UPDATE browsing_events SET error=NULL WHERE id='new'")
    await finished(browsing, automatic=True, interaction_only=True)
    assert [row["kind"] for row in browsing.intents()] == ["top_level"]
    receipt.set()
    await wait_for_sends(browsing)


async def test_daily_capacity_resumes_original_source_after_local_midnight(browsing, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await browsing.configure({"max_per_hour": 0, "max_per_day": 0, "max_replies_per_video": 1,
                              "recipient_cooldown_seconds": 0, "top_level_auto_publish": True,
                              "reply_enabled": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    await finished(browsing, automatic=True, video_only=True)
    await wait_for_sends(browsing)
    assert len(browsing.adapter.sent) == 1

    arrival(browsing)
    calls = len(browsing.runtime.operation_llm.calls)
    await finished(browsing, automatic=True, interaction_only=True)
    event = browsing.store.one("SELECT status,error FROM browsing_events WHERE id='new'")
    assert event["status"] == "received"
    assert json.loads(event["error"])["next_at"] == day_window(now=clock[0])["next_at"]
    assert len(browsing.runtime.operation_llm.calls) == calls
    assert not browsing.store.all("SELECT * FROM operation_send_queue")

    model = browsing.runtime.operation_llm
    generate = model.generate_json

    async def new_reply(*args, **kwargs):
        draft = await generate(*args, **kwargs)
        return draft | {"text": "多谢你补了这条，鱼鱼这次知道该留意实验的哪一步啦。"}

    monkeypatch.setattr(model, "generate_json", new_reply)
    clock[0] = day_window(now=clock[0])["next_at"] + 1
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["0", "502"]
    assert browsing.store.one("SELECT status,error FROM browsing_events WHERE id='new'") == {
        "status": "drafted", "error": None,
    }


async def test_stop_preserves_ready_send_until_reactivation(browsing):
    entered, release = asyncio.Event(), asyncio.Event()

    async def before_submit():
        entered.set()
        await release.wait()

    browsing.adapter.before_submit = before_submit
    await browsing.configure({"top_level_auto_publish": True})
    await browsing.set_enabled(True)
    await finished(browsing, automatic=True, video_only=True)
    await asyncio.wait_for(entered.wait(), 2)
    assert (await browsing.stop())["state"] == "draining"
    drain = browsing.drain_task
    release.set()
    await asyncio.wait_for(drain, 2)
    assert not browsing.adapter.sent
    assert not browsing.store.all("SELECT * FROM account_writes")
    assert browsing.store.one("SELECT source_id FROM operation_send_queue")
    browsing.adapter.before_submit = None
    await browsing.set_enabled(True)
    await wait_for_sends(browsing)
    assert len(browsing.adapter.sent) == 1 and browsing.adapter.sent[0][0]["bvid"] == BV


@pytest.mark.parametrize("outcome", ["published", "pending_review", "publish_uncertain"])
async def test_video_round_waits_for_illustration_and_send_but_direct_lanes_continue(
    browsing, monkeypatch, outcome,
):
    auto_publish = outcome != "pending_review"
    deciding, finish_image = asyncio.Event(), asyncio.Event()
    submitting, finish_send = asyncio.Event(), asyncio.Event()

    async def choose(settings, material):
        deciding.set()
        await finish_image.wait()
        return None

    async def before_submit():
        submitting.set()
        await finish_send.wait()

    monkeypatch.setattr(operation_attachments, "gateway_base", lambda _: "http://fixture/v1")
    monkeypatch.setattr(operation_images, "resolve_reference", lambda *_: SimpleNamespace(source="fixture", sha256="ref"))
    monkeypatch.setattr(operation_images, "choose", choose)
    delivery = Delivery()

    async def sessions():
        return "100", ["123"]

    monkeypatch.setattr(delivery, "sessions", sessions)
    monkeypatch.setattr(browsing.messages, "adapter", delivery)
    await browsing.messages.configure({"enabled": True, "auto_send": True, "generate_images": False})
    await browsing.messages.open()
    await browsing.configure({"top_level_auto_publish": auto_publish, "reply_enabled": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    first = await finished(browsing, automatic=True, video_only=True)
    await asyncio.wait_for(deciding.wait(), 2)
    draft, = browsing.intents()
    job_id = draft["target"]["image_ref"]["job_id"]

    async def check_blocked():
        result = await browsing.run(automatic=True, video_only=True)
        assert result == {"status": "deferred", "reason": "上一轮配图或自动发送尚未结清"}
        assert browsing.store.one("SELECT COUNT(*) AS n FROM browsing_batches")["n"] == 1

    await check_blocked()
    arrival(browsing)
    dm_id = browsing.messages.enqueue("message", incoming(browsing.messages, account_id="100"), automatic=True)
    await finished(browsing, automatic=True, interaction_only=True)
    async with asyncio.timeout(2):
        while not browsing.adapter.sent or not delivery.sent:
            await asyncio.sleep(0.01)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["502"]
    assert len(delivery.sent) == 1 and delivery.sent[0][0] == "123"
    assert browsing.store.one("SELECT status FROM dm_drafts WHERE id=?", (dm_id,))["status"] == "published"
    assert browsing.store.one("SELECT status FROM operation_image_jobs WHERE job_id=?", (job_id,))["status"] == "deciding"
    await check_blocked()

    browsing.adapter.before_submit = before_submit
    if outcome == "publish_uncertain":
        browsing.adapter.failure = PublishUncertain("模拟提交后断线")
    finish_image.set()
    if auto_publish:
        await asyncio.wait_for(submitting.wait(), 2)
        await check_blocked()
        finish_send.set()
        await wait_for_sends(browsing)
    else:
        async with asyncio.timeout(2):
            while browsing.store.one("SELECT status FROM operation_image_jobs WHERE job_id=?", (job_id,))["status"] != "text_only":
                await asyncio.sleep(0.01)
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["id"] != first["id"]
    assert browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (draft["id"],))["status"] == outcome
    assert [target["parent"] for target, _ in browsing.adapter.sent] == (["502", "0"] if auto_publish else ["502"])


async def test_video_round_waits_for_handoff_preparation_until_deferred(browsing):
    await browsing.messages.configure({"enabled": True, "allow_comment_handoff": True})
    await browsing.set_enabled(True)
    first = await finished(browsing, automatic=True, video_only=True)
    browsing.comments.import_items(BV, [{
        "comment_id": "601", "thread_id": "601", "platform_user_id": "300", "content": "请私信聊聊盐水实验。",
    }])
    source = browsing.store.one("SELECT id FROM comments WHERE comment_id='601'")
    draft_id = browsing.messages.enqueue("comment", source["id"], account_id="100", schedule=False,
                                         managed_handoff=True, video_evidence={"origin_round_id": first["id"]})
    assert (await browsing.run(automatic=True, video_only=True))["status"] == "deferred"

    browsing.store.execute("UPDATE dm_drafts SET next_attempt_at=? WHERE id=?", (time.time() + 60, draft_id))
    assert (await finished(browsing, automatic=True, video_only=True))["id"] != first["id"]
    assert browsing.store.one("SELECT status FROM dm_drafts WHERE id=?", (draft_id,))["status"] == "queued"
