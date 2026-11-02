"""Model aliases are local presentation, never durable evidence identities."""

import copy
import json

import httpx
import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet

from tuber_studio.browsing_inputs import (
    ContentDecisionInput,
    bind_content_decision,
    format_content_input,
)
from tuber_studio.browsing_models import ContentDecision
from tuber_studio.providers import LlamaCppProvider


def test_prepared_intent_combination_has_specific_field_feedback():
    value = {
        "interest": "like",
        "worth_commenting": True,
        "reason": "specific_detail",
        "evidence_ids": ["e1"],
        "coverage_required": "provided",
    }
    assert ContentDecision.model_validate(value).worth_commenting
    with pytest.raises(ValueError, match="字段组合不一致") as failure:
        ContentDecision.model_validate(value | {"reason": "unsafe_interaction"})
    for field in (
        "interest=like",
        "worth_commenting=true",
        "coverage_required=provided",
        "reason=unsafe_interaction",
    ):
        assert field in str(failure.value)


@pytest.mark.parametrize(
    "choice",
    [
        {
            "interest": "skip",
            "worth_commenting": False,
            "reason": "no_interest",
            "coverage_required": "provided",
        },
        {
            "interest": "like",
            "worth_commenting": False,
            "reason": "nothing_to_add",
            "coverage_required": "provided",
        },
        {
            "interest": "need_more",
            "worth_commenting": False,
            "reason": "visual_required",
            "coverage_required": "visual",
        },
    ],
)
def test_other_participation_choices_remain_available(choice):
    assert ContentDecision.model_validate(choice).model_dump() == choice | {"evidence_ids": []}


def material():
    return {
        "metadata": {
            "bvid": "BV1d8pU6xEZX",
            "title": "多P实验",
            "parts": [{"cid": "101", "title": "原理", "page": 1}, {"cid": "202", "title": "演示", "page": 2}],
        },
        "coverage": "sampled_visual",
        "missing_parts": [{"cid": "303", "reason": "permission"}],
        "parts": [
            {
                "cid": "101",
                "language": "zh-CN",
                "track_id": "human-track",
                "is_ai": False,
                "segments": [
                    {"id": "101:human:0", "start": 0, "end": 3.2, "text": "第一段\n原文与空白  全部保留。"},
                    {"id": "101:human:1", "start": 3.2, "end": 6, "text": "第二段。"},
                ],
            },
            {
                "cid": "202",
                "is_ai": True,
                "language": "ai-zh",
                "segments": [{"id": "202:ai:0", "start": 1.1, "end": 5, "text": "另一P的字幕。"}],
            },
            {
                "cid": "202",
                "visual": True,
                "visual_provider": {"provider_id": "current-model", "version": "2"},
                "segments": [{"id": "frame:202:actual", "start": 4, "end": 4, "text": "手拿着透明杯。"}],
            },
        ],
    }


def segments(value):
    return [dict(segment, cid=part["cid"]) for part in value["parts"] for segment in part["segments"]]


def decision(refs):
    return ContentDecision(
        interest="like",
        worth_commenting=True,
        reason="specific_detail",
        evidence_ids=refs,
    )


def test_compact_packet_keeps_every_text_time_part_track_and_current_visual_source():
    value = material()
    snapshot = copy.deepcopy(value)
    chunk = segments(value)
    original_chunk = copy.deepcopy(chunk)
    data, aliases = ContentDecisionInput(value).present(chunk)
    assert data["metadata"] == {"title": "多P实验"}
    assert data["missing_parts"] == [{"page": None, "title": "", "reason": "permission"}]
    presentation = ContentDecisionInput(value)
    assert presentation.missing_parts == value["missing_parts"]
    assert data["coverage"] == value["coverage"]
    assert data["segments"] == [{"id": "e1", "part": 1}, {"id": "e2", "part": 2}, {"id": "e3", "part": 3}]
    bound = [source for block in aliases.values() for source in block.sources]
    assert [source.text for source in bound] == [s["text"] for s in chunk]
    assert [(source.part.index, source.start, source.end) for source in bound] == [
        (1, 0, 3.2),
        (1, 3.2, 6),
        (2, 1.1, 5),
        (3, 4, 4),
    ]
    assert [(source.part.cid, source.part.page, source.part.title) for source in bound] == [
        ("101", 1, "原理"),
        ("101", 1, "原理"),
        ("202", 2, "演示"),
        ("202", 2, "演示"),
    ]
    assert aliases["e1"].part.metadata()["track_id"] == "human-track"
    assert data["parts"][0]["is_ai"] is False
    assert data["parts"][1]["is_ai"] is True
    assert aliases["e3"].part.metadata()["visual_provider"] == {
        "provider_id": "current-model",
        "version": "2",
    }
    assert data["source_kinds"] == ["subtitle", "visual_observation"] and data["has_visual"] is True
    assert [source.id for source in bound] == [s["id"] for s in chunk]
    assert data["content"].count("视频第2部分（P2） 演示") == 1
    assert "[e1|00:00]第一段\n原文与空白  全部保留。 第二段。" in data["content"]
    assert "[e3|00:04]手拿着透明杯。" in data["content"]
    assert all(s["text"] in format_content_input(data) for s in chunk)
    assert value == snapshot and chunk == original_chunk
    data["metadata"]["title"] = "presentation edit"
    aliases["e3"].part.metadata()["visual_provider"]["version"] = "presentation edit"
    assert value == snapshot


