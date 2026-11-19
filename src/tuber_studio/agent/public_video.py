"""Private research on an explicitly selected public video without a platform account."""

import time
import tempfile
from pathlib import Path
from uuid import UUID

import httpx

from ..http_api import InferenceBudgetExceeded, InferenceQueueBusy
from ..platforms.bilibili import PlatformReadError
from ..platforms.bilibili_browsing import BrowsingAdapter
from .public_sources import video_read_restriction


def clip_path(runtime_dir, step_id):
    return Path(runtime_dir) / "browsing" / "private-clips" / (str(UUID(step_id)) + ".mp4")


def preview_times(start, end):
    span = end - start
    interior = [start + span * fraction for fraction in (0.15, 0.5, 0.85)]
    if span >= 30:
        # A proportional sample of a long part can start minutes after its opening.
        return [start + 1, *interior, end - 1]
    return interior


async def save_clip(adapter, metadata, part, start, end, request, granted, runtime_dir):
    try:
        clip = await adapter.video_stream_clip(metadata, cid=part["cid"], start=start, end=end)
        if not granted():
            return {"error": "任务授权已变化。"}
        movie = clip.pop("movie")
        dest = clip_path(runtime_dir, request.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as file:
                staged = Path(file.name)
                file.write(movie)
            staged.replace(dest)
        finally:
            if staged:
                staged.unlink(missing_ok=True)
        return {"clip": {"bvid": metadata["bvid"], "part": part["page"], **clip}}
    except (PlatformReadError, httpx.HTTPError, TimeoutError, OSError) as error:
        if isinstance(error, PlatformReadError) and error.kind == "archive_window":
            return {"clip_error": str(error)}
        restriction = video_read_restriction(error, metadata["bvid"], part["page"], "连续短片")
        if restriction:
            return restriction | {"clip_error": restriction["message"]}
        return {"clip_error": f"该窗口未保存连续画面（{type(error).__name__}）。"}


async def read_public_part(request, selection, vision, granted, runtime_dir=None, *, archive=None):
    if selection.part is None:
        return {"error": "匿名视频取材需要指定分 P。"}
    adapter = archive or BrowsingAdapter()
    if archive is None:
        adapter.read_video_tags = False
    try:
        if not granted():
            return {"error": "任务授权已变化。"}
        url = f"https://www.bilibili.com/video/{selection.bvid}/?p={selection.part}"
        try:
            metadata = await adapter.video_metadata(selection.bvid)
        except PlatformReadError as error:
            restriction = video_read_restriction(error, selection.bvid, selection.part, "分 P 资料")
            if not restriction:
                raise
            return restriction | {"error": restriction["message"], "url": url, "request_id": request.id}
        part = next((p for p in metadata["parts"] if p["page"] == selection.part), None)
        if metadata["bvid"] != selection.bvid or not part:
            return {"error": "公开视频没有指定的分 P。"}
        start = selection.start or 0
        end = min(part["duration"], selection.end if selection.end is not None else part["duration"])
        if start >= end:
            return {"error": "指定区间超出该分 P 的实际时长。", "part_duration": part["duration"]}
        findings, gaps = [], []
        restriction = {}

        def finding(seconds, text, kind):
            return {"part": selection.part, "cid": part["cid"], "seconds": seconds, "text": text,
                    "kind": kind, "url": f"{url}&t={int(seconds)}"}

        if not granted():
            return {"error": "任务授权已变化。"}
        ranges = archive.ranges(start, end) if archive else [(start, end)]
        text_tracks = []
        try:
            subtitles = await adapter.subtitles(selection.bvid, part["cid"])
            rows = []
            if (isinstance(subtitles, dict) and subtitles.get("bvid", selection.bvid) == selection.bvid
                    and str(subtitles.get("cid", part["cid"])) == str(part["cid"])):
                rows = [row for row in subtitles.get("segments") or ()
                        if any(a <= row["start"] < row["end"] <= b for a, b in ranges)]
                text_tracks.append(subtitles | {"bvid": selection.bvid, "cid": part["cid"], "segments": rows}
                                   | ({"retrieval": "archive"} if archive else {}))
            if rows:
                for index in sorted({0, len(rows) // 2, len(rows) - 1}):
                    row = rows[index]
                    findings.append(finding(row["start"], row["text"], "公开字幕抽样"))
        except (PlatformReadError, httpx.HTTPError) as error:
            restriction = video_read_restriction(error, selection.bvid, selection.part, "字幕")
        has_subtitles = bool(findings)
        if not has_subtitles:
            gaps.append("归档中未取得此区间的字幕。" if archive else "此区间未取得可公开读取的字幕。")
        visual_count = 0
        seen = set()
        targets = preview_times(start, end)
        provenance = {}
        if archive:
            windows = "、".join(f"[{a:g}, {b:g})" for a, b in archive.windows)
            provenance = {"source_kind": "archived_video", "available_windows": archive.windows,
                          "message": f"已读取归档来源目录，P{selection.part} 可用时窗（秒）为 {windows}。"}
            if not ranges:
                provenance["message"] = "请求时段与归档原件没有交集。" + provenance["message"]
                gaps.append(provenance["message"])
            gaps.append("画面与元数据来自已授权归档。每支原件只覆盖列出的左闭右开时窗。")
            targets = ([(a+b)/2 for a,b in ranges] if len(ranges) > 1
                       else [t for a,b in ranges for t in preview_times(a,b)])
        vision_metadata = metadata | {"visual_text_tracks": text_tracks}
        if archive:
            vision_metadata["retrieval"] = "archive"
        for target in targets:
            if restriction:
                break
            if not granted():
                return {"error": "任务授权已变化。"}
            try:
                frame = await adapter.video_stream_frame(metadata, cid=part["cid"], timestamp=target)
                actual = frame["actual_time"]
                if (str(frame["cid"]) != str(part["cid"]) or not start <= actual <= target < frame["presentation_end"]
                        or actual in seen):
                    continue
                if not granted():
                    return {"error": "任务授权已变化。"}
                observed = await vision.analyze_video_evidence(
                    {"id": request.id}, vision_metadata, [frame],
                    "描述该帧可见的人物位置、物件与构图。屏字清晰时保留原文。",
                    deadline=time.time() + 90)
                if frame["id"] not in observed.get("evidence_ids", []):
                    continue
                text = (observed.get("frame_observations") or {}).get(frame["id"]) or observed.get("observations")
                if isinstance(text, str) and text.strip():
                    seen.add(actual)
                    visual_count += 1
                    point = finding(actual, text.strip(), "实际视频帧")
                    if archive:
                        point.update(source_kind="archived_video", archive_url=frame["archive_url"])
                    findings.append(point)
            except (InferenceQueueBusy, InferenceBudgetExceeded):
                raise
            except (PlatformReadError, httpx.HTTPError, ValueError, TimeoutError) as error:
                restriction = video_read_restriction(error, selection.bvid, selection.part, "视频画面")
                if restriction:
                    break
                gaps.append(f"请求 {target:.2f} 秒处未取得可用画面（{type(error).__name__}）。")
        if not granted():
            return {"error": "任务授权已变化。"}
        clip_receipt = {}
        if restriction:
            gaps.append(restriction["message"])
        elif (runtime_dir is not None and selection.end is not None and end - start <= 10
                and hasattr(adapter, "video_stream_clip")):
            clip_receipt = await save_clip(adapter, metadata, part, start, end, request, granted, runtime_dir)
            if "error" in clip_receipt:
                return clip_receipt
            if "clip_error" in clip_receipt:
                gaps.append(clip_receipt["clip_error"])
        gaps.append("画面内容按时点抽样描述；未听审原片音轨。")
        if selection.end is not None and end < selection.end:
            gaps.append(f"该分 P 实际时长 {end:g} 秒，之后没有素材。")
        coverage = {(True, True): "partial", (True, False): "subtitles_only", (False, True): "visual_only",
                    (False, False): "unavailable"}
        return {"status": "partial", "request_id": request.id, "viewing": coverage[has_subtitles, bool(visual_count)],
            "findings": findings, "gaps": gaps,
            "selection": {"bvid": selection.bvid, "ranges": [
                {"part": selection.part, "start": a, "end": b} for a,b in ranges]}, **provenance,
            "url": url, "part_duration": part["duration"], "cid": part["cid"],
            "error": None if findings else (restriction or clip_receipt).get("message")
                     or (provenance.get("message") if not ranges else None), **restriction, **clip_receipt}
    finally:
        await adapter.close()
