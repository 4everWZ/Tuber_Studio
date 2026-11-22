import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent.memory_organizer import MemoryOrganizer, MemorySuggestion
from tuber_studio.app import create_app
from tuber_studio.core.models import Reply
from tuber_studio.operations.kernel.models import BrowsingPolicy
from tuber_studio.persona.memory.daily import DailyMemoryService


def add_turn(runtime, text, **result):
    scope = runtime.memory_organizer.scope()
    identity = str(uuid4())
    runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                          (identity, *scope[1:], text, json.dumps({"account_id": scope[0], **result}), time.time()))
    return identity


def test_owner_memory_switch_follows_character_across_operating_accounts(settings):
    app = create_app(settings)
    path = "/api/daily-memory/preferences"
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        runtime = app.state.runtime
        discovery = app.state.browsing.discovery
        discovery.remember_account("100")
        before = client.get(path).raise_for_status().json()
        assert before["enabled"] and before["revision"] == 0
        disabled = client.put(path, json={"enabled": False, "scope": before["scope"]}).raise_for_status().json()
        assert not disabled["enabled"] and disabled["revision"] == 1
        assert client.get("/api/daily-memory").json()["organization"]["status"] == "disabled"
        assert MemoryOrganizer(runtime).state()["status"] == "disabled"
        discovery.remember_account("200")
        assert not client.get(path).json()["enabled"]
        assert client.put(path, json={"enabled": False, "scope": before["scope"]}).status_code == 409
        discovery.remember_account("100")
        other = client.post("/api/characters", json={"name": "另一个角色"}).raise_for_status().json()
        client.post(f"/api/characters/{other['id']}/activate").raise_for_status()
        assert client.get(path).json()["enabled"]
        client.post("/api/characters/whale/activate").raise_for_status()
        assert client.get(path).json() == disabled


def test_memory_summary_ask_and_correct_existing_note(settings):
    app = create_app(settings)
    path = "/api/daily-memory"
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        runtime = app.state.runtime
        discovery = app.state.browsing.discovery
        discovery.remember_account("100")
        scope = runtime.memory_organizer.scope()
        source_text = "以后运营汇报先讲结论"
        source = {"key": "turn:summary-source", "kind": "private", "text": source_text, "at": time.time()}
        with runtime.store.db:
            runtime.daily_memory.save_organized(scope, source, MemorySuggestion(
                action="add", existing_id=None, headline="运营汇报顺序", note=source_text, tags=["汇报"],
            ), None)

        report = client.get(path).raise_for_status().json()
        token = report["scope"]
        item, = report["organized"]
        assert report["summary_updated_at"] == item["updated_at"]
        assert report["summary"][0]["notes"][0]["id"] == item["id"]
        client.put(path + "/preferences", json={"scope": token, "enabled": False}).raise_for_status()

        def interact(action, text, scope_token=token):
            return client.post(path + "/summary", json={"scope": scope_token, "action": action, "text": text})

        assert interact("ask", "你记得我什么？").raise_for_status().json()["items"] == [item]
        assert interact("ask", "你记得我对运营汇报有什么要求？").raise_for_status().json()["items"] == [item]
        assert interact("ask", "天气预报").raise_for_status().json()["status"] == "no_match"
        updated = interact("update", "把「运营汇报顺序」改成以后运营汇报先讲风险").raise_for_status().json()
        assert updated["status"] == "updated"
        assert updated["item"]["id"] == item["id"]
        assert updated["item"]["anecdote"] == "以后运营汇报先讲风险"
        assert updated["item"]["quote"] == "以后运营汇报先讲风险"
        edit = f"{path}/organized/{item['id']}?scope={token}"
        assert client.patch(edit + f"&revision={item['updated_at']}", json={"note": "过期更正"}).status_code == 412
        discovery.remember_account("200")
        assert interact("ask", "你记得我什么？").status_code == 409
        other = client.get(path).raise_for_status().json()
        assert interact("ask", "你记得我什么？", other["scope"]).raise_for_status().json()["items"] == []
        discovery.remember_account("100")
        client.delete(edit).raise_for_status()
        assert interact("ask", "你记得我什么？").raise_for_status().json()["items"] == []
        assert client.get(path).raise_for_status().json()["summary_updated_at"] is None


