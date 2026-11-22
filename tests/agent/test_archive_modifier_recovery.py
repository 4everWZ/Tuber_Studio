import json

import pytest

from tests.support.agent_helpers import with_frame_structure
from tuber_studio.agent.tasks.delivery import DelegationError
from tuber_studio.agent.tasks.visual_specificity import review_visual_specificity, shared_visual_facts


@with_frame_structure
async def model(messages, schema, **kwargs):
    properties = schema["properties"]
    content = messages[-1]["content"]
    if "details" in properties:
        return {"details": ["中央可见银质环纹"]}
    if "alternatives" in properties:
        return {
            "alternatives": {
                key: [{"term": "环纹", "other": "亮纹", "discriminator": "轮廓"}] if "环纹" in text else []
                for key, text in json.loads(content).items()
            }
        }
    if "counterfactual_clause" in properties:
        return {
            "counterfactual_clause": "中央可见银质环纹（亮纹）",
            "original_name": "原称呼指画中被表现的形象，另一解释是其载体",
            "unsupported_modifiers": ["没有出现在原句的词", "银质"],
            "basis": "画面中仅可见纹样",
        }
    if "conflict" in properties:
        return {"original_meaning": "纹样", "alternative_meaning": "纹样", "conflict": False}
    return {
        k: {
            "verdict": "原词仅描述共同可见特征",
            "common_label": "环纹",
            "observation": "轮廓有环纹",
            "distinguishing_cue": "",
            "identifying_feature": "",
        }
        for k in properties
    }


async def test_unlocated_modifier_is_quarantined_per_candidate_and_keeps_literal_objections():
    options = {"title": "中央可见银质环纹", "other": "左侧轮廓清晰"}
    disputed = await review_visual_specificity(options, {}, "step:1", "", model, 512)
    assert list(disputed) == ["title"]
    row = disputed["title"]
    assert row["field"] == "title" and row["primary_frame"] == "step:1" and row["text"] == options["title"]
    assert [claim["target"] for claim in row["claims"]] == ["银质"]
    assert row["unlocated"][0]["modifiers"] == ["没有出现在原句的词"]
    audit = []
    supported = await shared_visual_facts(
        {},
        "step:1",
        "",
        list(options.values()),
        [],
        model,
        512,
        field="items.0.title",
        candidate_reviews=audit,
    )
    assert supported == [options["other"]]
    assert audit[0]["candidate"] == options["title"] and audit[0]["field"] == "items.0.title"
    assert audit[0]["unlocated"] == row["unlocated"] and audit[0]["claims"] == row["claims"]
    with pytest.raises(DelegationError) as failed:
        await shared_visual_facts(
            {}, "step:1", "", [options["title"]], [], model, 512, field="items.0.rationale"
        )
    assert failed.value.attribution_reviews[0]["field"] == "items.0.rationale"
    assert failed.value.attribution_reviews[0]["primary_frame"] == "step:1"


async def test_unlocated_only_holds_one_candidate_without_claiming_its_whole_text_is_false():
    options = {"bad": "中央可见环纹", "good": "左侧轮廓清晰"}
    disputed = await review_visual_specificity(options, {}, "frame", "", model, 512)
    assert list(disputed) == ["bad"] and disputed["bad"]["claims"] == []
    assert disputed["bad"]["unlocated"][0]["modifiers"] == ["没有出现在原句的词", "银质"]