def test_chunk_local_aliases_bind_to_different_originals_and_reject_other_chunk_refs():
    value = material()
    presentation = ContentDecisionInput(value)
    first, first_refs = presentation.present(segments(value)[:2])
    second, second_refs = presentation.present(segments(value)[2:3])
    assert first["segments"][0]["id"] == second["segments"][0]["id"] == "e1"
    assert bind_content_decision(decision(["e1"]), first_refs).evidence_ids == ["101:human:0", "101:human:1"]
    assert bind_content_decision(decision(["e1"]), second_refs).evidence_ids == ["202:ai:0"]
    assert second["segments"][0]["part"] == 2
    assert second["source_kinds"] == ["subtitle"] and second["has_visual"] is False
    for ref in ["e2", "e999", "101:human:0", "202:ai:0", "E1"]:
        with pytest.raises(ValueError, match="引用"):
            bind_content_decision(decision([ref]), second_refs)
    with pytest.raises(ValueError, match="为空"):
        bind_content_decision(decision([]), second_refs)


async def test_native_http_schema_exposes_only_short_refs_and_database_keeps_real_ids(browsing):
    bodies = []

    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096}})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "mock rendered template"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(32))})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        bodies.append(body)
        data = model_packet(body["messages"][-1]["content"])
        if body["response_format"]["schema"]["title"] == "ContentDecision":
            result = decision(["e1"]).model_dump()
        else:
            result = {
                "action": "reply_public",
                "text": "盐水托起鸡蛋，好有意思！",
                "reason": "specific_detail",
                "evidence_ids": [data["evidence"][0]["id"]],
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)}}
                ],
                "usage": {"prompt_tokens": 32, "completion_tokens": 20},
            },
        )

    native = LlamaCppProvider(
        {"base_url": "http://offline.invalid", "context_tokens": 4096, "enable_thinking": True}
    )
    await native.client.aclose()
    native.client = httpx.AsyncClient(
        base_url="http://offline.invalid", transport=httpx.MockTransport(handle)
    )
    controlled = browsing.runtime.llm
    browsing.runtime.llm = native
    browsing.settings.data["llm"]["context_tokens"] = 4096
    try:
        row = await finished(browsing, preview=True)
        assert row["status"] == "completed"
        schema = bodies[0]["response_format"]["schema"]
        assert all(b["properties"]["evidence_ids"]["items"]["enum"] == ["e1"] for b in schema["oneOf"])
        packet = model_packet(bodies[0]["messages"][-1]["content"])
        assert packet["segments"][0] == {"id": "e1"}
        assert "[e1|00:00]加盐之后，鸡蛋浮了起来。" in packet["content"]
        assert "parts" not in packet
        assert bodies[0]["chat_template_kwargs"]["enable_thinking"] is True
        response_body = next(
            body for body in bodies if body["response_format"]["schema"]["title"] == "ResponseDecision"
        )
        assert all(
            branch["properties"]["evidence_ids"]["items"]["enum"] == ["r1"]
            for branch in response_body["response_format"]["schema"]["oneOf"]
        )
        response_user = response_body["messages"][-1]["content"]
        assert "[r1|" in response_user and "11:0" not in response_user
        assert browsing.tasks()[0]["decisions"][0]["evidence_ids"] == ["11:0"]
        assert browsing.intents()[0]["evidence"] == ["11:0"]
        assert browsing.intents()[0]["status"] == "pending_review" and not browsing.adapter.sent
    finally:
        browsing.runtime.llm = controlled
        await native.close()


