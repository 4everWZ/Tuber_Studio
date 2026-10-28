import json
import sqlite3
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.context import ContextManager
from tuber_studio.providers import DemoProvider
from tuber_studio.retention import RetentionManager


def memory(settings, store):
    return ContextManager(settings, store, DemoProvider())


def test_default_policy_and_disabled_policy_survive_reinitialization(settings, store):
    memory(settings, store)
    retention = RetentionManager(settings, store)
    assert retention.policy()["days"] == 30
    retention.set_policy(45)
    assert RetentionManager(settings, store).policy()["days"] == 45
    retention.set_policy(None)
    restored = RetentionManager(settings, store)
    assert restored.policy()["enabled"] is False and restored.policy()["next_run"] is None
    with pytest.raises(ValueError, match="截至时间"):
        restored.preview()
    assert restored.preview("2020-01-01T00:00:00Z")["cutoff"] == "2020-01-01T00:00:00Z"


def test_legacy_event_retention_does_not_override_new_dialogue_default(settings, tmp_path, monkeypatch):
    config = dict(settings.data)
    config.pop("conversation_retention_days")
    config["event_retention_days"] = 7
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("TUBER_CONFIG", str(path))
    restored = Settings(runtime=tmp_path)
    assert restored.data["conversation_retention_days"] == 30
    assert "event_retention_days" not in restored.data


@pytest.mark.parametrize("days", [True, False, 0, -1, 1.5, "30", 36501])
def test_invalid_retention_settings_do_not_change_policy(settings, store, days):
    memory(settings, store)
    retention = RetentionManager(settings, store)
    with pytest.raises(ValueError):
        retention.set_policy(days)
    assert retention.days == 30 and store.get("conversation_retention_days") is None


def test_expiry_removes_recent_summary_of_old_sources_without_deleting_dataset(settings, store):
    context = memory(settings, store)
    character = store.character()
    now = time.time()
    old = now - 31 * 86400
    scope = 'comments:["bilibili","v","floor"]'
    context.record(scope, character, {"event_id": "old", "text": "去了哪里？"}, "我去了珊瑚铺。")
    # A new compacted summary must not make expired source material immortal.
    store.execute(
        "UPDATE context_windows SET history=?,oldest_source_at=?,updated_at=?",
        (json.dumps([{"role": "user", "kind": "summary", "content": "珊瑚铺"}]), now, now),
    )
    store.execute("UPDATE grounded_memory SET created_at=?", (old,))
    store.execute(
        "INSERT INTO comments(video_id,comment_id,thread_id,content,imported_at) VALUES('v','1','floor','原文',?)",
        (old,),
    )
    store.execute(
        "INSERT INTO drafts(comment_id,text,status,created_at) VALUES(1,'待核对回执','publish_uncertain',?)",
        (old,),
    )
    retention = RetentionManager(settings, store)
    before = datetime.fromtimestamp(now - 30 * 86400, UTC).isoformat()
    preview = retention.preview(before)
    assert preview["counts"]["context_windows"] == 1 and preview["counts"]["grounded_memory"] == 2
    assert store.one("SELECT key FROM context_windows")  # Preview is read-only.
    result = retention.cleanup(before, now=now, manual=True)
    assert result["counts"] == preview["counts"]
    assert not store.one("SELECT key FROM context_windows")
    assert not context.memory.retrieve(scope, character, "上次去了哪里？")["evidence"]
    assert store.one("SELECT content FROM comments")["content"] == "原文"
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"


def test_oldest_input_age_expires_active_window_and_legacy_cache(settings, store):
    context = memory(settings, store)
    char = store.character()
    now = time.time()
    for scope in ("old", "legacy", "young"):
        context.record(scope, char, {"event_id": scope, "text": "你好"}, "你好呀")
    store.execute("UPDATE context_windows SET oldest_source_at=? WHERE mode='old'", (now - 31 * 86400,))
    store.execute("UPDATE context_windows SET oldest_source_at=NULL WHERE mode='legacy'")
    result = RetentionManager(settings, store).cleanup(now=now)
    assert result["counts"]["context_windows"] == 2
    assert [r["mode"] for r in store.all("SELECT mode FROM context_windows")] == ["young"]


