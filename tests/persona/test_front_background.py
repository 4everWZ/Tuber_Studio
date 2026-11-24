import json
import time

import pytest

from tuber_studio.agent.memory_organizer import MemorySuggestion
from tuber_studio.agent.memory_organizer.owner_memory import private_rows
from tuber_studio.app import create_app
from tuber_studio.core.config import ROOT
from tuber_studio.persona.characters.background import front_background
from tuber_studio.persona.context.message_builder import build_messages
from tuber_studio.persona.memory.daily import front_topic_keywords


def preset():
    return json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))[0]


@pytest.mark.parametrize("question", [
    "晚上好！", "鱼鱼你好呀", "你是谁？", "你觉得我们是朋友吗？", "你平时说话是什么风格？",
    "我想说说我的过去。", "你平时喜欢做什么？",
])
def test_ordinary_questions_leave_card_background_out(question):
    card = preset()
    assert front_background(card, question) == []
    messages = build_messages(card, {"text": question}, {}, "operator_chat")
    assert "角色背景" not in messages[2]["content"]


def test_preset_and_readonly_legacy_fields_are_verbatim():
    bundled = preset()
    assert isinstance(front_background(bundled, "你平时做什么？"), list)
    card = {
        "name": "Aqua",
        "core": {"background": "平时看视频、逛评论区，桌边亮着一盏小小的贝壳灯。"},
        "lore": [{"keywords": ["摸鱼"], "text": "偶尔摸鱼放空看气泡，被抓包会嘴硬。"}],
    }
    result = front_background(card, "你的小屋里有贝壳灯吗？")
    assert result and result[0] in card["core"]["background"] and "贝壳灯" in result[0]
    assert front_background(card, "你是谁？你的小屋里有贝壳灯吗？") == result
    lore = next(entry for entry in card["lore"] if "摸鱼" in entry["keywords"])
    assert any(text in lore["text"] for text in front_background(card, "摸鱼的时候呢？"))

    legacy = {"name": "小舟", "core": {"identity": "你是摆渡人。小时候第一次去了石桥。你住在渔村。常说「水涨啦」。",
                                      "appearance": {"eyes": "眼睛是琥珀色。"}},
              "style": {"instructions": "说话轻快。"}, "lore": []}
    for question, expected in [
        ("讲讲你的过去", "小时候第一次去了石桥。"), ("你住在哪？", "你住在渔村。"),
        ("你长什么样？", "眼睛是琥珀色。"), ("你有什么口头禅？", "常说「水涨啦」。"),
    ]:
        assert expected in front_background(legacy, question)


def test_stored_experiences_follow_last_available_topic_only():
    experience = "雨夜沿岸找回了船铃。"
    card = preset()
    card["core"]["experiences"] = [{"title": "雾港寻铃", "text": experience, "source": "隐藏元数据"}]
    history = [{"role": "user", "content": "说说雾港寻铃"}, {"role": "assistant", "content": "还记得那场雨。"}]
    assert front_background(card, "说说你的经历")[0] == experience
    assert front_background(card, "你以前有什么经历？")[0] == experience
    assert not front_background(card, "讲讲你以前在火星采矿的事")
    assert front_background(card, "后来呢？", history=history) == [experience]
    assert front_background(card, "你是谁？后来呢？", history=history) == [experience]
    saved = {"text": "算术题", "event_id": "雾港寻铃"}
    for question, window in [
        ("后来呢？", []), ("晚上好", history),
        ("后来呢？", [{"kind": "summary", "role": "user", "content": "雾港寻铃"}]),
        ("后来呢？", [{"kind": "viewer", "role": "user", "content": "当前号主消息：" + json.dumps(saved, ensure_ascii=False)}]),
        ("后来呢？", [*history, {"role": "user", "content": "算术题"}, {"role": "assistant", "content": "一加一等于二。"}]),
    ]:
        assert not front_background(card, question, history=window)
    messages = build_messages(card, {"text": "后来呢？"}, {}, "operator_chat", history=history)
    assert sum(item["content"].count(experience) for item in messages) == 1
    assert "隐藏元数据" not in str(messages)


def test_long_card_keeps_only_short_matching_original():
    card = {"core": {"background": "很久以前的事。" * 70 + "去年迎接了信天翁。"}, "lore": []}
    assert front_background(card, "信天翁呢？") == ["去年迎接了信天翁。"]
    assert not front_topic_keywords("what do you usually like about the weather") & {"the", "what", "you", "like"}
    assert "theater" in front_topic_keywords("the theater")


async def test_unrelated_framing_cannot_recall_three_independent_private_preferences(settings):
    app = create_app(settings)
    runtime = app.state.runtime
    memory = runtime.daily_memory
    scope = runtime.memory_organizer.scope()
    try:
        for topic in ("字幕", "剪辑", "排版"):
            note = "我平时喜欢" + topic + "先给我一份样稿。"
            source = {"kind": "private", "key": "settings:" + topic, "text": note, "at": time.time(),
                      "private_owner": memory.private_owner, "owner_confirmed": True}
            suggestion = MemorySuggestion(action="add", existing_id=None, headline=topic, note=note, tags=[])
            assert memory.save_organized(scope, source, suggestion, None)
        assert not private_rows(runtime, "你平时喜欢做什么？")
        rows = private_rows(runtime, "你平时喜欢什么颜色的字幕？")
        assert [row["headline"] for row in rows] == ["字幕"]
        assert private_rows(runtime, "settings:排版")[0]["headline"] == "排版"
    finally:
        await runtime.llm.close()
        await app.state.bilibili_account.adapter.close()
        await app.state.browsing.close()
        runtime.store.close()
