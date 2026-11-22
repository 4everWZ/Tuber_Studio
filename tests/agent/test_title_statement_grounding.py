import json
from types import SimpleNamespace

import pytest

from evaluations.memory.owner_memory_video import measure_edition
from tuber_studio.agent.tasks.title_form import grounded_options
from tuber_studio.core.identity import captured_identity, provider_identity_messages


@pytest.mark.parametrize("omit_place", [False, True])
async def test_replay_cannot_credit_an_unobserved_setting_or_ignore_that_part_of_the_title(omit_place):
    fact = "华丽的金色雕花宝座"
    title = fact + "位于直播间中央。"
    async def reader(messages, schema):
        data = json.loads(messages[-1]["content"])
        if "title_words" in data:
            return {"form":"陈述句", "core":"位于直播间中央", "supported":True, "observation":fact, "answer":""}
        assert data == {"statement":title[:-1], "frame_observation":fact}
        parts = [("实体",fact,fact), ("关系","位于",fact)]
        if not omit_place:
            parts.append(("场所","直播间中央",fact))
        return {"same_relations":True, "parts":[{"role":r, "words":w, "basis":b, "entailed":True} for r,w,b in parts]}
    result = await measure_edition([{"title":title, "primary_frame":"p"}], {"p":{"details":[fact]}}, "voice_corrected", reader)
    row, = result["items"]
    assert row["form_matches"] and row["punctuation_matches"] and not result["passed"]
    proof = row["reading"]["statement_basis"]
    assert proof["covers_statement"] != omit_place
    assert not row["reading"]["grounded"]


async def test_candidate_can_select_a_complete_statement_instead_of_the_first_nominal_detail():
    details = ["装饰图案", "白色器皿摆在桌子右侧"]
    statement = details[1] + "。"
    provider = SimpleNamespace(identity_resolver=lambda: {"identity_version":"v1", "character_name":"测试编辑"})
    async def editor(messages, schema):
        assert captured_identity(provider_identity_messages(provider, messages)) is None
        data = json.loads(messages[-1]["content"])
        if "observations_by_id" in data:
            assert list(data["observations_by_id"].values()) == details
            return {"0":"", "1":"摆在桌子右侧"}
        if "frame_details" in data:
            assert schema["properties"]["fact"]["enum"] == data["frame_details"] == [details[1]]
            assert set(data) == {"frame_details","title_form","title_format"}
            return {"fact":details[1],"options":[{"title":t,"evidence_span":"白色器皿"} for t in
                                                ("白色器皿在摄影棚内。",statement)]}
        assert data == {"title_words":details[1], "observations":[details[1]]}
        return {"form":"陈述句", "core":"摆在桌子右侧", "supported":True, "observation":details[1], "answer":""}
    result = await grounded_options(details, "陈述句", [], editor)
    assert result["chosen"] == statement and result["fact"] == details[1]
    assert captured_identity(provider_identity_messages(provider, []))["character_name"] == "测试编辑"