def test_exact_cutoff_and_active_jobs_are_preserved(settings, store):
    memory(settings, store)
    cutoff = 1_700_000_000
    for event_id, status, timestamp in (
        (1, "completed", cutoff - 1),
        (2, "generating", cutoff - 1),
        (3, "completed", cutoff),
    ):
        store.execute(
            "INSERT INTO events(id,event_key,event_id,payload,status,created_at) VALUES(?,?,?,'{}',?,?)",
            (event_id, str(event_id), str(event_id), status, timestamp),
        )
        store.execute(
            "INSERT INTO jobs(id,event_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (str(event_id), event_id, status, timestamp, timestamp),
        )
    result = RetentionManager(settings, store).cleanup("2023-11-14T22:13:20Z")
    assert result["counts"]["events"] == result["counts"]["jobs"] == 1
    assert [row["id"] for row in store.all("SELECT id FROM events ORDER BY id")] == [2, 3]


def test_cleanup_rollback_preserves_sources_windows_and_last_run(settings, store):
    context = memory(settings, store)
    context.record("test", store.character(), {"event_id": "old", "text": "你好"}, "你好呀")
    store.execute("UPDATE grounded_memory SET created_at=1")
    store.execute(
        "CREATE TRIGGER fail_expiry BEFORE DELETE ON grounded_memory BEGIN SELECT RAISE(ABORT,'blocked'); END"
    )
    retention = RetentionManager(settings, store)
    with pytest.raises(sqlite3.IntegrityError):
        retention.cleanup()
    assert store.one("SELECT key FROM context_windows") and store.one("SELECT id FROM grounded_memory")
    assert retention.last_result is None and store.get("retention_last_result") is None


def test_old_session_summary_is_cleared_even_with_recent_conversations(settings, store):
    memory(settings, store)
    now = time.time()
    old = now - 31 * 86400
    store.execute("INSERT INTO sessions VALUES('session','whale',1,?,'expired summary')", (old,))
    store.execute(
        "INSERT INTO conversations(session_id,event_text,reply_text,created_at) VALUES('session','new','new',?)",
        (now,),
    )
    retention = RetentionManager(settings, store)
    result = retention.cleanup(now=now)
    assert result["counts"]["sessions"] == 0
    assert result["cleared_fields"]["session_summaries"] == 1
    assert store.one("SELECT summary FROM sessions")["summary"] == ""
    assert store.one("SELECT id FROM conversations")


def test_retention_api_is_authenticated_confirmed_and_keeps_disabled_manual_boundary(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/retention").status_code == 401
        assert client.put("/api/retention", json={"days": None}).status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        assert client.get("/api/retention").json()["days"] == 30
        assert client.put("/api/retention", json={"days": None}).json()["enabled"] is False
        assert client.post("/api/retention/preview", json={}).status_code == 400
        before = "2020-01-01T00:00:00Z"
        assert client.post("/api/retention/preview", json={"before": before}).status_code == 200
        assert client.post("/api/retention/cleanup", json={"before": before}).status_code == 400
        result = client.post("/api/retention/cleanup", json={"before": before, "confirm": True})
        assert result.status_code == 200 and result.json()["cutoff"] == before
        assert (
            client.post("/api/retention/preview", json={"before": "2099-01-01T00:00:00Z"}).status_code == 400
        )
        assert client.post("/api/retention/preview", json={"before": "2020-01-01"}).status_code == 400


async def test_disabled_automatic_cleanup_and_busy_manual_guard(runtime):
    runtime.retention.set_policy(None)
    assert await runtime.cleanup_retention(automatic=True) is None
    async with runtime.generation_lock:
        with pytest.raises(ValueError, match="正在生成"):
            await runtime.cleanup_retention("2020-01-01T00:00:00Z")
        assert await runtime.cleanup_retention(automatic=True) is None
