"""Private research on an explicitly selected public video without a platform account."""

import time

import httpx

from ..http_api import InferenceBudgetExceeded, InferenceQueueBusy
from ..platforms.bilibili import PlatformReadError
from ..platforms.bilibili_browsing import BrowsingAdapter


async def read_public_part(request, selection, vision, granted):
    if selection.part is None:
        return {"error": "匿名视频取材需要指定分 P。"}
    adapter = BrowsingAdapter()
    try:
        if not granted():
            return {"error": "任务授权已变化。"}
        metadata = await adapter.video_metadata(selection.bvid)
        part = next((p for p in metadata["parts"] if p["page"] == selection.part), None)
        if metadata["bvid"] != selection.bvid or not part:
            return {"error": "公开视频没有指定的分 P。"}
        start = selection.start or 0
        end = min(part["duration"], selection.end if selection.end is not None else part["duration"])
        if start >= end:
            return {"error": "指定区间超出该分 P 的实际时长。", "part_duration": part["duration"]}
        findings, gaps = [], []
        url = f"https://www.bilibili.com/video/{selection.bvid}/?p={selection.part}"

        def finding(seconds, text, kind):
            return {"part": selection.part, "cid": part["cid"], "seconds": seconds, "text": text,
                    "kind": kind, "url": f"{url}&t={int(seconds)}"}

        if not granted():
            return {"error": "任务授权已变化。"}
        try:
            subtitles = await adapter.subtitles(selection.bvid, part["cid"])
            rows = [row for row in subtitles["segments"] if start <= row["start"] < row["end"] <= end]
            if rows:
                for index in sorted({0, len(rows) // 2, len(rows) - 1}):
                    row = rows[index]
                    findings.append(finding(row["start"], row["text"], "公开字幕抽样"))
        except (PlatformReadError, httpx.HTTPError):
            pass
        has_subtitles = bool(findings)
        if not has_subtitles:
            gaps.append("此区间未取得可公开读取的字幕。")
        visual_count = 0
        seen = set()
        for fraction in (0.15, 0.5, 0.85):
            if not granted():
                return {"error": "任务授权已变化。"}
            target = start + fraction * (end - start)
            try:
                frame = await adapter.video_stream_frame(metadata, cid=part["cid"], timestamp=target)
                actual = frame["actual_time"]
                if (str(frame["cid"]) != str(part["cid"]) or not start <= actual <= target < frame["presentation_end"]
                        or actual in seen):
                    continue
                if not granted():
                    return {"error": "任务授权已变化。"}
                observed = await vision.analyze_video_evidence(
                    {"id": request.id}, metadata, [frame], "只描述该帧可见的构图与内容。",
                    deadline=time.time() + 90)
                if frame["id"] not in observed.get("evidence_ids", []):
                    continue
                text = (observed.get("frame_observations") or {}).get(frame["id"]) or observed.get("observations")
                if isinstance(text, str) and text.strip():
                    seen.add(actual)
                    visual_count += 1
                    findings.append(finding(actual, text.strip(), "实际视频帧"))
            except (InferenceQueueBusy, InferenceBudgetExceeded):
                raise
            except (PlatformReadError, httpx.HTTPError, ValueError, TimeoutError) as error:
                gaps.append(f"请求 {target:.2f} 秒处未取得可用画面（{type(error).__name__}）。")
        if not granted():
            return {"error": "任务授权已变化。"}
        gaps.append("画面按时点抽样，未连续观看或听审音轨。")
        if selection.end is not None and end < selection.end:
            gaps.append(f"该分 P 实际时长 {end:g} 秒，之后没有素材。")
        coverage = {(True, True): "partial", (True, False): "subtitles_only", (False, True): "visual_only",
                    (False, False): "unavailable"}
        return {"status": "partial", "request_id": request.id, "viewing": coverage[has_subtitles, bool(visual_count)],
            "findings": findings, "gaps": gaps,
            "selection": {"bvid": selection.bvid, "ranges": [{"part": selection.part, "start": start, "end": end}]},
            "url": url, "part_duration": part["duration"], "error": None}
    finally:
        await adapter.close()
