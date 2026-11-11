"""One operator history across public replies, private messages and dynamics."""

import json

import pytest
from test_browsing_activity_history import BV, reply, round_row, stamp, task
from test_browsing_activity_history import service as service

from tuber_studio.operation_feedback import publication_binding
from tuber_studio.operations.browsing.activity_history import activity, history
from tuber_studio.operations_control import OperationsControl


@pytest.fixture
def channels(service):
    store = service.store
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(drafts)")}
    if "account_id" not in columns:
        store.execute("ALTER TABLE drafts ADD COLUMN account_id TEXT DEFAULT ''")
    store.db.executescript("""
        CREATE TABLE dm_inbox(id INTEGER PRIMARY KEY,account_id TEXT,character_id TEXT,display_name TEXT,platform_user_id TEXT,content TEXT,sent_at REAL,received_at REAL);
        CREATE TABLE dm_drafts(id INTEGER PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,display_name TEXT,platform_user_id TEXT,text TEXT,status TEXT,reason TEXT,error TEXT,receipt TEXT,updated_at REAL);
        CREATE TABLE dynamic_posts(id INTEGER PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,content TEXT,reason TEXT,status TEXT,error TEXT,receipt TEXT,published_at REAL,updated_at REAL);
    """)
    return service


def private_message(
    service,
    identity,
    at,
    *,
    account="100",
    character="whale",
    status="published",
    text="这段是仅供运营者查看的私信正文。",
):
    service.store.execute(
        "INSERT INTO dm_drafts VALUES(?,?,?,1,'鲸友','user-7',?,?,?,NULL,?,?)",
        (
            identity,
            account,
            character,
            text,
            status,
            "对方私信询问，我想继续聊。",
            json.dumps({"published_at": at}),
            at,
        ),
    )


def test_activity_preserves_incoming_reply_and_outreach_direction(channels):
    at = stamp("2026-11-07T10:00:00+08:00")
    private_message(channels, 1, at, status="pending_review")
    private_message(channels, 2, at + 1, status="not_initiated")
    for field in ("source_kind", "contact_type", "outreach_check"):
        channels.store.execute(f"ALTER TABLE dm_drafts ADD COLUMN {field} TEXT")
    channels.store.execute("UPDATE dm_drafts SET source_kind='message',contact_type='reply' WHERE id=1")
    channels.store.execute("UPDATE dm_drafts SET source_kind='comment',contact_type='outreach' WHERE id=2")
    result = activity(channels, day="2026-11-07", kind="messages")
    by_id = {item["id"]: item for item in result["items"]}
    reply_item, outreach = by_id["message/draft/1"], by_id["message/draft/2"]
    assert reply_item["contact_type"] == "reply" and reply_item["source_kind"] == "message"
    assert reply_item["title"].startswith("私信回复")
    assert outreach["contact_type"] == "outreach" and outreach["source_kind"] == "comment"
    assert outreach["outreach_status"] == "not_initiated" and outreach["title"].startswith("主动私信")
    assert result["counts"]["sent"] == 0


def dynamic(service, identity, at, *, account="100", character="whale", status="published"):
    service.store.execute(
        "INSERT INTO dynamic_posts VALUES(?,?,?,1,'今天海底小屋的白饭香气飘出窗了。','想分享海底小屋的小趣事。',?,NULL,?,?,?)",
        (
            identity,
            account,
            character,
            status,
            json.dumps({"published_at": at}),
            at if status == "published" else None,
            at,
        ),
    )


def manual_comment(service, identity, at, *, account="100", character="whale", status="published"):
    comment = service.store.execute(
        "INSERT INTO comments(video_id,comment_id,thread_id,display_name,content) VALUES(?,?,?,'评论观众','鱼鱼想试哪个盐水比例？')",
        (BV, f"source-{identity}", f"source-{identity}"),
    ).lastrowid
    service.store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,receipt,created_at,updated_at,account_id) VALUES(?,'本鱼打算先用一小杯试试。',?,?,1,?,?,?,?)",
        (comment, status, character, json.dumps({"published_at": at}), at, at, account),
    )


