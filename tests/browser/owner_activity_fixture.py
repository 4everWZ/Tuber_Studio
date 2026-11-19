"""Run the owner activity Chromium acceptance against a disposable, offline Studio."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from tuber_studio.agent.task_delivery import PrivateDelivery
from tuber_studio.app import create_app
from tuber_studio.config import ROOT, Settings
from tuber_studio.responses_provider import FunctionCall, ResponseTurn


class Platform:
    host = "platform.example.test"
    uid = "100"
    _sessdata = "fixture-owner-100"
    _csrf = "fixture-csrf"

    def __init__(self):
        self.requests = []
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.receive), cookies={"SESSDATA": self._sessdata},
            event_hooks={"request": [self.route]}, trust_env=False,
        )

    async def route(self, request):
        assert request.url.host == "api.bilibili.com"
        request.url = request.url.copy_with(host=self.host)
        request.headers["host"] = self.host

    def receive(self, request):
        assert request.method == "POST" and request.url.host == self.host
        assert request.headers["cookie"] == f"SESSDATA={self._sessdata}"
        path = request.url.path
        record = {"host": request.url.host, "path": path, "uid": self.uid}
        self.requests.append(record)
        if path == "/x/dynamic/feed/draw/upload_bfs":
            assert b"image/png" in request.content
            data = {"image_url": "https://i0.hdslb.com/bfs/fixture.png", "image_width": 960,
                    "image_height": 600, "img_size": 1}
        else:
            assert path == "/x/dynamic/feed/create/dyn" and request.url.params["csrf"] == self._csrf
            payload = json.loads(request.content)["dyn_req"]
            assert len(payload["content"]["contents"]) == len(payload["pics"]) == 1
            record["text"] = payload["content"]["contents"][0]["raw_text"]
            data = {"dyn_id_str": "99001"}
        return httpx.Response(200, json={"code": 0, "data": data})

    async def self_uid(self):
        return self.uid

    async def wbi_params(self, params):
        return params

    async def close(self):
        await self.client.aclose()


async def scripted_response(*, input, **kwargs):
    goal = json.loads(input[1]["content"])["goal"]
    outputs = [json.loads(item["output"]) for item in input if item.get("type") == "function_call_output"]
    if goal == "合计输入数据" and not outputs:
        call = FunctionCall(str(uuid4()), "private_code", {
            "name": "合计.txt", "script": "return String(JSON.parse(files[0].text).reduce((a,b)=>a+b,0))"})
    elif goal == "合计输入数据":
        call = FunctionCall(str(uuid4()), "submit_materials", {"items": [
            {"title": "计算合计", "rationale": "供号主核对输入数据。", "sources": list(outputs[-1]["evidence"])}
        ], "gaps": []})
    elif "先核对" in goal and not outputs:
        call = FunctionCall(str(uuid4()), "inspect_dynamic_draft", {"post_id": 1})
    elif "先核对" in goal:
        assert outputs[-1]["receipt"]["sources"][0]["receipt_id"] == "77"
        call = FunctionCall(str(uuid4()), "ask_owner", {"question": "已核对原评论出处。需要我提交发布吗？"})
    elif outputs:
        call = FunctionCall(str(uuid4()), "submit_materials", {"items": [{
            "title": "发布回执", "rationale": "供号主核对本次发布。",
            "sources": list(outputs[-1]["evidence"]),
        }], "gaps": []})
    else:
        call = FunctionCall(str(uuid4()), "publish_dynamic_draft", {"post_id": 1})
    return ResponseTurn(str(uuid4()), (call,), "", {})


async def select_candidate(task, evidence):
    return PrivateDelivery.model_validate(task["plan"]["candidate"])


async def setup(runtime_dir, denied):
    settings = Settings(runtime=runtime_dir)
    platform = Platform()
    app = create_app(settings, adapter=platform)
    state = app.state
    runtime, store = state.runtime, state.store
    runtime.tasks.responses_client = lambda: scripted_response
    runtime.tasks.edit_delivery = select_candidate
    state.comments.credentials.update(sessdata=platform._sessdata, csrf=platform._csrf)
    assert await state.comments.account_id() == platform.uid
    char = runtime.character
    for i, body in enumerate(["今晚开场分享海底厨房的来信。", "另一份未获授权的草稿。"], 1):
        cid = store.execute("INSERT INTO comments(video_id,comment_id,content) VALUES(?,?,?)",
                            ("BV1d8pU6xEZX", str(41 + i), f"海底厨房来信 {i}")).lastrowid
        draft = store.execute(
            "INSERT INTO drafts(comment_id,text,status,character_id,character_version,receipt) VALUES(?,?,?,?,?,?)",
            (cid, f"海底厨房回信 {i}", "published", char["id"], char["version"], json.dumps({"rpid": str(76 + i)})),
        ).lastrowid
        sources = state.dynamics._sources()
        decision = {"action": "post", "reason": "浏览器验收", "content": body, "source_ids": [f"reply:{draft}"],
                    "next_evaluation_seconds": 1800}
        post = state.dynamics._save_decision(char, True, decision, sources, schedule=False, account_id=platform.uid,
                                             generation_identity=runtime.identity_snapshot(char, platform.uid))
        assert post["post_id"] == i
    for i in range(18):
        store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
            str(uuid4()), char["id"], char["version"], f"素材札记 {i:02d}",
            json.dumps({"reply_text": f"第 {i} 页的开场素材已记下。", "account_id": platform.uid, "card": None}),
            time.time() - 100 + i,
        ))

    @app.get("/__fixture__/observations")
    async def observations():
        return {"requests": platform.requests, "denied_connections": denied,
                "character": char["name"], "version": char["version"],
                "posts": [dict(row) for row in store.all("SELECT id,status,receipt FROM dynamic_posts ORDER BY id")],
                "attempts": store.one("SELECT count(*) AS n FROM account_writes")["n"]}

    return app


async def main():
    denied = []

    def block_connect(event, args):
        if event == "socket.getaddrinfo" and args[0] not in {None, "localhost", "127.0.0.1", "::1"}:
            denied.append(str(args[0]))
            raise PermissionError("Offline Studio fixture forbids external DNS")
        if event == "socket.connect" and args[0].family in {socket.AF_INET, socket.AF_INET6}:
            denied.append(str(args[1]))
            raise PermissionError("Offline Studio fixture forbids outbound TCP/UDP connections")

    os.environ.pop("TUBER_CONFIG", None)
    os.environ["TUBER_ADMIN_TOKEN"] = secrets.token_urlsafe(32)
    tmp = ROOT / "tmp"
    tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp, prefix="owner-activity-") as folder, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        sys.addaudithook(block_connect)
        app = await setup(Path(folder), denied)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=5))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if running.done():
                    await running
                    raise RuntimeError("Studio failed to start")
                await asyncio.sleep(.05)
            script = Path(__file__).with_name("owner-activity.cjs")
            await asyncio.to_thread(subprocess.run, ["node", script, f"http://127.0.0.1:{port}"], check=True)
        finally:
            server.should_exit = True
            await running


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
