import json

import pytest

from tests.support.agent_helpers import BV, frame, reading, selection, short_read
from tuber_studio.agent.tasks.clip_refinement import positive_subwindows
from tuber_studio.agent.tasks.delivery import WINDOW_REVIEW_VERSION, AdditionalVideoRead, evidence_index
from tuber_studio.agent.tasks.tools import create_task_runtime


def rejected_read():
    step = short_read("parent", 0)
    step["result"]["part_duration"] = 3
    clip = evidence_index([step]).video_coverage["parent"]["clip"]
    samples = [{"frame": i, "seconds": i / 25} for i in (0, 5, 12, 30, 50, 74)]
    rows = [p | {"scene": "符合要求", "captions": "已读且符合", "screen_text": "节目片头", "basis": "栏目标题"}
            for p in samples]
    for i in (0, 4, 5):
        rows[i].update(captions="无法读清", screen_text="", basis="屏字不清")
    return step, {"screening_version": WINDOW_REVIEW_VERSION, "clip": clip, "accepted": False,
                  "coverage": {"scanned_frames": 75, "complete": True, "samples": samples, "uncovered": []},
                  "observations": rows}


def test_positive_run_is_not_a_clip_receipt_and_only_exact_new_media_can_reenter():
    parent, rejected = rejected_read()
    child = reading(frame(0.4), frame(0.8))
    child.update(id="child", arguments={"part": 2, "bvid": BV, "start": 0.2, "end": 1.2})
    child["result"].update(clip=rejected["clip"] | {"start": 0.2, "end": 1.2, "frames": 25},
                           selection={"bvid": BV, "ranges": [{"part": 2, "start": 0.2, "end": 1.2}]})
    evidence = evidence_index([parent, child])
    proposals = positive_subwindows(evidence, {"parent": rejected})
    proposed = proposals["parent#5-30"]
    assert proposed["positive_frame_bounds"] == [5, 30] and proposed["window"] == child["arguments"]
    assert [r["from"] for r in proposed["parent_objections"]] == [0, 2]
    assert set(evidence.unused_video_material(["parent:1"], refinements=proposals.values()).points) == {"child:1", "child:2"}
    assert not evidence.unused_video_material(["parent:1"]).points
    del child["result"]["clip"]
    assert not evidence_index([parent, child]).unused_video_material(["parent:1"], refinements=proposals.values()).points
    rejected["coverage"]["complete"] = False
    assert not positive_subwindows(evidence, {"parent": rejected})


@pytest.mark.parametrize("fixed", [None, 3])
async def test_editor_requests_new_subclip_with_objections_or_respects_fixed_length(runtime, store, monkeypatch, fixed):
    parent, rejected = rejected_read()
    task = {"goal": f"从 {BV} P2 选一段", "steps": [parent], "plan": {
        "prior_goals": [], "candidate": selection(primary="parent:1").model_dump(),
        "window_reviews": {"parent": rejected}}}
    evidence = evidence_index([parent])

    async def model(messages, schema, **kwargs):
        props = schema["properties"]
        if "requested_count" in props:
            return {"requested_count": 1, "usage": "展示制作", "playable": True, "trace_origin": False, "clip_seconds": fixed}
        if "decision" in props:
            return {"decision": "另取材料", "primary_frame": None, "rationale": "整段不合格"}
        if "samples" in props:
            packet = json.loads(messages[-1]["content"])
            assert fixed is None and packet["untried_observations"]["parent#5-30"]["parent_read"] == "parent"
            assert packet["parent_objections"]["parent"][0]["basis"] == "屏字不清"
            return {"samples": {"parent#5-30": {"priority": "优先补读", "material": "节目引介"}}, "first": "parent#5-30"}
        pytest.fail(str(props))

    monkeypatch.setattr(runtime.llm, "generate_json", model)

    monkeypatch.setattr(runtime.operation_llm, "generate_json", model)
    editor = create_task_runtime(runtime, store, None).edit_delivery
    if fixed is None:
        with pytest.raises(AdditionalVideoRead) as request:
            await editor(task, evidence)
        assert request.value.windows == [{"bvid": BV, "part": 2, "start": 0.2, "end": 1.2}]
    else:
        result = await editor(task, evidence)
        assert not result.items and result.gaps
