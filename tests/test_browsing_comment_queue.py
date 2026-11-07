"""Video batches hand off durable comments without waiting for the reply lane."""

import asyncio

from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_comment_only import attach_comment_source, comment, enable_comment_choices


async def test_comment_backlog_does_not_barrier_next_video_batch(browsing, monkeypatch):
    mode, _ = await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    first = await finished(browsing, automatic=True, video_only=True)
    queued = browsing.tasks(round_id=first["id"])
    assert queued[0]["phase"] == "comment_queue" and queued[0]["status"] == "selected"
    assert not browsing.intents()

    entered, release = asyncio.Event(), asyncio.Event()
    original = browsing.decisions._reply_to_video_comment

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(browsing.decisions, "_reply_to_video_comment", blocked)
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
        assert browsing.store.one("SELECT status FROM browsing_batch_items WHERE bvid=?", (BV,))["status"] == "running"
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
    original = browsing.decisions._reply_to_video_comment
    block = True

    async def blocked(*args, **kwargs):
        if args[4]["comment_id"] == "602" and block:
            entered.set()
            await asyncio.Event().wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(browsing.decisions, "_reply_to_video_comment", blocked)
    reply = await browsing.run(automatic=True, interaction_only=True)
    worker = browsing.active[reply["id"]]
    await asyncio.wait_for(entered.wait(), 2)
    assert {item["target"]["parent"] for item in browsing.intents()} == {"601"}
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    key = "browsing_comment_page:100:whale:" + BV
    assert browsing.store.get(key) is None  # The unfinished page is not acknowledged.
    assert not browsing.comment_active
    browsing.repo.recover()
    block = False
    resumed = await finished(browsing, automatic=True, interaction_only=True)
    assert resumed["status"] == "completed"
    assert browsing.store.get(key) == 2
    assert calls == [(1, True), (1, False), (1, True), (1, False)]
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
