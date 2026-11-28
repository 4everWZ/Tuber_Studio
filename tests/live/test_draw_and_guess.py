import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from tuber_studio.live.games.draw_and_guess import (
    WORD_BANK,
    DrawAndGuessService,
    normalize_guess,
    sanitize_strokes,
)


def test_word_bank_structure_and_no_legacy_name():
    assert 10 <= len(WORD_BANK) <= 12
    aqua_items = [item for item in WORD_BANK if item["word"] == "Aqua"]
    assert len(aqua_items) == 1
    aqua = aqua_items[0]
    assert "蓝发小鲸鱼" in aqua["category"] or "蓝发小鲸鱼" in aqua["aliases"]
    assert "小鲸鱼" in aqua["aliases"]
    assert "大肥鱼" not in json.dumps(aqua, ensure_ascii=False)

    for item in WORD_BANK:
        assert "word" in item
        assert "category" in item
        assert "aliases" in item
        assert "hint" in item
        assert isinstance(item["progressive_hints"], list)
        assert len(item["progressive_hints"]) == 3
        assert all(isinstance(h, str) and h.strip() for h in item["progressive_hints"])
        assert len(item["strokes"]) >= 3
        cleaned = sanitize_strokes(item["strokes"])
        assert len(cleaned) == len(item["strokes"])


def test_normalize_guess():
    assert normalize_guess("猫咪") == "猫咪"
    assert normalize_guess("我猜是 猫咪！") == "猫咪"
    assert normalize_guess("答案是：西瓜。") == "西瓜"
    assert normalize_guess("难道是小鲸鱼？") == "小鲸鱼"
    assert normalize_guess("CAT") == "cat"


def test_sanitize_strokes_supports_six_primitives_and_clamps():
    raw = [
        {"type": "path", "d": "M -20 50 L 500 400 Z", "fill": "NONE", "stroke": "#38BDF8", "width": 50},
        {"type": "circle", "cx": 450, "cy": -10, "r": 250, "fill": "#0f172a"},
        {"type": "ellipse", "cx": 200, "cy": 150, "rx": 300, "ry": 200, "fill": "#fda4af"},
        {"type": "rect", "x": -10, "y": 350, "width": 500, "height": 400, "rx": 8, "fill": "#1e293b", "stroke": "#38bdf8"},
        {"type": "line", "x1": -5, "y1": 0, "x2": 999, "y2": 999, "stroke": "#ffffff", "width": 0.1},
        {"type": "polygon", "points": "-10,20 450,350 200,150", "fill": "#facc15", "stroke": "#ca8a04", "width": 4},
    ]
    cleaned = sanitize_strokes(raw)
    assert len(cleaned) == 6
    assert [s["type"] for s in cleaned] == ["path", "circle", "ellipse", "rect", "line", "polygon"]

    # Clamped checks
    assert cleaned[0]["d"] == "M 0 50 L 400 300 Z"
    assert cleaned[0]["fill"] == "none"
    assert cleaned[0]["width"] == 24
    assert (cleaned[1]["cx"], cleaned[1]["cy"], cleaned[1]["r"]) == (400, 0, 200)
    assert (cleaned[2]["rx"], cleaned[2]["ry"]) == (200, 150)
    assert (cleaned[3]["x"], cleaned[3]["y"], cleaned[3]["width"], cleaned[3]["height"]) == (0, 300, 400, 300)
    assert (cleaned[4]["x1"], cleaned[4]["x2"], cleaned[4]["y2"], cleaned[4]["width"]) == (0, 400, 300, 0.5)
    assert cleaned[5]["points"] == "0,20 400,300 200,150"


