import datetime

import pytest

from tuber_studio.daily_memory import DailyMemoryService


def test_is_retrospective_query():
    service = DailyMemoryService(None)

    # Retrospective triggers
    assert service.is_retrospective_query("你还记得上周有人说你尾巴太胖吗？")
    assert service.is_retrospective_query("上次直播玩星露谷好玩吗？")
    assert service.is_retrospective_query("之前评论区那个热心观众说了什么？")
    assert service.is_retrospective_query("那天直播你怎么突然下播了？")
    assert service.is_retrospective_query("大肥鱼记不记得以前的事情？")
    assert service.is_retrospective_query("前几天你在动态里发了什么？")
    assert service.is_retrospective_query("当时你怎么回答的？")
    assert service.is_retrospective_query("还记得我吗？")

    # Non-retrospective everyday chatter
    assert not service.is_retrospective_query("今天天气真好呀！")
    assert not service.is_retrospective_query("大肥鱼好可爱，摸摸头")
    assert not service.is_retrospective_query("点歌 晴天")
    assert not service.is_retrospective_query("晚上吃什么好吃的？")
    assert not service.is_retrospective_query("")
    assert not service.is_retrospective_query(None)


def test_extract_recall_keywords():
    service = DailyMemoryService(None)
    keywords = service._extract_recall_keywords("你还记得上周有人说你尾巴太胖吗？")
    assert "尾巴" in keywords or "尾巴太胖" in keywords
    assert "太胖" in keywords or "尾巴太胖" in keywords
    # Scaffolding words should not dominate
    assert "你" not in keywords
    assert "吗" not in keywords
    assert "还记得" not in keywords


def test_recall_historical_highlight(store):
    service = DailyMemoryService(store)
    today = datetime.date.today()
    ten_days_ago = (today - datetime.timedelta(days=10)).isoformat()

    # Seed an archived highlight from 10 days ago (outside 3-day active window)
    item_id = service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=ten_days_ago,
        category="comment_anecdote",
        headline="尾巴太胖的抗议",
        quote="大肥鱼你的尾巴好胖呀",
        anecdote="被弹幕说尾巴太胖游得慢，本鲸鱼当场气鼓鼓并多炫了一碗饭！",
        source_ref="video:BVold:comment:99",
        source_user="某弹幕小哥",
        tags=["尾巴", "游速", "身材"],
        status="archived",
    )

    # 1. Non-retrospective query -> 0 tokens / returns None immediately
    res_normal = service.recall_historical_highlight("whale", 1, "今天天气真好呀，大肥鱼！")
    assert res_normal is None

    # 2. Irrelevant retrospective query -> returns None
    res_irrelevant = service.recall_historical_highlight("whale", 1, "你还记得上周去火星挖矿的事吗？")
    assert res_irrelevant is None

    # 3. Target retrospective query asking about tail -> successful match!
    res_hit = service.recall_historical_highlight("whale", 1, "你还记得上周有人说你尾巴太胖吗？")
    assert res_hit is not None
    assert res_hit["id"] == item_id
    assert res_hit["date"] == ten_days_ago
    assert res_hit["headline"] == "尾巴太胖的抗议"
    assert "气鼓鼓" in res_hit["memory"]
    assert res_hit["formatted_clue"].startswith(f"[{ten_days_ago} 尾巴太胖的抗议:")
    assert len(res_hit["formatted_clue"]) < 80

    # 4. Exclusion works (if already in active memories, don't duplicate)
    res_excluded = service.recall_historical_highlight(
        "whale", 1, "你还记得上周有人说你尾巴太胖吗？", exclude_ids={item_id}
    )
    assert res_excluded is None


def test_get_active_anecdotes_excludes_older_than_three_days(store):
    service = DailyMemoryService(store)
    today = datetime.date.today()
    ten_days_ago = (today - datetime.timedelta(days=10)).isoformat()

    service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=ten_days_ago,
        category="comment_anecdote",
        headline="10天前的远古梗",
        quote="远古引用",
        anecdote="这是十天前的事情了",
        status="active",
    )

    # get_active_anecdotes default days=3 must NOT include 10 days ago
    active = service.get_active_anecdotes("whale", 1, days=3)
    assert len(active) == 0

    # But recall_historical_highlight CAN recall it when retrospectively queried
    recalled = service.recall_historical_highlight("whale", 1, "你还记得之前那个远古梗吗？")
    assert recalled is not None
    assert recalled["headline"] == "10天前的远古梗"


class DummyProvider:
    async def count_tokens(self, messages):
        return 50

    async def summarize(self, messages, *, max_tokens=1024):
        return ""


@pytest.mark.asyncio
async def test_context_manager_passive_fallback_e2e(settings, store):
    from tuber_studio.context import ContextManager

    service = DailyMemoryService(store)
    today = datetime.date.today()
    ten_days_ago = (today - datetime.timedelta(days=10)).isoformat()

    # Record archived highlight from 10 days ago
    service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=ten_days_ago,
        category="comment_anecdote",
        headline="尾巴太胖的抗议",
        quote="大肥鱼你的尾巴好胖呀",
        anecdote="被弹幕说尾巴太胖游得慢，本鲸鱼当场气鼓鼓并多吃了一碗热白饭！",
        source_ref="video:BVold:comment:99",
        source_user="某弹幕小哥",
        tags=["尾巴", "身材", "胖"],
        status="archived",
    )

    provider = DummyProvider()
    ctx = ContextManager(settings, store, provider, daily_memory=service)
    char = store.character()

    # 1. Normal daily banter: 0 token cost, NO historical recall injected
    evt_normal = {
        "id": "e1",
        "event_id": "e1",
        "type": "chat",
        "user": "u1",
        "name": "小明",
        "text": "今天吃什么好吃的？",
    }
    msgs_normal = await ctx.prepare("live:test_session_1", char, evt_normal, mode="live")
    assert not any("被动唤醒的历史回忆线索" in m.get("content", "") for m in msgs_normal)
    assert not any("尾巴太胖" in m.get("content", "") for m in msgs_normal)

    # 2. Retrospective question on 10th day: passive recall triggers and injects targeted clue
    evt_retro = {
        "id": "e2",
        "event_id": "e2",
        "type": "chat",
        "user": "u1",
        "name": "老观众",
        "text": "你还记得上周有人说你尾巴太胖吗？",
    }
    msgs_retro = await ctx.prepare("live:test_session_1", char, evt_retro, mode="live")
    clue_msg = next((m for m in msgs_retro if "被动唤醒的历史回忆线索" in m.get("content", "")), None)
    assert clue_msg is not None
    assert "尾巴太胖的抗议" in clue_msg["content"]
    assert "气鼓鼓" in clue_msg["content"]
    # Check length discipline (<100 characters total for clue message)
    assert len(clue_msg["content"]) < 120
