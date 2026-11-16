"""Durable model/tool loop. Applications register capabilities and own their side effects."""

import asyncio
import json
import logging
import math
import operator
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from ..http_api import InferenceQueueBusy
from ..store import dump
from .task_delivery import LABELS, deliver_from_receipts

MAX_STEPS = 8
log = logging.getLogger(__name__)
_TOOL_NAME = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", re.ASCII)


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["tool", "finish", "ask"]
    tool: str = ""
    arguments: dict = Field(default_factory=dict)
    message: str = Field(default="", max_length=12000)


class ReceiptCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    path: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*$", max_length=128)
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "exists", "missing"]
    value: str | int | float | bool | None = None

    def evaluate(self, steps):
        result = next((step["result"] for step in reversed(steps)
                       if step["tool"] == self.tool and "result" in step), None)
        if result is None:
            return None
        for key in self.path.split("."):
            if not isinstance(result, dict) or key not in result:
                return True if self.operator == "missing" else False if self.operator == "exists" else None
            result = result[key]
        if self.operator in {"exists", "missing"}:
            return self.operator == "exists"
        if self.operator in {"eq", "ne"}:
            equal = type(result) is type(self.value) and result == self.value
            return equal if self.operator == "eq" else not equal
        if type(result) not in {int, float} or type(self.value) not in {int, float}:
            return None
        return {"gt": operator.gt, "ge": operator.ge, "lt": operator.lt, "le": operator.le}[self.operator](
            result, self.value,
        )


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    condition: ReceiptCondition | None = None


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlannedStep] = Field(max_length=MAX_STEPS)
    question: str = Field(default="", max_length=1000)


@dataclass(frozen=True)
class ToolRequest:
    id: str
    goal: str
    scope: list
    checkpoint: dict | None
    approval: dict | None
    receipts: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ToolWait:
    checkpoint: dict
    delay: float


@dataclass(frozen=True)
class Tool:
    description: str
    arguments: type[BaseModel]
    run: Callable[[ToolRequest, BaseModel], Awaitable[dict | ToolWait]]
    # Stops can be replayed after interruption. Look up the same domain operation by its step ID.
    stop: Callable[[str, dict], Awaitable[dict | None]] | None = None
    review: Callable[[BaseModel], dict] | None = None


