"""Exercise the reading entries through the actual visual HTTP message builder."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_archived_video_source import package as package
from test_browsing_evidence import BV, PublicAdapter, config as config, evidence_factory as evidence_factory
from test_video_vision import FRAMES, generations, provider as provider

from tuber_studio.agent.public_video import read_public_part
from tuber_studio.agent.task_tools import Video


def messages(state):
    bodies = (json.loads(request.content) for request in generations(state))
    return [body["messages"] for body in bodies if body["max_tokens"] > 1]


def track(cid, rows, **fields):
    return {"bvid": BV, "cid": cid, "track_id": "1", "language": "zh", "is_ai": False,
            "segments": [{"id": str(i), "start": t, "end": t + 1, "text": text}
                         for i, (t, text) in enumerate(rows)], **fields}


@pytest.mark.parametrize("provider", [{"frame_strategy": "sequential"}, {"frame_strategy": "joint"}], indirect=True)
async def test_browsing_cc_followup_context_at_model_transport(provider, evidence_factory, config):
    vision, state = provider
    adapter = PublicAdapter()
    adapter.metadata.update(owner={"name": "旧版作者", "private": "无关字段"}, description="简介起点" + "长" * 500 + "简介末尾")
    adapter.frames = [f | {"cid": "456"} for f in FRAMES]
    adapter.subtitle_rows = {
        "456": track("456", [(10, "第一处台词"), (30, "第二处台词"), (59, "远处字幕")]),
        "457": track("457", [(10, "别的分P同秒")]),
    }
    if vision.config.frame_strategy == "joint":
        state["result"]["captions"] = ["红色圆形", "蓝色方形"]
    service = evidence_factory(adapter, vision)
    _, original = await service.prepare(BV, config)
    assert not messages(state)
    _, watched = await service.prepare(BV, config, visual_source_ids=["456:0", "456:1"])
    assert watched["parts"][:2] == original["parts"]
    assert watched["parts"][-1]["visual"]
    assert [call[0] for call in adapter.calls] == ["metadata", "subtitles", "subtitles", "frames"]
    sent = messages(state)
    rendered = [json.dumps(item, ensure_ascii=False) for item in sent]
    if vision.config.frame_strategy == "joint":
        assert len(sent) == 1
        assert sum(item["type"] == "image_url" for item in sent[0][-1]["content"]) == 2
    else:
        assert len(sent) == 3
        assert "第一处台词" in rendered[0] and "第二处台词" not in rendered[0]
        assert "第二处台词" in rendered[1] and "第一处台词" not in rendered[1]
        assert "observations" in rendered[2]
    for value in rendered:
        for token in (BV, "海底观察", "简介起点", "旧版作者", "第一P", "科普"):
            assert value.count(token) == 1
        assert "平台字幕" in value and "画面所见须以图像为据" in value
        assert all(token not in value for token in ("简介末尾", "无关字段", "别的分P同秒", "第二P", "远处字幕"))
    assert all(rendered[-1].count(token) == 1 for token in ("第一处台词", "第二处台词"))


async def test_precise_cache_is_scoped_to_part_text_and_reuses_another_parts_edits(provider, evidence_factory):
    vision, state = provider
    state["result"]["used_frames"] = [1]
    adapter = PublicAdapter()
    reads = []

    async def stream(metadata, *, cid, timestamp):
        reads.append(timestamp)
        return FRAMES[0] | {"cid": cid, "kind": "stream", "presentation_end": 11}

    adapter.video_stream_frame = stream
    service = evidence_factory(adapter, vision)
    meta = adapter.metadata
    tracks = [track("456", [(10, "本地识别")], asr=True), track("457", [(10, "旁P旧")]),
              track("456", [(10, "错误BV")], bvid="BV1aa1234567")]

    async def inspect():
        parts, _ = await service.inspect_frame(meta, {}, None, "456", 10.5, text_tracks=tracks)
        assert parts

    await inspect()
    first = json.dumps(messages(state), ensure_ascii=False)
    assert "本地ASR" in first and "本地识别" in first and "错误BV" not in first and "旁P旧" not in first
    tracks[1] = track("457", [(10, "旁P新")])
    await inspect()
    assert len(reads) == 1 and len(messages(state)) == 2
    tracks[0] = track("456", [(10, "更正识别")], asr=True)
    await inspect()
    meta["owner"] = {"name": "新UP"}
    await inspect()
    assert len(reads) == 3 and len(messages(state)) == 6
    last = json.dumps(messages(state)[-1], ensure_ascii=False)
    assert "新UP" in last and "更正识别" in last and "本地识别" not in last


async def test_browsing_uses_available_timed_asr_when_cc_is_missing(provider, evidence_factory, config):
    vision, state = provider
    state["result"]["used_frames"] = [1]
    adapter = PublicAdapter()
    adapter.failures["456"] = "no_subtitles"
    adapter.frames = [FRAMES[0] | {"cid": "456"}]
    service = evidence_factory(adapter, vision)
    calls = []

    async def transcribe(metadata, part, *, deadline):
        calls.append(part["cid"])
        return {"bvid": metadata["bvid"], "cid": part["cid"], "asr": True,
                "segments": [{"id": "456:asr:1", "start": 9, "end": 11, "text": "可用音轨"}]}

    service.asr = SimpleNamespace(enabled=True, transcribe=transcribe)
    _, watched = await service.prepare(BV, config, visual_source_ids=["456:asr:1"])
    assert calls == ["456"] and watched["parts"][-1]["visual"]
    assert len(messages(state)) == 2
    for item in messages(state):
        value = json.dumps(item, ensure_ascii=False)
        assert value.count("本地ASR") == value.count("可用音轨") == 1
        assert "章鱼有三个心脏" not in value


async def test_fresh_metadata_prevents_reuse_of_older_visual_followup(provider, evidence_factory, config):
    vision, state = provider
    state["result"]["used_frames"] = [1]
    adapter = PublicAdapter()
    adapter.frames[0]["mime_type"] = "image/png"
    service = evidence_factory(adapter, vision)
    target = ["456:1:0"]
    await service.prepare(BV, config, visual_source_ids=target)
    adapter.metadata["owner"] = {"name": "更新的署名"}
    await service.prepare(BV, config, force=True)
    _, fresh = await service.prepare(BV, config, visual_source_ids=target)
    assert len(messages(state)) == 4 and fresh["parts"][-1]["visual"]
    assert "更新的署名" in messages(state)[-1][-1]["content"]
    await service.prepare(BV, config, visual_source_ids=target)
    assert len(messages(state)) == 4


async def test_private_archive_same_pixels_with_and_without_optional_text(package, provider):
    vision, state = provider
    state["result"]["used_frames"] = [1]
    selection = Video(bvid=package.source["bvid"], part=2, start=20, end=21)
    package.source.update(upload_title="《雍正王朝》精讲合集", part_title="第2集")

    async def read():
        state["requests"].clear()
        result = await read_public_part(SimpleNamespace(id=str(uuid4())), selection, vision, lambda: True, archive=package)
        assert len([row for row in result["findings"] if row["kind"] == "实际视频帧"]) == 3
        sent = messages(state)
        assert len(sent) == 6
        for item in sent:
            text = json.dumps(item, ensure_ascii=False)
            assert all(text.count(token) == 1 for token in ("《雍正王朝》精讲合集", "第2集", package.source["uploader"]))
        return sent

    sparse = await read()
    assert "简介" not in str(sparse) and "归档字幕" not in str(sparse)
    archive_metadata = package.video_metadata

    async def metadata(bvid):
        return await archive_metadata(bvid) | {"description": "后来取得的简介"}

    async def subtitles(bvid, cid):
        return {"bvid": bvid, "cid": cid, "segments": [
            {"start": 20.1, "end": 20.9, "text": "窗口内字幕"},
            {"start": 19, "end": 20, "text": "授权窗口外"}]}

    package.video_metadata, package.subtitles = metadata, subtitles
    full = await read()
    for before, after in zip(sparse[::2], full[::2], strict=True):
        assert before[0]["content"][1] == after[0]["content"][1]
    for item in full:
        text = json.dumps(item, ensure_ascii=False)
        assert "后来取得的简介" in text and "窗口内字幕" in text and "授权窗口外" not in text