def test_sanitize_strokes_rejects_xss_urls_and_non_hex_colors():
    raw = [
        {"type": "script", "d": "alert(1)"},
        {"type": "path", "d": "<script>alert(1)</script>", "stroke": "#fff"},
        {"type": "path", "d": "M 10 10 L 50 50", "onclick": "alert(1)", "onload": "x()"},
        {"type": "circle", "cx": 100, "cy": 100, "r": 20, "fill": "javascript:alert(1)", "stroke": "url(https://evil.test/x)"},
        {"type": "rect", "x": 10, "y": 10, "width": 40, "height": 40, "fill": "red", "stroke": "rgb(1,2,3)"},
        {"type": "polygon", "points": "10,10 20,20 javascript:alert(1)"},
        {"type": "line", "x1": 10, "y1": 10, "x2": 30, "y2": 30, "onmouseover": "evil()", "stroke": "https://evil.test"},
    ]
    cleaned = sanitize_strokes(raw)
    serialized = json.dumps(cleaned)
    assert "<script" not in serialized.lower()
    assert "javascript:" not in serialized.lower()
    assert "https://" not in serialized.lower()
    assert "onclick" not in serialized.lower()
    assert "onload" not in serialized.lower()
    assert "onmouseover" not in serialized.lower()
    assert "url(" not in serialized.lower()

    # The path with onclick stripped keeps safe keys only; circle/rect get safe hex fallback colors; unsafe polygon dropped
    types = [s["type"] for s in cleaned]
    assert "script" not in types
    assert "polygon" not in types
    for stroke in cleaned:
        for color_key in ("fill", "stroke"):
            if color_key in stroke:
                val = stroke[color_key]
                assert val == "none" or val.startswith("#")


def test_start_round_and_theme():
    service = DrawAndGuessService()
    assert service.is_active() is False

    res = service.start_round(trigger_user="小明", gift_name="画笔", theme="蓝发小鲸鱼")
    assert res["status"] == "started"
    assert res["llm_generated"] is False
    assert res["theme"] == "蓝发小鲸鱼"
    assert service.is_active() is True
    assert "小明" in res["spoken_text"]
    assert res["gift_name"] == "画笔"
    assert service.current_word is not None
    assert service.current_word["word"] == "Aqua"


def test_check_guess_and_win_updates_scoreboard():
    daily_memory_mock = MagicMock()
    service = DrawAndGuessService(daily_memory=daily_memory_mock)
    service.start_round(trigger_user="小明")
    service.current_word = {
        "id": "test_cat",
        "word": "猫咪",
        "category": "动物",
        "aliases": ["猫", "小猫", "cat"],
        "hint": "尖尖耳朵",
        "progressive_hints": ["线索一", "线索二", "线索三"],
        "strokes": [{"type": "path", "d": "M0 0"}],
    }

    wrong_res = service.check_guess("u1", "小李", "这是一只小狗")
    assert wrong_res is None
    assert service.is_active() is True

    win_res = service.check_guess("u2", "小红", "我猜是小猫！", character_id="test-role")
    assert win_res is not None
    assert win_res["status"] == "won"
    assert win_res["won"] is True
    assert win_res["winner"] == "小红"
    assert win_res["display_name"] == "小红"
    assert win_res["word"] == "猫咪"
    assert service.scoreboard == {"小红": 1}
    assert service.is_active() is False
    assert daily_memory_mock.record_highlight.called


def test_tick_timeout_and_stop_round():
    service = DrawAndGuessService()
    service.start_round(trigger_user="小李", duration_sec=1)
    service.current_word = {
        "id": "aqua",
        "word": "Aqua",
        "category": "蓝发小鲸鱼",
        "aliases": ["小鲸鱼"],
        "hint": "可爱的蓝发小鲸鱼",
        "progressive_hints": ["p1", "p2", "p3"],
        "strokes": [],
    }

    service.started_at = time.time() - 2
    timeout_res = service.tick()
    assert timeout_res is not None
    assert timeout_res["status"] == "timeout"
    assert timeout_res["word"] == "Aqua"
    assert service.state == "finished"

    service.start_round(trigger_user="小李", duration_sec=60)
    stopped = service.stop_round()
    assert stopped["status"] == "stopped"
    assert service.is_active() is False


