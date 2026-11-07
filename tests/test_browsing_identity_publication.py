"""A renamed platform identity regenerates a queued reply, not its target."""

import json

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished

from tuber_studio.operation_identity import identity_snapshot


@pytest.mark.parametrize("profile_available", [True, False])
async def test_identity_changes_refresh_the_approved_public_intent_without_another_approval(browsing, monkeypatch, profile_available):
    state = {"platform": "bilibili", "account_uid": "100", "account_display_name": "旧名字", "account_bio": "旧简介"}

    def current(character=None, account_id=None):
        return identity_snapshot(state, character or browsing.runtime.character)

    async def refresh(character=None, account_id=None):
        if not profile_available:
            raise ValueError("主页暂不可读")
        state.update(account_display_name="新名字", account_bio="白饭仍然喜欢")
        return current(character, account_id)

    monkeypatch.setattr(browsing.runtime, "identity_snapshot", current)
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    source_key = intent["source_key"]

    async def profile():
        return {}

    monkeypatch.setattr(browsing.adapter, "account_profile", profile, raising=False)
    monkeypatch.setattr(browsing.runtime, "refresh_identity", refresh)
    original = browsing.runtime.llm.generate_json
    generated = []

    async def generate(messages, schema, *, max_tokens):
        generated.append(messages)
        value = await original(messages, schema, max_tokens=max_tokens)
        if schema.get("title") == "ResponseDecision":
            value["text"] = "本鲸换好新门牌，还想接着聊盐水里浮起来的鸡蛋。"
        return value

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generate)
    if not profile_available:
        with pytest.raises(ValueError, match="主页暂不可读"):
            await browsing.publish(intent["id"])
        assert not generated and not browsing.adapter.sent
        assert browsing.intents()[0]["status"] == "approved"
        return
    await browsing.publish(intent["id"])
    saved = browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (intent["id"],))
    assert saved["status"] == "published" and saved["source_key"] == source_key
    assert len(generated) == 1 and len(browsing.adapter.sent) == 1
    assert "新名字" in json.dumps(generated[0], ensure_ascii=False)
    assert json.loads(saved["target"])["generation_identity"]["identity_version"] == current()["identity_version"]
