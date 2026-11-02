import asyncio
import struct
import zlib

import httpx
import pytest

from tuber_studio.bilibili_browsing import BrowsingAdapter, PlatformReadError
from tuber_studio.bilibili_frames import FfmpegTiles, FrameSampler, public_storyboard_url, storyboard_entries

BV = "BV1xx411c7mD"
META = {"bvid": BV, "aid": "12", "parts": [{"cid": "34", "duration": 40}]}


class FakeDecoder:
    available = True

    async def dimensions(self, image):
        return 32, 16

    async def crop(self, image, width, height, x, y):
        return b"\xff\xd8\xff" + bytes((x, y))

    async def close(self):
        pass


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = b"".join(
        b"\0" + b"".join(bytes((255, 0, 0)) if x < 16 else bytes((0, 0, 255)) for x in range(32))
        for _ in range(16)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 16, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
async def adapter():
    value = BrowsingAdapter()
    value._read_spacing = 0
    await value.client.aclose()
    yield value
    await value.close()


def transport(adapter, *, data=None, cdn_status=200, view=None, cdn_body=None, cdn_headers=None):
    seen = []
    payload = data or {
        "cid": 34,
        "index": [0, 10, 30],
        "img_x_size": 16,
        "img_y_size": 16,
        "img_x_len": 2,
        "img_y_len": 1,
        "image": ["https://i0.hdslb.com/bfs/videoshot/sheet.png"],
    }

    def handle(request):
        seen.append(request)
        if request.url.path == "/x/web-interface/view":
            body = view or {"bvid": BV, "aid": 12, "pages": [{"cid": 34, "duration": 40}]}
            return httpx.Response(200, json={"code": 0, "data": body})
        if request.url.path == "/x/player/videoshot":
            return httpx.Response(200, json={"code": 0, "data": payload})

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield cdn_body if cdn_body is not None else png()

        return httpx.Response(
            cdn_status,
            stream=Stream(),
            headers={"location": "https://evil.test/"} | (cdn_headers or {}),
        )

    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handle),
        cookies={"SESSDATA": "private"},
        headers={"Authorization": "private"},
    )
    return seen


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/bfs/videoshot/a",
        "file:///tmp/a",
        "https://i0.hdslb.com/x",
        "https://user@i0.hdslb.com/bfs/videoshot/a",
        "https://i0.hdslb.com:8443/bfs/videoshot/a",
        "https://i0.hdslb.com/bfs/videoshot/%2e%2e/secret",
        "https://boss.hdslb.com/bfs/videoshot/a",
    ],
)
def test_cdn_allowlist(url):
    with pytest.raises(PlatformReadError):
        public_storyboard_url(url)


def test_seconds_and_binary_millisecond_proof():
    assert storyboard_entries({"index": [0, 0, 10, 20]}, 40)[0] == [(0, 0), (10, 1), (20, 2)]
    entries, source = storyboard_entries({"index": [0, 10000, 20000]}, 40, struct.pack(">3H", 0, 10, 20))
    assert entries[-1] == (20, 1) and source == "pvdata_u16be_seconds"
    # Long videos legitimately have second values over 1000; no guessing.
    assert storyboard_entries({"index": [0, 1200, 2400]}, 4000)[0][-1] == (2400, 1)


@pytest.mark.parametrize(
    "index,binary",
    [
        ([20, 10], None),
        ([0, 40000], None),
        ([0, float("nan")], None),
        ([0, 9000], struct.pack(">2H", 0, 10)),
        ([], b"x"),
    ],
)
def test_invalid_index_preserves_ordinal_and_rejects(index, binary):
    with pytest.raises(PlatformReadError):
        storyboard_entries({"index": index}, 40, binary)


async def test_real_ffmpeg_crops_selected_tiles_and_accounts_reads(adapter):
    decoder = FfmpegTiles()
    if not decoder.available:
        pytest.skip("真实ffmpeg/ffprobe未安装；其他测试仍验证HTTP边界")
    seen = transport(adapter)
    reads = []

    async def gate():
        reads.append(True)

    adapter.request_gate = gate
    frames = await FrameSampler(adapter, decoder).frames(META, 2)
    assert [frame["actual_time"] for frame in frames] == [10, 30]
    assert all(frame["requested_time"] != frame["actual_time"] for frame in frames)
    assert len(reads) == len(seen) == 3
    assert seen[1].url.params["cid"] == "34" and seen[1].url.params["index"] == "1"
    assert "cookie" not in seen[2].headers and "authorization" not in seen[2].headers
    assert frames[0]["image"] != frames[1]["image"]
    for frame, dominant in zip(frames, (0, 2), strict=True):
        assert await decoder.dimensions(frame["image"]) == (16, 16)
        raw = await decoder._run(
            [
                decoder.ffmpeg,
                "-v",
                "error",
                "-f",
                "image2pipe",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            frame["image"],
            16 * 16 * 3,
        )
        assert raw[dominant] > 240 and raw[2 - dominant] < 10
    assert not decoder.processes


@pytest.mark.parametrize(
    "status,kind",
    [(302, "network"), (403, "permission"), (412, "restricted"), (429, "restricted"), (503, "network")],
)
async def test_no_redirect_no_retry_no_bypass(adapter, status, kind):
    seen = transport(adapter, cdn_status=status)
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder()).frames(META, 1)
    assert caught.value.kind == kind and len(seen) == 3
    assert f"HTTP {status}" in str(caught.value) and "evil.test" not in str(caught.value)


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadError]
)
async def test_cdn_transport_type_is_visible_without_request_secrets(adapter, error_type):
    seen = []

    def handle(request):
        seen.append(request)
        raise error_type("https://private.invalid/?secret-query Cookie=secret-cookie", request=request)

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder())._download(
            "https://i0.hdslb.com/bfs/videoshot/sheet.png?token=secret-query", 1024
        )
    assert caught.value.kind == "network" and error_type.__name__ in str(caught.value)
    assert len(seen) == 1 and "secret" not in str(caught.value) and "https://" not in str(caught.value)


