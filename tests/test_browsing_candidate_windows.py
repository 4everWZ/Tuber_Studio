import json

import pytest
from pydantic import ValidationError
from test_browsing import BV, finished, model_packet
from test_browsing import browsing as browsing
from test_browsing_batches import authorize

from tuber_studio.operation_continuations import content_hash
from tuber_studio.operations.browsing.models import BatchCandidateChoice


def candidate(number):
    return {"bvid": f"BV{number:010d}", "title": f"独立视频{number}", "duration": 30}


def test_candidate_action_requires_a_nonblank_explanation():
    with pytest.raises(ValidationError):
        BatchCandidateChoice(bvid=BV, action="comments", explanation=" \n\t ")
    choice = BatchCandidateChoice(bvid=BV, action="comments", explanation=" 想看看大家怎么讨论盐水实验 ")
    assert choice.explanation == "想看看大家怎么讨论盐水实验"


@pytest.mark.parametrize(("limit", "source_mode"), [(20, "retry"), (3, "retry"), (20, "multiple")])
async def test_overflow_from_retries_or_multiple_sources_is_bounded_and_carried_to_the_next_batch(
    browsing, monkeypatch, limit, source_mode
):
    await browsing.configure(
        {
            "candidate_limit": limit,
            "max_pages": 1,
            "sources": ["recommend", "popular"] if source_mode == "multiple" else ["recommend"],
        }
    )
    batches = []

    async def discover(source, keyword, page, count):
        numbers = (
            range(1, 16)
            if source == "recommend" and source_mode == "multiple"
            else range(7, 22)
            if source_mode == "multiple"
            else range(1, limit + 1)
        )
        return {"items": [candidate(number) for number in numbers], "has_more": False}

    async def skip(messages, schema, *, max_tokens):
        assert schema["title"] == "CandidateBatchPlan"
        packet = model_packet(messages[-1]["content"])
        entries = packet["candidates"]
        assert len(entries) <= limit
        assert schema["properties"]["choices"]["minItems"] == len(entries)
        assert schema["properties"]["choices"]["maxItems"] == len(entries)
        assert "videos" not in schema["properties"]
        assert len({item["bvid"] for item in entries}) == len(entries)
        batches.append([item["bvid"] for item in entries])
        return {
            "videos": [],
            "workers": 1,
            "choices": [
                {"bvid": item["bvid"], "action": "skip", "explanation": item["title"] + "本次先略过"}
                for item in entries
            ],
        }

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", skip)
    original_retries = browsing.repo.retry_candidates
    if source_mode == "retry":
        monkeypatch.setattr(browsing.repo, "retry_candidates", lambda row: [candidate(999)])
    await authorize(browsing)
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed", first
    items = browsing.store.all(
        "SELECT * FROM browsing_batch_items WHERE batch_id=?", (first["config"]["batch_id"],)
    )
    assert len(items) == limit + 1
    deferred = [item for item in items if item["status"] == "later"]
    assert len(deferred) == 1
    carried = json.loads(deferred[0]["payload"])
    assert carried["bvid"] not in batches[0] and "selection" not in carried
    assert "queue_reason" in carried
    for item in items:
        if item["status"] == "skipped":
            assert json.loads(item["payload"])["selection"]["decided_at"] > 0
    if source_mode == "multiple":
        overlap = next(json.loads(item["payload"]) for item in items if item["bvid"] == candidate(7)["bvid"])
        assert {entry["source"] for entry in overlap["sources"]} == {"recommend", "popular"}
    monkeypatch.setattr(browsing.repo, "retry_candidates", original_retries)
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["status"] == "completed"
    assert batches[1] == [carried["bvid"]]
    assert (
        browsing.store.one(
            "SELECT status FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
            (first["config"]["batch_id"], carried["bvid"]),
        )["status"]
        == "completed"
    )


