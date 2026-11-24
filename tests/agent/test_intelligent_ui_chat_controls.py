import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.support.operations_helpers import BV, PNG, ControlledPlatform, TestVisual
from tuber_studio.agent.chat.lookup import operator_facts
from tuber_studio.agent.chat.lookup.operator_task_patch import TaskPatch, grounded_patch, patch_vocabulary
from tuber_studio.app import create_app
from tuber_studio.core.identity import provider_identity_messages
from tuber_studio.core.models import Reply
from tuber_studio.infra.inference.providers import Audio
from tuber_studio.live.games.game_livestream import GameLivestreamService


def test_operations_card_refreshes_permissions_and_confirmation(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    async def model(messages, schema, *, max_tokens):
        return {"action": "chat"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"
        control_turn = {"request_id": str(uuid4()), "message": "调整自主运营设置", "intent": {"action": "operation_control"}}
        control = client.post(path, json=control_turn).json()["card"]
        assert control["type"] == "operation_control" and control["data"]["policy"]["enabled"] is False
        updated = client.put("/api/v1/operations/automation/settings", json={"reply_enabled": True}).json()
        current = client.get(path + "/turns/" + control_turn["request_id"]).json()["card"]["data"]
        assert current["permissions"] == updated["permissions"] and current["permissions"]["replies"] == "review"
        assert client.post(path + f"/turns/{control_turn['request_id']}/confirm/save", json={}).status_code == 200
        assert client.post(path + f"/turns/{control_turn['request_id']}/refresh-confirmation").status_code == 200
        started = client.post(path, json={"request_id": str(uuid4()), "message": "启动自主运营", "intent": {"action": "start_operations"}}).json()
        assert started["card"]["type"] == "operation_control" and started["card"]["data"]["policy"]["enabled"] is True


def test_semantic_chat_binds_displayed_event_order_and_memory_version(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    events = [
        {"bvid": BV, "title": name, "source": {"kind": "comment", "text": quote, "author": name, "uid": str(index),
         "href": f"https://www.bilibili.com/video/{BV}#reply{index}"}, "reply": {"state": "confirmed", "text": "知道了"}}
        for index, name, quote in [(1, "甲", "第一句"), (2, "乙", "第二句")]
    ]
    monkeypatch.setattr(operator_facts, "daily_events", lambda *args: [event.copy() for event in events])

    async def model(messages, schema, *, max_tokens):
        assert schema["title"] == "EventSelection"
        return {"items": [2, 1]}

    async def assistant(messages, schema, **options):
        if schema["title"] == "FrontDialogueReply":
            sys_msg = messages[-2]["content"]
            marker = "以下是本轮已经取得的读取回执："
            if marker in sys_msg:
                summary = json.loads(sys_msg.split(marker, 1)[1])["summary"]
                return {"reply_markdown": summary}
            return {"reply_markdown": "这就来。"}
        assert schema["title"] == "TurnRoute"
        message = json.loads(messages[-1]["content"])["message"]
        action = {"今天有什么趣事": "activity", "第二件是谁讲的": "event_author", "后来回了没": "event_reply",
                  "把它记成公开经历": "remember_event", "那件公开经历": "recall_experience",
                  "查看公开经历": "public_experiences", "你记得我什么": "memory_summary",
                  "现在去吃饭吧": "chat", "再查第一句是谁说的": "event_author"}[message]
        if action == "chat":
            return {"route": "chat", "action": "chat"}
        route_name = "direct_action" if action == "remember_event" else "instant_read"
        return {
            "route": route_name,
            "action": action,
            "position": 2 if message.startswith("第二件") else None,
            "topic": "第一句" if action == "recall_experience" or message.startswith("再查") else "",
        }

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", assistant, raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"
        app.state.browsing.discovery.remember_account("100")

        def send(text):
            request = str(uuid4())
            response = client.post(path, json={"request_id": request, "message": text})
            assert response.status_code == 200, response.text
            return request, response.json()

        _, activity = send("今天有什么趣事")
        assert [item["title"] for item in activity["card"]["data"]["items"]] == ["乙", "甲"]
        events.reverse()
        assert len(client.get(path).json()["turns"]) == 1
        assert send("第二件是谁讲的")[1]["reply_text"] == "是甲说的（UID 1）。"
        assert send("后来回了没")[1]["card"]["data"]["event"]["reply"]["text"] == "知道了"
        proposal, turn = send("把它记成公开经历")
        assert turn["card"]["data"]["text"] == "第一句"
        memory_path = "/api/v1/persona/daily-memory"
        saved, = client.get(memory_path).json()["organized"]
        assert saved["source"]["references"][0]["text"] == "第一句"
        assert saved["visibility"] == "shared"
        summary_id, turn = send("你记得我什么")
        assert turn["card"]["data"]["groups"] == client.get(memory_path).json()["summary"]
        listing, turn = send("查看公开经历")
        assert turn["card"]["data"]["items"] == [{
            "headline": saved["headline"], "anecdote": "第一句", "links": [events[1]["source"]["href"]],
        }]
        assert any(source["id"] == f"organized:{saved['id']}" for source in app.state.dynamics._sources())
        app.state.browsing.discovery.remember_account("200")
        assert not client.get(memory_path).json()["organized"]
        app.state.browsing.discovery.remember_account("100")
        recalled, turn = send("那件公开经历")
        assert turn["reply_text"] == "第一句" and turn["memory_dependent"]
        assert client.delete(f"{memory_path}/organized/{saved['id']}").status_code == 200
        assert client.get(f"{path}/turns/{summary_id}").json()["card"]["data"]["groups"] == []
        assert client.get(f"{path}/turns/{listing}").json()["card"]["data"]["items"] == []
        assert client.get(f"{path}/turns/{recalled}").json()["reply_text"] == "这条回复引用的记忆已变更。"
        assert send("第二件是谁讲的")[1]["reply_text"].startswith("是甲说的")
        send("现在去吃饭吧")
        assert send("后来回了没")[1]["reply_text"] == "你指的是哪一件事？"
        assert send("再查第一句是谁说的")[1]["reply_text"].startswith("是甲说的")

        legacy_request = str(uuid4())
        runtime = app.state.runtime
        runtime.store.execute(
            "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
            (legacy_request, runtime.character["id"], runtime.character["version"], "记下这件事", json.dumps({
                "account_id": "100", "reply_text": "请确认这件事。",
                "card": {"type": "experience_proposal", "data": {"event": events[1], "fact": "早期草稿"}},
            }), time.time()),
        )
        assert client.get(f"{path}/turns/{legacy_request}").json()["card"] == {
            "type": "operation_event", "data": {"event": events[1]},
        }
        app.state.browsing.discovery.remember_account("200")
        assert client.get(f"{path}/turns/{proposal}").json()["card"] is None
        assert client.get(f"{path}/turns/{legacy_request}").json()["card"] is None


def test_live_proposal_runs_existing_control_once_and_demo_shortcut_remains_available(settings, monkeypatch):
    settings.data["features"]["live_enabled"] = True
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    actions = []
    original = runtime.control

    async def control(action):
        actions.append(action)
        return await original(action)

    monkeypatch.setattr(runtime, "control", control)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"
        request = {"request_id": str(uuid4()), "message": "开始直播"}
        assert client.post(path, json=request).status_code == 503
        proposed = client.post(path, json=request | {"intent": {"action": "start_live"}}).json()
        assert proposed["card"]["data"]["state"] == "stopped" and actions == []
        url = f"{path}/turns/{request['request_id']}/confirm/live"
        assert client.post(url, json={}).json()["card"]["data"]["state"] == "live"
        assert client.post(url, json={}).status_code == 200
        assert actions.count("start") == 1

        async def model(messages, schema, *, max_tokens):
            return {"action": "stop"}

        monkeypatch.setattr(runtime.llm, "generate_json", model)
        runtime.operations_waiting = lambda: True
        stopped = client.post(path, json={"request_id": str(uuid4()), "message": "停一下"})
        assert stopped.json()["card"]["data"]["state"] == "stopped"


@pytest.mark.parametrize("stream_available", [True, False])
def test_precise_video_chat_uses_bound_part_and_reports_fallback(settings, monkeypatch, stream_available):
    class Platform(ControlledPlatform):
        async def video_metadata(self, video):
            metadata = await super().video_metadata(video)
            return metadata | {"parts": metadata["parts"] + [{"cid": "22", "title": "第二P", "duration": 180}]}

        async def video_stream_frame(self, metadata, *, cid, timestamp):
            assert cid == "22" and timestamp == 137.4
            self.calls.append("stream")
            if not stream_available:
                raise ValueError("播放来源未按请求返回有效 HTTP Range")
            return {"id": "frame-22", "cid": cid, "kind": "stream", "requested_time": timestamp,
                    "actual_time": 137.383, "presentation_end": 137.417, "width": 1, "height": 1, "image": PNG}

        async def video_frames(self, metadata, count, *, cid, timestamps):
            assert cid == "22" and timestamps == [137.4]
            self.calls.append("storyboard")
            return [{"id": "board-22", "cid": cid, "kind": "storyboard", "actual_time": 135,
                     "width": 1, "height": 1, "image": PNG}]

    platform, vision = Platform(), TestVisual()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: vision)
    app.state.browsing.read_interval = 0

    async def intent(messages, schema, *, max_tokens):
        return {"action": "inspect_video_frame", "part": 2, "seconds": 137}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", intent)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"
        for _ in range(2 if stream_available else 1):
            identity = str(uuid4())
            queued = client.post(path, json={"request_id": identity, "message": f"看一下 {BV} P2 的 02:17.400"})
            assert queued.status_code == 200, queued.text
            assert queued.json()["card"]["data"]["selection"]["frame"]["seconds"] == 137.4
            for _ in range(200):
                data = client.get(f"{path}/turns/{identity}").json()["card"]["data"]
                if data["status"] not in {"waiting", "queued", "running"}:
                    break
                time.sleep(0.01)
            assert data["status"] == "completed", data
            assert data["inspection"]["requested_time"] == 137.4
            assert data["inspection"]["actual_time"] == (137.383 if stream_available else 135)
            assert data["inspection"]["kind"] == ("stream" if stream_available else "storyboard")
            assert bool(data["inspection"].get("fallback_reason")) is not stream_available
            assert "preview" not in data["inspection"]
            picture = client.get(f"{path}/turns/{identity}/inspection-image")
            assert picture.status_code == 200 and picture.content == PNG
        assert platform.calls.count("stream") == vision.calls == 1
        assert platform.calls.count("storyboard") == (0 if stream_available else 1)
        assert not platform.sent


def test_specified_video_reuses_range_checkpoints_and_reconnects_under_same_id(settings, monkeypatch):
    class MultipartPlatform(ControlledPlatform):
        hold_second = True

        async def video_metadata(self, video):
            metadata = await super().video_metadata(video)
            return metadata | {"parts": metadata["parts"] + [{"cid": "22", "title": "第二P", "duration": 30}]}

        async def video_frames(self, metadata, count, *, cid, timestamps):
            if self.hold_second and cid == "22":
                await asyncio.Event().wait()
            return [
                {"id": f"{cid}-{index}", "cid": cid, "actual_time": timestamp,
                 "width": 1, "height": 1, "image": PNG, "kind": "storyboard"}
                for index, timestamp in enumerate(timestamps[:count])
            ]

    platform, vision = MultipartPlatform(), TestVisual()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: vision)
    browsing = app.state.browsing
    browsing.read_interval = 0
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"
        request = {"request_id": str(uuid4()), "message": "看完整视频：" + BV, "intent": {"action": "full_video"}, "video_selection": {
            "bvid": BV, "ranges": [{"part": 1, "start": 0, "end": 5}, {"part": 2, "start": 3, "end": 20}],
        }}
        turn_url = path + "/turns/" + request["request_id"]
        queued = client.post(path, json=request)
        assert queued.status_code == 200, queued.text
        assert queued.json()["card"]["data"]["selection"]["ranges"][1]["part"] == 2

        def until(predicate):
            for _ in range(200):
                data = client.get(turn_url).json()["card"]["data"]
                if predicate(data):
                    return data
                time.sleep(0.01)
            pytest.fail(f"视频任务没有进入期望状态：{data}")

        first = until(lambda data: data["coverage"] and data["coverage"]["parts"][0]["ranges"][0]["visual"])
        assert first["status"] == "running"
        waiting_request = request | {"request_id": str(uuid4()), "video_selection": {"bvid": BV}}
        waiting_url = path + "/turns/" + waiting_request["request_id"]
        assert client.post(path, json=waiting_request).json()["card"]["data"]["status"] == "waiting"
        assert client.post(waiting_url + "/cancel-video").json()["card"]["data"]["status"] == "cancelled"
        assert client.post(waiting_url + "/resume-video").json()["card"]["data"]["status"] == "waiting"
        assert client.post("/api/v1/operations/automation/stop", json={"emergency": True}).json()["state"] == "emergency"
        assert client.get(waiting_url).json()["card"]["data"]["status"] == "paused"
        cancelled = client.get(turn_url).json()["card"]["data"]
        assert cancelled["status"] == "cancelled" and cancelled["can_resume"]
        assert client.post(path, json=request).json()["card"]["data"]["status"] == "cancelled"
        assert client.post(path, json=request | {"video_selection": {"bvid": BV}}).status_code == 409
        assert client.post(turn_url + "/resume-video").status_code == 400
        assert client.post("/api/v1/operations/automation/reset-stop").status_code == 200
        platform.hold_second = False
        assert client.post(turn_url + "/resume-video").status_code == 200
        result = until(lambda data: data["status"] == "completed")
        first, second = [part["ranges"][0] for part in result["coverage"]["parts"]]
        assert first["subtitle"]["covered_seconds"] == 5 and first["subtitle"]["track_complete"]
        assert second["subtitle"]["covered_seconds"] == 7 and second["subtitle"]["gap_seconds"] == 10
        assert first["visual"]["state"] == second["visual"]["state"] == "sampled"
        assert len(first["visual"]["samples"]) == 1 and len(second["visual"]["samples"]) == 2
        assert result["phase"] == "complete" and result["comments"]["at_end"]
        assert second["audio"]["state"] == "unavailable"
        assert platform.calls.count(("subtitles", BV, "11")) == 1
        assert platform.calls.count(("subtitles", BV, "22")) == 1
        assert vision.calls == 2 and platform.sent == []
        assert app.state.store.one("SELECT COUNT(*) AS n FROM browsing_tasks WHERE round_id=?", (result["id"],))["n"] == 1
        browsing.bilibili_account.verified_account_id = "200"
        assert client.get(turn_url).json()["card"]["data"]["status"] == "unavailable"


@pytest.mark.parametrize("case", ["success", "private-fragment", "account-switch"])
def test_operator_discussion_to_dynamic_uses_only_sanitized_material(settings, monkeypatch, case):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    runtime = app.state.runtime
    app.state.browsing.discovery.remember_account("100")
    private_name, private_url = "样例甲", "https://internal.invalid/fixture"
    safe = "用两盏小灯试出画面层次。"

    async def assistant(messages, schema, **options):
        if schema["title"] == "TurnRoute":
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        return {"reply_markdown": "可以先开一盏暖光，再用冷光补轮廓。"}

    async def sanitize(messages, schema, *, max_tokens):
        packet = json.loads(messages[-1]["content"])
        assert schema["title"] == "PublicText" and packet["用途"] == "动态"
        assert private_name in json.dumps(packet, ensure_ascii=False)
        assert any("补轮廓" in turn.get("content", "") for turn in packet["资料"])
        return {"text": f"{private_name}说：{safe}" if case == "private-fragment" else safe}

    async def draft(messages, schema, *, max_tokens):
        packet = json.loads(messages[1]["content"])
        source, = packet["public_sources"]
        assert source["text"] == safe
        wire = json.dumps(messages, ensure_ascii=False)
        assert private_name not in wire and private_url not in wire and "补轮廓" not in wire
        if case == "account-switch":
            app.state.browsing.discovery.remember_account("200")
        return {"action": "post", "reason": "想分享布光", "content": "小窝的两盏灯，各管一半层次。",
                "source_ids": [source["id"]], "next_evaluation_seconds": 1800}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", assistant, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_operator_json", sanitize, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_json", draft)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        client.cookies.set("tuber_overlay", settings.overlay_token)
        path = "/api/v1/agent/chat"
        client.post(path, json={"request_id": str(uuid4()), "message": f"姓名：{private_name}，聊聊布光 {private_url}"}).raise_for_status()
        command = {"request_id": str(uuid4()), "message": "把刚才聊的布光写成动态",
                   "intent": {"action": "dynamic", "source": "discussion"}}
        response = client.post(path, json=command)
        if case != "success":
            if case == "private-fragment":
                assert response.raise_for_status().json()["card"] is None
            else:
                assert response.status_code == 400 and "账号或角色已变化" in response.text
            assert app.state.dynamics.list_posts() == [] and platform.sent == []
            return
        result = response.raise_for_status().json()
        assert result["card"]["type"] == "dynamic_draft"
        assert client.post(path, json=command).raise_for_status().json() == result
        post, = app.state.dynamics.list_posts()
        assert post["sources"][0]["text"] == safe and post["comment_card"] is None
        assert post["status"] == "pending_review" and platform.sent == []


def test_operator_video_to_live_uses_only_handoff_text_and_player_receipt(settings, monkeypatch):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: TestVisual())
    runtime, browsing = app.state.runtime, app.state.browsing
    browsing.read_interval = 0
    browsing.discovery.remember_account("100")
    seen = []
    safe_text = "加盐会提高水的密度，让鸡蛋浮起来。"
    spoken = "结论是，加盐后鸡蛋就浮起来了。"
    name = "验收姓名甲"
    private_url = "http://internal.invalid/secret"

    async def interpret(messages, schema, *, max_tokens):
        wire = json.loads(messages[-1]["content"])
        kind = schema["title"]
        if kind == "ChatIntent":
            action = {
                "本场讲一下这个视频的发现": {"action": "live_handoff", "source": "video"},
                "以后直播都先讲结果": {"action": "live_preference"},
                "忘掉直播先讲结果这条": {"action": "forget_memory", "topic": "直播先讲结果"},
                "本场讲一下刚才讨论的打法": {"action": "live_handoff", "source": "discussion"},
            }
            return action.get(wire["message"], {"action": "chat"})
        if kind == "LivePreferenceSelection":
            return {"preference": "result_first"}
        assert kind == "PublicText"
        seen.append(wire)
        if wire["要求"].endswith("打法"):
            assert name in json.dumps(wire, ensure_ascii=False)
        else:
            assert wire["资料"][0]["seconds"] == 0
            assert name not in json.dumps(wire, ensure_ascii=False)
        return {"text": safe_text}

    async def generate(messages, character, event_id, source_text):
        if source_text != safe_text:
            return Reply(spoken_text="记下了。")
        provider = type("Provider", (), {"identity_resolver": staticmethod(
            lambda: {"account_bio": name, "account_uid": "private-account", "identity_version": "test"}
        )})()
        wire = json.dumps(provider_identity_messages(provider, messages), ensure_ascii=False)
        assert name not in wire and private_url not in wire and BV not in wire
        assert "private-account" not in wire
        assert all(isinstance(message["content"], str) for message in messages)
        seen.append(wire)
        if "先讲结果" in wire:
            return Reply(spoken_text="这句来自删除前的直播偏好。")
        return Reply(spoken_text=spoken)

    async def synthesize(text, voice_profile):
        return Audio(settings.audio / "handoff-test.wav", 1, text)

    monkeypatch.setattr(runtime.llm, "generate_operator_json", interpret, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_operator_json", interpret, raising=False)
    monkeypatch.setattr(runtime.llm, "generate", generate)
    monkeypatch.setattr(runtime.voices, "synthesize_live", synthesize)
    path = "/api/v1/agent/chat"

    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        client.cookies.set("tuber_overlay", settings.overlay_token)

        def send(message, **kwargs):
            req = {"message": message, "request_id": str(uuid4()), **kwargs}
            return req, client.post(path, json=req).raise_for_status().json()

        send(f"姓名：{name}，私下讨论加盐让鸡蛋浮起来，内部链接 {private_url}")
        request, queued = send(f"帮{name}看一下 {BV}，范围是 P1 开头五秒。", intent={"action": "full_video"}, video_selection={
            "bvid": BV, "ranges": [{"part": 1, "start": 0, "end": 5}],
        })
        assert queued["card"]["data"]["selection"]["bvid"] == BV
        turn_url = f"{path}/turns/{request['request_id']}"
        for _ in range(200):
            status = client.get(turn_url).json()["card"]["data"]
            if status["coverage"] and status["coverage"]["parts"][0]["ranges"][0]["highlights"]:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"指定视频没有完成取材：{status}")
        row = browsing.repo.round(request["request_id"])
        assert name not in json.dumps(row, ensure_ascii=False) and private_url not in json.dumps(row)
        assert ("subtitles", BV, "11") in platform.calls and platform.sent == []
        client.post("/api/v1/live/start").raise_for_status()
        send("以后直播都先讲结果")
        req, handoff = send("本场讲一下这个视频的发现")
        assert handoff["card"]["data"]["text"] == safe_text
        assert handoff["card"]["data"]["sources"][0]["seconds"] == 0
        assert client.post(path, json=req).json()["card"]["data"]["job_id"] == handoff["card"]["data"]["job_id"]
        handoff_url = f"{path}/turns/{req['request_id']}"
        for _ in range(100):
            if client.get(handoff_url).json()["card"]["data"]["status"] == "ready_to_play":
                break
            time.sleep(0.01)
        else:
            pytest.fail("待播任务没有完成第一次合成")
        assert any(isinstance(entry, str) and "先讲结果" in entry for entry in seen)
        _, forgotten = send("忘掉直播先讲结果这条")
        assert forgotten["reply_text"] == "已删除这条记忆。"
        with client.websocket_connect("/ws/overlay") as player:
            assert player.receive_json()["type"] == "hello"
            play = player.receive_json()
            assert play["type"] == "play" and play["text"] == spoken
            ack = {key: play[key] for key in ("job_id", "playback_token")}
            player.send_json(ack | {"type": "started"})
            player.send_json(ack | {"type": "ended"})
            for _ in range(100):
                done = client.get(f"{path}/turns/{req['request_id']}").json()["card"]["data"]
                if done["status"] == "completed":
                    break
                time.sleep(0.01)
            assert done["spoken"] == spoken
        client.post("/api/v1/live/stop").raise_for_status()
        client.post("/api/v1/live/start").raise_for_status()
        assert client.get(f"{path}/turns/{req['request_id']}").json()["card"]["data"]["spoken"] == spoken
        _, history = send("刚才有说鸡蛋吗", intent={"action": "live_history", "topic": "鸡蛋"})
        assert [entry["text"] for entry in history["card"]["data"]["items"]] == [spoken]
        assert not runtime.live.queue
        assert any(isinstance(entry, str) and safe_text in entry for entry in seen)

        # The discussion variant sees the named private source only at extraction.
        with client.websocket_connect("/ws/overlay") as player:
            player.receive_json()
            _, discussion = send("本场讲一下刚才讨论的打法")
            assert discussion["card"]["data"]["text"] == safe_text
            assert player.receive_json()["text"] == spoken


