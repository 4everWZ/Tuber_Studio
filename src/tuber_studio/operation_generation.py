"""Generation validation and prose diagnostics reuse the task continuation loop."""

import time
from contextlib import nullcontext

from .compaction import ContextWindowExceeded
from .generation_meter import GenerationBudgetExceeded
from .operation_continuations import (
    ContinuationCheckpoint,
    GenerationFeedback,
    content_hash,
    continuation_messages,
)
from .operation_feedback import FeedbackModel, coordinator_for, episode_limits


def reply_generation_binding(scope, character, event, mode):
    source = {
        key: event.get(key)
        for key in (
            "event_id",
            "text",
            "platform",
            "platform_user_id",
            "account_id",
            "video_id",
            "thread_id",
            "parent_comment_id",
            "platform_comment_id",
            "source_created_at",
        )
        if key in event
    }
    return {
        "task": "generation",
        "scope": scope,
        "mode": mode,
        "character": {"id": character["id"], "version": character["version"]},
        "source": source,
    }


class GenerationDeferred(ContinuationCheckpoint):
    def __init__(self, continuation):
        super().__init__(
            "运营生成已保留检查点："
            + continuation["state"]
            + "；"
            + continuation["feedback"].get("message", "")
        )
        self.continuation = continuation


def recoverable_feedback(error, provider):
    if isinstance(error, (ContextWindowExceeded, GenerationBudgetExceeded, ContinuationCheckpoint)):
        raise error
    if isinstance(error, GenerationFeedback):
        return error
    raw = getattr(provider, "last_content", None)
    if raw is None:
        raise error
    return GenerationFeedback(str(error), original_output=raw)


async def generate_with_recovery(
    runtime,
    *,
    channel,
    identity,
    binding,
    character,
    messages,
    generate,
    validate,
    max_tokens,
    guard,
    diagnose=None,
    generation_context=nullcontext,
    readers=None,
    parent_round_id=None,
    deadline=None,
    context_data=None,
    reuse_completed=False,
):
    """Callers retain their existing generation-lock contract and source validator."""
    coordinator = coordinator_for(runtime, runtime.store, runtime.operations)
    row = coordinator.find(channel, identity)
    if reuse_completed and row and row["state"] == "completed":
        if content_hash(row["binding"]) != content_hash(binding):
            raise ValueError("生成检查点的来源绑定已变化")
        guard()
        return validate(row["current_output"]), coordinator.dto(channel, identity)

    def checked(value):
        try:
            return validate(value)
        except ValueError as error:
            raw = getattr(runtime.llm, "last_content", None)
            raise GenerationFeedback(str(error), original_output=raw if raw is not None else value) from None

    run_initial = row is None or row["state"] in {"completed", "abandoned"}
    if row is None:
        row = coordinator.begin(
            channel,
            identity,
            binding,
            {"kind": "generation_start"},
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=episode_limits(max_tokens),
        )
    elif run_initial:
        row = coordinator.restart_generation(
            row["id"],
            binding=binding,
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=episode_limits(max_tokens),
        )
    elif row["can_resume"] and row["state"] != "needs_reconciliation":
        row = coordinator.resume(
            row["id"],
            binding=binding,
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=episode_limits(max_tokens),
        )
    model = FeedbackModel(
        runtime,
        coordinator,
        character,
        context_data if context_data is not None else {"task_context": messages},
        max_tokens=row["limits"].get("generation_max_tokens", max_tokens),
        reads=(readers or {}).keys(),
        generation_context=generation_context,
    )

    async def produce(packet, current):
        started = time.perf_counter()
        value = None
        # Preparation may fail before the provider starts a new generation.
        # Never attribute an earlier task's visible output to this failure.
        if getattr(runtime.llm, "generation_metered", False):
            for name, empty in (("last_content", None), ("last_usage", {}), ("last_finish_reason", None)):
                setattr(runtime.llm, name, empty)
        try:
            value = checked(await generate(packet))
        except ValueError as error:
            raise recoverable_feedback(error, runtime.llm) from None
        finally:
            coordinator._step(
                current,
                "generation_model",
                "completed" if value is not None else "interrupted",
                output=getattr(runtime.llm, "last_content", None),
                context={
                    "usage": getattr(runtime.llm, "last_usage", {}),
                    "finish_reason": getattr(runtime.llm, "last_finish_reason", None),
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "max_tokens": max_tokens,
                },
            )
        issue = diagnose(value) if diagnose else None
        if issue:
            raise GenerationFeedback(
                issue, original_output=value, kind="style_diagnostic", accepted_value=value
            )
        return value

    async def initial(current):
        return await produce(messages, current)

    async def revise(current, decision):
        return await produce(continuation_messages(messages), current)

    async def decide(current):
        if not hasattr(runtime.llm, "generate_json"):
            raise ContinuationCheckpoint("当前Provider未提供恢复决策接口")
        return await model.decide(current)

    try:
        result = await coordinator.run(
            row["id"],
            decide=decide,
            revise=revise,
            guard=guard,
            read_more=readers,
            initial=initial if run_initial else None,
        )
    except Exception as error:
        error.continuation = coordinator.dto(channel, identity)
        raise
    if result.state in {"awaiting_review", "completed"} and result.value is not None:
        value = validate(result.value)
        coordinator._state(row["id"], "completed", output=value)
        return value, coordinator.dto(channel, identity) if coordinator.get(row["id"])["recoveries"] else None
    raise GenerationDeferred(coordinator.dto(channel, identity))
