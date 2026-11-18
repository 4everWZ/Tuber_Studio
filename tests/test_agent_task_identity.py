import sqlite3
from uuid import uuid4

import httpx
import pytest

from tuber_studio.agent.task_identity import TaskIdentity
from tuber_studio.agent.task_runtime import TaskRuntime
from tuber_studio.store import Store, dump


def studio_tasks(store):
    def binding():
        character = store.character()
        account = store.get("browsing_discovery_account", {}).get("account_id", "")
        return [account, character["id"], character["version"], character.get("_edit_identity")]

    return TaskRuntime(store, TaskIdentity(store, lambda: store.get("active_character"), binding), None)


async def test_migrate_backup_preserves_receipts_without_transferring_action_grants(store, tmp_path):
    old = TaskRuntime(store, lambda: ["first-account", "whale", 1, None], None)
    known, unknown, corrupt = (str(uuid4()) for _ in range(3))
    for identity in (known, unknown, corrupt):
        old.create(identity, "稍后核对同一回执")
    store.execute("UPDATE agent_tasks SET scope=? WHERE id=?", (dump(["second", "missing", 1, None]), unknown))
    store.execute("UPDATE agent_tasks SET scope='malformed' WHERE id=?", (corrupt,))
    store.execute("UPDATE agent_tasks SET steps=?,status='waiting' WHERE id=?",
                  (dump([{"id": "original-step", "checkpoint": {"job": "original-job"}}]), known))
    before = store.all("SELECT * FROM agent_tasks ORDER BY id")
    path = tmp_path / "backup.sqlite3"
    copy = sqlite3.connect(path)
    try:
        store.db.backup(copy)
    finally:
        copy.close()
    for attempt in range(2):
        duplicate = Store(path, [])
        try:
            tasks = studio_tasks(duplicate)
            assert [item["id"] for item in tasks.activity()] == [known]
            assert tasks.current(known)["steps"][0]["checkpoint"]["job"] == "original-job"
            assert tasks.executable(known) is None
            with pytest.raises(ValueError, match="授权已失效"):
                await tasks.control(known, 1, "steer", "继续")
            assert tasks.current(unknown) is None and tasks.current(corrupt) is None
            assert duplicate.all("SELECT * FROM agent_tasks ORDER BY id") == before
            if attempt == 0:
                duplicate.save_character(duplicate.character() | {"id": "missing"})
            duplicate.activate("missing")
            assert not tasks.activity()
            duplicate.activate("whale")
        finally:
            duplicate.close()
    assert not store.one("SELECT name FROM sqlite_master WHERE name='agent_task_authority'")


@pytest.mark.parametrize("switch", ["account", "account_round_trip", "role_edit", "role_round_trip"])
async def test_task_history_is_not_an_execution_grant(store, switch):
    store.set("browsing_discovery_account", {"account_id": "first"})
    store.save_character(store.character() | {"id": "other"})
    tasks = studio_tasks(store)
    identity, step = str(uuid4()), str(uuid4())
    task = tasks.create(identity, "待批准的操作")
    pending = [{"id": step, "tool": "write", "revision": 1, "approval": {"target": "first"}}]
    tasks._save(task, steps=pending, status="awaiting_approval")
    store.set("bilibili_account_last_profile", {"profile": {"uid": "first"}, "checked_at": 1})
    store.set("bilibili_account_last_profile", {"profile": {"uid": "first"}, "checked_at": 2})
    assert tasks.executable(identity)["steps"] == pending
    if switch.startswith("account"):
        store.set("browsing_discovery_account", {"account_id": "second"})
        if switch == "account_round_trip":
            store.set("browsing_discovery_account", {"account_id": "first"})
    elif switch == "role_edit":
        store.replace_character(store.character() | {"name": "改版"})
    else:
        store.activate("other")
        assert tasks.current(identity) is None and not tasks.activity()
        with pytest.raises(LookupError):
            await tasks.control(identity, 1, "approve", step_id=step)
        store.activate("whale")
    assert not tasks.can_control(tasks.current(identity))
    assert tasks.current(identity)["steps"] == pending
    assert [item["id"] for item in tasks.activity()] == [identity]
    assert tasks.executable(identity) is None
    await tasks._advance(tasks.current(identity))
    with pytest.raises(ValueError, match="授权已失效"):
        await tasks.control(identity, 1, "approve", step_id=step)
    with pytest.raises(ValueError):
        tasks.create(identity, "待批准的操作")
    assert tasks.current(identity)["status"] == "awaiting_approval"
    new_task = tasks.create(str(uuid4()), "重新交办")
    assert tasks.executable(new_task["id"]) == new_task
    assert studio_tasks(store).executable(new_task["id"]) == new_task


async def test_authenticated_owner_reads_after_key_rotation_without_platform_lookup(settings, monkeypatch):
    from tuber_studio.app import create_app

    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "test-key-before")
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.1", 9000))
    endpoint = "/api/agent/tasks"
    identity = str(uuid4())
    async with httpx.AsyncClient(transport=transport, base_url="http://studio.test") as client:
        assert (await client.get(endpoint)).status_code == 401
        client.headers["authorization"] = "Bearer test-key-before"
        response = await client.post(endpoint, json={"request_id": identity, "goal": "在私聊内核对资料"})
        assert response.status_code == 200, response.text
        app.state.store.set("bilibili_account_last_profile", None)
        assert app.state.runtime.tasks.executable(identity)
        monkeypatch.setenv("TUBER_ADMIN_TOKEN", "test-key-after")
        assert (await client.get(f"{endpoint}/{identity}")).status_code == 401
        client.headers["authorization"] = "Bearer test-key-after"

        def no_platform():
            pytest.fail("历史读取不应请求平台绑定")

        monkeypatch.setattr(app.state.browsing.discovery, "history_scope", no_platform)
        assert (await client.get(f"{endpoint}/{identity}")).json()["id"] == identity
        assert (await client.get(endpoint)).json()["tasks"][0]["id"] == identity
    app.state.store.close()
