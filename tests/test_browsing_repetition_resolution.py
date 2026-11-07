import json
import time

import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_batches import authorize

from tuber_studio.operation_feedback import FeedbackModel
from tuber_studio.operation_generation import GenerationDeferred
from tuber_studio.operations.browsing.decisions import BrowsingDecisions
from tuber_studio.publication_errors import SendLimitReached


def test_normal_short_acknowledgements_and_shared_openings_are_not_treated_as_mechanical_repetition():
    check = BrowsingDecisions.repetition_reason
    assert check("鲸鲸记住啦！", [{"text": "鲸鲸记住啦！"}], {}) is None
    opening = "看完这个视频我最想说的是"
    assert (
        check(
            opening + "这段剪辑节奏很舒服，每次切镜都刚刚好。",
            [{"text": opening + "盐水密度的对照很直观，加盐后鸡蛋才浮起来。"}],
            {},
        )
        is None
    )
    repeated = "白饭也看这一版，刚才看到那段画面卡住了。你发我一条我直接补全。"
    assert check(repeated, [{"text": repeated}], {})
    assert check(
        "我猜是DNS，你换个网络试，我帮你查DNS？", [{"text": "我猜是DNS。你换个网络试，我帮你查DNS。"}], {}
    )


@pytest.mark.parametrize("action", ["wait", "abandon"])
async def test_repeated_main_reply_can_be_deferred_by_the_model_without_parking_the_batch(
    browsing, monkeypatch, action
):
    await finished(browsing, preview=True)
    published = browsing.intents()[0]
    browsing.review(published["id"], "approve")
    await browsing.publish(published["id"])
    next_video = "BV1xx411c7mD"
    await browsing.configure({"sources": ["specified"], "watch_videos": [next_video]})

    async def decide(self, row):
        assert row["feedback"]["kind"] == "style_diagnostic"
        return {"action": action, "reason": "这句话刚发过，先不重复发，看看别人的新评论"}

    monkeypatch.setattr(FeedbackModel, "decide", decide)
    await authorize(browsing)
    completed = await finished(browsing, automatic=True, video_only=True)
    assert completed["status"] == "completed", completed
    assert completed["metrics"]["failures"] == 0
    assert len(browsing.adapter.sent) == 1
    task = next(
        item
        for item in browsing.tasks(round_id=completed["id"])
        if item["bvid"] == next_video and "generation_resolution" in item["payload"]
    )
    assert task["status"] == "viewed_without_comment" and not task["error"]
    resolution = task["payload"]["generation_resolution"]
    assert resolution["decision"]["action"] == action
    assert resolution["original_candidate"]["text"] == published["text"]
    assert resolution["feedback"]["kind"] == "style_diagnostic"
    assert browsing.repo.content_progress(completed, next_video)["main_decision"]["action"] == "skip"
    assert browsing.repo.batches.get(completed)["state"] == "completed"

    async def comments(video, max_pages=1):
        return {
            "items": [
                {
                    "comment_id": "601",
                    "thread_id": "601",
                    "platform_user_id": "300",
                    "display_name": "邻居",
                    "content": "鸡蛋浮起来需要多少盐？",
                }
            ]
        }

    async def source(target):
        return {"author_uid": "300", "text": "鸡蛋浮起来需要多少盐？"}

    original = browsing.runtime.llm.generate_json

    async def different_response(messages, schema, *, max_tokens):
        result = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision":
            result["text"] = "盐水的密度够大时鸡蛋就会浮上来，先慢慢加盐试试看。"
        return result

    monkeypatch.setattr(browsing.adapter, "fetch", comments)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", source, raising=False)
    monkeypatch.setattr(browsing.runtime.llm, "generate_json", different_response)
    await browsing.configure({"reply_enabled": True})
    revisit = await finished(browsing, automatic=True, video_only=True)
    assert revisit["status"] == "completed"
    assert revisit["id"] != completed["id"]
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["0", "601"]
    assert (
        len(
            [
                item
                for item in browsing.intents()
                if item["target"].get("bvid") == next_video and item["kind"] == "top_level"
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    ("state", "kind", "message"),
    [
        ("checkpoint", "style_diagnostic", "正文重复"),
        ("waiting", "model_error", "模型网络中断"),
        ("waiting", "style_diagnostic", "标点异常"),
    ],
)
def test_unrelated_checkpoints_are_not_finalized_as_repetition(state, kind, message):
    error = GenerationDeferred(
        {
            "id": "saved",
            "state": state,
            "feedback": {"kind": kind, "message": message},
            "revisions": [
                {
                    "phase": "decision",
                    "status": "completed",
                    "action": "wait",
                    "output": {"action": "wait", "reason": "等网络恢复"},
                }
            ],
        }
    )
    assert BrowsingDecisions._repetition_resolution(error) is None


@pytest.mark.parametrize("on_resume", [False, True])
async def test_real_send_limit_is_persisted_as_scheduling_instead_of_a_failed_retry(
    browsing, monkeypatch, on_resume
):
    next_at = time.time() + 3600

    def full(*args, **kwargs):
        raise SendLimitReached("达到渠道发送上限", next_at)

    if on_resume:

        async def resume(*args, **kwargs):
            full()

        monkeypatch.setattr(browsing, "_resume_batch_publications", resume)
    else:
        monkeypatch.setattr(browsing.operations, "reserve", full)
    await authorize(browsing)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed" and row["metrics"]["failures"] == 0
    task = browsing.tasks(round_id=row["id"])[0]
    assert task["status"] == "deferred_daily" and task["payload"]["retry_kind"] == "send_limit"
    assert task["payload"]["next_at"] == next_at
    assert not browsing.adapter.sent and not browsing.repo.video_retry(row, BV)
    persisted = browsing.store.one(
        "SELECT payload,status FROM browsing_batch_items WHERE batch_id=?", (row["config"]["batch_id"],)
    )
    assert persisted["status"] == "daily_wait" and json.loads(persisted["payload"])["next_at"] == next_at
