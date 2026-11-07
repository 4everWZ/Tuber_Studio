import asyncio
import json

import pytest
from test_browsing import BV, batch_plan_wire, finished, model_packet
from test_browsing import browsing as browsing

from tuber_studio.operation_turns import operation_lane


async def enable_comment_choices(browsing, monkeypatch, *, automatic=False):
    await browsing.configure({
        "sources": ["specified"], "watch_videos": [BV], "discovery_enabled": True,
        "top_level_enabled": True, "top_level_auto_publish": True,
        "reply_enabled": True, "reply_auto_publish": automatic,
        "recent_sample_limit": 0, "max_replies_per_video": 0, "recipient_cooldown_seconds": 0,
    })
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.set_enabled(True)
    original = browsing.runtime.llm.generate_json
    mode = {"action": "comments"}
    calls = []

    async def choose(messages, schema, *, max_tokens):
        calls.append((schema["title"], operation_lane.get()))
        if schema["title"] == "CandidateBatchPlan":
            candidates = model_packet(messages[-1]["content"])["candidates"]
            return batch_plan_wire([
                {"bvid": item["bvid"], "action": mode["action"], "explanation": "先看看评论里大家用了多少盐，再接着聊"}
                for item in candidates
            ])
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", choose)
    return mode, calls


def comment(identity="601", root="601"):
    return {"comment_id": identity, "thread_id": root, "platform_user_id": "300",
            "display_name": "邻居", "content": "盐水浮鸡蛋要准备多少盐？"}


def attach_comment_source(browsing, monkeypatch):
    async def fetch(video, max_pages=1):
        return {"items": [comment()]}

    async def read_reply(target):
        return {"author_uid": "300", "text": comment()["content"]}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", read_reply, raising=False)


