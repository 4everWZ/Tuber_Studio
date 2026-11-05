import json

import pytest

from tuber_studio.config import Settings
from tuber_studio.inference_profiles import DAFEIYU_KEY, DAFEIYU_MODEL
from tuber_studio.models import Character
from tuber_studio.persona import uses_built_in_persona
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


@pytest.mark.parametrize("mode", ["live", "comment_draft", "comment_channel_decision", "direct_message"])
def test_build_messages_fine_tuned_vs_prompt_oc(mode):
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
        "lore": [{"text": "珊瑚村的料理故事", "keywords": ["吃什么"]}],
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
        "lore": prompt_oc["lore"],
    }

    event = {"event_id": "e_test_1", "text": "主播今天吃什么？"}
    rel = {"affection": 10}

    # 1. Build messages for prompt_oc
    oc_messages = build_messages(prompt_oc, event, rel, mode=mode)
    oc_system_str = "\n".join(m["content"] for m in oc_messages if m["role"] == "system")
    assert "角色设定：" in oc_messages[0]["content"]
    assert ("角色互动示例：" in oc_system_str) == (mode != "comment_channel_decision")
    assert "必须展现人设细节" in oc_system_str
    assert "珊瑚村的料理故事" in str(oc_messages)

    # 2. Build messages for fine_tuned
    settings = {
        "llm": {"kind": "llama_cpp", "model": DAFEIYU_MODEL, "base_url": "http://jetson.local:9012/llm"},
        "inference_broker": {"enabled": True, "base_url": "http://jetson.local:9012"},
    }
    ft_messages = build_messages(fine_tuned, event, rel, mode=mode, settings=settings)
    ft_system_str = "\n".join(m["content"] for m in ft_messages if m["role"] == "system")
    # Compact anchor prompt only
    assert ft_messages[0]["content"] == "你叫大肥鱼，深海鲸鱼娘主播。用自然短句与直播间观众互动。"
    assert "角色设定：" not in ft_messages[0]["content"]
    assert "角色互动示例：" not in ft_system_str
    assert "必须展现人设细节" not in ft_system_str
    assert "珊瑚村的料理故事" not in str(ft_messages)
    if mode == "live":
        assert "用10到80字回答。" in oc_system_str
        assert "用1到60字回答。" in ft_system_str
        assert "用10到80字回答。" not in ft_system_str

    # Model choice controls the prompt source even if the selected role is of the other type.
    builtin = build_messages(prompt_oc, event, rel, mode=mode, settings=settings)
    assert builtin[0]["content"] == "你是大肥鱼。"
    assert "必须展现人设细节" not in str(builtin)
    base = build_messages(
        fine_tuned,
        event,
        rel,
        mode=mode,
        settings={"llm": {"kind": "llama_cpp", "model": DAFEIYU_MODEL, "base_url": "http://localhost:8080"}},
    )
    assert "必须展现人设细节" in base[0]["content"]
    assert fine_tuned["compact_system_prompt"] not in str(base)


@pytest.mark.parametrize(
    ("route", "old_model", "model", "expected"),
    [("dafeiyu", "MiniCPM5-2B", DAFEIYU_MODEL, True), ("2b", DAFEIYU_KEY, "MiniCPM5-2B", False)],
)
def test_legacy_node_configuration_migrates_its_model_and_prompt_source(
    settings, route, old_model, model, expected
):
    base_url = "http://jetson.local:9012"
    data = settings.data
    data["inference_broker"].update(enabled=True, base_url=base_url)
    data["llm"].update(kind="llama_cpp", base_url=f"{base_url}/llm/{route}/", model=old_model)
    data["work_mode_profiles"] = {
        "text": {"llm": {**data["llm"], "kind": "openai_compatible"}},
        "live": {
            "llm": {**data["llm"], "kind": "openai_compatible", "base_url": "https://api.example/llm/2b"}
        },
    }
    settings.path.write_text(json.dumps(data), encoding="utf-8")

    restored = Settings(runtime=settings.runtime)
    llm = restored.data["llm"]
    profiles = restored.data["work_mode_profiles"]
    assert llm["base_url"] == base_url + "/llm" and llm["model"] == model
    assert profiles["text"]["llm"] == llm
    assert profiles["live"]["llm"] == data["work_mode_profiles"]["live"]["llm"]
    assert uses_built_in_persona(restored.data) is expected
    assert restored.endpoint_key("llm") == restored.inference_token
    assert restored.broker_key_for(f"{base_url}/llm/{route}") is None
    assert json.loads(settings.path.read_text())["llm"]["model"] == old_model

    restored.save_public(restored.data)
    assert json.loads(settings.path.read_text())["llm"]["base_url"] == base_url + "/llm"


def test_compatible_api_uses_card_even_with_a_matching_model_name():
    assert not uses_built_in_persona({"llm": {"kind": "openai_compatible", "model": DAFEIYU_KEY}})
