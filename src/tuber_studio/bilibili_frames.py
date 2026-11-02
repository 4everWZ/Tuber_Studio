"""CID-bound platform storyboards and bounded, pipe-only bitmap crops.

No video playback/download or old /pvideo fallback. Endpoint parameters follow
bilibili-api-python 17.4.2 get_video_snapshot(pvideo=False, json_index=True).
"""

import asyncio
import hashlib
import json
import math
import shutil
import struct
from urllib.parse import urlsplit, urlunsplit

import httpx

from .bilibili import _BV, _positive_id
from .bilibili_browsing import PlatformReadError, http_read_detail

CDN_PATHS = {
    "i0.hdslb.com": ("/bfs/videoshot/", "/bfs/storyff/"),
    "i1.hdslb.com": ("/bfs/videoshot/", "/bfs/storyff/"),
    "i2.hdslb.com": ("/bfs/videoshot/", "/bfs/storyff/"),
    "boss.hdslb.com": ("/videoshotpvhdboss/",),
    "bimp.hdslb.com": ("/videoshotpvhdboss/",),
}
MAX_SHEET_BYTES = 8 * 1024 * 1024
MAX_SHEET_PIXELS = 24 * 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024
MAX_FRAMES_BYTES = 1024 * 1024


def _integer(value, minimum=1, maximum=100000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PlatformReadError("invalid_frames", "故事板尺寸、序号或数量无效")
    return value


def _seconds(value, duration):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= duration:
        raise PlatformReadError("invalid_frames", "故事板实际时间不属于当前分P")
    return float(value)


def public_storyboard_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise PlatformReadError("invalid_frames", "故事板CDN地址无效")
    value = "https:" + value if value.startswith("//") else value
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in CDN_PATHS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or not parsed.path.startswith(CDN_PATHS[parsed.hostname])
            or "%" in parsed.path
            or any(segment in {".", ".."} for segment in parsed.path.split("/"))
            or parsed.fragment
        ):
            raise ValueError
    except ValueError:
        raise PlatformReadError("invalid_frames", "故事板只接受允许CDN的HTTPS媒体路径") from None
    # Older API responses use http or scheme-relative URLs. Only the verified
    # public host/path is retained; the actual request always uses HTTPS.
    return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))


def storyboard_entries(data, duration, binary=None):
    """Remove the sentinel, then preserve ordinal→tile mapping exactly.

    Official player getIndex uses index=o-1 for t[o] <= time < t[o+1].
    https://s1.hdslb.com/bfs/static/player/main/widgets/npd.165.cd9329c3.js
    SHA256 26dd6da2f9cf47b3dddaba5141cfa5eed8ef67c085efc6811daac95d6ce678d7
    Millisecond-looking JSON requires binary proof rather than guessing.
    """
    values = data.get("index") or []
    if not isinstance(values, list) or len(values) > 20000:
        raise PlatformReadError("invalid_frames", "故事板时间索引无效或过大")
    json_values = []
    for value in values:
        value = value.get("timestamp") if isinstance(value, dict) else value
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise PlatformReadError("invalid_frames", "故事板时间索引不是有限数值")
        json_values.append(float(value))
    if binary is not None:
        if not binary or len(binary) % 2 or len(binary) > 40000:
            raise PlatformReadError("invalid_frames", "故事板pvdata不是有界大端uint16索引")
        numbers = list(struct.unpack(f">{len(binary) // 2}H", binary))
        if json_values and (
            len(json_values) != len(numbers)
            or not any(
                all(
                    abs(value / scale - number) < 1e-6
                    for value, number in zip(json_values, numbers, strict=True)
                )
                for scale in (1, 1000)
            )
        ):
            raise PlatformReadError("invalid_frames", "故事板JSON与pvdata实际时间不一致")
        source = "pvdata_u16be_seconds"
    else:
        numbers, source = json_values, "json_seconds"
    if not numbers:
        raise PlatformReadError("no_frames", "当前分P没有可核验的故事板时间索引")
    times = [_seconds(value, duration) for value in numbers]
    if any(right < left for left, right in zip(times, times[1:])):
        raise PlatformReadError("invalid_frames", "故事板索引时间倒序或单位无法核验")
    if len(times) < 2 or times[0] != 0:
        raise PlatformReadError("invalid_frames", "故事板时间表缺少平台0哨兵与真实tile时间")
    return [(value, index) for index, value in enumerate(times[1:])], source


