import asyncio
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_browsing import BV, ControlledPlatform
from test_browsing_evidence import PNG, TestVisual

from tuber_studio.agent.operator_chat import action_for
from tuber_studio.app import create_app
from tuber_studio.operations.browsing.models import BrowsingPolicy
from tuber_studio.operations.browsing.repository import BrowsingRepository


@pytest.mark.parametrize(("message", "expected"), [
    ("不要发个动态：测试", "chat"),
    ("「去刷视频：鲸鱼」是举例", "chat"),
    ("你准备开播吗？", "chat"),
    ("发个动态是什么意思？", "chat"),
    ("请发个动态：聊聊白饭", "dynamic"),
    ("写动态：聊聊白饭，保存为草稿", "dynamic"),
    ("先别写动态：聊聊白饭", "chat"),
    ("去刷视频看看原神新版本", "browse"),
    ("准备开播", "live"),
    ("今天刷了多少视频？", "video_activity"),
    ("查看公开经历", "public_experiences"),
    ("启动自主运营", "operation_control"),
    ("不要启动自主运营", "chat"),
    ("看完整视频：" + BV, "full_video"),
])
def test_intelligent_chat_only_dispatches_direct_commands(message, expected):
    assert action_for(message)[0] == expected


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

        dynamic = {"request_id": str(uuid4()), "message": "发个动态：聊白饭"}
        card = client.post(path, json=dynamic).json()["card"]
        post_id = card["data"]["id"]
        assert client.post(path, json=dynamic).json()["card"]["data"]["id"] == post_id
        assert store.one("SELECT status FROM dynamic_posts WHERE id=?", (post_id,))["status"] == "pending_review"
        assert "xyz123" not in str(packets)
        response = client.post(f"/api/dynamics/posts/{post_id}/publish", json={"text": "换成这句白饭。"})
        assert response.status_code == 400  # this isolated app has no platform credentials
        assert app.state.dynamics._post(post_id)["content"] == "换成这句白饭。"

        browse = {"request_id": str(uuid4()), "message": "去刷视频：原神"}
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
        query = {"request_id": str(uuid4()), "message": "今天刷了多少视频？"}
        card = client.post(path, json=query).json()["card"]
        assert card["type"] == "video_activity"
        data = card["data"]
        assert (data["started"], data["completed"], data["fully_read"]) == (2, 1, 1)
        assert any(entry["round_id"] == round_["id"] for entry in data["records"])
        browsing.discovery.remember_account("200")
        assert client.get(path + "/turns/" + query["request_id"]).json()["card"]["data"]["available"] is False
        assert client.post(path, json=query | {"request_id": str(uuid4())}).json()["card"]["data"]["started"] == 1


def test_explicit_public_experience_and_operations_cards_use_current_sources(settings):
    app = create_app(settings, adapter=ControlledPlatform())
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
        lookup = {"request_id": str(uuid4()), "message": "查看公开经历"}
        lookup_url = path + "/turns/" + lookup["request_id"]
        assert client.post(path, json=lookup).json()["card"]["data"] == {"items": [item]}
        for status in ("archived", "pending_review", "active"):
            assert client.patch(memory_url, json={"status": status}).json()["status"] == status
            items = client.get(lookup_url).json()["card"]["data"]["items"]
            assert bool(items) == (status == "active")
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

        command = {"request_id": str(uuid4()), "message": "启动自主运营"}
        control = client.post(path, json=command).json()["card"]
        assert control["type"] == "operation_control" and control["data"]["policy"]["enabled"] is False
        updated = client.put("/api/automation/settings", json={"reply_enabled": True}).json()
        current = client.get(path + "/turns/" + command["request_id"]).json()["card"]["data"]
        assert current["permissions"] == updated["permissions"] and current["permissions"]["replies"] == "review"


def test_specified_video_reuses_range_checkpoints_and_reconnects_under_same_id(settings, monkeypatch):
    class MultipartPlatform(ControlledPlatform):
        hold_second = True

        async def video_metadata(self, video):
            metadata = await super().video_metadata(video)
            return metadata | {"parts": metadata["parts"] + [{"cid": "22", "title": "第二P", "duration": 30}]}

        async def subtitles(self, bvid, cid, languages):
            if self.hold_second and cid == "22":
                await asyncio.Event().wait()
            return await super().subtitles(bvid, cid, languages)

        async def video_frames(self, metadata, count, *, cid, timestamps):
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
        assert len(first["visual"]["samples"]) == len(second["visual"]["samples"]) == 3
        assert second["audio"]["state"] == "unavailable"
        assert platform.calls.count(("subtitles", BV, "11")) == 1
        assert platform.calls.count(("subtitles", BV, "22")) == 1
        assert vision.calls == 2 and platform.sent == []
        assert app.state.store.one("SELECT COUNT(*) AS n FROM browsing_tasks WHERE round_id=?", (result["id"],))["n"] == 1
        browsing.comments.verified_account_id = "200"
        assert client.get(turn_url).json()["card"]["data"]["status"] == "unavailable"
