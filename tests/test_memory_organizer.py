import json
import time
from uuid import uuid4

import pytest

from tuber_studio.agent.memory_organizer import MemoryOrganizer, MemorySuggestion
from tuber_studio.app import create_app
from tuber_studio.models import Reply
from tuber_studio.operations.browsing.models import BrowsingPolicy


def add_turn(runtime, text):
    scope = runtime.memory_organizer.scope()
    identity = str(uuid4())
    runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                          (identity, *scope[1:], text, json.dumps({"account_id": scope[0]}), time.time()))
    return identity


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
        memory.change_organized(scope, candidate["id"], channels=["live"])
        with pytest.raises(ValueError, match="记忆已变更"):
            runtime.validate_memory_delivery(bound["_memory_audit"])
        assert not app.state.dynamics._valid_sources(dynamic)

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


async def test_public_organizer_uses_legacy_receipt_and_keeps_one_pending_candidate(settings):
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
        assert candidate["status"] == "pending_review" and candidate["channels"] == []
        assert [item["text"] for item in candidate["source"]["references"]] == [target["source_text"], "第二只一直往前探头"]
        assert await MemoryOrganizer(runtime).run_once() == 0
    finally:
        await original.close()
        await browsing.close()
        runtime.store.close()
