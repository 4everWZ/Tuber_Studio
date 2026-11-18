import json

import pytest

from tuber_studio.agent.editor_contract import FormatCheck, literal_checks, literal_evidence, review_preferences
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
    assert issues == {"items.1.title":[2], "items.1.rationale":[2,5]}
    with pytest.raises(DelegationError, match="未选"):
        await review_preferences("", notes, [(100, checks[0][1])], product, review, set())


def test_private_punctuation_and_codepoint_length_checks():
    suffix = FormatCheck(field="title", operation="ends_with", value="？")
    assert suffix.meets("这是哪里?") and not suffix.meets("宫廷场景")
    size = suffix.model_copy(update={"operation":"max_chars","value":"1"})
    assert size.meets("Ⅳ") and not size.meets("IV")


@pytest.mark.parametrize(("note","expected"), [
    ("以后写直播素材提纲，每个标题都用问句，以问号结尾。", [("title","ends_with","？")]),
    ("以后直播提纲的标题都用陈述句，以句号结尾。", [("title","ends_with","。")]),
    ("标题以「本周」开头，理由最多20个字。", [("title","starts_with","本周"),("rationale","max_chars","20")]),
    ("缺口须包含“待核实”。", [("gap","contains","待核实")]),
    ("如果是开播预告，标题可以以问号结尾。", []),
    ("第二个标题以问号结尾。", []), ("标题不要以问号结尾。", []),
    ("标题讨论以问号结尾的商标。", []), ("标题不要求包含“独家”。", []),
    ("标题以？结尾。", [("title","ends_with","？")]),
    ("标题以「甲，乙」开头。", [("title","starts_with","甲，乙")]),
])
def test_unambiguous_owner_field_literals(note, expected):
    assert [(c.field,c.operation,c.value) for c in literal_checks(note)] == expected
    for binding in literal_evidence(note):
        assert binding["quote"] == note[binding["start"]:binding["end"]]


@pytest.mark.parametrize(("phase","title","form","core","observation","punctuation","passed"), [
    ("voice_corrected","皇帝宝座的华丽装饰。","名词短语","装饰","宝座在中央",True,False),
    ("voice_corrected","宝座在中央。","陈述句","在中央","宝座在中央",True,True),
    ("voice_corrected","宝座在中央","陈述句","在中央","宝座在中央",False,False),
    ("voice_corrected","宝座象征权力。","陈述句","象征权力","无法核实",True,False),
    ("voice_corrected","宝座的装饰。","陈述句","呈金色","宝座在中央",True,False),
    ("before","宝座在什么位置？","问句","什么位置","宝座在中央",True,True),
    ("before","宝座在中央？","陈述句","在中央","宝座在中央",True,False),
    ("after","中央的宝座","名词短语","宝座","宝座在中央",True,True),
])
async def test_three_edition_measurement_separates_form_punctuation_and_grounding(
        phase, title, form, core, observation, punctuation, passed):
    from evaluations.owner_memory_video import measure_edition

    async def read(messages, schema):
        packet = json.loads(messages[-1]["content"])
        assert packet == {"title_words":title.rstrip("。？"), "observations":["宝座在中央"]}
        return {"form":form, "core":core, "supported":True, "observation":observation}

    report = await measure_edition([{"title":title,"primary_frame":"frame"}], {"frame":{"details":["宝座在中央"]}}, phase, read)
    assert report["items"][0]["punctuation_matches"] is punctuation
    assert report["passed"] is passed


async def test_editor_selects_a_sentence_against_one_observation_for_the_original_frame_review():
    from tuber_studio.agent.title_form import grounded_options

    fact = "白色台灯位于书桌右侧"
    async def call(messages, schema):
        data = json.loads(messages[-1]["content"])
        if "verified_observation" in data:
            assert data["verified_observation"] == fact
            return {"titles":["书桌右侧的白色台灯。","台灯在书桌右侧。","书桌右侧有一盏白色台灯。"]}
        assert data["observations"] == [fact] and "writing_preference" not in data
        phrase = data["title_words"].endswith("台灯")
        return {"form":"名词短语" if phrase else "陈述句", "core":"台灯" if phrase else "在书桌右侧",
                "observation":fact, "supported":True}
    review = await grounded_options(fact, "标题用陈述句，以句号结尾。", "陈述句",
                                    [FormatCheck(field="title", operation="ends_with",value="。")], call)
    assert review["chosen"] == "台灯在书桌右侧。"
    assert review["candidates"][0]["reading"]["form"] == "名词短语"
    assert review["candidates"][1]["reading"]["grounded"]
