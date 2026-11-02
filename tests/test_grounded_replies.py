import json

import pytest

from tuber_studio.compaction import SUMMARY_PREFIX
from tuber_studio.context import summary_budget
from tuber_studio.models import Reply
from tuber_studio.providers import single_system_messages


def reply(text, event_id="ask"):
    return Reply(spoken_text=text, emotion="cheerful", animation_id="talk", reply_to=[event_id])


@pytest.mark.parametrize(
    "first_answer",
    [
        "小壳送了我三份海苔饭团。",
        "就是那家珊瑚小店呀，小壳塞给我仨海苔饭团！",
        "我把饭团抱得可紧了，小壳都笑啦，回头还想再去珊瑚铺！",
        "我去了月光车站，阿泽送我四份果冻。",
    ],
)
async def test_source_survives_compaction_and_lexical_diagnostic_keeps_first_answer(
    runtime, store, monkeypatch, first_answer
):
    character, scope = runtime.character, "one-thread"
    original = "我去了珊瑚铺，店主小壳送我三份海苔饭团。"
    runtime.context.record(scope, character, {"event_id": "comment:4", "text": "你去哪里了？"}, original)
    row = runtime.context.window(scope, character)
    store.execute(
        "UPDATE context_windows SET history=? WHERE key=?",
        (
            json.dumps([{"role": "user", "kind": "summary", "content": SUMMARY_PREFIX + "\n聊过一个故事。"}]),
            row["key"],
        ),
    )
    calls = []

    async def generate(messages, character, event_id, source_text=None):
        calls.append(messages)
        assert any(original in message["content"] for message in messages)
        assert any('"fictional": true' in message["content"] for message in messages)
        assert len(calls) == 1, "Lexical diagnostics must not trigger a second generation"
        return reply(first_answer)

    monkeypatch.setattr(runtime.llm, "generate", generate)
    event = {"event_id": "ask", "text": "你刚才去了哪里，谁送了你什么，有多少份？"}
    async with runtime.generation_lock:
        answer, audit = await runtime.generate_reply(scope, character, event)
    assert len(calls) == 1 and audit["retried"] is False
    assert answer.spoken_text == first_answer
    assert answer.emotion == "cheerful" and answer.animation_id == "talk"
    assert audit["missing"]
    assert audit["method"] == "lexical_coverage" and audit["status"] == "needs_review"
    assert audit["semantic_verdict"] is None
    assert audit["warning"].startswith("待核对：")
    if "月光车站" in first_answer:
        assert audit["missing"] == ["location", "person", "item", "quantity"]
    assert audit["sources"][0]["event_id"] == "comment:4"
    runtime.context.record(scope, character, event, answer.spoken_text, memory_kind=audit["memory_kind"])
    derived = store.one("SELECT facts,kind FROM grounded_memory WHERE event_id='ask' AND role='assistant'")
    assert derived["kind"] == "derived_reply" and json.loads(derived["facts"]) == []


@pytest.mark.parametrize("question", ["今天想聊什么？", "你刚才去了哪里，谁送了什么？"])
async def test_no_supported_source_generates_without_fact_review(runtime, monkeypatch, question):
    calls = []

    async def generate(*args):
        calls.append(args)
        return reply("我还没有讲过这段故事呀。")

    monkeypatch.setattr(runtime.llm, "generate", generate)
    async with runtime.generation_lock:
        _, audit = await runtime.generate_reply(
            "empty-thread", runtime.character, {"event_id": "ask", "text": question}
        )
    assert len(calls) == 1
    assert audit["sources"] == [] and audit["missing"] == [] and audit["retried"] is False
    assert audit["status"] == "not_applicable" and audit["semantic_verdict"] is None


@pytest.mark.parametrize(
    "first_answer",
    [
        "我去了珊瑚铺，小壳送我三份海苔饭团。",
        "我没去珊瑚铺，小壳也没送我三份海苔饭团。",
    ],
)
async def test_literal_matches_are_not_semantic_fact_acceptance(runtime, monkeypatch, first_answer):
    scope, character = "literal-only", runtime.character
    runtime.context.record(
        scope,
        character,
        {"event_id": "story", "text": "刚才发生了什么？"},
        "我去了珊瑚铺，店主小壳送我三份海苔饭团。",
    )
    calls = []

    async def generate(*args):
        calls.append(args)
        return reply(first_answer)

    monkeypatch.setattr(runtime.llm, "generate", generate)
    async with runtime.generation_lock:
        answer, audit = await runtime.generate_reply(
            scope, character, {"event_id": "ask", "text": "你刚才去了哪里，谁送了你什么，有多少份？"}
        )
    assert len(calls) == 1 and answer.spoken_text == first_answer
    assert audit["missing"] == [] and audit["status"] == "lexical_match"
    assert audit["semantic_verdict"] is None and audit["retried"] is False


async def test_casual_role_story_keeps_voice_without_fact_check(runtime, monkeypatch):
    first_answer = "我想在鲸尾上挂一盏小灯，陪你一起看海底的星星。"
    scope, character = "casual-story", runtime.character
    runtime.context.record(
        scope, character, {"event_id": "old-story", "text": "逛街了吗？"}, "我去了珊瑚铺。"
    )

    async def generate(*args):
        return reply(first_answer)

    monkeypatch.setattr(runtime.llm, "generate", generate)
    async with runtime.generation_lock:
        answer, audit = await runtime.generate_reply(
            scope, character, {"event_id": "ask", "text": "今晚想做点什么？"}
        )
    assert answer.spoken_text == first_answer
    assert audit["status"] == "not_applicable" and not audit["warning"]
    assert audit["required_slots"] == [] and audit["memory_kind"] == "role_story"


def test_adaptive_summary_budget_tracks_information_not_only_text_size():
    base = {"compaction_max_tokens": 2048}
    greetings = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
    assert summary_budget(greetings, base) == 512
    assert summary_budget(greetings, base | {"enable_thinking": True}) == 1024
    dense = [
        {"role": "assistant", "content": f"我去了星桥{i}号铺，店主阿岚送我三份饭团。"} for i in range(40)
    ]
    assert 2048 < summary_budget(dense, base) <= 6144
    assert base == {"compaction_max_tokens": 2048}


def test_single_system_template_adapter_keeps_instructions_and_dialogue_intact():
    original = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "original question"},
        {"role": "assistant", "content": "original answer"},
        {"role": "system", "content": "format"},
    ]
    assert single_system_messages(original) == [
        {"role": "system", "content": "identity\n\nformat"},
        {"role": "user", "content": "original question"},
        {"role": "assistant", "content": "original answer"},
    ]
    assert original[-1] == {"role": "system", "content": "format"}


def test_nonverbal_event_does_not_fabricate_a_viewer_source(runtime, store):
    runtime.context.record("live:sfx", runtime.character, {"event_id": "sfx:1", "text": ""}, "喵")
    rows = store.all("SELECT role,text FROM grounded_memory WHERE event_id='sfx:1'")
    assert rows == [{"role": "assistant", "text": "喵"}]
