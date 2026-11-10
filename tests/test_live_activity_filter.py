"""Polls and typed activities never enter the reply queue or raw room history."""

import asyncio
from datetime import UTC, datetime, timedelta

from tuber_studio.live.activity_filter import LivePoll
from tuber_studio.models import Event


def chat(text, uid="u1", **extra):
    return Event(text=text, platform_user_id=uid, display_name="同名观众", **extra)


async def test_poll_admission_room_context_and_result(runtime, store):
    await runtime.control("start")
    await runtime.control("pause")
    request = LivePoll(topic="吃什么", options=["白饭", "海带"], duration_seconds=60)
    started = runtime.live.start_poll(request)
    vote = chat("1")
    assert (await runtime.submit(vote))["reason"] == "已计票"
    assert (await runtime.submit(vote))["status"] == "duplicate"
    assert (await runtime.submit(chat("B")))["reason"] == "已计票"
    assert (await runtime.submit(chat("#A", "u2")))["reason"] == "已计票"
    assert "未计票" in (await runtime.submit(chat("2", None)))["reason"]
    stale = chat("2", "u3", timestamp=datetime.now(UTC) - timedelta(seconds=1))
    assert "不在活动时段" in (await runtime.submit(stale))["reason"]
    outside = chat("白饭", "u1", room_id="other")
    assert (await runtime.submit(outside))["status"] == "queued"
    one = chat("进入直播间后这个投票会显示结果吗")
    two = chat(one.text, "u2")
    for event in (one, two):
        assert (await runtime.submit(event))["status"] == "queued"
    result = runtime.live.finish_poll()
    assert result["id"] == started["id"] and result["counts"] == {"白饭": 1, "海带": 1}
    assert result["winners"] == ["白饭", "海带"] and result["winner"] is None
    assert runtime.live.finish_poll() is None
    assert (await runtime.submit(vote))["status"] == "duplicate"
    packet = runtime.live.room_context(two.model_dump())
    assert packet["弹幕"] == [
        {"event_id": one.event_id, "uid": "u1", "display_name": "同名观众", "text": one.text}
    ]
    assert [item["phase"] for item in packet["活动"]] == ["started", "ended"]
    assert len(store.all("SELECT * FROM events")) == 3
    assert len(runtime.live.queue) == 3
    await runtime.control("stop")
    await runtime.control("start")
    assert runtime.live.room_context(two.model_dump()) == {"弹幕": [], "活动": []}


async def test_poll_expires_once_while_queue_is_paused(runtime, store):
    await runtime.control("start")
    await runtime.control("pause")
    request = LivePoll(topic="接下来做什么", options=["画画", "唱歌"], duration_seconds=1)
    runtime.live.start_poll(request)
    async with asyncio.timeout(3):
        while runtime.live.activity_filter.active_poll:
            await asyncio.sleep(0.02)
    assert [item["phase"] for item in runtime.live.recent_activities] == ["started", "ended"]
    assert not store.all("SELECT * FROM jobs")
    assert runtime.live.recent_activities[-1]["total_votes"] == 0
    assert runtime.live.recent_activities[-1]["winner"] is None


async def test_typed_activity_admits_only_summary(runtime, store):
    await runtime.control("start")
    await runtime.control("pause")
    raw = Event(type="activity", text="观众已打卡")
    summary = Event(type="activity", metadata={"summary": {
        "id": "platform-poll", "phase": "ended", "topic": "今晚选歌", "result": "白饭之歌",
    }})
    assert (await runtime.submit(raw))["status"] == "filtered"
    assert (await runtime.submit(summary))["status"] == "recorded"
    assert (await runtime.submit(summary))["status"] == "duplicate"
    assert not store.all("SELECT * FROM events")
    assert runtime.live.room_context(chat("投票结束了吗").model_dump())["活动"][0]["result"] == "白饭之歌"
