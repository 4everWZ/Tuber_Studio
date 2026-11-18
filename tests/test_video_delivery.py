import base64
import io
import json
import shutil
import subprocess
from uuid import uuid4

import pytest
from PIL import Image
from test_agent_creative_delegation import call, gateway

from tuber_studio.agent.public_video import clip_path
from tuber_studio.agent.task_delivery import (
    WINDOW_REVIEW_VERSION,
    AdditionalVideoRead,
    DelegationError,
    EditorialCorrection,
    PrivateDelivery,
    evidence_index,
    previous_frame_items,
    render_private_delivery,
)
from tuber_studio.agent.task_tools import (
    accepted_window,
    attribution_receipt,
    cited_programme_names,
    classify_clip_captions,
    clip_review_image,
    create_task_runtime,
    discovery_windows,
    sampled_video_findings,
    screen_clip_window,
    video_provenance,
)

BV = "BV1234567890"


@pytest.fixture(autouse=True)
def anonymous_frame_fields(monkeypatch):
    async def extract(fields, *_):
        return {path: [] for path in fields}

    monkeypatch.setattr("tuber_studio.agent.task_tools.extract_people", extract)


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


def test_discovery_uses_actual_nominations_in_disjoint_bounded_windows():
    read = reading(frame(0), frame(1), frame(28))
    read["result"]["selection"]["ranges"][0]["end"] = 30
    candidate = {"items": [{"primary_frame": k, "sources": [k]} for k in ["v:1", "v:2", "v:3"]]}
    evidence = evidence_index([read])
    assert discovery_windows(candidate, evidence, 3, 2) == [
        {"bvid": BV, "part": 2, "start": 0, "end": 3}, {"bvid": BV, "part": 2, "start": 26, "end": 29}]
    assert discovery_windows(candidate, evidence, 15, 2) == []


async def test_responses_nominates_scoped_video_samples_without_replaying_unrelated_html(runtime, store, monkeypatch):
    read = reading(frame(11), frame(29))
    read["result"]["part_duration"] = 300
    origin = page("source")
    origin["result"].update(title="公开节目资料", excerpt="网页导航和剧情简介。")

    def worker(payload):
        assert [t["name"] for t in payload["tools"]] == ["submit_materials"]
        item_schema = payload["tools"][0]["parameters"]["properties"]["items"]["items"]
        assert item_schema["properties"]["primary_frame"] == {"type": "string", "enum": ["v:1", "v:2"]}
        packet = json.loads(payload["input"][1]["content"])
        assert packet["samples"]["v:1"]["seconds"] == 11 and packet["samples"]["v:2"]["part"] == 2
        assert packet["documents"]["source"]["title"] == "公开节目资料" and "剧情简介" not in str(packet)
        if not packet["submission_errors"]:
            empty = {"title": "待调查", "rationale": "只有查询线索", "sources": [], "primary_frame": None}
            return [call("submit_materials", {"items": [empty], "gaps": []}, "missing")]
        assert "items.0" in packet["submission_errors"][0]
        return [call("submit_materials", selection("source", primary="v:1").model_dump(), "nomination")]

    requests = gateway(runtime.settings, monkeypatch, worker)
    tasks = create_task_runtime(runtime, store, None)

    async def nominate(*args):
        return True

    tasks.can_nominate_video = nominate
    identity = str(uuid4())
    task = tasks.create(identity, f"为 {BV} P2 选两个镜头")
    search = {"id": "search", "revision": 1, "tool": "web_search", "arguments": {"query": "节目资料"},
              "result": {"results": [{"url": origin["result"]["url"]}]}}
    tasks._save(task, plan={"backend": "responses", "prior_goals": []}, steps=[read, search, origin])
    await tasks._advance(tasks.current(identity))
    assert tasks.current(identity)["steps"][-1]["invalid_call"] and not tasks.current(identity)["plan"].get("candidate")
    await tasks._advance(tasks.current(identity))
    assert len(requests) == 2 and tasks.current(identity)["plan"]["candidate"]["items"][0]["primary_frame"] == "v:1"


