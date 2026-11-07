import concurrent.futures
import datetime as dt

import pytest

from tuber_studio.operations.browsing.quotas import BrowsingQuotas, day_window
from tuber_studio.operations_control import OperationsControl, canonical_intent
from tuber_studio.store import Store

BV = "BV1d8pU6xEZX"
BEFORE_MIDNIGHT = dt.datetime(2026, 11, 7, 15, 59, 59, tzinfo=dt.UTC).timestamp()


def source(number="top-level", character="whale"):
    return canonical_intent("bilibili", BV, number, character)


def test_business_day_uses_the_configured_timezone_instead_of_host_midnight():
    before = day_window("Asia/Taipei", BEFORE_MIDNIGHT)
    after = day_window("Asia/Taipei", BEFORE_MIDNIGHT + 1)
    assert before["day"] == "2026-11-07"
    assert after["day"] == "2026-11-08" and before["next_at"] == after["start_at"]
    spring_forward = dt.datetime(2026, 3, 8, 12, tzinfo=dt.UTC).timestamp()
    new_york = day_window("America/New_York", spring_forward)
    assert new_york["next_at"] - new_york["start_at"] == 23 * 3600


def test_watching_starts_once_and_resuming_the_task_does_not_consume_the_next_day(store):
    quotas = BrowsingQuotas(store)
    assert quotas.start_watch("watch-1", "100", "whale", BV, 1, now=BEFORE_MIDNIGHT)
    assert quotas.start_watch("watch-1", "100", "whale", BV, 1, now=BEFORE_MIDNIGHT)
    assert not quotas.start_watch("watch-2", "100", "whale", BV, 1, now=BEFORE_MIDNIGHT)
    assert quotas.watch_status("100", "whale", 1, now=BEFORE_MIDNIGHT)["started"] == 1
    after = BEFORE_MIDNIGHT + 1
    assert quotas.start_watch("watch-1", "100", "whale", BV, 1, now=after)
    assert quotas.watch_status("100", "whale", 1, now=after)["started"] == 0
    assert quotas.start_watch("watch-2", "100", "whale", BV, 1, now=after)
    assert quotas.start_watch("watch-3", "other-account", "whale", BV, 1, now=after)
    assert quotas.start_watch("watch-4", "100", "other-oc", BV, 1, now=after)


def test_zero_daily_limits_keep_counting_without_blocking(store):
    quotas = BrowsingQuotas(store)
    for index in range(25):
        assert quotas.start_watch(str(index), "100", "whale", BV, 0, now=BEFORE_MIDNIGHT)
        quotas.reserve_reply(str(index), "100", "whale", BV, source(str(index)), 0, now=BEFORE_MIDNIGHT)
        quotas.settle_reply(str(index), "published")
    assert quotas.watch_status("100", "whale", 0, now=BEFORE_MIDNIGHT)["started"] == 25
    state = quotas.reply_status("100", "whale", BV, 0, now=BEFORE_MIDNIGHT)
    assert state["published"] == 25 and state["remaining"] is None


def test_changing_business_timezone_recomputes_the_window_from_durable_timestamps(store):
    quotas = BrowsingQuotas(store)
    started = dt.datetime(2026, 11, 7, 20, tzinfo=dt.UTC).timestamp()
    now = started + 5 * 3600
    quotas.start_watch("one", "100", "whale", BV, 1, "Asia/Taipei", now=started)
    quotas.reserve_reply("one", "100", "whale", BV, source(), 1, "Asia/Taipei", now=started)
    quotas.settle_reply("one", "published")
    assert quotas.watch_status("100", "whale", 1, "America/New_York", now=now)["started"] == 1
    assert quotas.reply_status("100", "whale", BV, 1, "America/New_York", now=now)["published"] == 1


def test_daily_video_replies_combine_main_and_thread_successes_but_not_drafts_or_rejections(store):
    quotas = BrowsingQuotas(store)
    quotas.reserve_reply("failed", "100", "whale", BV, source("failed"), 2, now=BEFORE_MIDNIGHT)
    quotas.settle_reply("failed", "rejected")
    quotas.reserve_reply("main", "100", "whale", BV, source(), 2, now=BEFORE_MIDNIGHT)
    assert quotas.reply_status("100", "whale", BV, 2, now=BEFORE_MIDNIGHT)["published"] == 0
    quotas.settle_reply("main", "published")
    quotas.reserve_reply("thread", "100", "whale", BV, source("501"), 2, now=BEFORE_MIDNIGHT)
    quotas.settle_reply("thread", "published")
    with pytest.raises(ValueError, match="今日回复上限"):
        quotas.reserve_reply("extra", "100", "whale", BV, source("502"), 2, now=BEFORE_MIDNIGHT)
    state = quotas.reply_status("100", "whale", BV, 2, now=BEFORE_MIDNIGHT)
    assert state["published"] == 2 and state["reserved"] == 0
    assert not quotas.video_reply_limit_reached("100", "whale", BV, 2, now=BEFORE_MIDNIGHT + 1)


