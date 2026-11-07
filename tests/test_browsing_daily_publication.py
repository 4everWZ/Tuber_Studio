import pytest
from test_browsing import browsing as browsing
from test_browsing import finished

from tuber_studio.operations.browsing.models import ResponseDecision


async def test_daily_publication_limit_counts_main_and_thread_successes_and_keeps_extra_drafts(
    browsing, monkeypatch
):
    await browsing.configure({"max_replies_per_video": 2, "recipient_cooldown_seconds": 0})
    await finished(browsing, preview=True)
    main = browsing.intents()[0]
    row = browsing.repo.round(main["round_id"])
    replies = []
    for parent, author in (("501", "300"), ("502", "301")):
        replies.append(
            browsing.publication._intent(
                row,
                "reply_public",
                {
                    "type": 1,
                    "oid": "42",
                    "bvid": main["target"]["bvid"],
                    "root": parent,
                    "parent": parent,
                    "recipient": author,
                    "source_text": "盐水怎么把鸡蛋托起来的？",
                },
                ResponseDecision(
                    action="reply_public",
                    text="盐水密度变大，鸡蛋就浮起来啦。",
                    reason="specific_detail",
                    evidence_ids=main["evidence"],
                ),
                main["material_keys"],
                main["evidence"],
            )
        )
    assert all(replies)
    assert (
        browsing.runtime.browsing_quotas.reply_status("100", row["character_id"], main["target"]["bvid"], 2)[
            "published"
        ]
        == 0
    )

    async def read_reply(target):
        return {"author_uid": target["recipient"], "text": target["source_text"]}

    monkeypatch.setattr(browsing.adapter, "read_video_reply", read_reply, raising=False)
    for intent in (main, replies[0]):
        browsing.review(intent["id"], "approve")
        assert (await browsing.publish(intent["id"]))["status"] == "published"
    browsing.review(replies[1]["id"], "approve")
    with pytest.raises(ValueError, match="今日回复上限"):
        await browsing.publish(replies[1]["id"])
    assert len(browsing.adapter.sent) == 2
    extra = browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (replies[1]["id"],))
    assert extra["status"] == "approved"
