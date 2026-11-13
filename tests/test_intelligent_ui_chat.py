import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_browsing import BV, ControlledPlatform
from test_browsing_evidence import PNG, TestVisual

from tuber_studio.agent import operator_facts
from tuber_studio.app import create_app
from tuber_studio.operations.browsing.models import BrowsingPolicy
from tuber_studio.operations.browsing.repository import BrowsingRepository


async def test_stop_generation_cancels_only_the_bound_chat_and_releases_model(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, browsing = app.state.runtime, app.state.browsing
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original = runtime.llm.generate

    async def wait_for_stop(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime.llm, "generate", wait_for_stop)
    path = "/api/chat/intelligent"
    request_id = str(uuid4())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1",
    ) as client:
        browsing.discovery.remember_account("100")
        request = asyncio.create_task(client.post(path, json={
            "request_id": request_id, "message": "你好呀", "intent": {"action": "chat"},
        }))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            url = f"{path}/turns/{request_id}/stop-generation"
            browsing.discovery.remember_account("200")
            assert (await client.post(url)).json() == {"stopping": False}
            assert not cancelled.is_set()
            browsing.discovery.remember_account("100")
            assert (await client.post(url)).json() == {"stopping": True}
            assert (await asyncio.wait_for(request, 3)).json() == {"stopped": True}
            assert cancelled.is_set() and not runtime.generation_lock.locked()
            assert (await client.get(path)).json()["turns"] == []
            assert (await client.post(url)).json() == {"stopping": False}
            monkeypatch.setattr(runtime.llm, "generate", original)
            followup = await client.post(path, json={
                "request_id": str(uuid4()), "message": "还在吗", "intent": {"action": "chat"},
            })
            assert followup.status_code == 200 and followup.json()["reply_text"]
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)


def test_preview_request_survives_completion_and_policy_edits(store):
    repo = BrowsingRepository(store)
    char, request = store.character(), str(uuid4())
    selection = {"video": None, "keywords": None}
    config = BrowsingPolicy().model_dump() | {"operator_selection": selection}
    result, created = repo.create_round("900", char, config, 1, True, request_id=request)
    assert created and result["id"] == request
    store.execute("UPDATE browsing_rounds SET status='completed' WHERE id=?", (request,))
    repeated, created = repo.create_round("900", char, config | {"sources": ["popular"]}, 2, True, request_id=request)
    assert not created and repeated["status"] == "completed"
    with pytest.raises(ValueError, match="其他浏览轮次"):
        repo.requested_preview(request, char, selection | {"keywords": ["其他视频"]}, "900")


