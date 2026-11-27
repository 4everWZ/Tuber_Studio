"""Tests for strict ChatGPT Two-Layer Memory, soft cap, purge, boot isolation, and tool executors."""

import json
import time

from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.app.runtime import Runtime
from tuber_studio.persona.memory.recall import (
    execute_recall_history_tool,
    recall_history,
    recall_history_tool_schema,
)
from tuber_studio.persona.memory.unified import (
    DEFAULT_MEMORY_LIMIT,
    UnifiedMemoryStore,
    delete_memory,
    execute_saved_memory_tool,
    migrate_legacy_memories,
    save_memory,
    saved_memory_tool_schemas,
    update_memory,
)


def _seed_legacy_tables(store):
    now = time.time()
    store.execute(
        "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,headline,source_ref,"
        "source_user,quote,anecdote,tags,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "whale",
            1,
            "2026-11-10",
            "public_incident",
            "评论区热梗",
            "operator-public:1",
            "viewer",
            "原评说海带",
            "在评论区和观众聊起海带炖豆腐",
            '["美食"]',
            "active",
            now,
            now,
        ),
    )
    store.execute(
        "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,headline,source_ref,"
        "source_user,quote,anecdote,tags,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "whale",
            1,
            "2026-11-10",
            "owner_preference",
            "私密作息",
            "turn:req-99",
            "owner",
            "以后晚上十一点后不要吵我",
            "晚上十一点后保持安静",
            "[]",
            "active",
            now,
            now,
        ),
    )
    store.execute(
        "CREATE TABLE IF NOT EXISTS grounded_memory("
        "id INTEGER PRIMARY KEY,scope TEXT NOT NULL,character_id TEXT NOT NULL,"
        "character_version INTEGER NOT NULL,event_id TEXT NOT NULL,role TEXT NOT NULL,"
        "text TEXT NOT NULL,kind TEXT NOT NULL,fictional INTEGER NOT NULL,"
        "facts TEXT NOT NULL,created_at REAL NOT NULL,"
        "UNIQUE(scope,character_id,character_version,event_id,role))"
    )
    store.execute(
        "INSERT INTO grounded_memory(scope,character_id,character_version,event_id,role,text,kind,fictional,facts,created_at) "
        "VALUES('operator:whale','whale',1,'op-1','system','私人工作提醒','operator_note',0,'[]',?)",
        (now,),
    )


def test_runtime_boot_skips_legacy_auto_migration_and_purge_preserves_legacy_tables(settings, store):
    _seed_legacy_tables(store)
    runtime = Runtime(settings, store)
    assert runtime.unified_memory.list_memories() == []
    assert runtime.unified_memory.stats(character_id="whale")["legacy_migrated_count"] == 0

    # Even if legacy rows were previously migrated, purge_migrated_memories cleans ONLY mig:* rows
    migrated = migrate_legacy_memories(store, unified=runtime.unified_memory)
    assert migrated == 3
    manual = runtime.unified_memory.add_memory("手工保留的偏好：喜欢红茶", scope="owner", character_id="whale")
    assert runtime.unified_memory.legacy_migrated_count(character_id="whale") == 3

    purged = runtime.unified_memory.purge_migrated_memories()
    assert purged == 3
    assert runtime.unified_memory.legacy_migrated_count() == 0
    remaining = runtime.unified_memory.list_memories()
    assert [item["id"] for item in remaining] == [manual["id"]]
    # Legacy archive tables remain completely intact
    assert len(store.all("SELECT * FROM daily_highlights")) == 2
    assert len(store.all("SELECT * FROM grounded_memory")) == 1

    # Re-booting Runtime after purge MUST NOT re-create mig:* rows
    store.execute("DELETE FROM kv WHERE key='unified_memories_migrated_v1'")
    runtime2 = Runtime(settings, store)
    assert runtime2.unified_memory.legacy_migrated_count() == 0
    assert [item["id"] for item in runtime2.unified_memory.list_memories()] == [manual["id"]]