def test_game_chat_handoff_uses_save_and_yields_to_operator(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    game = app.state.runtime.live.game_livestream
    path = "/api/v1/agent/chat"
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        def handoff(action, *, request_id=None):
            request_id = request_id or str(uuid4())
            return client.post(path, json={"request_id": request_id, "message": "私聊背景：验收姓名乙和内部链接",
                                          "intent": {"action": "game_handoff", "game_action": action}}).raise_for_status().json()

        original = client.get("/api/v1/live/games/status").json()["active_status"]
        identity = str(uuid4())
        first = handoff("water_crops", request_id=identity)
        assert first["card"]["type"] == "game_handoff"
        assert "验收姓名乙" not in json.dumps(game.last_handoff, ensure_ascii=False)
        watered = client.get("/api/v1/live/games/status").json()["active_status"]
        assert watered["unwatered_crops"] < original["unwatered_crops"]
        assert handoff("water_crops", request_id=identity) == first
        assert client.post("/api/v1/live/games/control", json={"owner": "operator"}).json()["owner"] == "operator"
        assert "号主接管" in handoff("sleep_day")["reply_text"]
        assert client.post("/api/v1/live/games/step").status_code == 400
        assert client.post("/api/v1/live/games/action", json={"action": "sleep_day"}).json()["success"]
        saved = GameLivestreamService(store=app.state.store)
        assert saved.status()["active_status"]["day"] == original["day"] + 1 and saved.owner == "operator"
        client.post("/api/v1/live/games/control", json={"owner": "studio"}).raise_for_status()
        assert handoff("step")["card"]["type"] == "game_handoff"


def test_specified_video_can_resume_when_previously_unavailable_subtitles_appear(settings, monkeypatch):
    from tuber_studio.operations.kernel.evidence import EvidenceUnavailable

    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    browsing = app.state.browsing
    browsing.read_interval = 0
    original = browsing.evidence.subtitle_part
    available = False

    async def subtitles(*args):
        if not available:
            raise EvidenceUnavailable("no_subtitles", "当前没有字幕")
        return await original(*args)

    monkeypatch.setattr(browsing.evidence, "subtitle_part", subtitles)
    path, identity = "/api/v1/agent/chat", str(uuid4())
    turn = f"{path}/turns/{identity}"
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        assert client.post(path, json={"request_id": identity, "message": "看完整视频：" + BV,
                                      "intent": {"action": "full_video"}}).status_code == 200

        def wait_for(status):
            for _ in range(200):
                data = client.get(turn).json()["card"]["data"]
                if data["status"] == status:
                    return data
                time.sleep(0.01)
            pytest.fail(f"视频任务没有进入 {status}：{data}")

        partial = wait_for("partial")
        assert partial["coverage"]["parts"][0]["ranges"][0]["subtitle"]["state"] == "unavailable"
        available = True
        assert client.post(turn + "/resume-video").status_code == 200
        resumed = wait_for("completed")
        assert resumed["id"] == partial["id"] == identity
        assert resumed["coverage"]["parts"][0]["ranges"][0]["subtitle"]["state"] == "available"

@pytest.mark.parametrize(("operation", "quote", "expected", "revision"), [
    ("revise", "另挑一段", "queued", 2),
    ("cancel", "停止这项交办", "cancelled", 2),
    ("cancel", "另一个人的指令", "queued", 1),
    ("revise", "", "queued", 1),
])
async def test_opened_edition_routes_once_with_current_instruction(settings, monkeypatch, operation, quote, expected, revision):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    path = "/api/v1/agent/chat"
    identity = str(uuid4())
    message = "原定的素材，另挑一段。" if operation == "revise" else "这项交办，停止这项交办，已有的稿子留着。"
    coord = {"task_revision": 1, "edition": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "message": "挑选两段素材",
                                       "intent": {"action": "agent_task"}})).raise_for_status()
        with runtime.store.db as db:
            runtime.tasks.workspace.record_delivery(db, runtime.tasks.current(identity), "这是首版。")

        async def route(messages, schema, **options):
            assert schema["title"] == "TurnRoute" and options["on_content"] is None
            packet = json.loads(messages[-1]["content"])
            assert packet["message"] == message
            assert packet["opened_artifact"] == {"id": identity, **coord}
            assert [card["id"] for card in packet["recent_task_cards"]] == [identity]
            assert not packet.get("history_task_cards")
            return {
                "route": "steer_task",
                "action": "cancel_task" if operation == "cancel" else "steer_task",
                "task_id": identity,
                "task_effect": operation,
                "change_quote": quote,
            }

        monkeypatch.setattr(runtime.llm, "generate_assistant_json", route, raising=False)
        payload = {"request_id": str(uuid4()), "message": message, "opened_artifact": {"task_id": identity, **coord}}
        invalid = await client.post(path, json=payload | {"opened_artifact": {**payload["opened_artifact"], "edition": 2}})
        assert invalid.status_code == 409
        for _ in range(2):
            response = await client.post(path, json=payload)
            assert response.status_code == 200, response.text
        task = runtime.tasks.current(identity)
        assert (task["status"], task["revision"]) == (expected, revision)
        if revision == 2 and operation == "revise":
            assert "引用成果 r1 v1" in task["goal"] and task["goal"].endswith(message)
        assert runtime.tasks.workspace.latest(identity)["body"] == "这是首版。"

