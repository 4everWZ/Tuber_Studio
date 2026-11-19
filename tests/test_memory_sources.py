import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent.memory_organizer import MemorySuggestion
from tuber_studio.app import create_app
from tuber_studio.daily_memory import DailyMemoryService, MemoryChanged
from tuber_studio.memory import GroundedMemory
from tuber_studio.store import dump


A = ("100", "whale", 1)
B = ("200", "whale", 2)


def save(memory, scope, key, quote, at, existing=None):
    decision = MemorySuggestion(action="add", existing_id=None, headline="汇报", note=quote, tags=["汇报"])
    source = {"key": key, "text": quote, "kind": "private", "private_owner": "studio-owner", "at": at}
    with memory.store.db:
        return memory.save_organized(scope, source, decision, existing)


def test_owner_memory_switch_moves_with_role_and_preserves_unmapped_opt_out(store):
    memory = DailyMemoryService(store)
    memory.private_owner = "owner-a"
    store.set("memory_preference:100:whale", {"enabled": False, "revision": 3})
    assert memory.preference(A) == {"enabled": False, "revision": 3}
    assert memory.preference(B)["enabled"]
    memory.set_preference(B, False)
    assert not memory.preference(A)["enabled"]
    assert not memory.preference(("", "whale", 8))["enabled"]
    assert memory.preference(("200", "other-character", 1))["enabled"]
    memory.private_owner = "owner-b"
    assert memory.preference(B)["enabled"]
    memory.private_owner = "owner-a"
    memory.set_preference(B, True)
    assert not memory.preference(A)["enabled"]
    assert memory.set_preference(A, True)["enabled"]
    revision = memory.preference(A)["revision"]
    assert memory.set_preference(A, True)["revision"] == revision


def test_portable_sources_revisions_and_atomic_forgetting(store):
    memory = DailyMemoryService(store)
    memory.private_owner = "studio-owner"
    assert save(memory, A, "turn:first", "汇报先说结论", 1)
    first, = memory.organized(B)
    assert save(memory, B, "turn:second", "汇报先说风险", 2, first)
    latest, = memory.organized(A)
    assert save(memory, B, "turn:second", "汇报先说风险", 5)
    assert not save(memory, B, "turn:second", "以其他文字冒充原话", 5)
    assert memory.organized(B)[0]["revision"] == latest["revision"]
    assert latest["revision"] > first["revision"] and latest["id"] == first["id"]
    assert memory.lookup_source(B, "operator", "turn:first")[0]["quote"] == "汇报先说结论"
    assert memory.lookup_source(A, "operator", "turn:second")[0]["quote"] == "汇报先说风险"
    assert not memory.lookup_source(B, "live", "turn:second")
    memory.private_owner = "other"
    assert not memory.lookup_source(A, "operator", "turn:first")
    assert memory.change_organized(A, first["id"], delete=True) is None
    memory.private_owner = "studio-owner"
    with pytest.raises(MemoryChanged):
        memory.change_organized(B, first["id"], delete=True, expected_revision=first["revision"])

    grounded = GroundedMemory(store)
    for scope, event in [(A, "first"), (A, "unrelated"), (B, "second")]:
        grounded.record("operator:" + dump([scope[0], scope[1]]), {"id": scope[1], "version": scope[2]},
                        "operator:" + event, "user", "原话")
    store.execute("CREATE TRIGGER reject_forgetting BEFORE DELETE ON daily_highlights "
                  "BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fault"):
        memory.change_organized(B, first["id"], delete=True)
    assert not memory.source_deleted(A, "turn:first", "studio-owner")
    assert len(store.all("SELECT id FROM grounded_memory")) == 3
    store.execute("DROP TRIGGER reject_forgetting")
    memory.change_organized(B, first["id"], delete=True, expected_revision=latest["revision"])
    assert store.all("SELECT event_id FROM grounded_memory") == [{"event_id": "operator:unrelated"}]
    assert not store.all("SELECT * FROM memory_sources") and not store.all("SELECT * FROM memory_source_aliases")
    assert not store.one("SELECT 1 FROM sqlite_master WHERE name='memory_revisions'")
    # Rebuilding the organizer's per-account cursor cannot undo an owner's deletion.
    store.execute("DELETE FROM memory_organization")
    reopened = DailyMemoryService(store)
    reopened.private_owner = "studio-owner"
    for scope in [A, B]:
        assert not save(reopened, scope, "turn:first", "汇报先说结论", 1)
        assert not reopened.lookup_source(scope, "operator", "turn:second")
    assert save(reopened, B, "turn:new", "汇报先说结论", 3)


def test_exact_grounded_event_precedes_recent_window_and_obeys_source_filter(store):
    allowed = True
    grounded = GroundedMemory(store, source_filter=lambda row: allowed)
    char = {"id": A[1], "version": 1}
    grounded.record("room", char, "entry:old", "user", "一段原话")
    for i in range(205):
        grounded.record("room", char, f"noise:{i}", "user", "后来的消息")
    for evidence in (grounded.retrieve("room", char, "查 entry:old"),
                     grounded.retrieve("room", char, "", event_id="entry:old")):
        assert [row["text"] for row in evidence["evidence"]] == ["一段原话"]
    assert not grounded.retrieve("other", char, "", event_id="entry:old")["evidence"]
    allowed = False
    assert not grounded.retrieve("room", char, "entry:old")["evidence"]


