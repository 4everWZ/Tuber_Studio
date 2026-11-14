"""Video batches hand off durable comments without waiting for the reply lane."""

import asyncio
import json

from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_comment_only import comment, enable_comment_choices


async def test_fixed_comment_page_blocks_next_batch_and_survives_restart(browsing, monkeypatch):
    mode, _ = await enable_comment_choices(browsing, monkeypatch)
    entered = asyncio.Event()
    reads = []
    first_page = [comment(), comment("602", "601")]

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        reads.append((video, start_page, newest))
        return {"items": list(first_page), "next_page": 2}

    original = browsing._event_unlocked

    async def blocked(event_row, row, metrics, event, thread):
        if thread["source"]["comment_id"] == "602":
            entered.set()
            await asyncio.Future()
        return await original(event_row, row, metrics, event, thread)

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing, "_event_unlocked", blocked)
    start = await browsing.run(automatic=True, video_only=True)
    worker = browsing.active[start["id"]]
    await asyncio.wait_for(entered.wait(), 3)
    assert browsing.repo.round(start["id"])["status"] == "running"
    assert {intent["target"]["parent"] for intent in browsing.intents()} == {"601"}
    mode["action"] = "watch"
    await browsing.configure({"watch_videos": ["BV1xx411c7mD"]})
    again = await browsing.run(automatic=True, video_only=True)
    assert again["reused"] and again["id"] == start["id"]
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    browsing.repo.recover()
    first_page.append(comment("603", "603"))
    monkeypatch.setattr(browsing, "_event_unlocked", original)
    resumed = await finished(browsing, automatic=True, video_only=True)
    assert resumed["id"] == start["id"] and resumed["status"] == "completed"
    assert reads == [(BV, 1, True)]
    assert {intent["target"]["parent"] for intent in browsing.intents()} == {"601", "602"}
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")


async def test_backlog_high_water_pauses_new_video_until_both_measures_recover(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    import time

    now = time.time()
    for index in range(6):
        identity = str(600 + index)
        data = {"resource_type": "video", "oid": "42", "root": identity,
                "comment_id": identity, "character_id": "whale"}
        browsing.store.execute(
            "INSERT INTO browsing_events VALUES(?, '100','comment_visit',?,?,'received',?,NULL,?)",
            (identity, identity, now, json.dumps(data), now - 301 if index == 0 else now),
        )
    start = await browsing.run(automatic=True, video_only=True)
    assert start["status"] == "deferred"
    assert browsing.backlog_status()["discovery_paused"]
    browsing.store.execute("UPDATE browsing_events SET status='drafted' WHERE id<>'600'")
    assert browsing.backlog_status()["discovery_paused"]
    browsing.store.execute("UPDATE browsing_events SET status='drafted'")
    assert not browsing.backlog_status()["discovery_paused"]
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"


async def test_old_page_keeps_its_cursor_when_backlog_pauses_admission(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    reads = []

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        reads.append((start_page, newest))
        if newest:
            return {"items": [comment()], "next_page": 2}
        return {"items": [comment("609", "609")], "next_page": start_page + 1}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    cursor = "browsing_comment_page:100:whale:" + BV
    browsing.store.set(cursor, 4)
    await finished(browsing, automatic=True, video_only=True)
    assert reads == [(1, True)]
    assert browsing.store.one("SELECT page FROM browsing_comment_revisits")["page"] == 4
    allowed = browsing.page_capacity
    monkeypatch.setattr(browsing, "page_capacity", lambda: False)
    await finished(browsing, automatic=True, interaction_only=True)
    assert reads == [(1, True)] and browsing.store.get(cursor) == 4
    monkeypatch.setattr(browsing, "page_capacity", allowed)
    await finished(browsing, automatic=True, interaction_only=True)
    assert reads == [(1, True), (4, False)]
    assert browsing.store.get(cursor) == 5
    assert {intent["target"]["parent"] for intent in browsing.intents()} == {"601", "609"}


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
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed"
    assert [item["target"]["parent"] for item in browsing.intents()] == ["602"]
    assert browsing.store.get("browsing_comment_page:100:whale:" + BV) is None
    diagnostics = browsing.tasks(round_id=first["id"])[0]["repetition_decisions"]
    assert diagnostics[0]["comment_id"] == "601" and diagnostics[0]["state"] == "invalid"
    await finished(browsing, automatic=True, video_only=True)
    await finished(browsing, automatic=True, interaction_only=True)
    assert len(calls) == 2
    assert not browsing.adapter.sent