@pytest.mark.parametrize("bad_route", [False, True])
async def test_unopened_running_task_cannot_absorb_unrelated_conversation(settings, monkeypatch, bad_route):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    identity = str(uuid4())
    goal = "为下场直播从视频取两个片段，保存提纲，写两句引言。"
    path = "/api/v1/agent/chat"
    conversation = "辛苦啦，跟我聊两句吧。"

    async def generate(messages, schema, **options):
        if schema["title"] == "FrontDialogueReply":
            return {"reply_markdown": "我在呢，今天聊点什么好？"}
        assert schema["title"] == "TurnRoute"
        if bad_route:
            return {
                "route": "steer_task",
                "action": "steer_task",
                "task_id": identity,
                "task_effect": "revise",
                "change_quote": "两句",
            }
        return {"route": "chat", "action": "chat"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "message": goal, "intent": {"action": "agent_task"}})).raise_for_status()
        runtime.store.db.execute("UPDATE agent_tasks SET status='running' WHERE id=?", (identity,))
        monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
        reply = (await client.post(path, json={"request_id": str(uuid4()), "message": conversation})).raise_for_status().json()
        task = runtime.tasks.current(identity)
        assert reply["card"] is None and reply["reply_text"] == "我在呢，今天聊点什么好？"
        assert (task["goal"], task["revision"], task["status"]) == (goal, 1, "running")

