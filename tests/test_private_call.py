import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent import private_call
from tuber_studio.app import create_app
from tuber_studio.front_persona import front_identity
from tuber_studio.operation_identity import private_participant_instructions
from tuber_studio.store import editable_character


@pytest.mark.parametrize("temporary", [False, True])
def test_ticket_instructions_follow_current_character_owner(settings, monkeypatch, temporary):
    async def mint(origin, token):
        return "test-ticket"

    monkeypatch.setattr(private_call, "private_service", lambda: ("https://voice.example", "test-key"))
    monkeypatch.setattr(private_call, "mint_ticket", mint)
    app = create_app(settings)
    runtime = app.state.runtime
    auth = {"Authorization": "Bearer " + settings.admin_token}
    with TestClient(app) as browser:
        for owner in ({"uid": "123", "name": "示例号主"}, None):
            runtime.store.replace_character(editable_character(runtime.character) | {"bilibili_owner": owner})
            ticket = browser.post("/api/chat/private-call/ticket", headers=auth, json={
                "temporary": temporary, "topic": "来聊聊吧",
            }).raise_for_status().json()
            instructions = ticket["instructions"]
            participants = json.loads(instructions.split("私聊参与者：", 1)[1].splitlines()[0])
            assert participants == {"当前交谈者": "工坊号主", "角色": runtime.character["name"], "bilibili_owner": owner}
            if owner is None:
                assert "示例号主" not in instructions


async def test_ticket_uses_only_the_eligible_chat_topic_and_revalidates_it(settings, monkeypatch):
    app = create_app(settings)
    runtime = app.state.runtime
    path = "/api/chat/private-call"
    question = "咱们电话聊一下明天开场提纲。"
    related = "写开场提纲时，先介绍海底厨房。"
    unrelated = "只有整理账单时，才按金额倒序排列。"
    monkeypatch.setattr(private_call, "private_service", lambda: ("https://voice.example", "test-key"))

    async def mint(*args):
        return "test-ticket"

    monkeypatch.setattr(private_call, "mint_ticket", mint)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        async def ticket(**body):
            return (await client.post(path + "/ticket", json=body)).raise_for_status().json()

        for note in (related, unrelated):
            body = {"source": "settings", "text": note}
            preview = (await client.post("/api/agent/memory.propose", json=body)).raise_for_status().json()
            (await client.post("/api/agent/memory/confirm", json=body | {"stamp": preview["stamp"]})).raise_for_status()
        turn = str(uuid4())
        (await client.post("/api/chat/intelligent", json={
            "request_id": turn, "message": question, "intent": {"action": "chat"},
        })).raise_for_status()
        inherited = await ticket(topic_turn=turn)
        assert related in inherited["instructions"] and unrelated not in inherited["instructions"]
        assert question not in inherited["instructions"]
        use = inherited["memory_use"] | {"topic_source": inherited["topic_source"]}
        assert (await client.post(path + "/memory-validate", json=use)).status_code == 200
        assert (await ticket())["memory_use"] is None
        off_topic = str(uuid4())
        (await client.post("/api/chat/intelligent", json={
            "request_id": off_topic, "message": "我们去看月亮吧。", "intent": {"action": "chat"},
        })).raise_for_status()
        assert (await ticket(topic_turn=off_topic))["memory_use"] is None
        assert (await ticket(topic_turn=turn, topic="月亮的背面"))["memory_use"] is None
        temporary = await ticket(topic_turn=turn, topic=related, temporary=True)
        assert temporary["memory_use"] is None and related not in temporary["instructions"]
        prefs = (await client.get("/api/daily-memory/preferences")).json()
        for enabled in (False, True):
            (await client.put("/api/daily-memory/preferences", json={
                "scope": prefs["scope"], "enabled": True, "history_enabled": enabled,
            })).raise_for_status()
            assert (await client.post(path + "/memory-validate", json=use)).status_code == 409
            if not enabled:
                assert (await ticket(topic_turn=turn))["memory_use"] is None
                assert related in (await ticket(topic_turn=turn, topic=related))["instructions"]
        original = runtime.store.one("SELECT * FROM operator_chat_turns WHERE request_id=?", (turn,))
        result = json.loads(original["result"])
        for changes in ({"owner_source": "another-owner"}, {"private": False}, {"card": {"type": "agent_task"}},
                        {"account_id": "another-account", "private_owner": None}):
            runtime.store.execute("UPDATE operator_chat_turns SET result=? WHERE request_id=?",
                                  (json.dumps(result | changes), turn))
            assert (await ticket(topic_turn=turn))["memory_use"] is None
        for role, at in (("another-character", time.time()), (original["character_id"], time.time() - 31 * 60)):
            runtime.store.execute("UPDATE operator_chat_turns SET result=?, character_id=?, created_at=? WHERE request_id=?",
                                  (original["result"], role, at, turn))
            assert (await ticket(topic_turn=turn))["memory_use"] is None
        runtime.store.execute("UPDATE operator_chat_turns SET created_at=? WHERE request_id=?", (time.time(), turn))
        fresh = await ticket(topic_turn=turn)
        runtime.store.execute("DELETE FROM operator_chat_turns WHERE request_id=?", (turn,))
        revoked = fresh["memory_use"] | {"topic_source": fresh["topic_source"]}
        assert (await client.post(path + "/memory-validate", json=revoked)).status_code == 409
        assert (await ticket(topic_turn=turn))["memory_use"] is None
    await app.state.comments.adapter.close()
    app.state.store.close()


