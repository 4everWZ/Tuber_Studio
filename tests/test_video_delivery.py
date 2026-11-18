import json
from uuid import uuid4

import pytest
from test_agent_creative_delegation import call, gateway

from tuber_studio.agent.task_delivery import (
    DelegationError,
    PrivateDelivery,
    evidence_index,
    render_private_delivery,
)
from tuber_studio.agent.task_tools import create_task_runtime, sampled_video_findings

BV = "BV1234567890"


def frame(seconds, *, part=2, cid="part-two", bvid=BV):
    return {"part": part, "cid": cid, "seconds": seconds, "kind": "画面", "text": "左侧有一把椅子",
            "url": f"https://www.bilibili.com/video/{bvid}/?p={part}&t={int(seconds)}"}


def reading(*frames):
    return {"tool": "inspect_video", "id": "v", "revision": 1, "arguments": {"bvid": BV, "part": 2},
            "result": {"status": "partial", "findings": list(frames),
                       "selection": {"bvid": BV, "ranges": [{"part": 2, "start": 0, "end": 300}]}}}


def page(key):
    return {"id": key, "tool": "web_page", "revision": 1, "arguments": {},
            "result": {"url": f"https://example.org/{key}", "excerpt": "剧目资料"}}


def selection(*sources, primary=None):
    return PrivateDelivery.model_validate({"items": [{"title": "开场", "rationale": "我会用这个构图引出话题。",
                                                     "sources": sources, "primary_frame": primary}], "gaps": []})


@pytest.mark.parametrize("primary", [None, "v:1", "v:2"])
@pytest.mark.parametrize("seconds", [(11, 210), (210, 230)])
def test_distinct_points_in_requested_range_cannot_form_one_clip(primary, seconds):
    evidence = evidence_index([reading(*map(frame, seconds)), page("source")])
    with pytest.raises(DelegationError):
        render_private_delivery(selection("v:1", "v:2", "source", primary=primary), evidence)


def test_sampled_window_membership_comes_from_domain_samples():
    window = {"start": 10, "end": 13, "state": "sampled", "samples": [11.125, 12]}
    highlights = [{"start": f["seconds"], "source_url": f["url"], "text": f["text"], "kind": f["kind"]}
                  for f in map(frame, [11.125, 12, 12.5])]
    status = {"selection": {"bvid": BV}, "coverage": {"parts": [{"part": 2, "cid": "part-two", "ranges": [{
        "start": 0, "end": 300, "visual": {"windows": [window]}, "highlights": highlights}]}]}}
    evidence = evidence_index([reading(*sampled_video_findings(status, 2)), page("source")])
    _, body = render_private_delivery(selection("source", "v:2", "v:1", primary="v:1"), evidence)
    assert BV in body and "主帧：" + evidence["v:1"] in body
    assert "分 P 2 · 11.125 秒" in body and "?p=2&t=11" in body and "抽样窗 10–13 秒" in body
    assert evidence["v:2"] in body and evidence["source"] in body and "连续画面及音轨" in body
    with pytest.raises(DelegationError, match="同一已核实"):
        render_private_delivery(selection("v:1", "v:3", primary="v:1"), evidence)


@pytest.mark.parametrize("foreign", [frame(12, part=1), frame(12, cid="replacement"), frame(12, bvid="BV0000000000")])
def test_window_coordinates_do_not_cross_media_identity(foreign):
    window = {"start": 10, "end": 13}
    other = reading(foreign | {"sample_window": window})
    other["id"] = "other"
    other["arguments"] = {"bvid": foreign["url"].split("/")[4], "part": foreign["part"]}
    other["result"]["selection"] = other["arguments"]
    evidence = evidence_index([reading(frame(11) | {"sample_window": window}), other])
    with pytest.raises(DelegationError, match="同一已核实"):
        render_private_delivery(selection("v:1", "other:1", primary="v:1"), evidence)


