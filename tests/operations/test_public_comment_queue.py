import json
import time

import pytest

from tests.support.operations_helpers import (
    BV,
    enable_comment_choices,
    finish_comment_round,
    finished,
    model_packet,
    wait_for_sends,
)
from tests.support.operations_helpers import browsing as browsing
from tuber_studio.infra.platforms.bilibili.client import PlatformReadError, PublishUncertain
from tuber_studio.operations.discovery.comments import admit_comment_page


def item(rpid, uid="300", parent=None):
    return {"comment_id": rpid, "thread_id": "601", "parent_comment_id": parent,
            "parent_verified": True, "platform_user_id": uid, "display_name": "邻居",
            "content": "盐水里的鸡蛋怎么浮起来？", "created_at": int(time.time())}


@pytest.mark.parametrize("operator_replied", [False, True])
async def test_duplicate_notification_and_visit_share_context(browsing, monkeypatch, operator_replied):
    _, calls = await enable_comment_choices(browsing, monkeypatch)
    target = {"oid": "42", "type": 1, "resource_type": "video", "bvid": BV, "root": "601", "parent": "601"}
    source = item("601")
    matched = item("602", uid="100", parent="601")
    items = [source, matched] if operator_replied else [source]

    async def fetch(*args, **kwargs):
        return {"items": items}

    async def thread(event):
        return {"account_id": "100", "resource_type": "video", "target": target,
                "source": {"comment_id": "601", "author_uid": "300", "text": source["content"]}, "items": items}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_public_thread", thread)
    event = target | {"kind": "reply", "event_id": "incoming", "account_id": "100", "sender_uid": "300",
                      "comment_id": "601", "source_text": source["content"]}
    browsing.store.execute("INSERT INTO browsing_events VALUES('incoming','100','reply','601',?,'received',?,NULL,?)",
                           (time.time() - 1, json.dumps(event), time.time()))
    await finish_comment_round(browsing)
    expected_calls = [("CandidateBatchPlan", "background")]
    if not operator_replied:
        expected_calls.append(("ResponseDecision", "direct"))
    assert calls == expected_calls
    rows = browsing.store.all("SELECT kind,status,payload FROM browsing_events ORDER BY kind")
    status = "operator_replied" if operator_replied else "drafted"
    assert [(row["kind"], row["status"]) for row in rows] == [("comment_visit", "skipped"), ("reply", status)]
    if operator_replied:
        assert json.loads(rows[1]["payload"])["account_reply"]["comment_id"] == "602"
        assert not browsing.intents()
    else:
        intent, = browsing.intents()
        assert intent["target"]["parent"] == "601"
    assert not browsing.adapter.sent