async def test_resuming_a_completed_retrieval_with_21_raw_candidates_uses_a_fresh_bounded_plan(
    browsing, monkeypatch
):
    await authorize(browsing)
    config = browsing.policy() | {
        "autonomous_batch": True,
        "overlay_epoch": browsing.operations.policy()["epoch"],
    }
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character, config, browsing.operations.policy()["epoch"], False
    )

    browsing.repo.batches.ensure(row)
    browsing.repo.batches.merge(row, [candidate(number) for number in range(21)])
    browsing.repo.batches.retrieval(row, {"complete": True})

    async def choose(messages, schema, *, max_tokens):
        entries = model_packet(messages[-1]["content"])["candidates"]
        assert len(entries) == 20
        return {
            "videos": [],
            "workers": 1,
            "choices": [
                {"bvid": item["bvid"], "action": "skip", "explanation": "这一条已经看过标题，先略过"}
                for item in entries
            ],
        }

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", choose)
    assert await browsing._discover(row) == []
    assert (
        browsing.store.one(
            "SELECT COUNT(*) n FROM browsing_batch_items WHERE batch_id=? AND status='later'",
            (row["config"]["batch_id"],),
        )["n"]
        == 1
    )


@pytest.mark.parametrize("redundant_videos", [None, [], [BV]])
async def test_ordered_choices_are_the_single_plan_even_if_a_provider_adds_a_stale_video_index(
    browsing, monkeypatch, redundant_videos
):
    await authorize(browsing)
    config = browsing.effective() | {"autonomous_batch": True}
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character, config, browsing.operations.policy()["epoch"], False
    )
    browsing.repo.batches.ensure(row)
    second, skipped = candidate(2), candidate(3)

    async def choose(messages, schema, *, max_tokens):
        assert "videos" not in schema["properties"]
        entries = model_packet(messages[-1]["content"])["candidates"]
        assert all("comments" in item["available_actions"] for item in entries)
        result = {
            "workers": 1,
            "choices": [
                {"bvid": second["bvid"], "action": "comments", "explanation": "想看看实验下的讨论"},
                {"bvid": BV, "action": "watch", "explanation": "想看实验的具体过程"},
                {"bvid": skipped["bvid"], "action": "skip", "explanation": "这次想先看实验话题"},
            ],
        }
        if redundant_videos is not None:
            result["videos"] = redundant_videos
        return result

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", choose)
    selected = await browsing._plan_batch(row, [{"bvid": BV, "title": "实验"}, second, skipped])
    assert [item["bvid"] for item in selected] == [second["bvid"], BV]
    assert [item["task_kind"] for item in selected] == ["comment_visit", "new_video"]
    assert browsing.repo.batches.get(row)["plan"]["videos"] == [second["bvid"], BV]
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")


async def test_disabled_comments_are_absent_from_the_plan_schema_and_candidate_actions(browsing, monkeypatch):
    await authorize(browsing)
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character,
        browsing.effective() | {"autonomous_batch": True, "reply_enabled": False},
        browsing.operations.policy()["epoch"], False,
    )
    browsing.repo.batches.ensure(row)

    async def choose(messages, schema, *, max_tokens):
        item = model_packet(messages[-1]["content"])["candidates"][0]
        assert "comments" not in item["available_actions"]
        assert "comments" not in schema["$defs"]["BatchCandidateChoice"]["properties"]["action"]["enum"]
        return {"workers": 1, "choices": [{"bvid": BV, "action": "skip", "explanation": "先略过这个标题"}]}

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", choose)
    assert await browsing._plan_batch(row, [{"bvid": BV, "title": "实验"}]) == []