class FfmpegTiles:
    def __init__(self, *, ffmpeg=None, ffprobe=None, timeout=10):
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or shutil.which("ffprobe")
        self.timeout = timeout
        self.gate = asyncio.Semaphore(1)
        self.processes = set()
        self.operations = set()
        self.closed = False

    @property
    def available(self):
        return bool(self.ffmpeg and self.ffprobe and not self.closed)

    async def _run(self, command, data, maximum):
        operation = asyncio.current_task()
        self.operations.add(operation)
        try:
            return await self._run_process(command, data, maximum)
        finally:
            self.operations.discard(operation)

    async def _run_process(self, command, data, maximum):
        if not self.available:
            raise PlatformReadError("frames_unavailable", "未安装ffmpeg／ffprobe，无法安全裁切故事板")
        async with self.gate:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.processes.add(process)

            async def read(stream, cap):
                result = bytearray()
                while chunk := await stream.read(65536):
                    result.extend(chunk)
                    if len(result) > cap:
                        raise PlatformReadError("invalid_frames", "故事板解码输出超过大小上限")
                return bytes(result)

            async def write():
                try:
                    process.stdin.write(data)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

            children = [
                asyncio.create_task(write()),
                asyncio.create_task(read(process.stdout, maximum)),
                asyncio.create_task(read(process.stderr, 65536)),
            ]
            try:
                async with asyncio.timeout(self.timeout):
                    _, output, _ = await asyncio.gather(*children)
                    await process.wait()
                if process.returncode != 0 or not output:
                    raise PlatformReadError("invalid_frames", "故事板解码／裁切失败")
                return output
            except TimeoutError:
                raise PlatformReadError("frames_timeout", "故事板解码／裁切达到期限") from None
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                for child in children:
                    child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
                self.processes.discard(process)

    async def dimensions(self, image):
        raw = await self._run(
            [
                self.ffprobe or "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "image2pipe",
                "-i",
                "pipe:0",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,codec_name",
                "-of",
                "json",
            ],
            image,
            65536,
        )
        try:
            streams = json.loads(raw)["streams"]
            if len(streams) != 1 or streams[0].get("codec_name") not in {"mjpeg", "png", "webp"}:
                raise ValueError
            width = _integer(streams[0]["width"], maximum=16384)
            height = _integer(streams[0]["height"], maximum=16384)
            if width * height > MAX_SHEET_PIXELS:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise PlatformReadError("invalid_frames", "故事板不是尺寸受限的JPEG／PNG／WebP拼图") from None
        return width, height

    async def crop(self, image, width, height, x, y):
        width, height = _integer(width, maximum=2048), _integer(height, maximum=2048)
        x, y = _integer(x, minimum=0), _integer(y, minimum=0)
        return await self._run(
            [
                self.ffmpeg or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "image2pipe",
                "-i",
                "pipe:0",
                "-vf",
                f"crop={width}:{height}:{x}:{y}:exact=1,format=yuvj444p",
                "-frames:v",
                "1",
                "-an",
                "-map_metadata",
                "-1",
                "-c:v",
                "mjpeg",
                "-q:v",
                "3",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            image,
            MAX_FRAME_BYTES,
        )

    async def close(self):
        self.closed = True
        operations = [task for task in self.operations if task is not asyncio.current_task()]
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        children = list(self.processes)
        for child in children:
            if child.returncode is None:
                child.kill()
        await asyncio.gather(*(child.wait() for child in children), return_exceptions=True)


class FrameSampler:
    def __init__(self, adapter, decoder=None):
        self.adapter, self.decoder = adapter, decoder or FfmpegTiles()

    async def _download(self, value, maximum):
        url = public_storyboard_url(value)
        request = self.adapter.client.build_request("GET", url)
        request.headers.pop("cookie", None)
        request.headers.pop("authorization", None)
        request.headers["accept-encoding"] = "identity"
        await self.adapter._pace()
        response = None
        try:
            response = await self.adapter.client.send(request, stream=True, follow_redirects=False)
            if response.status_code in {401, 403}:
                raise PlatformReadError("permission", f"故事板下载没有权限（HTTP {response.status_code}）")
            if response.status_code in {412, 429}:
                raise PlatformReadError("restricted", f"故事板下载受平台限制（HTTP {response.status_code}）")
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise PlatformReadError("invalid_frames", "故事板不接受HTTP压缩封装")
            content = bytearray()
            async for chunk in response.aiter_raw(chunk_size=65536):
                content.extend(chunk)
                if len(content) > maximum:
                    raise PlatformReadError("invalid_frames", "故事板下载超出字节上限")
            return bytes(content)
        except httpx.HTTPError as error:
            raise PlatformReadError(
                "network", "故事板下载失败：" + http_read_detail(error) + "；不跟随重定向或绕过限制"
            ) from None
        finally:
            if response is not None:
                await response.aclose()

    async def frames(self, metadata, count=3, *, cid=None):
        _integer(count, maximum=6)
        if not self.decoder.available:
            raise PlatformReadError("frames_unavailable", "未安装ffmpeg／ffprobe")
        bvid = metadata.get("bvid")
        if not isinstance(bvid, str) or not _BV.fullmatch(bvid):
            raise PlatformReadError("invalid_frames", "故事板需要已验证的BV与分P")
        aid = _positive_id(metadata.get("aid"))
        parts = metadata.get("parts")
        if not isinstance(parts, list) or not parts:
            raise PlatformReadError("invalid_frames", "故事板缺少真实分P信息")
        chosen = str(cid if cid is not None else parts[0].get("cid"))
        part = next((part for part in parts if str(part.get("cid")) == chosen), None)
        fresh = await self.adapter._get("/x/web-interface/view", {"bvid": bvid})
        if (
            fresh.get("bvid") != bvid
            or str(fresh.get("aid")) != aid
            or part is None
            or not any(
                str(item.get("cid")) == chosen for item in fresh.get("pages", []) if isinstance(item, dict)
            )
        ):
            raise PlatformReadError("invalid_frames", "故事板CID与当前视频分P不一致")
        duration = part.get("duration")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise PlatformReadError("invalid_frames", "当前分P没有有效时长")
        fresh_part = next(item for item in fresh["pages"] if str(item.get("cid")) == chosen)
        fresh_duration = fresh_part.get("duration")
        if (
            type(fresh_duration) not in (int, float)
            or not math.isfinite(fresh_duration)
            or abs(fresh_duration - duration) > 0.01
        ):
            raise PlatformReadError("invalid_frames", "分P当前时长与证据元数据不一致")
        data = await self.adapter._get(
            "/x/player/videoshot",
            {
                "aid": int(aid),
                "bvid": bvid,
                "cid": int(_positive_id(chosen)),
                "index": 1,
            },
        )
        if data.get("cid") is not None and str(data["cid"]) != chosen:
            raise PlatformReadError("invalid_frames", "平台故事板返回了其他CID")
        binary = await self._download(data["pvdata"], 40000) if data.get("pvdata") else None
        entries, index_source = storyboard_entries(data, duration, binary)
        sheets = data.get("image")
        if not isinstance(sheets, list) or not sheets or len(sheets) > 200:
            raise PlatformReadError("no_frames", "当前分P没有可核验的故事板拼图")
        width = _integer(data.get("img_x_size"), maximum=2048)
        height = _integer(data.get("img_y_size"), maximum=2048)
        targets = [duration * (i + 1) / (count + 1) for i in range(count)]
        selected, used = [], set()
        for target in targets:
            actual, ordinal = min(entries, key=lambda item: (abs(item[0] - target), item[1]))
            if ordinal not in used:
                selected.append((target, actual, ordinal))
                used.add(ordinal)
        cache, dimensions = {}, {}

        async def sheet(number):
            if number >= len(sheets):
                raise PlatformReadError("invalid_frames", "故事板时间索引超出拼图数量")
            if number not in cache:
                cache[number] = await self._download(sheets[number], MAX_SHEET_BYTES)
                dimensions[number] = await self.decoder.dimensions(cache[number])
            return cache[number], dimensions[number]

        _, (full_width, full_height) = await sheet(0)
        if full_width % width or full_height % height:
            raise PlatformReadError("invalid_frames", "故事板拼图与tile尺寸不整除")
        columns = (
            _integer(data["img_x_len"], maximum=1000)
            if data.get("img_x_len") is not None
            else full_width // width
        )
        rows = (
            _integer(data["img_y_len"], maximum=1000)
            if data.get("img_y_len") is not None
            else full_height // height
        )
        if (
            full_width > columns * width
            or full_height > rows * height
            or len(sheets) > 1
            and (full_width != columns * width or full_height != rows * height)
        ):
            raise PlatformReadError("invalid_frames", "故事板网格字段与实际拼图尺寸不符")
        result, total_bytes = [], 0
        for target, actual, ordinal in selected:
            sheet_number, position = divmod(ordinal, columns * rows)
            image, (sheet_width, sheet_height) = await sheet(sheet_number)
            if (
                sheet_width % width
                or sheet_height % height
                or sheet_width > columns * width
                or sheet_height > rows * height
                or sheet_number < len(sheets) - 1
                and (sheet_width != columns * width or sheet_height != rows * height)
            ):
                raise PlatformReadError("invalid_frames", "故事板拼图不符合固定网格或末页边界")
            x, y = (position % columns) * width, (position // columns) * height
            if x + width > sheet_width or y + height > sheet_height:
                raise PlatformReadError("invalid_frames", "故事板tile越过实际拼图边界")
            cropped = await self.decoder.crop(image, width, height, x, y)
            total_bytes += len(cropped)
            if not cropped.startswith(b"\xff\xd8\xff") or not cropped or total_bytes > MAX_FRAMES_BYTES:
                raise PlatformReadError("invalid_frames", "故事板帧不是有界JPEG字节")
            identity = f"{chosen}:storyboard:{ordinal}:" + hashlib.sha256(cropped).hexdigest()[:16]
            result.append(
                {
                    "id": identity,
                    "bvid": bvid,
                    "cid": chosen,
                    "kind": "storyboard",
                    "requested_time": target,
                    "actual_time": actual,
                    "width": width,
                    "height": height,
                    "image": cropped,
                    "mime_type": "image/jpeg",
                    "source": {
                        "index_source": index_source,
                        "tile_index": ordinal,
                        "sheet_index": sheet_number,
                        "sheet_sha256": hashlib.sha256(image).hexdigest(),
                    },
                }
            )
        return result

    async def close(self):
        await self.decoder.close()
