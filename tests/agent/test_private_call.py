import asyncio
import hashlib
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent.voice import origin as voice_origin
from tuber_studio.agent.voice import private_call
from tuber_studio.agent.voice import work as private_call_work
from tuber_studio.app import create_app
from tuber_studio.core.identity import operating_account_facts, private_participant_instructions
from tuber_studio.core.store import editable_character
from tuber_studio.persona.characters.front_persona import persona_instructions


def test_public_mount_keeps_the_shell_assets_auth_and_call_tickets_together(settings, monkeypatch):
    monkeypatch.setenv("TUBER_WEB_PREFIX", "/dafeiyu")
    monkeypatch.setattr(private_call, "private_service", lambda settings: ("https://studio.example", "test-key"))

    async def mint(origin, token):
        return "test-ticket"

    monkeypatch.setattr(private_call, "mint_ticket", mint)
    with TestClient(create_app(settings), base_url="https://studio.example") as browser:
        assert browser.get("/dafeiyu?x=1", follow_redirects=False).headers["location"] == "/dafeiyu/?x=1"
        for route in ("", "/live", "/overlay", "/overlay/draw-guess"):
            page = browser.get("/dafeiyu" + route)
            assert page.status_code == 200 and '<base href="/dafeiyu/">' in page.text
        assert '<base href="/">' in browser.get("/").text
        assert browser.get("/dafeiyu/static/studio-path.js").status_code == 200
        assert browser.get("/dafeiyu/assets/whale/idle.gif").status_code == 200
        assert browser.get("/dafeiyu/settings", follow_redirects=False).headers["location"] == "/dafeiyu/#settings"
        login = browser.post("/dafeiyu/api/v1/system/auth/login", json={"token": settings.admin_token},
                             headers={"Origin": "https://studio.example"})
        assert login.status_code == 200
        assert browser.get("/dafeiyu/api/v1/system/settings").json()["overlay_url"].startswith(
            "https://studio.example/dafeiyu/overlay#")
        ticket = browser.post("/dafeiyu/api/v1/agent/private-call/ticket", json={"temporary": True})
        assert ticket.status_code == 200 and ticket.headers["cache-control"] == "no-store"
        assert ticket.json()["socket"] == "wss://studio.example/dafeiyu/v1/realtime"
        assert browser.post("/api/v1/agent/private-call/ticket", json={"temporary": True}).json()["socket"] == (
            "wss://studio.example/v1/realtime")
        with browser.websocket_connect("wss://studio.example/dafeiyu/ws/admin",
                                       headers={"Origin": "https://studio.example"}) as socket:
            assert socket.receive_json()["type"] == "status"