async def test_playable_request_returns_to_collection_but_single_frame_can_finish(runtime, store, monkeypatch):
    read = reading(frame(11))
    read["result"]["part_duration"] = 300
    evidence = evidence_index([read])
    candidate = selection(primary="v:1").model_dump()
    task = {"goal": "准备本次交付", "steps": [read], "plan": {"prior_goals": [], "candidate": candidate}}
    playable = True

    async def model(messages, schema, **kwargs):
        if "requested_count" in schema["properties"]:
            return {"requested_count": 1, "usage": "展示构图", "playable": playable, "trace_origin": playable}
        if "supported" in schema["properties"]:
            return {"supported": True}
        return candidate

    monkeypatch.setattr(runtime.llm, "generate_json", model)
    tasks = create_task_runtime(runtime, store, None)
    with pytest.raises(AdditionalVideoRead) as research:
        await tasks.edit_delivery(task, evidence)
    assert research.value.sources and research.value.windows == [{"bvid": BV, "part": 2, "start": 9, "end": 12}]
    playable = False
    assert (await tasks.edit_delivery(task, evidence)).items[0].primary_frame == "v:1"


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
        legacy = selection(primary="window:1")
        legacy._window_reviews["window"] = {"accepted": True, "clip": evidence.video_coverage["window"]["clip"]}
        assert render_private_delivery(legacy, evidence)[1] == body
        assert render_private_delivery(selection("window:2", primary="window:1"), evidence)[0]
        with pytest.raises(DelegationError):
            render_private_delivery(selection("window:3", primary="window:1"), evidence)


async def test_source_lookup_uses_the_work_named_by_public_metadata(runtime, store, monkeypatch):
    read = reading(frame(11))
    read["result"]["part_duration"] = 300
    origin = page("upload")
    origin["result"].update(title="《例作》节目合辑", source_kind="bilibili_public_metadata",
                            url=f"https://www.bilibili.com/video/{BV}/?p=2")
    task = {"goal": "找两段可用素材，查原出处", "steps": [read, origin],
            "plan": {"prior_goals": [], "candidate": selection(primary="v:1").model_dump()}}

    async def model(messages, schema, **kwargs):
        if "requested_count" in schema["properties"]:
            return {"requested_count": 2, "usage": "直播取材", "playable": True, "trace_origin": True}
        assert schema["properties"]["title"]["enum"] == ["例作"]
        return {"title": "例作", "kind": "影视节目"}

    monkeypatch.setattr(runtime.llm, "generate_json", model)
    tasks = create_task_runtime(runtime, store, None)
    with pytest.raises(AdditionalVideoRead) as research:
        await tasks.edit_delivery(task, evidence_index(task["steps"]))
    assert research.value.lookup == {"query": "例作", "catalog": "央视节目库"}
    assert research.value.sources and research.value.windows


def test_clip_cites_unstructured_document_without_displaying_its_synopsis():
    steps = [short_read("v", 7), page("credits"), page("archive")]
    steps[1]["result"]["programme_credits"] = "示例作品 · 公开署名"
    steps[2]["result"].update(title="节目档案", excerpt="档案中记有剧情结局。")
    index = evidence_index(steps)
    item = selection("credits", "archive", primary="v:1")
    _, body = render_private_delivery(item, index)
    assert index["archive"] != index.display_source("archive")
    assert "节目档案" in body and "公开署名" in body and "剧情结局" not in body
    assert "剧情结局" in render_private_delivery(selection("archive"), index)[1]
    prior = body.replace(index.display_source("archive"), index["archive"])
    task = {"revision": 2, "steps": steps, "plan": {"previous_delivery": prior}}
    assert previous_frame_items(task, index)["v:1"]["rationale"] == item.items[0].rationale


