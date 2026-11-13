"""Public evidence storage, shared preparation and optional visual contracts."""

import asyncio
import base64
import copy
import json
import time

import httpx
import pytest
import pytest_asyncio

from tuber_studio.operations.browsing.evidence import BrowsingEvidence, EvidenceUnavailable
from tuber_studio.operations.browsing.models import BrowsingPolicy, DisabledVideoEvidence
from tuber_studio.platforms.bilibili_browsing import BrowsingAdapter, PlatformReadError

BV = "BV1d8pU6xEZX"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jHtkAAAAASUVORK5CYII="
)


class PublicAdapter:
    def __init__(self):
        self.calls = []
        self.metadata = {
            "bvid": BV,
            "aid": 123,
            "title": "海底观察",
            "description": "海底生物公开资料",
            "tags": ["科普"],
            "owner_uid": "77",
            "owner": "作者",
            "duration": 120,
            "published_at": 200,
            "category_id": 1,
            "parts": [
                {"cid": "456", "title": "第一P", "duration": 60},
                {"cid": "457", "title": "第二P", "duration": 60},
            ],
        }
        self.failures = {}
        self.subtitle_rows = {}
        self.metadata_gate = None
        self.subtitle_gate = None
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.frames = [
            {
                "id": "first",
                "cid": "456",
                "requested_time": 10,
                "actual_time": 8,
                "width": 1,
                "height": 1,
                "kind": "storyboard",
                "image": PNG,
            }
        ]

    async def video_metadata(self, video):
        self.calls.append(("metadata", video))
        if self.metadata_gate is not None:
            await self.metadata_gate.wait()
        return copy.deepcopy(self.metadata) | {"fetched_at": time.time()}

    async def subtitles(self, video, cid, languages):
        self.calls.append(("subtitles", video, cid, list(languages)))
        self.entered.set()
        try:
            if self.subtitle_gate is not None:
                await self.subtitle_gate.wait()
            if cid in self.failures:
                raise PlatformReadError(self.failures[cid], "分类后的平台失败")
            return copy.deepcopy(
                self.subtitle_rows.get(
                    cid,
                    {
                        "bvid": BV,
                        "cid": cid,
                        "track_id": "1",
                        "language": "zh",
                        "is_ai": cid == "457",
                        "segments": [{"id": f"{cid}:1:0", "start": 1, "end": 2, "text": "章鱼有三个心脏"}],
                        "coverage": {"complete": True, "start": 1, "end": 2, "segment_count": 1},
                        "fetched_at": time.time(),
                    },
                )
            )
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def video_frames(self, metadata, count, *, cid=None, timestamps=None, windows=None):
        self.calls.append(("frames", count, cid, timestamps, windows))
        frames = [frame for frame in self.frames if cid is None or frame["cid"] == cid or frame["cid"] == "999"]
        if windows:
            frames = [frame for frame in frames if not isinstance(frame["actual_time"], (int, float))
                      or not 0 <= frame["actual_time"] <= next(part["duration"] for part in metadata["parts"] if part["cid"] == cid)
                      or any(start <= frame["actual_time"] <= end for start, end in windows)]
        return copy.deepcopy(frames[:count])


class TestVisual:
    __test__ = False

    def __init__(self):
        self.calls = 0
        self.cancelled = []
        self.started = asyncio.Event()
        self.gate = None
        self.task = None
        self.frames = None
        self.returned = {}
        self.closed = False
        self.cancel_error = None
        self.cap = {
            "provider_id": "controlled-test",
            "version": "1",
            "available": True,
            "images": True,
            "multiple_images": True,
            "admitted": True,
            "max_frames": 6,
            "max_bytes": 4096,
            "max_pixels": 100,
            "context_tokens": 4096,
        }

    def capabilities(self):
        return self.cap

    async def analyze_video_evidence(self, task, metadata, frames, question, *, deadline):
        self.calls += 1
        self.task, self.frames = task, frames
        self.deadline = deadline
        self.received = copy.deepcopy(frames)
        self.started.set()
        if self.gate:
            await self.gate.wait()
        return {
            "observations": "所列画面里可见珊瑚",
            "evidence_ids": [frame["id"] for frame in frames],
            "frame_observations": {frame["id"]: "所列画面里可见珊瑚" for frame in frames},
            "provider_id": "controlled-test",
            "version": "1",
            "limitations": ["仅抽样帧，不推断动作与声音"],
        } | self.returned

    async def cancel(self, task_id):
        self.cancelled.append(task_id)
        if self.cancel_error:
            raise self.cancel_error

    async def close(self):
        self.closed = True


