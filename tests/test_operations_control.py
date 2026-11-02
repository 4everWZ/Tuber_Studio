"""Persistent authorization and actual SQLite write contention, no platform writes."""

import concurrent.futures
import time

import httpx
import pytest

from tuber_studio.bilibili_browsing import BrowsingAdapter
from tuber_studio.comments import Comments
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.operations_control import OperationsControl, canonical_intent
from tuber_studio.store import Store


def intent(character, source="42"):
    return canonical_intent("bilibili", "BV1d8pU6xEZX", source, character["id"])


def test_overlay_never_overwrites_latest_base_or_unrelated_business(store):
    control = OperationsControl(store)
    character = store.character()
    base = {"auto_browse": False, "auto_reply": False, "max_replies_per_hour": 2}
    assert control.effective("comments", base) == base
    control.configure({"enabled": True}, account_id="900", character=character)
    assert control.effective("comments", base)["auto_reply"]
    assert control.effective("comments", base)["max_replies_per_hour"] == 2
    assert not control.effective("dynamics", {"enabled": False})["enabled"]
    browsing = control.effective("browsing", {"autonomous_topics": False, "visual_enabled": False})
    assert (
        browsing["discovery_enabled"] and not browsing["autonomous_topics"] and not browsing["visual_enabled"]
    )
    base["auto_browse"] = True
    control.configure({"enabled": False})
    assert control.effective("comments", base) == base
    control.configure({"emergency": True})
    assert not control.effective("comments", base)["auto_browse"]
    again = OperationsControl(store)
    assert again.policy()["emergency"] and not again.policy()["enabled"]
    with pytest.raises(ValueError, match="紧急停止"):
        again.reserve("900", character, intent(character), "comments", automatic=True)


def test_stale_authorization_cannot_send_after_disable_enable(store):
    control, character = OperationsControl(store), store.character()
    first = control.configure({"enabled": True}, account_id="900", character=character)
    control.configure({"enabled": False})
    control.configure({"enabled": True}, account_id="900", character=character)
    with pytest.raises(ValueError, match="授权版本"):
        control.reserve("900", character, intent(character), "comments", automatic=True, epoch=first["epoch"])
    with pytest.raises(ValueError, match="账号或角色"):
        control.reserve("901", character, intent(character), "comments", automatic=True)


def test_quota_change_keeps_verified_binding_without_requiring_account_rebind(store):
    control, character = OperationsControl(store), store.character()
    old = control.configure({"enabled": True}, account_id="900", character=character)
    changed = control.configure({"max_per_hour": 2})
    assert changed["enabled"] and changed["account_id"] == "900"
    assert changed["enabled_at"] == old["enabled_at"] and changed["epoch"] > old["epoch"]


def test_quota_is_atomic_across_real_sqlite_connections(store, settings):
    control, character = OperationsControl(store), store.character()
    control.configure({"max_per_hour": 1})
    second_store = Store(settings.runtime / "test.sqlite3", [character])
    second = OperationsControl(second_store)

    def reserve(args):
        service, source, channel = args
        try:
            return service.reserve("900", character, intent(character, source), channel)
        except ValueError:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            result = list(pool.map(reserve, [(control, "1", "comments"), (second, "2", "messages")]))
        assert len([token for token in result if token]) == 1
        assert store.one("SELECT COUNT(*) AS n FROM account_writes")["n"] == 1
    finally:
        second_store.close()


@pytest.mark.parametrize("outcome", ["rejected", "uncertain", "published", "cancelled"])
def test_same_source_cannot_be_resent_through_private_channel_or_new_oc_version(store, outcome):
    control, character = OperationsControl(store), store.character()
    token = control.reserve("900", character, intent(character), "comments")
    control.finish(token, outcome)
    current = store.save_character(character)
    with pytest.raises(ValueError, match="禁止重复"):
        control.reserve("900", current, intent(current), "messages")
    with pytest.raises(ValueError):
        control.finish(token, "published")


def test_failure_keeps_quota_and_uncertain_pauses_only_affected_account_channel(store):
    control, character = OperationsControl(store), store.character()
    token = control.reserve("900", character, intent(character), "comments")
    control.finish(token, "uncertain")
    with pytest.raises(ValueError, match="不确定写入"):
        control.reserve("900", character, intent(character, "43"), "comments", automatic=True)
    other = control.reserve("900", character, intent(character, "44"), "messages", automatic=True)
    control.finish(other, "rejected")
    control.configure({"max_per_hour": 2})
    with pytest.raises(ValueError, match="全渠道"):
        control.reserve("900", character, intent(character, "45"), "messages")
    control.reconcile_channel("900", "comments")
    with pytest.raises(ValueError, match="禁止重复"):
        control.reserve("900", character, intent(character), "comments")


