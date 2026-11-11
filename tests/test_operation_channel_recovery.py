"""Confirmed invitations skip channel generation; actual body failures remain resumable."""

import asyncio

import pytest
from test_direct_messages import messages as messages
from test_message_outreach import ACTOR, invite
from test_operation_generation import model_for


async def prepare_invalid_body(messages):
    messages.operations.configure({"enabled": True}, account_id=ACTOR, character=messages.runtime.character)
    await messages.configure({"enabled": True, "auto_send": False})
    await messages.resume_pending()
    identity = invite(messages, proposal=False)
    draft = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    event = messages._event(draft, messages.runtime.character)
    requests, generated, _ = await model_for(messages.runtime, messages.settings,
        first={"spoken_text": "这项配置在设置页。", "reply_to": [event["event_id"]]},
        first_finish="length")
    messages.runtime.context.provider = messages.runtime.llm
    messages._schedule(identity, False)
    await asyncio.wait_for(messages.queue.join(), 3)
    saved = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert saved["status"] == "failed" and not saved["text"] and saved["receipt"] is None
    assert saved["generation_continuation_id"]
    ledger = messages.runtime.continuations.get(saved["generation_continuation_id"])
    assert ledger["state"] == "invalid" and ledger["current_output"] is None
    assert len(generated) == 1 and not messages.adapter.sent
    assert not messages.store.all("SELECT * FROM operation_continuations WHERE channel='generation.comment_channel'")
    return identity, event, ledger, requests


async def test_invalid_body_explicit_resume_uses_same_source_and_leaves_a_private_review_draft(messages):
    identity, event, original, old_requests = await prepare_invalid_body(messages)
    previous_count = len(old_requests)
    await messages._generate(identity, False)
    assert len(old_requests) == previous_count
    requests, generated, _ = await model_for(messages.runtime, messages.settings,
        corrected={"spoken_text": "这项配置在设置页，可以接着聊你问的选项。", "reply_to": [event["event_id"]]})
    messages.runtime.context.provider = messages.runtime.llm
    queued = await messages.resume_continuation("generation:" + str(identity), original["id"])
    assert queued["status"] == "queued"
    await asyncio.wait_for(messages.queue.join(), 3)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "pending_review", row
    assert row["platform_user_id"] == "123" and row["account_id"] == ACTOR
    assert row["text"] == "这项配置在设置页，可以接着聊你问的选项。" and row["receipt"] is None
    updated = messages.runtime.continuations.get(original["id"])
    assert updated["state"] == "completed" and updated["episode"] != original["episode"]
    assert len(generated) == 1 and requests and not messages.adapter.sent
    assert len(messages.store.all("SELECT * FROM dm_drafts")) == 1
    source = messages.store.one("SELECT * FROM comments WHERE id=?", (row["source_id"],))
    scope = messages.comments.account_context_scope(source["video_id"], source["thread_id"], account_id=ACTOR)
    window = messages.runtime.context.existing(scope, messages.runtime.character)
    assert not window or row["text"] not in window["history"]


async def test_invalid_body_resume_rejects_changed_original_comment(messages):
    identity, _, ledger, requests = await prepare_invalid_body(messages)
    before = len(requests)
    messages.store.execute("UPDATE comments SET content='原评论内容已更新' WHERE id=(SELECT source_id FROM dm_drafts WHERE id=?)", (identity,))
    with pytest.raises(ValueError, match="原始私信或角色已变化"):
        await messages.resume_continuation("generation:" + str(identity), ledger["id"])
    assert len(requests) == before and not messages.adapter.sent
    assert messages.runtime.continuations.get(ledger["id"])["state"] == "invalid"
