import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
from uuid import uuid4

import httpx
import pytest
import uvicorn

from tuber_studio.agent import private_call
from tuber_studio.app import create_app
from tuber_studio.responses_provider import FunctionCall, ResponseTurn


CHAT = "/api/chat/intelligent"
MEM = "/api/agent/memory"
NOTE = "写开场时我喜欢先讲海底厨房。"


@pytest.fixture
async def owner(settings):
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        yield client, app.state
    await app.state.comments.adapter.close()
    app.state.store.close()


async def save(client, source="settings", **fields):
    body = {"source": source, "text": NOTE, **fields}
    proposal = (await client.post(MEM + ".propose", json=body)).raise_for_status().json()
    response = await client.post(MEM + "/confirm", json=body | {"stamp": proposal["stamp"]})
    return response.raise_for_status().json()["id"]


async def test_owner_citations_voice_and_temporary_window(owner, monkeypatch):
    client, state = owner
    given = {"request_id": str(uuid4()), "message": NOTE, "intent": {"action": "chat"}}
    (await client.post(CHAT, json=given)).raise_for_status()
    denied = await client.post(MEM + ".propose", json={"source": "turn", "text": "假引语", "turn_id": given["request_id"]})
    assert denied.status_code == 409
    identity = await save(client, "turn", turn_id=given["request_id"])
    source_key = "turn:" + given["request_id"]
    hit, = (await client.post(MEM + ".search", json={"query": source_key})).json()["items"]
    assert (hit["id"], hit["quote"], hit["source_key"]) == (identity, NOTE, source_key)
    assert (await client.post(MEM + ".get", json={"id": identity})).json() == hit
    assert (await client.post(MEM + ".get", json={"id": identity, "channel": "public"})).status_code == 422

    prompts = []

    async def generate(messages, schema, **kwargs):
        prompts.append(json.dumps(messages, ensure_ascii=False))
        return {"action": "chat", "reply_markdown": "我记下了。"}

    monkeypatch.setattr(state.runtime.llm, "generate_assistant_json", generate, raising=False)
    reply = await client.post(CHAT, json=given | {"request_id": str(uuid4()), "message": source_key})
    assert reply.raise_for_status().json()["memory_citations"] == [hit] and NOTE in prompts[-1]
    transient = {"request_id": str(uuid4()), "message": source_key, "temporary": True,
                 "window": [{"role": "user", "content": "这个窗口里的线索"}]}
    assert (await client.post(CHAT, json=transient)).raise_for_status().json()["temporary"]
    assert NOTE not in prompts[-1] and "这个窗口里的线索" in prompts[-1]
    assert transient["request_id"] not in (await client.get(CHAT)).text
    assert (await client.post(CHAT, json=transient | {"intent": {"action": "agent_task"}})).status_code == 422
    assert not state.store.one("SELECT 1 FROM agent_tasks")

    async def mint(*args):
        return "test-ticket"

    monkeypatch.setattr(private_call, "private_service", lambda: ("https://voice.example", "test-key"))
    monkeypatch.setattr(private_call, "mint_ticket", mint)
    path = "/api/chat/private-call/ticket"
    ticket = (await client.post(path, json={"topic": source_key})).raise_for_status().json()
    assert NOTE in ticket["instructions"] and ticket["memory_citations"] == [hit]
    temporary = (await client.post(path, json={"topic": source_key, "temporary": True})).json()
    assert NOTE not in temporary["instructions"] and temporary["memory_use"] is None
    use = ticket["memory_use"]
    assert (await client.post(MEM + "/validate", json=use)).status_code == 200
    edit = f"/api/daily-memory/organized/{identity}?expected_revision={hit['revision']}"
    assert (await client.patch(edit, json={"note": "开场用三句话。"})).status_code == 200
    assert (await client.delete(edit)).status_code == 412
    assert (await client.post(MEM + "/validate", json=use)).status_code == 409
    assert all(not turn.get("memory_citations") for turn in (await client.get(CHAT)).json()["turns"])