def test_memory_summary_proposal_requires_exact_request_text_and_applies_once(settings):
    app = create_app(settings)
    path = "/api/daily-memory"
    content = "以后运营汇报先看风险"
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        runtime = app.state.runtime
        scope = runtime.memory_organizer.scope()
        original = "以后运营汇报先讲结论"
        source = {"key": "turn:proposal-source", "kind": "private", "text": original, "at": time.time()}
        with runtime.store.db:
            runtime.daily_memory.save_organized(scope, source, MemorySuggestion(
                action="add", existing_id=None, headline="运营汇报顺序", note=original, tags=["汇报"],
            ), None)
        report = client.get(path).raise_for_status().json()
        item, = report["organized"]
        body = {"scope": report["scope"], "action": "update", "text": f"运营汇报这条我改主意了，{content}"}
        replacement = "以后运营汇报先讲进度"

        async def propose(messages, schema, *, max_tokens):
            assert json.loads(messages[-1]["content"])["candidates"] == [
                {"item_id": item["id"], "title": item["headline"], "current": original},
            ]
            return {"item_id": item["id"], "replacement": replacement}

        runtime.llm.generate_json = propose
        assert client.post(path + "/summary", json=body).raise_for_status().json()["status"] == "clarify"
        replacement = content
        proposal = client.post(path + "/summary", json=body).raise_for_status().json()
        assert (proposal["status"], proposal["note"], proposal["item"]) == ("proposal", content, item)
        assert client.get(path).raise_for_status().json()["organized"] == [item]
        edit = f"{path}/organized/{item['id']}?scope={report['scope']}&revision={item['updated_at']}"
        assert client.patch(edit, json={"note": proposal["note"]}).raise_for_status().json()["anecdote"] == content
        assert client.patch(edit, json={"note": proposal["note"]}).status_code == 412


def test_legacy_notes_require_review_without_hiding_source_or_explicit_correction(settings):
    app = create_app(settings)
    path = "/api/daily-memory"
    original = "海豹在换毛吗？"
    unverified = "已经核实八千只在换毛。"
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        runtime = app.state.runtime
        app.state.browsing.discovery.remember_account("100")
        scope = runtime.memory_organizer.scope()
        memory = runtime.daily_memory
        href = "https://www.bilibili.com/video/BV1d8pU6xEZX#reply40"
        source = {"key": "event:" + href, "kind": "experience", "source_kind": "comment", "text": original,
                  "at": time.time(), "references": [{"href": href, "text": original}]}
        proposal = MemorySuggestion(action="add", existing_id=None, headline="海豹", note=original, tags=["海豹"])
        with runtime.store.db:
            assert memory.save_organized(scope, source, proposal, None)
            before, = memory.organized(scope)
            runtime.store.db.execute(
                "UPDATE daily_highlights SET headline=?,anecdote=?,tags=?,"
                "source_evidence=json_remove(source_evidence,'$.owner_provenance','$.memory_source.kind') WHERE id=?",
                ("八千只", unverified, json.dumps(["海豹", "八千只"]), before["id"]),
            )
        report = client.get(path).raise_for_status().json()
        assert report["organized"][0]["anecdote"] == unverified
        assert report["organized"][0]["source"]["references"][0]["text"] == original
        assert report["summary"][0]["notes"][0]["text"] == original
        assert not memory.recall_organized(scope, "operator", "八千只")
        item, = memory.recall_organized(scope, "operator", "海豹")
        assert (item["headline"], item["anecdote"]) == ("海豹", original)
        body = {"scope": report["scope"], "action": "ask", "text": "你记得我什么？"}
        asked, = client.post(path + "/summary", json=body).raise_for_status().json()["items"]
        assert asked["anecdote"] == original and asked["headline"] == "海豹"

        correction = "号主确认已核对原片段。"
        edit = f"{path}/organized/{before['id']}?scope={report['scope']}"
        client.patch(edit, json={"note": correction, "headline": "海豹记录"}).raise_for_status()
        approved, = client.post(path + "/summary", json=body).raise_for_status().json()["items"]
        assert (approved["anecdote"], approved["headline"]) == (correction, "海豹记录")
        shared, = memory.recall_organized(scope, "live", "海豹")
        assert shared["anecdote"] == f"「{original}」"