async def reader(method, *args):
    return await method(*args)


@pytest.fixture
def config():
    return BrowsingPolicy().model_dump()


@pytest_asyncio.fixture
async def evidence_factory(runtime):
    instances = []

    def create(adapter=None, visual=None):
        service = BrowsingEvidence(runtime, adapter or PublicAdapter(), reader, visual)
        instances.append(service)
        return service

    yield create
    for instance in instances:
        await instance.close()


async def test_cache_persists_raw_parts_and_same_material_is_shared_across_oc(
    evidence_factory, runtime, config
):
    adapter = PublicAdapter()
    service = evidence_factory(adapter)
    identity, material = await service.prepare(BV, config, task={"id": "oc-first"})
    other_character = runtime.store.save_character(runtime.character | {"id": "evidence-other-oc"})
    runtime.store.activate(other_character["id"])
    second, loaded = await service.prepare(BV, config, task={"id": "oc-other"})
    assert second == identity and loaded == material and service.hits == 1
    assert len(adapter.calls) == 3
    assert material["parts"][0]["segments"][0]["id"] == "456:1:0"
    assert material["parts"][1]["is_ai"] is True and material["coverage"] == "all_available_cc"
    raw = (service.directory / f"{identity}.json").read_bytes()
    assert json.loads(raw) == material
    assert b"oc-first" not in raw and b"oc-other" not in raw
    assert runtime.cache.status()["settings"]["music_limit_mib"] == 15 * 1024
    assert runtime.cache.usage("browsing")["bytes"] == len(raw)


async def test_real_adapter_normalization_connects_to_persistent_evidence(evidence_factory, config):
    requested = []
    raw_metadata = {
        "bvid": BV,
        "aid": 123,
        "title": "海底观察",
        "desc": "视频完整简介",
        "owner": {"mid": 77, "name": "作者"},
        "duration": 120,
        "pages": [
            {"cid": 456, "part": "人工字幕P", "duration": 60},
            {"cid": 457, "part": "AI字幕P", "duration": 60},
        ],
    }

    def handle(request):
        requested.append(request)
        path = request.url.path
        data = None
        if path == "/x/web-interface/view":
            data = raw_metadata
        elif path == "/x/web-interface/view/detail/tag":
            data = [{"tag_name": "科普"}]
        elif path == "/x/web-interface/nav":
            data = {
                "mid": 9,
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                },
            }
        elif path == "/x/player/wbi/v2":
            cid = int(request.url.params["cid"])
            data = {
                "cid": cid,
                "subtitle": {
                    "subtitles": [
                        {
                            "id": 7,
                            "lan": "zh",
                            "type": cid == 457,
                            "subtitle_url": f"https://i0.hdslb.com/subtitle-{cid}.json",
                        }
                    ]
                },
            }
        elif request.url.host == "i0.hdslb.com":
            assert "cookie" not in request.headers
            return httpx.Response(200, json={"body": [{"from": 1, "to": 2, "content": "章鱼有三个心脏"}]})
        assert data is not None, str(request.url)
        return httpx.Response(200, json={"code": 0, "data": data})

    adapter = BrowsingAdapter({"sessdata": "private-session", "csrf": "private-csrf"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handle),
        cookies={"SESSDATA": "private-session"},
    )
    adapter._read_spacing = 0
    try:
        service = evidence_factory(adapter)
        identity, material = await service.prepare(BV, config)
        assert [part["is_ai"] for part in material["parts"]] == [False, True]
        assert [part["segments"][0]["id"] for part in material["parts"]] == ["456:7:0", "457:7:0"]
        assert service.load(identity)["source_version"] == material["source_version"]
        assert all(request.method == "GET" for request in requested)
    finally:
        await adapter.close()


