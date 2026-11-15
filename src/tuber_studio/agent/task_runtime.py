"""Durable model/tool loop. Applications register capabilities and own their side effects."""

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from ..store import dump

MAX_STEPS = 8
log = logging.getLogger(__name__)


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["tool", "finish", "ask"]
    tool: str = ""
    arguments: dict = Field(default_factory=dict)
    message: str = Field(default="", max_length=12000)


@dataclass(frozen=True)
class ToolRequest:
    id: str
    goal: str
    scope: list
    checkpoint: dict | None
    approval: dict | None


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
    def __init__(self, store, scope, decide):
        self.store, self.scope, self.decide = store, scope, decide
        self.tools: dict[str, Tool] = {}
        self.wake = asyncio.Event()
        self.controls = asyncio.Lock()
        self.active = None
        self.worker = None
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_tasks(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, goal TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, steps TEXT NOT NULL, message TEXT NOT NULL, source_key TEXT,
                due REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, pending_control TEXT);
            CREATE INDEX IF NOT EXISTS agent_tasks_activity ON agent_tasks(scope,updated);
            CREATE INDEX IF NOT EXISTS agent_tasks_ready ON agent_tasks(scope,status,due);
        """)
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(agent_tasks)")}
        if "pending_control" not in columns:
            self.store.execute("ALTER TABLE agent_tasks ADD COLUMN pending_control TEXT")

    def register(self, name: str, tool: Tool):
        if name in self.tools:
            raise ValueError(f"duplicate agent tool: {name}")
        self.tools[name] = tool

    @staticmethod
    def _decode(row):
        return row | {"scope": json.loads(row["scope"]), "steps": json.loads(row["steps"])}

    @staticmethod
    def view(task):
        return {key: task[key] for key in ("id", "goal", "revision", "status", "steps", "message", "due", "updated")}

    def current(self, identity):
        row = self.store.one("SELECT * FROM agent_tasks WHERE id=? AND scope=?", (identity, dump(self.scope())))
        return self._decode(row) if row else None

    def activity(self):
        return self.store.all(
            "SELECT id,goal,revision,status,due,updated FROM agent_tasks WHERE scope=? "
            "ORDER BY updated DESC,id DESC LIMIT 50", (dump(self.scope()),)
        )

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
                  "goal": goal or task["goal"], "due": due}
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
        except Exception as error:
            log.warning("Agent task %s failed: %s", task["id"], type(error).__name__)
            fresh = self.current(task["id"])
            if fresh and fresh["revision"] == task["revision"]:
                self._save(fresh, status="failed", message="任务未完成，请查看步骤或补充要求后继续。")

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
            used = sum(item["revision"] == task["revision"] for item in steps)
            async with asyncio.timeout(120):
                decision = await self.decide(task["goal"], steps[-MAX_STEPS:], tools if used < MAX_STEPS else {})
            if self.scope() != task["scope"]:
                return
            if decision.action in {"finish", "ask"} and decision.message.strip():
                status = "completed" if decision.action == "finish" else "needs_input"
                self._save(task, status=status, message=decision.message.strip())
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
            request = ToolRequest(step["id"], task["goal"], task["scope"], step.get("checkpoint"), step.get("approval"))
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
            self._save(task, status="queued", steps=[*task["steps"][:-1], step], due=time.time())
