"""Continuation preserves original feedback and backend identity across bounded work."""

import asyncio
import json
import time
from contextlib import nullcontext

import pytest

from tuber_studio.operation_continuations import (
    ContinuationCheckpoint,
    ContinuationCoordinator,
    GenerationFeedback,
    content_hash,
    continuation_messages,
)
from tuber_studio.operations_control import OperationsControl


@pytest.fixture
def coordinator(store):
    return ContinuationCoordinator(store, OperationsControl(store))


def binding():
    return {
        "account_id": "100",
        "character": {"id": "whale", "version": 1},
        "intent": "one-source",
        "target": {"recipient_uid": "200", "root": "11", "parent": "12"},
        "source": {"platform_id": "12", "content_sha256": "original-source"},
    }


def begin(coordinator, *, kind="validation_error", **kwargs):
    return coordinator.begin(
        "comments",
        "draft-1",
        binding(),
        {"kind": kind, "message": "specific feedback"},
        original_output="original answer",
        **kwargs,
    )


def pick(action, read=""):
    return {"action": action, "reason": "continue this task", "read": read}


async def test_bad_reference_can_be_revised_with_original_output_and_specific_feedback(coordinator):
    row = begin(coordinator)
    attempts = []

    async def decide(current):
        attempts.append(current)
        return pick("revise")

    async def revise(current, decision):
        if len(attempts) == 1:
            raise GenerationFeedback("引用e99不在本块", original_output={"evidence_ids": ["e99"]})
        messages = continuation_messages([{"role": "user", "content": "same source"}])
        packet = json.loads(messages[-1]["content"].split("：", 1)[1])
        assert packet["original_output"] == "original answer"
        assert packet["feedback"]["message"] == "引用e99不在本块"
        assert packet["revisions"][-1]["output"] == {"evidence_ids": ["e99"]}
        return {"text": "修订正文", "evidence_ids": ["real-cid:0"]}

    result = await coordinator.run(row["id"], decide=decide, revise=revise, guard=lambda: True)
    assert result.state == "awaiting_review" and result.value["evidence_ids"] == ["real-cid:0"]
    saved = coordinator.get(row["id"])
    assert saved["recoveries"] == 2 and saved["binding"] == binding()
    assert saved["original_output"] == "original answer"
    assert saved["current_output"] == result.value and saved["owner"] is None


@pytest.mark.parametrize("action,state", [("wait", "waiting"), ("abandon", "abandoned")])
async def test_agent_can_defer_or_abandon_without_a_rewrite(coordinator, action, state):
    row = begin(coordinator)

    async def decide(row):
        return pick(action)

    async def revise(*args):
        raise AssertionError("No body generation requested")

    result = await coordinator.run(row["id"], decide=decide, revise=revise, guard=lambda: True)
    assert result.state == state
    assert coordinator.get(row["id"])["current_output"] is None


async def test_style_keep_is_allowed_but_cannot_bypass_invalid_structure(coordinator):
    row = begin(coordinator, kind="style_diagnostic")

    async def decide(row):
        return pick("keep")

    async def revise(*args):
        raise AssertionError

    result = await coordinator.run(
        row["id"], decide=decide, revise=revise, guard=lambda: True, original_value="有意重复的角色正文"
    )
    assert result.state == "completed" and result.value == "有意重复的角色正文"
    other = coordinator.begin(
        "comments", "bad-shape", binding(), {"kind": "validation_error", "message": "missing source"}
    )
    result = await coordinator.run(
        other["id"], decide=decide, revise=revise, guard=lambda: True, original_value="source missing"
    )
    saved = coordinator.get(other["id"])
    assert result.state == "checkpoint" and saved["recoveries"] == saved["max_recoveries"]
    assert saved["current_output"] is None