def test_restart_turns_sending_into_uncertain_without_restoring_submission(store):
    control, character = OperationsControl(store), store.character()
    token = control.reserve("900", character, intent(character), "comments")
    reopened = OperationsControl(store)
    assert store.one("SELECT status FROM account_writes WHERE token=?", (token,))["status"] == "uncertain"
    assert reopened.status()["channel_blocks"][0]["channel"] == "comments"
    with pytest.raises(ValueError):
        reopened.reserve("900", character, intent(character), "comments")


def test_cross_channel_recipient_cooldown_and_account_isolation(store):
    control, character = OperationsControl(store), store.character()
    token = control.reserve("900", character, intent(character), "comments", recipient="123")
    control.finish(token, "published")
    with pytest.raises(ValueError, match="跨渠道"):
        control.reserve("900", character, intent(character, "43"), "messages", recipient="123")
    assert control.reserve("901", character, intent(character, "43"), "messages", recipient="123")


def test_control_store_error_is_not_treated_as_no_record(store):
    control, character = OperationsControl(store), store.character()
    store.db.execute("DROP TABLE account_writes")
    with pytest.raises(Exception):
        control.reserve("900", character, intent(character), "comments")


def test_emergency_is_a_single_action_while_automation_running(store):
    control, character = OperationsControl(store), store.character()
    control.configure({"enabled": True}, account_id="900", character=character)
    stopped = control.configure({"emergency": True})
    assert stopped["emergency"] and not stopped["enabled"]
    with pytest.raises(ValueError):
        control.configure({"enabled": True}, account_id="900", character=character)


class PublicPlatform:
    def __init__(self):
        self.sent = []

    async def self_uid(self):
        return "900"

    async def publish(self, video, comment, text):
        self.sent.append((video, comment, text))
        return {"rpid": "321"}

    async def close(self):
        pass


class PrivatePlatform:
    def __init__(self):
        self.sent = []

    async def send(self, uid, text, *, authorized):
        if not authorized():
            raise ValueError("停止")
        self.sent.append((uid, text))
        return {"msg_key": "77"}

    async def close(self):
        pass


def approved_public(comments, store, *, status="approved", source="42"):
    comments.import_items(
        "BV1d8pU6xEZX", [{"comment_id": source, "platform_user_id": "123", "content": "你好鱼鱼"}]
    )
    row = store.one("SELECT * FROM comments WHERE comment_id=?", (source,))
    character = store.character()
    return store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (row["id"], "鱼宝你好呀。", status, character["id"], character["version"], time.time(), time.time()),
    ).lastrowid


async def test_legacy_public_and_private_send_paths_share_the_final_quota(settings, store, runtime):
    comments = Comments(settings, store, runtime, PublicPlatform())
    comments.credentials = {"sessdata": "fixture", "csrf": "fixture"}
    messages = DirectMessages(settings, store, runtime, comments, PrivatePlatform())
    await messages.configure({"enabled": True})
    comments.operations.configure({"max_per_hour": 1})
    first = approved_public(comments, store)
    await comments.publish(first)
    source, _ = messages.ingest(
        {
            "message_id": "888",
            "platform_user_id": "456",
            "display_name": "其他用户",
            "text": "你好",
            "timestamp": time.time(),
            "account_id": "900",
        }
    )
    second = messages.enqueue("message", source)
    store.execute("UPDATE dm_drafts SET status='approved',text='你好呀。' WHERE id=?", (second,))
    try:
        with pytest.raises(ValueError, match="全渠道"):
            await messages.send(second)
        assert len(comments.adapter.sent) == 1 and messages.adapter.sent == []
        assert store.one("SELECT COUNT(*) AS n FROM account_writes")["n"] == 1
    finally:
        await messages.close()
        await comments.close()


async def test_legacy_final_http_check_blocks_emergency_during_platform_preparation(settings, store, runtime):
    requests = []
    adapter = BrowsingAdapter({"sessdata": "fixture", "csrf": "fixture"})
    comments = Comments(settings, store, runtime, adapter)
    control = runtime.operations

    def handler(request):
        requests.append(request.method)
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": {"mid": 900}})
        control.configure({"emergency": True})
        return httpx.Response(200, json={"code": 0, "data": {"bvid": "BV1d8pU6xEZX", "aid": 1}})

    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handler)
    )
    comments.credentials = {"sessdata": "fixture", "csrf": "fixture"}
    store.set("comment_policy", {"auto_reply": True})
    identity = approved_public(comments, store, status="pending_review")
    try:
        with pytest.raises(ValueError):
            await comments.publish(identity, automatic=True)
        assert "POST" not in requests
        assert not store.all("SELECT * FROM account_writes")
    finally:
        await comments.close()