async def test_force_refresh_changes_cache_identity_but_not_source_version(evidence_factory, config):
    service = evidence_factory()
    first, before = await service.prepare(BV, config)
    second, after = await service.prepare(BV, config, force=True)
    assert first != second and before["source_version"] == after["source_version"]
    service.adapter.metadata["description"] = "视频简介已更改"
    _, changed = await service.prepare(BV, config, force=True)
    assert changed["source_version"] != before["source_version"]


async def test_single_flight_survives_one_cancelled_waiter_and_collects_children(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.subtitle_gate = asyncio.Event()
    service = evidence_factory(adapter)
    first = asyncio.create_task(service.prepare(BV, config, task={"id": "first-oc"}))
    await adapter.entered.wait()
    second = asyncio.create_task(service.prepare(BV, config, task={"id": "other-oc"}))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert not adapter.cancelled.is_set()
    adapter.subtitle_gate.set()
    identity, material = await second
    assert identity and material["parts"]
    assert len([call for call in adapter.calls if call[0] == "metadata"]) == 1
    assert len([call for call in adapter.calls if call[0] == "subtitles"]) == 2
    assert service.merged >= 2 and not service.pending and not service._waiters


async def test_last_cancelled_waiter_cancels_and_collects_underlying_read(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.subtitle_gate = asyncio.Event()
    service = evidence_factory(adapter)
    parent = asyncio.create_task(service.prepare(BV, config))
    await adapter.entered.wait()
    parent.cancel()
    await asyncio.gather(parent, return_exceptions=True)
    assert adapter.cancelled.is_set() and not service.pending and not service._waiters
    assert not list(service.directory.iterdir())


async def test_cancel_pending_collects_all_shared_reads(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.subtitle_gate = asyncio.Event()
    service = evidence_factory(adapter)
    parents = [asyncio.create_task(service.prepare(BV, config)) for _ in range(2)]
    await adapter.entered.wait()
    await service.cancel_pending()
    results = await asyncio.gather(*parents, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert adapter.cancelled.is_set() and not service.pending and not service._waiters


@pytest.mark.parametrize(
    "patch",
    [
        {"exclude_words": ["公开资料"]},
        {"excluded_author_uids": ["77"]},
        {"allowed_categories": ["3"]},
        {"max_duration_seconds": 30},
        {"published_after": 300},
    ],
)
async def test_complete_metadata_exclusions_happen_before_any_cc_read(evidence_factory, config, patch):
    service = evidence_factory()
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | patch)
    assert caught.value.kind == "filtered"
    assert service.adapter.calls == [("metadata", BV)]


async def test_excluded_caller_does_not_cancel_another_oc_public_read(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.metadata_gate = asyncio.Event()
    service = evidence_factory(adapter)
    excluded = asyncio.create_task(service.prepare(BV, config | {"exclude_words": ["海底"]}))
    allowed = asyncio.create_task(service.prepare(BV, config))
    await asyncio.sleep(0)
    adapter.metadata_gate.set()
    outcomes = await asyncio.gather(excluded, allowed, return_exceptions=True)
    assert isinstance(outcomes[0], EvidenceUnavailable) and outcomes[0].kind == "filtered"
    assert isinstance(outcomes[1], tuple)
    assert len(adapter.calls) == 3


async def test_new_exclusions_apply_even_when_raw_evidence_was_already_cached(evidence_factory, config):
    service = evidence_factory()
    await service.prepare(BV, config)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | {"exclude_words": ["科普"]})
    assert caught.value.kind == "filtered" and len(service.adapter.calls) == 3


async def test_partial_multi_part_cc_does_not_claim_full_coverage(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.failures["457"] = "no_subtitles"
    service = evidence_factory(adapter)
    _, material = await service.prepare(BV, config)
    assert material["coverage"] == "partial"
    assert [part["cid"] for part in material["parts"]] == ["456"]
    assert material["missing_parts"] == [{"cid": "457", "reason": "no_subtitles"}]


@pytest.mark.parametrize("failure", ["permission", "restricted", "network", "invalid"])
async def test_missing_subtitle_part_keeps_verified_other_sources(evidence_factory, config, failure):
    adapter = PublicAdapter()
    adapter.failures["456"] = failure
    visual = TestVisual()
    service = evidence_factory(adapter, visual)
    _, material = await service.prepare(BV, config)
    assert material["missing_parts"] == [{"cid": "456", "reason": failure}]
    assert {part["cid"] for part in material["parts"] if not part.get("visual")} == {"457"}
    assert service.material_mode(material) == "subtitle_visual" and visual.calls == 1


@pytest.mark.parametrize("source", ["cid", "credits", "empty"])
async def test_invalid_cid_empty_and_credit_only_material_never_become_evidence(
    evidence_factory, config, source
):
    adapter = PublicAdapter()
    for cid in ("456", "457"):
        adapter.subtitle_rows[cid] = {
            "bvid": BV,
            "cid": "999" if source == "cid" else cid,
            "segments": []
            if source == "empty"
            else [
                {
                    "id": "line",
                    "start": 0,
                    "end": 1,
                    "text": "字幕by制作人" if source == "credits" else "正文",
                }
            ],
        }
    service = evidence_factory(adapter)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config)
    assert caught.value.kind == ("invalid" if source == "cid" else "empty")


async def test_raw_material_larger_than_two_mib_is_rejected_without_truncation(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.metadata["description"] = "正文" * 400000
    service = evidence_factory(adapter)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config)
    assert caught.value.kind == "too_large" and not list(service.directory.iterdir())


async def test_cache_cleanup_invalidates_notes_and_preserves_pending_draft_evidence(
    evidence_factory, runtime, config
):
    service = evidence_factory()
    identity, _ = await service.prepare(BV, config)
    runtime.cache.leases.clear()
    runtime.store.execute(
        "INSERT INTO browsing_intents(id,status,material_keys) VALUES('pending','pending_review',?)",
        (json.dumps([identity]),),
    )
    result = await runtime.cache.clear("browsing")
    assert result["removed_files"] == 0 and service.load(identity)
    runtime.store.execute("UPDATE browsing_intents SET status='rejected' WHERE id='pending'")
    result = await runtime.cache.clear("browsing")
    assert result["removed_files"] == 1
    with pytest.raises(EvidenceUnavailable) as caught:
        service.load(identity)
    assert caught.value.kind == "cleared"
    assert runtime.store.one("SELECT id FROM browsing_intents WHERE id='pending'")


async def test_material_formatting_does_not_invalidate_the_source(evidence_factory, runtime, config):
    service = evidence_factory()
    identity, material = await service.prepare(BV, config)
    path = service.directory / f"{identity}.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert service.load(identity) == material
    path.write_bytes(b"{")
    with pytest.raises(EvidenceUnavailable) as changed:
        service.load(identity)
    assert changed.value.kind == "changed"
    runtime.store.execute("UPDATE browsing_materials SET expires_at=0 WHERE id=?", (identity,))
    with pytest.raises(EvidenceUnavailable) as expired:
        service.load(identity)
    assert expired.value.kind == "expired"


async def no_cc_adapter():
    adapter = PublicAdapter()
    adapter.failures = {"456": "no_subtitles", "457": "no_subtitles"}
    return adapter


async def test_visual_default_is_unavailable_and_never_creates_fake_evidence(evidence_factory, config):
    service = evidence_factory(await no_cc_adapter())
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | {"visual_enabled": True})
    assert caught.value.kind == "no_subtitles" and caught.value.missing_material
    assert not list(service.directory.iterdir())


async def test_cc_and_real_frames_are_used_without_resource_admission(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.failures["456"] = "no_subtitles"
    visual = TestVisual()
    visual.cap.update(available=False, admitted=False)
    service = evidence_factory(adapter, visual)
    _, material = await service.prepare(BV, config)
    assert service.material_mode(material) == "subtitle_visual" and visual.calls == 1
    assert material["visual_frames"][0]["actual_time"] == 8


@pytest.mark.parametrize("has_cc", [True, False])
async def test_visual_failure_preserves_cc_or_marks_material_unavailable(evidence_factory, config, has_cc):
    visual = TestVisual()
    visual.cancel_error = ValueError("视觉接口暂时无法返回结果")
    service = evidence_factory(PublicAdapter() if has_cc else await no_cc_adapter(), visual)
    if has_cc:
        _, material = await service.prepare(BV, config, visual_source_ids=["456:1:0"])
        assert service.material_mode(material) == "subtitle"
        assert "暂时无法返回结果" in material["visual_error"]
    else:
        with pytest.raises(EvidenceUnavailable, match="取材失败.*暂时无法返回结果") as error:
            await service.prepare(BV, config)
        assert error.value.missing_material


async def test_no_caption_fallback_distributes_bounded_storyboard_windows(evidence_factory, config):
    adapter = await no_cc_adapter()
    adapter.metadata["parts"][0]["duration"] = 300
    service = evidence_factory(adapter, TestVisual())
    _, material = await service.prepare(BV, config | {"frame_sampling": "adaptive"})
    batches = [call[4] for call in adapter.calls if call[0] == "frames" and call[2] == "456"]
    windows = [window for batch in batches for window in batch]
    assert max(map(len, batches)) == 6 and len(windows) == 25
    assert windows[0][0] == 0 and windows[-1][1] == 300
    assert all(10 <= end - start <= 15 for start, end in windows)
    scan = next(scan["windows"] for scan in material["visual_scan"] if scan["cid"] == "456")
    assert scan[0]["samples"] == [8] and scan[1]["state"] == "gap"


async def test_opt_in_local_asr_is_partial_audio_evidence_not_cc(evidence_factory, config, monkeypatch):
    import wave
    from unittest.mock import AsyncMock

    monkeypatch.setenv("TUBER_VIDEO_ASR_CLI", "whisper-cli")
    service = evidence_factory(await no_cc_adapter())
    model = service.directory / "stub.gguf"
    model.write_bytes(b"test identity")
    service.asr.model = str(model)
    service.asr.directory.mkdir()
    with wave.open(str(service.asr.directory / f"{BV}-456.wav"), "wb") as clip:
        clip.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        clip.writeframes(bytes(32000))
    run = AsyncMock(return_value=[{"id": "456:asr:1", "start": 0.1, "end": 0.5, "text": "现场说话"}])
    monkeypatch.setattr(service.asr, "_read", run)
    _, material = await service.prepare(BV, config)
    assert material["coverage"] == "sampled_audio"
    assert material["parts"][0]["asr"] and not material["parts"][0]["coverage"]["complete"]
    assert material["parts"][0]["track_id"].startswith("local-asr:")
    assert {row["cid"] for row in material["missing_parts"]} == {"456", "457"}
    assert not any(call[0] == "frames" for call in service.adapter.calls)
    assert run.await_count == 1


async def test_visual_references_actual_times_and_each_part_without_claiming_full_view(
    evidence_factory, config
):
    adapter = await no_cc_adapter()
    adapter.frames.append(dict(adapter.frames[0], id="second", cid="457", requested_time=35, actual_time=31))
    visual = TestVisual()
    service = evidence_factory(adapter, visual)
    identity, material = await service.prepare(
        BV, config | {"visual_enabled": True}, task={"id": "visual-task"}
    )
    assert [(part["cid"], part["segments"][0]["start"]) for part in material["parts"]] == [
        ("456", 8),
        ("457", 31),
    ]
    assert all(not part["coverage"]["complete"] for part in material["parts"])
    assert all(part["segments"][0]["text"] for part in material["parts"])
    assert material["coverage"] == "sampled_visual"
    assert base64.b64decode(material["visual_frames"][0]["image_base64"]) == PNG
    assert material["parts"][0]["visual_provider"] == {"provider_id": "controlled-test", "version": "1"}
    assert (
        set(visual.cancelled) == {"visual-task"} and visual.task["cancel_signal"].is_set() and visual.frames == []
    )
    assert service.load(identity)["visual_frames"]


async def test_visual_source_version_tracks_actual_frame_time_not_requested_time(evidence_factory, config):
    adapter = await no_cc_adapter()
    service = evidence_factory(adapter, TestVisual())
    _, first = await service.prepare(BV, config | {"visual_enabled": True})
    adapter.frames[0]["requested_time"] = 25
    _, same = await service.prepare(BV, config | {"visual_enabled": True}, force=True)
    assert same["source_version"] == first["source_version"]
    adapter.frames[0]["actual_time"] = 12
    _, changed = await service.prepare(BV, config | {"visual_enabled": True}, force=True)
    assert changed["source_version"] != first["source_version"]


@pytest.mark.parametrize(
    "modification",
    [
        {"blurred": True},
        {"sufficient": False},
        {"sufficient": "true"},
        {"evidence_ids": ["invented"]},
        {"provider_id": "another"},
        {"frame_observations": {"invented": "珊瑚"}},
    ],
)
async def test_uncertain_visual_results_do_not_become_evidence(evidence_factory, config, modification):
    visual = TestVisual()
    visual.returned = modification
    service = evidence_factory(await no_cc_adapter(), visual)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | {"visual_enabled": True}, task={"id": "uncertain"})
    assert caught.value.kind == "insufficient_visual" and visual.cancelled == ["uncertain"]
    assert visual.frames == [] and not list(service.directory.iterdir())


@pytest.mark.parametrize(
    "modification",
    [
        {"observations": "可见珊瑚，背景模糊，小字看不清。"},
        {"limitations": ["看不清背景的小字；cannot determine the small label"]},
        {"frame_observations": {"first": "珊瑚清楚可见，后方物体不可辨。"}},
        {"blurred": True},
    ],
)
async def test_answerable_visual_observations_keep_local_limitations(evidence_factory, config, modification):
    visual = TestVisual()
    visual.returned = {"sufficient": True} | modification
    service = evidence_factory(await no_cc_adapter(), visual)
    identity, material = await service.prepare(BV, config | {"visual_enabled": True})
    assert service.load(identity) == material
    assert (
        material["parts"][0]["segments"][0]["text"]
        == modification.get("frame_observations", {"first": "所列画面里可见珊瑚"})["first"]
    )
    assert material["visual_frames"]


@pytest.mark.parametrize(
    "frame_patch",
    [
        {"actual_time": None},
        {"actual_time": -1},
        {"actual_time": 70},
        {"cid": "999"},
        {"kind": "cover"},
        {"image": b"not-an-image"},
        {"width": 1000},
    ],
)
async def test_invalid_frames_are_rejected_and_released(evidence_factory, config, frame_patch):
    adapter = await no_cc_adapter()
    adapter.frames[0].update(frame_patch)
    visual = TestVisual()
    service = evidence_factory(adapter, visual)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | {"visual_enabled": True}, task={"id": "bad-frame"})
    assert caught.value.kind == "invalid_frames" and visual.calls == 0
    assert visual.cancelled == ["bad-frame"]


async def test_visual_cancellation_collects_provider_and_releases_frames(evidence_factory, config):
    visual = TestVisual()
    visual.gate = asyncio.Event()
    service = evidence_factory(await no_cc_adapter(), visual)
    task = asyncio.create_task(
        service.prepare(BV, config | {"visual_enabled": True}, task={"id": "cancelled"})
    )
    await visual.started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert visual.cancelled == ["cancelled"] and visual.task["cancel_signal"].is_set()
    assert not service.pending and visual.frames == []


async def test_visual_deadline_cancels_provider_without_cached_observations(evidence_factory, config):
    visual = TestVisual()
    visual.gate = asyncio.Event()
    service = evidence_factory(await no_cc_adapter(), visual)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | {"visual_enabled": True}, deadline=time.time() + 0.03)
    assert caught.value.kind == "deadline" and visual.cancelled == [""]
    assert visual.frames == [] and not list(service.directory.iterdir())


