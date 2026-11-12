"""Read-only evidence collection for an operator-selected video and time ranges."""

import asyncio
import time
import wave
from uuid import uuid4

from ...generation_meter import BrowsingBudgetDeferred
from ...providers import public_error
from .evidence import EvidenceUnavailable


def _plan(metadata, selection):
    parts = {part.get("page", ordinal): part for ordinal, part in enumerate(metadata["parts"], 1)}
    ranges = selection["ranges"] or [
        {"part": number, "start": 0, "end": part["duration"]} for number, part in parts.items()
    ]
    selected = {}
    for item in ranges:
        number, start, end = item["part"], item["start"], item["end"]
        part = parts.get(number)
        if not part or not 0 <= start < end <= part["duration"]:
            raise ValueError(f"第 {number} P 的取材时段超出实际分 P 长度")
        entry = selected.setdefault(number, {
            "part": number, "cid": str(part["cid"]), "title": part.get("title", ""),
            "duration": part["duration"], "ranges": [],
        })
        entry["ranges"].append({"start": start, "end": end, "subtitle": None, "audio": None, "visual": None})
    if not selected:
        raise ValueError("视频缺少有效分 P")
    return {"title": metadata["title"], "aid": metadata["aid"], "parts": list(selected.values())}


def _part_identity(report):
    return report["aid"], [(part["part"], part["cid"], part["duration"]) for part in report["parts"]]


def _source_url(bvid, part, start):
    return f"https://www.bilibili.com/video/{bvid}/?p={part}&t={int(start)}"


def _interval_report(segment_part, interval, material_key, url):
    start, end = interval["start"], interval["end"]
    segments = [segment for segment in segment_part["segments"] if segment["start"] < end and segment["end"] > start]
    # Count the union of actual caption/transcript spans, leaving silence or
    # absent material explicit and avoiding overlap inflation.
    edge, seconds = start, 0
    for segment in sorted(segments, key=lambda item: item["start"]):
        left, right = max(edge, segment["start"]), min(end, segment["end"])
        seconds += max(0, right - left)
        edge = max(edge, right)
    return {
        "state": "available" if segments else "gap", "segments": len(segments),
        "track_complete": segment_part.get("coverage", {}).get("complete") is True,
        "covered_seconds": round(seconds, 3), "gap_seconds": round(end - start - seconds, 3),
        "material_key": material_key, "source_url": url,
    }


async def _store(browsing, metadata, parts, *, frames=None):
    key, _ = await browsing.evidence._store({
        "metadata": metadata, "parts": parts, "languages": [], "missing_parts": [],
        "coverage": "operator_selection", "fetched_at": time.time(),
        "visual_frames": frames or [],
    })
    return key


async def _track(browsing, row, metadata, part, selection, kind):
    try:
        if kind == "subtitle":
            material = await browsing.evidence.subtitle_part(metadata, part, row["config"]["languages"])
        else:
            if not browsing.evidence.asr.enabled:
                return [{"state": "unavailable", "reason": "未启用本地音频转录"}] * len(selection["ranges"])
            material = await browsing.evidence.asr.transcribe(metadata, part, deadline=row["deadline"])
        if material is None:
            return [{"state": "unavailable", "reason": "没有该分 P 的已配置音频"}] * len(selection["ranges"])
        key = await _store(browsing, metadata, [material])
        return [
            _interval_report(material, interval, key, _source_url(metadata["bvid"], selection["part"], interval["start"]))
            for interval in selection["ranges"]
        ]
    except BrowsingBudgetDeferred:
        raise
    except (ValueError, TimeoutError, OSError, wave.Error, EOFError) as error:
        state = "unavailable" if getattr(error, "kind", None) in {"empty", "no_subtitles"} else "retry"
        return [{"state": state, "reason": public_error(error)}] * len(selection["ranges"])


