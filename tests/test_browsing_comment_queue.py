"""Video batches hand off durable comments without waiting for the reply lane."""

import asyncio
import json

import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_comment_only import attach_comment_source, comment, enable_comment_choices


async def test_comment_backlog_does_not_barrier_next_video_batch(browsing, monkeypatch):
    mode, _ = await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    first = await finished(browsing, automatic=True, video_only=True)
    queued = browsing.tasks(round_id=first["id"])
    assert queued, first.get("error")
    assert queued[0]["phase"] == "comment_queue" and queued[0]["status"] == "selected"
    assert not browsing.intents()

    entered, release = asyncio.Event(), asyncio.Event()
    original = browsing._event_unlocked

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(browsing, "_event_unlocked", blocked)
    reply = await browsing.run(automatic=True, interaction_only=True)
    reply_worker = browsing.active[reply["id"]]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        mode["action"] = "watch"
        await browsing.configure({"watch_videos": ["BV1xx411c7mD"]})
        second = await finished(browsing, automatic=True, video_only=True)
        assert second["id"] != first["id"] and second["status"] == "completed"
        assert not reply_worker.done()
        assert browsing.store.one("SELECT bvid FROM browsing_watch_starts")["bvid"] == "BV1xx411c7mD"
        assert browsing.store.one("SELECT status FROM browsing_batch_items WHERE bvid=?", (BV,))["status"] == "completed"
        assert browsing.store.one("SELECT status FROM browsing_events WHERE kind='comment_visit'")["status"] == "received"
        assert len([item for item in browsing.intents() if item["kind"] == "top_level"]) == 1
    finally:
        release.set()
        await asyncio.wait_for(reply_worker, 2)
    assert len([item for item in browsing.intents() if item["kind"] == "reply_public"]) == 1


