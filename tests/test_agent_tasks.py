import asyncio
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_browsing import BV, ControlledPlatform

from tuber_studio.agent import task_tools
from tuber_studio.agent.task_runtime import TaskDecision, TaskRuntime, Tool, ToolRequest, ToolWait
from tuber_studio.app import create_app
from tuber_studio.models import BilibiliOwner, Reply, StrictModel
from tuber_studio.providers import Audio
from tuber_studio.store import Store
from tuber_studio.web_fetch import WebFetchRequest


async def until(condition):
    async with asyncio.timeout(8):
        while not (result := condition()):
            await asyncio.sleep(0.02)
    return result


async def test_task_restarts_at_saved_tool_checkpoint(tmp_path):
    path = tmp_path / "agent.sqlite3"
    identity = str(uuid4())
    calls = []

    async def decide(goal, steps, tools):
        if steps:
            assert steps[-1]["result"] == {"confirmed": True}
            return TaskDecision(action="finish", message="核验完成。")
        return TaskDecision(action="tool", tool="lookup")

    async def lookup(request, arguments):
        calls.append((request.id, request.checkpoint))
        if not request.checkpoint:
            return ToolWait({"remote_id": "receipt"}, 0.5)
        return {"confirmed": True}

    store = Store(path, [])
    first = TaskRuntime(store, lambda: ["user"], decide)
    first.register("lookup", Tool("Read fixture", StrictModel, lookup))
    first.start()
    try:
        first.create(identity, "请核验收据")
        await until(lambda: first.current(identity)["status"] == "waiting")
    finally:
        await first.close()
        store.close()

    store = Store(path, [])
    second = TaskRuntime(store, lambda: ["user"], decide)
    second.register("lookup", Tool("Read fixture", StrictModel, lookup))
    second.start()
    try:
        await until(lambda: second.current(identity)["status"] == "completed")
        task = second.current(identity)
        assert task["steps"][-1]["result"] == {"confirmed": True}
        assert "已保存工具回执。" in task["message"]
        assert len(calls) == 2 and calls[0][0] == calls[1][0]
        assert calls[-1][1] == {"remote_id": "receipt"}
    finally:
        await second.close()
        store.close()


@pytest.mark.parametrize(("phase", "finish"), [
    ("plan", "generated"), ("decision", "generated"), ("plan", "timeout"), ("decision", "cancelled"),
])
async def test_agent_model_deadline_starts_after_background_admission(settings, monkeypatch, phase, finish):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, tasks = app.state.runtime, app.state.runtime.tasks
    queued, generating, respond = asyncio.Event(), asyncio.Event(), asyncio.Event()
    acquire = runtime.operation_turns.acquire
    loop = asyncio.get_running_loop()
    monotonic = loop.time
    offset = 0

    async def notice_queue(lane, ready_at):
        assert lane == "background"
        queued.set()
        return await acquire(lane, ready_at)

    async def generate(messages, schema, *, max_tokens):
        generating.set()
        await respond.wait()
        if phase == "plan":
            return {"question": "请补充资料。"}
        return {"action": "ask", "message": "请补充资料。"}

    async def process_timers():
        # Yield across deadline dispatch and the task cancellation it schedules.
        for _ in range(2):
            turn = loop.create_future()
            loop.call_soon(turn.set_result, None)
            await turn

    if phase == "decision":
        tasks.make_plan = None
    monkeypatch.setattr(runtime.operation_turns, "acquire", notice_queue)
    monkeypatch.setattr(runtime.llm, "generate_json", generate, raising=False)
    monkeypatch.setattr(loop, "time", lambda: monotonic() + offset)
    identity = str(uuid4())
    async with app.router.lifespan_context(app):
        async with runtime.generation_lock:
            tasks.create(identity, "请核对准备材料")
            await asyncio.wait_for(queued.wait(), 2)
            active = tasks.active[1]
            offset = 121
            await process_timers()
            assert not active.done()
            assert not generating.is_set()
            assert len(runtime.operation_turns.queues["background"]) == 1
            assert tasks.current(identity)["status"] == "running"
            assert tasks.current(identity)["steps"] == []
            if finish == "cancelled":
                result = await tasks.control(identity, 1, "cancel")
                assert result["status"] == "cancelled"
                assert not runtime.operation_turns.queues["background"]
                assert not generating.is_set()
                return

        await asyncio.wait_for(generating.wait(), 2)
        if finish == "timeout":
            offset += 121
        else:
            respond.set()
        await asyncio.wait_for(active, 2)
        expected = "failed" if finish == "timeout" else "needs_input"
        assert tasks.current(identity)["status"] == expected
        assert not runtime.generation_lock.locked()


