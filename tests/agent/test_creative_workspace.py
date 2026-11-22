import asyncio
from uuid import uuid4

import httpx
from fastapi import FastAPI

from tuber_studio.agent.api.tasks import register_task_api
from tuber_studio.agent.tasks.delivery import PrivateDelivery
from tuber_studio.agent.tasks.runtime import TaskRuntime
from tuber_studio.app import create_app
from tuber_studio.core.store import Store
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn


async def test_activity_available_while_model_health_is_blocked(settings):
    app = create_app(settings)
    started = asyncio.Event()

    async def stalled():
        started.set()
        await asyncio.Event().wait()

    app.state.runtime.llm.health = stalled
    async with asyncio.timeout(5), app.router.lifespan_context(app):
        await started.wait()
        assert app.state.runtime.tasks.activity() == []


async def test_attachment_task_has_no_public_tools_and_cites_private_input(settings):
    app = create_app(settings)
    tasks = app.state.runtime.tasks
    identity = str(uuid4())
    payload = {"request_id": identity, "goal": "提取开场依据", "attachments": [{"name": "开场.md", "text": "鲸鱼从左侧入场。"}]}
    base = "/api/agent/tasks"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        assert (await client.post(base, json=payload)).status_code == 200
        assert (await client.post(base, json=payload)).status_code == 200
        assert (await client.post(base, json=payload | {"attachments": []})).status_code == 409
        assert (await client.post(base, json=payload | {"attachments": [{"name": "../foo.md", "text": "test"}]})).status_code == 422

    async def submit(input, tools, **kwargs):
        assert {tool["name"] for tool in tools} == {"submit_materials", "ask_owner", "delegate_text"}
        source = tasks.current(identity)["steps"][0]["id"] + ":0"
        assert source in input[1]["content"] and "鲸鱼从左侧入场" in input[1]["content"]
        candidate = {"items": [{"title": "开场", "rationale": "适合作为第一镜", "sources": [source]}], "gaps": []}
        return ResponseTurn("turn", (FunctionCall("call", "submit_materials", candidate),), "", {})

    async def edit(task, evidence):
        return PrivateDelivery.model_validate(task["plan"]["candidate"])

    tasks.responses_client = lambda: submit
    tasks.edit_delivery = edit
    for _ in range(2):
        await tasks._advance(tasks.current(identity))
    assert "鲸鱼从左侧入场" in tasks.workspace.latest(identity)["body"]
    app.state.store.close()


async def test_private_goal_and_editions_survive_control_restart_and_scope_switch(tmp_path):
    path = tmp_path / "studio.sqlite"
    scope = ["owner", "whale"]
    store = Store(path, [])
    tasks = TaskRuntime(store, lambda: scope.copy(), None)
    root, child = str(uuid4()), str(uuid4())
    tasks.create(root, "准备直播")
    app = FastAPI()
    register_task_api(app, tasks, lambda: None)
    base = "/api/agent/tasks"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.put(f"{base}/{root}/goal")).json()["creative_goal"]["title"] == "准备直播"
        payload = {"request_id": child, "goal": "核对两处时码", "creative_goal_id": root}
        for _ in range(2):
            assert (await client.post(base, json=payload)).status_code == 200
        assert len(tasks.workspace.runs(root)) == 2
        assert (await client.post(f"{base}/{root}/goal/close")).status_code == 409
        assert tasks._save(tasks.current(child), status="completed", message="P2 · 01:20")
        assert (await client.get("/api/agent/attention")).json()["count"] == 1
        stamp = (await client.get(f"{base}/{child}")).json()["attention"]
        await client.delete(f"{base}/{child}/attention", params={"updated": stamp - 1})
        assert (await client.get("/api/agent/attention")).json()["count"] == 1
        await client.delete(f"{base}/{child}/attention", params={"updated": stamp})
        assert (await client.get("/api/agent/attention")).json()["count"] == 0
        edit = {"task_revision": 1, "edition": 1, "body": "开场：P2 · 01:20"}
        edited = await client.patch(f"{base}/{child}/artifact", json=edit)
        assert edited.json()["artifact"]["edition"] == 2
        versions = f"{base}/{child}/artifacts"
        assert [row["edition"] for row in (await client.get(versions)).json()] == [2, 1]
        assert (await client.get(versions + "/1/1")).json()["body"] == "P2 · 01:20"
        assert (await client.get(versions + "/2/1")).status_code == 404
        assert tasks.current(child)["message"] == "P2 · 01:20"
        assert (await client.patch(f"{base}/{child}/artifact", json=edit)).status_code == 409
        await tasks.control(root, 1, "cancel")
        assert not tasks.workspace.goal(root)["closed"]
        assert (await client.post(f"{base}/{root}/goal/close")).json()["creative_goal"]["closed"]
        assert (await client.post(base, json=payload | {"request_id": str(uuid4())})).status_code == 409
        scope[1] = "other"
        assert (await client.get(versions)).status_code == 404
        assert (await client.get(versions + "/1/1")).status_code == 404
        assert (await client.get(base)).json()["goals"] == []
        assert (await client.get(f"{base}/{child}")).status_code == 404
        assert (await client.patch(f"{base}/{child}/artifact", json=edit)).status_code == 404
    store.close()
    reopened = Store(path, [])
    restored = TaskRuntime(reopened, lambda: ["owner", "whale"], None)
    assert restored.workspace.latest(child)["body"] == edit["body"]
    assert restored.workspace.goal(root)["closed"]
    reopened.close()
