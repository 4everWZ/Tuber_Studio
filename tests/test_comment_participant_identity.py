"""Manual imports retain the same actors as automatic discussion replies."""

import asyncio
import json
import time

import pytest
from test_comments import comments as comments

from tuber_studio.bilibili_account import PROFILE_CACHE_KEY, signature
from tuber_studio.models import Reply
from tuber_studio.operation_identity import identity_snapshot
from tuber_studio.runtime import Runtime


async def test_comment_account_switch_does_not_inherit_other_account_or_unbound_assistant_history(comments, store, monkeypatch):
    runtime = comments.runtime
    character = runtime.character
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    active = {"uid": "100"}

    async def account_id():
        return active["uid"]

    monkeypatch.setattr(comments, "account_id", account_id)
    monkeypatch.setattr(comments, "display_account_id", lambda: active["uid"])
    video, floor = "import-account-context", "same-floor"
    scoped_a = comments.account_context_scope(video, floor, account_id="100")
    legacy = "comments:" + json.dumps(["bilibili", video, floor], separators=(",", ":"))
    runtime.context.record(scoped_a, character, {"event_id": "old-a", "text": "A账号历史", "account_id": "100"},
                           "仅A账号窗口记过的旧回复")
    runtime.context.record(legacy, character, {"event_id": "unbound", "text": "未绑定账号的原文"},
                           "旧无账号窗口的回复")
    comments.import_items(video, [{"comment_id": "current", "thread_id": floor,
                                   "platform_user_id": "neighbor", "content": "新账号来接这句话。"}])
    active["uid"] = "200"
    seen = []
    original = runtime.llm.generate

    async def capture(messages, character, event_id, source_text=None):
        seen.append(json.dumps(messages, ensure_ascii=False))
        return await original(messages, character, event_id, source_text)

    monkeypatch.setattr(runtime.llm, "generate", capture)
    comments.enqueue([store.one("SELECT id FROM comments")["id"]])
    await asyncio.wait_for(comments.queue.join(), 3)
    assert len(seen) == 1
    assert "仅A账号窗口记过的旧回复" not in seen[0] and "旧无账号窗口的回复" not in seen[0]
    scoped_b = comments.account_context_scope(video, floor)
    assert scoped_a != scoped_b != legacy
    history_b = json.loads(runtime.context.existing(scoped_b, character)["history"])
    assert history_b and all(item["role"] == "user" for item in history_b)
    assert "仅A账号窗口记过的旧回复" in runtime.context.existing(scoped_a, character)["history"]
    assert "旧无账号窗口的回复" in runtime.context.existing(legacy, character)["history"]


