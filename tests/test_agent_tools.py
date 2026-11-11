import datetime

from tuber_studio.agent_tools import (
    DrawAndGuessContextTool,
    HistoricalRecallTool,
    RecentAnecdotesTool,
    ToolRegistry,
    build_default_tool_registry,
)
from tuber_studio.daily_memory import DailyMemoryService
from tuber_studio.live.draw_and_guess import DrawAndGuessService
from tuber_studio.live.game_livestream import GameLivestreamService


def test_tool_registry_registration():
    registry = ToolRegistry()
    assert len(registry.list_tools()) == 0

    tool = RecentAnecdotesTool(None)
    registry.register(tool)
    assert registry.get("recent_anecdotes") == tool
    assert len(registry.list_tools()) == 1


def test_draw_and_guess_context_exposes_category_without_answer():
    service = DrawAndGuessService()
    service.current_word = {"word": "西瓜", "category": "水果美食"}
    tool = DrawAndGuessContextTool(service)
    data = tool.execute({}, {})
    assert data == {"category": "水果美食"}
    prompt = tool.render_context(data)
    assert "水果美食" in prompt and "西瓜" not in prompt


def test_recent_anecdotes_tool(store):
    service = DailyMemoryService(store)
    today = datetime.date.today().isoformat()
    service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=today,
        category="comment_anecdote",
        headline="热白饭",
        quote="吃了吗",
        anecdote="多炫了一碗饭",
        status="active",
    )

    tool = RecentAnecdotesTool(service)
    ctx_info = {"character": {"id": "whale", "version": 1}, "mode": "live"}
    assert tool.can_handle({"text": "你好"}, ctx_info)
    assert not tool.can_handle({"text": "你好"}, {"mode": "other"})

    res = tool.execute({"text": "今天吃了吗"}, ctx_info)
    assert len(res) == 1
    rendered = tool.render_context(res)
    assert "近期生活与社群新鲜事" in rendered
    assert "多炫了一碗饭" in rendered


def test_historical_recall_tool(store):
    service = DailyMemoryService(store)
    today = datetime.date.today()
    ten_days_ago = (today - datetime.timedelta(days=10)).isoformat()

    service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=ten_days_ago,
        category="comment_anecdote",
        headline="尾巴太胖的抗议",
        quote="你的尾巴好胖",
        anecdote="被弹幕说尾巴胖，本鲸鱼当场气鼓鼓！",
        status="archived",
    )

    tool = HistoricalRecallTool(service)
    ctx_info = {"character": {"id": "whale", "version": 1}, "mode": "live"}

    # Normal text -> can_handle is False
    assert not tool.can_handle({"text": "今天天气真好"}, ctx_info)

    # Retrospective question -> can_handle is True
    retro_event = {"text": "你还记得上周有人说你尾巴太胖吗？"}
    assert tool.can_handle(retro_event, ctx_info)

    res = tool.execute(retro_event, ctx_info)
    assert res is not None
    assert res["headline"] == "尾巴太胖的抗议"

    rendered = tool.render_context(res)
    assert "被动唤醒的历史回忆线索" in rendered
    assert "尾巴太胖的抗议" in rendered


def test_collect_context_augmentations(store):
    daily_memory = DailyMemoryService(store)
    game_livestream = GameLivestreamService()
    draw_and_guess = DrawAndGuessService()

    today = datetime.date.today()
    ten_days_ago = (today - datetime.timedelta(days=10)).isoformat()

    # Active highlight (today)
    daily_memory.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=today.isoformat(),
        category="comment_anecdote",
        headline="新鲜事",
        quote="今天真棒",
        anecdote="今天很有精神！",
        status="active",
    )

    # Historical highlight (10 days ago)
    daily_memory.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=ten_days_ago,
        category="comment_anecdote",
        headline="远古回忆",
        quote="当年往事",
        anecdote="十天前下大雨游得慢",
        status="archived",
    )

    registry = build_default_tool_registry(
        daily_memory=daily_memory,
        game_livestream=game_livestream,
        draw_and_guess=draw_and_guess,
    )

    char = {"id": "whale", "version": 1}

    # 1. Normal question: recent anecdotes present, but NO historical recall
    blocks_normal = registry.collect_context_augmentations(char, {"text": "今天吃什么？"}, "live")
    assert any("近期生活与社群新鲜事" in b["content"] for b in blocks_normal)
    assert not any("被动唤醒的历史回忆线索" in b["content"] for b in blocks_normal)
    assert not any("远古回忆" in b["content"] for b in blocks_normal)

    # 2. Retrospective question: both recent anecdotes AND historical recall
    blocks_retro = registry.collect_context_augmentations(
        char, {"text": "你还记得十天前下大雨那件事吗？"}, "live"
    )
    assert any("近期生活与社群新鲜事" in b["content"] for b in blocks_retro)
    assert any("被动唤醒的历史回忆线索" in b["content"] for b in blocks_retro)
    assert any("远古回忆" in b["content"] for b in blocks_retro)