def test_all_channels_share_one_day_filter_and_pagination_without_cross_account_bodies(channels):
    at = stamp("2026-11-07T04:00:00+00:00")
    row = round_row(channels)
    watch = task(channels, row, "video", at)
    channels.repo.quotas.start_watch(watch, "100", "whale", BV, 0, now=at)
    channels.repo.save_content_progress(
        row,
        {"metadata": {"bvid": BV, "title": "盐水实验"}, "coverage": "all_available_cc", "missing_parts": []},
        [],
    )
    channels.repo.save_main_decision(
        row,
        BV,
        {
            "action": "reply_public",
            "video_preference": "not_interested",
            "preference_reason": "教程步骤重复，我更想看看评论里的实验结果。",
            "explanation": "想接一下观众关于比例的问题。",
        },
    )
    reply(channels, row, "auto-main", at, kind="top_level")
    manual_comment(channels, 1, at + 1)
    channels.store.execute(
        "INSERT INTO dm_inbox VALUES(1,'100','whale','鲸友','user-7','鱼鱼，私下聊一聊这个比例？',?,?)",
        (at + 2, at + 2),
    )
    private_message(channels, 1, at + 3)
    private_message(channels, 2, at + 4, status="pending_review", text="这条私信只是待审核草稿。")
    private_message(channels, 3, at + 5, account="900", text="OTHER-ACCOUNT-PRIVATE")
    private_message(channels, 4, at + 5, character="another-oc", text="OTHER-OC-PRIVATE")
    dynamic(channels, 1, at + 6)
    dynamic(channels, 2, at + 7, status="pending_review")
    dynamic(channels, 3, at + 8, account="900")
    changes = channels.store.db.total_changes
    result = activity(channels, day="2026-11-07", page_size=100)
    assert channels.store.db.total_changes == changes
    assert {item["kind"] for item in result["items"]} == {"video", "comment", "message", "dynamic"}
    assert result["counts"]["videos"] == 1
    assert result["counts"]["comments"] == 2
    assert result["counts"]["messages"] == 3
    assert result["counts"]["dynamics"] == 2
    assert result["counts"]["sent"] == 4
    assert result["counts"]["pending"] == 2
    assert result["total"] == 8
    assert "OTHER-ACCOUNT-PRIVATE" not in json.dumps(result)
    assert "OTHER-OC-PRIVATE" not in json.dumps(result)
    messages = activity(channels, day="2026-11-07", kind="messages", page_size=1)
    assert messages["total"] == 3 and messages["pages"] == 3
    assert messages["items"][0]["status"] == "pending_review"
    second = activity(channels, day="2026-11-07", kind="messages", page_size=1, page=2)
    assert second["items"][0]["status"] == "published"
    videos = activity(channels, day="2026-11-07", kind="videos")
    assert videos["items"][0]["video_preference"] == "not_interested"
    assert videos["items"][0]["preference_reason"] == "教程步骤重复，我更想看看评论里的实验结果。"
    assert videos["items"][0]["has_main_reply"]
    assert "仅供运营者查看" not in json.dumps(videos)
    comments = activity(channels, day="2026-11-07", kind="comments")
    assert {item["reply_kind"] for item in comments["items"]} == {"top_level", "reply"}
    assert history(channels, day="2026-11-07")["total"] == 1


def test_private_receive_send_days_cutoff_and_draft_states_are_real(channels):
    before = stamp("2026-11-07T15:59:00+00:00")
    after = stamp("2026-11-07T16:01:00+00:00")
    channels.store.execute(
        "INSERT INTO dm_inbox VALUES(1,'100','whale','鲸友','7','昨晚收到的私信',?,?)", (before, before)
    )
    private_message(channels, 1, after, status="published")
    private_message(channels, 2, after, status="queued", text="")
    private_message(channels, 3, after, account="", text="UNBOUND")
    first = activity(channels, day="2026-11-07", kind="messages")
    second = activity(channels, day="2026-11-08", kind="messages")
    assert first["items"][0]["direction"] == "incoming" and first["items"][0]["status"] == "received"
    assert first["items"][0]["href"] == "#messages/inbox/1"
    assert second["total"] == 1 and second["counts"]["sent"] == 1
    assert second["items"][0]["direction"] == "outgoing"
    assert {"2026-11-07", "2026-11-08"} <= set(second["days"])
    channels.store.set("browsing_history_visible_since", after + 1)
    assert activity(channels, day="2026-11-08")["total"] == 0
    assert channels.store.one("SELECT COUNT(*) AS n FROM dm_drafts")["n"] == 3
    assert activity(channels, day="2026-11-08", visible_since=0)["total"] == 1


