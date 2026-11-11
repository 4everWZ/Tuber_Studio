import json

import pytest
from test_browsing import browsing as browsing
from test_context_delivery import original_and_draft

from tuber_studio.maintenance import (
    archive_character_version,
    preview_rejected_operations,
    prune_empty_interrupted_rounds,
    prune_rejected_operations,
)
from tuber_studio.operations.browsing.repository import BrowsingRepository


def test_archived_card_is_hidden_but_keeps_historical_provenance(store):
    old = store.character("whale")
    current = store.save_character(old)
    archive_character_version(store, "whale", old["version"], "旧版人设")
    assert store.character("whale", old["version"]) == old
    assert [item["version"] for item in store.versions("whale")] == [current["version"]]
    with pytest.raises(ValueError, match="归档"):
        store.activate("whale", old["version"])
    with pytest.raises(ValueError, match="当前"):
        archive_character_version(store, "whale", current["version"], "当前版本")


def test_cleanup_removes_only_empty_interrupted_rounds(store):
    BrowsingRepository(store)
    for identity in ("empty", "materials", "replies", "recoverable", "batch"):
        store.execute(
            "INSERT INTO browsing_rounds(id,status,config) VALUES(?,'cancelled',?)",
            (identity, '{"batch_id":"kept"}' if identity == "batch" else "{}"),
        )
    store.execute(
        "INSERT INTO browsing_tasks(id,round_id,status,material_key,payload) "
        "VALUES('t','materials','cancelled','real-material','{}')"
    )
    store.execute(
        "INSERT INTO browsing_tasks(id,round_id,status,payload) VALUES('q','recoverable','checkpoint','{}')"
    )
    store.execute("INSERT INTO browsing_intents(id,round_id,status) VALUES('reply','replies','published')")
    assert prune_empty_interrupted_rounds(store) == ["empty"]
    assert len(store.all("SELECT id FROM browsing_rounds")) == 4
    assert prune_empty_interrupted_rounds(store) == []