def test_get_overlay_state_progressive_hints():
    service = DrawAndGuessService()
    service.start_round(trigger_user="小王", duration_sec=100, theme="水果")
    service.current_word = {
        "id": "test_watermelon",
        "word": "西瓜",
        "category": "水果",
        "aliases": [],
        "hint": "红瓤黑籽",
        "progressive_hints": ["夏天吃的", "绿皮红瓤", "红瓤黑籽大西瓜"],
        "strokes": [
            {"type": "circle", "cx": 10, "cy": 10, "r": 5},
            {"type": "path", "d": "M0 0 L10 10"},
        ],
    }

    # < 35% elapsed -> hint_level 1
    service.started_at = time.time() - 10
    st1 = service.get_overlay_state()
    assert st1["active"] is True
    assert st1["hint_level"] == 1
    assert st1["progressive_hint"] == "夏天吃的"
    assert st1["hint"] == "夏天吃的"
    assert st1["mode"] == "aqua_draws"
    assert st1["theme"] == "水果"
    assert st1["llm_generated"] is False
    assert st1["scoreboard"] == {}

    # 35-70% elapsed -> hint_level 2
    service.started_at = time.time() - 50
    st2 = service.get_overlay_state()
    assert st2["hint_level"] == 2
    assert st2["progressive_hint"] == "绿皮红瓤"
    assert st2["hint"] == "绿皮红瓤"

    # >= 70% elapsed -> hint_level 3
    service.started_at = time.time() - 75
    st3 = service.get_overlay_state()
    assert st3["hint_level"] == 3
    assert st3["progressive_hint"] == "红瓤黑籽大西瓜"
    assert st3["hint"] == "红瓤黑籽大西瓜"


@pytest.mark.asyncio
async def test_start_round_llm_success_and_sanitization():
    service = DrawAndGuessService()
    service.recent_words.append("热白饭")

    llm = MagicMock()
    llm.generate_json = AsyncMock(
        return_value={
            "word": "咖啡机",
            "category": "日常家电",
            "aliases": ["咖啡机", "意式咖啡机"],
            "progressive_hints": ["早上提神的好帮手", "会发出蒸汽声的桌面机器", "三个字，用来萃取浓缩液"],
            "strokes": [
                {"type": "rect", "x": 100, "y": 50, "width": 500, "height": 200, "rx": 8, "fill": "#334155", "onclick": "alert(1)"},
                {"type": "circle", "cx": 200, "cy": 130, "r": 20, "fill": "#38bdf8"},
                {"type": "line", "x1": 120, "y1": 240, "x2": 280, "y2": 240, "stroke": "#0f172a", "width": 4},
                {"type": "polygon", "points": "160,180 240,180 230,230 170,230", "fill": "#f8fafc"},
                {"type": "ellipse", "cx": 200, "cy": 180, "rx": 30, "ry": 8, "fill": "#78350f"},
            ],
        }
    )

    res = await service.start_round_llm(llm, trigger_user="阿宅", theme="家电", duration_sec=75)
    assert res["status"] == "started"
    assert res["llm_generated"] is True
    assert res["category"] == "日常家电"
    assert res["theme"] == "家电"
    assert llm.generate_json.await_count == 1
    sent_messages = llm.generate_json.call_args.args[0]
    assert "热白饭" in sent_messages[1]["content"]

    st = service.get_overlay_state()
    assert st["llm_generated"] is True
    assert st["total_strokes"] == 5
    # Verify clamping and stripping on LLM-produced rect
    rect = service.current_word["strokes"][0]
    assert rect["width"] == 400
    assert "onclick" not in rect


