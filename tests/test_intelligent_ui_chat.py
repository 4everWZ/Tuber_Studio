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
from tuber_studio.agent.assistant_output import AssistantTextStream
from tuber_studio.app import create_app
from tuber_studio.live.game_livestream import GameLivestreamService
from tuber_studio.models import Reply
from tuber_studio.operation_identity import provider_identity_messages
from tuber_studio.operations.browsing.models import BrowsingPolicy
from tuber_studio.operations.browsing.repository import BrowsingRepository
from tuber_studio.providers import Audio


def test_incremental_assistant_output_handles_json_escapes_and_excludes_tool_fields():
    for wire, expected in [
        (json.dumps({"action": "chat", "reply_markdown": '鲸鲸 🌊\n"白饭"', "topic": "内部字段"}), '鲸鲸 🌊\n"白饭"'),
        (json.dumps({"action": "stop", "reply_markdown": "不应显示"}), ""),
    ]:
        fragments = []
        stream = AssistantTextStream(fragments.append)
        for char in wire:
            stream.feed(char)
        assert "".join(fragments) == expected


def test_assistant_stream_persists_only_final_text_and_retries_under_same_id(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    calls = []

    async def generate(messages, schema, *, max_tokens, on_content, on_stage):
        calls.append(messages)
        assert "reply_markdown" in schema["properties"]
        on_stage("generating")
        on_content('{"action":"chat","reply_markdown":"**白饭')
        on_content('**，分你半碗。"}')
        return {"action": "chat", "reply_markdown": "**白饭**，分你半碗。"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        payload = {"request_id": str(uuid4()), "message": "鱼鱼今天吃了几碗饭"}
        response = client.post(path + "/stream", json=payload)
        assert response.status_code == 200
        assert 'event: delta\ndata: "**白饭"' in response.text
        assert 'event: delta\ndata: "**，分你半碗。"' in response.text
        assert "event: done" in response.text
        assert client.post(path, json=payload).json()["reply_text"] == "**白饭**，分你半碗。"
        assert len(calls) == 1
        assert len(client.get(path).json()["turns"]) == 1


def test_assistant_rejects_invalid_tool_payload_before_dispatch(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())

    async def generate(messages, schema, *, max_tokens, on_content, on_stage):
        return {"action": "inspect_video_frame", "part": 0, "seconds": 1}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 52000)) as client:
        response = client.post("/api/chat/intelligent/stream", json={
            "request_id": str(uuid4()), "message": "帮我看一下视频中的一帧",
        })
        assert "event: error" in response.text
        assert "event: done" not in response.text
        assert not app.state.store.one("SELECT 1 FROM browsing_rounds")


@pytest.mark.parametrize("change", ["account", "character", "memory"])
def test_assistant_rechecks_private_scope_before_delivery(settings, monkeypatch, change):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, browsing = app.state.runtime, app.state.browsing
    memory = runtime.daily_memory

    async def generate(messages, schema, *, max_tokens, on_content, on_stage):
        on_stage("generating")
        if change == "account":
            browsing.discovery.remember_account("200")
        elif change == "character":
            char = runtime.character
            monkeypatch.setattr(type(runtime), "character", property(lambda self: char | {"version": 999}))
        else:
            assert on_content is None
            memory.set_preference(runtime.memory_organizer.scope(), False)
        if on_content:
            on_content('{"action":"chat","reply_markdown":"旧作用域的文字')
        return {"action": "chat", "reply_markdown": "旧作用域的文字"}

    if change == "memory":
        monkeypatch.setattr(memory, "recall_organized", lambda *args, **kwargs: [{"headline": "私有", "anecdote": "私有文字"}])
        monkeypatch.setattr(memory, "organized_reference", lambda entry: {"id": "fixture", "version": 1})
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 52000)) as client:
        browsing.discovery.remember_account("100")
        response = client.post("/api/chat/intelligent/stream", json={
            "request_id": str(uuid4()), "message": "还记得刚才的事吗",
        })
        assert "event: error" in response.text
        assert "event: delta" not in response.text
        assert "旧作用域的文字" not in response.text
        assert not app.state.store.one("SELECT 1 FROM operator_chat_turns")


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
        assert not app.state.dynamics._sources()


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


