import json

import pytest

from tuber_studio.agent.editor_contract import FormatCheck, review_preferences
from tuber_studio.agent.task_delivery import DelegationError, PrivateDelivery


async def test_private_contract_merges_literal_and_semantic_issues_and_preserves_locked_fields():
    product = PrivateDelivery(items=[{"title":"旧标题", "rationale":"旧理由", "sources":["frame"]},
                                     {"title":"试播", "rationale":"先介绍来龙去脉，再给结论", "sources":["frame"]}], gaps=[])
    notes = {2:"标题统一以「素材」开头，理由先给结论。", 5:"每个理由最多六个字。"}
    checks = [(2, FormatCheck(field="title", operation="starts_with", value="素材")),
              (5, FormatCheck(field="rationale", operation="max_chars", value="6"))]
    async def review(messages, schema):
        packet = json.loads(messages[-1]["content"])
        assert packet["writing_preference"] in notes.values()
        assert set(packet["delivery_fields"]) == {"items.1.title", "items.1.rationale"}
        return {"violations":["items.1.rationale"] if packet["writing_preference"] == notes[2] else []}
    issues = await review_preferences("原样保留第一项", notes, checks, product, review, {"items.0.title","items.0.rationale"})
    assert notes[2] in issues["items.1.title"]
    assert notes[2] in issues["items.1.rationale"] and notes[5] in issues["items.1.rationale"]
    with pytest.raises(DelegationError, match="未选"):
        await review_preferences("", notes, [(100, checks[0][1])], product, review, set())


def test_private_punctuation_and_codepoint_length_checks():
    suffix = FormatCheck(field="title", operation="ends_with", value="？")
    assert suffix.meets("这是哪里?") and not suffix.meets("宫廷场景")
    size = suffix.model_copy(update={"operation":"max_chars","value":"1"})
    assert size.meets("Ⅳ") and not size.meets("IV")
