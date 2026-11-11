"""One generation attempt, durable diagnostics, and explicit interrupted resumes."""

import time

from .compaction import ContextWindowExceeded
from .generation_meter import GenerationBudgetExceeded
from .operation_continuations import (
    ContinuationCheckpoint,
    GenerationFeedback,
    content_hash,
)
from .operation_feedback import coordinator_for, episode_limits
from .operation_identity import captured_identity, identity_messages, runtime_identity
from .web_fetch import OperationWebFetch


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
            "agreement_id",
            "conversation_sha256",
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


class GenerationInvalid(ValueError):
    def __init__(self, continuation):
        super().__init__("运营输出无效，保留原输出与诊断：" + continuation["feedback"].get("message", ""))
        self.continuation = continuation


def generation_requires_review(store, identity):
    if not identity:
        return False
    row = store.one("SELECT state FROM operation_continuations WHERE id=?", (identity,))
    return row is None or row["state"] != "completed"


def recoverable_feedback(error, provider):
    if isinstance(error, (ContextWindowExceeded, GenerationBudgetExceeded, ContinuationCheckpoint)):
        raise error
    if isinstance(error, GenerationFeedback):
        return error
    raw = getattr(provider, "last_content", None)
    if getattr(provider, "last_finish_reason", None) == "length":
        return GenerationFeedback(
            "finish_reason=length：输出额度耗尽，保留本次可见输出", original_output=raw, kind="output_limit"
        )
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
    parent_round_id=None,
    deadline=None,
    context_data=None,
    reuse_completed=False,
    allow_web_fetch=False,
):
    """Callers retain their existing generation-lock contract and source validator."""
    actor = captured_identity(messages) or runtime_identity(runtime, character, binding.get("account_id"))
    messages = identity_messages(messages, actor)
    binding = binding | {"self_identity": actor}
    coordinator = coordinator_for(runtime, runtime.store, runtime.operations)
    limits = episode_limits(max_tokens)
    row = coordinator.find(channel, identity)
    if row and content_hash(row["binding"]) != content_hash(binding):
        raise ValueError("生成检查点的来源绑定已变化")
    if row and (
        row["state"] in {"invalid", "deferred", "awaiting_review"}
        or reuse_completed and row["state"] == "completed"
    ):
        guard()
        if row["state"] == "invalid":
            raise GenerationInvalid(coordinator.dto(channel, identity))
        try:
            return validate(row["current_output"]), coordinator.dto(channel, identity)
        except ValueError:
            coordinator._state(row["id"], "invalid")
            raise GenerationInvalid(coordinator.dto(channel, identity)) from None

    def checked(value, original_output):
        try:
            return validate(value)
        except ValueError as error:
            raw = original_output if original_output is not None else value
            raise GenerationFeedback(str(error), original_output=raw) from None

    if row and row["state"] in {"waiting", "abandoned", "needs_material"} or (
        row and row["state"] == "checkpoint" and row["feedback"].get("kind") in {"validation_error", "output_limit", "style_diagnostic"}
    ):
        # Old repair-loop results remain auditable; they are not fresh, valid
        # initial decisions and never silently start another automatic episode.
        coordinator._state(row["id"], "awaiting_review" if row["current_output"] is not None else "invalid")
        saved = coordinator.dto(channel, identity)
        if saved["state"] == "invalid":
            raise GenerationInvalid(saved)
        try:
            return validate(saved["current_output"]), saved
        except ValueError:
            coordinator._state(row["id"], "invalid")
            raise GenerationInvalid(coordinator.dto(channel, identity)) from None
    if row is None:
        row = coordinator.begin(
            channel,
            identity,
            binding,
            {"kind": "generation_start"},
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=limits,
        )
    elif row["can_resume"] or row["state"] == "completed":
        row = coordinator.resume(
            row["id"],
            binding=binding,
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=limits,
        )
    can_fetch = (
        allow_web_fetch
        and character["id"] == "whale"
        and getattr(runtime.llm, "supports_operation_web_fetch", False)
    )

    async def invoke(current, web_fetch):
        started = time.perf_counter()
        value = None
        # Preparation may fail before the provider starts a new generation.
        # Never attribute an earlier task's visible output to this failure.
        if getattr(runtime.llm, "generation_metered", False):
            for name, empty in (("last_content", None), ("last_usage", {}), ("last_finish_reason", None)):
                setattr(runtime.llm, name, empty)
        try:
            raw = (
                await generate(messages, web_fetch=web_fetch) if web_fetch else await generate(messages)
            )
            value = checked(raw, getattr(runtime.llm, "last_content", None))
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
                resume_value=value,
            )
        return value

    async def initial(current):
        saved = coordinator.generation_checkpoint(current["id"])
        page = saved.get("web_fetch")
        proof_step = page["id"] if page and page["status"] == "ok" else None

        def record_fetch(receipt, context):
            nonlocal proof_step
            step_id = coordinator._step(
                current, "web_fetch", receipt["status"], context=receipt, resume_value=context
            )
            if receipt["status"] == "ok":
                proof_step = step_id

        try:
            result = saved.get("generation_model")
            if result is not None:
                value = checked(result["resume_value"], result["output"])
            else:
                web_fetch = (
                    OperationWebFetch(guard, context=page["resume_value"] if page else None, record=record_fetch)
                    if can_fetch else None
                )
                value = await invoke(current, web_fetch)
        finally:
            if proof_step is not None:
                event = (context_data or {}).get("event") or {}
                dm_service = getattr(getattr(runtime, "browsing", None), "messages", None)
                if event.get("agreement_id") and dm_service:
                    await dm_service.fulfill_agreement(event["agreement_id"], proof_step)
        issue = diagnose(value) if diagnose else None
        if issue:
            raise GenerationFeedback(
                issue, original_output=value, kind="style_diagnostic", accepted_value=value
            )
        return value

    try:
        result = await coordinator.run(
            row["id"],
            decide=None,
            revise=None,
            guard=guard,
            initial=initial,
        )
    except Exception as error:
        error.continuation = coordinator.dto(channel, identity)
        raise
    if result.state in {"awaiting_review", "completed", "deferred"} and result.value is not None:
        value = validate(result.value)
        return value, coordinator.dto(channel, identity)
    if result.state == "invalid":
        raise GenerationInvalid(coordinator.dto(channel, identity))
    raise GenerationDeferred(coordinator.dto(channel, identity))