async def test_chat_delegates_across_existing_services_and_cancels_video(settings, monkeypatch):
    monkeypatch.delenv("TUBER_NATIVE_PROGRAM_STATUS", raising=False)
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    tasks = app.state.runtime.tasks
    calls = ["operation_status", "live_status", "game_status", "inspect_video"]
    seen = []

    async def decide(goal, steps, tools):
        seen.extend(step["result"] for step in steps[-1:])
        if goal == "只返回最终结论":
            if not steps:
                return TaskDecision(action="tool", tool="operation_status")
            return TaskDecision(action="finish", message="已按新要求继续。")
        tool = calls[len(steps)]
        return TaskDecision(action="tool", tool=tool, arguments={"bvid": BV} if tool == "inspect_video" else {})

    monkeypatch.setattr(tasks, "decide", decide)
    monkeypatch.setattr(tasks, "make_plan", None)
    # Keep the real domain submission, scope and cancellation and hold the
    # scheduler so the isolated fixture cannot contact the video platform.
    async def idle_scheduler():
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.browsing, "_start_full_video", idle_scheduler)
    request_id = str(uuid4())
    payload = {"request_id": request_id, "message": "查看运营、直播、游戏，再读视频", "intent": {"action": "agent_task"}}
    client = httpx.ASGITransport(app=app, client=("127.0.0.1", 54000))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=client, base_url="http://127.0.0.1") as owner:
        app.state.browsing.discovery.remember_account("100")
        response = await owner.post("/api/chat/intelligent", json=payload)
        assert response.json()["card"]["type"] == "agent_task", response.text
        await until(lambda: tasks.current(request_id)["status"] == "waiting")
        assert seen[0]["state"] == "off" and seen[1]["state"] == "stopped"
        assert set(seen[1]) == {"state", "muted", "session_id"}
        assert seen[2]["simulated"] is True
        activity_url = f"/api/agent/tasks/{request_id}"
        # A second client reads the same task after chat has already returned.
        async with httpx.AsyncClient(transport=client, base_url="http://127.0.0.1") as other:
            result = (await other.get(activity_url)).json()
            video_id = result["steps"][-1]["id"]
            assert app.state.browsing.repo.round(video_id)["preview"]
            response = await other.patch(activity_url, json={"revision": 1, "action": "cancel"})
            assert response.json()["status"] == "cancelled", response.text
            assert app.state.browsing.repo.round(video_id)["status"] == "cancelled"
            conflict = await other.patch(activity_url, json={"revision": 1, "action": "steer", "goal": "旧要求"})
            assert conflict.status_code == 409
            continued = await other.patch(activity_url, json={"revision": 2, "action": "steer", "goal": "只返回最终结论"})
            assert continued.status_code == 200
        await until(lambda: tasks.current(request_id)["status"] == "completed")
        continued = (await owner.get(activity_url)).json()
        assert continued["message"] == "**自主运营**\n- 未启动"
        app.state.browsing.discovery.remember_account("200")
        assert (await owner.get(activity_url)).json()["steps"] == continued["steps"]
        rejected = await owner.patch(activity_url, json={
            "revision": continued["revision"], "action": "steer", "goal": "按旧授权再做一次",
        })
        assert rejected.status_code == 409 and "授权已失效" in rejected.json()["detail"]


