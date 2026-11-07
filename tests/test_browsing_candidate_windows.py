import json

import pytest
from pydantic import ValidationError
from test_browsing import BV, batch_plan_wire, finished, model_packet
from test_browsing import browsing as browsing
from test_browsing_batches import authorize

from tuber_studio.operation_continuations import content_hash
from tuber_studio.operations.browsing.models import (
    BatchCandidateChoice,
    candidate_batch_plan,
    candidate_batch_schema,
)


def candidate(number):
    return {"bvid": f"BV{number:010d}", "title": f"独立视频{number}", "duration": 30}


def test_candidate_action_requires_a_nonblank_explanation():
    with pytest.raises(ValidationError):
        BatchCandidateChoice(bvid=BV, action="comments", explanation=" \n\t ")
    choice = BatchCandidateChoice(bvid=BV, action="comments", explanation=" 想看看大家怎么讨论盐水实验 ")
    assert choice.explanation == "想看看大家怎么讨论盐水实验"


def test_keyed_plan_requires_all_twenty_sources_and_tied_priorities_use_candidate_order():
    from tuber_studio.inference_validation import _local_schema

    candidates = [candidate(number) | {"available_actions": ["watch", "comments", "skip", "later"]} for number in range(1, 21)]
    schema = candidate_batch_schema(candidates, 4)
    _local_schema(schema)
    choices = schema["properties"]["choices"]
    assert choices["required"] == [item["bvid"] for item in candidates]
    assert set(choices["properties"]) == set(choices["required"])
    assert choices["additionalProperties"] is False
    assert all(set(item["required"]) == {"action", "explanation", "priority"} for item in choices["properties"].values())
    assert all(item["additionalProperties"] is False for item in choices["properties"].values())
    wire = {
        "workers": 3,
        "choices": {
            item["bvid"]: {"action": "watch", "explanation": item["title"] + "想看看", "priority": -10}
            for item in reversed(candidates)
        },
    }
    wire["choices"][candidates[-1]["bvid"]]["priority"] = 4
    plan = candidate_batch_plan(wire, candidates)
    assert plan.workers == 3
    assert plan.videos == [candidates[-1]["bvid"], *[item["bvid"] for item in candidates[:-1]]]
    assert [item.bvid for item in plan.choices] == plan.videos
    assert all(item.action == "watch" and item.explanation for item in plan.choices)
    assert candidate_batch_plan(plan.model_dump(), candidates) == plan


@pytest.mark.parametrize("priority", [True, 1.5, "4"])
def test_candidate_priority_is_an_integer_without_silent_conversion(priority):
    candidates = [candidate(1) | {"available_actions": ["watch", "skip"]}]
    with pytest.raises(ValidationError):
        candidate_batch_plan({"workers": 1, "choices": {
            candidates[0]["bvid"]: {"action": "watch", "explanation": "想看看这条", "priority": priority},
        }}, candidates)


def test_canonical_history_remains_readable_but_cannot_bypass_current_actions():
    from tuber_studio.operations.browsing.models import CandidateBatchPlan

    historical = {
        "videos": [BV], "workers": 1,
        "choices": [{"bvid": BV, "action": "comments", "explanation": "想看看讨论"}],
    }
    assert CandidateBatchPlan.model_validate(historical).videos == [BV]
    with pytest.raises(ValueError, match="未开放"):
        candidate_batch_plan(historical, [{"bvid": BV, "available_actions": ["watch", "skip"]}])


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
        wire_choices = schema["properties"]["choices"]
        assert wire_choices["type"] == "object"
        assert wire_choices["required"] == [item["bvid"] for item in entries]
        assert set(wire_choices["properties"]) == set(wire_choices["required"])
        assert wire_choices["additionalProperties"] is False
        assert "videos" not in schema["properties"]
        assert len({item["bvid"] for item in entries}) == len(entries)
        batches.append([item["bvid"] for item in entries])
        return batch_plan_wire([
                {"bvid": item["bvid"], "action": "skip", "explanation": item["title"] + "本次先略过"}
                for item in entries
            ])

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
        return batch_plan_wire([
                {"bvid": item["bvid"], "action": "skip", "explanation": "这一条已经看过标题，先略过"}
                for item in entries
            ])

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
        result = batch_plan_wire([
                {"bvid": second["bvid"], "action": "comments", "explanation": "想看看实验下的讨论"},
                {"bvid": BV, "action": "watch", "explanation": "想看实验的具体过程"},
                {"bvid": skipped["bvid"], "action": "skip", "explanation": "这次想先看实验话题"},
            ])
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
        assert "comments" not in schema["properties"]["choices"]["properties"][BV]["properties"]["action"]["enum"]
        return batch_plan_wire([{"bvid": BV, "action": "skip", "explanation": "先略过这个标题"}])

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
        return batch_plan_wire([{"bvid": BV, "action": "comments", "explanation": "想聊聊楼里的讨论"}])

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
    [("legacy_duplicate", False), ("missing", True), ("missing", False), ("unknown", False)],
)
async def test_candidate_plan_keeps_all_sources_and_records_one_invalid_result_without_automatic_repair(
    browsing, monkeypatch, planner_round, invalid_kind, resume_old_checkpoint,
):
    from copy import deepcopy

    from tuber_studio.operation_generation import GenerationInvalid

    row, candidates = planner_round
    choices = [
        {"bvid": item["bvid"], "action": "watch" if index == 0 else "skip", "explanation": item["title"] + "本次选择"}
        for index, item in enumerate(candidates)
    ]
    invalid = batch_plan_wire(choices)
    if invalid_kind == "legacy_duplicate":
        invalid = {"workers": 1, "choices": deepcopy(choices)}
        invalid["choices"][-1]["bvid"] = candidates[0]["bvid"]
    else:
        last = invalid["choices"].pop(candidates[-1]["bvid"])
        if invalid_kind == "unknown":
            invalid["choices"][candidate(999)["bvid"]] = last
    calls = []

    async def generate(messages, schema, *, max_tokens):
        assert schema["title"] == "CandidateBatchPlan"
        source_message = next(message["content"] for message in messages if message["content"].startswith("公开来源材料："))
        packet = model_packet(source_message)
        assert [item["bvid"] for item in packet["candidates"]] == [item["bvid"] for item in candidates]
        assert all(item["title"] and item["available_actions"] for item in packet["candidates"])
        calls.append(deepcopy(messages))
        browsing.runtime.llm.last_content = json.dumps(invalid, ensure_ascii=False)
        return deepcopy(invalid)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    coordinator = browsing.runtime.continuations
    with pytest.raises(GenerationInvalid) as error:
        await browsing._plan_batch(row, candidates)
    saved = coordinator.get(error.value.continuation["id"])
    assert saved["state"] == "invalid"
    assert saved["current_output"] is None
    assert saved["original_output"] == json.dumps(invalid, ensure_ascii=False)
    assert json.loads(saved["original_output"]) == invalid
    assert saved["feedback"]["kind"] == "validation_error"
    diagnostic = saved["feedback"]["message"]
    if invalid_kind == "legacy_duplicate":
        assert "唯一键" in diagnostic
    else:
        assert candidates[-1]["bvid"] in diagnostic and "missing_bvids" in diagnostic
    if invalid_kind == "unknown":
        assert candidate(999)["bvid"] in diagnostic and "unknown_bvids" in diagnostic
    if resume_old_checkpoint:
        # Historical repair-loop checkpoints are diagnosed without implicitly
        # starting a new episode or discarding their original model output.
        coordinator._state(saved["id"], "checkpoint")
    with pytest.raises(GenerationInvalid) as repeated:
        await browsing._plan_batch(row, candidates)
    assert repeated.value.continuation["id"] == saved["id"]
    assert repeated.value.continuation["state"] == "invalid"
    assert repeated.value.continuation["original_output"] == saved["original_output"]
    assert len(calls) == 1
    assert browsing.repo.round(row["id"])["budget"]["model_calls"] == 1
    assert browsing.repo.batches.get(row)["plan"] is None
    assert not browsing.store.all("SELECT * FROM browsing_tasks")
    assert not browsing.adapter.sent


