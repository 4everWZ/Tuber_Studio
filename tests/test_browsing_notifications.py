"""Bounded, durable page scans do not release a conversation in reverse order."""

import asyncio
import json
import sqlite3

import pytest

from tuber_studio.bilibili_browsing import PlatformReadError
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.browsing_notifications import NotificationInbox


def event(identity, timestamp=1000, *, account="100", kind="mention", resource="video"):
    return {
        "event_id": identity,
        "timestamp": timestamp,
        "account_id": account,
        "kind": kind,
        "resource_type": resource,
        "sender_uid": "7",
        "comment_id": str(identity),
        "source_text": "真实消息",
    }


def page(items=(), cursor=None, *, account="100", has_more=None):
    return {
        "account_id": account,
        "items": list(items),
        "cursor": cursor,
        "has_more": cursor is not None if has_more is None else has_more,
    }


class Feed:
    def __init__(self):
        self.account = "100"
        self.calls = []
        self.pages = {}
        self.hook = None
        self.gate = None

    async def notifications(self, kind, cursor=None):
        self.calls.append((self.account, kind, cursor))
        if self.gate is not None:
            await self.gate.wait()
        if self.hook:
            self.hook()
        value = self.pages.get((kind, cursor), page(account=self.account))
        if isinstance(value, Exception):
            raise value
        return value


async def reader(method, *args):
    return await method(*args)


@pytest.fixture
def inbox(runtime, monkeypatch):
    monkeypatch.setattr("tuber_studio.browsing_notifications.time.time", lambda: 1000.9)
    return NotificationInbox(runtime.store, reader, Feed())


@pytest.fixture
def config():
    return BrowsingPolicy(mentions_enabled=True).model_dump()


async def sync(inbox, config, *, enabled=1000.6, started=1000.8):
    return await inbox.sync(inbox.adapter.account, config, enabled, started)


def statuses(inbox):
    return {
        json.loads(row["payload"])["event_id"]: row["status"]
        for row in inbox.store.all("SELECT payload,status FROM browsing_events")
    }


def cursor(inbox, kind="mention"):
    row = inbox.store.one(
        "SELECT * FROM browsing_cursors WHERE account_id=? AND kind=?", (inbox.adapter.account, kind)
    )
    return row, json.loads(row["cursor"])


async def test_initial_scan_only_establishes_history_and_seconds_cutoff(inbox, config):
    inbox.adapter.pages[("mention", None)] = page([event("old", 999), event("initial-same-second", 1000)])
    report = await sync(inbox, config)
    assert report["complete"] and report["released"] == 0
    assert statuses(inbox) == {"old": "historical", "initial-same-second": "historical"}
    row, state = cursor(inbox)
    assert row["initialized"] == 1 and row["watermark"] == 1000 and state["cutoff"] == 1000


async def test_partial_scan_buffers_across_polls_then_releases_oldest_first(inbox, config):
    await sync(inbox, config)
    config |= {"max_pages": 1}
    inbox.adapter.pages = {
        ("mention", None): page([event("newer", 1020)], "next"),
        ("mention", "next"): page([event("older", 1010)]),
    }
    first = await sync(inbox, config)
    assert not first["complete"] and first["buffered"] == 1
    assert statuses(inbox) == {"newer": "buffered"}
    row, state = cursor(inbox)
    assert row["watermark"] == 1000 and state["baseline"] == 1000 and state["latest"] == 1020
    before = len(inbox.adapter.calls)
    second = await sync(inbox, config)
    assert second["complete"] and second["released"] == 2 and second["buffered"] == 0
    assert inbox.adapter.calls[before:] == [("100", "mention", "next")]
    rows = inbox.store.all(
        "SELECT payload FROM browsing_events WHERE status='received' ORDER BY timestamp,id"
    )
    assert [json.loads(row["payload"])["event_id"] for row in rows] == ["older", "newer"]
    assert cursor(inbox)[0]["watermark"] == 1020


async def test_initial_partial_scan_stays_historical_until_all_pages_saved(inbox, config):
    config |= {"max_pages": 1}
    inbox.adapter.pages = {
        ("mention", None): page([event("old-head", 990)], "next"),
        ("mention", "next"): page([event("old-tail", 980)]),
    }
    first = await sync(inbox, config)
    assert not first["complete"] and not cursor(inbox)[0]["initialized"]
    second = await sync(inbox, config)
    assert second["complete"] and second["released"] == 0
    assert set(statuses(inbox).values()) == {"historical"}