def test_opened_unrelated_document_cannot_fulfil_the_video_origin_request():
    upload, unrelated, archive = page("upload"), page("map"), page("archive")
    upload["result"].update(title="《示例影片》评论", source_kind="bilibili_public_metadata",
                            url=f"https://www.bilibili.com/video/{BV}/?p=2")
    unrelated["result"].update(title="游戏地图下载")
    archive["result"].update(title="节目档案", programme_credits="作品：示例影片；导演：示例作者")
    steps = [upload, unrelated, archive]
    assert video_provenance(steps, evidence_index(steps)) == (["《示例影片》评论"], ["示例影片"], ["upload"], ["archive"])


def test_work_name_requires_cited_credit_and_can_never_identify_the_frame():
    archived, other = page("official"), page("other")
    archived["result"]["programme_credits"] = "节目名：示例作品；导演：示例作者"
    other["result"]["excerpt"] = "文字里出现了《另一个名字》。"
    names = cited_programme_names([archived, other], ["official", "other"])
    assert names == {"示例作品"} and cited_programme_names([archived, other], ["other"]) == set()
    item = selection("official", "other", primary="v:1").items[0]

    def review(text, claims=()):
        return attribution_receipt("items.0.title", text, claims, [archived, other], item)

    assert not review("宝座旁站着谁？")["claims"]
    unsupported = review("这是《另一个名字》的画面吗？")
    claim = unsupported["claims"][0]
    assert claim["issue"] and claim["phrase"] in unsupported["text"] and claim["other_programme_pages"] == ["official"]
    assert unsupported["pages"][0]["programme_credits"] == archived["result"]["programme_credits"]
    assert not review("节目资料列有《示例作品》。")["claims"][0]["issue"]
    claimed = {"phrase": "示例作品中的一幕", "target": "示例作品", "relation": "断言镜头或人物的作品或集归属"}
    attribution = review("这是示例作品中的一幕。", [claimed])["claims"][0]
    assert attribution["issue"] and attribution["matching_programme_pages"] == ["official"]
    assert review("雕花衣领", [claimed])["claims"][0]["issue"] == "审核未能逐字定位该主张，归属判断尚未核实。"


async def test_attribution_rejections_persist_original_and_repair_with_private_citations(runtime, store):
    tasks = create_task_runtime(runtime, store, None)
    task = tasks.create(str(uuid4()), "私下核对提纲")
    doc = page("catalogue")
    doc["result"].update(programme_credits="节目名：示例作品", title="节目目录")
    product = selection("catalogue", primary="v:1")
    tasks._save(task, steps=[reading(frame(7)), doc], plan={"backend": "responses", "prior_goals": [],
                                                                   "candidate": product.model_dump()})
    expected = []

    async def editor(task, evidence):
        item = product.items[0]
        item.title = "本镜头来自示例作品" if expected else "这是示例作品的一幕"
        claim = {"phrase": item.title, "target": "示例作品", "relation": "断言镜头或人物的作品或集归属"}
        receipt = attribution_receipt("items.0.title", item.title, [claim], task["steps"], item)
        expected.append(receipt)
        if len(expected) == 1:
            product._attribution_reviews = [receipt]
            raise EditorialCorrection(product, {"items.0.title": "归属未核"})
        raise DelegationError("作品归属未核", attribution_reviews=[receipt])

    tasks.edit_delivery = editor
    await tasks._advance(tasks.current(task["id"]))
    first = tasks.current(task["id"])
    assert first["status"] == "queued" and first["plan"]["attribution_rejections"] == expected
    await tasks._advance(first)
    final = tasks.current(task["id"])
    assert final["status"] == "failed" and final["plan"]["attribution_rejections"] == expected
    assert final["message"] == "作品归属未核" and expected[-1]["claims"][0]["matching_programme_pages"] == ["catalogue"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires local video decoder")
async def test_review_decodes_actual_primary_pts_and_checks_receipt_ownership(tmp_path):
    key = str(uuid4())
    read = short_read(key, 11)
    read["result"]["request_id"] = key
    destination = clip_path(tmp_path, key)
    destination.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=red:s=32x32:r=25:d=3,drawbox=color=blue:t=fill:enable='gte(t,1)'",
                    "-c:v", "libx264", "-movflags", "+empty_moov+frag_keyframe", str(destination)], check=True)
    evidence = evidence_index([read])
    for suffix, channel in [(1, 0), (2, 2)]:
        content = await clip_review_image(tmp_path, {"steps": [read]}, evidence, f"{key}:{suffix}")
        jpg = base64.b64decode(content["image_url"]["url"].split(",")[1])
        with Image.open(io.BytesIO(jpg)) as image:
            assert image.convert("RGB").getpixel((12, 12))[channel] > 240
    read["result"]["request_id"] = str(uuid4())
    with pytest.raises(DelegationError, match="本任务"):
        await clip_review_image(tmp_path, {"steps": [read]}, evidence, key + ":1")


