"""Persistent batch choices, pause recovery and independent daily deferrals."""

import asyncio
import json

from test_browsing import BV, authorize_automation, batch_plan_wire, finished, model_packet
from test_browsing import browsing as browsing


async def authorize(browsing, identity="durable-batch"):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await authorize_automation(browsing)


async def test_legacy_comment_auto_browse_cannot_start_work_when_scheduler_is_off(browsing, monkeypatch):
    browsing.store.set("comment_policy", {"auto_browse": True})
    started = []

    async def unexpected(**kwargs):
        started.append(kwargs)
        return {"id": "unexpected"}

    monkeypatch.setattr(browsing, "run", unexpected)
    await browsing.comments.open()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not started
    finally:
        await browsing.comments.close()


async def test_later_search_batches_continue_to_other_pages_after_title_skips(browsing, monkeypatch):
    await browsing.configure(
        {"sources": ["search"], "keywords": ["实验"], "candidate_limit": 1, "max_pages": 1}
    )
    pages = []

    async def search(source, keyword, page, limit):
        pages.append(page)
        return {
            "items": [{"bvid": f"BV{page:010d}", "title": f"实验{page}", "duration": 30}],
            "has_more": page < 3,
        }

    original = browsing.runtime.llm.generate_json

    async def skip(messages, schema, *, max_tokens):
        if schema["title"] == "CandidateBatchPlan":
            candidates = model_packet(messages[-1]["content"])["candidates"]
            return batch_plan_wire([
                    {
                        "bvid": item["bvid"],
                        "action": "skip",
                        "explanation": f"{item['title']}先不看，继续找其它内容",
                    }
                    for item in candidates
                ])
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.adapter, "discover", search)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", skip)
    await authorize(browsing)
    pages.clear()  # Account/source preflight is separate from durable retrieval.
    for _ in range(3):
        row = await finished(browsing, automatic=True, video_only=True)
        assert row["status"] == "completed"
    assert pages == [1, 2, 3]
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")


async def test_autonomous_batch_has_no_hidden_single_main_comment_limit(browsing):
    videos = [BV, "BV1xx411c7mD"]
    await browsing.configure(
        {
            "sources": ["specified"],
            "watch_videos": videos,
            "workers": 1,
            "inspect_limit": 1,
            "recent_sample_limit": 0,
        }
    )
    await authorize(browsing)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed"
    assert row["config"]["top_level_limit"] == 1
    assert row["budget"]["top_levels"] == 2
    assert len(browsing.adapter.sent) == 2


async def test_normal_operations_uses_video_and_comment_queues_together(browsing):
    videos = [BV, "BV1xx411c7mD"]
    await browsing.configure(
        {
            "sources": ["specified"],
            "watch_videos": videos,
            "workers": 1,
            "reply_enabled": True,
            "recent_sample_limit": 0,
        }
    )

    async def comments(video, max_pages=1):
        return {
            "items": [
                {
                    "comment_id": video + "-comment",
                    "thread_id": video + "-comment",
                    "platform_user_id": "300",
                    "display_name": "邻居",
                    "content": "盐水比例多少？",
                }
            ]
        }

    browsing.adapter.fetch = comments
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    row = await finished(browsing, automatic=True)
    assert row["status"] == "completed"
    assert len(browsing.store.all("SELECT * FROM browsing_watch_starts")) == 2
    assert len([intent for intent in browsing.intents() if intent["kind"] == "top_level"]) == 2
    assert len([intent for intent in browsing.intents() if intent["kind"] == "reply_public"]) == 2
    assert not browsing.adapter.sent


async def test_pause_and_restart_resume_original_plan_and_watch_task(browsing, monkeypatch):
    await browsing.configure({"sources": ["specified"], "watch_videos": [BV], "recent_sample_limit": 0})
    await authorize(browsing)
    entered = asyncio.Event()
    metadata = browsing.adapter.video_metadata

    async def pending(video):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(browsing.adapter, "video_metadata", pending)
    result = await browsing.run(automatic=True, video_only=True)
    await asyncio.wait_for(entered.wait(), 2)
    task_id = browsing.tasks(round_id=result["id"])[0]["id"]
    worker = browsing.active[result["id"]]
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert browsing.repo.round(result["id"])["status"] == "paused"
    assert browsing.repo.round(result["id"])["deadline"] is None
    browsing.repo.recover()
    assert not browsing.active
    monkeypatch.setattr(browsing.adapter, "video_metadata", metadata)
    await authorize(browsing, "new-explicit-start")
    resumed = await finished(browsing, automatic=True, video_only=True)
    assert resumed["id"] == result["id"]
    assert resumed["config"]["autonomous_batch"]
    assert resumed["status"] == "completed"
    assert task_id in {task["id"] for task in browsing.tasks(round_id=result["id"])}
    assert sum(name == "CandidateBatchPlan" for name, _ in browsing.runtime.llm.calls) == 1
    assert len(browsing.adapter.sent) == 1
    assert browsing.repo.quotas.watch_status("100", "whale", 0)["started"] == 1