def test_primary_requires_a_cited_frame_and_text_research_keeps_multiple_sources():
    evidence = evidence_index([reading(frame(0)), page("a"), page("b")])
    for primary in ["a", "missing"]:
        with pytest.raises(DelegationError):
            render_private_delivery(selection("a", "b", primary=primary), evidence)
    clip = selection("a", primary="v:1")
    assert clip.items[0].sources == ["v:1", "a"]
    assert "主帧：" in render_private_delivery(clip, evidence)[1]
    assert render_private_delivery(selection(primary="v:1"), evidence)[0]
    with pytest.raises(ValueError, match="至少需要"):
        selection()
    _, body = render_private_delivery(selection("a", "b"), evidence)
    assert evidence["a"] in body and evidence["b"] in body and "主帧" not in body
    _, body = render_private_delivery(selection("v:1", "a"), evidence)
    assert "单帧采样" in body and "音轨均未经该画面回执核实" in body


def test_legacy_subtitle_points_remain_citable_and_old_part_links_get_actual_timestamp():
    one = frame(18.625) | {"kind": "字幕", "url": f"https://www.bilibili.com/video/{BV}/?p=2"}
    two = frame(96) | {"kind": "字幕"}
    evidence = evidence_index([reading(one, two)])
    _, body = render_private_delivery(selection("v:1", "v:2"), evidence)
    assert "18.625 秒" in body and "?p=2&t=18>" in body and "字幕时点" in body and "主帧" not in body
    with pytest.raises(DelegationError, match="主帧"):
        render_private_delivery(selection("v:2", primary="v:1"), evidence)


@pytest.mark.parametrize("fault", ["backend_mix", "editor_mix", "unheard_audio", "unheard_gap"])
async def test_invalid_video_delivery_cannot_complete(runtime, settings, store, monkeypatch, fault):
    def worker(payload):
        schema = next(tool for tool in payload["tools"] if tool["name"] == "submit_materials")["parameters"]
        props = schema["properties"]["items"]["items"]["properties"]
        assert props["primary_frame"]["anyOf"][0]["enum"] == ["v:1", "v:2"]
        product = selection("v:1", "a", primary="v:1")
        if fault == "backend_mix":
            product.items[0].sources.append("v:2")
        return [call("submit_materials", product.model_dump(), "submit")]

    gateway(settings, monkeypatch, worker)
    reviews = []

    async def editor(messages, schema, *, max_tokens):
        packet = json.loads(messages[-1]["content"])
        if "candidate" in packet:
            assert fault != "backend_mix"
            assert schema["$defs"]["CreativeItem"]["properties"]["primary_frame"]["anyOf"][0]["enum"] == ["v:1", "v:2"]
            item = packet["candidate"]["items"][0]
            if fault == "editor_mix":
                item["sources"].append("v:2")
            elif fault == "unheard_gap":
                packet["candidate"]["gaps"] = ["只录到静默，无法核对台词。"]
            else:
                item["rationale"] = "片中一片寂静，适合开场。"
            return packet["candidate"]
        review = packet["items_to_verify"][0]
        assert set(review["evidence"]) == {"v:1", "a"}
        if fault == "unheard_gap":
            assert packet["gaps"] == ["只录到静默，无法核对台词。"]
        else:
            assert "一片寂静" in review["rationale"]
        reviews.append(review)
        return {"supported": False}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, f"请为 {BV} P2 选材")
    task = tasks.current(identity)
    tasks._save(task, steps=[reading(frame(11), frame(210)), page("a")], plan={"backend": "responses", "prior_goals": []})
    await tasks._advance(tasks.current(identity))
    pending = tasks.current(identity)
    if fault != "backend_mix":
        assert pending["status"] == "queued" and "candidate" in pending["plan"]
        await tasks._advance(pending)
    final = tasks.current(identity)
    assert final["status"] == "failed" and tasks.workspace.latest(identity) is None
    assert bool(reviews) == (fault in {"unheard_audio", "unheard_gap"})
    assert ("前台核对未通过" if reviews else "同一已核实") in final["message"]
    assert "一片寂静" not in final["message"] and "只录到静默" not in final["message"]
