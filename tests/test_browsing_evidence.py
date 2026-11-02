"""Public evidence storage, shared preparation and optional visual contracts."""

import asyncio
import base64
import copy
import hashlib
import json
import time

import httpx
import pytest
import pytest_asyncio

from tuber_studio.bilibili_browsing import BrowsingAdapter, PlatformReadError
from tuber_studio.browsing_evidence import BrowsingEvidence, EvidenceUnavailable
from tuber_studio.browsing_models import BrowsingPolicy

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

    async def video_frames(self, metadata, count):
        self.calls.append(("frames", count))
        return copy.deepcopy(self.frames)


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
    assert hashlib.sha256(raw).hexdigest() == identity
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
async def test_platform_errors_do_not_fall_back_to_other_part_or_visual(evidence_factory, config, failure):
    adapter = PublicAdapter()
    adapter.failures["456"] = failure
    visual = TestVisual()
    service = evidence_factory(adapter, visual)
    with pytest.raises(PlatformReadError) as caught:
        await service.prepare(BV, config | {"visual_enabled": True})
    assert caught.value.kind == failure and visual.calls == 0
    assert not list(service.directory.iterdir())


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


async def test_expired_or_changed_original_material_cannot_be_loaded(evidence_factory, runtime, config):
    service = evidence_factory()
    identity, _ = await service.prepare(BV, config)
    path = service.directory / f"{identity}.json"
    path.write_bytes(path.read_bytes() + b" ")
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
    assert caught.value.kind == "visual_unavailable" and not list(service.directory.iterdir())


async def test_cc_is_preferred_to_visual_even_when_some_parts_have_no_cc(evidence_factory, config):
    adapter = PublicAdapter()
    adapter.failures["457"] = "no_subtitles"
    visual = TestVisual()
    service = evidence_factory(adapter, visual)
    _, material = await service.prepare(BV, config | {"visual_enabled": True})
    assert material["parts"] and visual.calls == 0


async def test_visual_references_actual_times_and_each_part_without_claiming_full_view(
    evidence_factory, config
):
    adapter = await no_cc_adapter()
    adapter.frames.append(dict(adapter.frames[0], id="second", cid="457", requested_time=35, actual_time=31))
    visual = TestVisual()
    visual.returned = {"frame_observations": {adapter.frames[0]["id"]: "珊瑚", "second": "海龟"}}
    service = evidence_factory(adapter, visual)
    identity, material = await service.prepare(
        BV, config | {"visual_enabled": True}, task={"id": "visual-task"}
    )
    assert [(part["cid"], part["segments"][0]["start"]) for part in material["parts"]] == [
        ("456", 8),
        ("457", 31),
    ]
    assert all(not part["coverage"]["complete"] for part in material["parts"])
    assert [part["segments"][0]["text"] for part in material["parts"]] == ["珊瑚", "海龟"]
    assert material["coverage"] == "sampled_visual"
    assert base64.b64decode(material["visual_frames"][0]["image_base64"]) == PNG
    assert material["parts"][0]["visual_provider"] == {"provider_id": "controlled-test", "version": "1"}
    assert (
        visual.cancelled == ["visual-task"] and visual.task["cancel_signal"].is_set() and visual.frames == []
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


@pytest.mark.parametrize("configured,parent,expected", [(300, None, 300), (300, 30, 30), (None, None, 60)])
async def test_visual_preparation_honors_provider_timeout_and_parent_deadline(
    evidence_factory, config, configured, parent, expected
):
    visual = TestVisual()
    if configured is not None:
        visual.cap["timeout_seconds"] = configured
    service = evidence_factory(await no_cc_adapter(), visual)
    started = time.time()
    await service.prepare(
        BV,
        config | {"visual_enabled": True},
        deadline=started + parent if parent is not None else None,
    )
    assert visual.deadline - started == pytest.approx(expected, abs=2)


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
    key, material = await service.prepare(BV, enabled, require_visual=True, task={"id": "followup"})
    assert key != first_key and visual.calls == 1
    assert (
        service.store.one("SELECT expires_at FROM browsing_materials WHERE id=?", (key,))["expires_at"]
        == first_expiry
    )
    assert material["parts"][:2] == first["parts"]
    assert material["parts"][2]["visual_provider"] == {"provider_id": "controlled-test", "version": "1"}
    assert material["parts"][2]["segments"][0]["start"] == 8
    assert len([call for call in adapter.calls if call[0] == "subtitles"]) == 2
    assert len([call for call in adapter.calls if call[0] == "metadata"]) == 1
    cached, _ = await service.prepare(BV, enabled, require_visual=True)
    assert cached == key and visual.calls == 1


async def test_visual_request_is_separate_from_concurrent_cc_only_flight(evidence_factory, config):
    adapter, visual = PublicAdapter(), TestVisual()
    adapter.subtitle_gate = asyncio.Event()
    service = evidence_factory(adapter, visual)
    enabled = config | {"visual_enabled": True}
    ordinary = asyncio.create_task(service.prepare(BV, enabled))
    await adapter.entered.wait()
    requested = asyncio.create_task(service.prepare(BV, enabled, require_visual=True))
    await asyncio.sleep(0)
    adapter.subtitle_gate.set()
    plain, augmented = await asyncio.gather(ordinary, requested)
    assert not any(part.get("visual") for part in plain[1]["parts"])
    assert any(part.get("visual") for part in augmented[1]["parts"])
    assert visual.calls == 1


async def test_other_provider_visual_is_not_reused_but_original_cc_is_shared(evidence_factory, config):
    adapter, visual = PublicAdapter(), TestVisual()
    service = evidence_factory(adapter, visual)
    enabled = config | {"visual_enabled": True}
    _, first = await service.prepare(BV, enabled, require_visual=True)
    visual.cap["version"] = "2"
    visual.returned = {"version": "2", "frame_observations": {"first": "新模型看到的是透明容器"}}
    _, cc = await service.prepare(BV, enabled)
    assert visual.calls == 1 and not any(part.get("visual") for part in cc["parts"])
    assert cc["parts"] == [part for part in first["parts"] if not part.get("visual")]
    _, current = await service.prepare(BV, enabled, require_visual=True)
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
    service = evidence_factory(PublicAdapter(), visual)
    enabled = config | {"visual_enabled": True}
    original, _ = await service.prepare(BV, enabled)
    visual.gate = asyncio.Event()
    task = asyncio.create_task(
        service.prepare(
            BV,
            enabled,
            require_visual=True,
            task={"id": "followup"},
            deadline=time.time() + (30 if cancel else 0.03),
        )
    )
    await asyncio.wait_for(visual.started.wait(), 2)
    if cancel:
        task.cancel()
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(outcome, asyncio.CancelledError if cancel else EvidenceUnavailable)
    assert not service.pending and visual.frames == []
    assert visual.cancelled == ["followup"]
    assert not any(part.get("visual") for part in service.load(original)["parts"])