async def test_overlap_same_second_new_ids_and_late_out_of_order_events_are_preserved(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("a", 1000), event("future", 1020)])
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("a", 1000), event("b", 1000), event("late", 1010)])
    report = await sync(inbox, config)
    assert report["released"] == 2 and report["feeds"]["mention"]["duplicates"] == 1
    assert set(statuses(inbox)) == {"a", "future", "b", "late"}
    assert all(status == "received" for status in statuses(inbox).values())
    assert cursor(inbox)[0]["watermark"] == 1020


async def test_read_failure_retains_previous_page_cursor_and_unreleased_buffer(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages = {
        ("mention", None): page([event("first", 1001)], "next"),
        ("mention", "next"): PlatformReadError("network", "网络失败"),
    }
    with pytest.raises(PlatformReadError):
        await sync(inbox, config)
    row, state = cursor(inbox)
    assert row["watermark"] == 1000 and state["page_cursor"] == "next"
    assert statuses(inbox) == {"first": "buffered"}
    inbox.adapter.pages[("mention", "next")] = page([event("second", 1002)])
    report = await sync(inbox, config)
    assert report["complete"] and report["released"] == 2


async def test_event_write_failure_rolls_back_whole_page_and_cursor(inbox, config):
    await sync(inbox, config)
    inbox.store.db.executescript("""
        CREATE TRIGGER reject_one BEFORE INSERT ON browsing_events
        WHEN json_extract(NEW.payload,'$.event_id')='broken'
        BEGIN SELECT RAISE(ABORT,'event persistence failure'); END;
    """)
    inbox.adapter.pages[("mention", None)] = page([event("saved-first", 1001), event("broken", 1002)], "next")
    with pytest.raises(sqlite3.IntegrityError):
        await sync(inbox, config)
    row, state = cursor(inbox)
    assert row["watermark"] == 1000 and state["page_cursor"] is None
    assert not statuses(inbox) and not inbox.store.all("SELECT * FROM browsing_notification_buffer")
    inbox.store.execute("DROP TRIGGER reject_one")
    inbox.adapter.pages[("mention", "next")] = page()
    assert (await sync(inbox, config))["released"] == 2


async def test_cursor_write_failure_rolls_back_new_events_together(inbox, config):
    await sync(inbox, config)

    def fail_after_read():
        inbox.store.db.executescript("""
            CREATE TRIGGER fail_cursor BEFORE UPDATE ON browsing_cursors
            BEGIN SELECT RAISE(ABORT,'cursor persistence failure'); END;
        """)
        inbox.adapter.hook = None

    inbox.adapter.hook = fail_after_read
    inbox.adapter.pages[("mention", None)] = page([event("unsaved", 1001)])
    with pytest.raises(sqlite3.IntegrityError):
        await sync(inbox, config)
    assert not statuses(inbox) and cursor(inbox)[0]["watermark"] == 1000
    assert cursor(inbox)[1]["page_cursor"] is None
    inbox.store.execute("DROP TRIGGER fail_cursor")
    assert (await sync(inbox, config))["released"] == 1


async def test_unknown_resource_is_visible_but_never_received(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page(
        [event("unknown", 1001, resource="opus"), event("normal", 1001)]
    )
    report = await sync(inbox, config)
    assert statuses(inbox) == {"unknown": "unsupported", "normal": "received"}
    assert report["feeds"]["mention"]["unsupported"] == 1
    assert inbox.store.one("SELECT error FROM browsing_events WHERE status='unsupported'")["error"]


async def test_restart_discards_incomplete_batch_and_does_not_resume_old_pagination(inbox, config):
    await sync(inbox, config)
    config |= {"max_pages": 1}
    inbox.adapter.pages[("mention", None)] = page([event("before-restart", 1001)], "old-cursor")
    await sync(inbox, config)
    other = NotificationInbox(inbox.store, reader, inbox.adapter)
    inbox.adapter.pages[("mention", None)] = page(
        [event("before-restart", 1001), event("after-restart", 1005)]
    )
    report = await sync(other, config, started=1005.7)
    assert report["complete"] and statuses(inbox) == {
        "before-restart": "historical",
        "after-restart": "received",
    }
    assert inbox.adapter.calls[-1][2] is None


async def test_authorization_change_does_not_release_old_batch(inbox, config):
    await sync(inbox, config)
    config |= {"max_pages": 1}
    inbox.adapter.pages[("mention", None)] = page([event("before-disable", 1001)], "old-cursor")
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("before-disable", 1001), event("new-enabled", 1010)])
    await sync(inbox, config, enabled=1010.9)
    assert statuses(inbox) == {"before-disable": "historical", "new-enabled": "received"}


