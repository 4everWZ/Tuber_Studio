"""The public/private classifier shares real metering and resumable generation."""

import asyncio
import json

import pytest
from test_direct_messages import messages as messages
from test_operation_generation import model_for

from tuber_studio.operation_generation import GenerationInvalid


def source_for(messages):
    messages.comments.import_items(
        "import-source-thread",
        [
            {
                "comment_id": "source-1",
                "platform_user_id": "123",
                "display_name": "鱼宝",
                "content": "可以私下接着聊这件事吗？",
            }
        ],
    )
    source = messages.store.one("SELECT * FROM comments")
    event = messages.comments._comment_event(source) | {"account_id": ""}
    return source, event


async def classify(messages, source, event):
    runtime = messages.runtime
    async with runtime.generation_lock:
        return await asyncio.wait_for(
            messages.route_comment(source, runtime.character, {}, runtime.context, "channel-test", event), 3
        )


async def test_invalid_classifier_requires_explicit_resume_and_reuses_completed_decision(messages, monkeypatch):
    requests, generated, _ = await model_for(
        messages.runtime,
        messages.settings,
        first={"channel": "direct_message", "reason": "public_topic"},
        corrected={"channel": "comment", "reason": "public_topic"},
    )
    messages.runtime.context.provider = messages.runtime.llm
    source, event = source_for(messages)
    with pytest.raises(GenerationInvalid) as captured:
        await classify(messages, source, event)
    coordinator = messages.runtime.continuations
    saved = coordinator.get(captured.value.continuation["id"])
    assert saved["state"] == "invalid" and saved["current_output"] is None
    assert len(generated) == 1
    count = len(requests)
    with pytest.raises(GenerationInvalid):
        await classify(messages, source, event)
    assert len(requests) == count
    coordinator.resume(saved["id"], binding=saved["binding"])
    assert await classify(messages, source, event) is None
    updated = coordinator.get(saved["id"])
    assert updated["state"] == "completed" and updated["episode"] != saved["episode"]
    assert len(generated) == 2
    assert all(request["chat_template_kwargs"]["enable_thinking"] is True for request in requests)
    assert all(request["max_tokens"] in {1, messages.policy()["channel_max_tokens"]} for request in requests)
    assert "回复渠道与理由分类不一致" in json.dumps(saved, ensure_ascii=False)

    async def no_prepare(*args, **kwargs):
        raise AssertionError("completed classification must precede context preparation")

    monkeypatch.setattr(messages.runtime.context, "prepare", no_prepare)
    count = len(requests)
    assert await classify(messages, source, event) is None and len(requests) == count
    assert not messages.store.all("SELECT * FROM dm_drafts") and not messages.adapter.sent


async def test_classifier_length_invalid_keeps_comment_source_and_explicit_resume_is_idempotent(
    messages, monkeypatch
):
    requests, generated, _ = await model_for(
        messages.runtime,
        messages.settings,
        first={"channel": "direct_message", "reason": "user_requested"},
        corrected={"channel": "direct_message", "reason": "user_requested"},
        first_finish="length",
        decision="wait",
    )
    messages.runtime.context.provider = messages.runtime.llm
    source, event = source_for(messages)
    with pytest.raises(GenerationInvalid) as captured:
        await classify(messages, source, event)
    saved = messages.runtime.continuations.get(captured.value.continuation["id"])
    assert saved["state"] == "invalid" and len(generated) == 1
    assert not messages.store.all("SELECT * FROM dm_drafts")
    assert any(
        step["phase"] == "source_view"
        and step["context"]["resume_target"]
        == {"channel": "comments", "local_id": "generation:" + str(source["id"])}
        for step in saved["revisions"]
    )

    # A new authorized episode may select a fresh valid route; the previous length result remains recorded.
    original = messages.enqueue
    monkeypatch.setattr(
        messages, "enqueue", lambda *args, **kwargs: original(*args, **kwargs, schedule=False)
    )
    resumed_requests, resumed_generated, _ = await model_for(
        messages.runtime,
        messages.settings,
        corrected={"channel": "direct_message", "reason": "user_requested"},
        decision="revise",
    )
    messages.runtime.context.provider = messages.runtime.llm
    with pytest.raises(GenerationInvalid):
        await classify(messages, source, event)
    assert not resumed_generated
    messages.runtime.continuations.resume(saved["id"], binding=saved["binding"])
    first = await classify(messages, source, event)
    assert first and await classify(messages, source, event) == first
    assert len(messages.store.all("SELECT * FROM dm_drafts")) == 1 and not messages.adapter.sent
    assert len(generated) == 1 and len(resumed_generated) == 1 and saved["original_output"] is not None
    updated = messages.runtime.continuations.get(saved["id"])
    assert updated["state"] == "completed" and updated["episode"] != saved["episode"]
    assert len(requests) == 3 and len(resumed_requests) == 3
    draft = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (first,))
    assert draft["source_id"] == source["id"] and draft["platform_user_id"] == "123"