async def test_operator_reply_is_rechecked_when_publishing_a_prepared_visit_draft(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    source = item("601")
    items = [source, item("602", uid="100", parent="999")]

    async def fetch(*args, **kwargs):
        return {"items": items}

    async def thread(target):
        return {"target": target, "source": {"comment_id": "601", "author_uid": "300", "text": source["content"]},
                "items": items}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_video_thread", thread, raising=False)
    await finish_comment_round(browsing)
    intent, = browsing.intents()
    browsing.review(intent["id"], "approve")
    items.append(item("603", uid="100", parent="601"))
    result = await browsing.publish(intent["id"])
    assert result["status"] == "operator_replied" and result["target"]["account_reply"]["comment_id"] == "603"
    assert not browsing.adapter.sent and not browsing.store.all("SELECT * FROM account_writes")


@pytest.mark.parametrize("restricted", [False, True])
async def test_own_dynamic_uses_comment_oid_and_regular_inbox(browsing, monkeypatch, restricted):
    await enable_comment_choices(browsing, monkeypatch, automatic=True)
    own = {"resource_type": "dynamic", "dynamic_id": "900", "oid": "700", "type": 11}
    source = item("601")
    seen = []

    async def dynamics(account):
        assert account == "100"
        if restricted:
            raise PlatformReadError.restricted(412)
        return [own]

    async def dynamic_target(account, identity):
        assert restricted and account == "100"
        seen.append(identity)
        if identity == "901":
            raise PlatformReadError("not_found", "动态已删除")
        assert identity == "900"
        return own

    async def fetch(target):
        assert target == own
        return [source]

    async def thread(event):
        assert (event["dynamic_id"], event["oid"]) == ("900", "700")
        return {"account_id": "100", "resource_type": "dynamic", "public_text": "盐水实验的更新",
                "target": own | {"root": "601", "parent": "601"}, "items": [source],
                "source": {"comment_id": "601", "author_uid": "300", "text": source["content"]}}

    monkeypatch.setattr(browsing.adapter, "own_dynamics", dynamics, raising=False)
    monkeypatch.setattr(browsing.adapter, "own_dynamic_target", dynamic_target, raising=False)
    monkeypatch.setattr(browsing.adapter, "fetch_dynamic_comments", fetch, raising=False)
    monkeypatch.setattr(browsing.adapter, "read_public_thread", thread)
    if restricted:
        for owner, identity, at in [("100", "901", 3), ("100", "900", 2), ("200", "999", 1)]:
            browsing.store.execute(
                "INSERT INTO dynamic_posts(account_id,status,receipt,published_at) VALUES(?,'published',?,?)",
                (owner, json.dumps({"dynamic_id": identity}), at),
            )
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    intent, = browsing.intents()
    assert intent["target"]["dynamic_id"] == "900" and intent["target"]["oid"] == "700"
    assert intent["source_key"] == '["bilibili","dynamic:700","601","whale"]'
    assert intent["status"] == "published"
    assert browsing.adapter.sent[0][0]["type"] == 11
    assert browsing.publication._publication_binding(intent, browsing.store.character(), [])["target"]["type"] == 11
    assert seen == (["901", "900"] if restricted else [])
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    assert len(browsing.adapter.sent) == 1


async def test_an_unreadable_own_video_does_not_stop_scanning_other_posts(browsing, monkeypatch):
    from tuber_studio.operations.inbox.comment_events import collect_own_comments

    await enable_comment_choices(browsing, monkeypatch)

    async def own_uploads(account):
        assert account == "100"
        return [{"bvid": "BV1Yjxw68E9G", "aid": "41"}, {"bvid": BV, "aid": "42"}]

    async def fetch(bvid, *, newest):
        assert newest
        if bvid != BV:
            raise PlatformReadError("not_found", "视频不可读")
        return {"items": [item("601")]}

    monkeypatch.setattr(browsing.adapter, "own_uploads", own_uploads, raising=False)
    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    char = browsing.runtime.character
    row = {"account_id": "100", "character_id": char["id"], "character_version": char["version"],
           "config": browsing.effective(), "epoch": browsing.operations.policy()["epoch"], "preview": False}
    metrics = {"failures": 0}
    await collect_own_comments(browsing, row, metrics)
    assert metrics["failures"] == 1
    event, = browsing.store.all("SELECT payload FROM browsing_events WHERE kind='own_comment'")
    assert json.loads(event["payload"])["bvid"] == BV


@pytest.mark.parametrize("mode", ["auto", "uncertain", "review"])
async def test_comment_like_after_reply_quota_is_full_is_single_and_reconcilable(browsing, monkeypatch, mode):
    first = item("601")
    first["created_at"] -= 2
    second = item("602", uid="301", parent="601") | {"content": "加了三勺盐以后鸡蛋才浮起来。"}
    items = [first]
    likes = []

    async def like(target, *, before_send):
        before_send()
        likes.append(target["parent"])
        second["liked_by_self"] = True
        if mode == "uncertain":
            raise PublishUncertain("测试提交回执丢失")
        return {"code": 0, "action": "comment_like", "rpid": target["parent"]}

    async def fetch(*args, **kwargs):
        return {"items": items}

    async def thread(target):
        source = next(row for row in items if row["comment_id"] == target["parent"])
        return {"target": target, "items": items, "source": {
            "comment_id": source["comment_id"], "author_uid": source["platform_user_id"], "text": source["content"],
        }}

    monkeypatch.setattr(browsing.adapter, "like_comment", like, raising=False)
    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_video_thread", thread, raising=False)
    await enable_comment_choices(browsing, monkeypatch, automatic=True)
    await browsing.configure({"comment_like_enabled": True, "comment_like_auto_publish": mode != "review",
                              "max_replies_per_video": 1})
    monkeypatch.setattr(browsing.comments, "publication_limits", lambda: {"max_per_day": 1})
    generate = browsing.runtime.operation_llm.generate_json
    offered = []

    async def choose(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            model_packet(messages[-1]["content"])
            actions = {branch["properties"]["action"]["enum"][0] for branch in schema["oneOf"]}
            offered.append(actions)
            if "reply_public" not in actions:
                return {"action": "like_comment", "text": "", "reason": "light_interaction",
                        "explanation": "三勺盐的结果值得记一下"}
        return await generate(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", choose)
    row = await finish_comment_round(browsing)
    items.insert(0, second)
    with browsing.store.db:
        admit_comment_page(browsing, row, None, {"bvid": BV, "aid": "42"}, items)
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    assert len(offered) == 2 and "reply_public" in offered[0] and "reply_public" not in offered[1]
    assert "like_comment" in offered[1]
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["601"]
    reply, liked = sorted(browsing.intents(), key=lambda row: row["created_at"])
    assert reply["status"] == "published" and liked["kind"] == "comment_like"
    if mode == "review":
        assert liked["status"] == "pending_review" and not likes
        browsing.review(liked["id"], "approve")
        liked = await browsing.publish(liked["id"])
    assert likes == ["602"]
    assert liked["status"] == ("publish_uncertain" if mode == "uncertain" else "published")
    if mode == "uncertain":
        checked = await browsing.publication._reconcile(liked["id"])
        assert checked["status"] == "published" and checked["receipt"]["observed"]
    assert browsing.runtime.browsing_quotas.reply_status("100", "whale", BV, 1)["published"] == 1
    assert browsing.store.all(
        "SELECT channel,resolved_status AS status,recipient FROM operation_write_attempts ORDER BY started_at"
    ) == [
        {"channel": "comments", "status": "published", "recipient": "300"},
        {"channel": "comment_likes", "status": "published", "recipient": ""},
    ]
    await finished(browsing, automatic=True, interaction_only=True)
    assert likes == ["602"]