async def test_old_plan_checkpoint_does_not_retrap_the_saved_batch_or_restart_retrieval(browsing, monkeypatch):
    await authorize(browsing)
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character, browsing.effective() | {"autonomous_batch": True},
        browsing.operations.policy()["epoch"], False,
    )
    browsing.repo.batches.ensure(row)
    item = {"bvid": BV, "title": "实验", "duration": 30}
    browsing.repo.batches.merge(row, [item])
    browsing.repo.batches.retrieval(row, {"complete": True, "page": 3})
    # Match the pre-fix binding, including its old candidate input. It cannot
    # recover by itself when recoveries are disabled, as in the reported issue.
    old_candidate = browsing._candidate_view(row, item)
    old_candidate.pop("available_actions")
    data = {"candidates": [old_candidate], "max_videos": 1, "max_workers": row["config"]["workers"]}
    binding = {
        "task": "generation", "account_id": row["account_id"],
        "character": {"id": row["character_id"], "version": row["character_version"]},
        "schema": "CandidateBatchPlan",
        "source_sha256": content_hash({"backend_source": None, "data": data}),
        "input_format": "bounded-candidate-window-v2", "autonomous_model": browsing.model_signature(),
        "autonomous_video": None,
    }
    coordinator = browsing.runtime.continuations
    old = coordinator.begin(
        "generation.browsing", content_hash(binding), binding,
        {"kind": "validation_error", "message": "尚未观看的视频请先选择watch"},
        parent_round_id=row["id"], budget_limits={"max_recoveries": 0},
    )
    coordinator._state(old["id"], "checkpoint")
    generated = []

    async def no_retrieval(*args, **kwargs):
        pytest.fail("the saved retrieval must be reused")

    async def choose(messages, schema, *, max_tokens):
        generated.append(schema["title"])
        return {"workers": 1, "choices": [{"bvid": BV, "action": "comments", "explanation": "想聊聊楼里的讨论"}]}

    monkeypatch.setattr(browsing.adapter, "discover", no_retrieval)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", choose)
    selected = await browsing._discover(row)
    assert generated == ["CandidateBatchPlan"]
    assert selected[0]["task_kind"] == "comment_visit"
    retrieval = browsing.repo.batches.get(row)["retrieval"]
    assert retrieval["complete"] and retrieval["page"] == 3
    assert coordinator.get(old["id"])["state"] == "checkpoint"
    assert len(browsing.store.all("SELECT * FROM operation_continuations")) == 2


@pytest.fixture
async def planner_round(browsing):
    await authorize(browsing)
    row, _ = browsing.repo.create_round(
        browsing.account, browsing.runtime.character, browsing.effective() | {"autonomous_batch": True},
        browsing.operations.policy()["epoch"], False,
    )
    browsing.repo.batches.ensure(row)
    return row, [candidate(number) for number in range(1, 21)]


