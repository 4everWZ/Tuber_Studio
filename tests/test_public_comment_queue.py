import json
import time

from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_comment_only import enable_comment_choices, finish_comment_handoff


def item(rpid, uid="300", parent=None):
    return {"comment_id": rpid, "thread_id": "601", "parent_comment_id": parent,
            "parent_verified": True, "platform_user_id": uid, "display_name": "邻居",
            "content": "盐水里的鸡蛋怎么浮起来？", "created_at": int(time.time())}


async def test_duplicate_notification_and_visit_share_context_and_operator_reply_suppresses_generation(browsing, monkeypatch):
    _, calls = await enable_comment_choices(browsing, monkeypatch)
    target = {"oid": "42", "type": 1, "resource_type": "video", "bvid": BV, "root": "601", "parent": "601"}
    source = item("601")
    matched = item("602", uid="100", parent="601")
    items = [source, matched]

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
    await finish_comment_handoff(browsing)
    assert calls == [("CandidateBatchPlan", "video")]
    rows = browsing.store.all("SELECT kind,status,payload FROM browsing_events ORDER BY kind")
    assert [(row["kind"], row["status"]) for row in rows] == [("comment_visit", "skipped"), ("reply", "operator_replied")]
    assert json.loads(rows[1]["payload"])["account_reply"]["comment_id"] == "602"
    assert not browsing.intents() and not browsing.adapter.sent


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
    await finish_comment_handoff(browsing)
    intent, = browsing.intents()
    browsing.review(intent["id"], "approve")
    items.append(item("603", uid="100", parent="601"))
    result = await browsing.publish(intent["id"])
    assert result["status"] == "operator_replied" and result["target"]["account_reply"]["comment_id"] == "603"
    assert not browsing.adapter.sent and not browsing.store.all("SELECT * FROM account_writes")


async def test_own_dynamic_uses_comment_oid_and_regular_inbox(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    own = {"resource_type": "dynamic", "dynamic_id": "900", "oid": "700"}
    source = item("601")

    async def dynamics(account):
        assert account == "100"
        return [own]

    async def fetch(target):
        assert target == own
        return [source]

    async def thread(event):
        assert (event["dynamic_id"], event["oid"]) == ("900", "700")
        return {"account_id": "100", "resource_type": "dynamic", "public_text": "盐水实验的更新",
                "target": own | {"type": 17, "root": "601", "parent": "601"}, "items": [source],
                "source": {"comment_id": "601", "author_uid": "300", "text": source["content"]}}

    monkeypatch.setattr(browsing.adapter, "own_dynamics", dynamics, raising=False)
    monkeypatch.setattr(browsing.adapter, "fetch_dynamic_comments", fetch, raising=False)
    monkeypatch.setattr(browsing.adapter, "read_public_thread", thread)
    await finished(browsing, automatic=True, interaction_only=True)
    intent, = browsing.intents()
    assert intent["target"]["dynamic_id"] == "900" and intent["target"]["oid"] == "700"
    assert intent["source_key"] == '["bilibili","dynamic:700","601","whale"]'