async def test_retrieval_resumes_after_last_saved_page_and_merges_sources(browsing, monkeypatch):
    second = "BV1xx411c7mD"
    await browsing.configure({"sources": ["search"], "keywords": ["盐水"], "recent_sample_limit": 0})
    await authorize(browsing)
    waiting = asyncio.Event()
    calls = []
    blocked = True

    async def discover(source, keyword, page, limit):
        calls.append(page)
        if page == 2 and blocked:
            waiting.set()
            await asyncio.Event().wait()
        return {
            "items": [{"bvid": BV, "title": "盐水"}]
            if page == 1
            else [{"bvid": BV, "title": "盐水"}, {"bvid": second, "title": "新实验"}],
            "has_more": page == 1,
        }

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    result = await browsing.run(automatic=True, video_only=True)
    await asyncio.wait_for(waiting.wait(), 2)
    worker = browsing.active[result["id"]]
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    blocked = False
    resumed = await finished(browsing, automatic=True, video_only=True)
    assert resumed["id"] == result["id"]
    assert calls == [1, 2, 2]
    candidates = browsing.repo.batches.candidates(resumed)
    first = next(item for item in candidates if item["bvid"] == BV)
    assert {source["page"] for source in first["sources"]} == {1, 2}
    assert len(first["sources"]) == 2
    assert sum(name == "CandidateBatchPlan" for name, _ in browsing.runtime.llm.calls) == 1


async def test_title_skips_are_durable_and_do_not_create_watch_records(browsing, monkeypatch):
    await authorize(browsing)
    original = browsing.runtime.llm.generate_json

    async def skip(messages, schema, *, max_tokens):
        if schema["title"] == "CandidateBatchPlan":
            packet = model_packet(messages[-1]["content"])
            return batch_plan_wire([
                    {
                        "bvid": item["bvid"],
                        "action": "skip",
                        "explanation": "盐水实验标题刚讨论过，先看其它话题。",
                    }
                    for item in packet["candidates"]
                ], explanation="这批标题没有想看的内容。")
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", skip)
    first = await finished(browsing, automatic=True, video_only=True)
    second = await finished(browsing, automatic=True, video_only=True)
    assert first["id"] != second["id"]
    assert first["status"] == second["status"] == "completed"
    assert not browsing.store.all("SELECT * FROM browsing_tasks")
    assert browsing.tasks()[0]["phase"] == "title_selection"
    assert browsing.tasks()[0]["reason"]
    choices = browsing.repo.batches.get(first)["plan"]["choices"]
    assert choices[0]["explanation"] == "盐水实验标题刚讨论过，先看其它话题。"


async def test_daily_watch_wait_does_not_block_new_batch_or_repeat_count(browsing):
    second = "BV1xx411c7mD"
    await browsing.configure(
        {
            "sources": ["specified"],
            "watch_videos": [BV, second],
            "workers": 1,
            "daily_watch_limit": 1,
            "recent_sample_limit": 0,
        }
    )
    await authorize(browsing)
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed"
    waiting = browsing.store.one("SELECT * FROM browsing_batch_items WHERE status='daily_wait'")
    assert json.loads(waiting["payload"])["next_at"] > first["updated_at"]
    assert browsing.repo.quotas.watch_status("100", "whale", 1)["started"] == 1
    assert first["metrics"]["failures"] == 0
    next_batch = await finished(browsing, automatic=True, video_only=True)
    assert next_batch["id"] != first["id"]
    assert not any(task["status"] == "failed" for task in browsing.tasks())
    assert browsing.repo.quotas.watch_status("100", "whale", 1)["started"] == 1


async def test_comment_visit_does_not_hold_the_only_new_video_window(browsing, monkeypatch):
    from tuber_studio.operation_turns import operation_lane

    entered_reply, release_reply, entered_video = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lanes = []

    async def execute(row, candidate, metrics):
        lanes.append((candidate["task_kind"], operation_lane.get()))
        if candidate["task_kind"] == "comment_visit":
            entered_reply.set()
            await release_reply.wait()
        else:
            entered_video.set()

    monkeypatch.setattr(browsing, "_run_discovery_video", execute)
    row = {"id": "independent-windows", "config": {"workers": 1}}
    window = asyncio.Semaphore(1)
    reply = asyncio.create_task(
        browsing._process_discovery_video(row, {"bvid": BV, "task_kind": "comment_visit"}, {}, window)
    )
    await asyncio.wait_for(entered_reply.wait(), 1)
    video = asyncio.create_task(
        browsing._process_discovery_video(row, {"bvid": "BV1xx411c7mD", "task_kind": "new_video"}, {}, window)
    )
    try:
        await asyncio.wait_for(entered_video.wait(), 1)
        assert not reply.done()
        assert lanes == [("comment_visit", "reply"), ("new_video", "video")]
    finally:
        release_reply.set()
        await asyncio.gather(reply, video)


def test_resolved_main_decision_and_partial_read_state_survive_material_updates(browsing):
    row = {"account_id": "100", "character_id": "whale"}
    material = {"metadata": {"bvid": BV}, "coverage": "sampled_visual", "missing_parts": []}
    browsing.repo.save_content_progress(row, material, [])
    browsing.repo.save_main_decision(row, BV, {"action": "skip", "explanation": "我暂时没想补充的。"})
    browsing.repo.mark_watch_result(row, BV, fully_read=True)
    browsing.repo.save_content_progress(row, material, [])
    progress = browsing.repo.content_progress(row, BV)
    assert progress["read_state"] == "viewed"
    assert not progress["fully_read"]
    assert progress["main_decision"]["action"] == "skip"
