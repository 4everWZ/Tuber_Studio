import asyncio
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tuber_studio.agent.task_api import register_task_api
from tuber_studio.agent.task_runtime import TaskDecision, TaskRuntime, Tool, ToolWait
from tuber_studio.models import StrictModel
from tuber_studio.store import Store


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
            assert steps[-1]["result"] == {"status": "cancelled"}
            return TaskDecision(action="finish", message="已按新要求核对。")
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
    runtime.register("read", Tool("Read fixture", StrictModel, run, recovered_stop))
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
    register_task_api(app, runtime, lambda: None)
    identity = str(uuid4())
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            body = {"request_id": identity, "goal": "旧要求"}
            created = await client.post("/api/agent/tasks", json=body)
            assert created.status_code == 200
            retried = await client.post("/api/agent/tasks", json=body)
            assert retried.json() == created.json()
            conflict = await client.post("/api/agent/tasks", json=body | {"goal": "新要求"})
            assert conflict.status_code == 409
            scope = ["other"]
            conflict = await client.post("/api/agent/tasks", json=body)
            assert conflict.status_code == 409 and "旧要求" not in conflict.text
    finally:
        store.close()


async def test_steering_a_scheduled_task_preserves_due_time(tmp_path):
    store = Store(tmp_path / "tasks.sqlite3", [])
    started = []

    async def decide(goal, steps, tools):
        started.append(goal)
        return TaskDecision(action="finish", message="完成。")

    runtime = TaskRuntime(store, lambda: ["owner"], decide)
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
