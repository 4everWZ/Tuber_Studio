"""Read one frame from the authorized Bilibili DASH AVC video track."""

import asyncio
import hashlib
import json
import math
import re
import struct
import time
from fractions import Fraction
from urllib.parse import urlsplit

import httpx

from .bilibili import PlatformReadError
from .bilibili_frames import MAX_FRAME_BYTES, MAX_SHEET_PIXELS

MAX_STREAM_BYTES = 16 * 1024 * 1024
STREAM_TIMEOUT = 8
_RANGE = re.compile(r"(\d{1,12})-(\d{1,12})")
_CONTENT_RANGE = re.compile(r"bytes (\d{1,12})-(\d{1,12})/(\d{1,12})")


def _error(message):
    return PlatformReadError("stream_frame", message)


def _range(value):
    match = _RANGE.fullmatch(str(value))
    if not match or int(match[1]) > int(match[2]):
        raise _error("播放流字节索引无效")
    return int(match[1]), int(match[2])


def _stream_url(value):
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or not url.hostname.endswith(".bilivideo.com")
                or url.port not in {None, 443} or url.username is not None or url.password is not None or url.fragment
                or not re.fullmatch(r"/[A-Za-z0-9_./-]+\.m4s", url.path)
                or any(part in {".", ".."} for part in url.path.split("/"))):
            raise ValueError
    except ValueError:
        raise _error("播放来源不是受支持的 B 站 HTTPS 视频轨") from None
    return value


def _segments(data, index_start):
    """Parse the flat ISO BMFF SIDX used by Bilibili SegmentBase."""
    cursor = 0
    try:
        while cursor + 8 <= len(data):
            size, kind = struct.unpack_from(">I4s", data, cursor)
            if size < 8 or cursor + size > len(data):
                break
            if kind == b"sidx":
                break
            cursor += size
        else:
            raise ValueError
        if kind != b"sidx" or size < 32 or cursor + size > len(data):
            raise ValueError
        box = data[cursor:cursor + size]
        version = box[8]
        timescale = struct.unpack_from(">I", box, 16)[0]
        if version not in {0, 1} or not timescale:
            raise ValueError
        _, first_offset = struct.unpack_from(">II" if version == 0 else ">QQ", box, 20)
        header = 28 if version == 0 else 36
        count = struct.unpack_from(">H", box, header + 2)[0]
        if not count or len(box) != header + 4 + count * 12:
            raise ValueError
        position = index_start + cursor + size + first_offset
        elapsed = 0
        segments = []
        for offset in range(header + 4, len(box), 12):
            length, duration, sap = struct.unpack_from(">III", box, offset)
            if not length or length & 0x80000000 or not duration or not sap & 0x80000000 or (sap >> 28) & 7 not in {0, 1, 2}:
                raise ValueError
            segments.append((Fraction(elapsed, timescale), position, position + length - 1))
            elapsed += duration
            position += length
        return segments, Fraction(elapsed, timescale)
    except (ValueError, struct.error, UnboundLocalError):
        raise _error("播放流没有可跳转的平面 DASH 视频索引") from None


class RangeReader:
    def __init__(self, adapter, url, maximum):
        self.adapter = adapter
        self.url = _stream_url(url)
        self.maximum = maximum
        self.used = 0
        self.total = None
        self.etag = None

    async def read(self, start, end):
        length = end - start + 1
        if length <= 0 or length > self.maximum - self.used or self.total is not None and end >= self.total:
            raise _error("精确取帧超过媒体读取预算或播放流范围")
        request = self.adapter.client.build_request("GET", self.url, headers={
            "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity", "Referer": "https://www.bilibili.com/",
        })
        request.headers.pop("cookie", None)
        request.headers.pop("authorization", None)
        if self.etag:
            request.headers["If-Range"] = self.etag
        await self.adapter._pace()
        try:
            response = await self.adapter.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code in {401, 403, 404, 410}:
                    raise _error("播放地址已过期或当前账号无读取权限")
                match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
                etag = response.headers.get("etag")
                if (response.status_code != 206 or not match or (int(match[1]), int(match[2])) != (start, end)
                        or int(match[3]) <= end or self.total not in {None, int(match[3])}
                        or self.etag is not None and self.etag != etag
                        or response.headers.get("content-encoding", "identity").lower() != "identity"
                        or response.headers.get("content-length", str(length)) != str(length)):
                    raise _error("播放来源未按请求返回有效 HTTP Range")
                self.total = int(match[3])
                if etag and not etag.startswith("W/"):
                    self.etag = etag
                result = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    self.used += len(chunk)
                    result.extend(chunk)
                    if len(result) > length or self.used > self.maximum:
                        raise _error("播放来源返回的字节超过读取预算")
                if len(result) != length:
                    raise _error("播放来源返回的 Range 不完整")
                return bytes(result)
            finally:
                await response.aclose()
        except httpx.HTTPError:
            raise _error("播放流 Range 连接失败") from None


async def _timestamps(decoder, media):
    raw = await decoder._run([
        decoder.ffprobe, "-v", "error", "-protocol_whitelist", "pipe", "-f", "mov", "-i", "pipe:0",
        "-select_streams", "v:0", "-show_frames", "-show_streams",
        "-show_entries", "frame=best_effort_timestamp,duration,pkt_duration,key_frame:stream=time_base,codec_name,width,height",
        "-of", "json",
    ], media, 512 * 1024)
    try:
        value = json.loads(raw)
        stream, = value["streams"]
        rate = Fraction(stream["time_base"])
        width, height = stream["width"], stream["height"]
        if (stream["codec_name"] != "h264" or rate <= 0 or type(width) is not int or type(height) is not int
                or width <= 0 or height <= 0 or width * height > MAX_SHEET_PIXELS):
            raise ValueError
        frames = []
        for item in value["frames"]:
            if not frames and item.get("key_frame") != 1:
                raise ValueError
            pts = int(item["best_effort_timestamp"])
            duration = int(item.get("duration", item.get("pkt_duration", 0)))
            if duration < 0 or frames and pts <= frames[-1][0]:
                raise ValueError
            frames.append((pts, duration))
        if not frames:
            raise ValueError
        return frames, rate
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise _error("视频帧没有可核验的呈现时间") from None