async def test_cid_mismatch_rejected_before_storyboard_read(adapter):
    seen = transport(adapter, view={"bvid": BV, "aid": 12, "pages": [{"cid": 99}]})
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder()).frames(META)
    assert caught.value.kind == "invalid_frames" and len(seen) == 1


async def test_binary_index_download_is_bounded_and_cookie_free(adapter):
    data = {"index": [10000, 30000], "pvdata": "https://i0.hdslb.com/bfs/videoshot/data.bin"}
    seen = transport(adapter, data=data, cdn_body=b"x" * 40002)
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder()).frames(META)
    assert caught.value.kind == "invalid_frames" and len(seen) == 3


async def test_real_decoder_cancel_kills_and_reaps_child():
    decoder = FfmpegTiles()
    if not decoder.available:
        pytest.skip("真实ffmpeg未安装")
    # Endless lavfi source exercises an actual running child, with bounded IO.
    task = asyncio.create_task(
        decoder._run(
            [
                decoder.ffmpeg,
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "color=black:s=16x16:r=1",
                "-f",
                "null",
                "-",
            ],
            b"",
            1000,
        )
    )
    for _ in range(200):
        if decoder.processes:
            break
        await asyncio.sleep(0.005)
    processes = list(decoder.processes)
    assert processes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not decoder.processes and all(child.returncode is not None for child in processes)


async def test_actual_duration_change_rejects_stale_metadata(adapter):
    seen = transport(adapter, view={"bvid": BV, "aid": 12, "pages": [{"cid": 34, "duration": 15}]})
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder()).frames(META)
    assert caught.value.kind == "invalid_frames" and len(seen) == 1


async def test_cdns_are_exact_and_http_gzip_is_rejected_without_decompression(adapter):
    assert public_storyboard_url("//boss.hdslb.com/videoshotpvhdboss/a.bin").startswith("https://boss.")
    assert public_storyboard_url("//bimp.hdslb.com/videoshotpvhdboss/a.jpg").startswith("https://bimp.")
    seen = transport(adapter, cdn_headers={"content-encoding": "gzip"}, cdn_body=b"compressed")
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder()).frames(META)
    assert caught.value.kind == "invalid_frames" and seen[-1].headers["accept-encoding"] == "identity"


async def test_single_partial_sheet_uses_declared_grid_but_actual_crop_bounds(adapter):
    data = {
        "index": [0, 10, 30],
        "img_x_size": 16,
        "img_y_size": 16,
        "img_x_len": 2,
        "img_y_len": 10,
        "image": ["https://i0.hdslb.com/bfs/videoshot/a.jpg"],
    }
    transport(adapter, data=data)
    frames = await FrameSampler(adapter, FakeDecoder()).frames(META, 2)
    assert [frame["source"]["tile_index"] for frame in frames] == [0, 1]
    assert [frame["actual_time"] for frame in frames] == [10, 30]


async def test_tile_index_beyond_partial_last_sheet_is_rejected(adapter):
    data = {
        "index": [0, 0, 5, 10, 15, 20, 25, 30, 35],
        "img_x_size": 16,
        "img_y_size": 16,
        "img_x_len": 2,
        "img_y_len": 10,
        "image": ["https://i0.hdslb.com/bfs/videoshot/a.jpg"],
    }
    transport(adapter, data=data)
    with pytest.raises(PlatformReadError) as caught:
        await FrameSampler(adapter, FakeDecoder()).frames(META, 1)
    assert caught.value.kind == "invalid_frames"


async def test_real_decoder_close_cancels_semaphore_waiters_and_reaps_processes():
    decoder = FfmpegTiles()
    if not decoder.available:
        pytest.skip("真实ffmpeg未安装")
    command = [
        decoder.ffmpeg,
        "-v",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "color=black:s=16x16:r=1",
        "-f",
        "null",
        "-",
    ]
    tasks = [asyncio.create_task(decoder._run(command, b"", 1000)) for _ in range(2)]
    for _ in range(200):
        if decoder.processes and len(decoder.operations) == 2:
            break
        await asyncio.sleep(0.005)
    processes = list(decoder.processes)
    assert len(processes) == 1
    await decoder.close()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert not decoder.operations and not decoder.processes and not decoder.available
    assert all(child.returncode is not None for child in processes)


async def test_real_decoder_timeout_is_bounded_and_reaped():
    decoder = FfmpegTiles(timeout=0.03)
    if not decoder.available:
        pytest.skip("真实ffmpeg未安装")
    with pytest.raises(PlatformReadError) as caught:
        await decoder._run(
            [
                decoder.ffmpeg,
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "color=black:s=16x16:r=1",
                "-f",
                "null",
                "-",
            ],
            b"",
            1000,
        )
    assert caught.value.kind == "frames_timeout" and not decoder.processes and not decoder.operations