@pytest.mark.parametrize("parent", [None, 120])
async def test_visual_preparation_forwards_only_explicit_parent_deadline(evidence_factory, config, parent):
    visual = TestVisual()
    service = evidence_factory(await no_cc_adapter(), visual)
    deadline = time.time() + parent if parent is not None else None
    await service.prepare(BV, config, deadline=deadline)
    assert visual.deadline == deadline


async def test_platform_frame_request_has_its_own_timeout(evidence_factory, config):
    adapter = PublicAdapter()
    visual = TestVisual()
    visual.cap["timeout_seconds"] = 0.03

    async def stalled_frames(*args):
        await asyncio.Event().wait()

    adapter.video_frames = stalled_frames
    service = evidence_factory(adapter, visual)
    _, material = await service.prepare(BV, config, visual_source_ids=["456:1:0"])
    assert material["visual_error"] == "读取视频画面超时"
    assert service.material_mode(material) == "subtitle" and visual.calls == 0


async def test_provider_cleanup_failure_still_releases_inline_frames(evidence_factory, config):
    visual = TestVisual()
    visual.cancel_error = ValueError("Provider cleanup failed")
    service = evidence_factory(await no_cc_adapter(), visual)
    with pytest.raises(ValueError, match="cleanup failed"):
        await service.prepare(BV, config | {"visual_enabled": True})
    assert visual.frames == [] and not service.pending and not list(service.directory.iterdir())


