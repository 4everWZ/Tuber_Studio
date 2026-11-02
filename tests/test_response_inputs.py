"""One-to-one response aliases preserve original evidence and recovery boundaries."""

import copy
import json

import pytest
from test_browsing import browsing as browsing

from tuber_studio.browsing import generation_resume
from tuber_studio.browsing_inputs import ResponseDecisionInput
from tuber_studio.browsing_models import RESPONSE_INSTRUCTION, ResponseDecision


def packet(count=2):
    return {
        "kind": "reply",
        "evidence": [
            {
                "id": f"canonical-track:segment:{i}",
                "cid": "real-cid",
                "start": i + 0.123456789,
                "end": i + 0.987654321,
                "text": f"第{i}段原文\n  空白也保留。",
            }
            for i in range(count)
        ],
        "thread": {
            "source": {"author_uid": "200", "text": "对方完整的公开问题。"},
            "items": [{"id": "source-comment", "text": "完整的楼内文字，带数字12345。"}],
        },
        "recent_public_responses": [{"text": "完整的近期回应。"}],
        "coverage": "all_available_cc",
    }


def reply(refs):
    return ResponseDecision(
        action="reply_public",
        reason="specific_detail",
        text="鱼鱼接住这个话题，顺着往下聊。" * 50,
        evidence_ids=refs,
    )


def test_response_presentation_keeps_all_text_thread_recent_and_exact_times():
    data = packet()
    original = copy.deepcopy(data)
    view = ResponseDecisionInput(data)
    header, body = view.format().split("\n\n所选材料：\n", 1)
    assert json.loads(header) == {
        k: v for k, v in original.items() if k not in {"evidence", "allowed_evidence_ids"}
    }
    for segment in original["evidence"]:
        assert segment["text"] in body
        assert segment["id"] not in body and segment["cid"] not in body
        assert str(segment["start"]) in body and str(segment["end"]) in body
    assert body.count("材料1") == 1 and len(view.references) == 2
    assert view.references[0].source() == original["evidence"][0]
    fresh = view.references[0].source()
    fresh["text"] = "mutated copy"
    data["evidence"][0]["id"] = "mutated original"
    data["thread"]["source"]["text"] = "mutated thread"
    assert view.references[0].source() == original["evidence"][0]
    assert json.loads(view.format().split("\n\n所选材料：\n")[0])["thread"] == original["thread"]
    bound = view.resolve(reply(["r1", "r2"]))
    assert bound.evidence_ids == [s["id"] for s in original["evidence"]]
    assert bound.text == reply(["r1"]).text


def test_one_to_one_29_segments_still_allow_only_eight_original_references():
    data = packet(29)
    view = ResponseDecisionInput(data)
    assert len(view.references) == 29
    result = view.resolve(reply([f"r{i}" for i in range(1, 9)]))
    assert len(result.evidence_ids) == 8 and result.evidence_ids == [s["id"] for s in data["evidence"][:8]]
    invalid = reply(["r1"]).model_copy(update={"evidence_ids": [f"r{i}" for i in range(1, 10)]})
    with pytest.raises(ValueError):
        view.resolve(invalid)
    with pytest.raises(ValueError, match="短编号"):
        view.resolve(reply(["r30"]))


def test_aliases_do_not_collide_with_original_ids_and_are_invocation_local():
    first = packet()
    first["evidence"][0]["id"] = "r1"
    view = ResponseDecisionInput(first)
    assert view.references[0].alias == "r2"
    for bad in ["r1", first["evidence"][1]["id"], "unknown"]:
        with pytest.raises(ValueError, match="短编号"):
            view.resolve(reply([bad]))
    another = ResponseDecisionInput(packet())
    assert another.resolve(reply(["r1"])).evidence_ids == ["canonical-track:segment:0"]
    assert view.resolve(reply(["r2"])).evidence_ids == ["r1"]


