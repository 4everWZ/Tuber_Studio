"""Persistent, bounded continuation of one backend-bound operations task.

Callbacks own their existing generation/read locks and real request meters.
This coordinator owns the recovery state machine, never platform destinations.
"""

import asyncio
import hashlib
import inspect
import json
import time
from collections import Counter
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from .compaction import ContextWindowExceeded
from .generation_meter import (
    GenerationBudgetExceeded,
    GenerationMeter,
    current_meter,
    reserve_counters,
    settle_counters,
)
from .live_media import StrictModel
from .store import dump

continuation_context = ContextVar("operation_continuation_context", default=None)


def content_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def continuation_messages(messages):
    row = continuation_context.get()
    if row is None:
        return messages
    return [
        *messages,
        {
            "role": "user",
            "content": "继续本次任务，结合上次输出、校验或平台反馈，以及实际补读材料修订。以下为任务记录数据："
            + dump(working_record(row)),
        },
    ]


def working_record(row):
    """Compile a bounded working set; the persisted trace remains complete."""
    revisions = row["revisions"]
    summaries = {
        item["context"]["source_output_sha256"]: item["output"]
        for item in revisions
        if item["phase"] == "compaction"
        and (item.get("context") or {}).get("source_output_sha256")
        and item["status"] == "completed"
    }

    def output(value):
        digest = content_hash(value)
        return (
            {"kind": "previous_output_summary", "source_sha256": digest, "summary": summaries[digest]}
            if digest in summaries
            else value
        )

    latest = next(
        (item for item in reversed(revisions) if item["status"] == "invalid" and item["output"] is not None),
        None,
    )
    failures = [] if latest is None else [latest | {"output": output(latest["output"])}]
    materials = {
        content_hash(item["context"]): item["context"]
        for item in revisions
        if item["phase"] == "read" and item["status"] == "completed"
    }
    counts = Counter((item["phase"], item["status"], item["action"]) for item in revisions)
    packet = {
        "original_output": output(row["original_output"]),
        "feedback": row["feedback"],
        "revisions": failures,
        "read_materials": list(materials.values()),
        "history_summary": [
            {"phase": phase, "status": status, "action": action, "count": count}
            for (phase, status, action), count in counts.items()
        ],
    }
    if row["current_output"] is not None and row["current_output"] != row["original_output"]:
        packet["current_output"] = output(row["current_output"])
    return packet


class ContinuationDecision(StrictModel):
    action: Literal["revise", "keep", "wait", "abandon", "read_more"]
    reason: str = Field(min_length=1, max_length=1000)
    read: str = Field(default="", max_length=80)


def continuation_schema(row, reads=()):
    schema = ContinuationDecision.model_json_schema()
    actions = ["revise", "wait", "abandon"]
    if row.get("keep_allowed", row["trigger_feedback"].get("kind") == "style_diagnostic"):
        actions.append("keep")
    if reads:
        actions.append("read_more")
    schema["properties"]["action"]["enum"] = actions
    schema["properties"]["read"]["enum"] = ["", *sorted(reads)]
    return schema


class GenerationFeedback(ValueError):
    def __init__(self, message, *, original_output=None, kind="validation_error", accepted_value=None):
        super().__init__(message)
        self.original_output, self.kind = original_output, kind
        self.accepted_value = accepted_value


class ContinuationCheckpoint(ValueError):
    """A recoverable capacity boundary; the task remains available to resume."""


@dataclass
class ContinuationResult:
    state: str
    value: Any = None
    continuation_id: str | None = None