async def test_role_background_is_selected_at_open_and_its_inherited_source_can_be_withdrawn(settings, monkeypatch):
    app = create_app(settings)
    runtime = app.state.runtime
    passage = "白塔码头的铜钟曾被海浪冲走。"
    unrelated = "北岭茶馆的招牌是紫色的。"
    card = editable_character(runtime.character)
    card.update(self_description="码头看守", speaking_style="轻声慢讲",
                core={"identity": "旧卡身份长段，后面还有旧设定。"},
                style={"instructions": "旧卡风格长段，后面还有旧示例。"},
                lore=[{"keywords": ["白塔码头"], "text": passage}, {"keywords": ["北岭茶馆"], "text": unrelated}])
    runtime.store.replace_character(card)
    base = "\n".join([front_identity(runtime.character), private_participant_instructions(runtime.character)])
    assert all(word in base for word in ("码头看守", "轻声慢讲", runtime.character["name"]))
    monkeypatch.setattr(private_call, "private_service", lambda: ("https://voice.example", "test-key"))

    async def mint(*args):
        return "test-ticket"

    monkeypatch.setattr(private_call, "mint_ticket", mint)
    path = "/api/chat/private-call"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        async def ticket(**body):
            return (await client.post(path + "/ticket", json=body)).raise_for_status().json()

        query = "聊聊白塔码头吧。"
        assert (await ticket())["instructions"] == base
        assert (await ticket(topic="晚饭吃西瓜吗"))["instructions"] == base
        assert (await ticket(temporary=True, topic=query))["instructions"] == base
        explicit = await ticket(topic=query)
        assert explicit["instructions"].startswith(base) and passage in explicit["instructions"]
        assert unrelated not in explicit["instructions"] and "旧卡" not in explicit["instructions"]
        turn = str(uuid4())
        (await client.post("/api/chat/intelligent", json={
            "request_id": turn, "message": query, "intent": {"action": "chat"},
        })).raise_for_status()
        inherited = await ticket(topic_turn=turn)
        assert inherited["instructions"] == explicit["instructions"]
        assert not inherited["memory_citations"] and inherited["memory_use"]["ids"] == []
        use = inherited["memory_use"] | {"topic_source": inherited["topic_source"]}
        assert (await client.post(path + "/memory-validate", json=use)).status_code == 200
        runtime.store.execute("UPDATE operator_chat_turns SET message=? WHERE request_id=?", ("今天吃西瓜吧。", turn))
        assert (await client.post(path + "/memory-validate", json=use)).status_code == 409
        assert (await ticket(topic_turn=turn))["instructions"] == base
        runtime.store.execute("DELETE FROM operator_chat_turns WHERE request_id=?", (turn,))
        assert (await client.post(path + "/memory-validate", json=use)).status_code == 409
        assert (await ticket(topic_turn=turn))["instructions"] == base
    await app.state.comments.adapter.close()
    app.state.store.close()


