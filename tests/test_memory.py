import json

import pytest

from tuber_studio.memory import GroundedMemory, extract_facts, missing_slots, required_slots

CHAR = {"id": "original_seal", "version": 1}
SCOPE = "bilibili:BVdemo:thread:root7"
STORY = "我去了城北书店，店员阿岚送我两本诗集。"


@pytest.fixture
def memory(store):
    return GroundedMemory(store)


def save(memory, text=STORY, *, event="comment:7", char=None, scope=SCOPE, role="assistant", **kwargs):
    return memory.record(scope, char or CHAR, event, role, text, **kwargs)


def test_source_provenance_is_literal_and_fictional(memory):
    source = save(memory)
    result = memory.retrieve(SCOPE, CHAR, "你刚才去了哪里，谁送了你什么？")
    assert result["required_slots"] == ["location", "person", "item"]
    row = result["evidence"][0]
    assert row["source_id"] == source
    assert row["event_id"] == "comment:7"
    assert row["role"] == "assistant" and row["kind"] == "role_story" and row["fictional"]
    assert row["text"] == STORY
    assert {f["slot"]: f["value"] for f in row["facts"]} == {
        "location": "城北书店",
        "person": "店员阿岚",
        "item": "诗集",
    }
    for fact in row["facts"]:
        assert STORY[slice(*fact["span"])] == fact["quote"]
        assert fact["value"] in fact["quote"]
    assert missing_slots("我去了城北书店，阿岚送我两本诗集。", result) == []


@pytest.mark.parametrize(
    "fact",
    [
        {"slot": "location", "value": "月光城", "quote": STORY, "span": [0, len(STORY)]},
        {"slot": "location", "value": "城北书店", "quote": "我去了月光城", "span": [0, 7]},
        {"slot": "location", "value": "城北书店", "quote": STORY, "span": [-1, len(STORY)]},
        {"slot": "person", "value": "城北书店", "quote": STORY, "span": [0, len(STORY)]},
        {"slot": "location", "value": "城北书店", "quote": STORY, "span": [False, len(STORY)]},
    ],
)
def test_external_facts_cannot_forge_value_span_or_semantic_slot(memory, fact):
    with pytest.raises(ValueError):
        save(memory, facts=[fact])
    assert memory.retrieve(SCOPE, CHAR, "去了哪里？")["evidence"] == []


def test_literal_but_unsupported_external_semantics_rejected(memory):
    text = "城北书店是一个好听的名字。"
    with pytest.raises(ValueError, match="not supported"):
        save(
            memory,
            text,
            facts=[{"slot": "location", "value": "城北书店", "quote": text, "span": [0, len(text)]}],
        )


def test_supported_fact_candidates_are_canonicalized(memory):
    candidate = next(f for f in extract_facts(STORY) if f["slot"] == "location")
    save(memory, facts=[candidate])
    result = memory.retrieve(SCOPE, CHAR, "你去了哪里？")
    assert result["evidence"][0]["facts"] == [candidate]


def test_candidate_aliases_cannot_invent_another_identity(memory):
    person = next(f for f in extract_facts(STORY) if f["slot"] == "person")
    assert person["aliases"] == ["阿岚"]
    with pytest.raises(ValueError, match="aliases"):
        save(memory, facts=[{**person, "aliases": ["另一位角色"]}])
    save(memory, facts=[person])
    evidence = memory.retrieve(SCOPE, CHAR, "谁送了你东西？")
    assert missing_slots("阿岚送的。", evidence) == []


@pytest.mark.parametrize(
    "scope,char",
    [
        ("bilibili:BVdemo:thread:other", CHAR),
        (SCOPE, {"id": "other_oc", "version": 1}),
        (SCOPE, {"id": CHAR["id"], "version": 2}),
    ],
)
def test_thread_character_and_version_are_isolated(memory, scope, char):
    save(memory)
    assert memory.retrieve(scope, char, "你去了哪里？")["evidence"] == []


def test_viewer_statement_is_preserved_without_becoming_character_fact(memory):
    save(memory, role="user")
    result = memory.retrieve(SCOPE, CHAR, "城北书店的诗集呢？")
    row = result["evidence"][0]
    assert row["role"] == "user" and row["kind"] == "viewer_message"
    assert row["fictional"] is False and row["facts"] == []
    assert missing_slots("不知道", {**result, "required_slots": ["location", "person", "item"]}) == []


@pytest.mark.parametrize("kind", ["derived_reply", "grounded_reply"])
def test_grounded_replies_are_audited_but_never_reused_as_independent_proof(memory, store, kind):
    save(memory, kind=kind)
    row = store.one("SELECT * FROM grounded_memory")
    assert row["text"] == STORY and row["fictional"] == 1 and json.loads(row["facts"]) == []
    assert memory.retrieve(SCOPE, CHAR, "城北书店里谁送你什么？")["evidence"] == []
    with pytest.raises(ValueError, match="not supported"):
        save(memory, event="second", kind=kind, facts=[extract_facts(STORY)[0]])


def test_corresponding_occurred_event_outranks_recent_plan_and_does_not_mix_stories(memory):
    save(memory)
    save(memory, "我去了南岸花店，摊主阿泽送我三束鲜花。", event="comment:8")
    save(memory, "下期我去了月光夜市，店主阿树送我四份果冻。", event="comment:9")
    latest = memory.retrieve(SCOPE, CHAR, "你刚才去了哪里，谁送了你什么？")
    assert [r["event_id"] for r in latest["evidence"]] == ["comment:8"]
    older = memory.retrieve(SCOPE, CHAR, "在城北书店，谁送了你什么？")
    assert [r["event_id"] for r in older["evidence"]] == ["comment:7"]
    assert "月光夜市" not in json.dumps(latest, ensure_ascii=False)