def forbid_video_content(browsing, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("A comment-only task must not read captions/frames or start video processing")

    monkeypatch.setattr(browsing, "_prepare_evidence", forbidden)
    monkeypatch.setattr(browsing.adapter, "subtitles", forbidden)


async def finish_comment_handoff(browsing):
    main = await finished(browsing, automatic=True, video_only=True)
    assert main["status"] == "completed"
    queued = browsing.repo.batches.pending_comment_visits(main)
    assert queued, "The completed video batch must retain its independent comment queue"
    return await finished(browsing, automatic=True, interaction_only=True)


@pytest.mark.parametrize("automatic", [False, True])
async def test_new_video_can_go_straight_to_real_comments_without_watch_main_reply_or_preference(browsing, monkeypatch, automatic):
    _, calls = await enable_comment_choices(browsing, monkeypatch, automatic=automatic)
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    row = await finish_comment_handoff(browsing)
    assert row["status"] == "completed" and row["metrics"]["failures"] == 0
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")
    assert not browsing.store.all("SELECT * FROM browsing_read_progress")
    intents = browsing.intents()
    assert len(intents) == 1 and intents[0]["kind"] == "reply_public"
    assert intents[0]["target"]["parent"] == "601"
    assert intents[0]["material_keys"] == [] and intents[0]["evidence"] == ["public:601"]
    assert intents[0]["status"] == ("published" if automatic else "pending_review")
    assert calls == [("CandidateBatchPlan", "video"), ("ResponseDecision", "reply")]
    task = browsing.tasks(round_id=row["id"])[0]
    assert task["reason"] == "先看看评论里大家用了多少盐，再接着聊"
    assert task["task_kind"] == "comment_visit" and "video_preference" not in task
    if not automatic:
        browsing.review(intents[0]["id"], "approve")
        assert (await browsing.publish(intents[0]["id"]))["status"] == "published"
    assert len(browsing.adapter.sent) == 1


async def test_full_watch_quota_does_not_block_new_video_comments_and_later_watch_counts_its_first_start(browsing, monkeypatch):
    mode, _ = await enable_comment_choices(browsing, monkeypatch)
    await browsing.configure({"daily_watch_limit": 1})
    assert browsing.repo.quotas.start_watch("other-task", "100", "whale", "BV1xx411c7mD", 1)
    attach_comment_source(browsing, monkeypatch)
    first = await finish_comment_handoff(browsing)
    assert first["status"] == "completed" and len(browsing.intents()) == 1
    assert not browsing.repo.content_progress(first, BV)
    assert not browsing.store.one("SELECT task_id FROM browsing_watch_starts WHERE bvid=?", (BV,))
    await browsing.configure({"daily_watch_limit": 0})
    mode["action"] = "watch"
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["status"] == "completed"
    assert len(browsing.store.all("SELECT task_id FROM browsing_watch_starts WHERE bvid=?", (BV,))) == 1
    assert browsing.repo.content_progress(second, BV)
    assert len([intent for intent in browsing.intents() if intent["kind"] == "top_level"]) == 1


async def test_comment_only_keeps_scroll_cursor_and_checks_new_comments_on_return(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    phase, reads = 1, []

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        reads.append((start_page, newest))
        if newest:
            return {"items": [comment()] if phase == 1 else [comment(), comment("602", "602")]}
        return {"items": [comment()] if phase == 1 else [comment("603", "603")], "next_page": start_page + 1}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    first = await finish_comment_handoff(browsing)
    key = "browsing_comment_page:100:whale:" + BV
    assert first["status"] == "completed" and browsing.store.get(key) == 2
    browsing.repo.recover()
    phase = 2
    second = await finish_comment_handoff(browsing)
    assert second["status"] == "completed" and browsing.store.get(key) == 3
    assert reads == [(1, True), (1, False), (1, True), (2, False)]
    assert {intent["target"]["parent"] for intent in browsing.intents()} == {"601", "602", "603"}
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")


async def test_disabled_comment_permission_does_not_read_or_prepare_an_old_comment_task(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    await browsing.configure({"reply_enabled": False})
    forbid_video_content(browsing, monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Disabled comments must not read metadata or comment pages")

    monkeypatch.setattr(browsing.adapter, "video_metadata", forbidden)
    monkeypatch.setattr(browsing.adapter, "fetch", forbidden)
    config = browsing.effective() | {"autonomous_batch": True}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, browsing.operations.policy()["epoch"], False)
    await browsing._process_discovery_video(row, {"bvid": BV, "task_kind": "comment_visit"}, {"prepared": 0, "failures": 0}, asyncio.Semaphore(1))
    assert {task["status"] for task in browsing.tasks(round_id=row["id"])} == {"comments_checked"}
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")


async def test_manual_reread_keeps_comment_only_task_kind_and_does_not_start_watching(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    row = await finish_comment_handoff(browsing)
    task = browsing.tasks(round_id=row["id"])[0]
    browsing.store.execute("DELETE FROM browsing_intents")
    browsing.store.execute("DELETE FROM browsing_comment_progress")
    await browsing.set_enabled(False)
    result = await browsing.reread(task["id"])
    child = browsing.active.get(result["id"])
    if child:
        await child
    assert browsing.repo.round(result["id"])["status"] == "completed"
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")
    assert {intent["kind"] for intent in browsing.intents()} == {"reply_public"}
    decision = json.loads(browsing.store.one("SELECT decision FROM browsing_comment_progress")["decision"])
    restored_task = browsing.tasks(round_id=result["id"])[0]
    assert decision["explanation"] and restored_task["reason"] == decision["explanation"]


async def test_comment_visit_reuses_existing_material_without_changing_viewing_decision_or_preference(browsing, monkeypatch):
    mode, calls = await enable_comment_choices(browsing, monkeypatch)
    mode["action"] = "watch"
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed"
    before = browsing.store.one("SELECT * FROM browsing_video_progress WHERE bvid=?", (BV,))
    assert before["main_decision"]
    watches = browsing.store.all("SELECT * FROM browsing_watch_starts")
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)

    async def forbidden_metadata(*args):
        raise AssertionError("Retained material already supplies the video's verified identity")

    monkeypatch.setattr(browsing.adapter, "video_metadata", forbidden_metadata)
    mode["action"] = "comments"
    calls.clear()
    second = await finish_comment_handoff(browsing)
    assert second["status"] == "completed"
    assert browsing.store.one("SELECT * FROM browsing_video_progress WHERE bvid=?", (BV,)) == before
    assert browsing.store.all("SELECT * FROM browsing_watch_starts") == watches
    assert calls == [("CandidateBatchPlan", "video"), ("ResponseDecision", "reply")]
    assert len([intent for intent in browsing.intents() if intent["kind"] == "top_level"]) == 1
    reply = next(intent for intent in browsing.intents() if intent["kind"] == "reply_public")
    assert reply["material_keys"] and reply["evidence"] == ["public:601"]


async def test_comment_only_agent_can_wait_without_inventing_video_content_or_main_decision(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    original = browsing.runtime.llm.generate_json

    async def wait(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            return {"action": "wait", "reason": "insufficient_evidence", "text": "", "evidence_ids": [],
                    "explanation": "对方还没说盐水比例，等这层补充再聊"}
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", wait)
    row = await finish_comment_handoff(browsing)
    assert row["status"] == "completed" and not browsing.intents()
    decision = browsing.store.one("SELECT decision FROM browsing_comment_progress")
    assert decision and '"action": "wait"' in decision["decision"]
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")


async def test_comment_checkpoint_keeps_model_selection_reason_and_puts_program_state_in_error(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    original = browsing.runtime.llm.generate_json

    async def pause(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            return {"action": "reply_public", "reason": "specific_detail", "text": "盐水浓度有多少？",
                    "evidence_ids": ["missing-reference"], "explanation": "想问清楚这层用的比例"}
        if schema["title"] == "ContinuationDecision":
            return {"action": "wait", "reason": "等补充这条评论的引用", "read": ""}
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", pause)
    row = await finish_comment_handoff(browsing)
    task = browsing.tasks(round_id=row["id"])[0]
    assert task["status"] == "checkpoint"
    assert task["reason"] == "先看看评论里大家用了多少盐，再接着聊"
    assert task["error"] and task["generation_continuation_id"]
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