async def test_partial_candidate_plan_is_invalid_without_dropping_or_filling_choices(
    browsing, monkeypatch, planner_round,
):
    from tuber_studio.operation_generation import GenerationInvalid

    row, candidates = planner_round
    invalid = batch_plan_wire([
        {"bvid": candidates[0]["bvid"], "action": "watch", "explanation": "我仍想看看这一条"},
    ])
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        browsing.runtime.llm.last_content = json.dumps(invalid, ensure_ascii=False)
        return invalid

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    with pytest.raises(GenerationInvalid) as error:
        await browsing._plan_batch(row, candidates)
    assert calls == ["CandidateBatchPlan"]
    saved = error.value.continuation
    assert saved["state"] == "invalid" and saved["current_output"] is None
    assert json.loads(saved["original_output"]) == invalid
    assert len(json.loads(saved["original_output"])["choices"]) == 1
    assert "missing_bvids" in saved["feedback"]["message"]
    with pytest.raises(GenerationInvalid) as repeated:
        await browsing._plan_batch(row, candidates)
    assert repeated.value.continuation["revisions"] == saved["revisions"]
    assert calls == ["CandidateBatchPlan"]
    assert browsing.repo.batches.get(row)["plan"] is None
    assert not browsing.store.all("SELECT * FROM browsing_tasks")
    assert not browsing.adapter.sent


async def test_keyed_wire_version_plans_the_same_candidates_without_reusing_an_old_invalid_list_result(
    browsing, monkeypatch, planner_round,
):
    from tuber_studio.operation_generation import GenerationInvalid
    from tuber_studio.operations.browsing import decisions

    row, candidates = planner_round
    choices = [
        {"bvid": item["bvid"], "action": "skip", "explanation": item["title"] + "本次略过"}
        for item in candidates
    ]
    legacy = {"workers": 1, "choices": [*choices[:-1], choices[0]]}
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema)
        result = legacy if len(calls) == 1 else batch_plan_wire(choices)
        browsing.runtime.llm.last_content = json.dumps(result, ensure_ascii=False)
        return result

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    original = decisions.generate_with_recovery

    async def old_wire_result(*args, **kwargs):
        kwargs["binding"] = kwargs["binding"] | {"input_format": "independent-candidate-actions-v3"}
        kwargs["identity"] = content_hash(kwargs["binding"])
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(decisions, "generate_with_recovery", old_wire_result)
        with pytest.raises(GenerationInvalid) as error:
            await browsing._plan_batch(row, candidates)
    old_id = error.value.continuation["id"]
    assert await browsing._plan_batch(row, candidates) == []
    assert len(calls) == 2
    old = browsing.runtime.continuations.get(old_id)
    assert old["state"] == "invalid"
    assert old["original_output"] == json.dumps(legacy, ensure_ascii=False)
    records = browsing.store.all("SELECT id,binding,state FROM operation_continuations")
    current = next(record for record in records if record["id"] != old_id)
    assert json.loads(current["binding"])["input_format"] == "keyed-candidate-priorities-v4"
    assert current["state"] == "completed"
    assert browsing.repo.batches.get(row)["plan"]["choices"] == choices
    assert not browsing.adapter.sent
