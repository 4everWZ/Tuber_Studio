"""Model decisions and a shared publication-rejection continuation pipeline."""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .compaction import ContextWindowExceeded
from .generation_meter import GenerationBudgetExceeded, budgeted_call, budgeted_json
from .operation_continuations import (
    ContinuationCoordinator,
    GenerationFeedback,
    content_hash,
    continuation_context,
    continuation_messages,
    continuation_schema,
)
from .operation_text import operation_character, operation_output_diagnostics, validate_operation_text
from .providers import SYSTEM_RULES, public_error
from .publication_errors import PlatformRejected, UnknownPublication
from .store import dump


def publication_binding(channel, account_id, character, intent, target, source):
    return {
        "channel": channel,
        "account_id": str(account_id),
        "character": {key: character[key] for key in ("id", "version")},
        "intent": intent,
        "target": target,
        "source": source,
    }


def coordinator_for(runtime, store, operations):
    coordinator = getattr(runtime, "continuations", None)
    if coordinator is None:
        coordinator = ContinuationCoordinator(
            store, operations, parent_repository=getattr(runtime, "browsing_repository", None)
        )
        runtime.continuations = coordinator
    return coordinator


def continuation_view(runtime, channel, row, identity=None):
    if row is None:
        return None
    coordinator = getattr(runtime, "continuations", None)
    return row | {
        "continuation": coordinator.dto(channel, row["id"] if identity is None else identity)
        if coordinator
        else None
    }


def generation_view(runtime, continuation_id, channel, local_id, *, private=False):
    if not continuation_id:
        return None
    coordinator = getattr(runtime, "continuations", None)
    if coordinator is None:
        return None
    row = coordinator.get(continuation_id)
    view = coordinator.dto(row["channel"], row["entity_id"]) | {
        "phase": "generation",
        "resume_target": {"channel": channel, "local_id": "generation:" + str(local_id)},
    }
    if row["binding"].get("schema") in {"ResponseDecision", "ChannelDecision"}:
        dispositions = []
        outputs = [
            row["original_output"],
            row["current_output"],
            *(
                step.get("output")
                for step in row["revisions"]
                if step["phase"] not in {"decision", "budget_start"}
            ),
        ]
        for output in outputs:
            if output is None:
                continue
            try:
                value = json.loads(output) if isinstance(output, str) else output
            except (ValueError, TypeError):
                dispositions.append("unresolved")
                continue
            action = value.get("action") if isinstance(value, dict) else None
            channel = value.get("channel") if isinstance(value, dict) else None
            if action == "handoff_private" or channel == "direct_message":
                dispositions.append("private")
            elif channel != "comment" and action not in {
                "reply_public",
                "skip",
                "wait",
                "revise",
                "keep",
                "abandon",
                "read_more",
            }:
                dispositions.append("unresolved")
        if dispositions:
            visibility = "private" if "private" in dispositions else "unresolved"
            view.update(
                output_visibility=visibility,
                private_handoff=visibility == "private",
                private_href="#messages",
            )
            if not private:
                view.update(
                    original_output=None,
                    current_output=None,
                    revisions=[],
                    feedback={"kind": "private_generation", "message": "生成原文在私信工作区查看"},
                    trigger_feedback=None,
                )
    return view


def episode_limits(max_tokens):
    return {
        "max_model_calls": 32,
        "max_input_tokens": 1048576,
        "max_output_tokens": min(2097152, 32 * max_tokens),
        "generation_max_tokens": max_tokens,
    }


def private_generation_views(runtime, account_id, character):
    coordinator = getattr(runtime, "continuations", None)
    if coordinator is None:
        return []
    result = []
    for saved in coordinator.store.all(
        "SELECT id FROM operation_continuations WHERE channel IN ('generation.browsing','generation.comment_channel') AND CAST(json_extract(binding,'$.account_id') AS TEXT)=? AND json_extract(binding,'$.character.id')=? ORDER BY updated_at DESC LIMIT 100",
        (str(account_id), character["id"]),
    ):
        row = coordinator.get(saved["id"])
        if (
            str(row["binding"].get("account_id")) != str(account_id)
            or row["binding"].get("character", {}).get("id") != character["id"]
        ):
            continue
        public = generation_view(runtime, row["id"], "browsing", "")
        if public.get("output_visibility") not in {"private", "unresolved"}:
            continue
        view = generation_view(runtime, row["id"], "browsing", "", private=True)
        source = next(
            (step["context"] for step in reversed(row["revisions"]) if step["phase"] == "source_view"), None
        )
        if source:
            view.update(source)
        else:
            view.pop("resume_target", None)
            view["can_resume"] = False
            view["source_title"] = "浏览回应生成记录"
        result.append(view)
    return result


