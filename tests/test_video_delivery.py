import json
from uuid import uuid4

import pytest
from test_agent_creative_delegation import call, gateway

from tuber_studio.agent.task_delivery import (
    AdditionalVideoRead,
    DelegationError,
    PrivateDelivery,
    evidence_index,
    previous_frame_items,
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


def short_read(key, start, *, clip=True, revision=1):
    read = reading(*(frame(start + delta) for delta in (0.44, 1.48, 2.52)))
    bounds = {"part": 2, "start": start, "end": start + 3}
    read.update(id=key, revision=revision, arguments={"bvid": BV, **bounds})
    read["result"]["selection"]["ranges"] = [bounds]
    if clip:
        read["result"]["clip"] = {"bvid": BV, **bounds, "cid": "part-two", "frames": 75, "audio": "visual_only"}
    return read


@pytest.mark.parametrize("alter", [None, {"cid": "elsewhere"}, {"end": 15}, {"part": 1}, {"frames": 1}, {"audio": "unknown"}])
def test_physical_clip_is_distinct_from_semantic_sampling(alter):
    read = short_read("window", 11)
    if alter:
        read["result"]["clip"].update(alter)
    evidence = evidence_index([read])
    _, body = render_private_delivery(selection(primary="window:1"), evidence)
    assert "内容语义仅观察 3 帧" in body and "11.44、12.48、13.52" in body
    assert ("保存 75 个连续原帧" in body) == (alter is None)
    if not alter:
        assert "出点 14 秒（不含）" in body and "私有短片不含音轨" in body
        assert render_private_delivery(selection("window:2", primary="window:1"), evidence)[0]
        with pytest.raises(DelegationError):
            render_private_delivery(selection("window:3", primary="window:1"), evidence)


async def test_revision_reopens_research_and_recomputes_gaps(runtime, store, monkeypatch):
    first, second = short_read("a", 11), short_read("b", 35)
    old = PrivateDelivery(items=[selection(primary=key).items[0] for key in ["a:1", "b:1"]],
                          gaps=["另一窗口尚未采用。"])
    candidate = old.model_dump()
    goal = "保留第一段原文，另读开场三秒。"
    task = {"revision": 2, "steps": [first, second], "goal": goal, "plan": {
        "candidate": candidate, "prior_goals": ["选两段"], "previous_selection": candidate}}
    tasks = create_task_runtime(runtime, store, None)

    async def editor(messages, schema, **kwargs):
        packet = json.loads(messages[-1]["content"])
        if "上一版" in packet:
            return {"ordered_actions": ["保留整项原文", "另读新窗口再替换"]}
        if "candidate" in packet:
            assert "另一窗口尚未采用" not in str(packet)
            assert set(packet["evidence"]) == {"new:1", "new:2", "new:3"}
            return selection(primary="new:1").model_dump()
        assert packet["field_visual_sources"]
        return {"supported": True, "field_basis": {p: {"source": keys[0], "observation": "椅子"}
                                                   for p, keys in packet["field_visual_sources"].items()}}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    with pytest.raises(AdditionalVideoRead):
        await tasks.edit_delivery(task, evidence_index(task["steps"]))
    task["steps"].extend([short_read("overlap", 12, revision=2), short_read("new", 0, revision=2)])
    evidence = evidence_index(task["steps"])
    selected = await tasks.edit_delivery(task, evidence)
    assert selected.items[0] == old.items[0] and selected.items[1].primary_frame == "new:1"
    assert selected.gaps == [] and "另一窗口" not in render_private_delivery(selected, evidence)[1]


@pytest.mark.parametrize("quote", ["椅子", "河南治水"])
async def test_saved_clip_omni_decides_each_item_and_displays_attribution(runtime, store, monkeypatch, quote):
    read = short_read("window", 0)
    for point in read["result"]["findings"]:
        point["cid"] = "321"
    read["result"]["clip"]["cid"] = "321"
    origin = page("origin")
    origin["result"].update(title="例片", url=f"https://www.bilibili.com/video/{BV}/?p=2",
                            source_kind="bilibili_public_metadata",
                            excerpt="投稿《例片》，UP 主：示例作者。合集 P2《开场》，CID 321，时长 90 秒。投稿简介：剧情结局。")
    evidence = evidence_index([read, origin])
    candidate = selection("origin", primary="window:1")
    candidate.gaps = ["旧的未选用窗口"]
    task = {"goal": "要无剧透的画面", "plan": {"prior_goals": [], "candidate": candidate.model_dump()}}
    calls = []

    async def editor(messages, schema, **kwargs):
        packet = json.loads(messages[-1]["content"])
        calls.append(packet)
        if "candidate_primary" in packet:
            assert packet["candidate_primary"] == "window:1"
            assert "示例作者" in packet["item_evidence"]["origin"] and "剧情结局" not in str(packet)
            assert "旧的未选用窗口" not in str(packet)
            return {"decision": "采用", "primary_frame": "window:2", "title": "画中的椅子", "rationale": "从画面左侧的椅子引出布景。"}
        assert packet["field_visual_sources"] == dict.fromkeys(["items.0.title", "items.0.rationale"], ["window:2"])
        return {"supported": True, "field_basis": {p: {"source": "window:2", "observation": quote}
                                                   for p in packet["field_visual_sources"]}}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    if quote == "河南治水":
        with pytest.raises(DelegationError, match="回执原文"):
            await tasks.edit_delivery(task, evidence)
        return
    result = await tasks.edit_delivery(task, evidence)
    assert result._field_sources["items.0.title"] == {"source": "window:2", "observation": quote}
    _, body = render_private_delivery(result, evidence)
    assert len(calls) == 2 and "示例作者" in body and "剧情结局" not in body and not result.gaps
    assert "来源摘录" not in body and "左侧有一把椅子" not in body


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
        assert coverage["samples"] == [[1, 2, 11, "单帧采样"]]
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
        index = packet["items_to_verify"][0]["index"]
        assert f"items.{index}.rationale" in props["field"]["enum"]
        assert all(path.startswith(f"items.{index}.") for path in props["field"]["enum"])
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


def test_legacy_retention_requires_verbatim_render_and_unambiguous_visible_receipts():
    steps = [reading(frame(11)), page("origin")]
    evidence = evidence_index(steps)
    initial = selection("origin", primary="v:1")
    initial.items[0].title = "[原题]"
    _, body = render_private_delivery(initial, evidence)
    task = {"revision": 2, "steps": steps, "plan": {"previous_delivery": body}}
    assert previous_frame_items(task, evidence) == {"v:1": initial.items[0].model_dump()}
    assert previous_frame_items(task, evidence_index(steps[1:])) == {}
    altered = task | {"plan": {"previous_delivery": body.replace("仅核实所列采样时点", "连续观看")}}
    assert previous_frame_items(altered, evidence) == {}


@pytest.mark.parametrize("fault", ["backend_mix", "editor_mix", "unheard_audio", "unheard_gap", "correct_reason", "still_wrong"])
async def test_invalid_video_delivery_cannot_complete(runtime, settings, store, monkeypatch, fault):
    def worker(payload):
        schema = next(tool for tool in payload["tools"] if tool["name"] == "submit_materials")["parameters"]
        props = schema["properties"]["items"]["items"]["properties"]
        assert props["primary_frame"]["anyOf"][0]["enum"] == ["v:1", "v:2"]
        product = selection("v:1", "a", primary="v:1")
        if fault == "backend_mix":
            product.items[0].sources.append("v:2")
        retry = any(entry.get("type") == "function_call_output" and json.loads(entry["output"])["receipt"].get("error")
                    for entry in payload["input"])
        return [call("submit_materials", product.model_dump(), "submit2" if retry else "submit")]

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
            assert "title" not in item and "rationale" not in item
            item.update(title="开场", rationale="我会用这个构图引出话题。")
            if fault == "editor_mix":
                item["sources"].append("v:2")
            elif fault == "unheard_gap":
                packet["candidate"]["gaps"] = ["只录到静默，无法核对台词。"]
            else:
                item["rationale"] = "片中一片寂静，适合开场。"
            return packet["candidate"]
        if not packet["items_to_verify"]:
            assert packet["gaps"] == ["只录到静默，无法核对台词。"]
            reviews.append(packet["gaps"])
            return {"supported": False}
        review = packet["items_to_verify"][0]
        assert set(review["evidence"]) == {"v:1", "a"}
        if fault == "unheard_gap":
            return {"supported": True}
        if fault == "correct_reason" and reviews:
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
    if fault == "backend_mix":
        assert pending["steps"][-1]["invalid_call"] and pending["status"] == "queued"
        await tasks._advance(pending)
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
