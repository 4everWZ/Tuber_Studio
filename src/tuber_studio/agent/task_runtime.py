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

from ..http_api import InferenceBudgetExceeded, InferenceQueueBusy
from ..store import dump
from . import task_delegation
from .creative_workspace import CreativeWorkspace
from .task_delivery import LABELS, DelegationError, deliver_from_receipts
from .task_identity import TaskIdentity
from .task_video_scope import selection_plan, video_binding

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
    parallel_read: bool = False
    reusable: bool = False
    own_deadline: bool = False
    responses_only: bool = False
    accepts_goal: Callable[[str, BaseModel], bool] | None = None
    checkpoint_submission: bool = False


class TaskRuntime:
    """Persist tasks and their tool calls. Model callbacks bound execution after admission."""

    def __init__(self, store, scope, decide, make_plan=None):
        self.store, self.scope, self.decide = store, scope, decide
        self.make_plan = make_plan
        self.responses_client = lambda: None
        self.requires_responses = lambda: False
        self.edit_delivery = None
        self.read_parallelism = lambda: 2
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
        self.identity = scope if isinstance(scope, TaskIdentity) else None
        if self.identity:
            self.identity.install()
        self.workspace = CreativeWorkspace(store)

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
        if isinstance(task["plan"], list):
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
        if result["status"] in {"queued", "running", "waiting"}:
            for step in reversed(task["steps"]):
                tool = self.tools.get(step.get("tool"))
                if step.get("approved") and "result" not in step and tool and tool.checkpoint_submission:
                    checkpoint = step.get("checkpoint") or {}
                    if not self.can_control(task):
                        result.update(status="waiting", message="执行授权已变化，原提交待核对，不再发送。")
                    elif checkpoint.get("status") == "needs_reconciliation":
                        result.update(message=checkpoint["reason"] + "；原提交待核对。")
                    break
        if result["status"] == "completed" and not task_delegation.uses_responses(task):
            verified, result["message"] = self._deliver(task)
            if not verified:
                result["status"] = "failed"
        return result

    def can_control(self, task):
        if self.identity:
            return self.identity.grant_current(task["scope"])
        return self.scope() == task["scope"]

    def executable(self, identity):
        row = self.store.one("SELECT * FROM agent_tasks WHERE id=? AND scope=?", (identity, dump(self.scope())))
        return self._decode(row) if row else None

    def visible_scopes(self):
        if self.identity:
            return ("(SELECT scope FROM agent_task_scopes WHERE principal=? AND character_id=?)",
                    self.identity.reader())
        return "(?)", (dump(self.scope()),)

    def current(self, identity):
        scopes, params = self.visible_scopes()
        row = self.store.one(f"SELECT * FROM agent_tasks WHERE id=? AND scope IN {scopes}", (identity, *params))
        return self._decode(row) if row else None

    def activity(self):
        scopes, params = self.visible_scopes()
        rows = self.store.all(
            f"SELECT * FROM agent_tasks WHERE scope IN {scopes} "
            "ORDER BY updated DESC,id DESC LIMIT 50", params
        )
        return [{key: task[key] for key in ("id", "goal", "revision", "status", "due", "updated")}
                for task in (self.view(self._decode(row)) for row in rows)]

    def create(self, identity, goal, *, due=None, source_key=None, creative_goal_id=None, attachments=(), video_selection=None):
        scope, now = dump(self.scope()), time.time()
        if creative_goal_id:
            project = self.workspace.goal(creative_goal_id)
            if not project or not self.current(creative_goal_id) or project["closed"]:
                raise ValueError("长期目标已结束或不可访问")
        selection = selection_plan(video_selection, goal) if video_selection is not None else {}
        key = dump([source_key, goal, due] + ([selection["video_selection"]] if selection else []))
        inputs = [{"id": str(uuid5(UUID(identity), f"attachment:{index}")), "revision": 0,
                   "tool": "owner_attachment", "arguments": {}, "result": attachment}
                  for index, attachment in enumerate(attachments)]
        plan = dump({"backend": "responses", "prior_goals": [], **selection}) if inputs or selection else None
        due = now if due is None else due
        self.store.execute(
            "INSERT OR IGNORE INTO agent_tasks"
            "(id,scope,goal,revision,status,steps,message,source_key,due,created,updated,creative_goal_id,plan) "
            "VALUES (?,?,?,1,?,?,'',?,?,?,?,?,?)",
            (identity, scope, goal, "scheduled" if due > now else "queued", dump(inputs), key, due, now, now,
             creative_goal_id, plan),
        )
        task = self.executable(identity)
        if (not task or task["source_key"] != key or
                [step for step in task["steps"] if step["tool"] == "owner_attachment"] != inputs):
            raise ValueError("该请求编号已用于另一项任务")
        self.wake.set()
        return task

    async def close_goal(self, identity, confirmed):
        def stops_for(row):
            task = self._decode(row)
            if task["pending_control"]:
                return [task["steps"][-1]["id"]]
            return self._stop_ids(task)

        if not self.current(identity):
            raise LookupError(identity)
        stopped = self.workspace.stop_goal(identity, dump(self.scope()), confirmed, stops_for)
        if self.active and self.active[0] in stopped:
            self.active[1].cancel()
        self.wake.set()

    def _stop_ids(self, task):
        return [item["id"] for item in self._current_steps(task)
                if "result" not in item and task["status"] != "awaiting_approval"
                and (item["tool"] not in self.tools or self.tools[item["tool"]].stop
                     or (self.tools[item["tool"]].checkpoint_submission and item.get("checkpoint")))]

    async def control(self, identity, revision, action, goal=None, step_id=None, *, video_selection=None):
        async with self.controls:
            return await self._control(identity, revision, action, goal, step_id, video_selection)

    async def _control(self, identity, revision, action, goal, step_id, video_selection=None):
        task = self.current(identity)
        if not task:
            raise LookupError(identity)
        if task["creative_goal_id"] and self.workspace.goal(task["creative_goal_id"])["closed"]:
            raise ValueError("长期目标已结束，不能再改口、审批或执行；历史与成果仍可查看。")
        if self.scope() != task["scope"]:
            raise ValueError("原任务的执行授权已失效，历史仍可查看；请重新交办")
        if task["revision"] != revision:
            raise ValueError("任务已更新，请刷新后重试")
        status = task["status"]
        if task["pending_control"]:
            raise ValueError("关联操作的停止结果待核对")
        if video_selection is not None and action != "steer":
            raise ValueError("只有改口可以更换视频范围")
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
        pending = [item for item in self._current_steps(task) if "result" not in item]
        stops = self._stop_ids(task)
        final_status = "cancelled" if action == "cancel" else "queued"
        final_message = "任务已停止。" if action == "cancel" else ""
        due = time.time()
        if action == "steer" and status == "scheduled":
            final_status, due = "scheduled", task["due"]
        change = {"status": final_status, "message": final_message, "revision": revision + 1,
                  "goal": goal or task["goal"], "due": due, "plan": None, "plan_cursor": 0}
        if task_delegation.uses_responses(task):
            change["plan"] = {"backend": "responses", "prior_goals": [*task["plan"]["prior_goals"], task["goal"]],
                              "previous_delivery": task["plan"].get("previous_delivery")}
            if task["status"] == "completed":
                change["plan"]["previous_delivery"] = task["message"]
            change["plan"].update({key: task["plan"][key] for key in ("video_selection", "video_parts") if key in task["plan"]})
        if video_selection is not None:
            if change["plan"] is None:
                change["plan"] = {"backend": "responses", "prior_goals": [task["goal"]]}
            change["plan"].pop("video_parts", None)
            change["plan"].update(selection_plan(video_selection, change["goal"]))
        if change["plan"] and task_delegation.uses_responses(task) and video_binding(task) != video_binding(task | change):
            change["plan"].pop("previous_delivery", None)
        if stops:
            # Persist the control and original step together before interrupting its coroutine.
            change.update(status="waiting", message="正在核对关联操作的停止结果。",
                          pending_control=dump({"status": final_status, "message": final_message, "stops": stops}))
        elif pending:
            change["steps"] = [item for item in task["steps"] if item not in pending]
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
        control = json.loads(task["pending_control"])
        goal_stop = control.pop("goal_stop", False)
        stops = control.pop("stops", [task["steps"][-1]["id"]])
        steps = []
        try:
            for pending in task["steps"]:
                if "result" in pending:
                    steps.append(pending)
                elif pending["id"] in stops:
                    if self.scope() != task["scope"]:
                        return
                    tool = self.tools[pending["tool"]]
                    stop_context = (pending.get("checkpoint") or {}) | (pending.get("approval") or {})
                    async with asyncio.timeout(20):
                        receipt = await tool.stop(pending.get("resource_id", pending["id"]), stop_context)
                    if tool.checkpoint_submission and pending.get("checkpoint") and not receipt:
                        raise ValueError("原提交尚无可核实的停止回执")
                    if receipt is not None:
                        stopped = pending | {"result": receipt}
                        if tool.reusable and control["status"] == "queued" and receipt.get("status") == "cancelled":
                            stopped["paused_for_steer"] = True
                        steps.append(stopped)
                    elif goal_stop:
                        steps.append(pending)
                elif goal_stop:
                    steps.append(pending)
        except Exception as error:
            log.warning("Agent tool %s stop failed: %s", pending["tool"], type(error).__name__)
            self._save(task, message="关联操作的停止结果待核对。", due=time.time() + 30)
            return
        self._save(task, **control, steps=steps, pending_control=None, due=time.time())

    def _save(self, task, **changes):
        changes["updated"] = max(time.time(), math.nextafter(task["updated"], math.inf))
        if "steps" in changes:
            changes["steps"] = dump(changes["steps"])
        if changes.get("plan") is not None:
            changes["plan"] = dump(changes["plan"])
        query = "UPDATE agent_tasks SET " + ",".join(f"{key}=?" for key in changes)
        with self.store.db as database:
            applied = database.execute(
                query + " WHERE id=? AND revision=? AND updated=?",
                (*changes.values(), task["id"], task["revision"], task["updated"]),
            ).rowcount == 1
            if applied and changes.get("status") == "completed" and changes.get("message"):
                self.workspace.record_delivery(database, task, changes["message"])
            if applied and changes.get("status") in {"completed", "failed", "awaiting_approval", "needs_input"}:
                self.workspace.notify(database, task["id"], changes["updated"])
            elif applied and changes.get("status") and changes["status"] != task["status"]:
                database.execute("DELETE FROM creative_attention WHERE task_id=?", (task["id"],))
            return applied

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
            if followups := getattr(self, "followups", None):
                await followups.tick()
            if events := getattr(self, "goal_events", None):
                events.tick()
            async with self.controls:
                row = self.store.one(
                    "SELECT * FROM agent_tasks WHERE scope=? AND status IN ('queued','running','waiting','scheduled') "
                    "AND due<=? AND (creative_goal_id IS NULL OR NOT EXISTS "
                    "(SELECT 1 FROM creative_goals WHERE id=agent_tasks.creative_goal_id AND closed=1) "
                    "OR json_extract(pending_control,'$.goal_stop')=1) ORDER BY due,created LIMIT 1",
                    (dump(self.scope()), time.time()),
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
            fresh = self.executable(task["id"])
            if fresh and fresh["revision"] == task["revision"]:
                self._save(fresh, status="waiting", due=time.time() + busy.retry_after,
                           message="等待推理资源，已保存任务进度。")
        except Exception as error:
            log.warning("Agent task %s failed: %s", task["id"], type(error).__name__)
            fresh = self.executable(task["id"])
            if fresh and fresh["revision"] == task["revision"]:
                _, delivery = self._deliver(fresh)
                reason = "后台请求超出服务预算。" if isinstance(error, InferenceBudgetExceeded) else "任务未完成。"
                if isinstance(error, DelegationError):
                    reason = str(error)
                elif task_delegation.uses_responses(fresh) and "candidate" in fresh["plan"]:
                    reason = "前台未能完成选材；已保留候选材料，尚未交付。"
                self._save(fresh, status="failed", message=reason + "\n\n" + delivery)

    async def _prepare_plan(self, task, tools):
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
        if task_delegation.uses_responses(task):
            await task_delegation.advance(self, task)
            return
        if task["plan"] is None and not steps and (self.requires_responses() or self.responses_client() is not None):
            self._save(task, plan={"backend": "responses", "prior_goals": []}, status="queued", due=time.time())
            return
        step = steps[-1] if steps and "result" not in steps[-1] else None
        if step is None:
            tools = {name: {"description": tool.description, "arguments": tool.arguments.model_json_schema(),
                            "requires_approval": tool.review is not None}
                     for name, tool in self.tools.items() if not tool.responses_only}
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

        step, status, due = await self._invoke(task, step, self._current_steps(task), task["goal"])
        if self.scope() == task["scope"]:
            changes = {"plan_cursor": task["plan_cursor"] + 1} if "result" in step and task["plan"] is not None else {}
            self._save(task, status=status, steps=[*task["steps"][:-1], step], due=due, **changes)

    async def _invoke(self, task, step, context, goal):
        tool = self.tools[step["tool"]]
        try:
            arguments = tool.arguments.model_validate(step["arguments"])
            if tool.accepts_goal and not tool.accepts_goal(task["goal"], arguments):
                return step | {"result": {"error": "本轮目标没有明确指定该草稿 ID。"}}, "queued", time.time()
            if tool.review and not step.get("approved"):
                step = step | {"approval": tool.review(arguments)}
                return step, "awaiting_approval", time.time()
            receipts = tuple(item for item in context if "result" in item)
            request = ToolRequest(step.get("resource_id", step["id"]), goal, task["scope"], step.get("checkpoint"),
                                  step.get("approval"), receipts)
            if tool.checkpoint_submission and not step.get("checkpoint"):
                if not step.get("approved") or not request.approval:
                    raise ValueError("没有当前步骤的明确审批")
                step = step | {"checkpoint": {"submission_started": True}}
                if not self._save(task, steps=[step if item["id"] == step["id"] else item for item in task["steps"]]):
                    return step, "waiting", time.time()
            async with asyncio.timeout(None if tool.own_deadline else 20):
                result = await tool.run(request, arguments)
        except InferenceQueueBusy as busy:
            return step, "waiting", time.time() + busy.retry_after
        except InferenceBudgetExceeded:
            result = {"error": "后台请求超出服务预算。"}
        except Exception as error:
            log.info("Agent tool %s failed: %s", step["tool"], type(error).__name__)
            if tool.checkpoint_submission and step.get("checkpoint"):
                result = ToolWait(step["checkpoint"] | {"status": "needs_reconciliation",
                                                        "reason": "无法确认原提交结果，不再发送"}, 60)
            else:
                result = {"error": "工具调用失败，参数无效或数据暂时不可用。"}
        if isinstance(result, ToolWait):
            return step | {"checkpoint": result.checkpoint}, "waiting", time.time() + result.delay
        return {key: value for key, value in step.items() if key != "checkpoint"} | {"result": result}, "queued", time.time()