async def test_old_rejected_cleanup_preserves_receipts_sources_and_progress(browsing):
    store, runtime = browsing.store, browsing.runtime
    char = runtime.character
    scope, source, event, rejected_text, rejected = original_and_draft(browsing.comments, status="rejected", suffix="rejected")
    _, _, published_event, published_text, published = original_and_draft(browsing.comments, receipt={"rpid": "900"}, suffix="published")
    _, _, _, pending_text, pending = original_and_draft(browsing.comments, status="approved", suffix="approved")
    _, _, _, _, uncertain = original_and_draft(browsing.comments, status="rejected", suffix="uncertain-attempt")
    store.execute("INSERT INTO publish_attempts(draft_id,status) VALUES(?,'uncertain')", (uncertain,))
    legacy = 'comments:["bilibili","import-delivery","root"]'
    runtime.context.record(legacy, char, published_event, published_text, "comment_draft")
    runtime.context._archive_history(runtime.context.existing(scope, char), "fixture old compaction")
    trace = runtime.continuations.begin("generation.comment_draft", "old-output", {
        "task": "generation", "source": event, "character": {"id": char["id"], "version": char["version"]},
    }, {"kind": "generation_start"}, original_output={"spoken_text": rejected_text})
    runtime.continuations._step(trace, "generation_model", "completed", output={"spoken_text": rejected_text})
    rejected_memory = store.one("SELECT id FROM grounded_memory WHERE role='assistant' AND event_id=?", (event["event_id"],))["id"]
    store.execute("INSERT INTO dynamic_memory_public VALUES(?,'old-fingerprint',1)", (rejected_memory,))
    store.execute("INSERT INTO dynamic_memory_summaries VALUES(?,?,?,100,1,?,1,'fixture')", (
        char["id"], char["version"], "old-signature", json.dumps([{"source_id": f"memory:{rejected_memory}", "quote": rejected_text}]),
    ))

    def private_draft(status, body, receipt=None):
        identity = store.execute("INSERT INTO dm_drafts(platform,platform_user_id,source_kind,source_id,character_id,character_version,"
            "status,text,account_id,receipt) VALUES('bilibili','300','message',NULL,?,?,?,?,'100',?)", (
            char["id"], char["version"], status, body, json.dumps(receipt) if receipt else None,
        )).lastrowid
        private_event = {"event_id": f"dm:{identity}", "text": "私信来源原文", "account_id": "100"}
        runtime.context.record(private_scope, char, private_event, body, "direct_message")
        return identity

    private_scope = 'dm:["bilibili","100","300"]'
    dm_rejected = private_draft("rejected", "未发送的私信候选去过青藤书店")
    dm_sent = private_draft("published", "已发送的私信去过青藤书店", {"msg_key": "1", "receiver_id": "300"})
    dm_wait = private_draft("pending_review", "等待审核的私信去过青藤书店")
    assert [item["source_event_id"] for item in runtime.context.history_for_model(private_scope, char)
            if item["role"] == "assistant"] == [f"dm:{dm_sent}"]
    recalled = runtime.context.memory.retrieve(private_scope, char, "你上次去过青藤书店吗？")["evidence"]
    assert recalled and all(item["event_id"] == f"dm:{dm_sent}" for item in recalled)

    dynamic = browsing.dynamics._save_decision(char, True, {
        "action": "post", "content": "未发布动态候选", "reason": "旧工作记录", "source_ids": [], "next_evaluation_seconds": 60,
    }, [])
    store.execute("UPDATE dynamic_posts SET status='rejected' WHERE id=?", (dynamic["post_id"],))
    bvid, actor = "fixture-video", "100"
    decision = json.dumps({"action": "reply_public", "text": "旧拒绝主评候选", "reason": "specific_detail", "video_preference": "like"})
    store.execute("INSERT INTO browsing_video_progress VALUES(?,?,?,'{}','[]',1,?,'read')", (actor, char["id"], bvid, decision))
    store.execute("INSERT INTO browsing_comment_progress VALUES(?,?,?,'301','source-proof',?,1)", (actor, char["id"], bvid, decision))
    store.execute("INSERT INTO browsing_watch_starts VALUES(?,?,?,?,'2026-11-07',1)", (actor, char["id"], "watch", bvid))
    for identity, status, kind, parent in (("top", "rejected", "top_level", "0"), ("reply", "rejected", "reply_public", "301"), ("sent", "published", "reply_public", "302")):
        store.execute("INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,status,text,target,receipt) "
                      "VALUES(?,?,?,?,?,?,'round',?,?,?,?)", (identity, actor, char["id"], char["version"], identity, kind,
                      status, "旧拒绝主评候选", json.dumps({"bvid": bvid, "parent": parent}), '{"rpid":"901"}' if status == "published" else None))
    store.execute("INSERT INTO browsing_tasks(id,round_id,bvid,status,payload) VALUES('task','round',?,'selected',?)", (
        bvid, json.dumps({"intent_id": "top", "response_decision": json.loads(decision), "reply_intent_ids": ["reply", "sent"], "coverage": "retained"}),
    ))
    store.execute("INSERT INTO browsing_reply_coverage VALUES(?,?,?,'sent')", (actor, char["id"], "covered-published"))
    preview = preview_rejected_operations(store)
    assert preview["drafts"] == preview["dm_drafts"] == preview["dynamic_posts"] == 1
    assert preview["browsing_intents"] == 2 and preview["operation_continuations"] == 1
    assert store.one("SELECT id FROM drafts WHERE id=?", (rejected,))
    assert prune_rejected_operations(store) == preview
    assert not preview_rejected_operations(store)
    assert not store.one("SELECT id FROM drafts WHERE id=?", (rejected,))
    assert not store.one("SELECT id FROM operation_continuation_steps WHERE continuation_id=?", (trace["id"],))
    assert store.one("SELECT id FROM comments WHERE id=?", (source["id"],))
    assert not store.one("SELECT id FROM dm_drafts WHERE id=?", (dm_rejected,))
    assert store.one("SELECT status FROM dm_drafts WHERE id=?", (dm_wait,))["status"] == "pending_review"
    assert store.one("SELECT receipt FROM dm_drafts WHERE id=?", (dm_sent,))["receipt"]
    assert store.one("SELECT receipt FROM drafts WHERE id=?", (published,))["receipt"]
    assert store.one("SELECT text FROM drafts WHERE id=?", (pending,))["text"] == pending_text
    assert store.one("SELECT status FROM publish_attempts WHERE draft_id=?", (uncertain,))["status"] == "uncertain"
    assert published_text in runtime.context.existing(legacy, char)["history"]
    assert rejected_text not in runtime.context.existing(scope, char)["history"]
    assert rejected_text not in store.one("SELECT history FROM context_history_audit")["history"]
    assert not store.one("SELECT id FROM grounded_memory WHERE id=?", (rejected_memory,))
    assert not store.all("SELECT * FROM dynamic_memory_summaries")
    assert store.one("SELECT status,post_id,content FROM dynamic_decisions WHERE id=?", (dynamic["id"],)) == {
        "status": "rejected", "post_id": None, "content": "",
    }
    video = store.one("SELECT * FROM browsing_video_progress")
    assert video["read_state"] == "read" and video["material"] == "{}"
    assert json.loads(video["main_decision"]) == json.loads(decision) | {"text": "", "action": "read_comments"}
    progress = store.one("SELECT * FROM browsing_comment_progress")
    assert progress["source_hash"] == "source-proof" and json.loads(progress["decision"])["action"] == "skip"
    assert store.one("SELECT task_id FROM browsing_watch_starts")["task_id"] == "watch"
    assert json.loads(store.one("SELECT payload FROM browsing_tasks")["payload"]) == {"reply_intent_ids": ["sent"], "coverage": "retained"}
    assert store.one("SELECT receipt FROM browsing_intents WHERE id='sent'")["receipt"] == '{"rpid":"901"}'
    assert store.one("SELECT intent_id FROM browsing_reply_coverage")["intent_id"] == "sent"