async def test_invalid_classifier_resumes_via_original_comment_and_generates_public_review_draft(messages):
    requests, _, _ = await model_for(
        messages.runtime,
        messages.settings,
        first={"channel": "direct_message", "reason": "user_requested"},
        first_finish="length",
        decision="wait",
    )
    messages.runtime.context.provider = messages.runtime.llm
    source, _ = source_for(messages)
    with pytest.raises(GenerationInvalid):
        await asyncio.wait_for(
            messages.comments._process_comment(source, messages.runtime.character, False), 3
        )
    saved = messages.store.one("SELECT * FROM comments WHERE id=?", (source["id"],))
    assert saved["generation_continuation_id"]
    coordinator = messages.runtime.continuations
    before = coordinator.get(saved["generation_continuation_id"])
    assert before["state"] == "invalid"
    fresh_requests, fresh_generated, _ = await model_for(
        messages.runtime,
        messages.settings,
        first={"channel": "comment", "reason": "public_topic"},
        corrected={"spoken_text": "先在这层继续聊刚才那件事吧。", "reply_to": [messages.comments._comment_event(source)["event_id"]]},
    )
    messages.runtime.context.provider = messages.runtime.llm
    count = len(requests)
    queued = await messages.comments.resume_continuation(
        "generation:" + str(source["id"]), saved["generation_continuation_id"]
    )
    assert (
        queued["status"] == "pending" and queued["continuation"]["id"] == saved["generation_continuation_id"]
    )
    assert len(requests) == count and not messages.store.all("SELECT * FROM dm_drafts")
    messages.comments.worker = asyncio.create_task(messages.comments._worker())
    await asyncio.wait_for(messages.comments.queue.join(), 3)
    task = messages.store.one("SELECT * FROM comment_tasks WHERE id=?", (queued["task_id"],))
    assert task["status"] == "completed", task
    draft = messages.store.one("SELECT * FROM drafts WHERE comment_id=?", (source["id"],))
    assert draft["status"] == "pending_review" and draft["text"] == "先在这层继续聊刚才那件事吧。"
    assert draft["comment_id"] == source["id"] and draft["receipt"] is None
    after = coordinator.get(saved["generation_continuation_id"])
    assert after["state"] == "completed" and after["episode"] != before["episode"]
    assert len(fresh_generated) == 2 and fresh_requests
    assert not messages.store.all("SELECT * FROM dm_drafts") and not messages.adapter.sent


async def test_classifier_cannot_rebind_recipient_after_await(messages, monkeypatch):
    requests, _, _ = await model_for(
        messages.runtime,
        messages.settings,
        corrected={"channel": "direct_message", "reason": "user_requested"},
    )
    messages.runtime.context.provider = messages.runtime.llm
    source, event = source_for(messages)
    original = messages.runtime.llm.generate_json

    async def changed(packet, schema, *, max_tokens):
        result = await original(packet, schema, max_tokens=max_tokens)
        if "channel" in schema["properties"]:
            messages.store.execute("UPDATE comments SET platform_user_id='999' WHERE id=?", (source["id"],))
        return result

    monkeypatch.setattr(messages.runtime.llm, "generate_json", changed)
    with pytest.raises(ValueError, match="原评论"):
        await classify(messages, source, event)
    assert (
        len(requests) == 3 and not messages.store.all("SELECT * FROM dm_drafts") and not messages.adapter.sent
    )