async def test_manual_comment_keeps_real_mentions_and_speaking_account_through_generation(comments, store, monkeypatch):
    credentials = {"sessdata": "test-session", "csrf": "test-csrf"}
    comments.credentials = credentials
    store.set(PROFILE_CACHE_KEY, {"format": 1, "credential_signature": signature(credentials),
              "profile": {"uid": "injected-test-account", "name": "大肥鱼只想过平静生活", "bio": "喜欢白饭。"},
              "checked_at": time.time()})
    rows = [
        {"comment_id": "root", "thread_id": "root", "platform_user_id": "other-oc", "display_name": "鲸鲸",
         "content": "我们接着聊昨天的话题。"},
        {"comment_id": "source", "thread_id": "root", "parent_comment_id": "root", "platform_user_id": "viewer",
         "content": "@鲸鲸 我能追求你吗？", "mentions": [{"uid": "other-oc", "name": "鲸鲸"}]},
    ]
    comments.import_items("import-actors", rows)
    source = store.one("SELECT * FROM comments WHERE comment_id='source'")
    assert json.loads(source["mentions"]) == rows[1]["mentions"]
    original = comments.runtime.llm.generate
    captured = []

    async def capture(messages, character, event_id, source_text=None):
        context = next(json.loads(message["content"].split("：", 1)[1]) for message in messages
                       if message["content"].startswith("上下文数据："))
        captured.append(context)
        return await original(messages, character, event_id, source_text)

    monkeypatch.setattr(comments.runtime.llm, "generate", capture)
    comments.enqueue([source["id"]])
    await asyncio.wait_for(comments.queue.join(), 3)
    assert store.one("SELECT status FROM drafts")["status"] == "pending_review"
    actor = captured[0]["self_actor"]
    assert actor["account_uid"] == "injected-test-account" and actor["account_display_name"] == "大肥鱼只想过平静生活"
    assert captured[0]["reply_target"]["author_uid"] == "viewer"
    addressed = captured[0]["reply_target"]["addressed_to"]
    assert addressed == [{"uid": "other-oc", "name": "鲸鲸", "relation_to_self": "other", "basis": ["mention", "reply_parent"]}]
    wire_context = captured[0]
    assert "comment_context" not in wire_context
    thread = wire_context["thread"]
    assert thread["root"]["text_ref"] == thread["parent"]["text_ref"] == "thread.comments[0].text"
    assert thread["comments"][0]["text"] == rows[0]["content"]
    assert thread["source"]["text_ref"] == "当前观众消息.text"
    # Wire naming changes no event/draft source fields or original body.
    original_event = json.loads(store.one("SELECT input_summary FROM drafts")["input_summary"])
    assert original_event["comment_context"]["source"]["text"] == rows[1]["content"]
    assert "thread" not in original_event
    assert not comments.adapter.sent
    # Re-import enriches identity metadata without replacing a draft or source body.
    comments.import_items("import-actors", [rows[1] | {"content": "不覆盖原文", "mentions": [{"uid": "other-oc", "name": "新显示名"}]}])
    assert store.one("SELECT content FROM comments WHERE id=?", (source["id"],))["content"] == rows[1]["content"]
    assert len(store.all("SELECT * FROM drafts")) == 1


@pytest.mark.parametrize("account_changed", [False, True], ids=["same-uid", "different-uid"])
async def test_manual_publish_keeps_reviewed_text_and_verifies_current_uid(comments, store, monkeypatch, account_changed):
    runtime = comments.runtime
    comments.credentials = {"sessdata": "test-session", "csrf": "test-csrf"}
    state = {"account_uid": "100", "account_display_name": "旧账号名", "account_bio": "旧简介"}
    verified_uids = []

    async def self_uid():
        uid = state["account_uid"]
        verified_uids.append(uid)
        return uid

    def current(character=None, account_id=None):
        return identity_snapshot(state, character or runtime.character)

    monkeypatch.setattr(comments.adapter, "self_uid", self_uid, raising=False)
    monkeypatch.setattr(runtime, "identity_snapshot", current)
    monkeypatch.setattr(runtime, "generate_reply", Runtime.generate_reply.__get__(runtime, Runtime))
    video, comment = "BV1d8pU6xEZX", "321"
    comments.import_items(video, [{"comment_id": comment, "platform_user_id": "neighbor", "content": "你现在叫什么名字？"}])
    source = store.one("SELECT id FROM comments")
    comments.enqueue([source["id"]])
    await asyncio.wait_for(comments.queue.join(), 3)
    row = store.one("SELECT * FROM drafts")
    generation_identity = json.loads(row["answer_check"])["generation_identity"]
    assert generation_identity["identity_version"] == current()["identity_version"]
    original = " 这句话是我审核过的。\n保留首尾空格和换行。 "
    comments.review(row["id"], "approve", original)
    state.update(account_uid="200" if account_changed else "100", account_display_name="新账号名", account_bio="新简介")
    assert current()["identity_version"] != generation_identity["identity_version"]
    verified_uids.clear()

    async def unexpected(*args, **kwargs):
        pytest.fail("Manual publication must not read the profile or call the model")

    monkeypatch.setattr(comments.adapter, "account_profile", unexpected, raising=False)
    monkeypatch.setattr(runtime, "refresh_identity", unexpected)
    monkeypatch.setattr(runtime, "generate_reply", unexpected)
    monkeypatch.setattr(runtime.llm, "generate", unexpected)
    if account_changed:
        with pytest.raises(ValueError, match="该草稿属于其他账号"):
            await comments.publish(row["id"])
        assert not comments.adapter.sent and not store.all("SELECT * FROM account_writes")
    else:
        await comments.publish(row["id"])
        assert comments.adapter.sent == [(video, comment, original)]
        assert store.all("SELECT account_id,status FROM account_writes") == [{"account_id": "100", "status": "published"}]
    assert verified_uids == [state["account_uid"]]
    saved = store.one("SELECT * FROM drafts")
    assert saved["status"] == ("approved" if account_changed else "published")
    assert saved["text"] == original and saved["account_id"] == "100"
    assert json.loads(saved["answer_check"])["generation_identity"] == generation_identity