@pytest.fixture
def reviewed_window(monkeypatch):
    # Isolate caption/revision tests. Separate tests decode and review the full clip.
    def media(_directory, _task, evidence, key):
        return None, evidence.video_coverage[key.rsplit(":", 1)[0]]["clip"]

    async def reviewed(media, *_):
        clip = media[1]
        samples = [{"frame": 0, "seconds": clip["start"]}, {"frame": clip["frames"] - 1, "seconds": clip["end"] - 0.04}]
        return {"clip": clip, "coverage": {"complete": True, "uncovered": [], "scanned_frames": clip["frames"], "samples": samples,
                                          "start": clip["start"], "end": clip["end"]},
                "observations": [s | {"scene": "符合要求", "captions": "无烧录字幕", "screen_text": ""} for s in samples],
                "caption_topics": [], "usage": "仅无剧透素材", "accepted": True, "screening_version": WINDOW_REVIEW_VERSION}

    monkeypatch.setattr("tuber_studio.agent.task_tools.saved_review_media", media)
    monkeypatch.setattr("tuber_studio.agent.task_tools.screen_clip_window", reviewed)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires video decoder")
@pytest.mark.parametrize("tail", ["已读且符合", "已读但不符合", "无法读清", "已被视觉误判符合的叙事"])
async def test_window_gate_reviews_changed_tail_pixels(tmp_path, tail):
    file = tmp_path / "two-colours.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=red:s=96x64:r=25:d=3,drawbox=color=blue:t=fill:enable='gte(t,2)'",
                    "-c:v", "libx264", str(file)], check=True)
    seen = []

    async def review(messages, schema, **kwargs):
        content = messages[-1]["content"]
        if "usage" in schema["properties"]:
            return {"usage": "仅无剧透素材"}
        if "1" in schema["properties"]:
            assert json.loads(content) == {"待标注原句": {"1": "测试字幕"}}
            subject = "提及片中人物或所讲世界中发生的事" if tail == "已被视觉误判符合的叙事" else "只谈片外的节目、制作、传播或可见形式"
            return {"1": {"topic": "播出及制作资料", "subject": subject}}
        replies = {}
        for header, image in zip(content[:-1:2], content[1::2], strict=True):
            coordinate = json.loads(header["text"])
            seen.append(coordinate["seconds"])
            jpeg = base64.b64decode(image["image_url"]["url"].split(",")[1])
            with Image.open(io.BytesIO(jpeg)) as picture:
                red, _, blue = picture.getpixel((10, 10))
            verdict = "已读且符合" if tail == "已被视觉误判符合的叙事" else tail
            replies[str(coordinate["frame"])] = {"scene": "符合要求", "captions": verdict if blue > red else "无烧录字幕",
                    "screen_text": "测试字幕" if blue > red else "", "basis": "按当前实际颜色标记测试状态。"}
        return replies

    result = await screen_clip_window((file, {"start": 7, "end": 10, "frames": 75}), "整段无剧透", review, 512)
    assert result["coverage"]["scanned_frames"] == 75 and seen[0] == 7 and any(t >= 9 for t in seen)
    assert result["accepted"] == (tail == "已读且符合")
    if result["accepted"]:
        assert seen[-1] == pytest.approx(9.96)
        assert accepted_window(result, result["clip"], True)
        assert not accepted_window(result | {"screening_version": 1}, result["clip"], True)
    if tail == "已被视觉误判符合的叙事":
        contradictory = result | {"accepted": True, "usage": "允许讲述剧情或无此限制"}
        assert not accepted_window(contradictory, result["clip"], True)
        assert not accepted_window(result | {"accepted": True}, result["clip"], False)