async def test_disputed_new_task_needs_an_independent_deliverable():
    from types import SimpleNamespace

    from tuber_studio.agent.chat.lookup.operator_task_patch import patch_vocabulary, review_change

    card = {"goal": "备两段素材", "editable": {"goal": "备两段素材", "item:1": "甲", "item:2": "乙"}}
    message = "假设保留第一段、换掉第二段，你怎么看？先别改。"
    choices, bindings = patch_vocabulary([card], message)

    async def check(**request):
        assert [tool["name"] for tool in request["tools"]] == ["record_review"]
        assert request["tool_choice"] == "required" and "previous_response_id" not in request
        packet = json.loads(request["input"][0]["content"])
        assert packet["owner_message"] == message and packet["omni_proposal"] == "new_work"
        return SimpleNamespace(calls=[SimpleNamespace(name="record_review", arguments=assessment)])

    assessment = {"operation": "answer", "changes": {field: "沿用现有内容" for field, _ in choices if field.startswith("item:")},
                  "separate_deliverable": ""}
    assert await review_change(check, message, card, "new_work", choices, bindings) == ("discuss", None)
    assessment.update(operation="new_task", separate_deliverable="换掉第二段")
    assert await review_change(check, message, card, "new_work", choices, bindings) == ("clarify", None)
    assessment.update(operation="edit_existing", changes=["unknown"])
    with pytest.raises(ValueError, match="未知选项"):
        await review_change(check, message, card, "new_work", choices, bindings)


def test_revision_binds_new_requirement_after_existing_deliverable():
    card = {"editable": {"goal": "私下准备一份直播状态交接便笺。"}}
    source = "继续刚才的那份便笺，改为只检查自主运营是否启动，写进同一份便笺。"
    choices, _ = patch_vocabulary([card], source)
    requirement = "改为只检查自主运营是否启动，写进同一份便笺"
    assert choices["goal", "便笺"] == {requirement, "写进同一份"}
    assert grounded_patch(TaskPatch(field="goal", subject="便笺", requirement=requirement), card, source)