@pytest.mark.parametrize(("profile_available", "diagnostic"), [
    pytest.param(True, False, id="send"),
    pytest.param(False, False, id="profile-unavailable"),
    pytest.param(True, True, id="needs-review"),
])
async def test_automatic_publish_refreshes_comment_profile_and_honors_new_diagnostics(
    comments, store, monkeypatch, profile_available, diagnostic
):
    monkeypatch.setattr(comments.runtime, "generate_reply", Runtime.generate_reply.__get__(comments.runtime, Runtime))
    comments.credentials = {"sessdata": "test-session", "csrf": "test-csrf"}
    state = {"platform": "bilibili", "account_uid": "injected-test-account", "account_display_name": "旧账号名", "account_bio": "旧简介"}
    comments.operations.configure({"enabled": True}, account_id=state["account_uid"], character=comments.runtime.character)
    store.set("browsing_policy", {"reply_enabled": True, "reply_auto_publish": True})

    def current(character=None, account_id=None):
        return identity_snapshot(state, character or comments.runtime.character)

    async def refresh(character=None, account_id=None):
        if not profile_available:
            raise ValueError("主页暂不可读")
        state.update(account_display_name="现在的账号名", account_bio="现在的简介")
        return current(character, account_id)

    async def profile():
        return {}

    monkeypatch.setattr(comments.adapter, "account_profile", profile, raising=False)
    monkeypatch.setattr(comments.runtime, "identity_snapshot", current)
    monkeypatch.setattr(comments.runtime, "refresh_identity", refresh)
    comments.import_items("BV1d8pU6xEZX", [{"comment_id": "321", "platform_user_id": "neighbor", "content": "你现在叫什么名字？"}])
    source = store.one("SELECT id FROM comments")
    comments.enqueue([source["id"]])
    await asyncio.wait_for(comments.queue.join(), 3)
    row = store.one("SELECT * FROM drafts")
    old_text = row["text"]
    calls = []

    async def generate(messages, character, event_id, source_text=None):
        calls.append(messages)
        text = state["account_display_name"] + "，" + "本鲸记住新门牌了。" * (8 if diagnostic else 1)
        return Reply(spoken_text=text, emotion="cheerful", animation_id="talk_happy", reply_to=[event_id])

    monkeypatch.setattr(comments.runtime.llm, "generate", generate)
    if not profile_available:
        with pytest.raises(ValueError, match="主页暂不可读"):
            await comments.publish(row["id"], automatic=True)
        retained = store.one("SELECT * FROM drafts")
        assert retained["status"] == "pending_review" and retained["text"] == old_text
        assert not calls and not comments.adapter.sent
        return
    if diagnostic:
        with pytest.raises(ValueError, match="正文诊断尚待审核"):
            await comments.publish(row["id"], automatic=True)
        retained = store.one("SELECT * FROM drafts")
        assert retained["status"] == "pending_review"
        check = json.loads(retained["answer_check"])
        assert check["output"]["needs_review"] and check["continuation"]["state"] == "awaiting_review"
        assert len(calls) == 1 and not comments.adapter.sent
        comments.review(row["id"], "approve")
    await comments.publish(row["id"], automatic=not diagnostic)
    sent = store.one("SELECT * FROM drafts")
    assert sent["status"] == "published" and sent["text"].startswith("现在的账号名")
    assert len(calls) == 1
    assert comments.adapter.sent == [("BV1d8pU6xEZX", "321", sent["text"])]
    assert json.loads(sent["answer_check"])["generation_identity"]["identity_version"] == current()["identity_version"]