@pytest.mark.parametrize("bad_ref", ["e99", "11:0"])
async def test_invalid_or_original_model_ref_is_checkpointed_without_being_adopted(browsing, bad_ref):
    async def generate(messages, schema, *, max_tokens):
        assert schema["title"] == "ContentDecision"
        return decision([bad_ref]).model_dump()

    browsing.runtime.llm.generate_json = generate
    row = await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert row["status"] == "partial" and task["status"] == "checkpoint"
    assert task["continuation"]["id"] and task["continuation"]["phase"] == "generation"
    episode = browsing.runtime.continuations.get(task["continuation"]["id"])
    assert episode["binding"]["schema"] == "ContentDecision"
    assert episode["original_output"] == decision([bad_ref]).model_dump()
    assert any(
        step["phase"] == "feedback"
        and "内容判断引用不存在或为空" in (step.get("feedback") or {}).get("message", "")
        for step in episode["revisions"]
    )
    assert task["decisions"] == []
    assert not browsing.intents() and not browsing.adapter.sent


async def test_two_real_model_invocations_reuse_alias_but_keep_chunk_ownership(browsing):
    value = material()
    chunks = [segments(value)[:2], segments(value)[2:]]

    async def split(material, row):
        return chunks

    browsing._chunks = split
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    decisions = await browsing._content_decisions(value, row, {"lock_wait_seconds": 0})
    assert [d.evidence_ids for d in decisions] == [["101:human:0", "101:human:1"], ["202:ai:0"]]
    calls = [data for kind, data in browsing.runtime.llm.calls if kind == "ContentDecision"]
    assert [data["segments"][0]["id"] for data in calls] == ["e1", "e1"]
    assert row["config"]["max_model_calls"] >= 2


async def test_one_group_expands_to_twelve_real_segments_through_sqlite_intent(browsing):
    async def subtitles(bvid, cid, languages):
        return {
            "bvid": bvid,
            "cid": cid,
            "track_id": "complete-track-v1",
            "content_hash": "unchanged-subtitle-version",
            "language": "zh-CN",
            "is_ai": False,
            "segments": [{"id": str(i), "start": i, "end": i + 1, "text": "盐水啊"} for i in range(12)],
            "coverage": {"complete": True},
        }

    browsing.adapter.subtitles = subtitles
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed"
    expected = [f"11:{i}" for i in range(12)]
    task = browsing.tasks()[0]
    assert task["decisions"][0]["evidence_ids"] == expected
    assert set(browsing.intents()[0]["evidence"]) == set(expected)
    assert browsing.intents()[0]["status"] == "pending_review"
    cached = browsing.evidence.load(browsing.intents()[0]["material_keys"][0])
    assert [segment["id"] for segment in cached["parts"][0]["segments"]] == expected
    assert cached["parts"][0]["content_hash"] == "unchanged-subtitle-version"
    model_calls = [packet for kind, packet in browsing.runtime.llm.calls if kind == "ContentDecision"]
    assert model_calls[0]["segments"] == [{"id": "e1"}]
    assert not browsing.adapter.sent


@pytest.mark.parametrize("correction", ["prepare", "skip"])
async def test_inconsistent_intent_reaches_generation_feedback_and_can_change_choice(browsing, correction):
    bad = {
        "interest": "like",
        "worth_commenting": True,
        "reason": "unsafe_interaction",
        "evidence_ids": ["e1"],
        "coverage_required": "provided",
    }
    corrected = (
        bad | {"reason": "specific_detail"}
        if correction == "prepare"
        else {
            "interest": "skip",
            "worth_commenting": False,
            "reason": "no_interest",
            "evidence_ids": [],
            "coverage_required": "provided",
        }
    )
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append((schema["title"], copy.deepcopy(messages)))
        if schema["title"] == "ContinuationDecision":
            return {"action": "revise", "reason": "重新选择一致的参与意图。", "read": ""}
        assert schema["title"] == "ContentDecision"
        return copy.deepcopy(bad if sum(kind == "ContentDecision" for kind, _ in calls) == 1 else corrected)

    browsing.runtime.llm.generate_json = generate
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    results = await browsing._content_decisions(material(), row, {"lock_wait_seconds": 0})
    assert [kind for kind, _ in calls] == ["ContentDecision", "ContinuationDecision", "ContentDecision"]
    identity = browsing.store.one(
        "SELECT id FROM operation_continuations WHERE channel='generation.browsing'"
    )["id"]
    episode = browsing.runtime.continuations.get(identity)
    assert episode["state"] == "completed" and episode["recoveries"] == 1
    assert episode["original_output"] == bad
    assert episode["current_output"] == corrected
    assert any("字段组合不一致" in str(step["feedback"]) for step in episode["revisions"])
    assert (
        results[0].interest == corrected["interest"]
        and results[0].worth_commenting == corrected["worth_commenting"]
    )
    assert not browsing.adapter.sent