async def test_handed_off_comments_run_while_another_video_in_same_batch_is_unfinished(browsing, monkeypatch):
    mode, _ = await enable_comment_choices(browsing, monkeypatch)
    mode["action"] = "watch"
    second = "BV1xx411c7mD"
    await browsing.configure({"watch_videos": [BV, second], "workers": 1})
    attach_comment_source(browsing, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = browsing._run_discovery_video

    async def blocked(row, candidate, metrics):
        if candidate["bvid"] == second:
            entered.set()
            await release.wait()
        await original(row, candidate, metrics)

    monkeypatch.setattr(browsing, "_run_discovery_video", blocked)
    start = await browsing.run(automatic=True, video_only=True)
    worker = browsing.active[start["id"]]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        main = browsing.repo.round(start["id"])
        assert main["status"] == "running"
        assert browsing.repo.batches.pending_comment_visits(main)[0]["bvid"] == BV
        reply = await finished(browsing, automatic=True, interaction_only=True)
        assert reply["status"] == "completed" and not worker.done()
        assert len([item for item in browsing.intents() if item["kind"] == "reply_public"]) == 1
    finally:
        release.set()
        await asyncio.wait_for(worker, 2)


async def test_stopped_comment_page_resumes_cursor_without_repeating_finished_floor(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    calls = []

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        calls.append((start_page, newest))
        return {"items": [comment(), comment("602", "601")], "next_page": start_page + 1}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    await finished(browsing, automatic=True, video_only=True)
    entered = asyncio.Event()
    original = browsing._event_unlocked
    block = True

    async def blocked(event_row, row, metrics, event, thread):
        if thread["source"]["comment_id"] == "602" and block:
            entered.set()
            await asyncio.Event().wait()
        return await original(event_row, row, metrics, event, thread)

    monkeypatch.setattr(browsing, "_event_unlocked", blocked)
    reply = await browsing.run(automatic=True, interaction_only=True)
    worker = browsing.active[reply["id"]]
    await asyncio.wait_for(entered.wait(), 2)
    assert {item["target"]["parent"] for item in browsing.intents()} == {"601"}
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    key = "browsing_comment_page:100:whale:" + BV
    assert browsing.store.get(key) == 2
    assert browsing.store.one("SELECT status FROM browsing_events WHERE source_key='602'")["status"] == "received"
    assert not browsing.comment_active
    browsing.repo.recover()
    block = False
    resumed = await finished(browsing, automatic=True, interaction_only=True)
    assert resumed["status"] == "completed"
    assert browsing.store.get(key) == 2
    assert calls == [(1, True), (1, False)]
    assert {item["target"]["parent"] for item in browsing.intents()} == {"601", "602"}
    assert len(browsing.intents()) == 2
    assert sum(title == "ResponseDecision" for title, _ in browsing.runtime.llm.calls) == 2
    assert not browsing.store.all("SELECT * FROM browsing_video_retries")
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
    assert browsing.store.one("SELECT status FROM browsing_batch_items")["status"] == "completed"


async def test_same_video_comment_queue_merges_and_scope_stays_isolated(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    first = await finished(browsing, automatic=True, video_only=True)
    second = await finished(browsing, automatic=True, video_only=True)
    assert first["id"] != second["id"]
    pending = browsing.repo.batches.pending_comment_visits(second)
    assert len(pending) == 1 and len(pending[0]["comment_batch_ids"]) == 2
    for field, value in (("account_id", "other"), ("character_id", "other"), ("character_version", 999)):
        assert not browsing.repo.batches.pending_comment_visits(second | {field: value})
    assert not browsing.repo.batches.pending_comment_visits(second | {"preview": True})
    assert not browsing.repo.batches.pending_comment_visits(second | {
        "config": second["config"] | {"review_session_id": "another-review"},
    })
    reply = await finished(browsing, automatic=True, interaction_only=True)
    assert reply["status"] == "completed"
    assert len(browsing.intents()) == 1
    items = browsing.store.all("SELECT status,task_id FROM browsing_batch_items")
    assert {item["status"] for item in items} == {"completed"}
    assert len({item["task_id"] for item in items}) == 1


@pytest.mark.parametrize("status", ["invalid", "awaiting_review"])
async def test_model_diagnostic_finishes_queue_item_without_automatic_retry(browsing, monkeypatch, status):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    main = await finished(browsing, automatic=True, video_only=True)
    calls = []

    async def diagnose(result, row, metrics):
        calls.append(result["id"])
        browsing.repo.complete(
            result["id"], result["owner"], result["generation"], status,
            {"reason": "模型输出已保留供检查"}, result["material_key"], error="诊断示例",
        )
        result["heartbeat"].cancel()
        await asyncio.gather(result["heartbeat"], return_exceptions=True)

    monkeypatch.setattr(browsing.decisions, "_evaluate_task", diagnose)
    first = await finished(browsing, automatic=True, interaction_only=True)
    assert first["status"] == "completed"
    assert browsing.repo.batches.pending_comment_visits(main) == []
    await finished(browsing, automatic=True, interaction_only=True)
    assert len(calls) == 1
    assert browsing.store.one("SELECT status,error FROM browsing_tasks WHERE id=?", (calls[0],)) == {
        "status": status, "error": "诊断示例",
    }
    assert not browsing.store.all("SELECT * FROM browsing_video_retries")
    assert not browsing.intents()


async def test_finishing_video_batch_does_not_pause_independent_comment_owner(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    main = await finished(browsing, automatic=True, video_only=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = browsing._event_unlocked

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(browsing, "_event_unlocked", blocked)
    reply = await browsing.run(automatic=True, interaction_only=True)
    worker = browsing.active[reply["id"]]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        browsing.store.execute("UPDATE browsing_batches SET plan=NULL WHERE round_id=?", (main["id"],))
        assert browsing.repo.batches.finish(main, status="cancelled")
        assert browsing.store.one("SELECT status FROM browsing_batch_items")["status"] == "completed"
        assert browsing.store.one("SELECT status FROM browsing_events WHERE kind='comment_visit'")["status"] == "received"
        assert browsing.repo.batches.pending_comment_visits(main) == []
    finally:
        release.set()
        await asyncio.wait_for(worker, 2)
    assert browsing.store.one("SELECT status FROM browsing_events WHERE kind='comment_visit'")["status"] == "drafted"


async def test_saved_comment_decision_keeps_review_provenance_before_intent_creation(browsing, monkeypatch):
    from tuber_studio.operations.browsing.models import ResponseDecision

    await enable_comment_choices(browsing, monkeypatch, automatic=True)
    main = await finished(browsing, automatic=True, video_only=True)
    source = {"comment_id": "601", "author_uid": "300", "text": comment()["content"]}
    decision = ResponseDecision(action="reply_public", text="先看看ref:1。", reason="specific_detail")
    coordinator = browsing.runtime.continuations
    generation = coordinator.begin("generation.browsing", "review-lineage", {
        "task": "generation", "character": {"id": "whale", "version": 1},
    }, {"kind": "generation_start"})
    coordinator._state(generation["id"], "awaiting_review", output=decision)
    decision._generation_id = generation["id"]
    decision._generation_identity = {"account_uid": "100", "identity_version": "verified"}
    progress = browsing.decisions.comment_progress
    progress.save(main, BV, source, decision)
    restored = progress.get(main, BV, source)
    assert restored._generation_id == generation["id"]
    assert restored._generation_identity == decision._generation_identity
    intent = browsing.publication._intent(main, "reply_public", {
        "bvid": BV, "oid": "42", "root": "601", "parent": "601",
    }, restored, [], [])
    assert intent["status"] == "pending_review" and not intent["auto_authorized"]
    assert not browsing.adapter.sent


async def test_invalid_comment_does_not_block_other_floors_or_retry_its_model(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    calls = []

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        return {"items": [comment(), comment("602", "602")], "next_page": start_page + 1}

    original = browsing.runtime.llm.generate_json

    async def generate(messages, schema, *, max_tokens):
        value = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision":
            calls.append(schema["title"])
            if len(calls) == 1:
                value["evidence_ids"] = ["ref:999"]
        browsing.runtime.llm.last_content = json.dumps(value, ensure_ascii=False)
        return value

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    await finished(browsing, automatic=True, video_only=True)
    first = await finished(browsing, automatic=True, interaction_only=True)
    assert first["status"] == "completed"
    assert [item["target"]["parent"] for item in browsing.intents()] == ["602"]
    assert browsing.store.get("browsing_comment_page:100:whale:" + BV) == 2
    diagnostics = browsing.tasks(round_id=first["id"])[0]["repetition_decisions"]
    assert diagnostics[0]["comment_id"] == "601" and diagnostics[0]["state"] == "invalid"
    await finished(browsing, automatic=True, video_only=True)
    await finished(browsing, automatic=True, interaction_only=True)
    assert len(calls) == 2
    assert not browsing.adapter.sent