class FeedbackModel:
    def __init__(
        self, runtime, coordinator, character, context, *, max_tokens=None, reads=(), generation_context=None
    ):
        self.runtime, self.coordinator, self.character, self.context = (
            runtime,
            coordinator,
            character,
            context,
        )
        self.max_tokens = (
            max_tokens
            if max_tokens is not None
            else runtime.settings.data["llm"].get("operations_max_tokens", 8192)
        )
        self.reads = tuple(reads)
        self.generation_context = generation_context

    def messages(self):
        card = operation_character(self.character)
        return continuation_messages(
            [
                {
                    "role": "system",
                    "content": SYSTEM_RULES + dump({key: card[key] for key in ("core", "style", "rules")}),
                },
                {"role": "user", "content": "本次交流的已核验来源与上下文：" + dump(self.context)},
            ]
        )

    async def _compact_output(self, row):
        summaries = {
            item["context"]["source_output_sha256"]
            for item in row["revisions"]
            if item["phase"] == "compaction" and item["status"] == "completed"
        }
        candidates = [row["original_output"], row["current_output"]]
        invalid = next(
            (
                item["output"]
                for item in reversed(row["revisions"])
                if item["status"] == "invalid" and item["output"] is not None
            ),
            None,
        )
        candidates.append(invalid)
        candidates = [
            value for value in candidates if value is not None and content_hash(value) not in summaries
        ]
        if not candidates or not hasattr(self.runtime.llm, "summarize"):
            raise ContextWindowExceeded("当前原始材料与工作输出超窗，保留检查点")
        value = max(candidates, key=lambda item: len(dump(item)))
        summary_messages = [
            {
                "role": "system",
                "content": "为下一步修订整理这份历史草稿：保留原意、情绪、事实主张与需要调整的要点。",
            },
            {"role": "user", "content": dump(value)},
        ]
        maximum = min(self.max_tokens, self.runtime.settings.data["llm"].get("compaction_max_tokens", 4096))
        summary = await budgeted_call(
            self.runtime.llm,
            lambda: self.runtime.llm.summarize(summary_messages, max_tokens=maximum),
            summary_messages,
            max_tokens=maximum,
        )
        if not isinstance(summary, str) or not summary.strip():
            raise GenerationFeedback("旧输出概要为空", original_output=summary)
        self.coordinator._step(
            row,
            "compaction",
            "completed",
            output=summary,
            context={
                "source_output_sha256": content_hash(value),
                "usage": getattr(self.runtime.llm, "last_usage", {}),
            },
        )

    async def _call(self, row, make_messages, schema, phase):
        started = time.perf_counter()
        result = None
        attempted = False
        try:
            lock = (
                self.generation_context()
                if self.generation_context is not None
                else (
                    self.runtime.offline_generation()
                    if hasattr(self.runtime, "offline_generation")
                    else self.runtime.generation_lock
                )
            )
            async with lock:
                while True:
                    current = self.coordinator.get(row["id"])
                    token = continuation_context.set(current)
                    try:
                        try:
                            attempted = True
                            result = await budgeted_json(
                                self.runtime.llm, make_messages(), schema, max_tokens=self.max_tokens
                            )
                            break
                        except ContextWindowExceeded:
                            await self._compact_output(current)
                    finally:
                        continuation_context.reset(token)
        except (ContextWindowExceeded, GenerationBudgetExceeded):
            raise
        except ValueError as error:
            if getattr(self.runtime.llm, "last_content", None) is None:
                raise
            raise GenerationFeedback(
                public_error(error), original_output=getattr(self.runtime.llm, "last_content", None)
            ) from None
        finally:
            self.coordinator._step(
                row,
                "model_" + phase,
                "completed" if result is not None else "interrupted",
                output=(getattr(self.runtime.llm, "last_content", None) or result) if attempted else None,
                context={
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "usage": getattr(self.runtime.llm, "last_usage", {}),
                    "finish_reason": getattr(self.runtime.llm, "last_finish_reason", None),
                },
            )
        return result

    async def decide(self, row):
        schema = continuation_schema(row, self.reads)

        def messages():
            return self.messages() + [
                {
                    "role": "system",
                    "content": "选择本次任务下一步。动作含义："
                    + dump(
                        {
                            key: value
                            for key, value in {
                                "revise": "修订正文；正文在下一步生成",
                                "wait": "稍后继续",
                                "abandon": "结束本次",
                                "read_more": "使用本次提供的读取能力补充材料",
                                "keep": "确认已通过校验正文中的有意文风",
                            }.items()
                            if key in schema["properties"]["action"]["enum"]
                        }
                    )
                    + " 可用读取能力："
                    + dump(self.reads),
                }
            ]

        return await self._call(row, messages, schema, "decision")

    async def revise(self, row, decision):
        def messages():
            return self.messages() + [
                {"role": "system", "content": "结合本次已读取内容与反馈，重新写这次发言的自然正文。"}
            ]

        schema = {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        }
        value = await self._call(row, messages, schema, "revision")
        if not isinstance(value, dict) or set(value) != {"text"}:
            raise GenerationFeedback("修订结果需要唯一text正文", original_output=value)
        try:
            text = validate_operation_text(value["text"])
        except ValueError as error:
            raise GenerationFeedback(str(error), original_output=value) from None
        if row["trigger_feedback"].get("content_hash") == content_hash({"text": text}):
            raise GenerationFeedback("平台已拒绝这个正文版本，选择修订或暂缓", original_output=value)
        if operation_output_diagnostics(text)["needs_review"]:
            raise GenerationFeedback(
                "正文包含大量重复段落；结合表达意图选择保留、修订或暂缓",
                original_output=value,
                kind="style_diagnostic",
                accepted_value=text,
            )
        return text

    async def generate_structured(self, row, schema, instruction):
        return await self._call(
            row, lambda: self.messages() + [{"role": "system", "content": instruction}], schema, "revision"
        )


