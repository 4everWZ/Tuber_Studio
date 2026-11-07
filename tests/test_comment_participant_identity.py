"""Manual imports retain the same actors as automatic discussion replies."""

import asyncio
import json
import time

import pytest
from test_comments import comments as comments

from tuber_studio.bilibili_account import PROFILE_CACHE_KEY, signature
from tuber_studio.models import Reply
from tuber_studio.operation_identity import identity_snapshot


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
    assert not comments.adapter.sent
    # Re-import enriches identity metadata without replacing a draft or source body.
    comments.import_items("import-actors", [rows[1] | {"content": "不覆盖原文", "mentions": [{"uid": "other-oc", "name": "新显示名"}]}])
    assert store.one("SELECT content FROM comments WHERE id=?", (source["id"],))["content"] == rows[1]["content"]
    assert len(store.all("SELECT * FROM drafts")) == 1


@pytest.mark.parametrize("profile_available", [True, False])
async def test_profile_change_regenerates_the_approved_comment_before_one_send(comments, store, monkeypatch, profile_available):
    comments.credentials = {"sessdata": "test-session", "csrf": "test-csrf"}
    state = {"platform": "bilibili", "account_uid": "injected-test-account", "account_display_name": "旧账号名", "account_bio": "旧简介"}

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
    comments.review(row["id"], "approve")
    calls = []

    async def generate(messages, character, event_id, source_text=None):
        calls.append(messages)
        return Reply(spoken_text=state["account_display_name"] + "，本鲸记住新门牌了。", emotion="cheerful", animation_id="talk_happy", reply_to=[event_id])

    monkeypatch.setattr(comments.runtime.llm, "generate", generate)
    if not profile_available:
        with pytest.raises(ValueError, match="主页暂不可读"):
            await comments.publish(row["id"])
        retained = store.one("SELECT * FROM drafts")
        assert retained["status"] == "approved" and retained["text"] == old_text
        assert not calls and not comments.adapter.sent
        return
    await comments.publish(row["id"])
    sent = store.one("SELECT * FROM drafts")
    assert sent["status"] == "published" and sent["text"].startswith("现在的账号名")
    assert len(calls) == 1 and len(comments.adapter.sent) == 1
    assert json.loads(sent["answer_check"])["generation_identity"]["identity_version"] == current()["identity_version"]
