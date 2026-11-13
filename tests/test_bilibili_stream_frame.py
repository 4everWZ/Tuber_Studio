import asyncio
import json
import struct
import subprocess

import httpx
import pytest

from tuber_studio.platforms.bilibili import PlatformReadError
from tuber_studio.platforms.bilibili_frames import FfmpegTiles
from tuber_studio.platforms.bilibili_stream_frame import RangeReader, extract_frame


class Adapter:
    async def _pace(self):
        pass


class Body(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.mark.parametrize("full_range", [False, True], ids=["limited-range", "full-range"])
async def test_numbered_variable_rate_track_matches_reference_frame(tmp_path, full_range):
    decoder = FfmpegTiles()
    if not decoder.available:
        pytest.skip("FFmpeg and FFprobe are required")
    path = tmp_path / "numbered.mp4"
    # Encode a frame number in the brightness and change the lower half at each keyframe.
    raw = b"".join(bytes([8 + 4 * index]) * (96 * 32) + bytes([20 if index // 10 % 2 else 200]) * (96 * 32)
                   for index in range(50))
    command = [decoder.ffmpeg, "-v", "error", "-f", "rawvideo", "-pixel_format", "gray", "-video_size", "96x64",
               "-framerate", "10", "-i", "pipe:0", "-vf", "setpts=PTS+floor(N/3)*0.07/TB", "-fps_mode", "vfr",
               "-c:v", "libx264", "-g", "10", "-keyint_min", "10", "-sc_threshold", "0", "-bf", "2",
               "-bsf:v", f"h264_metadata=video_full_range_flag={int(full_range)}",
               "-movflags", "empty_moov+default_base_moof+frag_keyframe+global_sidx", str(path)]
    subprocess.run(command, input=raw, check=True, capture_output=True)
    media = path.read_bytes()
    cursor = 0
    while True:
        size, tag = struct.unpack_from(">I4s", media, cursor)
        if tag == b"sidx":
            break
        cursor += size
    track = {"SegmentBase": {"Initialization": f"0-{cursor - 1}", "indexRange": f"{cursor}-{cursor + size - 1}"}}
    probe = subprocess.check_output([decoder.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                                     "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)])
    times = [float(item["best_effort_timestamp_time"]) for item in json.loads(probe)["frames"]]
    assert times[0] > 0 and len({round(right-left, 2) for left, right in zip(times, times[1:])}) > 1
    # Decode the complete track sequentially through the same JPEG output format.
    # H.264/JPEG range conversion can change brightness; raw input bytes are not
    # a decoded-frame oracle across FFmpeg versions.
    reference_jpegs = subprocess.check_output(
        [
            decoder.ffmpeg,
            "-v",
            "error",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "mjpeg",
            "-q:v",
            "3",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "pipe:1",
        ]
    )
    reference_pixels = subprocess.check_output(
        [
            decoder.ffmpeg,
            "-v",
            "error",
            "-f",
            "mjpeg",
            "-i",
            "pipe:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        input=reference_jpegs,
    )
    frame_size = 96 * 64
    assert len(reference_pixels) == len(times) * frame_size
    references = [
        reference_pixels[start : start + frame_size] for start in range(0, len(reference_pixels), frame_size)
    ]
    reads = []

    def serve(request):
        assert "cookie" not in request.headers
        left, right = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        reads.append((left, right))
        return httpx.Response(206, headers={"Content-Range": f"bytes {left}-{right}/{len(media)}"},
                              stream=Body(media[left:right + 1]))

    adapter = Adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), cookies={"SESSDATA": "fixture"}) as client:
        adapter.client = client
        for index in (5, 10, 31):
            target = round(times[index] - times[0] + (0.01 if index != 10 else 0), 6)
            reader = RangeReader(adapter, "https://upos.example.bilivideo.com/track.m4s", 16 * 1024 * 1024)
            frame = await extract_frame(reader, decoder, track, target)
            assert frame["actual_time"] == pytest.approx(times[index] - times[0])
            assert frame["actual_time"] <= target < frame["presentation_end"]
            decoded = subprocess.check_output([decoder.ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "rawvideo",
                                               "-pix_fmt", "gray", "pipe:1"], input=frame["image"])
            assert (frame["width"], frame["height"]) == (96, 64)
            assert decoded == references[index]
            assert decoded != references[index - 1]
            assert decoded != references[index + 1]
            assert 0 < reader.used < len(media)
    assert reads and all(right < len(media) for _, right in reads)


@pytest.mark.parametrize(("status", "message"), [(200, "HTTP Range"), (403, "过期"), (206, "HTTP Range")])
async def test_rejected_range_response_never_reads_body(status, message):
    class Unreadable(httpx.AsyncByteStream):
        def __aiter__(self):
            pytest.fail("Rejected response body must not be read")

    adapter = Adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=Unreadable()))) as client:
        adapter.client = client
        reader = RangeReader(adapter, "https://upos.example.bilivideo.com/track.m4s", 16)
        with pytest.raises(PlatformReadError, match=message):
            await reader.read(0, 7)
        with pytest.raises(PlatformReadError, match="预算"):
            await reader.read(0, 16)
        assert reader.used == 0


async def test_range_cancellation_closes_stream():
    started = asyncio.Event()
    closed = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ab"
            started.set()
            await asyncio.Future()

        async def aclose(self):
            closed.set()

    def serve(_):
        return httpx.Response(206, headers={"Content-Range": "bytes 0-7/8"}, stream=SlowBody())

    adapter = Adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        adapter.client = client
        reader = RangeReader(adapter, "https://upos.example.bilivideo.com/track.m4s", 16)
        task = asyncio.create_task(reader.read(0, 7))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