async def test_switch_blocks_inflight_recall_and_discards_inflight_organization(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    memory = runtime.daily_memory
    organizer = runtime.memory_organizer
    base = runtime.llm
    organizer_model = OrganizerModel()
    app.state.browsing.discovery.remember_account("100")
    scope = organizer.scope()
    used = "以后运营汇报先说结论"
    try:
        runtime.operation_llm = organizer_model
        add_turn(runtime, used)
        assert await organizer.run_once() == 1
        row, = memory.organized(scope)

        async def generate(messages, _character, _event_id, _source_text):
            assert any(used in item["content"] for item in messages)
            memory.set_preference(scope, False)
            memory.set_preference(scope, True)
            return Reply(spoken_text=used)

        base.generate = generate
        with pytest.raises(ValueError, match="记忆设置已变更"):
            await runtime.generate_reply("operator:probe", runtime.character,
                                         {"event_id": "probe", "text": "运营汇报"}, mode="operator_chat")
        memory.set_preference(scope, False)
        assert not memory.recall_organized(scope, "operator", "运营汇报")
        assert not app.state.dynamics._sources()
        assert memory.organized(scope) == [row]
        assert await organizer.run_once() == 0

        started, resume = asyncio.Event(), asyncio.Event()

        class BlockingOrganizer(OrganizerModel):
            async def generate_json(self, messages, schema, *, max_tokens):
                started.set()
                await resume.wait()
                return await super().generate_json(messages, schema, max_tokens=max_tokens)

        runtime.operation_llm = BlockingOrganizer()
        turnover = add_turn(runtime, "以后运营汇报先说风险")
        memory.set_preference(scope, True)
        task = asyncio.create_task(organizer.run_once())
        await asyncio.wait_for(started.wait(), timeout=3)
        memory.set_preference(scope, False)
        resume.set()
        assert await task == 0
        assert memory.organized(scope) == [row]
        assert not runtime.store.one("SELECT 1 FROM memory_organization WHERE source=?", (f"turn:{turnover}",))
        runtime.operation_llm = organizer_model
        memory.set_preference(scope, True)
        assert await organizer.run_once() == 1
        assert memory.organized(scope)[0]["anecdote"].endswith("先说风险")
    finally:
        await base.close()
        await app.state.browsing.close()
        app.state.store.close()


class OrganizerModel:
    async def generate_json(self, messages, schema, *, max_tokens):
        if schema["title"] == "OwnerUtterance":
            text = messages[-1]["content"]
            return {"meaning": "持续合作方式", "statement": text, "scope_quote": "以后",
                    "practice_quote": text.split("以后", 1)[1]}
        packet = json.loads(messages[-1]["content"])
        if schema["title"] == "ChangeOfPractice":
            return {"relation": "新做法取代原做法", "common_reference": "运营汇报",
                    "old_value": packet["original"], "new_value": packet["new"]}
        if schema["title"] == "AgreementReview":
            existing = packet["existing"]
            return {"meaning": "号主本人的持续协作约定", "scope_supported": True, "practice_supported": True,
                    "same_agreement": True, "topic_quote": "运营汇报", "existing_id": existing[0]["id"] if existing else None,
                    "summary_kind": "preference"}
        items = {}
        for item in packet:
            existing = item["existing"]
            items[item["key"]] = {"action": "update" if existing else "add",
                          "existing_id": existing[0]["id"] if existing else None, "headline": "运营汇报顺序",
                          "note": item["original"], "tags": ["运营汇报"]}
        return items


async def test_queued_private_chat_correction_preserves_both_turns_and_versions(settings):
    class ChatProvider(OrganizerModel):
        async def generate_assistant_json(self, messages, schema, **kwargs):
            if schema.get("title") == "TurnRoute":
                return {"route": "chat", "action": "chat"}
            return {"reply_markdown": "好，我们接着聊。"}

    app = create_app(settings)
    runtime = app.state.runtime
    original = runtime.llm
    runtime.llm = ChatProvider()
    runtime.operation_llm = OrganizerModel()
    organizer = runtime.memory_organizer
    turns = [(str(uuid4()), text) for text in ("以后运营汇报先说结论", "我改主意了，以后运营汇报先说风险")]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost",
                                     headers={"Authorization": "Bearer " + settings.admin_token}) as client:
            pref = (await client.get("/api/daily-memory/preferences")).raise_for_status().json()
            assert pref["enabled"] and pref["revision"] == 0
            for request_id, quote in turns:
                reply = await client.post("/api/chat/intelligent", json={"request_id": request_id, "message": quote})
                assert reply.raise_for_status().json()["card"] is None
            assert await organizer.run_once() == 2
            assert await organizer.run_once() == 0
            scope = organizer.scope()
            latest, = runtime.daily_memory.organized(scope)
            assert latest["revision"] == 2
            assert runtime.store.all("SELECT source_key,quote FROM memory_sources WHERE item_id=? ORDER BY rowid",
                                     (latest["id"],)) == [{"source_key": "turn:" + rid, "quote": text} for rid, text in turns]
            for rid, quote in turns:
                source = "turn:" + rid
                response = await client.post("/api/agent/memory.get", json={"id": latest["id"], "source_key": source})
                if rid != turns[-1][0]:
                    assert response.status_code == 404
                    continue
                item = response.raise_for_status().json()
                assert (item["revision"], item["source_key"], item["quote"], item["note"]) == (
                    2, source, quote, turns[-1][1])
    finally:
        await original.close()
        await app.state.browsing.close()
        runtime.store.close()


