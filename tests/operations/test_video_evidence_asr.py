"""Tests for CC-missing Bilibili ASR fallback and subtitle-guided visual target binding."""

import copy
import time

import pytest
import pytest_asyncio

from tuber_studio.infra.inference.omni_asr import AsrUnavailable
from tuber_studio.infra.platforms.bilibili.browsing import PlatformReadError
from tuber_studio.operations.kernel.evidence import BrowsingEvidence
from tuber_studio.operations.kernel.inputs import ContentDecisionInput, bind_content_decision
from tuber_studio.operations.kernel.models import BrowsingPolicy, ContentDecision
from tests.operations.test_browsing_evidence import BV, PNG, PublicAdapter, TestVisual


class NoCcAudioAdapter(PublicAdapter):
    def __init__(self):
        super().__init__()
        self.metadata = copy.deepcopy(self.metadata) | {
            "parts": [{"cid": "456", "title": "第一P", "duration": 60}]
        }
        self.failures["456"] = "empty"
        self.audio_calls = []

    async def video_stream_audio(self, metadata, *, cid, max_duration=600):
        self.audio_calls.append((metadata["bvid"], cid, max_duration))
        return {
            "cid": str(cid),
            "audio": b"fake-mp3-payload",
            "mime_type": "audio/mpeg",
            "filename": "audio.mp3",
            "duration": 60.0,
            "truncated": False,
        }


class FakeAsr:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    async def transcribe(self, audio_bytes, *, filename="audio.mp3", mime_type="audio/mpeg", language="zh"):
        self.calls.append((audio_bytes, filename, mime_type, language))
        if self.fail:
            raise AsrUnavailable("503 unavailable")
        return {
            "language": "zh",
            "duration": 60.0,
            "text": "这页PPT写着实测数据对比 下一段看屏幕右上角曲线",
            "segments": [
                {"id": "asr:0", "start": 2.0, "end": 8.0, "text": "这页PPT写着实测数据对比"},
                {"id": "asr:1", "start": 18.0, "end": 26.0, "text": "下一段看屏幕右上角曲线"},
            ],
        }


async def _reader(method, *args):
    return await method(*args)


@pytest_asyncio.fixture
async def make_evidence(runtime):
    created = []

    def create(adapter, visual=None, *, asr=None):
        svc = BrowsingEvidence(runtime, adapter, _reader, visual, asr=asr)
        created.append(svc)
        return svc

    yield create
    for svc in created:
        await svc.close()


async def test_cc_missing_triggers_asr_and_allows_visual_target_binding(make_evidence):
    adapter = NoCcAudioAdapter()
    visual = TestVisual()
    asr = FakeAsr()
    svc = make_evidence(adapter, visual, asr=asr)
    config = BrowsingPolicy().model_dump() | {"visual_enabled": True}

    _, material = await svc.prepare(BV, config)
    assert len(adapter.audio_calls) == 1
    assert len(asr.calls) == 1
    assert visual.calls == 0
    assert material["missing_parts"] == []
    assert material["coverage"] == "all_available_cc"
    part = material["parts"][0]
    assert part["asr"] is True
    assert part["subtitle_source"] == "asr"
    assert part["segments"][0]["id"] == "456:asr:0"
    assert part["segments"][1]["start"] == 18.0

    # Verify ContentDecisionInput and bind_content_decision accept audio_transcript visual_targets
    presentation = ContentDecisionInput(material)
    chunk = [dict(seg, cid="456") for seg in part["segments"]]
    data, aliases = presentation.present(chunk)
    assert "audio_transcript" in data["source_kinds"]
    first_alias = next(iter(aliases))
    wire = ContentDecision(
        interest="like",
        worth_commenting=True,
        reason="visual_required",
        explanation="需要核对右上角实测曲线细节",
        evidence_ids=[first_alias],
        coverage_required="visual",
        visual_targets=[{"evidence_id": first_alias, "position": "during", "precision": "stream"}],
    )
    resolved = bind_content_decision(wire, aliases)
    assert len(resolved.visual_targets) == 1
    assert resolved.visual_targets[0].evidence_id == "456:asr:0"
    assert resolved.visual_targets[0].precision == "stream"


async def test_asr_unavailable_falls_back_to_visual_scan(make_evidence):
    adapter = NoCcAudioAdapter()
    visual = TestVisual()
    asr = FakeAsr(fail=True)
    svc = make_evidence(adapter, visual, asr=asr)
    config = BrowsingPolicy().model_dump() | {"visual_enabled": True}

    _, material = await svc.prepare(BV, config)
    assert len(adapter.audio_calls) == 1
    assert len(asr.calls) == 1
    assert visual.calls >= 1
    assert any(part.get("visual") for part in material["parts"])


async def test_empty_cc_without_asr_still_scans_visual(make_evidence):
    adapter = NoCcAudioAdapter()
    visual = TestVisual()
    svc = make_evidence(adapter, visual, asr=None)
    if svc.asr is not None and hasattr(svc.asr, "close"):
        await svc.asr.close()
    svc.asr = None
    config = BrowsingPolicy().model_dump() | {"visual_enabled": True}

    _, material = await svc.prepare(BV, config)
    assert adapter.audio_calls == []
    assert visual.calls >= 1
    assert any(part.get("visual") for part in material["parts"])
