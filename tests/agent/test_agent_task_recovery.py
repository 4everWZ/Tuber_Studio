import asyncio
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tuber_studio.agent.tasks.runtime import (
    PlannedStep,
    ReceiptCondition,
    TaskDecision,
    TaskPlan,
    TaskRuntime,
    Tool,
    ToolWait,
)
from tuber_studio.app.routes.agent_tasks import register_task_routes
from tuber_studio.core.models import StrictModel
from tuber_studio.core.store import Store
from tuber_studio.infra.inference.http_api import response_json


async def wait_status(runtime, identity, status):
    async with asyncio.timeout(5):
        while True:
            task = runtime.current(identity)
            if task["status"] == status:
                return task
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(("action", "target"), [("cancel", "cancelled"), ("steer", "completed")])
async def test_control_retries_interrupted_stop_before_resuming(tmp_path, action, target):
    path = tmp_path / "tasks.sqlite3"
    identity = str(uuid4())
    stop_started = asyncio.Event()
    submitted = []
    stopped = []

    async def decide(goal, steps, tools):
        if goal == "新要求":
            if not steps:
                return TaskDecision(action="tool", tool="read")
            assert steps[-1]["result"] == {"confirmed": True}
            return TaskDecision(action="finish")
        return TaskDecision(action="tool", tool="read")

    async def run(request, args):
        submitted.append(request.id)
        return ToolWait({"remote_id": "task-1"}, delay=180)

    async def interrupted_stop(step, checkpoint):
        stop_started.set()
        await asyncio.Event().wait()

    async def recovered_stop(step, checkpoint):
        stopped.append((step, checkpoint))
        return {"status": "cancelled"}

    async def fresh_read(request, args):
        assert request.goal == "新要求"
        return {"confirmed": True}

    store = Store(path, [])
    runtime = TaskRuntime(store, lambda: ["owner"], decide)
    runtime.register("read", Tool("Read fixture", StrictModel, run, interrupted_stop))
    runtime.start()
    request = None
    try:
        runtime.create(identity, "旧要求")
        pending = await wait_status(runtime, identity, "waiting")
        request = asyncio.create_task(runtime.control(identity, 1, action, "新要求" if action == "steer" else None))
        async with asyncio.timeout(5):
            await stop_started.wait()
    finally:
        await runtime.close()
        if request:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        store.close()

    store = Store(path, [])
    runtime = TaskRuntime(store, lambda: ["owner"], decide)
    runtime.register("read", Tool("Read fixture", StrictModel, fresh_read, recovered_stop))
    runtime.start()
    try:
        result = await wait_status(runtime, identity, target)
        step_id = pending["steps"][-1]["id"]
        assert submitted == [step_id]
        assert stopped == [(step_id, {"remote_id": "task-1"})]
        assert result["revision"] == 2 and result["pending_control"] is None
    finally:
        await runtime.close()
        store.close()


async def test_create_conflict_returns_http_conflict_without_exposing_previous_task(tmp_path):
    store = Store(tmp_path / "tasks.sqlite3", [])
    app = FastAPI()
    scope = ["owner"]
    runtime = TaskRuntime(store, lambda: scope, None)
    register_task_routes(app, runtime, lambda: None)
    identity = str(uuid4())
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            body = {"request_id": identity, "goal": "旧要求"}
            created = await client.post("/api/v1/agent/tasks", json=body)
            assert created.status_code == 200
            retried = await client.post("/api/v1/agent/tasks", json=body)
            assert retried.json() == created.json()
            conflict = await client.post("/api/v1/agent/tasks", json=body | {"goal": "新要求"})
            assert conflict.status_code == 409
            scope = ["other"]
            conflict = await client.post("/api/v1/agent/tasks", json=body)
            assert conflict.status_code == 409 and "旧要求" not in conflict.text
    finally:
        store.close()