async def test_stopping_discards_buffer_and_never_releases_old_ids(inbox, config):
    await sync(inbox, config)
    config |= {"max_pages": 1}
    inbox.adapter.pages[("mention", None)] = page([event("before-stop", 1001)], "old-cursor")
    await sync(inbox, config)
    inbox.discard_pending("100")
    assert statuses(inbox)["before-stop"] == "historical"
    inbox.adapter.pages[("mention", None)] = page([event("before-stop", 1001), event("new-after-stop", 1002)])
    await sync(inbox, config)
    assert statuses(inbox) == {"before-stop": "historical", "new-after-stop": "received"}


async def test_each_account_keeps_separate_ids_and_cursors(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("same-id", 1001)])
    await sync(inbox, config)
    inbox.adapter.account = "200"
    inbox.adapter.pages[("mention", None)] = page(account="200")
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("same-id", 1001, account="200")], account="200")
    await sync(inbox, config)
    rows = inbox.store.all("SELECT account_id,status FROM browsing_events ORDER BY account_id")
    assert rows == [{"account_id": "100", "status": "received"}, {"account_id": "200", "status": "received"}]
    assert len(inbox.store.all("SELECT * FROM browsing_cursors")) == 2


async def test_page_or_event_for_another_account_never_advances_cursor(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("wrong", 1001, account="200")])
    with pytest.raises(ValueError, match="账号"):
        await sync(inbox, config)
    assert not statuses(inbox) and cursor(inbox)[0]["watermark"] == 1000


async def test_two_feeds_share_one_page_budget_and_rotate_when_budget_is_one(inbox, config):
    config |= {"replies_enabled": True, "max_pages": 1}
    first = await sync(inbox, config)
    assert first["read_pages"] == 1 and len(inbox.adapter.calls) == 1
    second = await sync(inbox, config)
    assert second["read_pages"] == 1 and len(inbox.adapter.calls) == 2
    assert {call[1] for call in inbox.adapter.calls} == {"mention", "reply"}


async def test_nonadvancing_cursor_blocks_without_infinite_retries(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages = {
        ("mention", None): page([event("new", 1001)], "same"),
        ("mention", "same"): page([event("new", 1001)], "same"),
    }
    report = await sync(inbox, config)
    assert report["feeds"]["mention"]["blocked"] and statuses(inbox) == {"new": "buffered"}
    before = len(inbox.adapter.calls)
    next_report = await sync(inbox, config)
    assert len(inbox.adapter.calls) == before and not next_report["complete"]
    assert "游标未前进" in next_report["feeds"]["mention"]["diagnostic"]


async def test_multi_poll_scan_reports_long_incomplete_pagination(inbox, config):
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("1", 1001)], "1")
    for number in range(1, 7):
        inbox.adapter.pages[("mention", str(number))] = page([event(str(number + 1), 1001)], str(number + 1))
    for _ in range(3):
        report = await sync(inbox, config)
        assert report["read_pages"] <= config["max_pages"]
    assert "跨多轮" in report["feeds"]["mention"]["diagnostic"]
    assert set(statuses(inbox).values()) == {"buffered"}


async def test_cancellation_before_page_commit_does_not_advance_or_leave_rows(inbox, config):
    await sync(inbox, config)
    inbox.adapter.gate = asyncio.Event()
    inbox.adapter.pages[("mention", None)] = page([event("cancelled-read", 1001)])
    task = asyncio.create_task(sync(inbox, config))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not statuses(inbox) and cursor(inbox)[0]["watermark"] == 1000


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), 1000000000000, -1, True])
async def test_invalid_or_millisecond_timestamps_do_not_change_watermark(inbox, config, timestamp):
    await sync(inbox, config)
    inbox.adapter.pages[("mention", None)] = page([event("invalid", timestamp)])
    with pytest.raises(ValueError, match="时间"):
        await sync(inbox, config)
    assert not statuses(inbox) and cursor(inbox)[0]["watermark"] == 1000