def test_dynamic_legacy_binding_is_used_without_guessing_current_account(channels):
    at = stamp("2026-11-07T04:00:00+00:00")
    dynamic(channels, 1, at, account="")
    dynamic(channels, 2, at, account="")
    dynamic(channels, 3, at, account="900")
    control = OperationsControl(channels.store)
    character = channels.store.character("whale")
    binding = publication_binding(
        "dynamics", "100", character, "legacy-dynamic", {"post_id": 1}, {"kind": "public_operations"}
    )
    token = control.reserve("100", character, "legacy-dynamic", "dynamics", binding=binding)
    control.finish(token, "published")
    result = activity(channels, day="2026-11-07", kind="dynamics")
    assert [item["id"] for item in result["items"]] == ["dynamic/1"]
    assert result["counts"]["dynamics"] == 1


def test_unknown_preference_remains_unknown_instead_of_inferring_like_from_reply(channels):
    at = stamp("2026-11-07T04:00:00+00:00")
    row = round_row(channels)
    task(channels, row, "video", at)
    channels.repo.save_content_progress(
        row, {"metadata": {"bvid": BV}, "coverage": "all_available_cc", "missing_parts": []}, []
    )
    channels.repo.save_main_decision(row, BV, {"action": "reply_public", "explanation": "只是想吐槽一句。"})
    reply(channels, row, "main", at, kind="top_level")
    item = activity(channels, day="2026-11-07", kind="videos")["items"][0]
    assert item["interest"] is None
    assert item["video_preference"] is None


def test_prior_main_reply_remains_real_state_without_redisplaying_its_archived_body(channels):
    before = stamp("2026-11-07T15:59:00+00:00")
    after = stamp("2026-11-07T16:01:00+00:00")
    old = round_row(channels)
    reply(channels, old, "old-main", before, kind="top_level")
    channels.store.execute("UPDATE browsing_intents SET text='ARCHIVED-OLD-MAIN' WHERE id='old-main'")
    current = round_row(channels, version=2)
    task(channels, current, "visit", after, kind="comment_visit")
    reply(channels, current, "new-reply", after)
    result = activity(channels, day="2026-11-08", kind="videos", visible_since=after - 1)
    assert result["items"][0]["has_main_reply"]
    assert not result["items"][0]["has_main_reply_today"]
    assert result["items"][0]["main_replies"] == 0
    assert "ARCHIVED-OLD-MAIN" not in json.dumps(result)


def test_archived_retained_main_draft_is_not_embedded_in_a_new_comment_visit(channels):
    old = stamp("2026-11-07T04:00:00+00:00")
    fresh = stamp("2026-11-07T05:00:00+00:00")
    row = round_row(channels)
    channels.repo.save_content_progress(
        row, {"metadata": {"bvid": BV}, "coverage": "all_available_cc", "missing_parts": []}, []
    )
    decision = {
        "action": "reply_public",
        "text": "ARCHIVED-UNREVIEWED-BODY",
        "evidence_ids": ["original-source"],
        "explanation": "这段盐水实验引起了我的兴趣。",
        "video_preference": "like",
        "preference_reason": "喜欢看鸡蛋浮起来的过程。",
    }
    channels.repo.save_main_decision(row, BV, decision)
    channels.store.execute("UPDATE browsing_video_progress SET updated_at=?", (old,))
    task(channels, row, "new-visit", fresh, kind="comment_visit")
    result = activity(channels, day="2026-11-07", kind="videos", visible_since=old + 1)
    main = result["items"][0]["main_decision"]
    assert main["action"] == "reply_public"
    assert main["explanation"] == decision["explanation"]
    assert main["video_preference"] == "like"
    assert "ARCHIVED-UNREVIEWED-BODY" not in json.dumps(result)
    assert "original-source" not in json.dumps(result)
    assert channels.repo.content_progress(row, BV)["main_decision"] == decision


def test_invalid_activity_kind_is_rejected(channels):
    with pytest.raises(ValueError, match="活动类型"):
        activity(channels, kind="private-everything")
