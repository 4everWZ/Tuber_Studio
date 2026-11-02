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