async def test_opening_revision_inherits_spoiler_restriction_even_when_new_message_omits_it():
    receipt = {"observations": [{"screen_text": "城门倒了"}]}

    async def audit(messages, schema, **kwargs):
        assert set(schema["properties"]) == {"1"}
        return {"1": {"topic": "故事事件的起因、经过、结局或分析", "subject": "提及片中人物或所讲世界中发生的事"}}

    result = await classify_clip_captions(receipt, "保留第一段，第二段换成适合开场的", audit, 512, True)
    assert result["usage"] == "仅无剧透素材" and not result["accepted"]


async def test_uncovered_subtitle_change_cannot_use_single_frame_approval(monkeypatch):
    async def incomplete(*args, **kwargs):
        return {"coverage": {"complete": False, "scanned_frames": 75, "samples": [{"frame": 0}],
                             "uncovered": [{"start": 2, "end": 3}]}, "images": [{}]}

    monkeypatch.setattr("tuber_studio.agent.task_tools.review_clip_window", incomplete)
    result = await screen_clip_window((None, {"start": 0, "end": 3, "frames": 75}), "不要透露剧情", None, 512)
    assert not result["accepted"] and not result["observations"]


@pytest.mark.parametrize("invented_title", [False, True])
async def test_real_image_inventory_and_caption_do_not_inherit_worker_plot(runtime, store, monkeypatch, invented_title, reviewed_window):
    read = short_read("read", 1)
    read["result"]["findings"][0]["text"] = "后台推测的登基剧情"
    evidence = evidence_index([read])
    task = {"goal": "取一段画面", "steps": [read], "plan": {"prior_goals": [], "candidate": selection(primary="read:1").model_dump()}}

    async def pixels(*_):
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,test"}}

    async def omni(messages, schema, **kwargs):
        props = schema["properties"]
        if props and all("含具体影视作品名称或将画面归属特定影片剧集" in v.get("enum", ()) for v in props.values()):
            return dict.fromkeys(props, "构图、物件、外观或服装")
        if "requested_count" in props:
            return {"requested_count": 1, "usage": "讲解构图", "playable": True, "trace_origin": False}
        if "decision" in props:
            return {"decision": "采用", "primary_frame": "read:1", "rationale": "可以看构图"}
        content = messages[-1]["content"]
        if "work_claims" in props:
            fields = json.loads(content)
            assert all(k.startswith("items.0.") for k in fields)
            return {"work_claims": {path: [] for path in fields}}
        if "opening" in props:
            assert json.loads(content)["号主交办"] == task["goal"] and "后台推测" not in content
            return {"opening": "我会用这一镜引出布景话题。", "main": "在讲解时插入。", "transition": "先停一下旁白。"}
        if "title_action" in props:
            return {"title_action": "另拟标题", "fact": props["fact"]["enum"][0]}
        if set(props) == {"title"}:
            assert "enum" not in props["title"]
            assert json.loads(content)["选定的事实锚点"] == "座椅旁站着一人"
            return {"title": "这是登基仪式吗？" if invented_title else "椅子旁站着谁？"}
        assert isinstance(content, list) and "后台推测" not in str(content)
        if "labels" in props:
            assert content[0]["type"] == "image_url" and [p["type"] for p in content] == ["image_url", "text"]
            return {"labels": ["座椅旁站着一人"], "details": ["一人站在椅子旁"], "screen_text": []}
        if "items.0.title" in props:
            assert props["items.0.title"]["anyOf"][1]["properties"]["use_fact"]["enum"] == ["座椅旁站着一人", "一人站在椅子旁"]
            assert "fixed_title_anchors" in content[0]["text"] and "座椅旁站着一人" in content[0]["text"]
            return {"items.0.title": "一人站在椅子旁。"}
        if "anchor" in props:
            assert props["anchor"]["enum"] == ["座椅旁站着一人"]
            assert props["detail"]["enum"] == ["一人站在椅子旁"]
            return {"anchor": "座椅旁站着一人", "detail": "一人站在椅子旁",
                    "use": "用在开场，我会用这一镜引出布景话题。"}
        assert "title_fact_anchor" in str(content) and any(v["type"] == "image_url" for v in content)
        if invented_title and "登基仪式" in content[0]["text"]:
            return {"supported": False, "issues": [{"field": "items.0.title", "quote": "登基仪式", "reason": "引用未支持该断言"}]}
        return {"supported": True, "field_basis": {f"items.0.{p}": {"source": "read:1", "observation": "一人站在椅子旁",
                "claim": "编辑用途", "work_names": [], "work_reference": "无作品引用"} for p in ["title", "rationale"]}}

    monkeypatch.setattr("tuber_studio.agent.task_tools.clip_review_image", pixels)
    monkeypatch.setattr(runtime.llm, "generate_json", omni)
    tasks = create_task_runtime(runtime, store, None)
    if invented_title:
        with pytest.raises(EditorialCorrection) as correction:
            await tasks.edit_delivery(task, evidence)
        assert set(correction.value.issues) == {"items.0.title"}
        task["plan"].update(editor_draft=correction.value.draft, editor_issues=correction.value.issues,
                            frame_reviews=correction.value.frame_reviews, window_reviews=correction.value.window_reviews)
        revised = await tasks.edit_delivery(task, evidence)
        assert revised.items[0].title == "一人站在椅子旁。"
        assert revised._frame_reviews["read:1"]["title_anchor"] == ["座椅旁站着一人"]
    else:
        result = await tasks.edit_delivery(task, evidence)
        assert result._frame_reviews["read:1"]["title_anchor"] == ["座椅旁站着一人"]
        assert result.items[0].title == "椅子旁站着谁？"
        assert result.items[0].rationale == "画面依据：一人站在椅子旁。剪辑安排：用在开场，我会用这一镜引出布景话题。"


