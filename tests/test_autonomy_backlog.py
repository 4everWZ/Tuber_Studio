import asyncio
import json
import time

import httpx
from test_browsing import BV, finished, wait_for_sends
from test_browsing import browsing as browsing
from test_browsing_comment_only import comment, enable_comment_choices
from test_direct_messages import incoming

from tuber_studio.comment_workbench import prepare_queue_selection, review_queue_selection
from tuber_studio.core.errors import PublishUncertain
from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.operations.browsing.backlog import inventory
from tuber_studio.operations.browsing.comment_reader import admit_comment_page, resume_comment_page
from tuber_studio.operations.browsing.models import ResponseDecision


async def approved_replies(browsing, count, monkeypatch):
    await browsing.configure({"max_per_hour": 3, "max_per_day": 0, "max_replies_per_video": 0,
                              "recipient_cooldown_seconds": 0, "reply_enabled": True})
    await browsing.messages.configure({"enabled": True, "auto_poll": True})
    await browsing.set_enabled(True)
    browsing.bilibili_account.credentials = {"sessdata": "fixture", "csrf": "fixture"}
    monkeypatch.setattr(browsing.bilibili_account, "display_account_id", lambda: "100")

    async def thread(target):
        parent = target["parent"]
        return {"target": target, "source": {"comment_id": parent, "author_uid": "300", "text": f"原评 {parent}"},
                "items": []}

    monkeypatch.setattr(browsing.adapter, "read_video_thread", thread, raising=False)
    row = await finished(browsing, automatic=True, interaction_only=True)
    drafts = []
    for parent in map(str, range(601, 601 + count)):
        item = browsing.publication._intent(
            row, "reply_public", {"type": 1, "oid": "42", "bvid": BV, "root": parent, "parent": parent,
                                  "recipient": "300", "source_text": f"原评 {parent}"},
            ResponseDecision(action="reply_public", reason="specific_detail", text=f"小鱼读完第 {parent} 楼啦。"), [], [],
        )
        saved = browsing.review(item["id"], "approve")
        drafts.append({"channel": "browsing", "id": item["id"], "updated_at": saved["updated_at"], "expected_image": None})
    return drafts


async def test_approved_batch_uses_existing_ledger_and_resumes_fifo(browsing, monkeypatch):
    drafts = await approved_replies(browsing, 10, monkeypatch)
    selection = prepare_queue_selection(browsing.store, {"items": drafts})
    preview = await review_queue_selection(browsing, selection, "preview")
    assert [item["status"] for item in preview["items"]] == ["ready"] * 10
    assert not browsing.adapter.sent
    queued = await review_queue_selection(browsing, selection, "queue")
    assert [item["status"] for item in queued["items"]] == ["send_queued"] * 10
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["601", "602", "603"]
    assert browsing.store.one("SELECT COUNT(*) n FROM operation_send_queue WHERE wake_at>?", (time.time(),))["n"] == 7
    assert (await browsing.run(automatic=True, video_only=True))["status"] == "deferred"
    message = incoming(browsing.messages, account_id="100")
    browsing.messages._queue_newest_messages("100")
    assert browsing.store.one("SELECT source_id FROM dm_drafts") == {"source_id": message}

    await browsing.stop()
    await browsing.set_enabled(True)
    browsing.store.execute("UPDATE account_writes SET started_at=started_at-3601")
    browsing.store.execute("UPDATE operation_send_queue SET wake_at=0")
    assert browsing.backlog_status()["catching_up"]
    browsing.sends.changed.set()
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == [str(parent) for parent in range(601, 607)]
    for last in (610, 611):
        browsing.store.execute("UPDATE account_writes SET started_at=started_at-3601")
        browsing.store.execute("UPDATE operation_send_queue SET wake_at=0")
        browsing.sends.changed.set()
        await wait_for_sends(browsing)
        assert [target["parent"] for target, _ in browsing.adapter.sent] == [str(parent) for parent in range(601, last)]
    assert not browsing.store.one("SELECT 1 FROM operation_send_queue")


async def test_changed_draft_isolated_and_unknown_result_reconciles_without_resubmit(browsing, monkeypatch):
    drafts = await approved_replies(browsing, 2, monkeypatch)
    browsing.review(drafts[0]["id"], "edit", "这条改过了，等确认。")
    submissions, visible = [], asyncio.Event()

    async def submit(target, text, *, before_send):
        before_send()
        submissions.append((target["parent"], text))
        raise PublishUncertain("平台处理了，连接中断")

    async def reconcile(target, **options):
        return {"status": "visible", "receipt": {"rpid": "900", "code": 0}} if visible.is_set() else {"status": "not_found"}

    monkeypatch.setattr(browsing.adapter, "publish_resource_reply", submit)
    monkeypatch.setattr(browsing.adapter, "reconcile_publication", reconcile, raising=False)
    queued = await review_queue_selection(browsing, prepare_queue_selection(browsing.store, {"items": drafts}), "queue")
    assert [item["status"] for item in queued["items"]] == ["failed", "send_queued"]
    await wait_for_sends(browsing)
    assert browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (drafts[1]["id"],))["status"] == "publish_uncertain"
    visible.set()
    browsing.store.execute("UPDATE operation_send_queue SET wake_at=0")
    browsing.sends.changed.set()
    await wait_for_sends(browsing)
    assert submissions == [("602", "小鱼读完第 602 楼啦。")]
    assert browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (drafts[1]["id"],))["status"] == "published"


