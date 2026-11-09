import time
from unittest.mock import MagicMock

from tuber_studio.operations.browsing.comment_reader import (
    CommentProgress,
    merge_continuous_comments,
)
from tuber_studio.operations.browsing.models import ResponseDecision
from tuber_studio.store import Store


def test_merge_continuous_comments_basic():
    # 1. Single comment returns as-is
    items = [
        {"comment_id": "101", "thread_id": "101", "user_id": "user_a", "content": "第一句", "ctime": 1000}
    ]
    res = merge_continuous_comments(items)
    assert len(res) == 1
    assert res[0]["comment_id"] == "101"
    assert res[0]["content"] == "第一句"

    # 2. Consecutive comments from same user in same thread within 10 minutes (600s)
    items = [
        {"comment_id": "101", "thread_id": "101", "user_id": "user_a", "content": "大肥鱼今天开播吗", "ctime": 1000},
        {"comment_id": "102", "thread_id": "101", "user_id": "user_a", "content": "我等你好久了！", "ctime": 1050},
    ]
    merged = merge_continuous_comments(items)
    assert len(merged) == 1
    assert merged[0]["comment_id"] == "102"
    assert "大肥鱼今天开播吗\n我等你好久了！" in merged[0]["content"]
    assert merged[0]["merged_comment_ids"] == ["101", "102"]
    assert len(merged[0]["merged_sources"]) == 2
    assert merged[0]["merged_sources"][0]["comment_id"] == "101"
    assert merged[0]["merged_sources"][1]["comment_id"] == "102"

    # 3. Consecutive comments from different users are not merged
    items = [
        {"comment_id": "101", "thread_id": "101", "user_id": "user_a", "content": "你好", "ctime": 1000},
        {"comment_id": "102", "thread_id": "101", "user_id": "user_b", "content": "他也好", "ctime": 1010},
    ]
    assert len(merge_continuous_comments(items)) == 2

    # 4. Comments from same user exceeding time window (e.g. 700s > 600s) are not merged
    items = [
        {"comment_id": "101", "thread_id": "101", "user_id": "user_a", "content": "早安", "ctime": 1000},
        {"comment_id": "102", "thread_id": "101", "user_id": "user_a", "content": "晚安", "ctime": 1800},
    ]
    assert len(merge_continuous_comments(items)) == 2

    # 5. Consecutive comments in different threads are not merged
    items = [
        {"comment_id": "101", "thread_id": "101", "user_id": "user_a", "content": "主楼", "ctime": 1000},
        {"comment_id": "201", "thread_id": "201", "user_id": "user_a", "content": "另一个主楼", "ctime": 1010},
    ]
    assert len(merge_continuous_comments(items)) == 2


def test_merge_continuous_comments_multi_chain():
    # 3 consecutive comments from user_a, followed by user_b, followed by 2 comments from user_a
    items = [
        {"comment_id": "1", "thread_id": "root", "user_id": "user_a", "content": "A1", "ctime": 100},
        {"comment_id": "2", "thread_id": "root", "user_id": "user_a", "content": "A2", "ctime": 120},
        {"comment_id": "3", "thread_id": "root", "user_id": "user_a", "content": "A3", "ctime": 140},
        {"comment_id": "4", "thread_id": "root", "user_id": "user_b", "content": "B1", "ctime": 150},
        {"comment_id": "5", "thread_id": "root", "user_id": "user_a", "content": "A4", "ctime": 160},
        {"comment_id": "6", "thread_id": "root", "user_id": "user_a", "content": "A5", "ctime": 170},
    ]
    res = merge_continuous_comments(items)
    assert len(res) == 3
    assert res[0]["content"] == "A1\nA2\nA3"
    assert res[0]["comment_id"] == "3"
    assert res[0]["merged_comment_ids"] == ["1", "2", "3"]
    assert res[1]["content"] == "B1"
    assert res[1]["comment_id"] == "4"
    assert res[2]["content"] == "A4\nA5"
    assert res[2]["comment_id"] == "6"


def test_comment_progress_with_merged_sources(store):
    progress = CommentProgress(store)
    row = {"account_id": "acc_1", "character_id": "dafeiyu"}
    bvid = "BV1test"

    items = [
        {"comment_id": "101", "thread_id": "101", "user_id": "user_a", "content": "第1句", "ctime": 100},
        {"comment_id": "102", "thread_id": "101", "user_id": "user_a", "content": "第2句", "ctime": 120},
    ]
    merged = merge_continuous_comments(items)[0]

    # Simulate saving decision for merged item and all its merged sources
    decision = ResponseDecision(
        action="reply_public",
        text="收到两条连发留言啦！",
        reason="specific_detail",
        evidence_ids=[],
    )

    # Save for source and each merged source
    for ms in merged["merged_sources"]:
        progress.save(row, bvid, ms, decision)

    # Verify both 101 and 102 are recognized as responded
    assert progress.get(row, bvid, merged["merged_sources"][0]) is not None
    assert progress.get(row, bvid, merged["merged_sources"][1]) is not None
    assert progress.get(row, bvid, merged["merged_sources"][0]).text == "收到两条连发留言啦！"
    assert progress.get(row, bvid, merged["merged_sources"][1]).text == "收到两条连发留言啦！"
