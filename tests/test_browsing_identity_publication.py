"""Manual publication preserves approved browsing text under identity changes."""

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished

from tuber_studio.operation_identity import identity_snapshot
from tuber_studio.publication_errors import PlatformRejected


@pytest.mark.parametrize("rejected", [False, True])
async def test_manual_browsing_publish_preserves_approved_text_without_the_model(browsing, monkeypatch, rejected):
    state = {"platform": "bilibili", "account_uid": "100", "account_display_name": "旧名字", "account_bio": "旧简介"}

    def current(character=None, account_id=None):
        return identity_snapshot(state, character or browsing.runtime.character)

    monkeypatch.setattr(browsing.runtime, "identity_snapshot", current)
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    original = " 本鲸也想试试盐水浮鸡蛋。\n这次记得先把水称好。 "
    browsing.review(intent["id"], "approve", original)
    state.update(account_display_name="新名字", account_bio="白饭仍然喜欢")
    browsing.store.execute("UPDATE browsing_materials SET expires_at=0")

    async def unexpected(*args, **kwargs):
        pytest.fail("Manual publication must not refresh generation identity, retake evidence or call the model")

    monkeypatch.setattr(browsing.adapter, "account_profile", unexpected, raising=False)
    monkeypatch.setattr(browsing.runtime, "refresh_identity", unexpected)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", unexpected)
    monkeypatch.setattr(browsing, "_prepare_evidence", unexpected)
    if rejected:
        browsing.adapter.failure = PlatformRejected(12051, "请修改正文")
        with pytest.raises(PlatformRejected, match="请修改正文"):
            await browsing.publish(intent["id"])
        continuation = browsing.runtime.continuations.find("browsing", intent["id"])
        assert continuation["state"] == "checkpoint"
        assert continuation["trigger_feedback"]["code"] == 12051
        assert continuation["original_output"]["text"] == original
    else:
        await browsing.publish(intent["id"])
    saved = browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (intent["id"],))
    assert saved["status"] == ("approved" if rejected else "published")
    assert saved["text"] == original and saved["source_key"] == intent["source_key"]
    assert [text for _, text in browsing.adapter.sent] == [original]