async def test_revision_reopens_research_and_recomputes_gaps(runtime, store, monkeypatch, reviewed_window):
    async def synthetic_image(*_):
        return None

    monkeypatch.setattr("tuber_studio.agent.task_tools.clip_review_image", synthetic_image)
    first, second = short_read("a", 11), short_read("b", 35)
    first["accepted_clip_review"] = {"clip": first["result"]["clip"], "usage": "允许讲述剧情或无此限制", "accepted": True}
    old = PrivateDelivery(items=[selection(primary=key).items[0] for key in ["a:1", "b:1"]],
                          gaps=["另一窗口尚未采用。"])
    candidate = old.model_dump()
    goal = "保留第一段原文，另读开场三秒。"
    task = {"revision": 2, "steps": [first, second], "goal": goal, "plan": {
        "candidate": candidate, "prior_goals": ["选两段不剧透的"], "previous_selection": candidate}}
    tasks = create_task_runtime(runtime, store, None)
    from tuber_studio.agent import task_tools

    screen = task_tools.screen_clip_window
    screened = {}

    async def capture(media, goal, generate, budget, inherited):
        screened[media[1]["start"]] = inherited
        return await screen(media, goal, generate, budget, inherited)

    monkeypatch.setattr(task_tools, "screen_clip_window", capture)

    async def editor(messages, schema, **kwargs):
        if "previous_no_spoilers" in schema["properties"]:
            return {"previous_no_spoilers": True}
        if "audience_change" in schema["properties"]:
            return {"audience_change": "继续沿用", "explicit_release_quote": ""}
        packet = json.loads(messages[-1]["content"])
        if "上一版" in packet:
            return {"ordered_actions": ["保留整项原文", "另读新窗口再替换"]}
        if "候选窗口" in packet:
            assert set(packet["候选窗口"]) == {"new"}
            return {"windows": ["new"]}
        if "candidate_primary" in packet:
            assert packet["candidate_primary"] == "new:1" and "另一窗口尚未采用" not in str(packet)
            return {"decision": "采用", "rationale": "以椅子开场。", "primary_frame": "new:1"}
        if "coordinate" in packet:
            return {"title": "新开场", "rationale": "以椅子开场。"}
        if "candidate" in packet:
            assert "另一窗口尚未采用" not in str(packet)
            assert set(packet["evidence"]) == {"new:1", "new:2", "new:3"}
            return selection(primary="new:1").model_dump()
        assert packet["field_visual_sources"]
        return {"supported": True, "field_basis": {p: {"source": keys[0], "observation": "左侧有一把椅子", "claim": "编辑用途"}
                                                   for p, keys in packet["field_visual_sources"].items()}}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    with pytest.raises(AdditionalVideoRead):
        await tasks.edit_delivery(task, evidence_index(task["steps"]))
    task["steps"].extend([short_read("overlap", 12, revision=2), short_read("new", 0, revision=2)])
    evidence = evidence_index(task["steps"])
    selected = await tasks.edit_delivery(task, evidence)
    assert selected.items[0] == old.items[0] and selected.items[1].primary_frame == "new:1"
    assert screened == {11: True, 0: True}
    assert selected.gaps == [] and "另一窗口" not in render_private_delivery(selected, evidence)[1]


