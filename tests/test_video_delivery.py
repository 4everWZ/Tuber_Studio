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


def test_code_grant_is_not_an_artifact_receipt():
    evidence = evidence_index([
        {"id": "grant", "tool": "owner_code_grant", "result": {"allowed": True}},
        {"id": "missing", "tool": "private_code", "result": {"name": "notes.md"}},
        {"id": "done", "tool": "private_code", "result": {"name": "notes.md", "text": "已经整理"}},
    ])
    assert evidence == {"done": "工件 notes.md：已经整理"}


async def test_front_and_reviewer_share_scoped_media_and_unread_page_receipts(runtime, store, monkeypatch):
    read = reading(frame(11))
    read["result"].update(gaps=["此区间未取得可公开读取的字幕。", "画面按时点抽样，未连续观看或听审音轨。"])
    read["provided_page_urls"] = ["https://example.org/origin", "https://example.org/p2"]
    missing_page = {"tool": "web_page", "id": "missing", "arguments": {"web_fetch": "https://example.org/p2"},
                    "result": {"error": "未取得正文"}}
    evidence = evidence_index([read, page("origin"), missing_page])
    product = selection("v:1", "origin").model_dump()
    inspected = []

    async def editor(messages, schema, *, max_tokens):
        packet = json.loads(messages[-1]["content"])
        assert packet["pages_without_body_receipts"] == ["https://example.org/p2"]
        coverage = packet["video_read_coverage"]["v"]
        assert coverage["requested_ranges"] == read["result"]["selection"]["ranges"]
        assert coverage["samples"] == [{"id": "v:1", "part": 2, "seconds": 11, "medium": "单帧采样"}]
        assert coverage["read_gaps"] == read["result"]["gaps"]
        inspected.append("candidate" in packet)
        return product if "candidate" in packet else {"supported": True, "issues": []}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    task = {"goal": "整理", "plan": {"candidate": product, "prior_goals": []}}
    selected = await tasks.edit_delivery(task, evidence)
    assert inspected == [True, False]
    _, body = render_private_delivery(selected, evidence)
    assert body.endswith("以下给定来源尚无网页正文回执：\n- <https://example.org/p2>")


@pytest.mark.parametrize("bad_review", [False, True])
async def test_keep_original_frame_item_and_review_only_rendered_fields(runtime, store, monkeypatch, bad_review):
    evidence = evidence_index([reading(frame(11), frame(35), frame(230)), page("origin")])
    original = selection("origin", primary="v:1")
    old = original.items[0].model_dump()
    replacement = selection("origin", primary="v:3").items[0].model_dump()

    async def editor(messages, schema, *, max_tokens):
        packet = json.loads(messages[-1]["content"])
        if "candidate" in packet:
            assert packet["previous_items_by_primary_frame"] == {"v:1": old}
            keep = schema["properties"]["items"]["items"]["anyOf"][1]
            assert keep["properties"]["keep_previous"]["enum"] == ["v:1"]
            return {"items": [{"keep_previous": "v:1"}, replacement], "gaps": []}
        props = schema["$defs"]["EditorialIssue"]["properties"]
        assert "items.0.rationale" in props["field"]["enum"]
        assert not any("v:2" in key or "gap_evidence" in key for key in props["field"]["enum"])
        assert "画面不能判断实际声音" in props["reason"]["enum"]
        if bad_review:
            return {"supported": False, "issues": [
                {"field": "gap_evidence.v:2", "reason": "引用未支持该断言", "quote": "椅子"}]}
        return {"supported": True, "issues": []}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    task = {"goal": "保留第一项，换第二项", "plan": {"previous_selection": original.model_dump(),
                                                  "prior_goals": [], "candidate": original.model_dump()}}
    if bad_review:
        with pytest.raises(DelegationError, match="没有绑定"):
            await tasks.edit_delivery(task, evidence)
    else:
        selected = await tasks.edit_delivery(task, evidence)
        assert [item.model_dump() for item in selected.items] == [old, replacement]


def test_legacy_subtitle_points_remain_citable_and_old_part_links_get_actual_timestamp():
    one = frame(18.625) | {"kind": "字幕", "url": f"https://www.bilibili.com/video/{BV}/?p=2"}
    two = frame(96) | {"kind": "字幕"}
    evidence = evidence_index([reading(one, two)])
    _, body = render_private_delivery(selection("v:1", "v:2"), evidence)
    assert "18.625 秒" in body and "?p=2&t=18>" in body and "字幕时点" in body and "主帧" not in body
    with pytest.raises(DelegationError, match="主帧"):
        render_private_delivery(selection("v:2", primary="v:1"), evidence)


@pytest.mark.parametrize("fault", ["backend_mix", "editor_mix", "unheard_audio", "unheard_gap", "correct_reason", "still_wrong"])
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
        if "issues" in packet and "draft" in packet:
            assert packet["issues"] == {"items.0.rationale": "「一片寂静」画面不能判断实际声音"}
            assert set(schema["properties"]) == {"items.0.rationale"}
            return {"items.0.rationale": "我想用椅子的构图开场。" if fault == "correct_reason" else "一片寂静。"}
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
        elif fault == "correct_reason" and reviews:
            assert review["rationale"] == "我想用椅子的构图开场。"
        else:
            assert "一片寂静" in review["rationale"]
        reviews.append(review)
        if fault in {"correct_reason", "still_wrong"}:
            if len(reviews) == 2 and fault == "correct_reason":
                return {"supported": True, "issues": []}
            return {"supported": False, "issues": [{"field": "items.0.rationale", "quote": "一片寂静", "reason": "画面不能判断实际声音"}]}
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
    if fault in {"correct_reason", "still_wrong"}:
        assert final["status"] == "queued" and tasks.workspace.latest(identity) is None
        recovered = create_task_runtime(runtime, store, None)
        await recovered._advance(recovered.current(identity))
        final = recovered.current(identity)
        assert len(reviews) == 2
        if fault == "correct_reason":
            assert final["status"] == "completed" and "我想用椅子的构图开场。" in recovered.workspace.latest(identity)["body"]
            assert final["plan"]["selected"]["items"][0]["sources"] == ["v:1", "a"]
            return
    assert final["status"] == "failed" and tasks.workspace.latest(identity) is None
    assert bool(reviews) == (fault in {"unheard_audio", "unheard_gap", "still_wrong"})
    assert ("前台核对未通过" if reviews else "同一已核实") in final["message"]
    assert "一片寂静" not in final["message"] and "只录到静默" not in final["message"]