async def test_disabling_visual_cannot_reuse_previous_visual_only_cache(evidence_factory, config):
    service = evidence_factory(await no_cc_adapter(), TestVisual())
    await service.prepare(BV, config | {"visual_enabled": True})
    service.visual = DisabledVideoEvidence()
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config)
    assert caught.value.kind == "no_subtitles"


async def test_multi_frame_legacy_summary_cannot_be_copied_to_several_times(evidence_factory, config):
    adapter = await no_cc_adapter()
    adapter.frames.append(dict(adapter.frames[0], id="second", actual_time=22))
    visual = TestVisual()
    visual.returned = {"frame_observations": None}
    service = evidence_factory(adapter, visual)
    with pytest.raises(EvidenceUnavailable) as caught:
        await service.prepare(BV, config | {"visual_enabled": True})
    assert caught.value.kind == "insufficient_visual" and not list(service.directory.iterdir())


async def test_single_frame_legacy_summary_still_maps_to_its_actual_time(evidence_factory, config):
    visual = TestVisual()
    visual.returned = {"frame_observations": None}
    service = evidence_factory(await no_cc_adapter(), visual)
    _, material = await service.prepare(BV, config | {"visual_enabled": True})
    assert material["parts"][0]["segments"][0]["start"] == 8
    assert material["parts"][0]["segments"][0]["text"] == "所列画面里可见珊瑚"