def test_chat_persists_private_turns_and_resolves_real_task_cards(settings, monkeypatch):
    app = create_app(settings)
    runtime, store = app.state.runtime, app.state.store
    writes = []
    packets = []
    private_packets = []
    generate = runtime.llm.generate

    async def private_model(messages, character, event_id, source_text=None):
        private_packets.append(messages)
        return await generate(messages, character, event_id, source_text)

    async def dynamic_model(messages, schema, *, max_tokens):
        if schema.get("title") == "ChatIntent":
            message = json.loads(messages[1]["content"])["message"]
            return {"action": "live_status" if message == "准备开播" else "chat"}
        source = json.loads(messages[1]["content"])["public_sources"][0]
        packets.append(messages)
        return {
            "action": "post", "reason": "分享角色的一件小事",
            "content": "海底小剧场，今天白饭蒸得正好。", "source_ids": [source["id"]],
            "next_evaluation_seconds": 1800,
        }

    async def fake_preview(**params):
        writes.append(params)
        return {"id": params["request_id"], "status": "queued", "preview": True}

    monkeypatch.setattr(runtime.llm, "generate_json", dynamic_model)
    monkeypatch.setattr(runtime.llm, "generate", private_model)
    monkeypatch.setattr(app.state.browsing, "run", fake_preview)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        chat_id = str(uuid4())
        chat = {"request_id": chat_id, "message": "私聊专用内容 xyz123"}
        response = client.post(path, json=chat)
        assert response.status_code == 200, response.text
        assert response.json()["private"]
        assert client.post(path, json=chat).json()["private"]
        assert len(client.get(path).json()["turns"]) == 1
        assert len(private_packets) == 1
        assert "xyz123" not in json.dumps(app.state.dynamics._sources())
        assert client.post(path, json=chat | {"message": "另一个内容"}).status_code == 409
        assert client.post(path, json={"request_id": str(uuid4()), "message": "还记得吗"}).status_code == 200
        assert "xyz123" in str(private_packets[-1])

        selected = client.post(f"{path}/{chat_id}/public-experience", json={"fact": "海底小屋放了一盆草"})
        assert selected.status_code == 200
        sources = app.state.dynamics._sources()
        assert [source["text"] for source in sources] == ["海底小屋放了一盆草"]

        dynamic = {"request_id": str(uuid4()), "message": "发个动态：聊白饭", "intent": {"action": "dynamic", "topic": "聊白饭"}}
        card = client.post(path, json=dynamic).json()["card"]
        post_id = card["data"]["id"]
        assert client.post(path, json=dynamic).json()["card"]["data"]["id"] == post_id
        assert store.one("SELECT status FROM dynamic_posts WHERE id=?", (post_id,))["status"] == "pending_review"
        assert "xyz123" not in str(packets)
        response = client.post(f"/api/dynamics/posts/{post_id}/publish", json={"text": "换成这句白饭。"})
        assert response.status_code == 400  # this isolated app has no platform credentials
        assert app.state.dynamics._post(post_id)["content"] == "换成这句白饭。"

        browse = {"request_id": str(uuid4()), "message": "去刷视频：原神", "intent": {"action": "browse", "topic": "原神"}}
        assert client.post(path, json=browse).json()["card"]["data"]["preview"]
        assert client.post(path, json=browse).json()["card"]["data"]["preview"]
        assert writes == [dict(preview=True, video_only=True, request_id=browse["request_id"], video=None, keywords=["原神"])]
        live = client.post(path, json={"request_id": str(uuid4()), "message": "准备开播"}).json()["card"]
        assert live["data"]["state"] == runtime.live.state
        assert client.delete(path).json()["ok"]
        assert client.get(path).json()["turns"] == []
        assert not store.one("SELECT 1 FROM grounded_memory WHERE scope LIKE 'operator:%'")
        assert not store.one("SELECT 1 FROM context_windows WHERE mode LIKE 'operator:%'")
        assert app.state.dynamics._post(post_id)["id"] == post_id
        assert app.state.dynamics._sources() == sources


def test_chat_history_cursor_and_activity_are_bound_to_current_scope(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    store, browsing = app.state.store, app.state.browsing
    char = app.state.runtime.character
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        requests = sorted(str(uuid4()) for _ in range(3))
        for request in requests:
            store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
                request, char["id"], char["version"], "测试", '{"reply_text":"应答","private":true}', 1,
            ))
        page = client.get(path, params={"limit": 2}).json()
        assert (page["character_id"], page["character_version"]) == (char["id"], char["version"])
        assert [turn["request_id"] for turn in page["turns"]] == requests[1:]
        before = page["next_cursor"]
        previous = client.get(path, params={"limit": 2, "before": before}).json()
        assert [turn["request_id"] for turn in previous["turns"]] == requests[:1]
        assert previous["next_cursor"] is None
        assert client.get(path, params={"before": str(uuid4())}).status_code == 404

        browsing.discovery.remember_account("100")
        round_, _ = browsing.repo.create_round("100", char, browsing.policy(), 1, False)
        task_id = browsing.repo.task(round_["id"], {"bvid": BV})
        assert browsing.repo.quotas.start_watch(task_id, "100", char["id"], BV, 0)
        assert browsing.repo.quotas.start_watch("second-attempt", "100", char["id"], BV, 0)
        assert browsing.repo.quotas.start_watch("other-account", "200", char["id"], BV, 0)
        store.execute(
            "UPDATE browsing_tasks SET status='viewed_without_comment',material_key='valid',payload=? WHERE id=?",
            ('{"coverage":"all_available_cc","missing_parts":[]}', task_id),
        )
        query = {"request_id": str(uuid4()), "message": "今天刷了多少视频？", "intent": {"action": "video_activity"}}
        card = client.post(path, json=query).json()["card"]
        assert card["type"] == "video_activity"
        data = card["data"]
        assert (data["started"], data["completed"], data["fully_read"]) == (2, 1, 1)
        assert any(entry["round_id"] == round_["id"] for entry in data["records"])
        browsing.discovery.remember_account("200")
        assert client.get(path + "/turns/" + query["request_id"]).json()["card"]["data"]["available"] is False
        assert client.post(path, json=query | {"request_id": str(uuid4())}).json()["card"]["data"]["started"] == 1


