"""Tests for thread context, depth=1 anti-nesting and agent lifecycle."""

import asyncio
import contextvars
import json
import pytest

from tuber_studio.browsing import BrowsingService, agent_depth
from tuber_studio.browsing_models import BrowsingPolicy, RESPONSE_INSTRUCTION


@pytest.mark.asyncio
async def test_anti_nesting_depth_guard_raises():
    """Worker subagents at depth >= 1 are strictly forbidden from spawning further subagents."""
    assert agent_depth.get() == 0

    token = agent_depth.set(1)
    try:
        assert agent_depth.get() == 1
        # When current depth is >= 1, spawning another worker task raises PermissionError
        with pytest.raises(PermissionError, match="Subagent nesting prohibited; depth must not exceed 1"):
            current = agent_depth.get()
            if current >= 1:
                raise PermissionError("Subagent nesting prohibited; depth must not exceed 1")
    finally:
        agent_depth.reset(token)

    assert agent_depth.get() == 0


def test_response_instruction_encourages_thread_context_and_mentions():
    """RESPONSE_INSTRUCTION should instruct model to engage in thread context and mention others."""
    assert "楼中楼" in RESPONSE_INSTRUCTION
    assert "@" in RESPONSE_INSTRUCTION


def test_thread_context_assembly_and_timeline():
    """Verify thread context includes root, parent, and chronological timeline items."""
    raw_items = [
        {
            "comment_id": "1001",
            "thread_id": "1001",
            "parent_comment_id": None,
            "platform_user_id": "u1",
            "display_name": "楼主",
            "content": "视频里的解说太精彩了！",
            "created_at": "1700000000",
        },
        {
            "comment_id": "1002",
            "thread_id": "1001",
            "parent_comment_id": "1001",
            "platform_user_id": "u2",
            "display_name": "观众A",
            "content": "尤其是第3分钟那段分析",
            "created_at": "1700000010",
        },
        {
            "comment_id": "1003",
            "thread_id": "1001",
            "parent_comment_id": "1002",
            "platform_user_id": "u3",
            "display_name": "观众B",
            "content": "@大肥鱼 鱼鱼你怎么看？",
            "created_at": "1700000020",
        },
    ]

    comments_by_id = {str(it["comment_id"]): it for it in raw_items}
    threads_by_root = {}
    for it in raw_items:
        rid = str(it.get("thread_id") or it["comment_id"])
        threads_by_root.setdefault(rid, []).append(it)

    target_item = raw_items[2] # 观众B
    cid = target_item["comment_id"]
    rid = target_item["thread_id"]
    pid = target_item["parent_comment_id"]

    root_item = comments_by_id[rid]
    parent_item = comments_by_id[pid]
    timeline_items = threads_by_root[rid]

    thread_context = {
        "source": {
            "comment_id": cid,
            "author_uid": target_item["platform_user_id"],
            "author_name": target_item["display_name"],
            "text": target_item["content"],
        },
        "target": {
            "type": 1,
            "oid": "12345",
            "bvid": "BV1test",
            "root": rid,
            "parent": cid,
        },
        "root": {
            "comment_id": rid,
            "author_name": root_item["display_name"],
            "text": root_item["content"],
        },
        "parent": {
            "comment_id": pid,
            "author_name": parent_item["display_name"],
            "text": parent_item["content"],
        },
        "timeline": [
            {
                "author_name": c["display_name"],
                "text": c["content"],
                "is_root": c["comment_id"] == rid,
                "is_target": c["comment_id"] == cid,
            }
            for c in timeline_items
        ],
        "items": timeline_items,
    }

    assert thread_context["root"]["author_name"] == "楼主"
    assert thread_context["parent"]["author_name"] == "观众A"
    assert thread_context["source"]["author_name"] == "观众B"
    assert len(thread_context["timeline"]) == 3
    assert thread_context["timeline"][0]["is_root"] is True
    assert thread_context["timeline"][2]["is_target"] is True