def test_public_text_reference_is_bound_without_inventing_video_segments():
    data = {
        "kind": "reply",
        "evidence": [],
        "thread": {"source": {"text": "完整文字动态", "author_uid": "200"}},
        "allowed_evidence_ids": ["public:123"],
        "recent_public_responses": [],
        "coverage": "public_text",
    }
    view = ResponseDecisionInput(data)
    assert "[r1] 当前公开来源" in view.format()
    assert "public:123" not in view.format()
    assert view.references[0].source() is None
    assert view.resolve(reply(["r1"])).evidence_ids == ["public:123"]
    skip = ResponseDecision(action="skip", reason="no_interest", evidence_ids=[])
    assert view.resolve(skip) == skip
    with pytest.raises(ValueError, match="为空"):
        view.resolve(reply([]))


@pytest.mark.parametrize("change", [{"start": -1}, {"end": False}, {"start": 9, "end": 1}, {"text": None}])
def test_invalid_source_records_are_rejected_before_model_generation(change):
    data = packet()
    data["evidence"][0].update(change)
    with pytest.raises(ValueError):
        ResponseDecisionInput(data)


async def prepared_row(browsing):
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    return row


async def test_wire_error_is_recoverable_and_normalization_happens_only_at_final_boundary(browsing):
    row = await prepared_row(browsing)
    data = packet(1)
    calls = []
    bad = reply([data["evidence"][0]["id"]]).model_dump()
    wire = reply(["r1"]).model_dump()

    async def generate(messages, schema, *, max_tokens):
        calls.append((schema["title"], copy.deepcopy(messages), copy.deepcopy(schema)))
        if schema["title"] == "ContinuationDecision":
            return {"action": "revise", "reason": "重新选择本次材料短编号。", "read": ""}
        return bad if sum(title == "ResponseDecision" for title, _, _ in calls) == 1 else wire

    browsing.runtime.llm.generate_json = generate
    result = await browsing._model(
        row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
    )
    assert result.evidence_ids == [data["evidence"][0]["id"]]
    assert [title for title, _, _ in calls] == [
        "ResponseDecision",
        "ContinuationDecision",
        "ResponseDecision",
    ]
    for title, messages, schema in calls:
        if title == "ResponseDecision":
            assert (
                "[r1|" in messages[2]["content"] and data["evidence"][0]["id"] not in messages[2]["content"]
            )
            assert all(b["properties"]["evidence_ids"]["items"]["enum"] == ["r1"] for b in schema["oneOf"])
    saved = browsing.store.one("SELECT id FROM operation_continuations WHERE channel='generation.browsing'")
    episode = browsing.runtime.continuations.get(saved["id"])
    assert episode["binding"]["input_format"] == ResponseDecisionInput.FORMAT_VERSION
    assert episode["original_output"] == bad and episode["current_output"] == wire
    assert any("回应引用不属于本次短编号" in str(step["feedback"]) for step in episode["revisions"])
    before = len(calls)
    token = generation_resume.set(True)
    try:
        again = await browsing._model(
            row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
        )
    finally:
        generation_resume.reset(token)
    assert len(calls) == before and again == result
    assert not browsing.adapter.sent and not browsing.intents()


async def test_format_version_change_does_not_reuse_previous_wire_checkpoint(browsing, monkeypatch):
    row = await prepared_row(browsing)
    data = packet(1)
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        return reply(["r1"]).model_dump()

    browsing.runtime.llm.generate_json = generate
    first = await browsing._model(row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0})
    first_capacity = browsing._capacity_signature(row["config"])
    monkeypatch.setattr(ResponseDecisionInput, "FORMAT_VERSION", "test-response-v2")
    assert browsing._capacity_signature(row["config"]) != first_capacity
    token = generation_resume.set(True)
    try:
        second = await browsing._model(
            row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
        )
    finally:
        generation_resume.reset(token)
    assert calls == ["ResponseDecision", "ResponseDecision"] and first == second
    episodes = [
        browsing.runtime.continuations.get(r["id"])
        for r in browsing.store.all(
            "SELECT id FROM operation_continuations WHERE channel='generation.browsing'"
        )
    ]
    assert {r["binding"]["input_format"] for r in episodes} == {
        "response-reference-lines-v1",
        "test-response-v2",
    }
    assert all(r["current_output"]["evidence_ids"] == ["r1"] for r in episodes)
    assert not browsing.adapter.sent
