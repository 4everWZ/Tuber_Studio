import json
import time

import pytest
from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.quotas import DailyReplyLimitReached
from tuber_studio.platforms.bilibili_browsing import PlatformReadError


@pytest.mark.parametrize("failure_kind", ["network", "daily_reply_limit"])
async def test_notification_retry_keeps_source_and_waits_until_eligible(browsing, monkeypatch, failure_kind):
    config = browsing.policy() | {"reply_enabled": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('source','100','reply','502',?,'received','{}',NULL,?)",
        (time.time(), time.time()),
    )
    calls = []
    wake_at = time.time() + 3600

    async def unavailable(*args):
        calls.append(1)
        if failure_kind == "daily_reply_limit":
            raise DailyReplyLimitReached({"next_at": wake_at, "day": "2026-11-07"})
        raise PlatformReadError("network", "网络暂时断开")

    monkeypatch.setattr(browsing, "_event", unavailable)
    metrics = {"failures": 0}
    await browsing._process_events(row, metrics)
    saved = browsing.store.one("SELECT * FROM browsing_events WHERE id='source'")
    assert saved["status"] == "received"
    details = json.loads(saved["error"])
    assert details["next_at"] > time.time() and details["occurrences"] == 1
    browsing.repo.recover()
    assert browsing.store.one("SELECT status FROM browsing_events WHERE id='source'")["status"] == "received"
    await browsing._process_events(row, metrics)
    assert len(calls) == 1
    assert metrics["failures"] == 0
    view = browsing.inbox()["items"][0]
    assert view["error"] == details["message"] and view["next_at"] == details["next_at"]


async def test_new_public_replies_insert_without_starving_an_older_reply(browsing, monkeypatch):
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    for identity, timestamp in (("old", 1), ("middle", 2), ("new", 3)):
        browsing.store.execute(
            "INSERT INTO browsing_events VALUES(?,'100','reply',?,?,'received','{}',NULL,?)",
            (identity, identity, timestamp, timestamp),
        )
    handled = []

    async def reply(event, *args):
        handled.append(event["id"])
        browsing.store.execute("UPDATE browsing_events SET status='drafted' WHERE id=?", (event["id"],))

    monkeypatch.setattr(browsing, "_event", reply)
    await browsing._process_events(row, {"failures": 0})
    assert handled == ["new", "old"]
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('newer','100','reply','newer',4,'received','{}',NULL,4)"
    )
    await browsing._process_events(row, {"failures": 0})
    assert handled == ["new", "old", "newer", "middle"]


async def test_notifications_keep_each_discussion_in_source_order_without_blocking_new_other_threads(browsing, monkeypatch):
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    for identity, timestamp, root in (("older-floor", 100, "501"), ("newer-floor", 200, "501"), ("new-other", 300, "601")):
        browsing.store.execute("INSERT INTO browsing_events VALUES(?,'100','reply',?,?,'received',?,NULL,?)", (
            identity, identity, timestamp,
            json.dumps({"resource_type": "video", "oid": "42", "root": root, "comment_id": identity}), timestamp,
        ))
    handled = []

    async def reply(event, *args):
        handled.append(event["id"])
        browsing.store.execute("UPDATE browsing_events SET status='drafted' WHERE id=?", (event["id"],))

    monkeypatch.setattr(browsing, "_event", reply)
    await browsing._process_events(row, {"failures": 0})
    assert handled == ["new-other", "older-floor"]
    await browsing._process_events(row, {"failures": 0})
    assert handled == ["new-other", "older-floor", "newer-floor"]


async def test_deferred_discussion_head_is_not_overtaken_by_a_newer_message_in_the_same_thread(browsing, monkeypatch):
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    for identity, timestamp, root, error in (
        ("wait", 100, "501", json.dumps({"next_at": time.time() + 60})),
        ("follow-up", 200, "501", None), ("other", 300, "601", None),
    ):
        browsing.store.execute("INSERT INTO browsing_events VALUES(?,'100','reply',?,?,'received',?,?,?)", (
            identity, identity, timestamp,
            json.dumps({"resource_type": "video", "oid": "42", "root": root}), error, timestamp,
        ))
    handled = []

    async def reply(event, *args):
        handled.append(event["id"])
        browsing.store.execute("UPDATE browsing_events SET status='drafted' WHERE id=?", (event["id"],))

    monkeypatch.setattr(browsing, "_event", reply)
    await browsing._process_events(row, {"failures": 0})
    assert handled == ["other"]