async def test_correction_finds_older_topic_and_preserves_qualifying_clause(settings):
    first = "以后运营汇报先说结论。"
    correction = "运营汇报的顺序改一下，以后先说风险。"
    app = create_app(settings)
    runtime = app.state.runtime
    original = runtime.llm
    memory = runtime.daily_memory
    scope = runtime.memory_organizer.scope()

    class ExcerptModel(OrganizerModel):
        async def generate_json(self, messages, schema, **kwargs):
            reply = await super().generate_json(messages, schema, **kwargs)
            if schema["title"] == "OwnerUtterance" and messages[-1]["content"] == correction:
                reply["statement"] = "以后先说风险。"
            if schema["title"] == "AgreementReview":
                packet = json.loads(messages[-1]["content"])
                assert packet["statement"] == packet["original"]
                reply["topic_quote"] = "" if packet["original"] == first else "不在号主原话中的主题"
            return reply

    runtime.operation_llm = ExcerptModel()
    try:
        add_turn(runtime, first)
        assert await runtime.memory_organizer.run_once() == 1
        old, = memory.organized(scope)
        assert old["headline"] == first
        for i in range(101):
            note = f"资料夹编号{i}"
            memory.save_organized(scope, {"kind": "private", "key": f"settings:{i}", "text": note, "at": time.time()},
                                  MemorySuggestion(action="add", existing_id=None, headline=note, note=note, tags=[]), None)
        assert old["id"] not in {row["id"] for row in memory.organized(scope)}
        add_turn(runtime, correction)
        assert await runtime.memory_organizer.run_once() == 1
        latest, = memory.organized(scope, item_id=old["id"])
        assert (latest["revision"], latest["headline"], latest["anecdote"]) == (2, correction, correction)
    finally:
        await original.close()
        await app.state.browsing.close()
        runtime.store.close()