@pytest.mark.parametrize("behind_edge", [True, False])
def test_ticket_is_admin_only_fixed_upstream_and_contains_no_service_credential(settings, tmp_path, monkeypatch, behind_edge):
    key = tmp_path / "voice.key"
    key.write_text("private-application-fixture")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_ORIGIN", "https://voice.example.org")
    if behind_edge:
        monkeypatch.setenv("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN", "https://192.0.2.10")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_TOKEN_FILE", str(key))
    issued = []

    async def issue(client, request, **kwargs):
        issued.append(request)
        assert not client.follow_redirects
        return httpx.Response(200, request=request, json={"ticket": "a" * 32, "expires_in": 30, "secret": key.read_text()})

    app = create_app(settings)
    with TestClient(app) as browser:
        monkeypatch.setattr(httpx.AsyncClient, "send", issue)
        path = "/api/chat/private-call/ticket"
        auth = {"Authorization": "Bearer " + settings.admin_token}
        assert browser.post(path).status_code == 401
        assert browser.post(path, headers=auth | {"Origin": "https://elsewhere.example"}).status_code == 403
        assert browser.get("/api/chat/private-call", headers=auth).json() == {"configured": True}
        assert not issued
        reply = browser.post(path, headers=auth, json={"path": "/api/mode", "origin": "https://elsewhere.example"})
        assert reply.status_code == 200
        assert reply.headers["cache-control"] == "no-store"
        assert key.read_text() not in reply.text and settings.admin_token not in reply.text
        browser_host = "192.0.2.10" if behind_edge else "voice.example.org"
        assert reply.json()["socket"] == f"wss://{browser_host}/v1/realtime"
        assert reply.json()["character"] == app.state.runtime.character["name"]
        assert reply.json()["avatar"] == {"still": "/assets/whale/idle.gif",
                                         "speaking": None, "fallback": "/assets/whale/idle.gif"}
        assert len(issued) == 1
        request = issued[0]
        assert request.method == "POST" and str(request.url) == "https://voice.example.org/v1/browser-tickets"
        assert request.headers["authorization"] == "Bearer " + key.read_text()
        assert request.content == b'{"path":"/v1/realtime"}'


def test_private_avatar_pair_is_not_inherited_by_name_or_manifest(settings):
    clone = {"id": "other", "name": "大肥鱼", "avatar_manifest": "assets/whale/manifest.json"}
    profile = private_call.call_avatar(settings, clone)
    assert profile["speaking"] is None and profile["still"] == "/assets/whale/idle.gif"
    for role in (clone, clone | {"id": "whale"}):
        assert private_call.call_avatar(settings, role | {"avatar_manifest": "missing.json"}) == {
            "still": None, "speaking": None, "fallback": None,
        }


def test_review_recordings_are_owner_only_and_role_bound(settings):
    app = create_app(settings)
    role = app.state.runtime.character["id"]
    root = settings.runtime / "private-call-previews" / role
    root.mkdir(parents=True)
    (root / "mature.mp4").write_bytes(b"private clip")
    with TestClient(app) as browser:
        auth = {"Authorization": "Bearer " + settings.admin_token}
        listed = browser.get("/api/chat/private-call", headers=auth).json()["recordings"]
        assert set(listed) == {"mature"}
        path = listed["mature"]
        assert browser.get(path).status_code == 401
        clip = browser.get(path, headers=auth | {"Range": "bytes=0-3"})
        assert (clip.status_code, clip.content) == (206, b"priv")
        assert "no-store" in clip.headers["cache-control"]
        assert browser.get(path.replace("/" + role + "/", "/another/"), headers=auth).status_code == 404
        assert browser.get(path.replace("mature.mp4", "q.mp4"), headers=auth).status_code == 404


@pytest.mark.parametrize("origin", ["", "http://192.0.2.3", "https://voice.example/llm", "https://user@voice.example"])
def test_unconfigured_or_insecure_service_is_not_offered(monkeypatch, tmp_path, origin):
    key = tmp_path / "token"
    key.write_text("test-app-token")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_TOKEN_FILE", str(key))
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_ORIGIN", origin)
    assert private_call.private_service() is None


@pytest.mark.parametrize("data", [{"ticket": "app-credential-do-not-echo"}, {}, {"ticket": 5}])
async def test_bad_upstream_replies_fail_closed(data, monkeypatch):
    async def issue(client, request, **kwargs):
        return httpx.Response(200, request=request, json=data)
    monkeypatch.setattr(httpx.AsyncClient, "send", issue)
    with pytest.raises(ValueError):
        await private_call.mint_ticket("https://voice.example", "app-credential-do-not-echo")
