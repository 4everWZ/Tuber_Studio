"""Long transcript source sets retain compact references through tool selection."""

import pytest

from tests.support.http_isolation import validate_schema_complexity
from tuber_studio.infra.web_client.fetch import MAX_URL, OperationWebFetch
from tuber_studio.operations.kernel.inputs import ResponseDecisionInput
from tuber_studio.operations.kernel.models import response_schema


def test_new_private_decisions_offer_invited_contact_only_while_old_audits_remain_readable():
    from tuber_studio.operations.kernel.models import ResponseDecision

    schema = response_schema({"r1"})
    assert all(branch["properties"]["action"]["enum"] != ["handoff_private"] for branch in schema["oneOf"])
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
    assert len(response_input.references) == 691
    assert all(ref.alias == ref.canonical_id for ref in response_input.references)
    assert len(response_input.allowed_aliases) == 691
    assert "[ref:" not in response_input.format()
    schema = response_schema((), video_actions=True)
    candidate = {
        "action": "reply_public",
        "reason": "specific_detail",
        "explanation": "就这段字幕里的细节接话",
        "text": "这段对白有点意思。",
        "video_preference": "neutral",
        "preference_reason": "有可以接话的细节",
    }
    captured = []

    async def invoke(messages, wire_schema):
        validate_schema_complexity(wire_schema)
        captured.append(wire_schema)
        return candidate

    result = await OperationWebFetch(lambda: None).complete([], schema, invoke)
    assert result == candidate
    assert len(captured) == 1
    wire_schema = captured[0]
    assert "$defs" not in wire_schema
    actions = set()
    for branch in wire_schema["oneOf"]:
        properties = branch["properties"]
        if "web_fetch" in properties:
            assert properties["web_fetch"]["maxLength"] == MAX_URL
            assert "pattern" not in properties["web_fetch"]
            continue
        action = properties["action"]["enum"][0]
        actions.add(action)
        assert "evidence_ids" not in properties
    assert actions == {"reply_public", "skip", "wait", "read_comments", "watch_frames", "read_more", "finish"}
    resolved = response_input.resolve(candidate, provenance=[s["id"] for s in evidence])
    assert resolved.evidence_ids == [segment["id"] for segment in evidence]
    assert len(resolved.evidence_ids) == 691