async def test_requested_visual_appends_to_cached_cc_without_refetching_originals(evidence_factory, config):
    adapter, visual = PublicAdapter(), TestVisual()
    service = evidence_factory(adapter, visual)
    enabled = config | {"visual_enabled": True}
    first_key, first = await service.prepare(BV, enabled)
    first_expiry = service.store.one("SELECT expires_at FROM browsing_materials WHERE id=?", (first_key,))[
        "expires_at"
    ]
    assert visual.calls == 0
    adapter.frames[0].update(id="second", actual_time=10)
    target = ["456:1:0"]
    key, material = await service.prepare(BV, enabled, visual_source_ids=target, task={"id": "followup"})
    assert key != first_key and visual.calls == 1
    assert (
        service.store.one("SELECT expires_at FROM browsing_materials WHERE id=?", (key,))["expires_at"]
        == first_expiry
    )
    assert material["parts"][:2] == first["parts"]
    assert material["parts"][2]["visual_provider"] == {"provider_id": "controlled-test", "version": "1"}
    assert material["parts"][2]["segments"][0]["start"] == 10
    assert len([call for call in adapter.calls if call[0] == "subtitles"]) == 2
    assert len([call for call in adapter.calls if call[0] == "metadata"]) == 1
    cached, _ = await service.prepare(BV, enabled, visual_source_ids=target)
    assert cached == key and visual.calls == 1