@pytest.mark.parametrize(
    "text",
    [
        "我没有去过城北书店。",
        "明天我去了城北书店，店员阿岚送我两本诗集。",
        "也许我去了城北书店。",
        "谁送了我两本诗集？",
        "店员阿岚可能送我两本诗集。",
    ],
)
def test_negated_future_uncertain_and_question_clauses_do_not_establish_facts(text):
    assert extract_facts(text) == []


def test_unknown_form_remains_searchable_as_original_only(memory):
    text = "银雾隧道的旋律像雨点一样。"
    save(memory, text)
    result = memory.retrieve(SCOPE, CHAR, "你说的银雾隧道是什么样？")
    assert result["evidence"][0]["text"] == text
    assert result["evidence"][0]["facts"] == []
    assert missing_slots("忘了", result) == []


def test_clear_question_reports_only_supported_missing_slots(memory):
    save(memory)
    evidence = memory.retrieve(SCOPE, CHAR, "你去了哪里，谁送了你什么，有几本？")
    assert evidence["required_slots"] == ["location", "person", "item", "quantity"]
    assert missing_slots("阿岚送我两本诗集。", evidence) == ["location"]
    assert missing_slots("去了城北书店。", evidence) == ["person", "item", "quantity"]


def test_casual_chat_and_absent_evidence_do_not_request_retry(memory):
    save(memory)
    casual = memory.retrieve(SCOPE, CHAR, "你好呀，今天心情不错！")
    assert casual["required_slots"] == [] and missing_slots("一起开心吧", casual) == []
    absent = memory.retrieve("empty-thread", CHAR, "去了哪里，谁送了你什么？")
    assert absent["evidence"] == [] and missing_slots("不记得了", absent) == []
    assert required_slots("下期准备去哪里？") == []


@pytest.mark.parametrize("quantity", ["三个", "3份", "三份"])
def test_known_counter_synonyms_are_not_falsely_rejected(memory, quantity):
    save(memory, "我去了风铃农场，朋友阿黎送我三份甜饼。")
    evidence = memory.retrieve(SCOPE, CHAR, "你收到了多少？")
    assert missing_slots("收到了" + quantity + "甜饼。", evidence) == []
    assert missing_slots("收到了四份甜饼。", evidence) == ["quantity"]


def test_unknown_count_expression_is_not_overjudged(memory):
    save(memory)
    evidence = memory.retrieve(SCOPE, CHAR, "刚才有几本？")
    assert missing_slots("阿岚给我一双诗集。", evidence) == []
    assert missing_slots("两。", evidence) == []


@pytest.mark.parametrize("question", ["你家在哪里？", "谁是DeepSeek？", "what's up?", "这家店的老板是谁？"])
def test_generic_identity_place_and_smalltalk_do_not_force_old_story_facts(memory, question):
    save(memory)
    evidence = memory.retrieve(SCOPE, CHAR, question)
    assert evidence["required_slots"] == []
    assert missing_slots("你好，很高兴认识你。", evidence) == []


@pytest.mark.parametrize(
    "question", ["城北书店的店主是谁？", "comment:7里谁送了礼物？", "阿岚送的是什么东西？"]
)
def test_explicit_source_entity_or_event_keeps_factual_followup(memory, question):
    save(memory)
    save(memory, "我去了南岸花店，摊主阿泽送我三束鲜花。", event="comment:8")
    evidence = memory.retrieve(SCOPE, CHAR, question)
    assert evidence["required_slots"]
    assert [r["event_id"] for r in evidence["evidence"]] == ["comment:7"]
    assert missing_slots("忘记啦。", evidence)


def test_audit_only_replies_do_not_evict_originals_from_bounded_search(memory):
    save(memory)
    for i in range(205):
        save(memory, "这是依据旧原句整理的回答。", event=f"derived:{i}", kind="derived_reply")
    evidence = memory.retrieve(SCOPE, CHAR, "刚才去了哪里？")
    assert [r["event_id"] for r in evidence["evidence"]] == ["comment:7"]


@pytest.mark.parametrize("question", ["Where did you go earlier?", "What did the shopkeeper give you?"])
def test_explicit_english_past_questions_use_source_slots(memory, question):
    save(memory)
    evidence = memory.retrieve(SCOPE, CHAR, question)
    assert evidence["required_slots"] and evidence["evidence"]
    assert missing_slots("I forgot.", evidence)


def test_source_is_immutable_and_idempotent(memory):
    first = save(memory)
    assert save(memory) == first
    with pytest.raises(ValueError, match="cannot be rewritten"):
        save(memory, "我去了另一个地方。")


def test_retrieval_budget_limits_original_excerpts_and_count(memory):
    for i in range(6):
        save(memory, ("风铃旋律" * 100) + f"第{i}段。", event=f"e:{i}")
    result = memory.retrieve(SCOPE, CHAR, "风铃旋律是什么？", limit=2, max_chars=600)
    assert 0 < len(result["evidence"]) <= 2
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in result["evidence"]) <= 600
    assert all(
        r["text"] in ("风铃旋律" * 100 + f"第{i}段。")
        for r in result["evidence"]
        for i in [int(r["event_id"].split(":")[1])]
    )