def test_uncertain_submission_reserves_one_slot_until_original_receipt_is_resolved(store):
    quotas = BrowsingQuotas(store)
    quotas.reserve_reply("one-send", "100", "whale", BV, source(), 1, now=BEFORE_MIDNIGHT)
    quotas.settle_reply("one-send", "uncertain")
    pending = quotas.reply_status("100", "whale", BV, 1, now=BEFORE_MIDNIGHT)
    assert pending["published"] == 0 and pending["reserved"] == 1
    with pytest.raises(ValueError, match="待核对"):
        quotas.reserve_reply("new-send", "100", "whale", BV, source(), 1, now=BEFORE_MIDNIGHT + 1)
    quotas.settle_reply("one-send", "published")
    quotas.settle_reply("one-send", "published")
    state = quotas.reply_status("100", "whale", BV, 1, now=BEFORE_MIDNIGHT)
    assert state["published"] == 1 and state["reserved"] == 0


def test_last_daily_reply_slot_is_atomic_across_sqlite_connections(store, settings):
    first = BrowsingQuotas(store)
    other_store = Store(settings.runtime / "test.sqlite3", [store.character()])
    second = BrowsingQuotas(other_store)

    def reserve(pair):
        quotas, number = pair
        try:
            return quotas.reserve_reply(number, "100", "whale", BV, source(number), 1, now=BEFORE_MIDNIGHT)
        except ValueError:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            attempts = list(pool.map(reserve, [(first, "one"), (second, "two")]))
        assert len([token for token in attempts if token]) == 1
        assert first.reply_status("100", "whale", BV, 1, now=BEFORE_MIDNIGHT)["reserved"] == 1
    finally:
        other_store.close()


def test_publication_confirmed_after_midnight_uses_the_success_day_and_pending_carries_over(store):
    quotas = BrowsingQuotas(store)
    quotas.reserve_reply("last-night", "100", "whale", BV, source(), 1, now=BEFORE_MIDNIGHT)
    quotas.settle_reply("last-night", "uncertain", now=BEFORE_MIDNIGHT)
    after = BEFORE_MIDNIGHT + 2
    pending = quotas.reply_status("100", "whale", BV, 1, now=after)
    assert pending["reserved"] == 1 and pending["published"] == 0
    with pytest.raises(ValueError, match="今日回复上限"):
        quotas.reserve_reply("today", "100", "whale", BV, source("today"), 1, now=after)
    quotas.settle_reply("last-night", "published", now=after)
    quotas.settle_reply("last-night", "published", now=after + 86400)
    assert quotas.reply_status("100", "whale", BV, 1, now=after)["published"] == 1
    assert quotas.reply_status("100", "whale", BV, 1, now=BEFORE_MIDNIGHT)["published"] == 0


def test_receipt_publication_time_can_resolve_yesterdays_success_without_charging_today(store):
    quotas = BrowsingQuotas(store)
    quotas.reserve_reply("last-night", "100", "whale", BV, source(), 1, now=BEFORE_MIDNIGHT)
    quotas.settle_reply("last-night", "uncertain", now=BEFORE_MIDNIGHT)
    after = BEFORE_MIDNIGHT + 2
    quotas.settle_reply("last-night", "published", receipt={"ctime": BEFORE_MIDNIGHT}, now=after)
    assert quotas.reply_status("100", "whale", BV, 1, now=after)["published"] == 0
    assert quotas.reply_status("100", "whale", BV, 1, now=BEFORE_MIDNIGHT)["published"] == 1
    quotas.reserve_reply("today", "100", "whale", BV, source("today"), 1, now=after)


def test_daily_reply_reservation_and_operation_admission_roll_back_together(store):
    control = OperationsControl(store)
    character = store.character()

    def reject_after_reserving(token):
        control.browsing_quotas.reserve_reply(token, "100", character["id"], BV, source(), 1)
        raise ValueError("停止提交")

    with pytest.raises(ValueError, match="停止提交"):
        control.reserve("100", character, source(), "comments", admission_hook=reject_after_reserving)
    assert store.one("SELECT COUNT(*) AS n FROM account_writes")["n"] == 0
    assert store.one("SELECT COUNT(*) AS n FROM browsing_reply_quotas")["n"] == 0


def test_existing_successful_platform_writes_count_after_upgrade_and_reopening(store):
    control = OperationsControl(store)
    character = store.character()
    token = control.reserve("100", character, source(), "comments")
    control.finish(token, "published")
    quotas = BrowsingQuotas(store)
    assert quotas.reply_status("100", character["id"], BV, 1)["published"] == 1
    assert BrowsingQuotas(store).reply_status("100", character["id"], BV, 1)["published"] == 1
