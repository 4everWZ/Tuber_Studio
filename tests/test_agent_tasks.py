import asyncio
import time
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_browsing import BV, ControlledPlatform

from tuber_studio.agent.task_runtime import TaskDecision, TaskRuntime, Tool, ToolWait
from tuber_studio.app import create_app
from tuber_studio.models import BilibiliOwner, Reply, StrictModel
from tuber_studio.providers import Audio
from tuber_studio.store import Store


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
        assert second.current(identity)["message"] == "核验完成。"
        assert len(calls) == 2 and calls[0][0] == calls[1][0]
        assert calls[-1][1] == {"remote_id": "receipt"}
    finally:
        await second.close()
        store.close()


async def test_chat_delegates_across_existing_services_and_cancels_video(settings, monkeypatch):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    tasks = app.state.runtime.tasks
    calls = ["operation_status", "live_status", "game_status", "inspect_video"]
    seen = []

    async def decide(goal, steps, tools):
        seen.extend(step["result"] for step in steps[-1:])
        if goal == "只返回最终结论":
            return TaskDecision(action="finish", message="已按新要求继续。")
        tool = calls[len(steps)]
        return TaskDecision(action="tool", tool=tool, arguments={"bvid": BV} if tool == "inspect_video" else {})

    monkeypatch.setattr(tasks, "decide", decide)
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
        assert (await owner.get(activity_url)).json()["message"] == "已按新要求继续。"
        app.state.browsing.discovery.remember_account("200")
        assert (await owner.get(activity_url)).status_code == 404


async def test_approved_game_step_checks_control_and_replays_receipt(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    runtime.settings.data["features"]["live_enabled"] = True
    game, tasks = runtime.live.game_livestream, runtime.tasks

    async def decide(goal, steps, tools):
        if not steps:
            return TaskDecision(action="tool", tool="game_action",
                                arguments={"game": "stardew_valley", "action": "water_crops"})
        return TaskDecision(action="finish", message="游戏已返回实际结果。")

    monkeypatch.setattr(tasks, "decide", decide)
    async with app.router.lifespan_context(app):
        identity = str(uuid4())
        tasks.create(identity, "请给星露谷作物浇水")
        await until(lambda: tasks.current(identity)["status"] == "awaiting_approval")
        step = tasks.current(identity)["steps"][-1]
        initial = game.status()["active_status"]["unwatered_crops"]
        game.transfer_control("operator")
        game.transfer_control("studio")
        await tasks.control(identity, 1, "approve", step_id=step["id"])
        await until(lambda: tasks.current(identity)["status"] == "completed")
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
            assert finished["message"] == "已收到实际播放确认。"
    assert platform.sent == []