async def test_followup_and_initial_visual_requests_keep_distinct_targets(evidence_factory, config):
    adapter, visual = PublicAdapter(), TestVisual()
    adapter.subtitle_gate = asyncio.Event()
    service = evidence_factory(adapter, visual)
    enabled = config | {"visual_enabled": True}
    ordinary = asyncio.create_task(service.prepare(BV, enabled))
    await adapter.entered.wait()
    requested = asyncio.create_task(service.prepare(BV, enabled, visual_source_ids=["456:1:0"]))
    await asyncio.sleep(0)
    adapter.subtitle_gate.set()
    plain, augmented = await asyncio.gather(ordinary, requested)
    assert not any(part.get("visual") for part in plain[1]["parts"])
    assert any(part.get("visual") for part in augmented[1]["parts"])
    assert visual.calls == 1 and augmented[1]["visual_source_ids"] == ["456:1:0"]


async def test_other_provider_visual_is_not_reused_but_original_cc_is_shared(evidence_factory, config):
    adapter, visual = PublicAdapter(), TestVisual()
    service = evidence_factory(adapter, visual)
    enabled = config | {"visual_enabled": True}
    target = ["456:1:0"]
    _, first = await service.prepare(BV, enabled, visual_source_ids=target)
    visual.cap["version"] = "2"
    visual.returned = {"version": "2", "frame_observations": {"first": "新模型看到的是透明容器"}}
    _, current = await service.prepare(BV, enabled, visual_source_ids=target)
    assert current["parts"][:2] == first["parts"][:2]
    assert visual.calls == 2
    assert current["parts"][-1]["segments"][0]["text"] == "新模型看到的是透明容器"
    assert current["parts"][-1]["visual_provider"]["version"] == "2"
    assert len([call for call in adapter.calls if call[0] == "subtitles"]) == 2