@pytest.mark.parametrize("unwatered", [0, 3])
async def test_saved_plan_resumes_its_condition_and_cannot_select_an_unplanned_tool(tmp_path, unwatered):
    path = tmp_path / "saved-plan.sqlite3"
    identity = str(uuid4())
    prepared = asyncio.Event()
    calls = []

    class GameTarget(StrictModel):
        game: str

    async def plan(goal, tools):
        calls.append("plan")
        return TaskPlan(steps=[PlannedStep(tool="game_status"), PlannedStep(
            tool="game_action", condition=ReceiptCondition(tool="game_status", path="state.unwatered_crops",
                                                            operator="gt", value=0),
        )])

    async def decide(goal, steps, tools):
        if not steps:
            assert list(tools) == ["game_status"]
            prepared.set()
            await asyncio.Event().wait()
        assert list(tools) == ["game_action"]
        return TaskDecision(action="tool", tool="unplanned")

    async def run(request, args):
        calls.append("read")
        return {"simulated": True, "state": {"summary": "真实模拟状态", "unwatered_crops": unwatered}}

    async def act(request, args):
        calls.append("write")
        return {"success": True}

    def open_runtime(decider):
        store = Store(path, [])
        task = TaskRuntime(store, lambda: ["owner"], decider, plan)
        task.register("game_status", Tool("Read simulated state", GameTarget, run))
        task.register("game_action", Tool("Review action", StrictModel, act, review=lambda args: {"target": "模拟"}))
        task.register("unplanned", Tool("Must never be executed", StrictModel, act))
        return store, task

    store, task = open_runtime(decide)
    task.start()
    try:
        task.create(identity, "先核对待浇水数量，有需要再准备动作。")
        async with asyncio.timeout(5):
            await prepared.wait()
        assert task.current(identity)["plan_cursor"] == 0
    finally:
        await task.close()
        store.close()

    async def decide_after_restart(goal, steps, tools):
        if not steps:
            return TaskDecision(action="tool", tool="game_status", arguments={"game": "stardew_valley"})
        return await decide(goal, steps, tools)

    store, task = open_runtime(decide_after_restart)
    task.start()
    try:
        result = await wait_status(task, identity, "completed" if not unwatered else "failed")
        assert calls == ["plan", "read"]
        assert "真实模拟状态" in result["message"]
        if not unwatered:
            assert result["plan"][1]["skipped"] is True
        else:
            assert result["plan_cursor"] == 1
    finally:
        await task.close()
        store.close()


async def test_agent_waits_after_gateway_rejects_planning_before_admission(tmp_path):
    attempts, executed = [], []

    def reply(request):
        attempts.append(request.url.host)
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"code": "gateway_busy"}})

    async def plan(goal, tools):
        if not attempts:
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url="http://omni-gateway:8000") as client:
                await response_json(client, "POST", "/v1/chat/completions", payload={})
        return TaskPlan(steps=[PlannedStep(tool="operation_status")])

    async def read(request, args):
        executed.append(request.id)
        return {"state": "off"}

    store = Store(tmp_path / "busy.sqlite3", [])
    task = TaskRuntime(store, lambda: ["owner"], None, plan)
    task.register("operation_status", Tool("Read operations", StrictModel, read))
    task.start()
    identity = str(uuid4())
    try:
        task.create(identity, "请查询运营状态。")
        waiting = await wait_status(task, identity, "waiting")
        assert not waiting["steps"] and not executed and waiting["revision"] == 1
        completed = await wait_status(task, identity, "completed")
        assert attempts == ["omni-gateway"] and len(executed) == 1
        assert completed["message"] == "**自主运营**\n- 未启动"
    finally:
        await task.close()
        store.close()


async def test_steering_a_scheduled_task_preserves_due_time(tmp_path):
    store = Store(tmp_path / "tasks.sqlite3", [])
    started = []

    async def decide(goal, steps, tools):
        if steps:
            return TaskDecision(action="finish")
        started.append(goal)
        return TaskDecision(action="tool", tool="read")

    async def read(request, args):
        return {"confirmed": True}

    runtime = TaskRuntime(store, lambda: ["owner"], decide)
    runtime.register("read", Tool("Read fixture", StrictModel, read))
    scheduled, immediate = str(uuid4()), str(uuid4())
    due = time.time() + 60
    runtime.start()
    try:
        runtime.create(scheduled, "预约目标", due=due)
        updated = await runtime.control(scheduled, 1, "steer", "修订后的预约目标")
        runtime.create(immediate, "立即执行")
        await wait_status(runtime, immediate, "completed")
        assert updated["status"] == "scheduled" and updated["due"] == due
        assert runtime.current(scheduled)["revision"] == 2
        assert started == ["立即执行"]
    finally:
        await runtime.close()
        store.close()