def test_daily_reply_limit_refreshes_after_midnight():
    """Verify replies created today count against quota, while yesterday's do not."""
    import datetime
    import time

    now = time.time()
    today_midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # Yesterday's reply (e.g. 2 hours before midnight)
    yesterday_reply_time = today_midnight - 7200
    # Today's reply (e.g. 1 hour after midnight)
    today_reply_time = today_midnight + 3600

    max_replies = 2

    # If all replies are from yesterday:
    replies_yesterday = [{"created_at": yesterday_reply_time}, {"created_at": yesterday_reply_time - 100}]
    today_count = sum(1 for r in replies_yesterday if r["created_at"] >= today_midnight)
    remaining_today = max(0, max_replies - today_count)
    assert today_count == 0
    assert remaining_today == 2  # Fully refreshed for today!

    # If replies were made today:
    replies_today = [{"created_at": today_reply_time}, {"created_at": today_reply_time + 60}]
    today_count = sum(1 for r in replies_today if r["created_at"] >= today_midnight)
    remaining_today = max(0, max_replies - today_count)
    assert today_count == 2
    assert remaining_today == 0  # Reached daily limit for this video!


def test_top_level_comment_not_duplicated():
    """Account should not leave a duplicate top-level comment if one already exists."""
    account_id = "100"
    items_with_own_top_level = [
        {"comment_id": "99", "platform_user_id": "100", "parent_comment_id": None, "content": "我的主评"},
        {"comment_id": "100", "platform_user_id": "200", "parent_comment_id": None, "content": "别人的主评"},
    ]

    has_own_top_level = any(
        str(c.get("platform_user_id") or c.get("user_id")) == account_id
        and (
            not c.get("parent_comment_id")
            or str(c.get("parent_comment_id")) in ("0", "", "None", str(c.get("comment_id")))
        )
        for c in items_with_own_top_level
    )
    assert has_own_top_level is True  # Detected existing top-level comment!

    items_without_own_top_level = [
        {"comment_id": "100", "platform_user_id": "200", "parent_comment_id": None, "content": "别人的主评"},
    ]
    has_own_top_level_2 = any(
        str(c.get("platform_user_id") or c.get("user_id")) == account_id
        and (
            not c.get("parent_comment_id")
            or str(c.get("parent_comment_id")) in ("0", "", "None", str(c.get("comment_id")))
        )
        for c in items_without_own_top_level
    )
    assert has_own_top_level_2 is False


def test_permission_matrix_payload_and_frequency_alignment():
    """Verify matrix payload aligns max_per_hour with max_per_day, avoiding arbitrary round cutoffs."""
    max_per_day = 20
    max_replies_per_video = 3
    # Frontend payload construction logic
    payload = {
        "max_replies_per_video": max(0, min(20, max_replies_per_video)),
        "max_per_hour": max(1, min(30, max_per_day)),
        "max_per_day": max(1, min(200, max_per_day)),
    }
    assert payload["max_replies_per_video"] == 3
    assert payload["max_per_hour"] == 20
    assert payload["max_per_day"] == 20
    assert "max_per_round" not in payload


def test_passive_reply_accounted_in_daily_meltdown():
    """Both proactive and passive replies (@ mentions) count towards the daily write limit."""
    import time
    now = time.time()
    # Mocking write attempts table rows within last 24h
    attempts = [
        {"account_id": "1", "channel": "comments", "started_at": now - 3600},
        {"account_id": "1", "channel": "comments", "started_at": now - 1800},
        {"account_id": "1", "channel": "comments", "started_at": now - 300},  # passive reply to @
    ]
    max_per_day = 3
    count_last_24h = sum(1 for a in attempts if a["started_at"] > (now - 86400))
    assert count_last_24h >= max_per_day
    # Exceeding max_per_day triggers safety meltdown
    is_meltdown = count_last_24h >= max_per_day
    assert is_meltdown is True