@dataclass
class PublicationContext:
    binding: dict
    text: str
    character: dict
    context: Any
    guard: Any
    parent_round_id: str | None = None
    deadline: float | None = None
    max_tokens: int | None = None
    readers: dict | None = None
    payload: dict | None = None
    revision: Any = None
    encode_revision: Any = None
    on_defer: Any = None
    manual_approved: bool = False

    def outgoing(self, value=None):
        if value is None:
            return self.payload if self.payload is not None else {"text": self.text}
        return self.encode_revision(value) if self.encode_revision is not None else {"text": value}


class FeedbackPublication:
    """One correction loop shared by channels; send still owns its final guard."""

    def __init__(self, runtime, store, operations):
        self.runtime, self.store, self.operations = runtime, store, operations
        self.coordinator = coordinator_for(runtime, store, operations)

    def limits(self, prepared):
        return episode_limits(
            prepared.max_tokens
            if prepared.max_tokens is not None
            else self.runtime.settings.data["llm"].get("operations_max_tokens", 8192)
        )

    def record_unknown(self, channel, identity, token, binding, payload, message):
        if not token:
            return None
        feedback = {
            "kind": "unknown_write",
            "write_token": token,
            "message": message,
            "sent_payload": payload,
        }
        row = self.coordinator.find(channel, identity)
        if row:
            self.coordinator.record_feedback(row["id"], binding, feedback, original_output=payload)
        else:
            row = self.coordinator.begin(channel, identity, binding, feedback, original_output=payload)
        self.coordinator._state(row["id"], "needs_reconciliation", feedback=feedback)
        return self.coordinator.get(row["id"])

    async def reconcile(self, channel, identity, reader, commit):
        row = self.coordinator.find(channel, identity)
        if row is None or row["state"] != "needs_reconciliation":
            raise ValueError("当前没有待核对的发送")
        token = row["feedback"]["write_token"]
        attempt = self.operations.get_attempt(token)
        if not attempt or attempt.get("binding") != row["binding"]:
            raise ValueError("待核对发送缺少原始目标绑定")
        result = await reader(row, attempt)
        if not isinstance(result, dict) or result.get("status") not in {
            "visible",
            "not_visible",
            "not_found",
            "ambiguous",
            "check_failed",
            "unsupported",
        }:
            raise ValueError("核对接口没有返回有效读取结果")
        self.coordinator._step(row, "reconciliation", result["status"], context=result)
        if result.get("reason") in {"account_changed", "account_unavailable"}:
            self.coordinator._state(
                row["id"], "needs_reconciliation", feedback=row["feedback"] | {"last_check": result}
            )
            raise ValueError("原账号发送结果待核对：当前账号身份无法匹配")
        latest = self.operations.get_attempt(token)
        receipt = (
            (latest.get("resolution_receipt") or latest["receipt"])
            if latest["resolved_status"] == "published"
            else result.get("receipt")
        )
        if (
            latest["resolved_status"] == "published"
            and receipt
            or result["status"] in {"visible", "not_visible"}
            and receipt
        ):
            self.coordinator._state(
                row["id"], "needs_reconciliation", feedback=row["feedback"] | {"last_check": result}
            )
            if latest["resolved_status"] != "published":
                self.operations.reconcile_attempt(
                    token,
                    account_id=row["binding"]["account_id"],
                    binding=row["binding"],
                    receipt=receipt,
                )
            try:
                value = commit(receipt, result)
                if hasattr(value, "__await__"):
                    await value
            except Exception:
                raise UnknownPublication("平台回执已确认，本地记录待同步", token) from None
            self.coordinator._state(
                row["id"],
                "completed",
                feedback=row["feedback"] | {"last_check": result, "message": "已确认平台曾接受本次提交"},
            )
            return {
                "status": "published",
                "receipt": receipt,
                "continuation": self.coordinator.dto(channel, identity),
            }
        self.coordinator._state(
            row["id"], "needs_reconciliation", feedback=row["feedback"] | {"last_check": result}
        )
        return None

    async def continue_task(
        self, channel, identity, prepared, apply_revision, *, automatic=False, epoch=None
    ):
        row = self.coordinator.find(channel, identity)
        maximum = row["limits"].get("generation_max_tokens", prepared.max_tokens)
        model = FeedbackModel(
            self.runtime,
            self.coordinator,
            prepared.character,
            prepared.context,
            max_tokens=maximum,
            reads=(prepared.readers or {}).keys(),
        )

        async def revise(current, decision):
            value = (
                await prepared.revision(model, current, decision)
                if prepared.revision
                else await model.revise(current, decision)
            )
            try:
                payload = prepared.outgoing(value)
            except ValueError as error:
                raise GenerationFeedback(str(error), original_output=value) from None
            if content_hash(payload) == current["trigger_feedback"].get("content_hash"):
                raise GenerationFeedback("平台已拒绝这个提交版本，选择修订或暂缓", original_output=value)
            return value

        result = await self.coordinator.run(
            row["id"],
            decide=model.decide,
            revise=revise,
            guard=prepared.guard,
            read_more=prepared.readers,
        )
        if result.state not in {"awaiting_review", "completed"} or result.value is None:
            if result.state != "cancelled" and prepared.on_defer is not None:
                changed = prepared.on_defer(prepared, result.state)
                if hasattr(changed, "__await__"):
                    await changed
            return {"status": "pending_review", "continuation": self.coordinator.dto(channel, identity)}
        await self.coordinator._guard(prepared.guard)
        self.coordinator._state(row["id"], "awaiting_review")
        changed = apply_revision(prepared, result.value)
        if hasattr(changed, "__await__"):
            await changed
        previous = row["trigger_feedback"].get("write_token")
        self.coordinator.ticket(
            row["id"], previous, prepared.outgoing(result.value), automatic=automatic, epoch=epoch
        )
        return {
            "status": "revised" if automatic else "pending_review",
            "text": result.value,
            "continuation": self.coordinator.dto(channel, identity),
        }

    async def resume(self, channel, identity, continuation_id, *, prepare, apply_revision, reconcile=None):
        row = self.coordinator.find(channel, identity)
        if row is None or row["id"] != continuation_id:
            raise ValueError("恢复任务与当前草稿不匹配")
        if row["state"] == "needs_reconciliation":
            resolved = (
                await reconcile(row["feedback"]["write_token"], None) if reconcile is not None else None
            )
            return resolved or {
                "status": "publish_uncertain",
                "continuation": self.coordinator.dto(channel, identity),
            }
        prepared = await prepare()
        self.coordinator.resume(
            row["id"],
            binding=prepared.binding,
            parent_round_id=prepared.parent_round_id,
            deadline=prepared.deadline,
            budget_limits=self.limits(prepared),
        )
        return await self.continue_task(channel, identity, prepared, apply_revision)

    async def submit(
        self, channel, identity, *, prepare, send, apply_revision, automatic=False, epoch=None, reconcile=None
    ):
        while True:
            existing = self.coordinator.find(channel, identity)
            prepared = await prepare() if existing and existing["state"] != "needs_reconciliation" else None
            if existing and existing["state"] == "needs_reconciliation":
                previous = existing["feedback"].get("write_token")
                if reconcile is not None:
                    resolved = await reconcile(previous, prepared)
                    if resolved is not None:
                        return resolved
                raise UnknownPublication("该发送结果尚未核对确认", previous)
            if automatic and existing and existing.get("manual_revision"):
                return {"status": "pending_review", "continuation": self.coordinator.dto(channel, identity)}
            if existing and existing["state"] not in {"awaiting_review", "completed"}:
                if not automatic and prepared.manual_approved:
                    await self.coordinator._guard(prepared.guard)
                    existing = self.coordinator.approve_manual_revision(
                        existing["id"], binding=prepared.binding, value=prepared.outgoing()
                    )
                else:
                    return {
                        "status": "pending_review",
                        "continuation": self.coordinator.dto(channel, identity),
                    }
            ticket = None
            if existing and existing["state"] == "awaiting_review":
                await self.coordinator._guard(prepared.guard)
                if prepared.binding != existing["binding"]:
                    raise ValueError("修订提交的原始目标或来源已变化")
                previous = existing["trigger_feedback"].get("write_token")
                ticket = self.coordinator.ticket(
                    existing["id"], previous, prepared.outgoing(), automatic=automatic, epoch=epoch
                )
            try:
                value = await send(
                    ticket,
                    prepared.binding if prepared else None,
                    content_hash(prepared.outgoing()) if prepared else None,
                )
                if existing:
                    self.coordinator._state(existing["id"], "completed")
                return value
            except UnknownPublication as error:
                row = self.coordinator.find(channel, identity)
                if row is None or row["state"] != "needs_reconciliation":
                    prepared = prepared or await prepare()
                    self.record_unknown(
                        channel,
                        identity,
                        error.write_token,
                        prepared.binding,
                        prepared.outgoing(),
                        str(error),
                    )
                # A read may positively resolve an attempt. Absence is never a
                # ticket to resubmit an unknown write.
                if reconcile is not None:
                    resolved = await reconcile(error.write_token, prepared)
                    if resolved is not None:
                        return resolved
                raise
            except PlatformRejected as error:
                if not getattr(error, "write_token", None):
                    raise
                attempt = self.operations.get_attempt(error.write_token)

                def checkpoint_rejection(message, state="checkpoint", rejection=error):
                    payload = getattr(rejection, "sent_payload", None)
                    if not attempt or not attempt.get("binding") or payload is None:
                        return
                    feedback = rejection.feedback() | {
                        "write_token": rejection.write_token,
                        "content_hash": content_hash(payload),
                        "preparation_error": message,
                    }
                    saved = self.coordinator.find(channel, identity)
                    if saved:
                        self.coordinator.record_feedback(
                            saved["id"], attempt["binding"], feedback, original_output=payload
                        )
                    else:
                        saved = self.coordinator.begin(
                            channel, identity, attempt["binding"], feedback, original_output=payload
                        )
                    self.coordinator._state(saved["id"], state, feedback=feedback)

                try:
                    prepared = prepared or await prepare()
                except (asyncio.CancelledError, Exception) as preparation_error:
                    checkpoint_rejection(
                        public_error(preparation_error),
                        "cancelled"
                        if isinstance(preparation_error, asyncio.CancelledError)
                        else "checkpoint",
                    )
                    raise
                if (
                    not attempt
                    or attempt["resolved_status"] != "rejected"
                    or attempt.get("binding") != prepared.binding
                    or getattr(error, "body_hash", None) != content_hash(prepared.outgoing())
                ):
                    checkpoint_rejection("发送期间账号、原始来源、角色或正文已变化")
                    raise ValueError("已拒绝发送的账号、原始来源、角色或正文与当前草稿不一致") from None
                feedback = error.feedback() | {
                    "write_token": error.write_token,
                    "content_hash": content_hash(prepared.outgoing()),
                }
                row = self.coordinator.find(channel, identity)
                if row:
                    self.coordinator.record_feedback(
                        row["id"], prepared.binding, feedback, original_output=prepared.outgoing()
                    )
                else:
                    row = self.coordinator.begin(
                        channel,
                        identity,
                        prepared.binding,
                        feedback,
                        original_output=prepared.outgoing(),
                        parent_round_id=prepared.parent_round_id,
                        deadline=prepared.deadline,
                        budget_limits=self.limits(prepared),
                    )
                result = await self.continue_task(
                    channel, identity, prepared, apply_revision, automatic=automatic, epoch=epoch
                )
                if result["status"] != "revised":
                    return result
