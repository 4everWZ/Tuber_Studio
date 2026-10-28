import json

import pytest

from tuber_studio.compaction import SUMMARY_PREFIX
from tuber_studio.context import summary_budget
from tuber_studio.models import Reply
from tuber_studio.providers import single_system_messages


def reply(text, event_id="ask"):
    return Reply(spoken_text=text, emotion="cheerful", animation_id="talk", reply_to=[event_id])


@pytest.mark.parametrize("repair_works", [True, False])
async def test_source_survives_compaction_and_repair_is_bounded(runtime, store, monkeypatch, repair_works):
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
        return reply(original if repair_works and len(calls) == 2 else "小壳送了我三份海苔饭团。")

    monkeypatch.setattr(runtime.llm, "generate", generate)
    event = {"event_id": "ask", "text": "你刚才去了哪里，谁送了你什么？"}
    async with runtime.generation_lock:
        answer, audit = await runtime.generate_reply(scope, character, event)
    assert len(calls) == 2 and audit["retried"] is True
    assert audit["missing"] == ([] if repair_works else ["location"])
    assert bool(audit["warning"]) is (not repair_works)
    assert audit["sources"][0]["event_id"] == "comment:4"
    runtime.context.record(scope, character, event, answer.spoken_text, memory_kind=audit["memory_kind"])
    derived = store.one("SELECT facts,kind FROM grounded_memory WHERE event_id='ask' AND role='assistant'")
    assert derived["kind"] == "derived_reply" and json.loads(derived["facts"]) == []


@pytest.mark.parametrize("question", ["今天想聊什么？", "你刚才去了哪里，谁送了什么？"])
async def test_no_supported_source_never_forces_fact_repair(runtime, monkeypatch, question):
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
