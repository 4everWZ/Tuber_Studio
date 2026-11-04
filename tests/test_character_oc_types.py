

from tuber_studio.models import Character
from tuber_studio.providers import build_messages


def test_character_model_persona_type_defaults():
    raw = {
        "id": "test_role",
        "name": "测试角色",
        "voice_profile": "test_voice",
        "avatar_manifest": "assets/manifest.json",
        "core": {
            "identity": "你是一个测试助手。",
            "traits": ["聪明", "友好"],
            "background": "来自测试实验室",
            "interaction": "友善对话",
        },
        "style": {
            "min_chars": 5,
            "max_chars": 50,
            "catchphrases": ["测试口癖"],
            "instructions": ["尽量精简回答"],
        },
    }
    char = Character.model_validate(raw)
    assert char.persona_type == "prompt_oc"
    assert char.fine_tuned_model is None
    assert char.compact_system_prompt is None

    dumped = char.model_dump()
    assert dumped["persona_type"] == "prompt_oc"


def test_character_model_fine_tuned_persona():
    raw = {
        "id": "whale_ft",
        "name": "大肥鱼微调版",
        "voice_profile": "whale_sovits_v2",
        "avatar_manifest": "assets/whale/manifest.json",
        "persona_type": "fine_tuned",
        "fine_tuned_model": "MiniCPM-V-4.7-1B",
        "compact_system_prompt": "你叫大肥鱼，是一只机灵傲娇、偏爱热白饭与海底小屋的深海鲸鱼娘主播。",
        "core": {
            "identity": "大肥鱼本体",
            "traits": ["傲娇", "爱吃白饭"],
            "background": "海底小屋",
            "interaction": "轻松逗趣",
        },
        "style": {
            "min_chars": 1,
            "max_chars": 60,
        },
    }
    char = Character.model_validate(raw)
    assert char.persona_type == "fine_tuned"
    assert char.fine_tuned_model == "MiniCPM-V-4.7-1B"
    assert "大肥鱼" in char.compact_system_prompt


def test_build_messages_fine_tuned_vs_prompt_oc():
    prompt_oc = {
        "id": "prompt_bot",
        "name": "Prompt OC",
        "persona_type": "prompt_oc",
        "core": {
            "identity": "你是来自深海的鲸鱼娘主播。",
            "traits": ["活泼", "话痨"],
            "background": "在珊瑚丛里生活了很久",
            "interaction": "热情问候每一位观众",
            "examples": [
                {"viewer": "主播好呀", "reply": "你好你好，欢迎来到本鱼的直播间！"},
                {"viewer": "在干嘛呢", "reply": "在摸鱼准备开饭呢~"},
            ],
        },
        "style": {
            "min_chars": 10,
            "max_chars": 80,
            "catchphrases": ["喵呜", "咕噜噜"],
            "instructions": ["每次都要详细展开背景故事", "必须展现人设细节"],
        },
        "rules": [
            "说话要带口癖",
            "不能破坏深海生物世界观",
            "对观众保持礼貌",
        ],
    }

    fine_tuned = {
        "id": "ft_bot",
        "name": "Fine-Tuned Persona",
        "persona_type": "fine_tuned",
        "fine_tuned_model": "MiniCPM-V-4.7-1B",
        "compact_system_prompt": "你叫大肥鱼，深海鲸鱼娘主播。用自然短句与直播间观众互动。",
        "core": prompt_oc["core"],
        "style": prompt_oc["style"],
        "rules": prompt_oc["rules"],
    }

    event = {"event_id": "e_test_1", "text": "主播今天吃什么？"}
    rel = {"affection": 10}

    # 1. Build messages for prompt_oc
    oc_messages = build_messages(prompt_oc, event, rel, mode="live")
    oc_system_str = "\n".join(m["content"] for m in oc_messages if m["role"] == "system")
    assert "角色设定：" in oc_messages[0]["content"]
    assert "角色互动示例：" in oc_system_str
    assert "必须展现人设细节" in oc_system_str

    # 2. Build messages for fine_tuned
    ft_messages = build_messages(fine_tuned, event, rel, mode="live")
    ft_system_str = "\n".join(m["content"] for m in ft_messages if m["role"] == "system")
    # Compact anchor prompt only
    assert ft_messages[0]["content"] == "你叫大肥鱼，深海鲸鱼娘主播。用自然短句与直播间观众互动。"
    assert "角色设定：" not in ft_messages[0]["content"]
    assert "角色互动示例：" not in ft_system_str
    assert "必须展现人设细节" not in ft_system_str

    # Token/length saving should be significant (>50% reduction in system instructions)
    assert len(ft_system_str) < len(oc_system_str) * 0.5
