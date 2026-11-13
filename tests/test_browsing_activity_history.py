"""Daily history keeps real reply outcomes and separate viewing starts."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from tuber_studio.operations.browsing.activity_history import activity, history
from tuber_studio.operations.browsing.comment_timeline import comment_thread, observe_thread
from tuber_studio.operations.browsing.models import BrowsingPolicy
from tuber_studio.operations.browsing.repository import BrowsingRepository

BV = "BV1d8pU6xEZX"


def stamp(value):
    return dt.datetime.fromisoformat(value).timestamp()


@pytest.fixture
def service(store):
    repo = BrowsingRepository(store)
    return SimpleNamespace(
        store=store,
        repo=repo,
        policy=lambda: {"business_timezone": "Asia/Taipei"},
        discovery=SimpleNamespace(history_scope=lambda: ("100", "whale", 99)),
    )


def round_row(service, *, account="100", version=1, batch=False):
    config = BrowsingPolicy().model_dump() | {"discovery_publish": True}
    if batch:
        config["autonomous_batch"] = True
    row, _ = service.repo.create_round(account, {"id": "whale", "version": version}, config, 0, False)
    service.store.execute("UPDATE browsing_rounds SET status='completed' WHERE id=?", (row["id"],))
    return service.repo.round(row["id"])


def task(
    service,
    row,
    identity,
    at,
    *,
    bvid=BV,
    kind="new_video",
    status="selected",
    explanation="杯里的鸡蛋真的浮起来了，想问问盐水比例。",
    error=None,
):
    payload = {"bvid": bvid, "title": "盐水实验", "task_kind": kind, "explanation": explanation}
    identity = service.repo.task(row["id"], payload)
    service.store.execute(
        "UPDATE browsing_tasks SET status=?,material_key=?,payload=?,error=?,created_at=?,updated_at=? WHERE id=?",
        (
            status,
            "kept-material" if status == "selected" else None,
            json.dumps(payload),
            error,
            at,
            at,
            identity,
        ),
    )
    return identity


def reply(service, row, identity, at, *, bvid=BV, kind="reply_public", status="published"):
    target = {"bvid": bvid, "recipient_name": "实验观众" if kind == "reply_public" else ""}
    service.store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,status,text,target,receipt,reason,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            row["account_id"],
            "whale",
            row["character_version"],
            identity,
            kind,
            row["id"],
            status,
            "鱼鱼也想试试这个比例。",
            json.dumps(target),
            json.dumps({"rpid": identity, "published_at": at}),
            "specific_detail",
            at,
            at,
        ),
    )


def test_comment_timeline_merges_discovery_and_distinguishes_round_outcomes(service, monkeypatch):
    previous, current = round_row(service), round_row(service)
    now = stamp("2026-11-08T10:05:00+08:00")
    monkeypatch.setattr("tuber_studio.operations.browsing.comment_timeline.time.time", lambda: now)
    items = [
        {"comment_id": "40", "thread_id": "40", "platform_user_id": "51", "content": "起因", "created_at": now - 50},
        {"comment_id": "41", "thread_id": "40", "parent_comment_id": "40", "platform_user_id": "52", "content": "追问", "created_at": now - 40},
        {"comment_id": "44", "thread_id": "40", "parent_comment_id": "43", "platform_user_id": "100", "content": "同账号回复", "created_at": now - 10},
    ]
    with service.store.db:
        for via in ("comment_visit", "own_comment", "reply"):
            observe_thread(service.store, current, BV, items, via=via, resource_author_uid="100")
        observe_thread(service.store, current | {"account_id": "900"}, BV,
                       items[:1] + [{"comment_id": "99", "thread_id": "40", "platform_user_id": "99", "content": "另一个账号观察"}],
                       via="reply", resource_author_uid="100")
    for key, row, parent, status in [
        ("101", previous, "40", "published"), ("102", current, "41", "published"),
        ("103", current, "43", "operator_replied"), ("104", current, "42", "publish_uncertain"),
    ]:
        reply(service, row, key, now - (20 if row == previous else 5), status=status)
        target = {"bvid": BV, "parent": parent, "root": "40", "source_text": "真实原话"}
        if status == "operator_replied":
            target["account_reply"] = {"comment_id": "44", "author_uid": "100", "text": "同账号回复", "created_at": now - 10}
        service.store.execute("UPDATE browsing_intents SET target=?,receipt=CASE WHEN status='published' THEN receipt END WHERE id=?",
                              (json.dumps(target), key))
    # An observation-only later round can report the earlier verified reply.
    revisit = round_row(service)
    with service.store.db:
        observe_thread(service.store, revisit, BV, items[:2], via="comment_visit", resource_author_uid="100")
    thread = comment_thread(service.store, BV, "40", account_id="100", character_id="whale")
    by_id = {entry["comment_id"]: entry for entry in thread["comments"]}
    assert set(by_id) == {"40", "41", "44", "101", "102"}
    assert set(by_id["41"]["discovered_via"]) == {"comment_visit", "own_comment", "reply"}
    assert by_id["44"]["publication"] == "same_account"
    assert thread["resource_author_uid"] == "100" and thread["root_author_uid"] == "51"
    data = activity(service, day="2026-11-08", kind="comments", round_id=current["id"])
    outcomes = {entry["comment_outcome"]: entry for entry in data["items"]}
    assert set(outcomes) == {"new_reply", "same_account", "pending_verification"}
    assert outcomes["new_reply"]["source_href"].endswith("#reply41")
    assert outcomes["new_reply"]["sent_href"].endswith("#reply102")
    assert outcomes["new_reply"]["own_resource"] and not outcomes["new_reply"]["own_thread"]
    assert outcomes["same_account"]["account_reply_text"] == "同账号回复"
    assert outcomes["pending_verification"]["sent_href"] is None
    old = activity(service, day="2026-11-08", kind="comments", round_id=revisit["id"])["items"][0]
    assert old["comment_outcome"] == "previous_reply"


def test_taipei_days_merge_versions_and_keep_comment_visits_out_of_watch_count(service):
    before = stamp("2026-11-07T15:58:00+00:00")
    after = stamp("2026-11-07T16:02:00+00:00")
    original = round_row(service)
    watched = task(service, original, "watch", before)
    service.repo.quotas.start_watch(watched, "100", "whale", BV, 0, now=before)
    service.repo.save_content_progress(
        original,
        {"metadata": {"bvid": BV, "title": "盐水实验"}, "coverage": "all_available_cc", "missing_parts": []},
        [],
    )
    service.repo.mark_watch_result(original, BV, fully_read=True)
    reply(service, original, "main-original", before, kind="top_level")
    revised = round_row(service, version=2)
    task(service, revised, "comments", after, kind="comment_visit")
    reply(service, revised, "comment-revised", after)
    other_account = round_row(service, account="900")
    reply(service, other_account, "other-account", after, bvid="BV1xx411c7mD")
    changes = service.store.db.total_changes
    first = history(service, day="2026-11-07")
    second = history(service, day="2026-11-08")
    assert service.store.db.total_changes == changes
    assert first["counts"]["watched"] == first["counts"]["fully_read"] == 1
    assert first["items"][0]["main_replies"] == 1
    assert second["counts"]["watched"] == 0
    assert second["counts"]["videos"] == second["counts"]["replies"] == 1
    assert second["items"][0]["bvid"] == BV
    assert second["items"][0]["comment_replies"] == 1
    assert second["items"][0]["replies"][0]["recipient_name"] == "实验观众"
    assert second["items"][0]["fully_read"]
    assert {"2026-11-07", "2026-11-08"} <= set(second["days"])


def test_watch_started_before_midnight_is_not_counted_again_when_finished_next_day(service):
    before = stamp("2026-11-07T15:58:00+00:00")
    after = stamp("2026-11-07T16:02:00+00:00")
    row = round_row(service)
    identity = task(service, row, "continued", after)
    service.repo.quotas.start_watch(identity, "100", "whale", BV, 0, now=before)
    assert history(service, day="2026-11-07")["counts"]["watched"] == 1
    assert history(service, day="2026-11-08")["counts"]["watched"] == 0


def test_skip_reasons_and_failures_are_paged_without_inventing_queue_judgments(service):
    at = stamp("2026-11-07T04:00:00+00:00")
    row = round_row(service, batch=True)
    service.repo.batches.merge(row, [{"bvid": BV, "title": "实验合集"}])
    service.repo.batches.save_plan(
        row,
        {
            "videos": [],
            "workers": 1,
            "choices": [
                {
                    "bvid": BV,
                    "action": "skip",
                    "explanation": "刚聊完盐水实验，这个合集暂时没想补充的。",
                    "decided_at": at,
                }
            ],
        },
    )
    service.store.execute(
        "UPDATE browsing_batches SET created_at=?,updated_at=? WHERE round_id=?", (at, at, row["id"])
    )
    service.store.execute(
        "UPDATE browsing_batch_items SET payload=json_set(payload,'$.selection.decided_at',?) WHERE batch_id=?",
        (at, row["config"]["batch_id"]),
    )
    task(service, row, "queued", at, bvid="BV1xx411c7mD", status="discovered", explanation="")
    task(service, row, "cancelled", at, bvid="BV17x411w7KC", status="cancelled", explanation="")
    task(
        service,
        row,
        "failed",
        at + 1,
        bvid="BV1Qx411J7mD",
        status="failed",
        explanation="",
        error="读取字幕时连接超时",
    )
    first = history(service, day="2026-11-07", page_size=1)
    second = history(service, day="2026-11-07", page_size=1, page=2)
    assert first["total"] == first["pages"] == 2
    assert first["counts"]["skipped"] == first["counts"]["failed"] == 1
    assert first["counts"]["watched"] == 0
    assert first["items"][0]["reason"] == ""
    assert first["items"][0]["error"] == "读取字幕时连接超时"
    assert second["items"][0]["reason"] == "刚聊完盐水实验，这个合集暂时没想补充的。"
    assert second["items"][0]["status"] == "skipped"


def test_comment_only_result_explains_what_was_read_without_counting_a_video_watch(service):
    at = stamp("2026-11-07T04:00:00+00:00")
    row = round_row(service)
    task(service, row, "comments", at, kind="comment_visit", status="comments_checked")
    reply(service, row, "reply-from-comments", at + 1)
    result = history(service, day="2026-11-07")
    assert result["items"][0]["read_state"] == "comments_only"
    assert not result["items"][0]["watched"]
    assert result["counts"]["watched"] == 0 and result["counts"]["replies"] == 1


def test_video_activity_shows_the_final_viewing_reason_instead_of_initial_title_interest(service):
    at = stamp("2026-11-07T04:00:00+00:00")
    row = round_row(service)
    identity = task(service, row, "watched", at, explanation="标题挺有意思，想看看实验")
    service.repo.quotas.start_watch(identity, "100", "whale", BV, 0, now=at)
    material = {"metadata": {"bvid": BV, "title": "盐水实验"}, "coverage": "all_available_cc"}
    service.repo.save_content_progress(row, material, [])
    reason = "盐水比例已经讲清楚了，这次看完没有想补充的话"
    service.repo.save_main_decision(row, BV, {"action": "finish", "explanation": reason})
    result = history(service, day="2026-11-07")
    assert result["items"][0]["reason"] == reason


def test_archiving_hides_old_events_without_removing_real_receipts(service):
    at = stamp("2026-11-07T04:00:00+00:00")
    row = round_row(service)
    reply(service, row, "kept-receipt", at, kind="top_level")
    service.store.execute("UPDATE browsing_intents SET updated_at=? WHERE id='kept-receipt'", (at + 100,))
    service.store.set("browsing_history_visible_since", at + 1)
    archived = history(service, day="2026-11-07")
    assert archived["total"] == archived["counts"]["replies"] == 0
    assert service.store.one("SELECT receipt FROM browsing_intents WHERE id='kept-receipt'")["receipt"]
    assert history(service, day="2026-11-07", visible_since=0)["counts"]["replies"] == 1


@pytest.mark.parametrize(
    "parameters",
    [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"day": "2026-02-30"}, {"day": "2026-1-2"}],
)
def test_invalid_history_query_is_rejected(service, parameters):
    with pytest.raises(ValueError):
        history(service, **parameters)