async def _visual(browsing, row, task_id, metadata, part, interval):
    if not browsing.evidence.has_visual_provider() or not hasattr(browsing.adapter, "video_frames"):
        return {"state": "unavailable", "reason": "未配置可用的视觉接口"}
    start, end = interval["start"], interval["end"]
    timestamps = [start + (end - start) * fraction for fraction in (0.2, 0.5, 0.8)]
    try:
        parts, frames = await browsing.evidence._visual_parts(
            metadata, {"id": task_id, "round_id": row["id"]}, row["deadline"],
            cid=part["cid"], timestamps=timestamps, sampling="adaptive",
        )
        samples = sorted({
            segment["start"] for source in parts for segment in source["segments"]
            if start <= segment["start"] < end
        })
        if not samples:
            return {"state": "gap", "reason": "没有取到指定时段内的有效画面"}
        key = await _store(browsing, metadata, parts, frames=frames)
        return {
            "state": "sampled", "samples": samples, "material_key": key,
            "source_url": _source_url(metadata["bvid"], part["part"], start),
        }
    except BrowsingBudgetDeferred:
        raise
    except (ValueError, TimeoutError, OSError) as error:
        return {"state": "retry", "reason": public_error(error)}


async def execute(browsing, row, metrics):
    selection = row["config"]["operator_selection"]
    bvid = selection["bvid"]
    task = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_tasks WHERE round_id=?", (row["id"],)))
    if task:
        identity = task["id"]
        payload = task["payload"]
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='discovered',attempts=0,lease_until=0 WHERE id=?", (identity,)
        )
    else:
        payload = {"bvid": bvid, "task_kind": "full_video"}
        identity = browsing.repo.task(row["id"], payload)
    owner = str(uuid4())
    generation = browsing.repo.claim(identity, owner)
    heartbeat = asyncio.create_task(browsing._heartbeat(identity, owner, generation), name=row["id"])
    browsing.heartbeats.add(heartbeat)
    heartbeat.add_done_callback(browsing.heartbeats.discard)

    def save(status="preparing", error=None):
        browsing._check_round(row)
        browsing.repo.complete(identity, owner, generation, status, payload, error=error)

    try:
        metadata = await browsing._read(browsing.adapter.video_metadata, bvid)
        if metadata.get("bvid") != bvid or not metadata.get("aid"):
            raise EvidenceUnavailable("invalid", "指定视频与平台实际来源不一致")
        current = _plan(metadata, selection)
        if prior := payload.get("full_coverage"):
            if _part_identity(current) != _part_identity(prior):
                raise EvidenceUnavailable("changed", "视频分 P 或时长已变化，请重新指派取材范围")
        else:
            payload["full_coverage"] = prior = current
        payload["metadata"] = {key: metadata[key] for key in ("bvid", "title")}
        save()
        by_cid = {str(part["cid"]): part for part in metadata["parts"]}
        # Advance untouched intervals first. Retrying previous failures before
        # them could consume every bounded attempt at the same earlier part.
        retry = all(
            interval[kind] is not None for part in prior["parts"]
            for interval in part["ranges"] for kind in ("subtitle", "audio", "visual")
        )

        def pending(result):
            return result is None or retry and result["state"] == "retry"

        for selected in prior["parts"]:
            part = by_cid[selected["cid"]]
            part_metadata = metadata | {"parts": [part]}
            for kind in ("subtitle", "audio"):
                if any(pending(interval[kind]) for interval in selected["ranges"]):
                    results = await _track(browsing, row, part_metadata, part, selected, kind)
                    for interval, result in zip(selected["ranges"], results, strict=True):
                        if pending(interval[kind]):
                            interval[kind] = result
                    save()
                    if any(result.get("material_key") for result in results):
                        metrics["prepared"] = 1
            for interval in selected["ranges"]:
                if pending(interval["visual"]):
                    interval["visual"] = await _visual(browsing, row, identity, part_metadata, selected, interval)
                    save()
                    if interval["visual"].get("material_key"):
                        metrics["prepared"] = 1
        finished = all(
            any(interval[kind]["state"] in {"available", "sampled"} for kind in ("subtitle", "audio", "visual"))
            and all(interval[kind]["state"] != "retry" for kind in ("subtitle", "audio", "visual"))
            for part in prior["parts"] for interval in part["ranges"]
        )
        status = "completed" if finished else "partial"
        save(status)
        metrics["prepared"] = 1
        return status
    except BrowsingBudgetDeferred as error:
        save("partial", public_error(error))
        return "partial"
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
