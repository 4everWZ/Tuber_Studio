"""Readable CC groups retain exact, invocation-bound original evidence."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from tuber_studio.browsing_inputs import (
    ContentDecisionInput,
    ResolvedContentDecision,
    bind_content_decision,
    format_content_input,
)
from tuber_studio.browsing_models import ContentDecision, content_decision_schema
from tuber_studio.subtitle_presentation import timestamp


def material(texts, *, track="human-track-v1", cid="101"):
    return {
        "metadata": {
            "bvid": "BVprivate",
            "aid": "9988",
            "owner_uid": "777",
            "title": "实验",
            "parts": [{"cid": cid, "page": 1, "title": "原理"}],
        },
        "parts": [
            {
                "cid": cid,
                "track_id": track,
                "content_hash": "source-version-1",
                "language": "zh-CN",
                "is_ai": False,
                "coverage": {"start": 0, "end": len(texts)},
                "segments": [
                    {"id": f"{cid}:{track}:{i}", "text": text, "start": i, "end": i + 1}
                    for i, text in enumerate(texts)
                ],
            }
        ],
        "coverage": "all_available_cc",
        "missing_parts": [],
    }


def segments(value):
    return [dict(segment, cid=part["cid"]) for part in value["parts"] for segment in part["segments"]]


def decision(ids):
    return ContentDecision(interest="like", worth_commenting=True, reason="specific_detail", evidence_ids=ids)


def test_filler_repeated_text_english_whitespace_and_originals_survive_grouping():
    value = material(["啊", "笑一下", "hello", " world", "\n  \t", "又笑了。", "又笑了。"])
    snapshot = deepcopy(value)
    presenter = ContentDecisionInput(value)
    data, aliases = presenter.present(segments(value))
    assert len(aliases) == 1
    for text in ["啊", "笑一下", "hello", " world"]:
        assert text in data["content"]
    assert data["content"].count("又笑了。") == 2
    bound = bind_content_decision(decision(["e1"]), aliases)
    assert [source.text for source in bound.sources] == [s["text"] for s in segments(value)]
    assert bound.evidence_ids == [s["id"] for s in segments(value)]
    assert value == snapshot


def test_one_model_selection_can_resolve_thirty_original_segments_without_bypassing_wire_cap():
    value = material(["啊"] * 30)
    _, aliases = ContentDecisionInput(value).present(segments(value))
    resolved = bind_content_decision(decision(["e1"]), aliases)
    assert isinstance(resolved, ResolvedContentDecision)
    assert resolved.wire_decision.evidence_ids == ["e1"]
    assert len(resolved.evidence_ids) == 30
    assert resolved.model_dump()["evidence_ids"] == [s["id"] for s in segments(value)]
    assert resolved.sources[0].part.cid == "101"
    assert resolved.sources[0].part.metadata()["content_hash"] == "source-version-1"
    # Internal expanded evidence is intentionally not a new model-output schema.
    with pytest.raises(ValidationError):
        ContentDecision.model_validate(resolved.model_dump())
    with pytest.raises(ValidationError):
        bind_content_decision(decision(["e1"]).model_copy(update={"evidence_ids": ["e1"] * 9}), aliases)
    assert all(
        branch["properties"]["evidence_ids"]["maxItems"] == 8
        for branch in content_decision_schema(set(aliases))["oneOf"]
    )


def test_track_versions_and_visual_frames_never_join_even_with_identical_text():
    value = material(["一样的内容"])
    second = deepcopy(value["parts"][0])
    second.update(track_id="automatic-v2", is_ai=True, content_hash="source-version-2")
    second["segments"][0]["id"] = "101:automatic-v2:0"
    visual = {
        "cid": "101",
        "visual": True,
        "visual_provider": {"provider_id": "vision", "version": "v3"},
        "segments": [
            {"id": "frame:101:0", "start": 0, "end": 0, "text": "一样的内容"},
            {"id": "frame:101:1", "start": 1, "end": 1, "text": "一样的内容"},
        ],
    }
    value["parts"].extend([second, visual])
    data, aliases = ContentDecisionInput(value).present(segments(value))
    assert len(aliases) == 4 and data["content"].count("视频第1部分（P1） 原理") == 1
    assert aliases["e1"].part.metadata()["track_id"] == "human-track-v1"
    assert aliases["e2"].part.metadata()["content_hash"] == "source-version-2"
    assert aliases["e3"].part.metadata()["visual_provider"] == {"provider_id": "vision", "version": "v3"}
    assert [source.id for block in aliases.values() for source in block.sources] == [
        s["id"] for s in segments(value)
    ]


def test_pause_and_unread_source_gap_create_separate_blocks():
    value = material(["连续第一句", "连续第二句", "隔段第三句"])
    presenter = ContentDecisionInput(value)
    _, omitted = presenter.present([segments(value)[0], segments(value)[2]])
    assert len(omitted) == 2
    value["parts"][0]["segments"][2].update(start=20, end=21)
    _, paused = ContentDecisionInput(value).present(segments(value))
    assert [len(block.sources) for block in paused.values()] == [2, 1]


def test_long_single_segment_is_not_truncated_or_summarized():
    original = "完整字幕啊笑。" * 200
    value = material([original])
    data, aliases = ContentDecisionInput(value).present(segments(value))
    assert original in data["content"]
    assert aliases["e1"].sources[0].text == original


def test_many_short_segments_keep_every_original_without_repeating_transport_ids():
    value = material(
        [f"第{i}段，啊哈哈，这些词照原样保留" for i in range(192)], track="12345678901234567890123456789012"
    )
    original = segments(value)
    data, aliases = ContentDecisionInput(value).present(original)
    assert [source.id for block in aliases.values() for source in block.sources] == [
        source["id"] for source in original
    ]
    assert [source.text for block in aliases.values() for source in block.sources] == [
        source["text"] for source in original
    ]
    assert len(aliases) < len(original)
    compact = format_content_input(data)
    legacy = json.dumps(original, ensure_ascii=False)
    assert len(compact.encode()) < len(legacy.encode()) * 0.6
    assert "12345678901234567890123456789012" not in compact


@pytest.mark.parametrize(
    "change",
    [
        {"cid": "202"},
        {"id": "other:guid"},
        {"text": "改过的原文"},
        {"start": 0.1},
        {"end": 0.9},
        {"start": False},
    ],
)
def test_foreign_or_changed_source_fails_before_rendering(change):
    value = material(["原文"])
    presenter = ContentDecisionInput(value)
    chunk = segments(value)
    chunk[0].update(change)
    with pytest.raises(ValueError):
        presenter.present(chunk)


def test_reordered_duplicate_sources_and_ambiguous_global_ids_are_rejected():
    value = material(["同一文本", "同一文本"])
    presenter = ContentDecisionInput(value)
    chunk = segments(value)
    for wrong in [chunk[::-1], [chunk[0], chunk[0]]]:
        with pytest.raises(ValueError):
            presenter.present(wrong)
    other = deepcopy(value["parts"][0])
    other["cid"] = "202"
    value["parts"].append(other)
    with pytest.raises(ValueError, match="身份"):
        ContentDecisionInput(value)


def test_model_text_has_one_reference_and_no_structured_ids_but_numeric_source_facts_remain():
    value = material(["CID 101 是片中原文，啊、笑都保留。"])
    data, aliases = ContentDecisionInput(value).present(segments(value))
    rendered = format_content_input(data)
    header, _, body = rendered.partition("\n\n内容：\n")
    assert "segments" not in json.loads(header) and "parts" not in json.loads(header)
    assert rendered.count("e1") == 1
    assert "human-track-v1" not in rendered and "9988" not in rendered and "777" not in rendered
    assert value["parts"][0]["segments"][0]["text"] in body
    assert aliases["e1"].part.metadata()["track_id"] == "human-track-v1"


def test_blank_only_source_kept_backend_and_timestamp_rounding_does_not_change_original_times():
    value = material(["\n  \t"])
    presenter = ContentDecisionInput(value)
    data, aliases = presenter.present(segments(value))
    assert not aliases and not data["segments"]
    assert len(presenter.sources) == 1
    value = material(["实际秒保留"])
    value["parts"][0]["segments"][0].update(start=59.999999, end=61.234567)
    data, aliases = ContentDecisionInput(value).present(segments(value))
    assert "[e1|01:00]" in data["content"]
    assert aliases["e1"].sources[0].start == 59.999999
    assert aliases["e1"].sources[0].end == 61.234567
    assert timestamp(3600) == "01:00:00"
