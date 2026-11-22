from uuid import uuid4

import httpx

from tuber_studio.agent.creative.private_code import CodeFile, run_code
from tuber_studio.agent.tasks.runtime import ToolRequest
from tuber_studio.app import create_app
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn


async def test_owner_grant_runs_attachment_calculation_and_preserves_output(settings):
    app = create_app(settings)
    tasks = app.state.runtime.tasks
    identity = str(uuid4())
    payload = {"request_id": identity, "goal": "统计播放数", "allow_code": True,
               "attachments": [{"name": "播放.json", "text": "[2,3,7]"}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        assert (await client.post("/api/v1/agent/tasks", json=payload)).status_code == 200
        assert (await client.post("/api/v1/agent/tasks", json=payload | {"allow_code": False})).status_code == 409

    args = {"name": "总计.txt", "script": "return String(JSON.parse(files[0].text).reduce((a,b)=>a+b,0))"}

    async def execute(*, tools, **kwargs):
        assert {tool["name"] for tool in tools} == {"submit_materials", "ask_owner", "delegate_text", "private_code"}
        return ResponseTurn("turn", (FunctionCall("call", "private_code", args),), "", {})

    tasks.responses_client = lambda: execute
    for _ in range(2):
        await tasks._advance(tasks.current(identity))
    assert tasks.current(identity)["steps"][-1]["result"] == {"name": "总计.txt", "text": "12"}
    app.state.store.close()


async def test_private_code_needs_owner_grant_and_has_no_host_bindings():
    args = CodeFile(name="检查.txt", script="return [typeof fetch,typeof process,typeof require,typeof std,typeof os].join(',')")
    request = ToolRequest("one", "核对", [], None, None)
    assert "error" in await run_code(request, args)
    request = ToolRequest("two", "核对", [], None, None, ({"revision": 0, "tool": "owner_code_grant"},))
    assert (await run_code(request, args))["text"] == ",".join(["undefined"] * 5)
    assert "error" in await run_code(request, args.model_copy(update={"script": "for(;;){}"}))
