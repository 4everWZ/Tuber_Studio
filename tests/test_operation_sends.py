import asyncio
import time

from test_browsing import BV, finished, wait_for_sends
from test_browsing import browsing as browsing


async def test_ready_direct_reply_gets_last_shared_send_slot(browsing):
    await browsing.configure({"max_per_hour": 1, "max_per_day": 1, "recipient_cooldown_seconds": 0,
                              "top_level_auto_publish": True, "reply_enabled": True, "reply_auto_publish": True})
    await browsing.set_enabled(True)
    await browsing.sends.close()

    # Freeze the sender until both intents are ready, then run the real quota ledger.
    start = browsing.sends.start
    browsing.sends.start = lambda: None
    await finished(browsing, automatic=True, video_only=True)
    top = next(item for item in browsing.intents() if item["kind"] == "top_level")
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('new','100','reply','502',?,'received',?,NULL,?)",
        (time.time(), '{"event_id":"new","kind":"reply","resource_type":"video","oid":"42","root":"501",'
                      '"comment_id":"502","sender_uid":"300"}', time.time()),
    )
    await finished(browsing, automatic=True, interaction_only=True)
    browsing.sends.start = start
    start()
    await wait_for_sends(browsing)
    assert len(browsing.adapter.sent) == 1 and browsing.adapter.sent[0][0]["parent"] == "502"
    deferred, = browsing.store.all("SELECT * FROM operation_send_queue")
    assert deferred["source_id"] == top["id"] and deferred["wake_at"] > time.time()
    assert len(browsing.store.all("SELECT * FROM account_writes")) == 1

    await browsing.stop()
    await browsing.set_enabled(True)
    browsing.store.execute("UPDATE account_writes SET started_at=started_at-90000")
    browsing.store.execute("UPDATE operation_send_queue SET wake_at=?", (time.time(),))
    browsing.sends.changed.set()
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["502", "0"]


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