async def test_visual_only_cache_is_reobserved_after_provider_changes(evidence_factory, config):
    visual = TestVisual()
    service = evidence_factory(await no_cc_adapter(), visual)
    enabled = config | {"visual_enabled": True}
    first, _ = await service.prepare(BV, enabled)
    visual.cap["provider_id"] = "new-model"
    visual.returned = {"provider_id": "new-model"}
    current, material = await service.prepare(BV, enabled)
    assert current != first and visual.calls == 2
    assert all(part["visual_provider"]["provider_id"] == "new-model" for part in material["parts"])


@pytest.mark.parametrize("cancel", [True, False])
async def test_cc_visual_followup_cancel_or_deadline_collects_children(evidence_factory, config, cancel):
    visual = TestVisual()
    adapter = PublicAdapter()
    service = evidence_factory(adapter, visual)
    enabled = config | {"visual_enabled": True}
    original, _ = await service.prepare(BV, enabled)
    adapter.frames[0].update(id="second", actual_time=10)
    visual.started.clear()
    visual.cancelled.clear()
    visual.gate = asyncio.Event()
    task = asyncio.create_task(
        service.prepare(
            BV,
            enabled,
            visual_source_ids=["456:1:0"],
            task={"id": "followup"},
            deadline=time.time() + (30 if cancel else 0.03),
        )
    )
    await asyncio.wait_for(visual.started.wait(), 2)
    if cancel:
        task.cancel()
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    if cancel:
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert "期限" in outcome[1]["visual_error"]
    assert not service.pending and visual.frames == []
    assert visual.cancelled == ["followup"]
    assert not any(part.get("visual") for part in service.load(original)["parts"])
