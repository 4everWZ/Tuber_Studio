import time
from unittest.mock import MagicMock

from tuber_studio.live.draw_and_guess import (
    WORD_BANK,
    DrawAndGuessService,
    normalize_guess,
)


def test_word_bank_structure():
    assert len(WORD_BANK) >= 5
    for item in WORD_BANK:
        assert "word" in item
        assert "category" in item
        assert "aliases" in item
        assert "hint" in item
        assert len(item["strokes"]) >= 3


def test_normalize_guess():
    assert normalize_guess("猫咪") == "猫咪"
    assert normalize_guess("我猜是 猫咪！") == "猫咪"
    assert normalize_guess("答案是：西瓜。") == "西瓜"
    assert normalize_guess("难道是大肥鱼？") == "大肥鱼"
    assert normalize_guess("CAT") == "cat"


def test_start_round():
    service = DrawAndGuessService()
    assert service.is_active() is False

    res = service.start_round(trigger_user="小明", gift_name="画笔")
    assert res["status"] == "started"
    assert service.is_active() is True
    assert "小明" in res["spoken_text"]
    assert "画笔" in res["spoken_text"]
    assert service.current_word is not None


def test_check_guess_and_win():
    daily_memory_mock = MagicMock()
    service = DrawAndGuessService(daily_memory=daily_memory_mock)
    service.start_round(trigger_user="小明")
    service.current_word = {
        "id": "test_cat",
        "word": "猫咪",
        "category": "动物",
        "aliases": ["猫", "小猫", "cat"],
        "hint": "尖尖耳朵",
        "strokes": [{"type": "path", "d": "M0 0"}],
    }

    # Wrong guess
    wrong_res = service.check_guess("u1", "小李", "这是一只小狗")
    assert wrong_res is None
    assert service.is_active() is True

    # Correct guess with alias
    win_res = service.check_guess("u2", "小红", "我猜是小猫！")
    assert win_res is not None
    assert win_res["status"] == "won"
    assert win_res["winner"] == "小红"
    assert win_res["word"] == "猫咪"
    assert "恭喜【小红】猜对啦" in win_res["spoken_text"]
    assert service.is_active() is False

    # Verify daily memory recorded
    assert daily_memory_mock.record_highlight.called


def test_tick_timeout():
    service = DrawAndGuessService()
    service.start_round(trigger_user="小李", duration_sec=1)
    service.current_word = {
        "id": "test_fish",
        "word": "大肥鱼",
        "category": "主播",
        "aliases": ["肥鱼"],
        "hint": "可爱的鱼",
        "strokes": [],
    }

    # Simulate time passing
    service.started_at = time.time() - 2
    timeout_res = service.tick()
    assert timeout_res is not None
    assert timeout_res["status"] == "timeout"
    assert "时间到啦" in timeout_res["spoken_text"]
    assert service.state == "finished"


def test_get_overlay_state():
    service = DrawAndGuessService()
    service.start_round(trigger_user="小王", duration_sec=60)
    service.current_word = {
        "id": "test_watermelon",
        "word": "西瓜",
        "category": "水果",
        "aliases": [],
        "hint": "红瓤黑籽",
        "strokes": [
            {"type": "circle", "cx": 10, "cy": 10, "r": 5},
            {"type": "path", "d": "M0 0 L10 10"},
        ],
    }

    st = service.get_overlay_state()
    assert st["active"] is True
    assert st["category"] == "水果"
    assert st["total_strokes"] == 2
    assert len(st["revealed_strokes"]) >= 1
    assert st["trigger_user"] == "小王"