class TaskRuntime:
    def __init__(self, store, scope, decide, make_plan=None):
        self.store, self.scope, self.decide = store, scope, decide
        self.make_plan = make_plan
        self.tools: dict[str, Tool] = {}
        self.wake = asyncio.Event()
        self.controls = asyncio.Lock()
        self.active = None
        self.worker = None
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_tasks(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, goal TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, steps TEXT NOT NULL, message TEXT NOT NULL, source_key TEXT,
                due REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, pending_control TEXT,
                plan TEXT, plan_cursor INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS agent_tasks_activity ON agent_tasks(scope,updated);
            CREATE INDEX IF NOT EXISTS agent_tasks_ready ON agent_tasks(scope,status,due);
        """)
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(agent_tasks)")}
        if "pending_control" not in columns:
            self.store.execute("ALTER TABLE agent_tasks ADD COLUMN pending_control TEXT")
        if "plan" not in columns:
            self.store.execute("ALTER TABLE agent_tasks ADD COLUMN plan TEXT")
        if "plan_cursor" not in columns:
            self.store.execute("ALTER TABLE agent_tasks ADD COLUMN plan_cursor INTEGER NOT NULL DEFAULT 0")

    def register(self, name: str, tool: Tool):
        if name in self.tools:
            raise ValueError(f"duplicate agent tool: {name}")
        self.tools[name] = tool

    @staticmethod
    def _decode(row):
        return row | {"scope": json.loads(row["scope"]), "steps": json.loads(row["steps"]),
                      "plan": json.loads(row["plan"]) if row["plan"] is not None else None}

    def _deliver(self, task):
        required = {name for name in _TOOL_NAME.findall(task["goal"])
                    if name in self.tools and self.tools[name].review is None}
        if task["plan"] is not None:
            skipped = {item["tool"] for item in task["plan"][:task["plan_cursor"]]
                       if item.get("skipped")}
            required -= skipped
        return deliver_from_receipts(
            task["steps"], task["revision"],
            required_tools=required,
            reviewed_tools={name for name, tool in self.tools.items() if tool.review is not None},
        )

    @staticmethod
    def _current_steps(task):
        return [step for step in task["steps"] if step["revision"] == task["revision"]]

    def view(self, task):
        result = {key: task[key] for key in ("id", "goal", "revision", "status", "steps", "message", "due", "updated")}
        if result["status"] == "completed":
            verified, result["message"] = self._deliver(task)
            if not verified:
                result["status"] = "failed"
        return result

    def current(self, identity):
        row = self.store.one("SELECT * FROM agent_tasks WHERE id=? AND scope=?", (identity, dump(self.scope())))
        return self._decode(row) if row else None

    def activity(self):
        rows = self.store.all(
            "SELECT * FROM agent_tasks WHERE scope=? "
            "ORDER BY updated DESC,id DESC LIMIT 50", (dump(self.scope()),)
        )
        return [{key: task[key] for key in ("id", "goal", "revision", "status", "due", "updated")}
                for task in (self.view(self._decode(row)) for row in rows)]

    def create(self, identity, goal, *, due=None, source_key=None):
        scope, now = dump(self.scope()), time.time()
        key = dump([source_key, goal, due])
        due = now if due is None else due
        self.store.execute(
            "INSERT OR IGNORE INTO agent_tasks"
            "(id,scope,goal,revision,status,steps,message,source_key,due,created,updated) "
            "VALUES (?,?,?,1,?,'[]','',?,?,?,?)",
            (identity, scope, goal, "scheduled" if due > now else "queued", key, due, now, now),
        )
        task = self.current(identity)
        if not task or task["source_key"] != key:
            raise ValueError("该请求编号已用于另一项任务")
        self.wake.set()
        return task

    async def control(self, identity, revision, action, goal=None, step_id=None):
        async with self.controls:
            return await self._control(identity, revision, action, goal, step_id)

    async def _control(self, identity, revision, action, goal, step_id):
        task = self.current(identity)
        if not task:
            raise LookupError(identity)
        if task["revision"] != revision:
            raise ValueError("任务已更新，请刷新后重试")
        status = task["status"]
        if task["pending_control"]:
            raise ValueError("关联操作的停止结果待核对")
        if action == "approve":
            step = task["steps"][-1] if task["steps"] else None
            if (status != "awaiting_approval" or not step or step["id"] != step_id
                    or step["revision"] != revision or "approval" not in step):
                raise ValueError("待确认步骤已变化")
            step = step | {"approved": True}
            if not self._save(task, status="queued", steps=[*task["steps"][:-1], step], due=time.time()):
                raise ValueError("待确认步骤已变化")
            self.wake.set()
            return self.current(identity)
        allowed = {
            "cancel": {"queued", "scheduled", "running", "waiting", "needs_input", "awaiting_approval"},
            "steer": {"queued", "scheduled", "running", "waiting", "needs_input", "awaiting_approval",
                      "failed", "completed", "cancelled"},
        }
        if status not in allowed[action]:
            raise ValueError("任务当前无法执行此操作")
        pending = task["steps"][-1] if task["steps"] and "result" not in task["steps"][-1] else None
        tool = self.tools.get(pending["tool"]) if pending else None
        final_status = "cancelled" if action == "cancel" else "queued"
        final_message = "任务已停止。" if action == "cancel" else ""
        due = time.time()
        if action == "steer" and status == "scheduled":
            final_status, due = "scheduled", task["due"]
        change = {"status": final_status, "message": final_message, "revision": revision + 1,
                  "goal": goal or task["goal"], "due": due, "plan": None, "plan_cursor": 0}
        if tool and tool.stop and status != "awaiting_approval":
            # Persist the control and original step together before interrupting its coroutine.
            change.update(status="waiting", message="正在核对关联操作的停止结果。",
                          pending_control=dump({"status": final_status, "message": final_message}))
        elif pending:
            change["steps"] = task["steps"][:-1]
        updated = self._save(task, **change)
        if not updated:
            raise ValueError("任务已更新，请刷新后重试")
        self.wake.set()
        if self.active and self.active[0] == identity:
            running = self.active[1]
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if "pending_control" in change and (fresh := self.current(identity)):
            await self._finish_control(fresh)
        fresh = self.current(identity)
        if not fresh:
            raise LookupError(identity)
        return fresh

    async def _finish_control(self, task):
        if self.scope() != task["scope"]:
            return
        pending = task["steps"][-1]
        try:
            tool = self.tools[pending["tool"]]
            if not tool.stop:
                raise ValueError("No stop handler is registered")
            stop_context = (pending.get("checkpoint") or {}) | (pending.get("approval") or {})
            async with asyncio.timeout(20):
                # The step ID also locates submissions made before their checkpoint was written.
                receipt = await tool.stop(pending["id"], stop_context)
        except Exception as error:
            log.warning("Agent tool %s stop failed: %s", pending["tool"], type(error).__name__)
            self._save(task, message="关联操作的停止结果待核对。", due=time.time() + 30)
            return
        steps = task["steps"][:-1]
        if receipt is not None:
            steps = [*steps, pending | {"result": receipt}]
        self._save(task, **json.loads(task["pending_control"]), steps=steps, pending_control=None, due=time.time())

    def _save(self, task, **changes):
        changes["updated"] = max(time.time(), math.nextafter(task["updated"], math.inf))
        if "steps" in changes:
            changes["steps"] = dump(changes["steps"])
        if changes.get("plan") is not None:
            changes["plan"] = dump(changes["plan"])
        query = "UPDATE agent_tasks SET " + ",".join(f"{key}=?" for key in changes)
        return self.store.execute(
            query + " WHERE id=? AND revision=? AND updated=?",
            (*changes.values(), task["id"], task["revision"], task["updated"]),
        ).rowcount == 1

    def start(self):
        self.worker = asyncio.create_task(self._work())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    async def _work(self):
        while True:
            self.wake.clear()
            async with self.controls:
                row = self.store.one(
                    "SELECT * FROM agent_tasks WHERE scope=? AND status IN ('queued','running','waiting','scheduled') "
                    "AND due<=? ORDER BY due,created LIMIT 1", (dump(self.scope()), time.time()),
                )
                if row:
                    task = asyncio.create_task(self._advance(self._decode(row)))
                    self.active = row["id"], task
            if row:
                try:
                    await asyncio.gather(task, return_exceptions=True)
                finally:
                    self.active = None
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except TimeoutError:
                pass

    async def _advance(self, task):
        try:
            if task["pending_control"]:
                await self._finish_control(task)
            else:
                await self._step(task)
        except InferenceQueueBusy as busy:
            fresh = self.current(task["id"])
            if fresh and fresh["revision"] == task["revision"]:
                self._save(fresh, status="waiting", due=time.time() + busy.retry_after,
                           message="等待推理资源，已保存任务进度。")
        except Exception as error:
            log.warning("Agent task %s failed: %s", task["id"], type(error).__name__)
            fresh = self.current(task["id"])
            if fresh and fresh["revision"] == task["revision"]:
                _, delivery = self._deliver(fresh)
                self._save(fresh, status="failed", message="任务未完成。\n\n" + delivery)

    async def _prepare_plan(self, task, tools):
        async with asyncio.timeout(120):
            plan = await self.make_plan(task["goal"], tools)
        if self.scope() != task["scope"]:
            return
        if not plan.steps:
            if plan.question.strip():
                self._ask(task)
                return
            raise ValueError("The task plan has no steps or clarification")
        scheduled = set()
        for item in plan.steps:
            if item.tool not in tools or (item.condition and item.condition.tool not in scheduled):
                raise ValueError("The task plan has an unavailable tool or forward dependency")
            scheduled.add(item.tool)
        self._save(task, status="queued", plan=[item.model_dump() for item in plan.steps], due=time.time())

    def _ask(self, task, planned=None):
        question = {
            "web_search": "请补充要检索的主题。",
            "web_page": "请提供要读取的网页链接。",
            "inspect_video": "请提供要取材的视频或 BV 号。",
        }.get(planned, f"请补充「{LABELS[planned]}」所需的条件。" if planned in LABELS else "请补充任务目标或来源。")
        if self._current_steps(task):
            _, delivery = self._deliver(task)
            question += "\n\n" + delivery
        self._save(task, status="needs_input", message=question)

    def _next_planned_step(self, task, current_steps):
        cursor = task["plan_cursor"]
        if cursor == len(task["plan"]):
            verified, delivery = self._deliver(task)
            self._save(task, status="completed" if verified else "failed", message=delivery)
            return None
        item = PlannedStep.model_validate(task["plan"][cursor])
        if item.condition:
            condition = item.condition.evaluate(current_steps)
            if condition is None:
                _, delivery = self._deliver(task)
                self._save(task, status="failed", message="缺少核对后续步骤条件的回执。\n\n" + delivery)
                return None
            if condition is False:
                plan = task["plan"].copy()
                plan[cursor] = plan[cursor] | {"skipped": True}
                self._save(task, status="queued", plan=plan, plan_cursor=cursor + 1, due=time.time())
                return None
        return item.tool

    async def _step(self, task):
        if self.scope() != task["scope"] or not self._save(task, status="running"):
            return
        task = self.current(task["id"])
        if not task:
            return
        steps = task["steps"]
        step = steps[-1] if steps and "result" not in steps[-1] else None
        if step is None:
            tools = {name: {"description": tool.description, "arguments": tool.arguments.model_json_schema(),
                            "requires_approval": tool.review is not None}
                     for name, tool in self.tools.items()}
            current_steps = self._current_steps(task)
            used = len(current_steps)
            planned = None
            if task["plan"] is None and self.make_plan and not current_steps:
                await self._prepare_plan(task, tools)
                return
            if task["plan"] is not None:
                planned = self._next_planned_step(task, current_steps)
                if planned is None:
                    return
                tools = {planned: tools[planned]}
            if planned and not self.tools[planned].arguments.model_fields and self.tools[planned].review is None:
                decision = TaskDecision(action="tool", tool=planned)
            else:
                async with asyncio.timeout(120):
                    decision = await self.decide(task["goal"], current_steps[-MAX_STEPS:], tools if used < MAX_STEPS else {})
            if self.scope() != task["scope"]:
                return
            if decision.action == "finish" and planned is None:
                verified, delivery = self._deliver(task)
                self._save(task, status="completed" if verified else "failed", message=delivery)
                return
            if decision.action == "ask" and decision.message.strip():
                self._ask(task, planned)
                return
            if decision.action != "tool" or decision.tool not in tools or used >= MAX_STEPS:
                raise ValueError("invalid agent step")
            step = {"id": str(uuid5(UUID(task["id"]), f"{task['revision']}:{used}")),
                    "tool": decision.tool, "arguments": decision.arguments, "revision": task["revision"]}
            if not self._save(task, steps=[*steps, step]):
                return
            task = self.current(task["id"])

        tool = self.tools[step["tool"]]
        try:
            arguments = tool.arguments.model_validate(step["arguments"])
            if tool.review and not step.get("approved"):
                step = step | {"approval": tool.review(arguments)}
                self._save(task, status="awaiting_approval", steps=[*task["steps"][:-1], step])
                return
            receipts = tuple(item for item in task["steps"] if item["revision"] == task["revision"] and "result" in item)
            request = ToolRequest(step["id"], task["goal"], task["scope"], step.get("checkpoint"),
                                  step.get("approval"), receipts)
            async with asyncio.timeout(20):
                result = await tool.run(request, arguments)
        except Exception as error:
            log.info("Agent tool %s failed: %s", step["tool"], type(error).__name__)
            result = {"error": "工具调用失败，参数无效或数据暂时不可用。"}
        if self.scope() != task["scope"]:
            return
        if isinstance(result, ToolWait):
            step = step | {"checkpoint": result.checkpoint}
            self._save(task, status="waiting", steps=[*task["steps"][:-1], step], due=time.time() + result.delay)
        else:
            step = {key: value for key, value in step.items() if key != "checkpoint"} | {"result": result}
            changes = {"plan_cursor": task["plan_cursor"] + 1} if task["plan"] is not None else {}
            self._save(task, status="queued", steps=[*task["steps"][:-1], step], due=time.time(), **changes)