def visible_payload(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


class ContinuationCoordinator:
    def __init__(self, store, operations, *, parent_repository=None):
        self.store, self.operations, self.parent_repository = store, operations, parent_repository
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS operation_continuations(
                id TEXT PRIMARY KEY,channel TEXT NOT NULL,entity_id TEXT NOT NULL,
                binding TEXT NOT NULL,state TEXT NOT NULL,parent_round_id TEXT,
                episode TEXT NOT NULL,recoveries INTEGER NOT NULL DEFAULT 0,
                total_recoveries INTEGER NOT NULL DEFAULT 0,deadline REAL NOT NULL,
                feedback TEXT NOT NULL,original_output TEXT,current_output TEXT,
                revision_ticket TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                owner TEXT,lease_until REAL,
                UNIQUE(channel,entity_id));
            CREATE TABLE IF NOT EXISTS operation_continuation_steps(
                id INTEGER PRIMARY KEY,continuation_id TEXT NOT NULL,episode TEXT NOT NULL,
                recovery INTEGER NOT NULL,phase TEXT NOT NULL,status TEXT NOT NULL,
                action TEXT,output TEXT,feedback TEXT,context TEXT,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_continuation_budgets(
                id TEXT PRIMARY KEY,limits TEXT NOT NULL,budget TEXT NOT NULL);
        """)

    def get(self, identity):
        row = self.store.one("SELECT * FROM operation_continuations WHERE id=?", (identity,))
        if not row:
            raise ValueError("恢复任务不存在")
        for key in ("binding", "feedback", "original_output", "current_output"):
            row[key] = json.loads(row[key]) if row[key] is not None else None
        row["max_recoveries"] = self.operations.policy().get("max_recoveries", 8)
        metering = self.store.one(
            "SELECT budget,limits FROM operation_continuation_budgets WHERE id=?", (identity,)
        )
        row["budget"] = json.loads(metering["budget"]) if metering else {}
        row["limits"] = json.loads(metering["limits"]) if metering else {}
        if row["parent_round_id"] and self.parent_repository is not None:
            parent = self.parent_repository.round(row["parent_round_id"])
            if parent:
                row["budget"] = parent["budget"]
                row["limits"] |= {
                    key: parent["config"][key]
                    for key in ("max_model_calls", "max_input_tokens", "max_output_tokens")
                }
        row["can_resume"] = row["state"] in {
            "waiting",
            "checkpoint",
            "needs_material",
            "cancelled",
        }
        row["can_resume"] |= row["state"] in {"pending", "deciding"} and (
            not row["owner"] or (row["lease_until"] or 0) <= time.time()
        )
        row["revisions"] = self.store.all(
            "SELECT * FROM operation_continuation_steps WHERE continuation_id=? ORDER BY id", (identity,)
        )
        for item in row["revisions"]:
            for key in ("output", "feedback", "context"):
                item[key] = json.loads(item[key]) if item[key] is not None else None
        row["trigger_feedback"] = next(
            (item["feedback"] for item in reversed(row["revisions"]) if item["phase"] == "feedback"),
            row["feedback"],
        )
        row["manual_revision"] = any(
            step["phase"] == "manual_revision" and step["status"] == "approved" for step in row["revisions"]
        )
        return row

    def find(self, channel, entity_id):
        row = self.store.one(
            "SELECT id FROM operation_continuations WHERE channel=? AND entity_id=?",
            (channel, str(entity_id)),
        )
        return self.get(row["id"]) if row else None

    def dto(self, channel, entity_id):
        row = self.find(channel, entity_id)
        if row is None:
            return None
        return {
            key: row[key]
            for key in (
                "id",
                "state",
                "recoveries",
                "total_recoveries",
                "max_recoveries",
                "can_resume",
                "feedback",
                "trigger_feedback",
                "original_output",
                "current_output",
                "revisions",
                "budget",
                "limits",
                "deadline",
            )
        }

    def bind_source_view(self, identity, channel, local_id, title):
        """Persist a trusted UI return target separately from immutable model input."""
        if channel not in {"comments", "messages", "dynamics", "browsing"} or not str(local_id):
            raise ValueError("生成入口无效")
        self._step(
            self.get(identity),
            "source_view",
            "bound",
            context={
                "resume_target": {"channel": channel, "local_id": "generation:" + str(local_id)},
                "source_title": title,
            },
        )

    def begin(
        self,
        channel,
        entity_id,
        binding,
        feedback,
        *,
        original_output=None,
        parent_round_id=None,
        deadline=None,
        budget_limits=None,
    ):
        if not isinstance(binding, dict) or not binding:
            raise ValueError("恢复任务需要程序绑定的真实目标与来源")
        old = self.find(channel, entity_id)
        if old:
            if content_hash(old["binding"]) != content_hash(binding):
                raise ValueError("恢复任务的账号、角色、目标或原始来源已变化")
            return old
        parent_round_id, deadline = self._parent_scope(parent_round_id, deadline)
        now, identity = time.time(), str(uuid4())
        self.store.execute(
            "INSERT INTO operation_continuations VALUES(?,?,?,?,?,?,?,0,0,?,?,?,?,?,?,?,NULL,NULL)",
            (
                identity,
                channel,
                str(entity_id),
                dump(binding),
                "pending",
                parent_round_id,
                str(uuid4()),
                deadline if deadline is not None else now + 1800,
                dump(feedback),
                dump(visible_payload(original_output)) if original_output is not None else None,
                None,
                None,
                now,
                now,
            ),
        )
        limits = budget_limits or {
            "max_model_calls": 32,
            "max_input_tokens": 1048576,
            "max_output_tokens": 65536,
        }
        self.store.execute(
            "INSERT INTO operation_continuation_budgets VALUES(?,?,?)",
            (identity, dump(limits), dump({"model_calls": 0, "input_tokens": 0, "output_tokens": 0})),
        )
        row = self.get(identity)
        self._step(row, "feedback", "received", output=original_output, feedback=feedback)
        self._step(
            row,
            "budget",
            "started",
            context={"limits": limits, "deadline": row["deadline"], "parent_round_id": parent_round_id},
        )
        return self.get(identity)

    def _parent_scope(self, parent_round_id, deadline):
        meter = current_meter.get()
        if parent_round_id is None and meter is not None and meter.repository is self.parent_repository:
            parent_round_id = meter.round_id
        deadline = deadline if deadline is not None else time.time() + 1800
        if parent_round_id and self.parent_repository is not None:
            parent = self.parent_repository.round(parent_round_id)
            if not parent:
                raise ValueError("恢复任务的父轮不存在")
            deadline = min(deadline, parent["deadline"])
        return parent_round_id, deadline

    def generation_scope(self, identity):
        row = self.get(identity)
        meter = current_meter.get()
        if meter is not None:
            if (
                row["parent_round_id"]
                and meter.repository is self.parent_repository
                and meter.round_id != row["parent_round_id"]
            ):
                raise ContinuationCheckpoint("恢复任务的父轮与当前计量窗口不同")
            return nullcontext()
        if row["parent_round_id"]:
            if self.parent_repository is None:
                raise ValueError("恢复任务的父预算不可用")
            return GenerationMeter(self.parent_repository, row["parent_round_id"])
        return GenerationMeter(self, identity)

    def reserve_budget(self, identity, **amounts):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.get(identity)
            if (
                row["state"] in {"cancelled", "abandoned", "needs_reconciliation"}
                or time.time() >= row["deadline"]
            ):
                raise ContinuationCheckpoint("恢复任务已停止或达到期限")
            limit_row = self.store.one(
                "SELECT limits FROM operation_continuation_budgets WHERE id=?", (identity,)
            )
            limits = json.loads(limit_row["limits"])
            caps = {key: limits["max_" + key] for key in ("model_calls", "input_tokens", "output_tokens")}
            budget = reserve_counters(row["budget"], caps, amounts)
            self.store.db.execute(
                "UPDATE operation_continuation_budgets SET budget=? WHERE id=?", (dump(budget), identity)
            )

    def _settle_budget(self, identity, field, reserved, actual):
        if actual is None:
            return
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.get(identity)
            budget = settle_counters(row["budget"], field, reserved, actual)
            self.store.db.execute(
                "UPDATE operation_continuation_budgets SET budget=? WHERE id=?", (dump(budget), identity)
            )

    def settle_input(self, identity, reserved, actual):
        self._settle_budget(identity, "input_tokens", reserved, actual)

    def settle_output(self, identity, reserved, actual):
        self._settle_budget(identity, "output_tokens", reserved, actual)

    def record_feedback(self, identity, binding, feedback, *, original_output):
        row = self.get(identity)
        if content_hash(row["binding"]) != content_hash(binding):
            raise ValueError("原始目标或来源已变化")
        if row["owner"] and (row["lease_until"] or 0) > time.time():
            raise ValueError("恢复任务仍在处理")
        self._step(row, "feedback", "received", output=original_output, feedback=feedback)
        self._state(identity, "pending", feedback=feedback)
        return self.get(identity)

    def _state(self, identity, state, *, feedback=None, output=None, owner=None):
        self.store.execute(
            "UPDATE operation_continuations SET state=?,feedback=COALESCE(?,feedback),"
            "current_output=COALESCE(?,current_output),updated_at=? WHERE id=?"
            + (" AND owner=?" if owner is not None else ""),
            (
                state,
                dump(feedback) if feedback is not None else None,
                dump(visible_payload(output)) if output is not None else None,
                time.time(),
                identity,
                *([owner] if owner is not None else []),
            ),
        )

    def _step(self, row, phase, status, *, action=None, output=None, feedback=None, context=None):
        if row.get("owner") and self.get(row["id"])["owner"] != row["owner"]:
            raise ContinuationCheckpoint("恢复执行者已变化，拒绝迟到写入")
        self.store.execute(
            "INSERT INTO operation_continuation_steps(continuation_id,episode,recovery,phase,status,action,"
            "output,feedback,context,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["episode"],
                row["recoveries"],
                phase,
                status,
                action,
                dump(visible_payload(output)) if output is not None else None,
                dump(feedback) if feedback is not None else None,
                dump(context) if context is not None else None,
                time.time(),
            ),
        )

    def _claim(self, identity, owner):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.get(identity)
            if row["owner"] != owner or (row["lease_until"] or 0) <= time.time():
                raise ContinuationCheckpoint("恢复任务的执行租约已失效")
            if row["state"] not in {"pending", "deciding"}:
                return None
            if time.time() >= row["deadline"] or row["recoveries"] >= row["max_recoveries"]:
                self.store.db.execute(
                    "UPDATE operation_continuations SET state='checkpoint',updated_at=? WHERE id=?",
                    (time.time(), identity),
                )
                return None
            self.store.db.execute(
                "UPDATE operation_continuations SET state='deciding',recoveries=recoveries+1,total_recoveries=total_recoveries+1,updated_at=? WHERE id=?",
                (time.time(), identity),
            )
        return self.get(identity)

    @staticmethod
    async def _guard(guard):
        result = guard()
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise asyncio.CancelledError

    async def _guard_current(self, identity, owner, guard):
        row = self.get(identity)
        await self._bounded(row, self._guard, guard)
        if (
            row["owner"] != owner
            or time.time() >= row["deadline"]
            or row["recoveries"] > row["max_recoveries"]
        ):
            raise ContinuationCheckpoint("恢复任务执行者变化或期限已到")

    @staticmethod
    async def _bounded(row, callback, *args):
        timer = asyncio.timeout(max(0, row["deadline"] - time.time()))
        try:
            async with timer:
                return await callback(*args)
        except TimeoutError:
            if timer.expired():
                raise ContinuationCheckpoint("恢复任务达到本轮期限") from None
            raise

    async def run(
        self,
        identity,
        *,
        decide,
        revise,
        guard,
        read_more=None,
        original_value=None,
        model_scope=None,
        initial=None,
    ):
        """All model callbacks must use the task's real generation meter and lock.

        Only GenerationFeedback is retried. Transport/authorization/programming
        exceptions are not silently converted into another platform submission.
        """
        owner = str(uuid4())
        candidate = original_value
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.get(identity)
            if row["owner"] and (row["lease_until"] or 0) > time.time():
                raise ValueError("恢复任务已有执行者")
            self.store.db.execute(
                "UPDATE operation_continuations SET owner=?,lease_until=? WHERE id=?",
                (owner, max(row["deadline"], time.time() + 1), identity),
            )
        try:
            if initial is not None:
                row = self.get(identity)
                await self._bounded(row, self._guard, guard)
                try:
                    with model_scope() if model_scope else self.generation_scope(identity):
                        value = await self._bounded(row, initial, row)
                    await self._guard_current(identity, owner, guard)
                    self._step(row, "initial_generation", "completed", output=value)
                    self._state(identity, "completed", output=value, owner=owner)
                    return ContinuationResult("completed", value, identity)
                except GenerationFeedback as error:
                    feedback = {"kind": error.kind, "message": str(error)}
                    self._step(row, "feedback", "received", output=error.original_output, feedback=feedback)
                    self.store.execute(
                        "UPDATE operation_continuations SET original_output=COALESCE(original_output,?) WHERE id=? AND owner=?",
                        (dump(visible_payload(error.original_output)), identity, owner),
                    )
                    candidate = error.accepted_value
                    self._state(identity, "pending", feedback=feedback, output=candidate, owner=owner)
                except (ContextWindowExceeded, GenerationBudgetExceeded) as error:
                    self._state(
                        identity,
                        "checkpoint",
                        feedback={"kind": "capacity", "message": str(error)},
                        owner=owner,
                    )
                    return ContinuationResult("checkpoint", continuation_id=identity)
            while True:
                await self._bounded(self.get(identity), self._guard, guard)
                row = self._claim(identity, owner)
                if row is None:
                    return ContinuationResult(self.get(identity)["state"], continuation_id=identity)
                if candidate is None and row["feedback"].get("kind") == "style_diagnostic":
                    candidate = row["current_output"]
                row["keep_allowed"] = (
                    row["feedback"].get("kind") == "style_diagnostic" and candidate is not None
                )
                token = continuation_context.set(row)
                raw_decision = None
                try:
                    with model_scope() if model_scope else self.generation_scope(identity):
                        raw_decision = await self._bounded(row, decide, row)
                    decision = ContinuationDecision.model_validate(raw_decision)
                    await self._guard_current(identity, owner, guard)
                    allowed = {"revise", "wait", "abandon"}
                    if row["keep_allowed"]:
                        allowed.add("keep")
                    if read_more:
                        allowed.add("read_more")
                    if decision.action not in allowed:
                        raise GenerationFeedback(
                            "当前反馈不支持此恢复动作", original_output=decision.model_dump()
                        )
                    self._step(row, "decision", "completed", action=decision.action, output=decision)
                    if decision.action in {"wait", "abandon"}:
                        state = "waiting" if decision.action == "wait" else "abandoned"
                        self._state(identity, state, owner=owner)
                        return ContinuationResult(state, continuation_id=identity)
                    if decision.action == "keep":
                        self._state(identity, "completed", output=candidate, owner=owner)
                        return ContinuationResult("completed", candidate, identity)
                    if decision.action == "read_more":
                        if decision.read not in read_more:
                            raise GenerationFeedback(
                                "读取能力不在本次任务已提供的范围", original_output=decision.model_dump()
                            )
                        addition = await self._bounded(row, read_more[decision.read], row)
                        await self._guard_current(identity, owner, guard)
                        self._step(
                            row,
                            "read",
                            "completed" if addition else "unavailable",
                            action=decision.read,
                            context=addition,
                        )
                        if not addition:
                            self._state(identity, "needs_material", owner=owner)
                            return ContinuationResult("needs_material", continuation_id=identity)
                        self._state(
                            identity,
                            "pending",
                            feedback={"kind": "material_updated", "message": "新材料已加入本任务上下文"},
                            owner=owner,
                        )
                        continue
                    with model_scope() if model_scope else self.generation_scope(identity):
                        value = await self._bounded(row, revise, row, decision)
                    await self._guard_current(identity, owner, guard)
                    self._step(row, "revision", "completed", action="revise", output=value)
                    self._state(identity, "awaiting_review", output=value, owner=owner)
                    return ContinuationResult("awaiting_review", value, identity)
                except (GenerationFeedback, ValidationError) as error:
                    feedback = {"kind": getattr(error, "kind", "validation_error"), "message": str(error)}
                    self._step(
                        row,
                        "generation",
                        "invalid",
                        output=getattr(error, "original_output", raw_decision),
                        feedback=feedback,
                    )
                    accepted = getattr(error, "accepted_value", None)
                    if accepted is not None:
                        if feedback["kind"] != "style_diagnostic":
                            raise ValueError("只有正文诊断可保留已校验候选") from None
                        candidate = accepted
                    self._state(identity, "pending", feedback=feedback, output=accepted, owner=owner)
                except (ContinuationCheckpoint, ContextWindowExceeded, GenerationBudgetExceeded) as error:
                    self._state(
                        identity,
                        "checkpoint",
                        feedback={"kind": "capacity", "message": str(error)},
                        owner=owner,
                    )
                    return ContinuationResult("checkpoint", continuation_id=identity)
                finally:
                    continuation_context.reset(token)
        except asyncio.CancelledError:
            self._state(identity, "cancelled", owner=owner)
            raise
        except ContinuationCheckpoint as error:
            self._state(
                identity, "checkpoint", feedback={"kind": "capacity", "message": str(error)}, owner=owner
            )
            return ContinuationResult("checkpoint", continuation_id=identity)
        except Exception as error:
            self._state(
                identity,
                "checkpoint",
                feedback={"kind": "interrupted", "error_type": type(error).__name__},
                owner=owner,
            )
            raise
        finally:
            self.store.execute(
                "UPDATE operation_continuations SET owner=NULL,lease_until=NULL WHERE id=? AND owner=?",
                (identity, owner),
            )

    def resume(self, identity, *, binding, parent_round_id=None, deadline=None, budget_limits=None):
        return self._new_episode(
            identity,
            binding=binding,
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=budget_limits,
        )

    def restart_generation(
        self, identity, *, binding, parent_round_id=None, deadline=None, budget_limits=None
    ):
        return self._new_episode(
            identity,
            binding=binding,
            parent_round_id=parent_round_id,
            deadline=deadline,
            budget_limits=budget_limits,
            restart_generation=True,
        )

    def _new_episode(
        self, identity, *, binding, parent_round_id, deadline, budget_limits, restart_generation=False
    ):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.get(identity)
            if content_hash(binding) != content_hash(row["binding"]):
                raise ValueError("恢复时原始目标或来源已变化")
            if row["owner"] and (row["lease_until"] or 0) > time.time():
                raise ValueError("恢复任务仍在处理")
            if restart_generation:
                if row["binding"].get("task") != "generation" or row["state"] not in {
                    "completed",
                    "abandoned",
                }:
                    raise ValueError("只有新的生成请求可以重新开始已完成的生成任务")
            elif not row["can_resume"] or row["state"] == "needs_reconciliation":
                raise ValueError("当前恢复任务不可继续生成；未知发送须先核对平台")
            parent_round_id, deadline = self._parent_scope(parent_round_id, deadline)
            self.store.db.execute(
                "UPDATE operation_continuations SET state='pending',episode=?,recoveries=0,parent_round_id=?,deadline=?,updated_at=?,owner=NULL,lease_until=NULL WHERE id=?",
                (str(uuid4()), parent_round_id, deadline, time.time(), identity),
            )
            self.store.db.execute(
                "UPDATE operation_continuation_budgets SET budget=?,limits=? WHERE id=?",
                (
                    dump({"model_calls": 0, "input_tokens": 0, "output_tokens": 0}),
                    dump(budget_limits or row["limits"]),
                    identity,
                ),
            )
            if restart_generation:
                self.store.db.execute(
                    "UPDATE operation_continuations SET original_output=NULL,current_output=NULL,feedback=? WHERE id=?",
                    (dump({"kind": "generation_start"}), identity),
                )
            updated = self.get(identity)
            if restart_generation:
                self._step(updated, "feedback", "received", feedback={"kind": "generation_start"})
            self._step(
                updated,
                "budget",
                "started",
                context={
                    "limits": updated["limits"],
                    "deadline": updated["deadline"],
                    "parent_round_id": parent_round_id,
                },
            )
        return self.get(identity)

    def approve_manual_revision(self, identity, *, binding, value):
        """Adopt an explicitly reviewed edit without another model generation."""
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.get(identity)
            if row["owner"] and (row["lease_until"] or 0) > time.time():
                raise ValueError("恢复任务仍在处理，不能覆盖在途执行者")
            if row["state"] not in {
                "waiting",
                "checkpoint",
                "needs_material",
                "abandoned",
                "cancelled",
                "pending",
                "deciding",
            }:
                raise ValueError("当前恢复状态不允许人工修订提交；未知发送须先核对")
            trigger = row["trigger_feedback"]
            if content_hash(binding) != content_hash(row["binding"]):
                raise ValueError("人工修订的原始目标或来源已变化")
            previous = self.operations.get_attempt(trigger.get("write_token"))
            if (
                trigger.get("kind") != "platform_rejection"
                or not trigger.get("content_hash")
                or not previous
                or previous["resolved_status"] != "rejected"
                or previous.get("binding") != binding
            ):
                raise ValueError("人工修订需要原平台明确拒绝的绑定发送")
            if content_hash(visible_payload(value)) == trigger.get("content_hash"):
                raise ValueError("人工修订正文未变化，不能重发原文")
            # Fence an expired worker; its owner-qualified writes may not adopt
            # a late result over the newly reviewed draft.
            self.store.db.execute(
                "UPDATE operation_continuations SET state='awaiting_review',owner=NULL,lease_until=NULL,current_output=?,updated_at=? WHERE id=?",
                (dump(visible_payload(value)), time.time(), identity),
            )
            self._step(
                self.get(identity),
                "manual_revision",
                "approved",
                action="manual",
                output=value,
                context={"previous_token": previous["token"], "automatic": False},
            )
        return self.get(identity)

    def ticket(self, identity, previous_token, value, *, automatic=None, epoch=None):
        row = self.get(identity)
        if row["owner"] and (row["lease_until"] or 0) > time.time():
            raise ValueError("恢复任务仍在处理")
        if automatic and row["manual_revision"]:
            raise ValueError("人工修订需人工发布，不复用自动授权")
        if (
            row["state"] != "awaiting_review"
            or row["trigger_feedback"].get("kind") != "platform_rejection"
            or previous_token != row["trigger_feedback"].get("write_token")
            or content_hash(visible_payload(value)) == row["trigger_feedback"].get("content_hash")
        ):
            raise ValueError("只有明确可修正的平台拒绝可申请修订提交")
        ticket = self.operations.issue_revision_ticket(
            previous_token,
            binding=row["binding"],
            content_hash=content_hash(visible_payload(value)),
            automatic=automatic,
            epoch=epoch,
        )
        self.store.execute(
            "UPDATE operation_continuations SET revision_ticket=?,updated_at=? WHERE id=?",
            (ticket, time.time(), identity),
        )
        return ticket
