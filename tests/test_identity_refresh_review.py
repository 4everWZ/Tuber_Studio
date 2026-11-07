"""A replacement identity reply keeps old approval only when its diagnostics pass."""

import json

import pytest
from test_direct_messages import finished as finished_message
from test_direct_messages import incoming
from test_direct_messages import messages as messages
from test_dynamics import public_memory
from test_dynamics import service as service

from tuber_studio.models import Reply
from tuber_studio.operation_identity import identity_snapshot


@pytest.mark.parametrize("diagnostic", [False, True])
async def test_dm_identity_replacement_requires_new_review_for_diagnostic_text(messages, monkeypatch, diagnostic):
    draft_id = messages.enqueue("message", incoming(messages))
    draft = await finished_message(messages, draft_id)
    state = json.loads(draft["answer_check"])["generation_identity"]
    old_version = state["identity_version"]
    messages.review(draft_id, "approve")

    def current(character=None, account_id=None):
        return identity_snapshot(state, character or messages.runtime.character)

    async def refresh(character=None, account_id=None):
        state["account_display_name"] = "新名字"
        return current(character, account_id)

    calls = []

    async def generate(packet, character, event_id, source_text=None):
        calls.append(packet)
        text = "我是新名字，" + "本鲸记住新门牌了。" * (8 if diagnostic else 1)
        return Reply(spoken_text=text, emotion="cheerful", animation_id="talk_happy", reply_to=[event_id])

    monkeypatch.setattr(messages.runtime, "identity_snapshot", current)
    monkeypatch.setattr(messages.runtime, "refresh_identity", refresh)
    monkeypatch.setattr(messages.runtime.llm, "generate", generate)
    if diagnostic:
        with pytest.raises(ValueError):
            await messages.send(draft_id)
        held = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft_id,))
        check = json.loads(held["answer_check"])
        assert held["status"] == "pending_review" and check["output"]["needs_review"]
        assert check["continuation"]["state"] == "awaiting_review"
        assert len(calls) == 1 and not messages.adapter.sent
        messages.review(draft_id, "approve")
    assert (await messages.send(draft_id))["status"] == "published"
    saved = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft_id,))
    check = json.loads(saved["answer_check"])
    assert check["generation_identity"]["identity_version"] == current()["identity_version"] != old_version
    assert len(calls) == 1 and len(messages.adapter.sent) == 1


@pytest.mark.parametrize("diagnostic", [False, True])
async def test_dynamic_identity_replacement_requires_new_review_for_diagnostic_text(service, diagnostic):
    public_memory(service)
    character = service._character()
    state = {"account_uid": "injected-test-account", "account_display_name": "旧名字"}
    service.runtime.identity_snapshot = lambda character=None, account_id=None: identity_snapshot(
        state, character or service._character(),
    )
    post_id = (await service.evaluate(manual=True))["post_id"]
    old_version = json.loads(service._post(post_id)["generation_identity"])["identity_version"]
    service.review(post_id, "approve")

    async def refresh(character=None, account_id=None):
        state["account_display_name"] = "新名字"
        return service.runtime.identity_snapshot(character, account_id)

    service.runtime.refresh_identity = refresh
    text = "我是新名字，" + "海底厨房今天照常开饭。" * (8 if diagnostic else 1)
    service.runtime.llm.output = json.dumps({
        "action": "post", "reason": "按当前身份分享", "content": text,
        "source_ids": service._post(post_id)["source_ids"], "next_evaluation_seconds": 1800,
    }, ensure_ascii=False)
    if diagnostic:
        with pytest.raises(ValueError):
            await service.publish(post_id)
        held = service._post(post_id)
        assert held["status"] == "pending_review" and held["content"] == text
        decision = service.store.one("SELECT generation_continuation_id FROM dynamic_decisions WHERE id=?", (held["decision_id"],))
        audit = service.runtime.continuations.get(decision["generation_continuation_id"])
        assert audit["state"] == "awaiting_review" and audit["current_output"]["content"] == text
        assert not service.sent
        service.review(post_id, "approve")
    assert (await service.publish(post_id))["status"] == "published"
    assert len(service.sent) == 1 and service.sent[0][1] == text
    version = json.loads(service._post(post_id)["generation_identity"])["identity_version"]
    assert version == service.runtime.identity_snapshot(character)["identity_version"] != old_version
    generation_calls = [packet for packet in service.runtime.llm.calls
                        if json.loads(packet[1]["content"]).get("task") != "operations_memory_summary"]
    assert len(generation_calls) == 2  # Initial draft and one identity replacement.