async def test_cross_client_withdrawal_while_streaming(owner, monkeypatch):
    client, state = owner
    identity = await save(client)
    row = (await client.post(MEM + ".get", json={"id": identity})).json()
    generating, finish = asyncio.Event(), asyncio.Event()

    async def generate(messages, schema, *, on_content, **kwargs):
        assert NOTE in json.dumps(messages, ensure_ascii=False)
        if on_content:
            on_content('{"action":"chat","reply_markdown":"绝不应交付')
        generating.set()
        await finish.wait()
        return {"action": "chat", "reply_markdown": "绝不应交付"}

    monkeypatch.setattr(state.runtime.llm, "generate_assistant_json", generate, raising=False)
    task_id = str(uuid4())
    stream = asyncio.create_task(client.post(CHAT + "/stream", json={"request_id": task_id,
                                 "message": row["source_key"], "intent": {"action": "chat"}}))
    try:
        await asyncio.wait_for(generating.wait(), 3)
        (await client.delete(f"/api/daily-memory/organized/{identity}")).raise_for_status()
    finally:
        finish.set()
    events = (await stream).text
    assert "event: error" in events and "绝不应交付" not in events and "event: delta" not in events
    assert not state.store.one("SELECT 1 FROM operator_chat_turns WHERE request_id=?", (task_id,))
    assert (await client.post(MEM + ".search", json={"query": row["source_key"]})).json()["items"] == []


async def test_private_child_memory_functions_and_mid_delivery_withdrawal(owner):
    client, state = owner
    identity = await save(client)
    hit = (await client.post(MEM + ".get", json={"id": identity})).json()
    task_id = str(uuid4())
    tasks = state.runtime.tasks
    url = "/api/agent/tasks"
    body = {"request_id": task_id, "goal": hit["source_key"], "attachments": [{"name": "开场.txt", "text": "下一场准备三段。"}]}
    stored = (await client.post(url, json=body)).raise_for_status().json()
    source = stored["steps"][0]["id"] + ":0"
    replies = []

    async def respond(**options):
        names = {tool["name"] for tool in options["tools"]}
        replies.append(names)
        index = len(replies)
        if index == 1:
            assert "web_page" not in names and "delegate_text" in names
            calls = [FunctionCall("child", "delegate_text", {"objective": "写开场", "sources": [source]})]
        elif index == 2:
            assert names == {"memory_search", "memory_get", "memory_propose"}
            calls = [FunctionCall("search", "memory_search", {"query": "海底厨房"}),
                     FunctionCall("get", "memory_get", {"id": identity}),
                     FunctionCall("proposal", "memory_propose", {"source_id": source, "quote": "准备三段"})]
        elif index == 3:
            assert NOTE in options["input"][-3]["output"]
            return ResponseTurn("child-done", (), "私有草稿", {})
        else:
            (await client.delete(f"/api/daily-memory/organized/{identity}")).raise_for_status()
            calls = [FunctionCall("ask", "ask_owner", {"question": "不能送出的旧记忆"})]
        return ResponseTurn(str(index), tuple(calls), "", {})

    tasks.responses_client = lambda: respond
    await tasks._advance(tasks.current(task_id))
    await tasks._advance(tasks.current(task_id))
    step = (await client.get(url + "/" + task_id)).json()["steps"][-1]
    assert step["result"]["memory_citations"] == [hit]
    assert step["result"]["memory_proposals"] == [{"source_id": source, "quote": "准备三段"}]
    assert len((await client.get("/api/daily-memory")).json()["organized"]) == 1
    await tasks._advance(tasks.current(task_id))
    final = (await client.get(url + "/" + task_id)).json()
    assert final["status"] == "failed" and final["artifact"] is None
    assert NOTE not in json.dumps(final, ensure_ascii=False) and "不能送出的旧记忆" not in str(final)


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Playwright and Chromium")
async def test_chromium_owner_controls(settings, monkeypatch):
    app = create_app(settings)

    async def chat(messages, schema, **kwargs):
        return {"action": "chat", "reply_markdown": "好的，我来准备开场。"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", chat, raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        monkeypatch.setattr(private_call, "private_service", lambda: (origin, "fixture"))
        async def mint(*args):
            return "session-fixture"
        monkeypatch.setattr(private_call, "mint_ticket", mint)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", lifespan="off",
                                               timeout_graceful_shutdown=2))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(.05)
            script = Path(__file__).parent / "browser" / "owner-memory.cjs"
            await asyncio.to_thread(subprocess.run, ["node", script, origin], check=True, timeout=150)
        finally:
            server.should_exit = True
            await serving
            await app.state.comments.adapter.close()
            app.state.store.close()