def test_saved_memory_soft_cap_and_openai_tool_executors(store):
    assert DEFAULT_MEMORY_LIMIT == 100
    unified = UnifiedMemoryStore(store)

    chat_tools = saved_memory_tool_schemas(format="chat")
    assert [t["function"]["name"] for t in chat_tools] == ["save_memory", "update_memory", "delete_memory"]
    resp_tools = saved_memory_tool_schemas(format="responses")
    assert [t["name"] for t in resp_tools] == ["save_memory", "update_memory", "delete_memory"]

    # Create + dedup + update + delete
    res1 = save_memory(unified, content="喜欢蓝白配色的桌面主题", scope="owner", character_id="whale", tags=["桌面"])
    assert res1["ok"] is True and res1["action"] == "created"
    assert res1["count"] == 1 and res1["limit"] == 100 and res1["soft_limit_reached"] is False

    dup = save_memory(unified, content="喜欢蓝白配色的桌面主题", scope="owner", character_id="whale", tags=["偏好"])
    assert dup["ok"] is True and dup["action"] == "deduplicated"
    assert dup["count"] == 1

    upd = update_memory(
        unified,
        match_query="桌面主题",
        content="喜欢深海蓝白配色主题",
        scope="owner",
        character_id="whale",
    )
    assert upd["ok"] is True and upd["action"] == "updated"
    assert upd["item"]["content"] == "喜欢深海蓝白配色主题"

    # Cross-scope security: public caller cannot save/update/delete owner memory
    cross_save = save_memory(
        unified,
        content="越权写入",
        scope="owner",
        character_id="whale",
        allowed_scopes=("public",),
    )
    assert cross_save["ok"] is False and cross_save["action"] == "rejected"

    cross_upd = update_memory(
        unified,
        memory_id=res1["memory_id"],
        content="篡改",
        scope="public",
        character_id="whale",
    )
    assert cross_upd["ok"] is False and cross_upd["action"] == "rejected"

    cross_del = delete_memory(
        unified,
        memory_id=res1["memory_id"],
        scope="public",
        character_id="whale",
    )
    assert cross_del["ok"] is False and cross_del["action"] == "rejected"

    # Delete by query
    deleted = execute_saved_memory_tool(
        unified,
        "delete_memory",
        json.dumps({"query": "深海蓝白"}),
        scope="owner",
        character_id="whale",
    )
    assert deleted["ok"] is True and deleted["action"] == "deleted" and deleted["count"] == 0

    # Soft cap enforcement in tool executor at DEFAULT_MEMORY_LIMIT (100)
    for i in range(DEFAULT_MEMORY_LIMIT):
        unified.add_memory(f"稳定事实第{i}条", scope="public", character_id="whale")
    capped = save_memory(unified, content="第101条新记忆", scope="public", character_id="whale")
    assert capped["ok"] is False and capped["action"] == "limit_reached"
    assert capped["count"] == 100 and capped["limit"] == 100 and capped["soft_limit_reached"] is True


def test_layer2_recall_history_stays_isolated_and_never_dumps_into_saved_memories(store):
    _seed_legacy_tables(store)
    now = time.time()
    store.execute(
        "CREATE TABLE IF NOT EXISTS operator_chat_turns ("
        "request_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, character_version INTEGER NOT NULL, "
        "message TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    store.execute(
        "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
        (
            "req-chat-1",
            "whale",
            1,
            "昨晚聊的海带汤记得吗？",
            json.dumps({"reply_text": "当然记得，你说下次放点豆腐。", "account_id": "100"}, ensure_ascii=False),
            now,
        ),
    )
    store.execute(
        "CREATE TABLE IF NOT EXISTS browsing_comment_timeline("
        "account_id TEXT,character_id TEXT,resource TEXT,root_id TEXT,comment_id TEXT,"
        "parent_comment_id TEXT,author_uid TEXT,author_name TEXT,text TEXT,created_at REAL,"
        "resource_author_uid TEXT,PRIMARY KEY(account_id,character_id,resource,root_id,comment_id))"
    )
    store.execute(
        "INSERT INTO browsing_comment_timeline VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("100", "whale", "BV1xx411c7mD", "500", "501", None, "301", "路人甲", "这期海带汤视频太香了", now, "5001"),
    )

    unified = UnifiedMemoryStore(store)
    schema = recall_history_tool_schema(format="chat")
    assert schema["function"]["name"] == "recall_history"
    assert "scope" not in schema["function"]["parameters"]["properties"]

    owner_res = recall_history(store, query="海带", scope="owner", character_id="whale", account_id="100")
    assert owner_res["ok"] is True and owner_res["scope"] == "owner"
    origins = {item["origin"] for item in owner_res["items"]}
    assert "operator_chat" in origins
    assert "daily_highlight" in origins

    # Public recall NEVER returns owner chat turns or private highlights
    pub_res = execute_recall_history_tool(
        store,
        {"query": "十一点", "days": 30},
        scope="public",
        character_id="whale",
        account_id="100",
    )
    assert pub_res["ok"] is True and pub_res["items"] == []

    pub_public = recall_history(store, query="海带", scope="public", character_id="whale", account_id="100")
    assert pub_public["ok"] is True
    assert all(item["origin"] != "operator_chat" for item in pub_public["items"])
    assert {item["origin"] for item in pub_public["items"]} == {"daily_highlight", "public_comment"}

    # Layer 2 recall NEVER auto-writes to Layer 1 memories
    assert unified.count_memories() == 0