async def extract_frame(reader, decoder, track, target):
    segment_base = track.get("SegmentBase") or track.get("segment_base") or {}
    init_left, init_right = _range(segment_base.get("Initialization", segment_base.get("initialization")))
    index_left, index_right = _range(segment_base.get("indexRange", segment_base.get("index_range")))
    if init_left != 0 or init_right >= index_left or index_right >= 512 * 1024:
        raise _error("播放流初始化及索引范围无效")
    header = await reader.read(0, index_right)
    initialization = header[:init_right + 1]
    segments, duration = _segments(header[index_left:], index_left)
    target = Fraction(str(target))
    if not 0 <= target < duration:
        raise _error("目标时间不在视频轨的可播放范围内")
    selected = max(index for index, item in enumerate(segments) if item[0] <= target)
    first = await reader.read(segments[0][1], segments[0][2])
    first_frames, time_base = await _timestamps(decoder, initialization + first)
    origin = first_frames[0][0] * time_base
    fetched = {0: first}

    async def fragment(index):
        if index not in fetched:
            fetched[index] = await reader.read(segments[index][1], segments[index][2])
        return fetched[index]

    media = initialization + await fragment(selected)
    frames, rate = (first_frames, time_base) if selected == 0 else await _timestamps(decoder, media)
    if selected and target < frames[0][0] * rate - origin:
        # The SIDX segment start can precede the first displayed frame.
        media = initialization + await fragment(selected - 1) + await fragment(selected)
        frames, rate = await _timestamps(decoder, media)
    elif selected + 1 < len(segments) and target >= frames[-1][0] * rate - origin:
        # Check the next actual presentation timestamp across the fragment edge.
        media += await fragment(selected + 1)
        frames, rate = await _timestamps(decoder, media)
    for index, (pts, frame_duration) in enumerate(frames):
        at = pts * rate - origin
        end_at = (frames[index + 1][0] if index + 1 < len(frames) else pts + frame_duration) * rate - origin
        if 0 <= at <= target < end_at:
            break
    else:
        raise _error("无法核验目标时刻实际显示的视频帧")
    image = await decoder._run([
        decoder.ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1", "-copyts",
        "-protocol_whitelist", "pipe", "-f", "mov", "-i", "pipe:0", "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"select=eq(pts\\,{pts}),scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease",
        "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "3", "-threads", "1", "-f", "image2pipe", "pipe:1",
    ], media, MAX_FRAME_BYTES)
    width, height = await decoder.dimensions(image)
    return {"actual_time": float(at), "presentation_end": float(end_at), "width": width, "height": height, "image": image}


class StreamFrameSampler:
    def __init__(self, adapter, decoder):
        self.adapter = adapter
        self.decoder = decoder
        self.gate = asyncio.Semaphore(1)

    async def frame(self, metadata, cid, target):
        part = next((item for item in metadata["parts"] if str(item["cid"]) == str(cid)), None)
        if not part or type(target) not in {int, float} or not math.isfinite(target) or not 0 <= target < part["duration"]:
            raise _error("目标时码超出已核验分 P")
        if not self.decoder.available:
            raise _error("精确取帧需要 FFmpeg 和 FFprobe")
        started = time.monotonic()
        try:
            async with asyncio.timeout(STREAM_TIMEOUT), self.gate:
                fresh = await self.adapter.video_metadata(metadata["bvid"])
                current = next((item for item in fresh["parts"] if str(item["cid"]) == str(cid)), None)
                if (not current or fresh["aid"] != metadata["aid"] or current.get("page") != part.get("page")
                        or current["duration"] != part["duration"]):
                    raise _error("视频分 P 或时长已变化")
                data = await self.adapter._get("/x/player/playurl", {
                    "bvid": metadata["bvid"], "cid": int(cid), "fnval": 16, "qn": 80,
                })
                dash = data.get("dash")
                tracks = dash.get("video") if isinstance(dash, dict) else None
                tracks = tracks if isinstance(tracks, list) else []
                compatible = [track for track in tracks if isinstance(track, dict)
                              and str(track.get("codecs", "")).startswith("avc1.")
                              and track.get("mimeType", track.get("mime_type")) == "video/mp4"
                              and type(track.get("height")) is int and 0 < track["height"] <= 1080]
                if not compatible:
                    raise _error("当前播放授权未提供可跳转的 DASH AVC 视频轨")
                track = max(compatible, key=lambda item: item["height"])
                reader = RangeReader(self.adapter, track.get("baseUrl") or track.get("base_url"), MAX_STREAM_BYTES)
                frame = await extract_frame(reader, self.decoder, track, target)
        except TimeoutError:
            raise _error("精确取帧达到 8 秒期限") from None
        digest = hashlib.sha256(frame["image"]).hexdigest()[:16]
        return frame | {"id": f"{cid}:stream:{digest}:{round(frame['actual_time'] * 1000000)}", "cid": str(cid),
                        "kind": "stream", "requested_time": target, "bytes_read": reader.used,
                        "elapsed_seconds": round(time.monotonic() - started, 3)}