@pytest.mark.parametrize("withdraw_during_edit", [False, True])
async def test_private_video_editor_reads_preferences_only_after_research(owner, monkeypatch, withdraw_during_edit):
    from types import SimpleNamespace
    from tuber_studio.agent import public_video

    client, state = owner
    tasks = state.runtime.tasks
    note = "以后写视频提纲，请让每个开场标题都用一个问题。"
    turn = str(uuid4())
    (await client.post(CHAT, json={"request_id": turn, "message": note, "intent": {"action": "chat"}})).raise_for_status()
    body = {"source":"turn", "turn_id": turn, "text":note}
    proposal = (await client.post(MEM+".propose", json=body)).raise_for_status().json()
    item = (await client.post(MEM+"/confirm", json=body | {"stamp":proposal["stamp"]})).raise_for_status().json()["id"]
    bv = "BV1aa1234567"
    reads = []
    class Platform:
        async def video_metadata(self, bvid):
            assert bvid == bv
            reads.append("metadata")
            return {"bvid":bv, "parts":[{"page":2, "cid":"27", "duration":18}]}

        async def subtitles(self, bvid, cid):
            assert (bvid,cid) == (bv,"27")
            return {"segments":[]}

        async def video_stream_frame(self, meta, *, cid, timestamp):
            assert meta["bvid"] == bv and cid == "27" and 4 <= timestamp < 10
            return {"cid":cid, "id":str(timestamp), "actual_time":timestamp,"presentation_end":timestamp+.04}

        async def close(self):
            pass

    async def observe(task, meta, frames, question, **options):
        assert note not in str([task,meta,frames,question,options])
        return {"evidence_ids":[frames[0]["id"]], "observations":"左侧有一把椅子。"}

    monkeypatch.setattr(public_video, "BrowsingAdapter", Platform)
    state.browsing.evidence.visual = SimpleNamespace(analyze_video_evidence=observe)
    task_id = None
    frames = []
    async def planner(**options):
        assert note not in json.dumps(options,ensure_ascii=False)
        assert "椅子放在哪里？" not in json.dumps(options, ensure_ascii=False)
        steps = tasks.current(task_id)["steps"]
        if not steps:
            call = FunctionCall("read", "inspect_video", {"bvid":bv,"part":2,"start":4,"end":10})
        else:
            step = next(s for s in steps if s["tool"]=="inspect_video")
            assert step["result"]["findings"]
            key = step["id"] + ":1"
            frames.append(key)
            call = FunctionCall("submit", "submit_materials", {"items":[{"title":"候选","sources":[key],
                "primary_frame":key,"rationale":"构图"}],"gaps":[]})
        return ResponseTurn("response"+call.call_id, (call,), "", {})

    tasks.responses_client = lambda: planner
    seen = []
    async def editor(messages, schema, **kwargs):
        packet = json.loads(messages[-1]["content"])
        if "options" in packet:
            assert packet["options"] == [{"id":item,"note":note}]
            return {"ids":[item]}
        if "candidate" in packet:
            prefs = packet.get("owner_writing_preferences", [])
            assert isinstance(packet["candidate"]["items"], list)
            assert note not in json.dumps(packet["candidate"],ensure_ascii=False)
            seen.append(prefs)
            if not prefs:
                assert "椅子放在哪里？" not in json.dumps(packet, ensure_ascii=False)
            if withdraw_during_edit:
                (await client.delete(f"/api/daily-memory/organized/{item}")).raise_for_status()
            return {"items":[{"title":"椅子放在哪里？" if prefs else "画面中的椅子",
                              "sources":frames[-1:], "primary_frame":frames[-1],"rationale":"用椅子的位置聊构图。"}],"gaps":[]}
        assert "owner_writing_preferences" not in packet
        return {"supported": True, "issues": []}

    monkeypatch.setattr(state.runtime.llm,"generate_json",editor)
    async def edition():
        nonlocal task_id
        task_id = str(uuid4())
        (await client.post("/api/agent/tasks", json={"request_id":task_id, "goal":f"用 {bv} P2 的 4 到 10 秒写私有视频提纲。",
            "video_selection":{"bvid":bv, "ranges":[{"part":2,"start":4,"end":10}]}})).raise_for_status()
        for _ in range(8):
            current = tasks.current(task_id)
            if current["status"] not in {"queued","running","waiting"}:
                break
            await tasks._advance(current)
        return (await client.get("/api/agent/tasks/"+task_id)).raise_for_status().json()

    first = await edition()
    assert reads and seen == [[note]]
    if withdraw_during_edit:
        assert first["status"] == "failed" and first["artifact"] is None
        return
    assert first["status"] == "completed" and "椅子放在哪里？" in first["artifact"]["body"]
    assert first["memory_citations"][0]["source_key"] == "turn:"+turn
    history = (await client.get(f"/api/agent/tasks/{task_id}/artifacts")).raise_for_status().json()
    rev = history[0]
    artifact = await client.get(f"/api/agent/tasks/{task_id}/artifacts/{rev['task_revision']}/{rev['edition']}")
    assert "椅子放在哪里？" in artifact.raise_for_status().json()["body"]
    (await client.delete(f"/api/daily-memory/organized/{item}")).raise_for_status()
    (await client.patch(f"/api/agent/tasks/{task_id}", json={
        "action":"steer", "revision":1, "goal":f"再看 {bv} P2 同一区间，重新写这份视频提纲。"
    })).raise_for_status()
    for _ in range(8):
        current = tasks.current(task_id)
        if current["status"] not in {"queued", "running", "waiting"}:
            break
        await tasks._advance(current)
    second = (await client.get(f"/api/agent/tasks/{task_id}")).raise_for_status().json()
    assert second["status"] == "completed" and "画面中的椅子" in second["artifact"]["body"]
    assert seen == [[note], []] and not second.get("memory_citations")
    assert not tasks.edition_recallable(task_id, 1) and tasks.edition_recallable(task_id, 2)