@pytest.mark.parametrize("with_overview", [False, True])
async def test_failed_window_after_narrowing_keeps_good_item_and_reads_an_unused_interval(runtime, store, monkeypatch, reviewed_window,
                                                                                        with_overview):
    good, bad = short_read("kept", 7), short_read("bad", 13)
    for step in (good, bad):
        step["result"]["part_duration"] = 20
    saved = selection(primary="kept:1").items[0].model_dump()
    saved["title"] = "首段已核对的原文"
    task = {"goal": f"从 {BV} P2 选两段", "steps": [good, bad], "plan": {
        "prior_goals": [], "requested_video_read": "此前已宽窗转短窗", "retained_candidates": [saved],
        "candidate": selection(primary="bad:1").model_dump()}}
    full_caption = "演播室侧面。右下角有频道水印。屏字写本节目制作团队，画面中央是署名卡。"
    if with_overview:
        overview = reading(frame(8), frame(3), frame(18) | {"text": full_caption})
        task["steps"].append(overview)
        task["plan"]["window_reviews"] = {"bad": {"clip": bad["result"]["clip"], "coverage": {"uncovered": []},
            "observations": [{"basis": "主体为剧情对白，虽有频道水印。"}], "screening_version": WINDOW_REVIEW_VERSION, "accepted": False}}

    async def editor(messages, schema, **kwargs):
        props = schema["properties"]
        if "requested_count" in props:
            return {"requested_count": 2, "usage": "讲解构图", "playable": True, "trace_origin": False}
        if "decision" in props:
            key = json.loads(messages[-1]["content"])["candidate_primary"]
            assert not key.startswith("kept:")
            return {"decision": "另取材料", "primary_frame": None, "rationale": "当前材料不合适。"}
        if "samples" in props:
            packet = json.loads(messages[-1]["content"])
            candidates = packet["untried_observations"]
            assert set(candidates) == {"v:2", "v:3"} and candidates["v:3"]["observation"] == full_caption
            assert packet["rejected_windows"][0]["last_observation"] == "主体为剧情对白，虽有频道水印。"
            return {"samples": {"v:2": {"material": "布景", "priority": "备选"},
                                "v:3": {"material": "片外署名", "priority": "优先补读"}}, "first": "v:3"}
        if "start" in props:
            assert not any(p["minimum"] <= 13 <= p["maximum"] for p in props["start"]["anyOf"])
            return {"read": "kept", "start": 0}
        pytest.fail(f"unexpected call: {list(props)}")

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    with pytest.raises(AdditionalVideoRead) as again:
        await tasks.edit_delivery(task, evidence_index(task["steps"]))
    assert again.value.retained == [saved] and again.value.exclude == ["bad:1"]
    start = 16 if with_overview else 0
    assert again.value.windows == [{"bvid": BV, "part": 2, "start": start, "end": start + 3}]
    repeated = bad | {"id": "same-window-new-call"}
    assert evidence_index([good, bad, repeated]).discovery_progress() == evidence_index([good, bad]).discovery_progress()


