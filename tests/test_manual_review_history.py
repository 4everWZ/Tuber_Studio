"""Manual review keeps retained OC drafts usable after a persona update."""

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished


async def test_reviewed_historical_browsing_draft_publishes_once_without_changing_provenance(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    original_version = intent["character_version"]
    newer = browsing.store.save_character(browsing.runtime.character)
    assert newer["version"] > original_version
    assert browsing.runtime.character["version"] == newer["version"]
    reviewed = browsing.review(intent["id"], "approve", "加盐后鸡蛋浮起来了，鱼鱼记住啦。")
    assert reviewed["character_version"] == original_version
    result = await browsing.publish(intent["id"])
    assert result["status"] == "published"
    assert result["character_version"] == original_version
    assert len(browsing.adapter.sent) == 1
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    assert len(browsing.adapter.sent) == 1