async def test_exact_original_ranked_before_eight_fuzzy_matches(owner, monkeypatch):
    client, state = owner
    turn = str(uuid4())
    (await client.post(CHAT, json={"request_id": turn, "message": NOTE, "intent": {"action": "chat"}})).raise_for_status()
    target = await save(client, "turn", turn_id=turn)
    from tuber_studio.agent.memory_organizer import MemorySuggestion
    memory = state.runtime.daily_memory
    scope = state.runtime.memory_organizer.scope()
    replacement = str(uuid4())
    note = "新的开场偏好。"
    (await client.post(CHAT, json={"request_id": replacement, "message": note, "intent": {"action": "chat"}})).raise_for_status()
    current, = memory.organized(scope, item_id=target)
    assert memory.save_organized(scope, {"kind": "private", "key": "turn:"+replacement, "text":note,
        "at":current["updated_at"]+1, "private_owner":memory.private_owner},
        MemorySuggestion(action="update", existing_id=target, headline="开场偏好", note=note, tags=[]), current)
    assert memory.lookup_source(scope, "operator", "turn:"+replacement)[0]["id"] == target
    fuzzy = []
    for number in range(8):
        identity = await save(client, text=f"另一个开场写法 {number}。")
        fuzzy.append(memory.owner_projection(memory.organized(scope, item_id=identity)[0]))
    monkeypatch.setattr(memory, "recall_organized", lambda *args, **kwargs: fuzzy)
    key = "turn:" + turn
    hits = (await client.post(MEM + ".search", json={"query": key})).raise_for_status().json()["items"]
    assert len(hits) == 8
    assert (hits[0]["id"],hits[0]["source_key"],hits[0]["quote"],hits[0]["note"]) == (target,key,NOTE,"新的开场偏好。")