def test_public_history_preserves_organized_original_author_and_excludes_unbound_note(store):
    from tuber_studio.persona.memory.daily import DailyMemoryService

    DailyMemoryService(store)
    timestamp = time.time()
    quote = "本鱼每天都会读屏幕里的日记"
    original = {"comment_id": "501", "author_uid": "215", "author_name": "别家的鱼",
                "text": quote, "href": "https://www.bilibili.com/video/BV1xx411c7mD#reply501", "kind": "comment"}
    proof = {"version": 1, "kind": "viewer", "memory_kind": "experience", "confirmed_by": "source_match",
             "operation_account_id": "100", "source_ref": "thread:example", "quote": quote,
             "memory_source": {"kind": "comment", "references": [original],
                               "thread": {"resource": "BV1xx411c7mD", "comment_id": "501"}}}
    sql = ("INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,headline,source_ref,"
           "quote,anecdote,tags,created_at,updated_at,source_evidence) "
           "VALUES('whale',1,'2026-11-27','experience','概括标题',?,?,?,'[]',?,?,?)")
    store.execute(sql, ("thread:example", quote, "未绑定原文的私有摘要", timestamp, timestamp, json.dumps(proof)))
    store.execute(sql, ("thread:unknown", "未核实的屏幕故事", "不该给公开渠道的猜测", timestamp, timestamp,
                        json.dumps({"kind": "viewer", "memory_kind": "experience"})))
    public = recall_history(store, query="屏幕", scope="public", character_id="whale", account_id="100")["items"]
    item, = public
    assert item["material_kind"] == "public_comment" and item["author_uid"] == "215"
    assert item["href"] == original["href"] and quote in item["text"]
    assert "私有摘要" not in str(item) and "猜测" not in str(item)
    assert recall_history(store, query="屏幕", scope="public", character_id="whale", account_id="101")["items"] == []


def test_persona_memories_admin_endpoints_stats_purge_and_clear(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/api/v1/system/auth/login", json={"token": settings.admin_token}).raise_for_status()
        runtime = app.state.runtime
        _seed_legacy_tables(runtime.store)
        migrate_legacy_memories(runtime.store, unified=runtime.unified_memory)
        client.post(
            "/api/v1/persona/memories",
            json={"scope": "owner", "content": "手动保存的常驻事实"},
        ).raise_for_status()

        listed = client.get("/api/v1/persona/memories").raise_for_status().json()
        assert listed["limit"] == 100
        assert listed["legacy_migrated_count"] == 3
        assert listed["count"] == 4
        assert listed["over_limit"] is False

        purged = client.post("/api/v1/persona/memories/purge-migrated").raise_for_status().json()
        assert purged["ok"] is True and purged["purged"] == 3
        assert purged["legacy_migrated_count"] == 0 and purged["count"] == 1
        # Legacy highlights remain intact
        assert len(runtime.store.all("SELECT * FROM daily_highlights")) == 2

        client.post(
            "/api/v1/persona/memories",
            json={"scope": "public", "content": "公开人设记住一条"},
        ).raise_for_status()
        cleared_pub = client.delete("/api/v1/persona/memories", params={"scope": "public"}).raise_for_status().json()
        assert cleared_pub["ok"] is True and cleared_pub["deleted"] == 1
        assert client.get("/api/v1/persona/memories").raise_for_status().json()["count"] == 1

        cleared_all = client.post("/api/v1/persona/memories/clear").raise_for_status().json()
        assert cleared_all["ok"] is True and cleared_all["deleted"] == 1
        assert client.get("/api/v1/persona/memories").raise_for_status().json()["count"] == 0