@pytest.mark.parametrize("unknown", [False, True])
def test_chat_corrects_original_dynamic_without_reposting(settings, monkeypatch, unknown):
    from tuber_studio import dynamics as module

    app = create_app(settings, adapter=ControlledPlatform())
    studio = app.state.dynamics
    identifier = "1270460143781281810"
    source = {"id": "reply:example", "type": "published_reply", "text": "有人提到了赛博保险。"}
    monkeypatch.setattr(studio, "_sources", lambda: [source])
    old = "我在评论区连着认错对象。第三次才翻回原楼找人把话接对。"
    new = "评论区有人说要卖我赛博保险。"
    picture = {"img_src": "https://i0.hdslb.com/bfs/new_dyn/1.png", "img_width": 960, "img_height": 512}
    remote = {"text": old, "pictures": [picture]}
    writes = []

    async def read(adapter, target, account):
        assert target == identifier and account == "100"
        return dict(remote)

    async def write(adapter, target, text, pictures, *, before_send):
        before_send()
        writes.append((target, text, pictures))
        if unknown:
            raise module.PublishUncertain("编辑回执不明")
        remote["text"] = text

    async def generate(messages, schema, *, max_tokens):
        if schema.get("title") == "ChatIntent":
            return {"action": "edit_dynamic"}
        packet = json.loads(messages[1]["content"])
        assert packet["original"] == old
        assert packet["public_sources"] == [source]
        return {"caption": new}

    monkeypatch.setattr(module, "read_dynamic_caption", read)
    monkeypatch.setattr(module, "edit_dynamic_caption", write)
    monkeypatch.setattr(app.state.runtime.llm, "generate_json", generate)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        app.state.browsing.discovery.remember_account("100")
        store = app.state.runtime.store
        receipt = {"dynamic_id": identifier, "code": 0, "platform": "bilibili"}
        row = studio._save_decision(app.state.runtime.character, False, {
            "action": "post", "reason": "评论区互动", "content": old,
            "source_ids": [source["id"]], "next_evaluation_seconds": 1800,
        }, [source], schedule=False)
        post_id = row["post_id"]
        store.execute("UPDATE dynamic_posts SET account_id='100' WHERE id=?", (post_id,))
        studio._status(post_id, "published", receipt=receipt)
        posted_at = studio._post(post_id)["published_at"]
        request_id = str(uuid4())
        response = client.post("/api/chat/intelligent", json={
            "request_id": request_id, "message": f"修改 https://www.bilibili.com/opus/{identifier}，原评论里没有认错人。",
        })
        card = response.raise_for_status().json()["card"]
        assert card["type"] == "dynamic_edit" and card["data"]["caption"] == new
        assert not writes
        path = f"/api/dynamics/edits/{request_id}/apply"
        result = client.post(path).raise_for_status().json()
        if unknown:
            assert result["status"] == "pending_verification"
            assert client.post(path).raise_for_status().json()["status"] == "pending_verification"
            assert studio._post(post_id)["content"] == old
            remote["text"] = new
        assert client.post(path).raise_for_status().json()["status"] == "applied"
        assert writes == [(identifier, new, [picture])]
        post = studio._post(post_id)
        assert post["content"] == new and post["receipt"] == receipt and post["published_at"] == posted_at


