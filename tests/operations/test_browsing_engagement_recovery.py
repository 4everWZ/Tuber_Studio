import pytest

from tests.support.operations_helpers import browsing as browsing
from tests.support.operations_helpers import finished
from tuber_studio.core.errors import UnknownPublication
from tuber_studio.infra.platforms.bilibili.client import PublishUncertain


@pytest.mark.parametrize("kind", ["video_like", "video_fav"])
async def test_uncertain_engagement_stays_unconfirmed_without_a_readback_api(browsing, monkeypatch, kind):
    await browsing.configure({"video_like_enabled": True, "video_fav_enabled": True})
    await finished(browsing, preview=True)
    candidate = next(item for item in browsing.intents() if item["kind"] == kind)
    browsing.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    browsing.review(candidate["id"], "approve")
    sent = []

    async def uncertain(target, *, before_send, **kwargs):
        before_send()
        sent.append(target)
        raise PublishUncertain("connection lost after submission")

    method = "like_video" if kind == "video_like" else "favorite_video"
    monkeypatch.setattr(browsing.adapter, method, uncertain)
    with pytest.raises(UnknownPublication):
        await browsing.publish(candidate["id"])
    assert await browsing.publication._reconcile(candidate["id"]) is None
    saved = browsing.runtime.continuations.find("browsing", candidate["id"])
    assert saved["feedback"]["last_check"]["status"] == "unsupported"
    assert saved["state"] == "needs_reconciliation"
    assert (
        browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (candidate["id"],))["status"]
        == "publish_uncertain"
    )
    with pytest.raises(UnknownPublication):
        await browsing.publish(candidate["id"])
    assert len(sent) == 1