def test_explicit_public_experience_and_operations_cards_use_current_sources(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    async def model(messages, schema, *, max_tokens):
        return {"action": "chat"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        turn = {"request_id": str(uuid4()), "message": "这句私下保留"}
        assert client.post(path, json=turn).json()["private"]
        confirmed = client.post(path + f"/{turn['request_id']}/public-experience", json={"fact": "这条事实单独公开"})
        assert confirmed.status_code == 200, confirmed.text
        item = confirmed.json()
        assert item["quote"] == "这条事实单独公开"
        assert item["source"] == {"kind": "confirmed_turn", "request_id": turn["request_id"]}
        memory_url = path + f"/public-experiences/{item['id']}"
        assert client.get(memory_url).json() == item
        lookup = {"request_id": str(uuid4()), "message": "查看公开经历", "intent": {"action": "public_experiences"}}
        lookup_url = path + "/turns/" + lookup["request_id"]
        assert client.post(path, json=lookup).json()["card"]["data"] == {"items": [item]}
        for status in ("archived", "pending_review", "active"):
            assert client.patch(memory_url, json={"status": status}).json()["status"] == status
            items = client.get(lookup_url).json()["card"]["data"]["items"]
            assert items[0]["status"] == status
            assert bool(app.state.dynamics._sources()) == (status == "active")
        assert client.delete(memory_url).json()["ok"]
        assert client.get(memory_url).status_code == 404
        assert client.get(lookup_url).json()["card"]["data"]["items"] == []
        assert client.get(path + "/turns/" + turn["request_id"]).json()["message"] == "这句私下保留"
        foreign = app.state.runtime.daily_memory.confirm_operator_experience(
            {"id": "other", "version": 1}, str(uuid4()), "其他角色的公开经历",
        )
        assert client.delete(path + f"/public-experiences/{foreign}").status_code == 404
        assert client.delete(f"/api/daily-memory/{foreign}").status_code == 404

        command = {"request_id": str(uuid4()), "message": "启动自主运营", "intent": {"action": "start_operations"}}
        control = client.post(path, json=command).json()["card"]
        assert control["type"] == "operation_control" and control["data"]["policy"]["enabled"] is False
        updated = client.put("/api/automation/settings", json={"reply_enabled": True}).json()
        current = client.get(path + "/turns/" + command["request_id"]).json()["card"]["data"]
        assert current["permissions"] == updated["permissions"] and current["permissions"]["replies"] == "review"
        assert client.post(path + f"/turns/{command['request_id']}/confirm/start", json={}).status_code == 409
        assert client.post(path + f"/turns/{command['request_id']}/refresh-confirmation").status_code == 200
        assert client.post(path + f"/turns/{command['request_id']}/confirm/save", json={}).status_code == 200


def test_semantic_chat_binds_displayed_event_order_and_memory_version(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    events = [
        {"bvid": BV, "title": name, "source": {"kind": "comment", "text": quote, "author": name, "uid": str(index),
         "href": f"https://www.bilibili.com/video/{BV}#reply{index}"}, "reply": {"state": "confirmed", "text": "知道了"}}
        for index, name, quote in [(1, "甲", "第一句"), (2, "乙", "第二句")]
    ]
    monkeypatch.setattr(operator_facts, "daily_events", lambda *args: [event.copy() for event in events])

    async def model(messages, schema, *, max_tokens):
        payload = json.loads(messages[1]["content"])
        if schema["title"] == "EventSelection":
            return {"items": [2, 1]}
        message = payload["message"]
        action = {"今天有什么趣事": "activity", "第二件是谁讲的": "event_author", "后来回了没": "event_reply",
                  "把它记成公开经历": "remember_event", "那件公开经历": "recall_experience"}[message]
        reference = next((item["reference"] for item in payload["events"] if item["position"] == 2), None)
        return {"action": action, "reference": reference, "topic": "确认后的事实" if action == "recall_experience" else ""}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
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
        assert turn["card"]["data"]["fact"] == "第一句"
        saved = client.post(f"{path}/{proposal}/public-experience", json={"fact": "确认后的事实"}).json()
        assert saved["source"]["items"][0]["text"] == "第一句"
        assert not app.state.dynamics._sources()
        app.state.browsing.discovery.remember_account("200")
        assert client.get(f"{path}/public-experiences/{saved['id']}").status_code == 404
        app.state.browsing.discovery.remember_account("100")
        recalled, turn = send("那件公开经历")
        assert turn["reply_text"] == "确认后的事实"
        assert client.patch(f"{path}/public-experiences/{saved['id']}", json={"status": "archived"}).status_code == 200
        assert client.get(f"{path}/turns/{recalled}").json()["reply_text"] == "这条公开经历当前不可引用。"
        recalled, turn = send("那件公开经历")
        assert turn["reply_text"] == "确认后的事实"
        assert client.delete(f"{path}/public-experiences/{saved['id']}").status_code == 200
        assert client.get(f"{path}/turns/{recalled}").json()["reply_text"] == "这条公开经历当前不可引用。"
        app.state.browsing.discovery.remember_account("200")
        assert client.get(f"{path}/turns/{proposal}").json()["card"] is None


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
        path = "/api/chat/intelligent"
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
        runtime.inference.status["mode"] = "live"
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
        path = "/api/chat/intelligent"
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
    monkeypatch.delenv("TUBER_VIDEO_ASR_CLI", raising=False)
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: vision)
    browsing = app.state.browsing
    browsing.read_interval = 0
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        request = {"request_id": str(uuid4()), "message": "看完整视频：" + BV, "video_selection": {
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
        assert client.post("/api/automation/stop", json={"emergency": True}).json()["state"] == "emergency"
        assert client.get(waiting_url).json()["card"]["data"]["status"] == "paused"
        cancelled = client.get(turn_url).json()["card"]["data"]
        assert cancelled["status"] == "cancelled" and cancelled["can_resume"]
        assert client.post(path, json=request).json()["card"]["data"]["status"] == "cancelled"
        assert client.post(path, json=request | {"video_selection": {"bvid": BV}}).status_code == 409
        assert client.post(turn_url + "/resume-video").status_code == 400
        assert client.post("/api/automation/reset-stop").status_code == 200
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
        browsing.comments.verified_account_id = "200"
        assert client.get(turn_url).json()["card"]["data"]["status"] == "unavailable"


def test_specified_video_can_resume_when_previously_unavailable_subtitles_appear(settings, monkeypatch):
    from tuber_studio.operations.browsing.evidence import EvidenceUnavailable

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
    monkeypatch.delenv("TUBER_VIDEO_ASR_CLI", raising=False)
    path, identity = "/api/chat/intelligent", str(uuid4())
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
