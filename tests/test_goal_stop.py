import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tuber_studio.agent.task_api import register_task_api
from tuber_studio.agent.task_runtime import TaskRuntime, Tool
from tuber_studio.models import StrictModel
from tuber_studio.store import Store


def new(tasks, title, root=None):
    return tasks.create(str(uuid4()), title, creative_goal_id=root)["id"]


async def test_confirmed_goal_stop_closes_admission_and_preserves_other_scopes_and_artifacts(tmp_path):
    store = Store(tmp_path / "studio.sqlite", [])
    scope = ["owner", "whale"]
    tasks = TaskRuntime(store, lambda: scope.copy(), None)
    root = new(tasks, "直播提纲")
    tasks.workspace.promote(tasks.current(root))
    app = FastAPI()
    register_task_api(app, tasks, lambda: None)
    path = f"/api/agent/tasks/{root}"
    delivered = new(tasks, "已交付", root)
    tasks._save(tasks.current(delivered), status="completed", message="原版提纲")
    tasks.workspace.edit(tasks.current(delivered), 1, 1, "号主修订版")
    foreign = new(tasks, "旧授权", root)
    store.execute("UPDATE agent_tasks SET scope=? WHERE id=?", (json.dumps(["other"]), foreign))
    unrelated = new(tasks, "另一个目标")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.get(path)).json()["creative_goal"]
        assert project["stop_scope"] == {root: 1}
        child = new(tasks, "明天取材", root)
        tasks._save(tasks.current(child), status="scheduled", due=time.time() + 86400)
        close = path + "/goal/close"
        for body in (None, {"confirmed_runs": project["stop_scope"]}):
            assert (await client.post(close, json=body)).status_code == 409
            assert not tasks.workspace.goal(root)["closed"]
        scope_now = (await client.get(path)).json()["creative_goal"]["stop_scope"]
        assert scope_now == {root: 1, child: 1}
        for _ in range(2):
            result = await client.post(close, json={"confirmed_runs": scope_now})
            assert result.status_code == 200
            assert result.json()["creative_goal"]["closed"] and not result.json()["can_control"]
        for identity in (root, child, delivered):
            task = tasks.current(identity)
            result = await client.patch(f"/api/agent/tasks/{identity}", json={
                "action": "steer", "revision": task["revision"], "goal": "继续",
            })
            assert result.status_code == 409
    assert [tasks.current(identity)["status"] for identity in (root, child)] == ["cancelled", "cancelled"]
    assert tasks.current(unrelated)["status"] == "queued"
    assert store.one("SELECT revision,status FROM agent_tasks WHERE id=?", (foreign,)) == {"revision": 1, "status": "queued"}
    assert tasks.workspace.latest(delivered)["body"] == "号主修订版"
    with pytest.raises(ValueError, match="目标已结束"):
        new(tasks, "不再准入", root)
    store.close()


@pytest.mark.parametrize("missing_receipt", [False, True])
async def test_restart_finishes_goal_stop_by_original_submission_without_running_tool(tmp_path, missing_receipt):
    path = tmp_path / "studio.sqlite"
    store = Store(path, [])
    def scope():
        return ["owner", "whale"]

    tasks = TaskRuntime(store, scope, None)
    root = new(tasks, "限定发布")
    tasks.workspace.promote(tasks.current(root))
    step = {"id": str(uuid4()), "revision": 1, "tool": "publish", "approved": True,
            "arguments": {}, "approval": {"target": "draft-1"}, "checkpoint": {"submission_started": True}}
    tasks._save(tasks.current(root), status="waiting", steps=[step])
    active = asyncio.create_task(asyncio.sleep(3600))
    tasks.active = root, active
    await tasks.close_goal(root, {root: 1})
    await asyncio.gather(active, return_exceptions=True)
    assert active.cancelled()
    store.close()

    store = Store(path, [])
    tasks = TaskRuntime(store, scope, None)
    calls = []
    retries = asyncio.Event()

    async def forbidden(*args):
        pytest.fail("已关闭目标不能重发或执行常规步骤")

    async def reconcile(identity, context):
        assert context == {"submission_started": True, "target": "draft-1"}
        calls.append(identity)
        if len(calls) == 1:
            retries.set()
            if missing_receipt:
                return None
            raise ValueError("回执未确认")
        return {"receipt": "original-receipt"}

    tasks.register("publish", Tool("test", StrictModel, forbidden, reconcile, checkpoint_submission=True))
    tasks.start()
    try:
        await asyncio.wait_for(retries.wait(), 2)
        waiting = tasks.current(root)
        assert waiting["status"] == "waiting" and waiting["steps"] == [step]
        assert waiting["pending_control"]
        tasks._save(waiting, due=time.time())
        tasks.wake.set()
        async with asyncio.timeout(2):
            while tasks.current(root)["status"] != "cancelled":
                await asyncio.sleep(.01)
        assert calls == [step["id"], step["id"]]
        assert tasks.current(root)["steps"] == [step | {"result": {"receipt": "original-receipt"}}]
    finally:
        await tasks.close()
        store.close()


async def test_closing_during_pending_steer_cannot_requeue_or_admit_work(tmp_path):
    store = Store(tmp_path / "studio.sqlite", [])
    tasks = TaskRuntime(store, lambda: ["owner", "whale"], None)
    root = new(tasks, "初版要求")
    tasks.workspace.promote(tasks.current(root))
    started, release = asyncio.Event(), asyncio.Event()
    stops = []

    async def stop(identity, context):
        stops.append(identity)
        started.set()
        await release.wait()
        return {"status": "cancelled"}

    tasks.register("read", Tool("read", StrictModel, None, stop))
    step = {"id": str(uuid4()), "revision": 1, "tool": "read", "arguments": {}}
    tasks._save(tasks.current(root), steps=[step], status="running")
    steering = asyncio.create_task(tasks.control(root, 1, "steer", "调整过的要求"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(ValueError, match="确认整体停止范围"):
            await tasks.close_goal(root, {root: 1})
        await asyncio.wait_for(tasks.close_goal(root, {root: 2}), 1)
        with pytest.raises(ValueError, match="目标已结束"):
            new(tasks, "不能排入", root)
        release.set()
        await steering
        assert json.loads(tasks.current(root)["pending_control"])["status"] == "cancelled"
        await tasks._advance(tasks.current(root))
        assert tasks.current(root)["status"] == "cancelled"
        assert all(identity == step["id"] for identity in stops)
    finally:
        release.set()
        await asyncio.gather(steering, return_exceptions=True)
        store.close()