async def test_signed_voice_work_stays_in_the_original_chat(settings, monkeypatch):
    key = "fixture-gateway"
    app = create_app(settings)
    runtime = app.state.runtime
    configured_key = settings.provider_key
    monkeypatch.setattr(settings, "provider_key", lambda name: key if name == "omni" else configured_key(name))
    path = "/api/v1/agent/private-call"
    completed = "演播间当前待命。"
    goal = "帮我核对演播间状态，留份私下的交接便笺。"
    casual = "这一阵风吹得真舒服。"
    change = "刚才那份便笺加上检查时间。"
    cancel = "停掉方才那份交接便笺。"
    pending_read = asyncio.Event()
    resume_read = asyncio.Event()

    async def route(chat, text, tasks):
        picked = {goal: "delegate", change: "revise", cancel: "cancel"}.get(text, "conversation")
        return private_call_work.VoicePlan(action=picked, quote=text,
                                           task_id=tasks[0]["id"] if tasks and picked in {"revise", "cancel"} else None)

    async def synthesize(words, voice):
        assert words == completed and voice == runtime.character["voice_profile"]
        pending_read.set()
        await resume_read.wait()
        from types import SimpleNamespace
        return SimpleNamespace(path=settings.root / "assets/whale/ref.wav")

    monkeypatch.setattr(private_call_work, "classify", route)
    monkeypatch.setattr(runtime.voices, "synthesize_live", synthesize)
    ticket = voice_origin.ticket_context(key, "fixture-ticket", runtime.tasks.scope())

    def spoken(capture, text, **changes):
        fields = ["omni.mic.v1", ticket["ticket_hash"], capture, 48000, text]
        return {"origin": {**ticket, "machine_text": text, "capture_id": capture, "samples": 48000,
                           "receipt": voice_origin.sign(key, fields),
                           "transcript_receipt": voice_origin.sign(key, ["omni.asr.v1", *fields[1:]]), **changes}}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as browser:
        def send(body):
            return browser.post(path + "/utterances", json=body)
        forged = await send(spoken("voice-1", casual, machine_text=goal))
        assert forged.status_code == 409
        legacy = await send(spoken("voice-1", goal, transcript_receipt=None))
        assert legacy.status_code == 409
        receipt = (await send(spoken("voice-1", goal))).raise_for_status().json()
        assert (await send(spoken("voice-1", goal))).json() == receipt
        assert (await send(spoken("voice-2", casual))).json() == {"turn_id": None}
        assert len(runtime.tasks.activity()) == 1
        task_id = receipt["turn_id"]
        row = runtime.store.one("SELECT * FROM operator_chat_turns WHERE request_id=?", (task_id,))
        assert json.loads(row["result"])["call_origin"] == {"id": ticket["ticket_hash"], "utterance": "voice-1"}
        active = runtime.tasks.executable(task_id)
        assert active["goal"] == goal
        # Responses completion uses the same persistence path as the worker.
        assert runtime.tasks._save(active, status="completed", message=completed,
                                   plan={"backend": "responses", "prior_goals": []})
        history_path = "/api/v1/agent/chat"
        turns = (await browser.get(history_path)).json()["turns"]
        assert len(turns) == 2
        assert turns[0]["card"]["data"]["delivered_in_chat"]
        assert turns[-1]["assistant_only"] and turns[-1]["reply_text"] == completed
        for _ in range(2):
            visible = (await browser.post(path + "/work/status", json=ticket)).raise_for_status().json()["tasks"]
            assert len(visible) == 1 and visible[0]["message"] == completed
        other = voice_origin.ticket_context(key, "other-ticket", runtime.tasks.scope())
        assert (await browser.post(path + "/work/status", json=other)).json() == {"tasks": []}
        endpoint = f"{path}/work/{task_id}/read"
        audio = asyncio.create_task(browser.post(endpoint, json={"call": ticket, "revision": 1}))
        await asyncio.wait_for(pending_read.wait(), 2)
        assert (await send(spoken("voice-3", change))).raise_for_status().json()["task_id"] == task_id
        resume_read.set()
        assert (await audio).status_code == 409
        assert (await send(spoken("voice-4", cancel))).raise_for_status().json()["task_id"] == task_id
        assert (await browser.post(endpoint, json={"call": ticket, "revision": 2})).status_code == 409
        turns = (await browser.get(history_path)).json()["turns"]
        assert len([turn for turn in turns if turn.get("card")]) == 1
        assert not any(turn.get("assistant_only") and not turn.get("hidden") for turn in turns)
        assert runtime.tasks.executable(task_id)["status"] == "cancelled"
        runtime.store.replace_character(editable_character(runtime.character) | {"name": "新的角色名字"})
        assert (await send(spoken("voice-5", goal))).status_code == 409


@pytest.mark.parametrize("temporary", [False, True])
def test_ticket_instructions_follow_current_character_owner(settings, monkeypatch, temporary):
    async def mint(origin, token):
        return "test-ticket"

    monkeypatch.setattr(private_call, "private_service", lambda settings: ("https://voice.example", "test-key"))
    monkeypatch.setattr(private_call, "mint_ticket", mint)
    app = create_app(settings)
    runtime = app.state.runtime
    auth = {"Authorization": "Bearer " + settings.admin_token}
    with TestClient(app) as browser:
        for owner, voice_id in (({"uid": "123", "name": "示例号主"}, "whale_reference"), (None, "neutral_reference")):
            runtime.store.replace_character(editable_character(runtime.character) |
                                            {"bilibili_owner": owner, "voice_profile": voice_id})
            ticket = browser.post("/api/v1/agent/private-call/ticket", headers=auth, json={
                "temporary": temporary, "topic": "来聊聊吧",
            }).raise_for_status().json()
            instructions = ticket["instructions"]
            voice = settings.voice(runtime.character["voice_profile"])
            assert ticket["voice"]["id"] == voice["id"]
            reference = browser.get("/api/v1/agent/private-call/reference", headers=auth).raise_for_status()
            assert reference.content == settings.voice_reference_path(voice).read_bytes()
            assert ticket["voice"]["reference_sha256"] == hashlib.sha256(reference.content).hexdigest()
            assert reference.headers["cache-control"] == "no-store"
            assert "ref_audio" not in ticket["voice"]
            participants = json.loads(instructions.split("私聊参与者：", 1)[1].splitlines()[0])
            expected_caller = owner["name"] if owner else "你"
            assert participants == {"当前交谈者": expected_caller, "角色": runtime.character["name"], "bilibili_owner": owner,
                                    "operating_account": {"state": "not_configured"}}
            if owner is None:
                assert "示例号主" not in instructions


