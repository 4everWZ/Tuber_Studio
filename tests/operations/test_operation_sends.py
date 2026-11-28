import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tests.support.operations_helpers import BV, Delivery, finished, incoming, wait_for_sends
from tests.support.operations_helpers import browsing as browsing
from tuber_studio.core.errors import PublishUncertain
from tuber_studio.operations.channels.attachments import attachments as operation_attachments
from tuber_studio.operations.channels.attachments import images as operation_images
from tuber_studio.operations.kernel.quotas import day_window


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
    wait = await browsing.run(automatic=True, video_only=True)
    assert wait["status"] == "deferred" and wait["next_at"] > time.time()

    await browsing.stop()
    await browsing.set_enabled(True)
    browsing.store.execute("UPDATE account_writes SET started_at=started_at-90000")
    browsing.store.execute("UPDATE operation_send_queue SET wake_at=?", (time.time(),))
    browsing.sends.changed.set()
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["502", "0"]


@pytest.mark.parametrize("limits", [
    {"max_per_hour": 1, "max_per_day": 0, "max_replies_per_video": 0},
    {"max_per_hour": 0, "max_per_day": 1, "max_replies_per_video": 0},
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


@pytest.mark.parametrize(("hourly", "daily", "window"), [(1, 0, 3600), (0, 1, 86400), (1, 1, 86400)])
async def test_replies_wait_for_every_exhausted_window(browsing, monkeypatch, hourly, daily, window):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await browsing.configure({
        "max_per_hour": hourly, "max_per_day": daily,
        "max_replies_per_video": 0, "recipient_cooldown_seconds": 0,
        "top_level_auto_publish": True, "reply_enabled": True, "reply_auto_publish": True,
    })
    await browsing.set_enabled(True)
    await finished(browsing, automatic=True, video_only=True)
    await wait_for_sends(browsing)
    started = browsing.store.one("SELECT started_at FROM operation_write_attempts")["started_at"]

    arrival(browsing)
    calls = len(browsing.runtime.operation_llm.calls)
    await finished(browsing, automatic=True, interaction_only=True)
    event = browsing.store.one("SELECT status,error FROM browsing_events WHERE id='new'")
    assert event["status"] == "received" and json.loads(event["error"])["next_at"] == started + window
    assert len(browsing.runtime.operation_llm.calls) == calls
    assert not browsing.store.all("SELECT * FROM operation_send_queue")

    if hourly and daily:
        clock[0] = started + 3601
        await finished(browsing, automatic=True, interaction_only=True)
        assert len(browsing.runtime.operation_llm.calls) == calls
        assert not browsing.store.all("SELECT * FROM operation_send_queue")

    model = browsing.runtime.operation_llm
    generate = model.generate_json

    async def new_reply(*args, **kwargs):
        return (await generate(*args, **kwargs)) | {"text": "谢谢你补充实验后的变化，鱼鱼记下啦。"}

    monkeypatch.setattr(model, "generate_json", new_reply)
    clock[0] = started + window + 1
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["0", "502"]


async def test_comment_slots_do_not_reserve_private_message_or_dynamic_capacity(browsing, monkeypatch):
    browsing.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"max_per_hour": 1, "max_per_day": 0, "recipient_cooldown_seconds": 0,
                              "top_level_auto_publish": True})
    await browsing.messages.configure({"enabled": True, "auto_send": True})
    await browsing.dynamics.set_policy({"enabled": True})
    await browsing.set_enabled(True)
    await browsing.sends.close()
    monkeypatch.setattr(browsing.sends, "start", lambda: None)
    await finished(browsing, automatic=True, video_only=True)
    assert browsing.store.one("SELECT channel FROM operation_send_queue") == {"channel": "browsing"}

    message_id = incoming(browsing.messages, account_id="100")
    browsing.messages._queue_newest_messages("100")
    assert browsing.store.one("SELECT source_id FROM dm_drafts") == {"source_id": message_id}
    assert browsing.sends.preparation_block("100", "dynamics", browsing.dynamics.policy(), retry_seconds=60) is None
    assert browsing.sends.preparation_block("100", "comments", browsing.comments.publication_limits(), retry_seconds=60)