async def test_page_handoff_leaves_waiting_floors_and_resumes_same_read(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    browsing.store.set("operation_background_heads_per_minute", 0.4)
    row = await finished(browsing, automatic=True, interaction_only=True)
    rows = [comment(str(identity), str(identity)) for identity in range(601, 605)]
    with browsing.store.db:
        assert not admit_comment_page(browsing, row, None, {"bvid": BV, "aid": "42"}, rows)
    browsing.store.execute("UPDATE browsing_events SET error=?", (json.dumps({"next_at": time.time() + 600}),))
    status = browsing.backlog_status()
    assert status["total_heads"] == 2 and status["ready_heads"] == 0 and status["discovery_paused"]
    assert not resume_comment_page(browsing, row)
    browsing.store.execute("UPDATE browsing_events SET status='drafted'")
    assert resume_comment_page(browsing, row)
    assert browsing.store.one("SELECT COUNT(*) n FROM browsing_events")["n"] == 4
    assert not browsing.store.one("SELECT 1 FROM browsing_comment_page_handoffs")


async def test_dynamic_like_requires_readback_and_does_not_consume_comment_hour(browsing, monkeypatch):
    await approved_replies(browsing, 0, monkeypatch)
    await browsing.configure({"max_per_hour": 1, "dynamic_like_enabled": True, "dynamic_like_auto_publish": True})
    assert browsing.operations.accepting_work
    row = await finished(browsing, automatic=True, interaction_only=True)
    sent = []
    liked = False
    original = "这条已读取动态里的新发现。"

    async def receive(request):
        nonlocal liked
        assert request.method == "POST" and request.url.path == "/x/dynamic/feed/dyn/thumb"
        assert request.url.params["csrf"] == "fixture-token"
        assert json.loads(request.content) == {"dyn_id_str": "81000", "up": 1}
        sent.append(request.url.path)
        liked = True
        return httpx.Response(200, json={"code": 0})

    async def read(identifier):
        return {"dynamic_id": identifier, "author_uid": "300", "text": original, "liked": liked}

    adapter = BrowsingAdapter({"sessdata": "fixture", "csrf": "fixture-token"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(base_url="https://api.bilibili.com", transport=httpx.MockTransport(receive))
    try:
        monkeypatch.setattr(browsing.adapter, "like_dynamic", adapter.like_dynamic, raising=False)
        monkeypatch.setattr(browsing.adapter, "read_dynamic_like", read, raising=False)
        candidate = browsing.publication._intent(row, "dynamic_like", {"type": 17, "dynamic_id": "81000", "oid": "81000",
            "resource_type": "dynamic", "root": "0", "parent": "0", "source_text": original, "recipient": "300"},
            ResponseDecision(action="like_dynamic", reason="light_interaction", evidence_ids=["dynamic:81000"]), [], ["dynamic:81000"])
        browsing.queue_publication(candidate["id"])
        await wait_for_sends(browsing)
        assert sent == ["/x/dynamic/feed/dyn/thumb"] and liked
        assert browsing.store.one("SELECT channel,resolved_status FROM operation_write_attempts") == {
            "channel": "dynamic_likes", "resolved_status": "published",
        }
        assert browsing.comment_window_due() is None
    finally:
        await adapter.close()


def test_inventory_uses_interaction_age_and_preserves_source(browsing):
    now = time.time()
    data = json.dumps({"character_id": "whale", "bvid": BV, "root": "601"})
    for identity, ago, payload in (("recent", 2, data), ("merged", 3, data),
                                   ("parked", 4 * 86400, data.replace("601", "602")),
                                   ("archive", 8 * 86400, data.replace("601", "603"))):
        browsing.store.execute("INSERT INTO browsing_events VALUES(?,'100','comment_visit',?,?, 'received',?,NULL,?)",
                               (identity, identity, now - ago, payload, now))
    previews = inventory(browsing.store.db, now=now)["visits"]
    assert previews == {"retain": 1, "revisit_merged": 1, "revisit_parked": 1, "archived": 1}
    assert browsing.store.one("SELECT COUNT(*) n FROM browsing_events WHERE status='received'")["n"] == 4
    assert inventory(browsing.store.db, now=now, apply=True)["visits"] == previews
    assert inventory(browsing.store.db, now=now, apply=True)["visits"] == {"retain": 1}
    assert browsing.store.one("SELECT payload FROM browsing_events WHERE id='merged'")["payload"] == data
