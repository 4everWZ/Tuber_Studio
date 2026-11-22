"""Build representative Omni inputs from one already-authorized, saved visual clip.

The caller owns the task/role/receipt binding. No URL or arbitrary source is
resolved here. Every decoded frame participates in change detection; only the
listed representative frames are supplied for semantic review.
"""

import asyncio
import base64
import io
import json
import math
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from ...infra.platforms.bilibili.frames import FfmpegTiles

_W, _H = 320, 180
_PROXY_BYTES = _W * _H * 3 // 2
_MAX_FILE = 8 * 1024 * 1024
_MAX_FRAMES = 600
_MAX_BATCH_IMAGES = 12
# Bound decoded RGB memory per pass independently of the number of states.
_RGB_BATCH_BYTES = 8 * 1024 * 1024


def unreviewed_preview(receipt):
    """An old total-image cap stopped screening before Omni saw a single frame."""
    coverage = receipt.get("coverage") or {}
    count = coverage.get("scanned_frames")
    return (
        receipt.get("accepted") is False
        and receipt.get("observations") == []
        and coverage.get("complete") is False
        and bool(coverage.get("uncovered"))
        and "batches" not in coverage
        and bool(coverage.get("samples"))
        and type(count) is int
        and count > 0
        and count == (receipt.get("clip") or {}).get("frames")
    )


def _unknown(start, end, note):
    return {
        "coverage": {
            "complete": False,
            "start": start,
            "end": end,
            "scanned_frames": 0,
            "samples": [],
            "uncovered": [{"start": start, "end": end}],
            "note": note,
        },
        "images": [],
    }


def _file(path):
    with path.open("rb") as source:
        media = source.read(_MAX_FILE + 1)
    if not 0 < len(media) <= _MAX_FILE:
        raise ValueError("短片文件为空或超过读取预算")
    return media


async def _probe(decoder, media, start, end, count):
    output = await decoder._run(
        [
            decoder.ffprobe,
            "-v",
            "error",
            "-threads",
            "1",
            "-protocol_whitelist",
            "pipe",
            "-f",
            "mov",
            "-i",
            "pipe:0",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_streams",
            "-show_entries",
            "stream=codec_name,width,height,time_base,duration_ts:"
            "frame=best_effort_timestamp,duration,pkt_duration",
            "-of",
            "json",
        ],
        media,
        512 * 1024,
    )
    info = json.loads(output)
    (stream,) = info["streams"]
    width, height = stream["width"], stream["height"]
    if stream["codec_name"] != "h264" or not (0 < width <= 960 and 0 < height <= 540):
        raise ValueError("短片编码或尺寸不符合已保存画面契约")
    frames = info["frames"]
    if len(frames) != count:
        raise ValueError("解码帧数与短片回执不符")
    tick = Fraction(stream["time_base"])
    pts = [int(f["best_effort_timestamp"]) for f in frames]
    if tick <= 0 or any(a >= b for a, b in zip(pts, pts[1:])):
        raise ValueError("缺少单调的实际帧时标")
    # Fragmented MP4s can start at a nonzero PTS and lack per-frame duration.
    # Between two timestamps the earlier frame is displayed. Use the actual
    # stream duration (or last sample duration) to check the trailing edge.
    duration = stream.get("duration_ts")
    if duration is None:
        duration = pts[-1] - pts[0] + int(frames[-1].get("duration") or frames[-1]["pkt_duration"])
    duration = int(duration)
    if duration <= pts[-1] - pts[0] or abs(float(duration * tick) - (end - start)) > float(tick) + 1e-6:
        raise ValueError("短片呈现时长与回执不符")
    return (width, height), [start + float((p - pts[0]) * tick) for p in pts]