@pytest.mark.parametrize("account_daily", [0, 1])
async def test_video_daily_capacity_waits_for_local_midnight_and_account_window(browsing, monkeypatch, account_daily):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await browsing.configure({"max_per_hour": 0, "max_per_day": account_daily, "max_replies_per_video": 1,
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
    midnight = day_window(now=clock[0])["next_at"]
    expected = clock[0] + 86400 if account_daily else midnight
    assert json.loads(event["error"])["next_at"] == expected
    assert len(browsing.runtime.operation_llm.calls) == calls
    assert not browsing.store.all("SELECT * FROM operation_send_queue")

    model = browsing.runtime.operation_llm
    generate = model.generate_json

    async def new_reply(*args, **kwargs):
        draft = await generate(*args, **kwargs)
        return draft | {"text": "多谢你补了这条，鱼鱼这次知道该留意实验的哪一步啦。"}

    monkeypatch.setattr(model, "generate_json", new_reply)
    clock[0] = expected + 1
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
    browsing.store.set("illustration_policy", {"use_memes": False, "generate_images": True})
    await browsing.messages.configure({"enabled": True, "auto_send": True})
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
    # Subsequent direct messages use text while the original decision is still running.
    browsing.store.set("illustration_policy", {"use_memes": False, "generate_images": False})
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


async def test_consecutive_invisible_comments_arm_30m_cooldown_and_auto_downgrade(browsing):
    from tuber_studio.core.errors import platform_recovery_condition

    for code in (-352, -412, -509, 12014, 12015):
        assert platform_recovery_condition(code) == "rate_limit"

    await browsing.configure(
        {
            "top_level_enabled": True,
            "top_level_auto_publish": True,
            "reply_enabled": True,
            "reply_auto_publish": True,
            "auto_downgrade_on_error": True,
        }
    )
    now = 2_000_000.0
    first = browsing.sends.record_visibility_outcome("100", "self_only", intent_id="i-1", bvid=BV, now=now)
    assert first is not None and first["streak"] == 1 and first["active"] is False
    assert browsing.sends.comments_cooldown_until("100") == 0.0

    # Re-verifying the same intent does not double-count the streak
    repeated = browsing.sends.record_visibility_outcome("100", "self_only", intent_id="i-1", bvid=BV, now=now + 5)
    assert repeated["streak"] == 1 and repeated["active"] is False

    second = browsing.sends.record_visibility_outcome("100", "shadow_deleted", intent_id="i-2", bvid=BV, now=now + 10)
    assert second is not None and second["streak"] == 2 and second["active"] is True
    assert second["downgraded"] is True
    assert second["cooldown_until"] == now + 10 + 1800
    assert browsing.sends.comments_cooldown_until("100") == now + 10 + 1800
    assert browsing.policy()["top_level_auto_publish"] is False
    assert browsing.policy()["reply_auto_publish"] is False

    late = browsing.sends.record_visibility_outcome("100", "shadow_deleted", intent_id="i-2", now=now + 180)
    assert late["streak"] == 2
    assert late["cooldown_until"] == now + 10 + 1800
    expired = browsing.sends.record_visibility_outcome("100", "shadow_deleted", intent_id="i-2", now=now + 2000)
    assert not expired["active"] and not expired["cooldown_until"]
    assert "冷却已结束" in expired["reason"]
    assert browsing.sends.comments_cooldown_until("100") == now + 10 + 1800

    # A subsequent public_visible outcome resets the streak
    cleared = browsing.sends.record_visibility_outcome("100", "public_visible", intent_id="i-3", bvid=BV, now=now + 20)
    assert cleared is None
    assert browsing.sends.visibility_risk_status("100", now=now + 20) is None


def test_old_visibility_readback_does_not_extend_or_reset_a_new_account_episode(browsing):
    sends = browsing.sends
    old = 2_000_000.0
    sends.record_visibility_outcome("100", "self_only", intent_id="older", now=old + 10, published_at=old)
    previous = sends.record_visibility_outcome("100", "self_only", intent_id="older-b", now=old + 25, published_at=old + 20)
    assert previous["active"]
    now = old + 3600
    first = sends.record_visibility_outcome("100", "self_only", intent_id="new-a", now=now, published_at=now - 10)
    assert first["streak"] == 1 and not first["active"]
    assert not first["cooldown_until"] and "连续 1/2 次" in first["reason"]
    late = sends.record_visibility_outcome("100", "public_visible", intent_id="older", now=now + 1, published_at=old)
    assert late["streak"] == 1 and late["last_intent_id"] == "new-a"
    delayed = sends.record_visibility_outcome("100", "self_only", intent_id="old-draft", now=now + 1, published_at=old)
    assert delayed["streak"] == 1
    second = sends.record_visibility_outcome("100", "self_only", intent_id="new-b", now=now + 10, published_at=now + 2)
    assert second["streak"] == 2 and second["active"]


async def test_send_queue_enforces_comment_same_video_and_reaction_pacing(browsing, monkeypatch):
    from tuber_studio.operations.kernel.control import OperationsPolicy

    defaults = OperationsPolicy()
    assert defaults.min_comment_interval_seconds == 45
    assert defaults.min_reaction_interval_seconds == 8

    clock = [2_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(browsing.sends, "_pacing_jitter", lambda _base: 0.0)
    await browsing.configure({"top_level_auto_publish": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    browsing.operations.configure({"min_comment_interval_seconds": 30, "min_reaction_interval_seconds": 10})
    await browsing.sends.close()

    def _insert_intent(intent_id: str, kind: str, bvid: str, root: str):
        target = json.dumps({"resource_type": "video", "oid": "42", "bvid": bvid, "root": root, "parent": root})
        browsing.store.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,"
            "epoch,auto_authorized,status,text,reason,target,evidence,material_keys,input_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent_id, "100", "whale", 1, f"k-{intent_id}", kind, "r-1",
                browsing.operations.policy()["epoch"], 0, "approved", "好耶", "", target,
                "[]", "[]", "v1", clock[0], clock[0],
            ),
        )

    browsing.store.execute(
        "INSERT OR IGNORE INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,epoch,config,created_at,updated_at) "
        "VALUES('r-1','100','whale',1,'finished',0,0,'{}',?,?)",
        (clock[0], clock[0]),
    )
    bv_a = BV
    bv_b = "BV1other00002"
    _insert_intent("c-a1", "reply_public", bv_a, "101")
    _insert_intent("c-a2", "reply_public", bv_a, "102")
    _insert_intent("c-b1", "reply_public", bv_b, "201")
    _insert_intent("r-1", "comment_like", bv_a, "301")
    _insert_intent("r-2", "comment_like", bv_a, "302")

    sent: list[tuple[str, float]] = []

    async def fake_dispatch(item):
        sent.append((item["source_id"], clock[0]))
        return True

    browsing.sends.dispatch = fake_dispatch
    for idx, sid in enumerate(("r-1", "r-2", "c-a1", "c-a2", "c-b1")):
        browsing.sends.enqueue("browsing", sid, approval=f"app-{idx}")

    async def _step_wait():
        await asyncio.sleep(0.05)

    # First comment c-a1 and first reaction r-1 send immediately at t=0.
    await _step_wait()
    assert [s[0] for s in sent] == ["c-a1", "r-1"]

    # At +11s: r-2 passes 10s reaction pacing; comments still gated until +30s.
    clock[0] += 11
    browsing.sends.changed.set()
    await _step_wait()
    assert [s[0] for s in sent] == ["c-a1", "r-1", "r-2"]

    # At +31s: c-b1 (different video) sends, while same-video c-a2 stays deferred until +60s!
    clock[0] += 20
    browsing.sends.changed.set()
    await _step_wait()
    assert [s[0] for s in sent] == ["c-a1", "r-1", "r-2", "c-b1"]

    # At +62s (>= 30s after c-b1 and >= 60s after c-a1 on bv_a): c-a2 sends.
    clock[0] += 31
    browsing.sends.changed.set()
    await _step_wait()
    assert [s[0] for s in sent] == ["c-a1", "r-1", "r-2", "c-b1", "c-a2"]

    # Setting pacing to 0 disables pacing immediately.
    _insert_intent("c-a3", "reply_public", bv_a, "103")
    browsing.operations.configure({"min_comment_interval_seconds": 0, "min_reaction_interval_seconds": 0})
    browsing.sends.enqueue("browsing", "c-a3", approval="app-z")
    await _step_wait()
    assert sent[-1][0] == "c-a3"


async def test_widened_jitter_and_interleaved_same_floor_pacing(browsing, monkeypatch):
    samples = [browsing.sends._pacing_jitter(45) for _ in range(40)]
    assert all(45 * 0.25 <= val <= 45 * 1.15 for val in samples)
    assert max(samples) - min(samples) > 10.0
    assert browsing.sends._pacing_jitter(0) == 0.0

    clock = [3_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(browsing.sends, "_pacing_jitter", lambda _base: 0.0)
    await browsing.configure({"top_level_auto_publish": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    browsing.operations.configure({"min_comment_interval_seconds": 30, "min_reaction_interval_seconds": 0})
    await browsing.sends.close()

    def _insert(intent_id: str, bvid: str, root: str, parent: str):
        target = json.dumps({"resource_type": "video", "oid": "42", "bvid": bvid, "root": root, "parent": parent})
        browsing.store.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,"
            "epoch,auto_authorized,status,text,reason,target,evidence,material_keys,input_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent_id, "100", "whale", 1, f"k-{intent_id}", "reply_public", "r-2",
                browsing.operations.policy()["epoch"], 0, "approved", "好耶", "", target,
                "[]", "[]", "v1", clock[0], clock[0],
            ),
        )

    browsing.store.execute(
        "INSERT OR IGNORE INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,epoch,config,created_at,updated_at) "
        "VALUES('r-2','100','whale',1,'finished',0,0,'{}',?,?)",
        (clock[0], clock[0]),
    )
    bv_a = BV
    bv_b = "BV1other00002"
    _insert("f-a1", bv_a, "101", "101")
    _insert("f-b1", bv_b, "201", "201")
    _insert("f-a1-dup", bv_a, "101", "102")

    sent = []

    async def fake_dispatch(item):
        sent.append(item["source_id"])
        return True

    browsing.sends.dispatch = fake_dispatch
    for idx, sid in enumerate(("f-a1", "f-b1", "f-a1-dup")):
        browsing.sends.enqueue("browsing", sid, approval=f"app-{idx}")

    await asyncio.sleep(0.05)
    assert sent == ["f-a1"]

    # At +31s: f-b1 sends, but f-a1-dup is in the same floor (bv_a/101) so it stays blocked even after f-b1.
    clock[0] += 31
    browsing.sends.changed.set()
    await asyncio.sleep(0.05)
    assert sent == ["f-a1", "f-b1"]

    # At +65s (>= 30s after f-b1 and >= 60s same-video, but < 90s same-floor): f-a1-dup is STILL deferred!
    clock[0] += 34
    browsing.sends.changed.set()
    await asyncio.sleep(0.05)
    assert sent == ["f-a1", "f-b1"]

    # At +92s (>= 90s same-floor window): f-a1-dup sends.
    clock[0] += 27
    browsing.sends.changed.set()
    await asyncio.sleep(0.05)
    assert sent == ["f-a1", "f-b1", "f-a1-dup"]