async def test_recovery_limit_keeps_checkpoint_and_explicit_resume_preserves_history(coordinator):
    coordinator.operations.configure({"max_recoveries": 2})
    row = begin(coordinator, parent_round_id="original-parent")

    async def decide(row):
        return pick("revise")

    async def revise(*args):
        raise GenerationFeedback("still invalid", original_output="still invalid raw")

    result = await coordinator.run(
        row["id"], decide=decide, revise=revise, guard=lambda: True, model_scope=nullcontext
    )
    assert result.state == "checkpoint"
    first = coordinator.get(row["id"])
    resumed = coordinator.resume(row["id"], binding=binding(), parent_round_id="new-authorized-parent")
    assert resumed["recoveries"] == 0 and resumed["total_recoveries"] == 2
    assert (
        resumed["revisions"][:-1] == first["revisions"]
        and resumed["parent_round_id"] == "new-authorized-parent"
    )

    async def valid(*args):
        return "valid complete text"

    assert (
        await coordinator.run(
            row["id"], decide=decide, revise=valid, guard=lambda: True, model_scope=nullcontext
        )
    ).value == "valid complete text"


async def test_budget_failure_and_real_material_read_are_explicit_without_losing_binding(coordinator):
    row = begin(coordinator)

    async def decide(row):
        return (
            pick("read_more", "visual")
            if not any(item["phase"] == "read" for item in row["revisions"])
            else pick("revise")
        )

    async def read_more(row):
        return {"frame_id": "real-frame", "cid": "10", "actual_time": 5, "source_version": "new-material"}

    async def revise(row, decision):
        assert any(
            step["context"] and step["context"].get("frame_id") == "real-frame" for step in row["revisions"]
        )
        raise ContinuationCheckpoint("parent model budget exhausted")

    result = await coordinator.run(
        row["id"], decide=decide, revise=revise, guard=lambda: True, read_more={"visual": read_more}
    )
    saved = coordinator.get(row["id"])
    assert result.state == "checkpoint" and saved["binding"] == binding()
    assert saved["feedback"]["kind"] == "capacity" and saved["can_resume"]


async def test_cancel_and_parallel_owner_cannot_start_a_second_generation(coordinator):
    row = begin(coordinator)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def decide(row):
        entered.set()
        await gate.wait()
        return pick("wait")

    async def revise(*args):
        raise AssertionError

    task = asyncio.create_task(coordinator.run(row["id"], decide=decide, revise=revise, guard=lambda: True))
    await entered.wait()
    with pytest.raises(ValueError, match="执行者"):
        await coordinator.run(row["id"], decide=decide, revise=revise, guard=lambda: True)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.get(row["id"])["state"] == "cancelled"
    assert coordinator.get(row["id"])["owner"] is None


async def test_unknown_write_is_checkpointed_without_model_or_resubmission(coordinator):
    row = begin(coordinator)
    coordinator._state(
        row["id"], "needs_reconciliation", feedback={"kind": "unknown_write", "message": "timeout"}
    )

    async def forbidden(*args):
        raise AssertionError("Unknown send cannot be revised or sent")

    result = await coordinator.run(row["id"], decide=forbidden, revise=forbidden, guard=lambda: True)
    assert result.state == "needs_reconciliation"
    with pytest.raises(ValueError, match="核对"):
        coordinator.resume(row["id"], binding=binding())
    with pytest.raises(ValueError, match="原始来源"):
        coordinator.begin(
            "comments", "draft-1", binding() | {"account_id": "other"}, {"kind": "validation_error"}
        )


async def test_expired_or_disabled_recovery_stops_before_model(coordinator):
    row = begin(coordinator, deadline=time.time() - 1)

    async def forbidden(*args):
        raise AssertionError

    assert (
        await coordinator.run(row["id"], decide=forbidden, revise=forbidden, guard=lambda: True)
    ).state == "checkpoint"
    coordinator.operations.configure({"max_recoveries": 0})
    other = coordinator.begin("comments", "disabled", binding(), {"kind": "validation_error"})
    assert (
        await coordinator.run(other["id"], decide=forbidden, revise=forbidden, guard=lambda: True)
    ).state == "checkpoint"
    assert coordinator.get(other["id"])["recoveries"] == 0
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})