def _ffmpeg(decoder, vf, pixel_format):
    return [
        decoder.ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-protocol_whitelist",
        "pipe",
        "-f",
        "mov",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        vf,
        "-fps_mode",
        "passthrough",
        "-threads",
        "1",
        "-pix_fmt",
        pixel_format,
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def _stroke_mask(gray):
    band = gray.crop((0, _H // 2, _W, _H))
    smooth = band.filter(ImageFilter.BoxBlur(2))
    light = ImageChops.subtract(band, smooth).point(lambda x: 255 if x >= 25 else 0)
    dark = ImageChops.subtract(smooth, band).point(lambda x: 255 if x >= 30 else 0)
    return ImageChops.lighter(
        ImageChops.darker(light, band.point(lambda x: 255 if x >= 153 else 0)),
        ImageChops.darker(dark, band.point(lambda x: 255 if x <= 68 else 0)),
    )


def _line_changed(grid, single, pair):
    values = grid.tobytes()
    width = grid.width
    return max(values) >= single or any(
        a >= pair and b >= pair and (i + 1) % width != 0 for i, (a, b) in enumerate(zip(values, values[1:]))
    )


def _text_change(before, after, previous_density, density, anchor_density):
    # Local ink occupancy rejects much of the apparent change from a moving
    # edge. Also compare actual stroke locations for words of similar density.
    adjacent = ImageChops.difference(previous_density, density)
    shapes = ImageChops.difference(before, after).resize(density.size, Image.Resampling.BOX)
    # Catch a gradual fade whose per-frame deltas are small. Demand a stronger
    # cumulative signal because the background can drift across a state.
    cumulative = ImageChops.difference(anchor_density, density)
    return (
        _line_changed(adjacent, 48, 20) or _line_changed(shapes, 78, 32) or _line_changed(cumulative, 75, 38)
    )


def _states(raw, times):
    states = [(0, ["first"])]
    previous = previous_mask = previous_density = anchor_density = None
    for index in range(len(times)):
        offset = index * _PROXY_BYTES
        y = Image.frombytes("L", (_W, _H), raw[offset : offset + _W * _H])
        # Chroma planes need only a global difference for equal-luminance cuts.
        uv = Image.frombytes("L", (_W // 2, _H), raw[offset + _W * _H : offset + _PROXY_BYTES])
        scene = (y.resize((40, 24), Image.Resampling.BOX), uv.resize((20, 24), Image.Resampling.BOX))
        mask = _stroke_mask(y)
        density = mask.resize((16, 9), Image.Resampling.BOX)
        if previous is not None:
            dy, dc = [ImageStat.Stat(ImageChops.difference(a, b)).mean[0] for a, b in zip(previous, scene)]
            reasons = []
            if dy >= 19 or dc >= 13:
                reasons.append("scene_change")
            if _text_change(previous_mask, mask, previous_density, density, anchor_density):
                reasons.append("lower_text_change")
            if times[index] - times[states[-1][0]] >= 1.5:
                reasons.append("interval")
            if reasons:
                states.append((index, reasons))
                anchor_density = density
        else:
            anchor_density = density
        previous_mask = mask
        previous_density = density
        previous = scene
    return states


def _select(states, times, end, budget):
    last = len(times) - 1
    ends = [i for i, _ in states[1:]] + [len(times)]
    selected = {0, last}
    candidates = {}
    reasons = {0: ["first"], last: ["last"]}
    for (i, labels), stop in zip(states, ends):
        if any(i <= k < stop for k in selected):
            continue
        # Keep the cut itself. Within a text state use its temporal centre
        # so a fade-in has had a chance to become legible.
        centre = (times[i] + (times[stop] if stop <= last else end)) / 2
        frame = i if "scene_change" in labels else min(range(i, stop), key=lambda k: abs(times[k] - centre))
        candidates[frame] = labels
    if len(candidates) <= budget - len(selected):
        selected.update(candidates)
        reasons.update(candidates)
        candidates.clear()
    while candidates and len(selected) < budget:
        frame = max(
            candidates,
            key=lambda k: (
                "scene_change" in candidates[k],
                "lower_text_change" in candidates[k],
                min(abs(times[k] - times[j]) for j in selected),
            ),
        )
        reasons[frame] = candidates.pop(frame)
        selected.add(frame)
    # Show an exact cut even in the final state if there is spare capacity.
    for i, labels in states:
        if "scene_change" in labels and i not in selected and len(selected) < budget:
            selected.add(i)
            reasons[i] = labels
    gaps = [
        {"start": times[i], "end": times[stop] if stop < len(times) else end}
        for (i, _), stop in zip(states, ends)
        if not any(i <= k < stop for k in selected)
    ]
    if last == 0:
        reasons[0] = ["first", "last"]
    samples = [{"frame": i, "seconds": times[i], "reasons": reasons[i]} for i in sorted(selected)]
    return samples, gaps


def _encode(raw, size, count):
    stride = size[0] * size[1] * 3
    if len(raw) != count * stride:
        raise ValueError("代表帧解码数量不符")
    parts = []
    for i in range(count):
        frame = Image.frombytes("RGB", size, raw[i * stride : (i + 1) * stride])
        out = io.BytesIO()
        frame.save(out, "JPEG", quality=90, subsampling=0)
        url = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


async def review_clip_window(
    path: Path, *, start: float, end: float, expected_frames: int, max_images: int | None = 6, on_batch=None
) -> dict:
    """With a total image budget, return a bounded preview. With max_images=None,
    deliver every representative to async on_batch(samples, images) in PTS order.
    The callback owns inference errors; a service failure is not a visual verdict.
    """
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= start + 10.000001):
        raise ValueError("需要已授权的至多十秒短片窗口")
    if max_images is None:
        if on_batch is None:
            raise ValueError("完整短片审核需要分批接收代表帧")
    elif type(max_images) is not int or max_images < 0:
        raise ValueError("图片预算须为非负整数")
    if (
        type(expected_frames) is not int
        or not 1 <= expected_frames <= _MAX_FRAMES
        or (max_images is not None and max_images < min(2, expected_frames))
    ):
        return _unknown(start, end, "帧数或图片预算不足，需缩短窗口或补充预算。")
    decoder = FfmpegTiles()
    try:
        media = await asyncio.to_thread(_file, path)
        size, times = await _probe(decoder, media, start, end, expected_frames)
        proxy = await decoder._run(
            _ffmpeg(decoder, f"scale={_W}:{_H}:flags=area", "yuv420p"), media, expected_frames * _PROXY_BYTES
        )
        if len(proxy) != expected_frames * _PROXY_BYTES:
            raise ValueError("变化扫描的帧数不符")
        states = await asyncio.to_thread(_states, proxy, times)
        del proxy
        budget = len(times) if max_images is None else min(max_images, len(times))
        samples, gaps = _select(states, times, end, budget)
    except ValueError as error:
        return _unknown(start, end, f"短片无法完整核对（{error}），需重新读取或缩短窗口。")
    except (OSError, KeyError, TypeError, ZeroDivisionError) as error:
        return _unknown(start, end, f"短片无法完整核对（{type(error).__name__}），需重新读取或缩短窗口。")
    stride = size[0] * size[1] * 3
    batch_size = min(_MAX_BATCH_IMAGES, max(1, _RGB_BATCH_BYTES // stride))
    images, batches = [], []
    for offset in range(0, len(samples), batch_size):
        batch = samples[offset : offset + batch_size]
        selection = "+".join(f"eq(n\\,{s['frame']})" for s in batch)
        try:
            raw = await decoder._run(
                _ffmpeg(decoder, "select=" + selection, "rgb24"), media, len(batch) * stride
            )
            parts = await asyncio.to_thread(_encode, raw, size, len(batch))
            del raw
        except (OSError, ValueError) as error:
            if on_batch is not None:
                raise
            return _unknown(start, end, f"代表帧解码失败（{error}），需重新读取短片。")
        if on_batch is None:
            images.extend(parts)
        else:
            await on_batch(batch, parts)
        batches.append(
            {"first_frame": batch[0]["frame"], "last_frame": batch[-1]["frame"], "count": len(batch)}
        )
    coverage = {
        "complete": not gaps,
        "start": start,
        "end": end,
        "scanned_frames": len(times),
        "samples": samples,
        "batches": batches,
        "changes": [{"frame": i, "seconds": times[i], "reasons": r} for i, r in states],
        "uncovered": gaps,
        "note": "逐帧检测画面和下方笔画变化，仅所列代表帧供视觉审读。微小变化可能漏检，音轨未经审核。",
    }
    if gaps:
        coverage["note"] += "部分状态未送审，需缩短窗口或增加图片预算。"
    return {"coverage": coverage, "images": images}