async def test_organization_cannot_create_memory_from_assistant_output(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    base = runtime.llm
    scope = runtime.memory_organizer.scope()
    owner_statement = "以后汇报不要先讲风险，先讲结论。"
    false_claim = "本轮已经看完两千个视频。"
    false_inference = "汇报先讲风险。"

    class UnreliableOrganizer(OrganizerModel):
        async def generate_json(self, messages, schema, *, max_tokens):
            result = await super().generate_json(messages, schema, max_tokens=max_tokens)
            if schema["title"] == "OwnerUtterance":
                assert messages[-1]["content"] == owner_statement
            elif schema["title"] == "AgreementReview":
                result["topic_quote"] = false_inference
            return result

    runtime.operation_llm = UnreliableOrganizer()
    try:
        origin = add_turn(runtime, owner_statement, reply_text=false_claim)
        add_turn(runtime, "核对已观看数量", memory_intent="chat", reply_text=false_claim,
                 submission={"intent": {"action": "full_video"}}, card={"type": "agent_task"})
        assert await runtime.memory_organizer.run_once() == 1
        saved, = runtime.daily_memory.organized(scope)
        assert saved["source"]["key"] == f"turn:{origin}"
        assert saved["anecdote"] == owner_statement
        assert false_claim not in json.dumps(saved, ensure_ascii=False)
        assert false_inference not in json.dumps(saved, ensure_ascii=False)
        corrected = runtime.daily_memory.change_organized(scope, saved["id"], note="以后优先汇报风险。")
        assert corrected["anecdote"] == "以后优先汇报风险。"
        assert corrected["quote"] == corrected["anecdote"]
        assert corrected["source"]["key"].startswith("settings:edit:")
        previous = {"key": "turn:earlier", "kind": "private", "text": "以后先汇报视频。", "at": saved["source"]["at"]}
        proposed = MemorySuggestion(action="update", existing_id=saved["id"], headline=saved["headline"],
                                    note=previous["text"], tags=["汇报"])
        with runtime.store.db:
            assert not runtime.daily_memory.save_organized(scope, previous, proposed, saved)
        assert runtime.daily_memory.organized(scope, item_id=saved["id"])[0]["anecdote"] == corrected["anecdote"]
    finally:
        await base.close()
        await app.state.browsing.close()
        runtime.store.close()


async def test_shared_memory_keeps_owner_correction_after_two_client_restart(settings):
    chat_path = "/api/chat/intelligent"
    memory_path = "/api/daily-memory"
    initial = "以后运营汇报先讲结论。"
    pending = "以后运营汇报先讲进度。"
    correction = "以后运营汇报先讲风险。"
    false_reply = "我昨天去了雾湾驿站，寄给小明三张明信片。"
    false_query = "还记得雾湾驿站吗？"
    organized_sources = []

    class CheckedProvider(OrganizerModel):
        supports_memory_organization = True

        def __init__(self, provider):
            self.provider = provider

        def __getattr__(self, name):
            return getattr(self.provider, name)

        async def generate_json(self, messages, schema, *, max_tokens):
            if schema["title"] == "OwnerUtterance":
                organized_sources.append(messages[-1]["content"])
            assert false_reply not in messages[-1]["content"]
            return await super().generate_json(messages, schema, max_tokens=max_tokens)

        async def generate_assistant_json(self, messages, schema, *, max_tokens, on_content, on_stage):
            if schema.get("title") == "TurnRoute":
                return {"route": "chat", "action": "chat"}
            return {"reply_markdown": false_reply}

    def new_app():
        app = create_app(settings)
        runtime = app.state.runtime
        runtime.llm = CheckedProvider(runtime.llm)
        runtime.operation_llm = CheckedProvider(runtime.operation_llm)
        return app, runtime

    def connected(app, device, cookies=None):
        transport = httpx.ASGITransport(app=app, client=(f"203.0.113.{device}", 52000))
        return httpx.AsyncClient(transport=transport, base_url="https://workbench.example", cookies=cookies)

    async def send(client, message):
        request_id = str(uuid4())
        response = await client.post(chat_path + "/stream", json={"request_id": request_id, "message": message})
        assert "event: done" in response.raise_for_status().text
        return request_id

    async def assert_transcript_is_not_recall(client, runtime, turn, scope_token, *, corrected=False):
        history = (await client.get(chat_path)).raise_for_status().json()["turns"]
        assert any(item["request_id"] == turn and item["reply_text"] == false_reply for item in history)
        asked = await client.post(memory_path + "/summary", json={"scope": scope_token, "action": "ask", "text": false_query})
        assert asked.raise_for_status().json()["items"] == []
        character = runtime.character
        memory_scope = "operator:" + json.dumps(["100", character["id"]], separators=(",", ":"))
        entry = runtime.store.one("SELECT facts,kind FROM grounded_memory WHERE event_id=? AND role='assistant'",
                                  (f"operator:{turn}",))
        assert entry == (None if corrected else {"facts": "[]", "kind": "operator_private"})
        assert false_reply not in str(runtime.context.memory.retrieve(memory_scope, character, false_query)["evidence"])

    app, runtime = new_app()
    async with app.router.lifespan_context(app), connected(app, 10) as first, connected(app, 20) as second:
        runtime.browsing.discovery.remember_account("100")
        for client in (first, second):
            assert (await client.get(memory_path)).status_code == 401
            (await client.post("/api/auth", json={"token": settings.admin_token})).raise_for_status()
        assert first.cookies != second.cookies
        initial_turn = await send(first, initial)
        await runtime.memory_organizer.run_once()
        report = (await second.get(memory_path)).raise_for_status().json()
        token = report["scope"]
        note, = report["organized"]
        assert note["anecdote"] == initial
        await assert_transcript_is_not_recall(second, runtime, initial_turn, token)

        preference = {"scope": token, "enabled": False}
        (await first.put(memory_path + "/preferences", json=preference)).raise_for_status()
        await send(second, pending)
        change = {"scope": token, "action": "update", "text": "把「运营汇报」改成" + correction}
        updated = (await first.post(memory_path + "/summary", json=change)).raise_for_status().json()["item"]
        assert (updated["id"], updated["anecdote"], updated["quote"]) == (note["id"], correction, correction)
        stale_path = f"{memory_path}/organized/{note['id']}"
        stale = await second.patch(stale_path, params={"scope": token, "revision": note["updated_at"]},
                                   json={"note": pending})
        assert stale.status_code == 412
        cookies = (first.cookies, second.cookies)

    app, runtime = new_app()
    async with app.router.lifespan_context(app), connected(app, 10, cookies[0]) as first, connected(app, 20, cookies[1]) as second:
        preference["enabled"] = True
        (await second.put(memory_path + "/preferences", json=preference)).raise_for_status()
        await runtime.memory_organizer.run_once()
        assert organized_sources == [initial, pending]
        for client in (first, second):
            report = (await client.get(memory_path)).raise_for_status().json()
            assert report["organized"] == [updated]
            assert report["summary"][0]["notes"] == [{"id": note["id"], "text": correction, "visibility": "owner"}]
        await assert_transcript_is_not_recall(second, runtime, initial_turn, token, corrected=True)


async def test_bound_collaboration_preference_can_be_corrected_across_bindings(settings, monkeypatch):
    app = create_app(settings)
    runtime = app.state.runtime
    memory, organizer = runtime.daily_memory, runtime.memory_organizer
    runtime.operation_llm = OrganizerModel()
    original = runtime.character
    discovery = app.state.browsing.discovery
    discovery.remember_account("100")
    initial_scope = organizer.scope()

    async def chat(messages, schema, *args, **kwargs):
        if schema.get("title") == "TurnRoute":
            return {"route": "chat", "action": "chat"}
        return {"reply_markdown": "记下了。"}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", chat, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        async def tell(text):
            result = await client.post("/api/chat/intelligent", json={"request_id": str(uuid4()), "message": text})
            result.raise_for_status()

        await tell("以后运营汇报先说结论")
        discovery.remember_account("200")
        runtime.store.save_character(original)
        assert await organizer.run_once() == 1
        first, = memory.organized(initial_scope)
        scope = organizer.scope()
        assert scope != initial_scope
        assert memory.recall_organized(scope, "operator", "运营汇报")[0]["id"] == first["id"]
        await tell("我改主意了，以后运营汇报先说风险")
        assert await organizer.run_once() == 1
        latest, = memory.organized(scope)
        assert latest["id"] == first["id"] and latest["quote"].endswith("先说风险")
        assert latest["source"]["scope"] == list(scope) and latest["proof"]["operation_account_id"] == "100"
        for channel in ("live", "comment", "direct_message"):
            assert not memory.recall_organized(scope, channel, "运营汇报")
        runtime.store.save_character(original | {"id": "another"})
        runtime.store.activate("another")
        assert not memory.organized(organizer.scope())
        assert not memory.change_organized(organizer.scope(), first["id"], note="不应修改")
        runtime.store.activate(original["id"])
        assert memory.change_organized(organizer.scope(), first["id"], delete=True)["deleted"]
        assert not memory.organized(initial_scope)


async def test_private_memory_updates_after_worker_restart_and_stays_private(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    provider = runtime.llm
    runtime.operation_llm = OrganizerModel()
    app.state.browsing.discovery.remember_account("100")
    scope = runtime.memory_organizer.scope()
    try:
        task_id = add_turn(runtime, "本场只讲 BV 的 P2 结论，验收样例有内部链接", memory_intent="live_handoff")
        temporary = {"key": f"turn:{task_id}", "kind": "private", "text": "本场只讲视频结论", "at": time.time()}
        with runtime.store.db:
            runtime.daily_memory.save_organized(scope, temporary, MemorySuggestion(
                action="add", existing_id=None, headline="本场直播", note="本场只讲视频结论", tags=["直播"],
            ), None)
            runtime.store.db.execute(
                "UPDATE daily_highlights SET source_evidence=json_remove(source_evidence,'$.summary_kind') WHERE source_ref=?",
                (temporary["key"],),
            )
        assert not runtime.daily_memory.summary(scope)
        assert not runtime.daily_memory.recall_organized(scope, "operator", "本场只讲视频结论")
        add_turn(runtime, "以后运营汇报先说结论")
        assert await runtime.memory_organizer.run_once() == 1
        first, = runtime.daily_memory.recall_organized(scope, "operator", "汇报")
        assert first["quote"] == "以后运营汇报先说结论"
        assert not runtime.daily_memory.get_active_anecdotes(*scope[1:])
        assert not runtime.daily_memory.recall_organized(scope, "live", "运营汇报")
        runtime.memory_organizer = MemoryOrganizer(runtime)
        assert await runtime.memory_organizer.run_once() == 0
        add_turn(runtime, "我改主意了，以后运营汇报先说风险")
        assert await runtime.memory_organizer.run_once() == 1
        latest, = runtime.daily_memory.recall_organized(scope, "operator", "汇报")
        assert latest["id"] == first["id"] and latest["quote"].endswith("先说风险")
        assert runtime.daily_memory.summary(scope) == [
            {"kind": "preference", "notes": [{"id": latest["id"], "text": latest["anecdote"], "visibility": "owner"}]},
        ]
        assert runtime.store.one("SELECT 1 FROM operator_chat_turns WHERE request_id=?", (task_id,))
        app.state.browsing.discovery.remember_account("200")
        assert not runtime.daily_memory.organized(runtime.memory_organizer.scope())
        app.state.browsing.discovery.remember_account("100")
        assert not runtime.daily_memory.organized((scope[0], scope[1], scope[2] + 1))
        assert not runtime.daily_memory.recall_organized(scope, "operator", "今天吃了什么")
        assert runtime.daily_memory.change_organized(scope, latest["id"], delete=True)["deleted"]
        assert await runtime.memory_organizer.run_once() == 0
        assert not runtime.daily_memory.organized(scope)
    finally:
        await provider.close()
        await app.state.browsing.close()
        app.state.store.close()


async def test_channel_scope_and_revocation_during_live_generation(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    app.state.browsing.discovery.remember_account("100")
    scope = runtime.memory_organizer.scope()
    memory = runtime.daily_memory
    resource = "BV1d8pU6xEZX"
    source = {"key": f"thread:{resource}:40", "kind": "experience", "source_kind": "comment",
              "at": time.time(), "text": "企鹅在排队",
              "anchor": {"resource": resource, "root_id": "40", "comment_id": "40"},
              "references": [{"href": f"https://www.bilibili.com/video/{resource}#reply{n}", "comment_id": str(n),
                              "text": text} for n, text in [(40, "企鹅在排队"), (41, "轮到第三只了")]]}
    proposal = MemorySuggestion(action="add", existing_id=None, headline="企鹅排队",
                                note="轮到第三只企鹅了", tags=["企鹅"])
    try:
        with runtime.store.db:
            assert not memory.save_organized(scope, source | {"source_kind": "visual"}, proposal, None)
            memory.save_organized(scope, source, proposal, None)
        candidate, = memory.organized(scope)
        assert candidate["anecdote"] == "企鹅在排队"
        assert candidate["source"]["references"][1]["text"] == "轮到第三只了"
        assert memory.recall_organized(scope, "comment", "接着说", resource=resource, thread="40")
        assert not memory.recall_organized(scope, "comment", "企鹅", resource=resource, thread="70")
        row, _ = app.state.browsing.repo.create_round(scope[0], runtime.character, BrowsingPolicy().model_dump(), 0, False)
        decision_input = {"thread": {"target": {"bvid": resource, "root": "40", "parent": "40"},
                                     "source": {"comment_id": "40", "text": "企鹅", "author_uid": "51"}}}
        bound = app.state.browsing.decisions._response_actor_data(row, decision_input)
        assert any("企鹅在排队" in item["text"] for item in bound["own_public_experiences"])
        assert "轮到第三只了" not in json.dumps(bound["own_public_experiences"], ensure_ascii=False)
        legacy_ref = {key: candidate[key] for key in ("id", "updated_at")}
        with pytest.raises(ValueError, match="记忆已变更"):
            memory.validate_organized(scope, "comment", [legacy_ref])
        source_snapshot = next(row for row in app.state.dynamics._sources() if row["id"] == f"organized:{candidate['id']}")
        assert "轮到第三只了" not in json.dumps(source_snapshot, ensure_ascii=False)
        dynamic = {"character_id": scope[1], "character_version": scope[2], "sources": [source_snapshot]}
        assert app.state.dynamics._valid_sources(dynamic)
        memory.record_highlight(
            scope[1], scope[2], memory.business_date(time.time()), "community_meme",
            "整理素材", "直播休息时整理素材。", "直播休息时整理素材。",
            source_ref="operator-public:other", operator_confirmed=True,
        )
        other = next(item for item in app.state.dynamics._sources() if item["id"].startswith("experience:"))
        saved = app.state.dynamics._save_decision(runtime.character, False, {
            "action": "post", "reason": "回顾整理素材", "content": "今天整理了素材。", "source_ids": [other["id"]],
            "next_evaluation_seconds": 300,
        }, [other, source_snapshot], schedule=False)
        assert app.state.dynamics._valid_sources(app.state.dynamics._post(saved["post_id"]))
        memory.change_organized(scope, candidate["id"], owner_only=True)
        with pytest.raises(ValueError, match="记忆已变更"):
            runtime.validate_memory_delivery(bound["_memory_audit"])
        assert not app.state.dynamics._valid_sources(dynamic)
        assert not app.state.dynamics._valid_sources(app.state.dynamics._post(saved["post_id"]))
        assert not memory.recall_organized(scope, "live", "企鹅")
        memory.change_organized(scope, candidate["id"], owner_only=False, note="号主家里那只叫松果糖。")
        assert memory.organized(scope, item_id=candidate["id"])[0]["anecdote"] == "号主家里那只叫松果糖。"

        async def generate(messages, character, event_id, source_text):
            assert any("企鹅在排队" in message["content"] for message in messages)
            assert all("轮到第三只了" not in message["content"] for message in messages)
            assert all("松果糖" not in message["content"] for message in messages)
            memory.change_organized(scope, candidate["id"], delete=True)
            return Reply(spoken_text="轮到第三只企鹅了")

        runtime.llm.generate = generate
        with pytest.raises(ValueError, match="记忆已变更"):
            await runtime.generate_reply("live:probe", runtime.character, {"event_id": "probe", "text": "企鹅"})
        assert not memory.recall_organized(scope, "live", "企鹅")
        assert not any(row["id"] == f"organized:{candidate['id']}" for row in app.state.dynamics._sources())
        with runtime.store.db:
            assert not memory.save_organized(scope, source, proposal, None)
    finally:
        await app.state.browsing.close()
        app.state.store.close()


async def test_public_organizer_saves_one_experience_from_legacy_receipt(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    original = runtime.llm
    runtime.operation_llm = OrganizerModel()
    browsing = app.state.browsing
    browsing.discovery.remember_account("100")
    scope = runtime.memory_organizer.scope()
    row, _ = browsing.repo.create_round(scope[0], runtime.character, BrowsingPolicy().model_dump(), 0, False)
    target = {"bvid": "BV1d8pU6xEZX", "parent": "40", "root": "40", "recipient": "51",
              "source_text": "企鹅排队时哪只最着急？"}
    try:
        at = time.time()
        for identifier, status in [("101", "published"), ("102", "publish_uncertain")]:
            runtime.store.execute(
                "INSERT INTO browsing_intents(id,account_id,character_id,character_version,kind,round_id,"
                "status,text,target,receipt,source_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, *scope, "reply_public", row["id"], status, "第二只一直往前探头",
                 json.dumps(target), json.dumps({"rpid": identifier}) if status == "published" else None,
                 identifier, at, at),
            )
        assert await runtime.memory_organizer.run_once() == 1
        candidate, = runtime.daily_memory.organized(scope)
        assert candidate["status"] == "active" and candidate["channels"] == ["live", "comment", "dynamic"]
        matches = runtime.daily_memory.recall_organized(scope, "operator", "企鹅")
        assert [item["id"] for item in matches] == [candidate["id"]]
        assert [item["text"] for item in candidate["source"]["references"]] == [target["source_text"], "第二只一直往前探头"]
        assert await MemoryOrganizer(runtime).run_once() == 0
    finally:
        await original.close()
        await browsing.close()
        runtime.store.close()


def test_migrate_organized_memory_without_restoring_revoked_public_experience(store):
    scope = ("", "whale", 1)
    # A pre-migration DB has JSON evidence, no source ledger or integer revision.
    store.execute("ALTER TABLE daily_highlights ADD COLUMN source_evidence TEXT")
    quote = "第三只企鹅"
    key = "thread:legacy"
    proof = {"version": 1, "kind": "viewer", "confirmed_by": "source_match", "source_ref": key,
             "quote": quote, "memory_kind": "experience", "operation_account_id": "",
             "memory_source": {"key": key, "kind": "comment", "at": 1, "references": []}}
    insert = ("INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,headline,quote,"
              "anecdote,source_ref,status,created_at,updated_at,source_evidence,tags) "
              "VALUES('whale',1,'2026-10-01','community_meme','企鹅',?,?,?,'pending_review',1,1,?,'[]')")
    saved = store.execute(insert, (quote, quote, key, json.dumps(proof))).lastrowid
    revoked = store.execute(insert, (quote, quote, "operator-public:revoked", None)).lastrowid
    memory = DailyMemoryService(store)
    item, = memory.organized(scope)
    assert item["id"] == saved and item["status"] == "active" and not item["channels"]
    assert memory.lookup_source(scope, "operator", key)[0]["quote"] == quote
    assert store.one("SELECT status FROM daily_highlights WHERE id=?", (revoked,))["status"] == "pending_review"
    store.execute("UPDATE daily_highlights SET status='pending_review' WHERE id=?", (saved,))
    reopened = DailyMemoryService(store)
    assert reopened.organized(scope)[0]["status"] == "pending_review"
    assert not reopened.lookup_source(scope, "operator", key)
