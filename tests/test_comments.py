import asyncio

import pytest

from tuber_studio.bilibili import PublishUncertain
from tuber_studio.comments import Comments


class Platform:
    def __init__(self):
        self.sent = []
        self.failure = None

    async def close(self):
        pass

    async def popular(self):
        return [{"bvid": "BV1d8pU6xEZX"}]

    async def fetch(self, video, max_pages):
        return {"video_id": "BV1d8pU6xEZX", "items": [{"comment_id": "123", "content": "鲸鱼娘怎么画？"}]}

    async def publish(self, video, comment, text):
        self.sent.append((video, comment, text))
        if self.failure:
            raise self.failure
        return {"rpid": 321}


@pytest.fixture
async def comments(settings, store, runtime):
    service = Comments(settings, store, runtime, Platform())
    # Isolated worker only: the browsing loop must never make a real network call.
    service.worker = asyncio.create_task(service._worker())
    yield service
    await service.close()


async def draft(comments, store, automatic=False):
    comments.import_items("BV1d8pU6xEZX", [{"comment_id": "123", "content": "鱼鱼今天吃什么？"}])
    cid = store.one("SELECT id FROM comments")["id"]
    comments.enqueue([cid], automatic=automatic)
    await asyncio.wait_for(comments.queue.join(), 3)
    return store.one("SELECT * FROM drafts")


async def test_default_review_and_manual_approval_never_sends(comments, store):
    row = await draft(comments, store)
    assert not comments.policy()["auto_reply"]
    assert row["status"] == "pending_review"
    with pytest.raises(ValueError):
        comments.copy(row["id"])
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    comments.review(row["id"], "approve", "修改后的候选回复")
    assert comments.copy(row["id"])["text"] == "修改后的候选回复"
    assert comments.adapter.sent == []
    assert store.one("SELECT status FROM drafts")["status"] == "copied"


async def test_auto_reply_requires_credentials_and_explicit_enable(comments, store):
    with pytest.raises(ValueError, match="需要先配置"):
        await comments.set_policy({"auto_reply": True})
    assert not comments.policy()["auto_reply"]
    comments.credentials = {"sessdata": "test-session", "csrf": "test-csrf"}
    store.set("comment_policy", {"auto_reply": True})
    row = await draft(comments, store, automatic=True)
    assert row["status"] == "published"
    assert len(comments.adapter.sent) == 1
    with pytest.raises(ValueError):
        await comments.publish(row["id"], automatic=True)
    assert len(comments.adapter.sent) == 1
    assert "test-session" not in str(comments.policy())


@pytest.mark.parametrize("failure", [PublishUncertain("uncertain"), RuntimeError("unexpected")])
async def test_uncertain_send_cannot_be_retried(comments, store, failure):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    comments.adapter.failure = failure
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert len(comments.adapter.sent) == 1


async def test_definite_platform_rejection_preserves_reviewed_draft(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    comments.adapter.failure = ValueError("平台明确拒绝")
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert store.one("SELECT status FROM drafts")["status"] == "approved"
    assert len(comments.adapter.sent) == 1


async def test_stop_auto_reply_retains_later_drafts(comments, store):
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    await comments.set_policy({"auto_reply": False})
    row = await draft(comments, store, automatic=True)
    assert row["status"] == "pending_review"
    assert comments.adapter.sent == []


def test_import_1000_comments_is_idempotent_and_keeps_same_name_users_separate(comments, store):
    items = [
        {
            "comment_id": str(i),
            "content": f"第{i}个问题怎么处理？",
            "display_name": "同名",
            "platform_user_id": str(i),
        }
        for i in range(1000)
    ]
    assert comments.import_items("import-fixture", items)["imported"] == 1000
    assert comments.import_items("import-fixture", items)["duplicates"] == 1000
    assert comments.analyze("import-fixture")["question"] == 1000
    rows = comments.list_comments("import-fixture", limit=1000)
    assert len({r["platform_user_id"] for r in rows}) == 1000


async def test_manual_batches_stay_reviewable_when_auto_mode_is_enabled(comments, store):
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"auto_reply": True})
    row = await draft(comments, store, automatic=False)
    assert row["status"] == "pending_review"
    assert comments.adapter.sent == []


async def test_platform_success_followed_by_database_failure_never_allows_resend(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.execute("""CREATE TRIGGER refuse_receipt BEFORE UPDATE OF status ON drafts
        WHEN NEW.status='published' BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    with pytest.raises(ValueError, match="禁止重发"):
        await comments.publish(row["id"])
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"
    with pytest.raises(ValueError):
        await comments.publish(row["id"])
    assert len(comments.adapter.sent) == 1


async def test_cancellation_after_send_starts_is_uncertain(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    started = asyncio.Event()

    async def blocked_send(*args):
        started.set()
        await asyncio.sleep(60)

    comments.adapter.publish = blocked_send
    sending = asyncio.create_task(comments.publish(row["id"]))
    await started.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert store.one("SELECT status FROM drafts")["status"] == "publish_uncertain"


async def test_hourly_limit_is_enforced_before_second_http_write(comments, store):
    row = await draft(comments, store)
    comments.review(row["id"], "approve")
    comments.credentials = {"sessdata": "test", "csrf": "test"}
    store.set("comment_policy", {"max_replies_per_hour": 1})
    await comments.publish(row["id"])
    comments.import_items("BV1d8pU6xEZX", [{"comment_id": "456", "content": "下一条问题怎么处理？"}])
    second = store.one("SELECT id FROM comments WHERE comment_id='456'")["id"]
    comments.enqueue([second])
    await asyncio.wait_for(comments.queue.join(), 3)
    second_draft = store.one("SELECT * FROM drafts WHERE comment_id=?", (second,))
    comments.review(second_draft["id"], "approve")
    with pytest.raises(ValueError, match="频率上限"):
        await comments.publish(second_draft["id"])
    assert len(comments.adapter.sent) == 1
