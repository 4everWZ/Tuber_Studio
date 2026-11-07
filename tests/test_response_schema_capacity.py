"""Long transcript source sets stay within the broker schema budget."""

import pytest

from tuber_studio.inference_validation import validate_llm_request
from tuber_studio.operations.browsing.inputs import ResponseDecisionInput
from tuber_studio.operations.browsing.models import response_schema
from tuber_studio.web_fetch import OperationWebFetch


def test_new_private_decisions_offer_invited_contact_only_while_old_audits_remain_readable():
    from tuber_studio.operations.browsing.models import ResponseDecision

    schema = response_schema({"r1"}, allow_handoff=True)
    private = next(branch for branch in schema["oneOf"] if branch["properties"]["action"]["enum"] == ["handoff_private"])
    assert private["properties"]["reason"]["enum"] == ["user_requested"]
    assert private["properties"]["contact_trigger"]["enum"] == ["invitation", "agreement"]
    historical = ResponseDecision(action="handoff_private", reason="long_followup", text="旧审计正文", evidence_ids=["r1"])
    assert historical.reason == "long_followup"


@pytest.mark.asyncio
async def test_long_video_keeps_all_sources_and_actions_through_optional_web_fetch():
    # The real failed video had three CC tracks with these segment counts.
    evidence = [
        {"id": f"{part}:{index}", "text": f"字幕片段 {part} {index}"}
        for part, count in enumerate((251, 233, 207), 1)
        for index in range(count)
    ]
    response_input = ResponseDecisionInput({"evidence": evidence})
    aliases = [reference.alias for reference in response_input.references]
    assert len(aliases) == 691
    schema = response_schema(aliases, allow_handoff=False, video_actions=True)
    candidate = {
        "action": "reply_public",
        "reason": "specific_detail",
        "explanation": "就这段字幕里的细节接话",
        "text": "这段对白有点意思。",
        "evidence_ids": aliases[-8:],
        "video_preference": "neutral",
        "preference_reason": "有可以接话的细节",
    }
    captured = []

    async def invoke(messages, wire_schema):
        captured.append(wire_schema)
        validate_llm_request(
            "v1/chat/completions",
            {
                "messages": messages,
                "response_format": {"type": "json_object", "schema": wire_schema},
                "dry_multiplier": 0.8,
                "dry_allowed_length": 5,
                "dry_penalty_last_n": 512,
            },
        )
        return candidate

    result = await OperationWebFetch(lambda: None).complete([], schema, invoke)
    assert result == candidate
    assert len(captured) == 1
    wire_schema = captured[0]
    assert wire_schema["$defs"] == schema["$defs"]
    assert set(wire_schema["$defs"]["evidence_id"]["enum"]) == set(aliases)
    actions = set()
    for branch in wire_schema["oneOf"]:
        properties = branch["properties"]
        if "web_fetch" in properties:
            continue
        action = properties["action"]["enum"][0]
        actions.add(action)
        references = properties["evidence_ids"]
        assert references["items"] == {"$ref": "#/$defs/evidence_id"}
        assert references["maxItems"] == 8
        assert references["minItems"] == (1 if action == "reply_public" else 0)
    assert actions == {"reply_public", "skip", "wait", "read_comments", "watch_frames", "read_more", "finish"}
    assert response_input.resolve(candidate).evidence_ids == [segment["id"] for segment in evidence[-8:]]
    with pytest.raises(ValueError, match="本次短编号"):
        response_input.validate_wire(candidate | {"evidence_ids": ["ref:692"]})
    with pytest.raises(ValueError):
        response_input.validate_wire(candidate | {"evidence_ids": aliases[-9:]})