async def test_ticket_uses_only_the_eligible_chat_topic_and_revalidates_it(settings, monkeypatch):
    app = create_app(settings)
    runtime = app.state.runtime
    path = "/api/v1/agent/private-call"
    question = "咱们电话聊一下明天开场提纲。"
    related = "写开场提纲时，先介绍海底厨房。"
    unrelated = "只有整理账单时，才按金额倒序排列。"
    monkeypatch.setattr(private_call, "private_service", lambda settings: ("https://voice.example", "test-key"))

    async def mint(*args):
        return "test-ticket"

    monkeypatch.setattr(private_call, "mint_ticket", mint)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        async def ticket(**body):
            return (await client.post(path + "/ticket", json=body)).raise_for_status().json()

        for note in (related, unrelated):
            body = {"source": "settings", "text": note}
            preview = (await client.post("/api/v1/agent/memory/propose", json=body)).raise_for_status().json()
            (await client.post("/api/v1/agent/memory/confirm", json=body | {"stamp": preview["stamp"]})).raise_for_status()
        turn = str(uuid4())
        (await client.post("/api/v1/agent/chat", json={
            "request_id": turn, "message": question, "intent": {"action": "chat"},
        })).raise_for_status()
        inherited = await ticket(topic_turn=turn)
        assert related in inherited["instructions"] and unrelated not in inherited["instructions"]
        assert question not in inherited["instructions"]
        use = inherited["memory_use"] | {"topic_source": inherited["topic_source"]}
        assert (await client.post(path + "/memory-validate", json=use)).status_code == 200
        assert (await ticket())["memory_use"] is None
        off_topic = str(uuid4())
        (await client.post("/api/v1/agent/chat", json={
            "request_id": off_topic, "message": "我们去看月亮吧。", "intent": {"action": "chat"},
        })).raise_for_status()
        assert (await ticket(topic_turn=off_topic))["memory_use"] is None
        assert (await ticket(topic_turn=turn, topic="月亮的背面"))["memory_use"] is None
        temporary = await ticket(topic_turn=turn, topic=related, temporary=True)
        assert temporary["memory_use"] is None and related not in temporary["instructions"]
        prefs = (await client.get("/api/v1/persona/daily-memory/preferences")).json()
        for enabled in (False, True):
            (await client.put("/api/v1/persona/daily-memory/preferences", json={
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
    await app.state.bilibili_account.adapter.close()
    app.state.store.close()


async def test_call_uses_the_same_complete_role_card_across_topics(settings, monkeypatch):
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
    base = "\n".join([persona_instructions(runtime.character), private_participant_instructions(
        runtime.character, operating_account=operating_account_facts(runtime.account.status()))])
    assert all(word in base for word in ("码头看守", "轻声慢讲", runtime.character["name"]))
    monkeypatch.setattr(private_call, "private_service", lambda settings: ("https://voice.example", "test-key"))

    async def mint(*args):
        return "test-ticket"

    monkeypatch.setattr(private_call, "mint_ticket", mint)
    path = "/api/v1/agent/private-call"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        async def ticket(**body):
            return (await client.post(path + "/ticket", json=body)).raise_for_status().json()

        query = "聊聊白塔码头吧。"
        assert (await ticket())["instructions"] == base
        assert (await ticket(topic="晚饭吃西瓜吗"))["instructions"] == base
        assert (await ticket(temporary=True, topic=query))["instructions"] == base
        explicit = await ticket(topic=query)
        assert explicit["instructions"] == base
        assert all(base.count(text) == 1 for text in (passage, unrelated, "码头看守", card["core"]["identity"]))
        turn = str(uuid4())
        (await client.post("/api/v1/agent/chat", json={
            "request_id": turn, "message": query, "intent": {"action": "chat"},
        })).raise_for_status()
        inherited = await ticket(topic_turn=turn)
        assert inherited["instructions"] == explicit["instructions"]
        assert not inherited["memory_citations"] and inherited["memory_use"] is None
        assert inherited.get("topic_source") is None
        runtime.store.execute("UPDATE operator_chat_turns SET message=? WHERE request_id=?", ("今天吃西瓜吧。", turn))
        assert (await ticket(topic_turn=turn))["instructions"] == base
        runtime.store.execute("DELETE FROM operator_chat_turns WHERE request_id=?", (turn,))
        assert (await ticket(topic_turn=turn))["instructions"] == base
    await app.state.bilibili_account.adapter.close()
    app.state.store.close()


@pytest.mark.parametrize("behind_edge", [True, False])
def test_ticket_is_admin_only_fixed_upstream_and_contains_no_service_credential(settings, tmp_path, monkeypatch, behind_edge):
    key = tmp_path / "voice.key"
    key.write_text("private-application-fixture")
    settings.data["omni"]["base_url"] = "https://voice.example.org/v1"
    settings.provider_keys = {"omni": key.read_text()}
    monkeypatch.setenv("OMNI_GATEWAY_URL", "https://voice.example.org")
    if behind_edge:
        monkeypatch.setenv("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN", "https://192.0.2.10")
    issued = []

    async def issue(client, request, **kwargs):
        issued.append(request)
        assert not client.follow_redirects
        return httpx.Response(200, request=request, json={"ticket": "a" * 32, "expires_in": 30, "secret": key.read_text()})

    app = create_app(settings)
    with TestClient(app) as browser:
        monkeypatch.setattr(httpx.AsyncClient, "send", issue)
        path = "/api/v1/agent/private-call/ticket"
        auth = {"Authorization": "Bearer " + settings.admin_token}
        assert browser.post(path).status_code == 401
        assert browser.get("/api/v1/agent/private-call/reference").status_code == 401
        assert browser.post(path, headers=auth | {"Origin": "https://elsewhere.example"}).status_code == 403
        assert browser.get("/api/v1/agent/private-call", headers=auth).json() == {"configured": True, "character": "Aqua"}
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
        listed = browser.get("/api/v1/agent/private-call", headers=auth).json()["recordings"]
        assert set(listed) == {"mature"}
        path = listed["mature"]
        assert browser.get(path).status_code == 401
        clip = browser.get(path, headers=auth | {"Range": "bytes=0-3"})
        assert (clip.status_code, clip.content) == (206, b"priv")
        assert "no-store" in clip.headers["cache-control"]
        assert browser.get(path.replace("/" + role + "/", "/another/"), headers=auth).status_code == 404
        assert browser.get(path.replace("mature.mp4", "q.mp4"), headers=auth).status_code == 404


def test_private_signing_follows_only_the_current_credential_and_secure_browser_route(settings, monkeypatch):
    monkeypatch.setenv("OMNI_GATEWAY_URL", "http://omni-gateway:8000")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN", "https://192.0.2.10")
    settings.data["omni"]["base_url"] = "http://omni-gateway:8000/v1"
    assert private_call.private_service(settings) is None
    settings.provider_keys = {"omni": "app-key"}
    assert private_call.private_service(settings) == ("http://omni-gateway:8000", "app-key")
    settings.data["omni"]["base_url"] = "https://changed.example/v1"
    assert private_call.private_service(settings) is None
    settings.provider_keys = {"omni": "new-key"}
    assert private_call.browser_socket_origin("https://changed.example") == "wss://changed.example"
    settings.data["omni"]["base_url"] = "http://192.0.2.11/v1"
    settings.provider_keys = {"omni": "network-key"}
    assert private_call.private_service(settings) is None


@pytest.mark.parametrize("data", [{"ticket": "app-credential-do-not-echo"}, {}, {"ticket": 5}])
async def test_bad_upstream_replies_fail_closed(data, monkeypatch):
    async def issue(client, request, **kwargs):
        return httpx.Response(200, request=request, json=data)
    monkeypatch.setattr(httpx.AsyncClient, "send", issue)
    with pytest.raises(ValueError):
        await private_call.mint_ticket("https://voice.example", "app-credential-do-not-echo")