@pytest.mark.parametrize(
    ("invalid_kind", "resume_old_checkpoint"),
    [("duplicate", False), ("duplicate", True), ("missing", False), ("unknown", False)],
)
async def test_candidate_plan_repairs_structure_once_with_full_sources_and_keeps_model_order(
    browsing, monkeypatch, planner_round, invalid_kind, resume_old_checkpoint,
):
    from copy import deepcopy

    from tuber_studio.operation_generation import GenerationDeferred
    from tuber_studio.operations.browsing import decisions

    row, candidates = planner_round
    choices = [
        {"bvid": item["bvid"], "action": "watch" if index == 0 else "skip", "explanation": item["title"] + "本次选择"}
        for index, item in enumerate(candidates)
    ]
    invalid = {"workers": 1, "choices": deepcopy(choices)}
    if invalid_kind == "missing":
        invalid["choices"].pop()
    else:
        invalid["choices"][-1]["bvid"] = candidates[0]["bvid"] if invalid_kind == "duplicate" else candidate(999)["bvid"]
    corrected = {
        "workers": 2,
        "choices": [
            choices[-1] | {"action": "watch", "explanation": "我想先看最后这条"},
            choices[0],
            *choices[1:-1],
        ],
    }
    calls = []

    async def generate(messages, schema, *, max_tokens):
        assert schema["title"] == "CandidateBatchPlan", "structural repair needs no separate recovery model call"
        source_message = next(message["content"] for message in messages if message["content"].startswith("公开来源材料："))
        packet = model_packet(source_message)
        assert [item["bvid"] for item in packet["candidates"]] == [item["bvid"] for item in candidates]
        assert all(item["title"] and item["available_actions"] for item in packet["candidates"])
        calls.append(deepcopy(messages))
        if len(calls) == 2:
            repair_record = next(message["content"] for message in messages if "以下为任务记录数据：" in message["content"])
            assert json.loads(repair_record.split("以下为任务记录数据：", 1)[1])["original_output"] == json.dumps(invalid, ensure_ascii=False)
            feedback = messages[-1]["content"]
            assert candidates[-1]["bvid"] in feedback and "missing_bvids" in feedback
            if invalid_kind == "duplicate":
                assert candidates[0]["bvid"] in feedback and "duplicate_bvids" in feedback
            if invalid_kind == "unknown":
                assert candidate(999)["bvid"] in feedback and "unknown_bvids" in feedback
        result = invalid if len(calls) == 1 else corrected
        browsing.runtime.llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    coordinator = browsing.runtime.continuations
    if resume_old_checkpoint:
        generate_with_recovery = decisions.generate_with_recovery

        async def previous_zero_recoveries(*args, **kwargs):
            kwargs["max_recoveries"] = 0
            return await generate_with_recovery(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(decisions, "generate_with_recovery", previous_zero_recoveries)
            with pytest.raises(GenerationDeferred):
                await browsing._plan_batch(row, candidates)
        old = coordinator.get(browsing.store.one("SELECT id FROM operation_continuations")["id"])
        assert old["state"] == "checkpoint" and old["limits"]["max_recoveries"] == 0
        coordinator.record_feedback(
            old["id"], old["binding"],
            {"kind": "validation_error", "message": "每个候选须有一个具体去向，同一个BV只选择一次"},
            original_output=json.dumps(invalid, ensure_ascii=False),
        )
        coordinator._state(old["id"], "checkpoint")
    selected = await browsing._plan_batch(row, candidates)
    assert len(calls) == 2
    assert [item["bvid"] for item in selected] == [candidates[-1]["bvid"], candidates[0]["bvid"]]
    plan = browsing.repo.batches.get(row)["plan"]
    assert plan["choices"] == corrected["choices"]
    assert plan["workers"] == corrected["workers"]
    saved = coordinator.get(browsing.store.one("SELECT id FROM operation_continuations")["id"])
    assert saved["state"] == "completed" and saved["limits"]["max_recoveries"] == 1
    assert saved["original_output"] == json.dumps(invalid, ensure_ascii=False)
    assert browsing.repo.round(row["id"])["budget"]["model_calls"] == 2
    assert not browsing.adapter.sent


async def test_candidate_plan_second_invalid_result_checkpoints_without_dropping_or_filling_choices(
    browsing, monkeypatch, planner_round,
):
    from tuber_studio.operation_generation import GenerationDeferred

    row, candidates = planner_round
    invalid = {
        "workers": 1,
        "choices": [
            {"bvid": candidates[0]["bvid"], "action": "watch", "explanation": "我仍想看看这一条"},
            {"bvid": candidates[0]["bvid"], "action": "skip", "explanation": "重复条目尚未修正"},
        ],
    }
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        browsing.runtime.llm.last_content = json.dumps(invalid, ensure_ascii=False)
        return invalid

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    with pytest.raises(GenerationDeferred) as error:
        await browsing._plan_batch(row, candidates)
    assert calls == ["CandidateBatchPlan", "CandidateBatchPlan"]
    saved = error.value.continuation
    assert saved["state"] == "checkpoint"
    assert "duplicate_bvids" in saved["feedback"]["message"]
    assert "missing_bvids" in saved["feedback"]["message"]
    assert browsing.repo.batches.get(row)["plan"] is None
    assert not browsing.store.all("SELECT * FROM browsing_tasks")
    assert not browsing.adapter.sent
