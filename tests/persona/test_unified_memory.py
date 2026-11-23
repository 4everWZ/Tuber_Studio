"""Tests for UnifiedMemoryStore, idempotent legacy migration, strict isolation, and 16-turn floor roles."""

import time

from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.operations.channels.comments.timeline import comment_thread_context
from tuber_studio.persona.memory.unified import UnifiedMemoryStore, migrate_legacy_memories


def test_unified_memory_crud_dedup_and_strict_scope_isolation(store):
    unified = UnifiedMemoryStore(store)
    assert unified.add_memory("喜欢在晚上喝热可可", scope="owner", character_id="whale")["scope"] == "owner"
    # Deduplicates same normalized text within same scope & character
    dup = unified.add_memory("喜欢在晚上喝热可可", scope="owner", character_id="whale", tags=["日常"])
    assert dup["tags"] == ["日常"]
    assert len(unified.list_memories(scope="owner", character_id="whale")) == 1

    pub = unified.add_memory("最讨厌别人叫我大肥鱼", scope="public", character_id="whale", tags=["人设"])
    assert pub["scope"] == "public"

    # Strict scope isolation: public NEVER sees owner memories
    public_lines = unified.prompt_lines("public", character_id="whale")
    assert public_lines == ["最讨厌别人叫我大肥鱼"]
    assert "喜欢在晚上喝热可可" not in public_lines

    owner_lines = unified.prompt_lines("owner", character_id="whale")
    assert owner_lines == ["喜欢在晚上喝热可可"]

    # Update active/content & delete
    updated = unified.update_memory(pub["id"], content="被人叫大肥鱼时会立刻反驳", active=False)
    assert updated["active"] is False
    assert unified.prompt_lines("public", character_id="whale") == []
    unified.update_memory(pub["id"], active=True)
    assert unified.prompt_lines("public", character_id="whale") == ["被人叫大肥鱼时会立刻反驳"]
    assert unified.delete_memory(pub["id"]) is True
    assert unified.get(pub["id"]) is None


def test_legacy_memory_migration_is_idempotent_and_preserves_legacy_tables(store):
    now = time.time()
    store.execute(
        "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,headline,source_ref,"
        "source_user,quote,anecdote,tags,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("whale", 1, "2026-11-01", "public_incident", "被错认", "operator-public:1", "op", "原话", "有人把我认成别人家AI", '["日常"]', "active", now, now),
    )
    store.execute(
        "INSERT INTO daily_highlights(character_id,character_version,highlight_date,category,headline,source_ref,"
        "source_user,quote,anecdote,tags,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("whale", 1, "2026-11-01", "owner_preference", "称呼偏好", "turn:req-1", "owner", "以后叫我名字", "号主希望直接叫名字", "[]", "active", now, now),
    )
    first_count = migrate_legacy_memories(store)
    assert first_count == 2
    unified = UnifiedMemoryStore(store)
    assert [m["content"] for m in unified.for_prompt("public", character_id="whale")] == ["有人把我认成别人家AI"]
    assert [m["content"] for m in unified.for_prompt("owner", character_id="whale")] == ["号主希望直接叫名字"]
    # Second run is a clean no-op and legacy rows remain intact
    assert migrate_legacy_memories(store, unified=unified) == 0
    assert len(store.all("SELECT * FROM daily_highlights")) == 2


def test_unified_memory_http_crud_endpoints(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/api/v1/system/auth/login", json={"token": settings.admin_token}).raise_for_status()
        created = client.post(
            "/api/v1/persona/memories",
            json={"scope": "public", "content": "最近看到离谱传闻会直接查证拆穿", "tags": ["吐槽"]},
        ).raise_for_status().json()
        assert created["scope"] == "public" and created["active"] is True
        listed = client.get("/api/v1/persona/memories", params={"scope": "public"}).raise_for_status().json()
        assert any(item["id"] == created["id"] for item in listed["items"])
        patched = client.patch(
            f"/api/v1/persona/memories/{created['id']}",
            json={"active": False, "content": "看到传闻先核实再吐糟"},
        ).raise_for_status().json()
        assert patched["active"] is False and patched["content"] == "看到传闻先核实再吐糟"
        deleted = client.delete(f"/api/v1/persona/memories/{created['id']}").raise_for_status().json()
        assert deleted["ok"] is True


def test_comment_thread_context_keeps_16_turns_and_speaker_roles():
    items = [
        {"comment_id": str(i), "thread_id": "1", "parent_comment_id": "1" if i > 1 else None,
         "platform_user_id": "200" if i == 1 else ("100" if i in (3, 4) else "300"),
         "display_name": f"用户{i}", "content": f"楼内第{i}条"}
        for i in range(1, 20)
    ]
    ctx = comment_thread_context(
        items[-1], items, {"root": "1", "parent": "19"},
        account_id="100", resource_author_uid="200", confirmed_ids={"3"},
    )
    assert len(ctx["timeline"]) == 16
    assert ctx["timeline"][0]["comment_id"] == "1"
    assert ctx["timeline"][0]["speaker_role"] == "UP主"
    role_by_id = {entry["comment_id"]: entry["speaker_role"] for entry in ctx["timeline"]}
    assert role_by_id["19"] == "评论者"
    ctx_early = comment_thread_context(
        items[3], items[:6], {"root": "1", "parent": "4"},
        account_id="100", resource_author_uid="200", confirmed_ids={"3"},
    )
    early_roles = {entry["comment_id"]: entry["speaker_role"] for entry in ctx_early["timeline"]}
    assert early_roles["1"] == "UP主"
    assert early_roles["3"] == "小鲸鱼"
    assert early_roles["4"] == "号主人工接管"
    assert early_roles["2"] == "评论者"