@pytest.mark.parametrize(("quote", "unverified_effect"), [("左侧有一把椅子", False), ("河南治水", False), ("左侧有一把椅子", True)])
async def test_saved_clip_omni_decides_each_item_and_displays_attribution(runtime, store, monkeypatch, quote, unverified_effect, reviewed_window):
    read = short_read("window", 0)
    async def no_pixels(*args):
        return None

    monkeypatch.setattr("tuber_studio.agent.task_tools.clip_review_image", no_pixels)
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
        if "requested_count" in schema["properties"]:
            return {"requested_count": 1, "usage": "无剧透画面", "playable": True, "trace_origin": False}
        packet = json.loads(messages[-1]["content"])
        calls.append(packet)
        if "candidate_primary" in packet:
            assert packet["candidate_primary"] == "window:1"
            assert "示例作者" in packet["item_evidence"]["origin"] and "剧情结局" not in str(packet)
            assert "旧的未选用窗口" not in str(packet)
            return {"decision": "采用", "primary_frame": "window:2", "rationale": "从画面左侧的椅子引出布景。"}
        if "coordinate" in packet:
            assert "observations_in_read" not in packet and packet["observation"] == "左侧有一把椅子"
            return {"title": "画中的椅子", "rationale": "能提升点击率。" if unverified_effect else "从画面左侧的椅子引出布景。"}
        assert packet["field_visual_sources"] == dict.fromkeys(["items.0.title", "items.0.rationale"], ["window:2"])
        return {"supported": True, "field_basis": {p: {"source": "window:2", "observation": quote,
            "claim": "无据的事实或成效" if unverified_effect and p.endswith(".rationale") else "编辑用途"}
                                                   for p in packet["field_visual_sources"]}}

    monkeypatch.setattr(runtime.llm, "generate_json", editor)
    tasks = create_task_runtime(runtime, store, None)
    if unverified_effect:
        with pytest.raises(EditorialCorrection) as patch:
            await tasks.edit_delivery(task, evidence)
        assert set(patch.value.issues) == {"items.0.rationale"}
        return
    if quote == "河南治水":
        with pytest.raises(DelegationError, match="回执原文"):
            await tasks.edit_delivery(task, evidence)
        return
    result = await tasks.edit_delivery(task, evidence)
    assert result._field_sources["items.0.title"] == {"source": "window:2", "observation": quote, "claim": "编辑用途"}
    _, body = render_private_delivery(result, evidence)
    assert len(calls) == 3 and "示例作者" in body and "剧情结局" not in body and not result.gaps
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
