import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

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
