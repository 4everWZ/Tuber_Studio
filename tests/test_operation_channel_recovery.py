"""The public/private classifier shares real metering and resumable generation."""

import asyncio
import json

import pytest
from test_direct_messages import messages as messages
from test_operation_generation import model_for

from tuber_studio.operation_generation import GenerationDeferred


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


async def test_classifier_repairs_schema_and_reuses_decision_before_context_prepare(messages, monkeypatch):
    requests, generated, _ = await model_for(
        messages.runtime,
        messages.settings,
        first={"channel": "direct_message", "reason": "public_topic"},
        corrected={"channel": "comment", "reason": "public_topic"},
    )
    messages.runtime.context.provider = messages.runtime.llm
    source, event = source_for(messages)
    assert await classify(messages, source, event) is None
    assert len(generated) == 2 and len(requests) == 7  # context count, then three count/generation pairs
    assert all(request["chat_template_kwargs"]["enable_thinking"] is True for request in requests)
    assert all(request["max_tokens"] in {1, messages.policy()["channel_max_tokens"]} for request in requests)
    assert "回复渠道与理由分类不一致" in json.dumps(requests, ensure_ascii=False)

    async def no_prepare(*args, **kwargs):
        raise AssertionError("completed classification must precede context preparation")

    monkeypatch.setattr(messages.runtime.context, "prepare", no_prepare)
    assert await classify(messages, source, event) is None and len(requests) == 7
    assert not messages.store.all("SELECT * FROM dm_drafts") and not messages.adapter.sent


async def test_classifier_length_checkpoint_uses_comment_resume_source_and_no_duplicate_private_handoff(
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
    with pytest.raises(GenerationDeferred) as captured:
        await classify(messages, source, event)
    saved = messages.runtime.continuations.get(captured.value.continuation["id"])
    assert saved["state"] == "waiting" and len(generated) == 1
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
    first = await classify(messages, source, event)
    assert first and await classify(messages, source, event) == first
    assert len(messages.store.all("SELECT * FROM dm_drafts")) == 1 and not messages.adapter.sent
    assert len(generated) == 1 and len(resumed_generated) == 1 and saved["original_output"] is not None
    assert len(requests) == 5 and len(resumed_requests) == 5


async def test_failed_classifier_checkpoint_is_exposed_and_resumable_via_original_comment(messages):
    requests, _, _ = await model_for(
        messages.runtime,
        messages.settings,
        first={"channel": "direct_message", "reason": "user_requested"},
        first_finish="length",
        decision="wait",
    )
    messages.runtime.context.provider = messages.runtime.llm
    source, _ = source_for(messages)
    with pytest.raises(GenerationDeferred):
        await asyncio.wait_for(
            messages.comments._process_comment(source, messages.runtime.character, False), 3
        )
    saved = messages.store.one("SELECT * FROM comments WHERE id=?", (source["id"],))
    assert saved["generation_continuation_id"]
    count = len(requests)
    queued = await messages.comments.resume_continuation(
        "generation:" + str(source["id"]), saved["generation_continuation_id"]
    )
    assert (
        queued["status"] == "pending" and queued["continuation"]["id"] == saved["generation_continuation_id"]
    )
    assert len(requests) == count and not messages.store.all("SELECT * FROM dm_drafts")


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