def test_operations_card_refreshes_permissions_and_confirmation(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    async def model(messages, schema, *, max_tokens):
        return {"action": "chat"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
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
                  "把它记成公开经历": "remember_event", "那件公开经历": "recall_experience",
                  "查看公开经历": "public_experiences", "你记得我什么": "memory_summary"}[message]
        reference = next((item["reference"] for item in payload["events"] if item["position"] == 2), None)
        return {"action": action, "reference": reference, "topic": "第一句" if action == "recall_experience" else ""}

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
        assert turn["card"]["data"]["text"] == "第一句"
        memory_path = "/api/daily-memory"
        saved, = client.get(memory_path).json()["organized"]
        assert saved["source"]["references"][0]["text"] == "第一句"
        assert saved["channels"] == ["operator"]
        summary_id, turn = send("你记得我什么")
        assert turn["card"]["data"]["groups"] == client.get(memory_path).json()["summary"]
        listing, turn = send("查看公开经历")
        assert turn["card"]["data"]["items"] == [{
            "headline": saved["headline"], "anecdote": "第一句", "links": [events[1]["source"]["href"]],
        }]
        assert not app.state.dynamics._sources()
        app.state.browsing.discovery.remember_account("200")
        assert not client.get(memory_path).json()["organized"]
        app.state.browsing.discovery.remember_account("100")
        recalled, turn = send("那件公开经历")
        assert turn["reply_text"] == "第一句" and turn["memory_dependent"]
        assert client.delete(f"{memory_path}/organized/{saved['id']}").status_code == 200
        assert client.get(f"{path}/turns/{summary_id}").json()["card"]["data"]["groups"] == []
        assert client.get(f"{path}/turns/{listing}").json()["card"]["data"]["items"] == []
        assert client.get(f"{path}/turns/{recalled}").json()["reply_text"] == "这条回复引用的记忆已变更。"

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


def test_operator_video_to_live_uses_only_handoff_text_and_player_receipt(settings, monkeypatch):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: TestVisual())
    runtime, browsing = app.state.runtime, app.state.browsing
    browsing.read_interval = 0
    browsing.discovery.remember_account("100")
    monkeypatch.delenv("TUBER_VIDEO_ASR_CLI", raising=False)
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
    monkeypatch.setattr(runtime.llm, "generate", generate)
    monkeypatch.setattr(runtime.voices, "synthesize_live", synthesize)
    path = "/api/chat/intelligent"

    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        client.cookies.set("tuber_overlay", settings.overlay_token)

        def send(message, **kwargs):
            req = {"message": message, "request_id": str(uuid4()), **kwargs}
            return req, client.post(path, json=req).raise_for_status().json()

        send(f"姓名：{name}，私下讨论加盐让鸡蛋浮起来，内部链接 {private_url}")
        request, queued = send(f"帮{name}看一下 {BV}，范围是 P1 开头五秒。", video_selection={
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
        client.post("/api/live/start").raise_for_status()
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
        client.post("/api/live/stop").raise_for_status()
        client.post("/api/live/start").raise_for_status()
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
    path = "/api/chat/intelligent"
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        def handoff(action, *, request_id=None):
            request_id = request_id or str(uuid4())
            return client.post(path, json={"request_id": request_id, "message": "私聊背景：验收姓名乙和内部链接",
                                          "intent": {"action": "game_handoff", "game_action": action}}).raise_for_status().json()

        original = client.get("/api/games/status").json()["active_status"]
        identity = str(uuid4())
        first = handoff("water_crops", request_id=identity)
        assert first["card"]["type"] == "game_handoff"
        assert "验收姓名乙" not in json.dumps(game.last_handoff, ensure_ascii=False)
        watered = client.get("/api/games/status").json()["active_status"]
        assert watered["unwatered_crops"] < original["unwatered_crops"]
        assert handoff("water_crops", request_id=identity) == first
        assert client.post("/api/games/control", json={"owner": "operator"}).json()["owner"] == "operator"
        assert "号主接管" in handoff("sleep_day")["reply_text"]
        assert client.post("/api/games/step").status_code == 400
        assert client.post("/api/games/action", json={"action": "sleep_day"}).json()["success"]
        saved = GameLivestreamService(store=app.state.store)
        assert saved.status()["active_status"]["day"] == original["day"] + 1 and saved.owner == "operator"
        client.post("/api/games/control", json={"owner": "studio"}).raise_for_status()
        assert handoff("step")["card"]["type"] == "game_handoff"


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
