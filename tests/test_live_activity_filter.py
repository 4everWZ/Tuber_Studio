"""Tests for LiveActivityFilter and poll/activity interception."""

import pytest
from tuber_studio.live.activity_filter import LiveActivityFilter
from tuber_studio.models import Event
from datetime import datetime, UTC


def make_chat(text, user_id="u1", name="观众1"):
    return Event(
        platform="live",
        room_id="r1",
        event_id=f"evt_{datetime.now().timestamp()}_{text}",
        type="chat",
        platform_user_id=user_id,
        display_name=name,
        text=text,
        timestamp=datetime.now(UTC),
    )


def test_activity_filter_platform_broadcast():
    filt = LiveActivityFilter()
    evt1 = make_chat("用户小明 进入直播间")
    filtered, reason = filt.check_event(evt1)
    assert filtered is True
    assert "广播" in reason

    evt2 = make_chat("点亮了粉丝勋章")
    filtered, reason = filt.check_event(evt2)
    assert filtered is True

    evt_normal = make_chat("大肥鱼今天好可爱呀")
    filtered, reason = filt.check_event(evt_normal)
    assert filtered is False
    assert reason is None


def test_activity_filter_interactive_poll():
    filt = LiveActivityFilter()
    assert not filt.is_poll_active()

    # Start poll
    poll_info = filt.start_poll("今晚吃什么", ["红烧大排", "清蒸海带"], duration_seconds=30)
    assert filt.is_poll_active()
    assert poll_info["topic"] == "今晚吃什么"

    # Vote with number '1'
    evt_v1 = make_chat("1", user_id="u1", name="小张")
    filtered, reason = filt.check_event(evt_v1)
    assert filtered is True
    assert "红烧大排" in reason

    # Vote with option letter 'B'
    evt_v2 = make_chat("B", user_id="u2", name="小李")
    filtered, reason = filt.check_event(evt_v2)
    assert filtered is True
    assert "清蒸海带" in reason

    # Vote with keyword '清蒸海带'
    evt_v3 = make_chat("清蒸海带", user_id="u3", name="小王")
    filtered, reason = filt.check_event(evt_v3)
    assert filtered is True
    assert "清蒸海带" in reason

    # Non-vote chat is NOT filtered
    evt_chat = make_chat("我都想吃怎么办呢", user_id="u4", name="吃货")
    filtered, reason = filt.check_event(evt_chat)
    assert filtered is False

    # End poll and verify synthesis
    result = filt.end_poll()
    assert not filt.is_poll_active()
    assert result["total_votes"] == 3
    assert result["counts"]["清蒸海带"] == 2
    assert result["counts"]["红烧大排"] == 1
    assert result["winner"] == "清蒸海带"
    assert "【直播间投票结果】" in result["summary_text"]
    assert "最终获胜选项是【清蒸海带】" in result["summary_text"]
