"""Actual public source fixtures preserve participant IDs before model inference."""

import copy
import json
from pathlib import Path

import httpx
import pytest
from test_bilibili_browsing import adapter_factory as adapter_factory
from test_browsing import browsing as browsing

from tuber_studio.operation_continuations import content_hash
from tuber_studio.operations.browsing.comment_reader import event_unlocked
from tuber_studio.operations.browsing.decisions import BrowsingDecisions
from tuber_studio.operations.browsing.models import ResponseDecision
from tuber_studio.platforms.bilibili import BilibiliAdapter

CASES = json.loads((Path(__file__).parent / "fixtures/browsing_reply_participants_20261107.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
async def test_real_platform_threads_keep_mentions_and_other_speakers_without_changing_source_hash(adapter_factory, case):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/x/v2/reply/reply"
        assert request.url.params["root"] == case["target"]["root"]
        return httpx.Response(200, json={"code": 0, "data": case["platform_response"]})

    adapter = await adapter_factory(handle)
    thread = await adapter.read_video_thread(case["target"])
    source = next(item for item in thread["items"] if item["comment_id"] == case["target"]["parent"])
    context = BrowsingDecisions._video_comment_context(source, thread["items"], case["target"])
    assert context["source"] == case["expected_source"]
    assert content_hash(context["source"]) == case["source_hash"]
    assert context["addressing"]["source_mentions"] == case["expected_mentions"]
    assert all(mention["uid"] != case["actor"]["uid"] for mention in context["addressing"]["source_mentions"])
    assert context["root"]["author_uid"] == str(case["platform_response"]["root"]["member"]["mid"])
    assert context["root"]["comment_id"] == case["target"]["root"]
    if source["comment_id"] != source["thread_id"]:
        assert context["parent"]["comment_id"] == source["parent_comment_id"]
        assert context["parent"]["author_uid"] == context["root"]["author_uid"]
        assert context["parent"]["author_uid"] != case["actor"]["uid"]
    else:
        assert context["parent"] is None
    assert all(row["comment_id"] and row["author_uid"] and row["root_id"] for row in context["timeline"])
    assert len(requests) == 1


def test_normalizer_retains_only_actual_mention_uid_and_name():
    raw = copy.deepcopy(CASES[0]["platform_response"]["root"])
    raw["content"]["members"] *= 2
    assert BilibiliAdapter._comment(raw)["mentions"] == CASES[0]["expected_mentions"]
    raw["content"]["members"] = {"mid": "123"}
    with pytest.raises(ValueError, match="提及列表"):
        BilibiliAdapter._comment(raw)


def test_missing_root_never_attributes_the_target_author_to_the_root():
    item = {"comment_id": "502", "thread_id": "501", "parent_comment_id": "501",
            "platform_user_id": "300", "display_name": "另一条鱼", "content": "回给上面那位"}
    context = BrowsingDecisions._video_comment_context(item, [item], {"root": "501", "parent": "502"})
    assert context["root"]["comment_id"] == context["parent"]["comment_id"] == "501"
    assert context["root"]["author_uid"] is None and context["root"]["available"] is False
    assert context["timeline"][0]["parent_comment_id"] == "501"


def test_unknown_reply_parent_is_not_guessed_from_the_root():
    item = {"comment_id": "502", "thread_id": "501", "platform_user_id": "300", "content": "继续聊"}
    context = BrowsingDecisions._video_comment_context(item, [item], {"root": "501", "parent": "502"})
    assert context["parent"] is None
    assert context["addressing"]["source_root_comment_id"] == "501"
    assert context["addressing"]["source_parent_comment_id"] is None


async def test_notification_context_parent_is_original_source_parent_not_source_itself(browsing, monkeypatch):
    config = browsing.policy() | {"reply_enabled": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    source = {"comment_id": "502", "author_uid": "300", "text": "接上你上一句", "display_name": "邻居"}
    original = dict(source)
    thread = {"account_id": "100", "resource_type": "video", "target": {
        "type": 1, "oid": "42", "bvid": "BV1d8pU6xEZX", "root": "501", "parent": "502",
    }, "source": source, "items": [
        {"comment_id": "501", "thread_id": "501", "parent_comment_id": None,
         "platform_user_id": "100", "display_name": "同名角色", "content": "我方的根评论", "mentions": []},
        {"comment_id": "502", "thread_id": "501", "parent_comment_id": "501",
         "platform_user_id": "300", "display_name": "邻居", "content": source["text"],
         "mentions": [{"uid": "100", "name": "同名角色"}]},
    ]}

    async def capture(_row, material, context, allowed, kind, metrics):
        assert context["parent"]["comment_id"] == "501"
        assert context["parent"]["author_uid"] == "100"
        assert context["root"]["author_uid"] == "100"
        assert context["addressing"]["source_mentions"] == [{"uid": "100", "name": "同名角色"}]
        assert context["source"] == original and content_hash(context["source"]) == content_hash(original)
        assert context["target"]["parent"] == "502"
        return ResponseDecision(action="wait", reason="cooldown", explanation="稍后接话")

    monkeypatch.setattr(browsing.decisions, "_response", capture)
    await event_unlocked(browsing, {"id": "event", "kind": "own_comment"}, row, {}, {}, thread)