@pytest.mark.asyncio
async def test_start_round_llm_fallback_on_failure_or_invalid_payload():
    service = DrawAndGuessService()

    # 1. Missing LLM -> fallback to WORD_BANK
    res_none = await service.start_round_llm(None, trigger_user="观众A")
    assert res_none["status"] == "started"
    assert res_none["llm_generated"] is False

    # 2. Raising LLM -> fallback to WORD_BANK
    bad_llm = MagicMock()
    bad_llm.generate_json = AsyncMock(side_effect=RuntimeError("boom"))
    res_err = await service.start_round_llm(bad_llm, trigger_user="观众B")
    assert res_err["status"] == "started"
    assert res_err["llm_generated"] is False

    # 3. Unsafe-only strokes -> fallback to WORD_BANK
    unsafe_llm = MagicMock()
    unsafe_llm.generate_json = AsyncMock(
        return_value={
            "word": "测试词",
            "category": "测试",
            "aliases": ["测试词"],
            "progressive_hints": ["1", "2", "3"],
            "strokes": [{"type": "script", "d": "alert(1)"}],
        }
    )
    res_unsafe = await service.start_round_llm(unsafe_llm, trigger_user="观众C")
    assert res_unsafe["status"] == "started"
    assert res_unsafe["llm_generated"] is False


@pytest.mark.asyncio
async def test_evaluate_guess_llm_fast_path_near_miss_and_semantic_win():
    daily_memory_mock = MagicMock()
    service = DrawAndGuessService(daily_memory=daily_memory_mock)
    service.start_round(trigger_user="小王", duration_sec=75)
    service.current_word = {
        "id": "mechanical_keyboard",
        "word": "机械键盘",
        "category": "数码外设",
        "aliases": ["客制化键盘", "keyboard"],
        "hint": "打字啪嗒响",
        "progressive_hints": ["h1", "h2", "h3"],
        "strokes": [{"type": "rect", "x": 50, "y": 80, "width": 200, "height": 80, "fill": "#1e293b"}],
    }

    llm = MagicMock()
    # 1. Near-miss roast (also verify secret word leak guard!)
    llm.generate_json = AsyncMock(
        return_value={
            "won": False,
            "near_miss": True,
            "roast": "哼，猜'键盘'算你蹭到边了，再想想是不是机械键盘的那种轴体手感？",
        }
    )
    near = await service.evaluate_guess_llm(llm, user_id="u1", display_name="路人甲", text="键盘")
    assert near is not None
    assert near["won"] is False
    assert near["near_miss"] is True
    assert near["display_name"] == "路人甲"
    assert "机械键盘" not in near["roast"]
    assert service.is_active() is True

    # 2. Semantic win via LLM
    llm.generate_json = AsyncMock(
        return_value={
            "won": True,
            "near_miss": False,
            "roast": "算你眼神还行，确实是能啪嗒啪嗒响的那玩意！",
        }
    )
    sem_win = await service.evaluate_guess_llm(
        llm, user_id="u2", display_name="大神乙", text="青轴打字机", character_id="whale", character_version=2
    )
    assert sem_win is not None
    assert sem_win["won"] is True
    assert sem_win["status"] == "won"
    assert sem_win["winner"] == "大神乙"
    assert sem_win["word"] == "机械键盘"
    assert service.scoreboard["大神乙"] == 1
    assert daily_memory_mock.record_highlight.called

    # 3. Fast-path exact match must NOT call LLM
    service.start_round(trigger_user="小王", duration_sec=75)
    service.current_word = {
        "id": "hot_white_rice",
        "word": "热白饭",
        "category": "干饭日常",
        "aliases": ["白饭"],
        "hint": "干饭",
        "progressive_hints": ["h1", "h2", "h3"],
        "strokes": [{"type": "circle", "cx": 100, "cy": 100, "r": 20, "fill": "#ffffff"}],
    }
    llm.generate_json = AsyncMock()
    fast_win = await service.evaluate_guess_llm(llm, user_id="u3", display_name="干饭王", text="我猜是白饭！")
    assert fast_win is not None
    assert fast_win["won"] is True
    assert fast_win["winner"] == "干饭王"
    assert llm.generate_json.await_count == 0
    assert service.scoreboard == {"大神乙": 1, "干饭王": 1}