async def test_live_status_reports_native_program_separately_in_tasks_and_chat(settings, monkeypatch):
    monkeypatch.setenv("TUBER_NATIVE_PROGRAM_STATUS", "1")
    app = create_app(settings, adapter=ControlledPlatform())
    live_tool = app.state.runtime.tasks.tools["live_status"]
    bridge_state = "publishing"

    async def health(client, method, address):
        assert method == "GET" and address == "http://omni-media:8080/healthz"
        assert "authorization" not in client.headers
        if bridge_state is None:
            raise httpx.ConnectError("bridge offline")
        return {"process": "ready", "native_program": {"state": bridge_state, "session_id": "must-not-forward"}}

    monkeypatch.setattr(task_tools, "response_json", health)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54000))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as owner:
        status = await live_tool.run(None, StrictModel())
        assert status["state"] == "stopped" and status["native_program"] == {"state": "publishing"}
        response = await owner.post("/api/chat/intelligent", json={
            "request_id": str(uuid4()), "message": "查直播状态", "intent": {"action": "live_status"},
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["card"]["data"]["state"] == "stopped"
        assert result["card"]["data"]["native_program"] == {"state": "publishing"}
        assert result["reply_text"] == "Omni 原生节目正在发布。"
        for bridge_state in ("idle", "unexpected", None):
            status = await live_tool.run(None, StrictModel())
            assert status["state"] == "stopped"
            assert status["native_program"] == {"state": "idle" if bridge_state == "idle" else "unavailable"}


async def test_agent_web_page_is_bound_to_user_or_current_search_receipts(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    searched = "https://example.com/publisher"
    incidental = "https://www.bilibili.com/video/BV1Az39sK8T5/?p=1&t=23"
    calls = []

    async def read(url):
        calls.append(url)
        return {"url": url, "excerpt": "来源原文"}

    monkeypatch.setattr(task_tools, "fetch_excerpt", read)
    request = ToolRequest(str(uuid4()), "打开 https://example.com/direct。", [], None, None, (
        {"tool": "web_search", "result": {"links": [searched]}},
        {"tool": "inspect_video", "result": {"findings": [{"url": incidental}]}},
    ))

    async def decide(messages, schema, *, max_tokens):
        web = next(choice for choice in schema["oneOf"] if choice["properties"].get("tool") == {"const": "web_page"})
        assert web["properties"]["arguments"]["properties"]["web_fetch"]["enum"] == [
            "https://example.com/direct", searched,
        ]
        return {"action": "tool", "tool": "web_page", "arguments": {"web_fetch": searched}}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", decide)
    async with app.router.lifespan_context(app):
        spec = tasks.tools["web_page"]
        catalog = {"web_page": {"description": spec.description, "arguments": spec.arguments.model_json_schema()}}
        result = await tasks.decide(request.goal, request.receipts, catalog)
        assert result.arguments == {"web_fetch": searched}
        assert "error" in await spec.run(request, WebFetchRequest(web_fetch=incidental))
        assert await spec.run(request, WebFetchRequest(web_fetch=searched)) == {"url": searched, "excerpt": "来源原文"}
        assert calls == [searched]


async def test_explicit_fallback_condition_tracks_video_receipt_without_model_reinterpretation(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    called = []

    async def planned(messages, schema, *, max_tokens):
        called.append(schema)
        raise AssertionError("The explicit video/source order does not require model planning")

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", planned)
    catalog = {name: {"description": spec.description, "requires_approval": spec.review is not None,
                      "arguments": spec.arguments.model_json_schema()}
               for name, spec in tasks.tools.items()}
    async with app.router.lifespan_context(app):
        goal = f"只读查看 {BV}，如果只有字幕就搜索制作方，再读搜索得到的公告。"
        plan = await tasks.make_plan(goal, catalog)
        assert [step.tool for step in plan.steps] == ["inspect_video", "web_search", "web_page"]
        assert plan.steps[0].condition is None
        assert plan.steps[1].condition == plan.steps[2].condition
        condition = plan.steps[1].condition
        assert condition.evaluate([{"tool": "inspect_video", "result": {"viewing": "subtitles_only"}}]) is True
        assert condition.evaluate([{"tool": "inspect_video", "result": {"viewing": "watched"}}]) is False
        video = await tasks.decide(goal, [], {"inspect_video": catalog["inspect_video"]})
        assert video.arguments == {"bvid": BV}
        link = "https://example.com/announcement"
        receipts = [{"tool": "web_search", "result": {"links": [link]}}]
        page = await tasks.decide(goal, receipts, {"web_page": catalog["web_page"]})
        assert page.arguments == {"web_fetch": link}
        assert not called


@pytest.mark.parametrize(("model_reply", "expected"), [
    ({"steps": ["web_page", "web_search"]}, "no supplied URL or preceding search"),
    ({"steps": ["web_search", "web_page"], "question": "没有网页地址"}, "PlanningQuestion"),
    ({"steps": ["web_search", "web_page"]}, None),
    ({"question": "请补充检索主题。"}, None),
])
async def test_planning_requires_sources_before_page_read_or_a_separate_question(settings, monkeypatch, model_reply, expected):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks

    async def planned(messages, schema, *, max_tokens):
        executable, clarification = schema["oneOf"]
        assert executable["required"] == ["steps"] and executable["additionalProperties"] is False
        assert clarification["required"] == ["question"] and clarification["additionalProperties"] is False
        return model_reply

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", planned)
    catalog = {name: {"description": spec.description, "requires_approval": spec.review is not None}
               for name, spec in tasks.tools.items()}
    async with app.router.lifespan_context(app):
        goal = "搜索展馆公告并打开找到的页面。"
        if expected:
            with pytest.raises(ValueError, match=expected):
                await tasks.make_plan(goal, catalog)
        else:
            plan = await tasks.make_plan(goal, catalog)
            assert [step.tool for step in plan.steps] == model_reply.get("steps", [])
            assert plan.question == model_reply.get("question", "")


@pytest.mark.parametrize(("goal", "proposed", "expected"), [
    ("打开 https://example.com/a，然后查直播。", ["web_page", "live_status", "web_page"], ["web_page", "live_status"]),
    ("读取 https://example.com/a，失败可重试。", ["web_page", "web_page"], ["web_page", "web_page"]),
    ("读取 https://example.com/a 和 https://example.com/b。", ["web_page", "web_page"], ["web_page", "web_page"]),
    ("读取 https://example.com/a，另搜索公告和打开原文。", ["web_page", "web_search", "web_page"],
     ["web_page", "web_search", "web_page"]),
])
async def test_planner_only_collapses_unrequested_single_source_reads(settings, monkeypatch, goal, proposed, expected):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks

    async def planned(messages, schema, *, max_tokens):
        return {"steps": proposed.copy()}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", planned)
    catalog = {name: {"description": spec.description, "requires_approval": spec.review is not None}
               for name, spec in tasks.tools.items()}
    async with app.router.lifespan_context(app):
        plan = await tasks.make_plan(goal, catalog)
        assert [step.tool for step in plan.steps] == expected


async def test_approved_game_step_checks_control_and_replays_receipt(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    runtime.settings.data["features"]["live_enabled"] = True
    game, tasks = runtime.live.game_livestream, runtime.tasks

    async def decide(goal, steps, tools):
        if not steps:
            return TaskDecision(action="tool", tool="game_status")
        if len(steps) == 1:
            return TaskDecision(action="tool", tool="game_action",
                                arguments={"game": "stardew_valley", "action": "water_crops"})
        return TaskDecision(action="finish", message="游戏已返回实际结果。")

    monkeypatch.setattr(tasks, "decide", decide)
    monkeypatch.setattr(tasks, "make_plan", None)
    async with app.router.lifespan_context(app):
        identity = str(uuid4())
        tasks.create(identity, "请给星露谷作物浇水")
        await until(lambda: tasks.current(identity)["status"] == "awaiting_approval")
        step = tasks.current(identity)["steps"][-1]
        initial = game.status()["active_status"]["unwatered_crops"]
        game.transfer_control("operator")
        game.transfer_control("studio")
        await tasks.control(identity, 1, "approve", step_id=step["id"])
        await until(lambda: tasks.current(identity)["status"] == "failed")
        assert "控制权已变化" in tasks.current(identity)["steps"][-1]["result"]["error"]
        assert game.status()["active_status"]["unwatered_crops"] == initial

        identity = str(uuid4())
        tasks.create(identity, "请给星露谷作物浇水")
        await until(lambda: tasks.current(identity)["status"] == "awaiting_approval")
        step = tasks.current(identity)["steps"][-1]
        await tasks.control(identity, 1, "approve", step_id=step["id"])
        await until(lambda: tasks.current(identity)["status"] == "completed")
        proof = tasks.current(identity)["steps"][-1]["result"]
        assert proof["success"]
        game.handoff(str(uuid4()), "stardew_valley", "eat_snack")
        assert game.handoff(step["id"], "stardew_valley", "water_crops") == proof


async def test_owner_lookup_routes_to_bound_uid(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    original = runtime.character
    binding = BilibiliOwner(uid="123", name="绑定对象")
    monkeypatch.setattr(type(runtime), "character", property(lambda self: original | {"bilibili_owner": binding.model_dump()}))

    async def profile(uid):
        assert uid == "123"
        return binding

    async def decide(messages, schema, *, max_tokens, on_content, on_stage):
        assert "owner_profile" in schema["properties"]["action"]["enum"]
        return {"action": "owner_profile"}

    monkeypatch.setattr(runtime.account, "owner_profile", profile)
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", decide, raising=False)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54000))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as owner:
        app.state.browsing.discovery.remember_account("200")
        result = await owner.post("/api/chat/intelligent", json={
            "request_id": str(uuid4()), "message": "去看看你主人的 B 站账号",
        })
        assert result.json()["card"]["data"]["url"] == "https://space.bilibili.com/123", result.text


def test_agent_live_handoff_requires_approval_and_actual_playback(settings, monkeypatch):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    runtime = app.state.runtime
    spoken = "这段话现在才真的播完。"

    async def decide(goal, steps, tools):
        if not steps:
            return TaskDecision(action="tool", tool="live_handoff", arguments={"text": "节目要讨论窗边风铃。"})
        assert steps[-1]["result"] == {"status": "completed", "spoken": spoken}
        return TaskDecision(action="finish", message="已收到实际播放确认。")

    async def generate(messages, character, event_id, source_text):
        return Reply(spoken_text=spoken)

    async def synthesize(text, voice_profile):
        return Audio(settings.audio / "agent-test.wav", 1, text)

    monkeypatch.setattr(runtime.tasks, "decide", decide)
    monkeypatch.setattr(runtime.tasks, "make_plan", None)
    monkeypatch.setattr(runtime.llm, "generate", generate)
    monkeypatch.setattr(runtime.voices, "synthesize_live", synthesize)
    identity = str(uuid4())
    url = f"/api/agent/tasks/{identity}"
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 54000)) as client:
        client.cookies.set("tuber_overlay", settings.overlay_token)
        client.post("/api/live/start").raise_for_status()
        client.post("/api/agent/tasks", json={"request_id": identity, "goal": "在本场节目谈风铃"}).raise_for_status()
        for _ in range(100):
            waiting = client.get(url).json()
            if waiting["status"] == "awaiting_approval":
                break
            time.sleep(0.02)
        assert waiting["status"] == "awaiting_approval"
        assert not app.state.store.one("SELECT 1 FROM events WHERE json_extract(payload,'$.platform')='operator_handoff'")
        with client.websocket_connect("/ws/overlay") as player:
            assert player.receive_json()["type"] == "hello"
            pending = waiting["steps"][-1]
            client.patch(url, json={"revision": 1, "action": "approve", "step_id": pending["id"]}).raise_for_status()
            play = player.receive_json()
            assert play["type"] == "play" and play["text"] == spoken
            before_ack = client.get(url).json()
            assert not before_ack["message"] and "result" not in before_ack["steps"][-1]
            ack = {key: play[key] for key in ("job_id", "playback_token")}
            player.send_json(ack | {"type": "started"})
            player.send_json(ack | {"type": "ended"})
            for _ in range(120):
                finished = client.get(url).json()
                if finished["status"] == "completed":
                    break
                time.sleep(0.03)
            assert finished["status"] == "completed"
            assert "播放器确认播完" in finished["message"] and spoken in finished["message"]
    assert platform.sent == []
