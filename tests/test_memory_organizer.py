import asyncio
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent.memory_organizer import MemoryOrganizer, MemorySuggestion
from tuber_studio.app import create_app
from tuber_studio.daily_memory import DailyMemoryService
from tuber_studio.models import Reply
from tuber_studio.operations.browsing.models import BrowsingPolicy


def add_turn(runtime, text):
    scope = runtime.memory_organizer.scope()
    identity = str(uuid4())
    runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                          (identity, *scope[1:], text, json.dumps({"account_id": scope[0]}), time.time()))
    return identity


def test_memory_switch_persists_per_account_and_character(settings):
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
        assert client.post("/api/daily-memory/organize").status_code == 409
        assert MemoryOrganizer(runtime).state()["status"] == "disabled"
        discovery.remember_account("200")
        assert client.get(path).json()["enabled"]
        assert client.put(path, json={"enabled": False, "scope": before["scope"]}).status_code == 409
        discovery.remember_account("100")
        other = client.post("/api/characters", json={"name": "另一个角色"}).raise_for_status().json()
        client.post(f"/api/characters/{other['id']}/activate").raise_for_status()
        assert client.get(path).json()["enabled"]
        client.post("/api/characters/whale/activate").raise_for_status()
        assert client.get(path).json() == disabled


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
        runtime.llm = organizer_model
        add_turn(runtime, used)
        assert await organizer.run_once() == 1
        row, = memory.organized(scope)
        runtime.llm = base

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

        runtime.llm = BlockingOrganizer()
        turnover = add_turn(runtime, "以后运营汇报先说风险")
        memory.set_preference(scope, True)
        task = asyncio.create_task(organizer.run_once())
        await asyncio.wait_for(started.wait(), timeout=3)
        memory.set_preference(scope, False)
        resume.set()
        assert await task == 0
        assert memory.organized(scope) == [row]
        assert not runtime.store.one("SELECT 1 FROM memory_organization WHERE source=?", (f"turn:{turnover}",))
        runtime.llm = organizer_model
        memory.set_preference(scope, True)
        assert await organizer.run_once() == 1
        assert memory.organized(scope)[0]["anecdote"].endswith("先说风险")
    finally:
        await base.close()
        await app.state.browsing.close()
        app.state.store.close()


class OrganizerModel:
    async def generate_json(self, messages, schema, *, max_tokens):
        items = {}
        for item in json.loads(messages[1]["content"]):
            existing = item["existing"]
            items[item["key"]] = {"action": "update" if existing else "add",
                          "existing_id": existing[0]["id"] if existing else None, "headline": "运营汇报顺序",
                          "note": item["original"], "tags": ["运营汇报"]}
        return items


async def test_private_memory_updates_after_worker_restart_and_stays_private(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    provider = runtime.llm
    runtime.llm = OrganizerModel()
    app.state.browsing.discovery.remember_account("100")
    scope = runtime.memory_organizer.scope()
    try:
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
    source = {"key": f"thread:{resource}:40", "kind": "experience", "at": time.time(), "text": "企鹅在排队",
              "anchor": {"resource": resource, "root_id": "40", "comment_id": "40"},
              "references": [{"href": f"https://www.bilibili.com/video/{resource}#reply{n}", "comment_id": str(n),
                              "text": text} for n, text in [(40, "企鹅在排队"), (41, "轮到第三只了")]]}
    proposal = MemorySuggestion(action="add", existing_id=None, headline="企鹅排队",
                                note="轮到第三只企鹅了", tags=["企鹅"])
    try:
        with runtime.store.db:
            memory.save_organized(scope, source, proposal, None)
        candidate, = memory.organized(scope)
        assert not memory.recall_organized(scope, "comment", "企鹅", resource=resource, thread="40")
        memory.change_organized(scope, candidate["id"], status="active", channels=["comment", "live", "dynamic"])
        assert memory.recall_organized(scope, "comment", "接着说", resource=resource, thread="40")
        assert not memory.recall_organized(scope, "comment", "企鹅", resource=resource, thread="70")
        row, _ = app.state.browsing.repo.create_round(scope[0], runtime.character, BrowsingPolicy().model_dump(), 0, False)
        decision_input = {"thread": {"target": {"bvid": resource, "root": "40", "parent": "40"},
                                     "source": {"comment_id": "40", "text": "企鹅", "author_uid": "51"}}}
        bound = app.state.browsing.decisions._response_actor_data(row, decision_input)
        assert any(item["text"] == "轮到第三只企鹅了" for item in bound["own_public_experiences"])
        source_snapshot = next(row for row in app.state.dynamics._sources() if row["id"] == f"organized:{candidate['id']}")
        dynamic = {"character_id": scope[1], "character_version": scope[2], "sources": [source_snapshot]}
        assert app.state.dynamics._valid_sources(dynamic)
        runtime.daily_memory.confirm_operator_experience(runtime.character, "other", "直播休息时整理素材。")
        other = next(item for item in app.state.dynamics._sources() if item["id"].startswith("experience:"))
        saved = app.state.dynamics._save_decision(runtime.character, False, {
            "action": "post", "reason": "回顾整理素材", "content": "今天整理了素材。", "source_ids": [other["id"]],
            "next_evaluation_seconds": 300,
        }, [other, source_snapshot], schedule=False)
        assert app.state.dynamics._valid_sources(app.state.dynamics._post(saved["post_id"]))
        memory.change_organized(scope, candidate["id"], channels=["live"])
        with pytest.raises(ValueError, match="记忆已变更"):
            runtime.validate_memory_delivery(bound["_memory_audit"])
        assert not app.state.dynamics._valid_sources(dynamic)
        assert not app.state.dynamics._valid_sources(app.state.dynamics._post(saved["post_id"]))

        async def generate(messages, character, event_id, source_text):
            assert any("第三只企鹅" in message["content"] for message in messages)
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
    runtime.llm = OrganizerModel()
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
        assert candidate["status"] == "active" and candidate["channels"] == []
        matches = runtime.daily_memory.recall_organized(scope, "operator", "运营汇报")
        assert [item["id"] for item in matches] == [candidate["id"]]
        assert [item["text"] for item in candidate["source"]["references"]] == [target["source_text"], "第二只一直往前探头"]
        assert await MemoryOrganizer(runtime).run_once() == 0
    finally:
        await original.close()
        await browsing.close()
        runtime.store.close()


def test_migrate_organized_memory_without_restoring_revoked_public_experience(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    store = runtime.store
    scope = runtime.memory_organizer.scope()
    memory = runtime.daily_memory
    try:
        source = {"key": "thread:legacy", "kind": "experience", "at": time.time(), "text": "第三只企鹅",
                  "references": [{"href": "https://www.bilibili.com/video/BV1d8pU6xEZX#reply40", "text": "第三只企鹅"}]}
        proposal = MemorySuggestion(action="add", existing_id=None, headline="企鹅", note="第三只企鹅", tags=["企鹅"])
        with store.db:
            memory.save_organized(scope, source, proposal, None)
        saved, = memory.organized(scope)
        revoked = memory.confirm_operator_experience(runtime.character, "revoked", "以前公开过的经历")
        store.execute("UPDATE daily_highlights SET status='pending_review' WHERE id IN (?,?)", (saved["id"], revoked))
        migrated = DailyMemoryService(store)
        assert migrated.organized(scope) == [saved]
        assert migrated.organized(scope)[0]["channels"] == []
        assert store.one("SELECT status FROM daily_highlights WHERE id=?", (revoked,))["status"] == "pending_review"
    finally:
        store.close()