def test_exact_public_aliases_only_disclose_the_shared_original(store):
    memory = DailyMemoryService(store)
    bv = "BV1d8pU6xEZX"
    key = f"thread:{bv}:40"
    quote = "第三只企鹅"
    link = f"https://www.bilibili.com/video/{bv}#reply40"
    source = {"key": key, "text": quote, "kind": "experience", "at": 1, "source_kind": "comment",
              "anchor": {"resource": bv, "root_id": "40", "comment_id": "40"},
              "references": [{"href": link, "text": quote, "author_uid": "51"}]}
    decision = MemorySuggestion(action="add", existing_id=None, headline=quote, note=quote, tags=[])
    with store.db:
        assert memory.save_organized(A, source, decision, None)
    item, = memory.organized(A)
    memory.change_organized(A, item["id"], note="私有补充")
    for key in [key, bv, link, "51"]:
        match, = memory.lookup_source(A, "comment", key)
        assert match["quote"] == quote and "私有补充" not in str(match)
        assert not memory.lookup_source(B, "comment", key)
    assert memory.recall_organized(A, "dynamic", "刚才说的", participant="51")[0]["id"] == item["id"]
    memory.change_organized(A, item["id"], owner_only=True)
    assert not memory.lookup_source(A, "comment", bv)
    assert not memory.recall_organized(A, "dynamic", bv)


def test_clear_chat_cleans_portable_turn_origin_and_preserves_durable_note(settings):
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        runtime = app.state.runtime
        memory, db = runtime.daily_memory, runtime.store.db
        app.state.browsing.discovery.remember_account("200")
        scope = ("100", runtime.character["id"], runtime.character["version"])
        mode = "operator:" + dump(list(scope[:2]))
        result = {"private_owner": memory.private_owner, "owner_source": memory.private_owner, "account_id": "100"}
        runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                              ("old", *scope[1:], "汇报先说风险", dump(result), time.time()))
        runtime.context.window(mode, runtime.character)
        runtime.context.memory.record(mode, runtime.character, "operator:old", "user", "汇报先说风险")
        source = {"kind": "private", "text": "汇报先说风险", "at": 1, "key": "turn:old",
                  "private_owner": memory.private_owner}
        decision = MemorySuggestion(action="add", existing_id=None, headline="汇报", note=source["text"], tags=[])
        assert memory.save_organized(("200", *scope[1:]), source, decision, None)
        assert not db.in_transaction
        assert runtime.store.one("SELECT account FROM memory_sources")["account"] == "100"
        client.delete("/api/chat/intelligent").raise_for_status()
        assert not runtime.store.one("SELECT id FROM grounded_memory WHERE event_id='operator:old'")
        assert not runtime.context.existing(mode, runtime.character)
        assert memory.lookup_source(scope, "operator", "turn:old")[0]["quote"] == source["text"]
def test_upgrading_snapshot_writers_keeps_offline_history_and_only_advances_current_row(store):
    memory = DailyMemoryService(store)
    memory.private_owner = "studio-owner"
    assert not store.one("SELECT 1 FROM sqlite_master WHERE name='memory_revisions'")
    assert save(memory, A, "turn:first", "先讲结论", 1)
    item, = memory.organized(A)
    # Reproduce the prior insert and revision-update trigger contract. The offline
    # snapshot is already present when the upgraded application opens this store.
    store.execute("CREATE TABLE memory_revisions(item_id INTEGER, revision INTEGER, note TEXT)")
    store.execute("INSERT INTO memory_revisions VALUES(?,?,?)", (item["id"], 1, "先讲结论"))
    for name, event in (("insert", "INSERT"), ("update", "UPDATE OF revision")):
        store.execute(f"CREATE TRIGGER memory_revision_{name} AFTER {event} ON daily_highlights BEGIN "
                      "INSERT INTO memory_revisions VALUES(NEW.id,NEW.revision,NEW.anecdote); END")
    store.execute("DROP VIEW superseded_private_turns")
    reopened = DailyMemoryService(store)
    reopened.private_owner = memory.private_owner
    assert save(reopened, A, "turn:correction", "先讲风险", 2, item)
    other = {"kind": "private", "key": "settings:other", "text": "每次核对字幕", "at": 3,
             "private_owner": reopened.private_owner}
    assert reopened.save_organized(A, other, MemorySuggestion(action="add", existing_id=None, headline="字幕",
                                                              note=other["text"], tags=[]), None)
    current, = reopened.organized(A, item_id=item["id"])
    assert current["revision"] == 2 and current["anecdote"] == "先讲风险"
    assert store.all("SELECT * FROM memory_revisions") == [{"item_id": item["id"], "revision": 1, "note": "先讲结论"}]
    assert not reopened.history_turn_available(A, "turn:first", memory.private_owner)
    assert reopened.lookup_source(A, "operator", "turn:first")[0]["quote"] == "先讲结论"


def test_old_settings_edit_has_current_text_and_its_own_source_after_upgrade(store):
    memory = DailyMemoryService(store)
    memory.private_owner = "studio-owner"
    assert save(memory, A, "turn:old", "汇报先说结论", 1)
    row, = memory.organized(A)
    store.execute("DROP VIEW superseded_private_turns")
    store.execute("UPDATE daily_highlights SET anecdote=?, source_evidence=json_set(source_evidence,'$.owner_edit_at',2) "
                  "WHERE id=?", ("先说风险", row["id"]))
    reopened = DailyMemoryService(store)
    reopened.private_owner = memory.private_owner
    current, = reopened.organized(A)
    assert current["source"]["key"].startswith("settings:edit:")
    assert current["quote"] == current["anecdote"] == current["headline"] == "先说风险"
    assert reopened.lookup_source(A, "operator", current["source"]["key"])[0]["quote"] == "先说风险"
    assert not reopened.history_turn_available(A, "turn:old", memory.private_owner)
    assert reopened.lookup_source(A, "operator", "turn:old")[0]["quote"] == "汇报先说结论"
