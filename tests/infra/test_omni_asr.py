"""Unit tests for OmniAsrClient (/v1/audio/transcriptions) and Bilibili DASH audio extraction."""

import json
import struct
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.infra.inference.omni_asr import AsrUnavailable, OmniAsrClient, _asr_base_url
from tuber_studio.infra.platforms.bilibili.frames import FfmpegTiles
from tuber_studio.infra.platforms.bilibili.stream_frame import (
    RangeReader,
    StreamFrameSampler,
    extract_audio,
)


def _synthetic_sidx_mp4() -> tuple[bytes, dict]:
    init = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x01isomavc1"
    # Build a minimal valid version=0 sidx box with 2 segments (timescale=1000Hz, duration=2000 ticks each -> 4s total)
    seg1 = b"A" * 64
    seg2 = b"B" * 64
    entries = struct.pack(">III", len(seg1), 2000, 0x80000000) + struct.pack(">III", len(seg2), 2000, 0x80000000)
    # version+flags (4) + ref_id (4) + timescale (4) + earliest_pts (4) + first_offset (4) + reserved+count (4)
    sidx_payload = struct.pack(">IIIIII", 0, 1, 1000, 0, 0, 2)
    sidx_size = 8 + len(sidx_payload) + len(entries)
    sidx = struct.pack(">I4s", sidx_size, b"sidx") + sidx_payload + entries
    blob = init + sidx + seg1 + seg2
    track = {
        "SegmentBase": {
            "Initialization": f"0-{len(init) - 1}",
            "indexRange": f"{len(init)}-{len(init) + sidx_size - 1}",
        },
        "mimeType": "audio/mp4",
        "bandwidth": 64000,
        "baseUrl": "https://upos-sz-mirror.bilivideo.com/upgcxcode/12/34/test-audio.m4s",
    }
    return blob, track


class _RawByteStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


class _FakeRangeAdapter:
    def __init__(self, blob: bytes, track: dict):
        self.blob = blob
        self.track = track

        def handler(request: httpx.Request) -> httpx.Response:
            rng = request.headers["range"]
            left_s, right_s = rng.removeprefix("bytes=").split("-")
            left, right = int(left_s), int(right_s)
            chunk = self.blob[left : right + 1]
            return httpx.Response(
                206,
                stream=_RawByteStream(chunk),
                headers={
                    "Content-Range": f"bytes {left}-{right}/{len(self.blob)}",
                    "Content-Length": str(len(chunk)),
                    "Content-Encoding": "identity",
                    "ETag": '"test-etag"',
                },
            )

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _pace(self):
        return None

    async def video_metadata(self, bvid):
        return {
            "bvid": bvid,
            "aid": 99,
            "parts": [{"cid": "101", "page": 1, "duration": 4}],
        }

    async def _get(self, path, params):
        return {"dash": {"audio": [self.track]}}


def test_asr_base_url_normalizes_gateway_variants():
    assert _asr_base_url("http://127.0.0.1:8000/llm/v1") == "http://127.0.0.1:8000/v1/"
    assert _asr_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1/"
    assert _asr_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1/"
    assert OmniAsrClient.from_settings(SimpleNamespace(data={"omni": {"base_url": ""}})) is None
    client = OmniAsrClient.from_settings(
        SimpleNamespace(
            data={"omni": {"base_url": "http://127.0.0.1:8000/llm/v1"}},
            provider_key=lambda name: "secret-key",
        )
    )
    assert client is not None and client.base_url == "http://127.0.0.1:8000/v1/"


async def test_omni_asr_client_parses_segments_and_degrades_gracefully():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "task": "transcribe",
                "language": "zh",
                "duration": 6.5,
                "text": "大家好 今天拆一个硬核模型",
                "segments": [
                    {"id": 0, "start": 0.0, "end": 2.4, "text": "大家好"},
                    {"id": 1, "start": 2.5, "end": 6.5, "text": "今天拆一个硬核模型"},
                ],
            },
        )

    client = OmniAsrClient("http://127.0.0.1:8000/llm/v1", "app-token")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        headers=client._client.headers,
        transport=httpx.MockTransport(handler),
    )
    try:
        res = await client.transcribe(b"mp3-bytes", filename="clip.mp3", mime_type="audio/mpeg", language="zh")
        assert captured["url"] == "http://127.0.0.1:8000/v1/audio/transcriptions"
        assert captured["auth"] == "Bearer app-token"
        assert len(res["segments"]) == 2
        assert res["segments"][1] == {"id": "asr:1", "start": 2.5, "end": 6.5, "text": "今天拆一个硬核模型"}
    finally:
        await client.close()

    unavail_client = OmniAsrClient("http://127.0.0.1:8000/v1", "app-token")
    unavail_client._client = httpx.AsyncClient(
        base_url=unavail_client.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"error": {"code": "asr_unavailable"}})
        ),
    )
    try:
        with pytest.raises(AsrUnavailable):
            await unavail_client.transcribe(b"mp3-bytes")
    finally:
        await unavail_client.close()


async def test_extract_audio_reads_sidx_and_truncates_by_duration():
    blob, track = _synthetic_sidx_mp4()
    adapter = _FakeRangeAdapter(blob, track)
    try:
        # Decoder unavailable -> returns coalesced raw MP4 bytes with accurate truncation signal
        no_ffmpeg = FfmpegTiles(ffmpeg="", ffprobe="")
        sampler = StreamFrameSampler(adapter, no_ffmpeg)
        result = await sampler.audio({"bvid": "BV1d8pU6xEZX", "aid": 99, "parts": [{"cid": "101", "page": 1, "duration": 4}]}, "101", max_duration=1.5)
        assert result["truncated"] is True
        assert result["mime_type"] == "audio/mp4"
        assert b"A" * 64 in result["audio"]
        assert b"B" * 64 not in result["audio"]

        full = await sampler.audio({"bvid": "BV1d8pU6xEZX", "aid": 99, "parts": [{"cid": "101", "page": 1, "duration": 4}]}, "101", max_duration=600)
        assert full["truncated"] is False
        assert b"A" * 64 in full["audio"] and b"B" * 64 in full["audio"]
    finally:
        await adapter.client.aclose()


async def test_extract_audio_with_real_ffmpeg_resamples_to_mono_16k(tmp_path):
    decoder = FfmpegTiles()
    if not decoder.available:
        pytest.skip("FFmpeg and FFprobe are required")
    audio_mp4 = tmp_path / "tone.m4s"
    cmd = [
        decoder.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:a", "aac", "-movflags", "empty_moov+default_base_moof+frag_keyframe+global_sidx",
        str(audio_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        pytest.skip("AAC encoder unavailable in ffmpeg build")
    blob = audio_mp4.read_bytes()
    cursor = 0
    while True:
        size, tag = struct.unpack_from(">I4s", blob, cursor)
        if tag == b"sidx":
            break
        cursor += size
    track = {
        "SegmentBase": {"Initialization": f"0-{cursor - 1}", "indexRange": f"{cursor}-{cursor + size - 1}"},
        "mimeType": "audio/mp4",
        "baseUrl": "https://upos-sz-mirror.bilivideo.com/upgcxcode/real/tone.m4s",
    }
    adapter = _FakeRangeAdapter(blob, track)
    try:
        reader = RangeReader(adapter, track["baseUrl"], 1024 * 1024)
        res = await extract_audio(reader, decoder, track)
        assert res["mime_type"] in {"audio/mpeg", "audio/wav"}
        assert len(res["audio"]) > 100
        assert res["truncated"] is False
    finally:
        await adapter.client.aclose()
