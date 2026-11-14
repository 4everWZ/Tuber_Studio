"""Priority video work in the ordinary video lane, with shared viewing and comment progress."""

import asyncio
import time
from uuid import uuid4

from ...generation_meter import BrowsingBudgetDeferred
from ...operation_turns import operation_lane
from ...providers import DemoProvider, public_error
from .caption_corrections import CORRECTED_VIDEOS, corrected_caption
from .comment_reader import commit_comment_context, enqueue_comment_visit, video_comment_context
from .evidence import (
    STORYBOARD_SCAN_QUESTION,
    EvidenceUnavailable,
    storyboard_changes,
    storyboard_windows,
    visual_gaps,
)
from .round_context import agent_depth


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
    return str(report["aid"]), [(part["part"], part["cid"], part["duration"]) for part in report["parts"]]


def _media_version(metadata):
    return [str(metadata["aid"]), metadata.get("published_at"),
            [[str(part["cid"]), part["duration"]] for part in metadata["parts"]]]


def _source_url(bvid, part, start):
    return f"https://www.bilibili.com/video/{bvid}/?p={part}&t={int(start)}"


def _interval_report(segment_part, interval, url):
    start, end = interval["start"], interval["end"]
    segments = [segment for segment in segment_part["segments"] if segment["start"] < end and segment["end"] > start]
    edge, seconds = start, 0
    for segment in sorted(segments, key=lambda item: item["start"]):
        left, right = max(edge, segment["start"]), min(end, segment["end"])
        seconds += max(0, right - left)
        edge = max(edge, right)
    return {
        "state": "available" if segments else "gap", "segments": len(segments),
        "track_complete": segment_part.get("coverage", {}).get("complete") is True,
        "covered_seconds": round(seconds, 3), "gap_seconds": round(end - start - seconds, 3),
        "source_url": url,
    }


def _needs_subtitle(interval, continuing_slice):
    previous = interval.get("subtitle")
    return not continuing_slice or not previous or previous["state"] not in {"unavailable", "retry"}


def _material(browsing, row, metadata):
    bvid = metadata["bvid"]
    progress = browsing.repo.content_progress(row, bvid) or {}
    prior = progress.get("material") or {}
    shared = browsing.repo.watch_evidence(row, bvid) or {}
    if shared.get("version") != _media_version(metadata):
        shared = {}
    material = shared.get("material") or {
        "metadata": metadata, "parts": [], "languages": row["config"]["languages"],
        "missing_parts": [], "coverage": "operator_selection", "fetched_at": time.time(),
    }
    material["metadata"] = metadata
    if prior.get("metadata") and _media_version(prior["metadata"]) == _media_version(metadata):
        old_parts = [
            part for part in prior["parts"]
            if not part.get("asr") and (
                part.get("visual") or prior.get("languages") == material["languages"] == row["config"]["languages"]
            )
        ]
        _append_parts(material, old_parts)
    if material["languages"] != row["config"]["languages"]:
        material["parts"] = [part for part in material["parts"] if part.get("visual")]
        material["languages"] = row["config"]["languages"]
    return material, shared.get("choices", {}), shared.get("scans", {})


def _append_parts(material, parts):
    known = {(str(part["cid"]), segment["id"]) for part in material["parts"] for segment in part["segments"]}
    for part in parts:
        cid = str(part["cid"])
        segments = [segment for segment in part["segments"] if (cid, segment["id"]) not in known]
        if segments:
            material["parts"].append(part | {"segments": segments})
            known.update((cid, segment["id"]) for segment in segments)


async def _subtitle(browsing, row, metadata, part, material):
    cid = str(part["cid"])
    for source in material["parts"]:
        if str(source["cid"]) == cid and not source.get("visual") and not source.get("asr"):
            return source, None
    try:
        source = await browsing.evidence.subtitle_part(metadata, part, row["config"]["languages"])
    except BrowsingBudgetDeferred:
        raise
    except (ValueError, TimeoutError, OSError) as error:
        state = "unavailable" if getattr(error, "kind", None) in {"empty", "no_subtitles"} else "retry"
        return None, {"state": state, "reason": public_error(error)}
    _append_parts(material, [source])
    return source, None


def _in_window(value, window, duration):
    return window["start"] <= value < window["end"] or value == window["end"] == duration


def _observations(material, cid, provider):
    parts = [part for part in material["parts"] if str(part["cid"]) == cid and part.get("visual")
             and part.get("visual_provider") == provider]
    return [segment for part in parts for segment in part["segments"]]


def _visual_report(material, part, interval, provider, scans, bvid):
    start, end = interval["start"], interval["end"]
    observations = _observations(material, part["cid"], provider)
    actual_times = sorted({item["start"] for item in observations})
    windows = []
    first_sample = 0
    for left, right in storyboard_windows(start, end):
        window = {"start": left, "end": right}
        while first_sample < len(actual_times) and actual_times[first_sample] < left:
            first_sample += 1
        last_sample = first_sample
        while last_sample < len(actual_times) and _in_window(actual_times[last_sample], window, part["duration"]):
            last_sample += 1
        samples = actual_times[first_sample:last_sample]
        first_sample = last_sample
        previous = scans.get(str([part["cid"], left, right]), "pending")
        # Existing checkpoints may store a bare state. Keep the reason next
        # to its window when a subsequent slice recomputes the report.
        result = previous if isinstance(previous, dict) else {"state": previous}
        if samples:
            result = {"state": "sampled"}
        windows.append(window | result | {"samples": samples})
    gaps = visual_gaps(start, end, actual_times)
    return {
        "state": "sampled" if not gaps else "partial",
        "complete": not gaps,
        "gaps": [{"start": left, "end": right} | scans.get(str([part["cid"], left, right, "gap"]), {}) for left, right in gaps],
        "windows": windows,
        "samples": sorted({value for window in windows for value in window["samples"]}),
        "source_url": _source_url(bvid, part["part"], start),
    }


def _highlights(material, part, interval, bvid):
    sources = sorted(
        ((segment if source.get("visual") or source.get("asr") else corrected_caption(bvid, source["cid"], segment),
          "画面" if source.get("visual") else "字幕")
         for source in material["parts"] if str(source["cid"]) == part["cid"]
         for segment in source["segments"] if (segment["start"] < interval["end"]
                                                or segment["start"] == interval["end"] == part["duration"])
         and (segment["start"] >= interval["start"] or segment["end"] > interval["start"])),
        key=lambda pair: pair[0]["start"],
    )
    if len(sources) > 12:
        sources = [sources[round(index * (len(sources) - 1) / 11)] for index in range(12)]
    return [
        {"id": source["id"], "start": source["start"], "text": source["text"][:500], "kind": kind,
         "source_url": _source_url(bvid, part["part"], source["start"]),
         **({"original_text": source["original_text"]} if "original_text" in source else {})}
        for source, kind in sources
    ]


def correct_coverage_captions(coverage, bvid):
    if not coverage or bvid not in CORRECTED_VIDEOS:
        return coverage
    return coverage | {"parts": [
        part | {"ranges": [
            interval | ({"highlights": [
                corrected_caption(bvid, part.get("cid"), entry)
                if entry.get("kind") == "字幕" else entry
                for entry in interval["highlights"]
            ]} if "highlights" in interval else {})
            for interval in part["ranges"]
        ]}
        for part in coverage["parts"]
    ]}


async def _visual_batch(browsing, row, identity, material, cid, *, timestamps=None, windows=None, precise=False, allow_fallback=True,
                        sparse_range=None):
    provider = dict(zip(("provider_id", "version"), browsing.evidence._visual_identity()))
    depth = agent_depth.set(agent_depth.get() + 1)
    try:
        task = {"id": identity + ":visual", "parent_id": identity, "round_id": row["id"]}
        known = {frame["id"] for part in material["parts"] if part.get("visual") and part.get("visual_provider") == provider
                 for frame in part.get("frame_evidence", [])}
        if precise:
            parts, _ = await browsing.evidence.inspect_frame(material["metadata"], task, row["deadline"], cid, timestamps[0],
                                                            known_frames=known, allow_fallback=allow_fallback)
        else:
            parts, _ = await browsing.evidence._visual_parts(
                material["metadata"], task, row["deadline"], cid=cid, timestamps=timestamps, windows=windows,
                question=STORYBOARD_SCAN_QUESTION if windows else None, known_frames=known,
                sparse_range=sparse_range,
                known_times={segment["start"] for segment in _observations(material, cid, provider)}
                if sparse_range else (),
            )
    finally:
        agent_depth.reset(depth)
    references = {segment["id"] for part in parts for segment in part["segments"]}
    if references:
        for part in material["parts"]:
            if part.get("visual") and part.get("visual_provider") != provider:
                part["segments"] = [segment for segment in part["segments"] if segment["id"] not in references]
                part["frame_evidence"] = [frame for frame in part.get("frame_evidence", [])
                                          if "frame:" + frame["id"] not in references]
        material["parts"] = [part for part in material["parts"] if part["segments"]]
    _append_parts(material, parts)


async def _watch_interval(browsing, row, identity, material, part, interval, choices, scans, save, *, retry):
    cid = part["cid"]
    capabilities = browsing.evidence.visual.capabilities()
    provider = {key: capabilities.get(key) for key in ("provider_id", "version")}
    bvid = material["metadata"]["bvid"]
    start, end = interval["start"], interval["end"]
    url = _source_url(bvid, part["part"], start)
    interval["audio"] = {"state": "unavailable", "reason": "读不到音轨"}
    if not browsing.evidence.has_visual_provider() or not hasattr(browsing.adapter, "video_frames"):
        interval["visual"] = {"state": "unavailable", "reason": "未配置可用的视觉接口", "source_url": url}
        interval["highlights"] = _highlights(material, part, interval, bvid)
        save()
        return
    batch_size = min(browsing.evidence.visual_batch_size(), 3 if row["config"]["frame_sampling"] == "baseline" else 6)
    policy = str(list(provider.values()))
    completed = choices.setdefault(policy, [])
    current_scans = scans.setdefault(policy, {})
    visual = interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
    choice_key = str([cid, start, end, interval["subtitle"].get("segments")])
    chosen_intervals = choices.setdefault("classified:" + policy, [])
    if (interval["subtitle"]["state"] == "available" and choice_key not in chosen_intervals
            and not isinstance(browsing.runtime.llm, DemoProvider)):
        subtitle = [source | {"segments": [segment for segment in source["segments"]
                                          if segment["start"] < end and segment["end"] > start]}
                    for source in material["parts"] if str(source["cid"]) == cid and not source.get("visual")]
        focus = material | {"parts": subtitle, "visual_source_ids": []}
        try:
            decisions = await browsing.decisions._content_decisions(focus, row, None, allow_visual=True)
        except BrowsingBudgetDeferred:
            raise
        except (ValueError, TimeoutError, OSError) as error:
            interval["visual_choice_error"] = public_error(error)
            decisions = []
        requests = {(target.evidence_id, target.position, target.fraction, target.precision)
                    for decision in decisions for target in decision.visual_targets}
        if not requests:
            requests = {(reference, "during", None, "storyboard") for decision in decisions
                        if decision.coverage_required == "visual" for reference in decision.evidence_ids[:1]}
        timestamps = browsing.evidence._visual_targets(focus, sorted(requests, key=repr)).get(cid, [])
        precise_times = browsing.evidence._visual_targets(focus, [item for item in requests if item[3] == "stream"]).get(cid, [])
        interval["visual_requests"] = [{"evidence_id": source, "position": position, "fraction": fraction, "precision": precision}
                                       for source, position, fraction, precision in sorted(requests, key=repr)]
        known_times = {item["start"] for item in _observations(material, cid, provider)}
        wanted = [target for target in timestamps if start <= target < end and target not in precise_times
                  and [cid, target] not in completed and not any(abs(target - time) <= 0.5 for time in known_times)]
        choice_ok = True
        for offset in range(0, len(wanted), batch_size):
            batch = wanted[offset:offset + batch_size]
            try:
                await _visual_batch(browsing, row, identity, material, cid, timestamps=batch)
            except BrowsingBudgetDeferred:
                raise
            except (ValueError, TimeoutError, OSError) as error:
                interval["visual"]["reason"] = public_error(error)
                choice_ok = False
                break
            completed.extend([cid, value] for value in batch)
            interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
            save()
        for target in precise_times:
            if start <= target < end and [cid, target, "stream"] not in completed:
                try:
                    await _visual_batch(browsing, row, identity, material, cid, timestamps=[target], precise=True)
                except BrowsingBudgetDeferred:
                    raise
                except (ValueError, TimeoutError, OSError) as error:
                    interval["visual_choice_error"] = public_error(error)
                    choice_ok = False
                else:
                    completed.append([cid, target, "stream"])
                    save()
        if choice_ok and not interval.get("visual_choice_error"):
            chosen_intervals.append(choice_key)
            save()
    visual = interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
    pending = [window for window in visual["windows"] if window["state"] == "pending"
               and any(gap["start"] < window["end"] and window["start"] < gap["end"] for gap in visual["gaps"])]
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        batch = [window for window in batch if any(gap["start"] < window["end"] and window["start"] < gap["end"]
                                                  for gap in visual["gaps"])]
        if not batch:
            continue
        result = {"state": "gap", "reason": "该时段没有通过核验的故事板画面"}
        try:
            await _visual_batch(browsing, row, identity, material, cid,
                                windows=[(window["start"], window["end"]) for window in batch])
        except BrowsingBudgetDeferred:
            raise
        except (ValueError, TimeoutError, OSError) as error:
            result = {"state": "retry", "reason": public_error(error)}
        for window in batch:
            current_scans[str([cid, window["start"], window["end"]])] = result
        visual = interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
        interval["highlights"] = _highlights(material, part, interval, bvid)
        save()
    attempted = set()
    while visual["gaps"]:
        gap = next((item for item in visual["gaps"] if (item["start"], item["end"]) not in attempted
                    and (retry or not item.get("stream_attempted"))), None)
        if not gap:
            break
        left, right = gap["start"], gap["end"]
        attempted.add((left, right))
        key = str([cid, left, right, "gap"])
        target = (left + right) / 2
        state = current_scans.setdefault(key, {})
        if not state.get("storyboard_attempted"):
            try:
                await _visual_batch(browsing, row, identity, material, cid, timestamps=[target], sparse_range=(left, right))
            except BrowsingBudgetDeferred:
                raise
            except (ValueError, TimeoutError, OSError) as error:
                state["reason"] = public_error(error)
            state["storyboard_attempted"] = True
            visual = interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
            save()
            if not any(item["start"] == left and item["end"] == right for item in visual["gaps"]):
                continue
        try:
            await _visual_batch(browsing, row, identity, material, cid, timestamps=[target], precise=True, allow_fallback=False)
        except BrowsingBudgetDeferred:
            raise
        except (ValueError, TimeoutError, OSError) as error:
            state["reason"] = public_error(error)
        else:
            state["reason"] = "取到的实际帧仍未覆盖该段"
        state["stream_attempted"] = True
        visual = interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
        interval["highlights"] = _highlights(material, part, interval, bvid)
        save()
    if visual["complete"] and choice_key not in choices.setdefault("changes:" + policy, []):
        # Observation and sampling are separate. Adjacent observed changes
        # warrant one intermediate frame; unchanged spans keep their baseline.
        observations = [source for source in material["parts"] if source.get("visual_provider") == provider]
        extra = [target for target in storyboard_changes(observations, cid, start, end) if [cid, target] not in completed]
        if extra:
            batch = extra[:batch_size]
            try:
                await _visual_batch(browsing, row, identity, material, cid, timestamps=batch)
            except BrowsingBudgetDeferred:
                raise
            except (ValueError, TimeoutError, OSError) as error:
                interval["visual"]["reason"] = public_error(error)
            else:
                completed.extend([cid, value] for value in batch)
                interval["visual"] = _visual_report(material, part, interval, provider, current_scans, bvid)
        choices["changes:" + policy].append(choice_key)
    interval["highlights"] = _highlights(material, part, interval, bvid)
    save()


def _viewing(report):
    ranges = [interval for part in report["parts"] for interval in part["ranges"]]
    captions = all(interval["subtitle"] and interval["subtitle"].get("track_complete") for interval in ranges)
    visual = all(interval["visual"] and interval["visual"].get("complete") for interval in ranges)
    return "watched" if captions and visual else "subtitles_only" if captions else "visual_only" if visual else "partial"


def _requires_resume(report):
    return any(
        interval["subtitle"].get("state") == "retry"
        or bool(interval["visual"].get("gaps"))
        for selected in report["parts"] for interval in selected["ranges"]
    )


def _checkpoint(payload):
    comments = payload.get("comment_progress", {})
    intervals = [interval for part in payload.get("full_coverage", {}).get("parts", []) for interval in part["ranges"]]
    return [
        sum(window["state"] in {"sampled", "gap"} for interval in intervals
            for window in (interval.get("visual") or {}).get("windows", [])),
        sum(len((interval.get("visual") or {}).get("samples", [])) for interval in intervals),
        comments.get("pages_read", 0), comments.get("thread_pages_read", 0),
        sum((interval.get("subtitle") or {}).get("state") in {"available", "gap", "unavailable"}
            for interval in intervals),
    ]


def _interaction_scope(browsing, row):
    if not browsing.operations.accepting_work:
        return None
    try:
        browsing.operations.assert_authorized(
            automatic=True, channel="comments", account_id=row["account_id"], character=browsing.runtime.character,
            epoch=row["epoch"],
        )
    except ValueError:
        return None
    config = browsing.effective()
    return row | {"preview": False, "config": row["config"] | config}


def _check_resource(metadata, context):
    bvid, aid = metadata["bvid"], metadata["aid"]
    if context.get("video_id", bvid) != bvid or str(context.get("aid", aid)) != str(aid):
        raise ValueError("评论页与指定视频不一致")


def _check_page(context, page):
    following = context.get("next_page")
    if (context.get("has_more") is True and following is None) or following is not None and (
        type(following) is not int or following <= page
    ):
        raise ValueError("平台评论分页没有前进，已保留原游标")
    return following


def _record_comments(browsing, row, identity, metadata, context):
    active = _interaction_scope(browsing, row)
    interactive = bool(active and (active["config"]["reply_enabled"] or active["config"].get("comment_like_enabled")))
    with browsing.store.db:
        items = context.get("items", [])
        enqueue_comment_visit(browsing, row, identity, metadata, items, interactive=False)
        if interactive:
            enqueue_comment_visit(browsing, active, identity, metadata, items)
    if interactive:
        browsing.interactions_changed.set()


def _thread_cursors(browsing, comments, items):
    if not hasattr(browsing.adapter, "video_thread_page"):
        return
    roots = {str(item["comment_id"]): item["reply_count"] for item in items
             if item.get("reply_count") and str(item.get("thread_id")) == str(item["comment_id"])}
    for item in items:
        root = str(item.get("thread_id"))
        if root in roots and str(item["comment_id"]) != root:
            roots[root] -= 1
    pending = comments.setdefault("threads", [])
    queued = {item["root"] for item in pending} | set(comments.get("read_threads", []))
    pending.extend({"root": root, "page": 1} for root, count in roots.items() if count > 0 and root not in queued)


async def _read_thread_pages(browsing, row, identity, metadata, comments, save):
    while comments.get("threads"):
        progress = comments["threads"][0]
        target = {"type": 1, "oid": str(metadata["aid"]), "bvid": metadata["bvid"],
                  "root": progress["root"], "parent": progress["root"]}
        context = await browsing._read(browsing.adapter.video_thread_page, target, page=progress["page"])
        following = _check_page(context, progress["page"])
        browsing._check_round(row)
        _record_comments(browsing, row, identity, metadata, context)
        comments["thread_pages_read"] = comments.get("thread_pages_read", 0) + 1
        if following:
            progress["page"] = following
        else:
            comments.setdefault("read_threads", []).append(progress["root"])
            comments["threads"].pop(0)
        save()
        await asyncio.sleep(0)


async def _read_comments(browsing, row, identity, payload, material, save, metrics):
    metadata = material["metadata"]
    bvid = metadata["bvid"]
    comments = payload["comment_progress"]
    if comments["at_end"]:
        return True
    newest = True
    while True:
        browsing._check_round(row)
        if newest and comments.get("root_page"):
            context = await video_comment_context(browsing, row, bvid, scroll=True, newest=True, latest_only=True)
            _check_resource(metadata, context)
            browsing._check_round(row)
            _record_comments(browsing, row, identity, metadata, context)
            _thread_cursors(browsing, comments, context.get("items", []))
            save()
            newest = False
        await _read_thread_pages(browsing, row, identity, metadata, comments, save)
        if previous := comments.pop("root_page", None):
            comments.update(next_page=previous["following"], at_end=previous["following"] is None)
            save()
            commit_comment_context(browsing, previous["context"])
        if comments["at_end"]:
            return True
        context = await video_comment_context(browsing, row, bvid, scroll=True, newest=newest)
        newest = False
        _check_resource(metadata, context)
        active = _interaction_scope(browsing, row)
        if not comments["pages_read"] and active and active["config"]["top_level_enabled"]:
            await browsing.decisions.priority_top_level(active, row, material, context, metrics)
        page = (context.get("_scroll_progress") or {}).get("page", 1)
        following = _check_page(context, page)
        browsing._check_round(row)
        _record_comments(browsing, row, identity, metadata, context)
        _thread_cursors(browsing, comments, context.get("items", []))
        comments["pages_read"] += 1
        comments["root_page"] = {"following": following, "context": {"_scroll_progress": context.get("_scroll_progress")}}
        save()
        await asyncio.sleep(0)


async def _inspect_target(browsing, row, identity, metadata, target):
    part = next((part for index, part in enumerate(metadata["parts"], 1)
                 if part.get("page", index) == target["part"]), None)
    if not part or target["seconds"] >= part["duration"]:
        raise EvidenceUnavailable("invalid_frames", "目标时码超出已核验分 P")
    task = {"id": identity + ":inspection", "parent_id": identity, "round_id": row["id"]}
    parts, frames = await browsing.evidence.inspect_frame(metadata, task, row["deadline"], str(part["cid"]), target["seconds"])
    if not parts or not frames:
        raise EvidenceUnavailable("no_frames", "目标时码没有可分析的画面")
    frame = frames[0]
    return {key: frame[key] for key in ("cid", "actual_time", "presentation_end", "requested_time", "kind", "fallback_reason",
                                        "bytes_read", "elapsed_seconds") if key in frame} | {
        "part": target["part"], "text": parts[0]["segments"][0]["text"],
        "source_url": _source_url(metadata["bvid"], target["part"], frame["actual_time"]),
        "preview": {"content": frame["image_base64"], "mime": frame["mime_type"]},
    }


async def execute(browsing, row, metrics):
    selection = row["config"]["operator_selection"]
    bvid = selection["bvid"]
    task = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_tasks WHERE round_id=?", (row["id"],)))
    if task:
        identity, payload = task["id"], task["payload"]
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
    material, choices, scans = None, {}, {}
    watch_dirty = False
    previous_checkpoint = _checkpoint(payload)

    def save(status="preparing", error=None):
        browsing._check_round(row)
        current_task = browsing.repo.decode(browsing.repo.check_owner(identity, owner, generation))
        for field in ("reply_intent_ids", "like_intent_ids", "repetition_decisions"):
            if field in current_task["payload"]:
                payload[field] = current_task["payload"][field]
        if report := payload.get("full_coverage"):
            report["viewing"] = _viewing(report)
        if watch_dirty and material is not None:
            material["source_version"] = browsing.evidence.source_version(material)
            browsing.repo.save_watch_evidence(row, bvid, {"version": _media_version(material["metadata"]),
                                                          "material": material, "choices": choices, "scans": scans})
        browsing.repo.complete(identity, owner, generation, status, payload, error=error)

    try:
        metadata = await browsing._read(browsing.adapter.video_metadata, bvid)
        if metadata.get("bvid") != bvid or not metadata.get("aid"):
            raise EvidenceUnavailable("invalid", "指定视频与平台实际来源不一致")
        if target := selection.get("frame"):
            payload["inspection"] = await _inspect_target(browsing, row, identity, metadata, target)
            payload["phase"] = "complete"
            save("completed")
            return "completed"
        current = _plan(metadata, selection)
        prior = payload.get("full_coverage")
        if prior and _part_identity(current) != _part_identity(prior):
            raise EvidenceUnavailable("changed", "视频分 P 或时长已变化，请重新指派取材范围")
        if prior:
            for previous, planned in zip(prior["parts"], current["parts"], strict=True):
                for old, new in zip(previous["ranges"], planned["ranges"], strict=True):
                    new.update({key: value for key, value in old.items() if key != "visual" or value and "windows" in value})
        continuing_slice = payload.pop("yielded", False)
        material, choices, scans = _material(browsing, row, metadata)
        payload["metadata"] = {key: metadata[key] for key in ("bvid", "title")}
        payload["full_coverage"] = current
        payload["phase"] = "watching"
        payload.setdefault("comment_progress", {"pages_read": 0, "next_page": None, "at_end": False}).pop("links", None)
        if not material["parts"] and not browsing.repo.quotas.start_watch(
            identity, row["account_id"], row["character_id"], bvid, row["config"]["daily_watch_limit"],
            row["config"]["business_timezone"],
        ):
            save("partial", "本业务日观看额度已用完")
            return "partial"
        save()
        by_cid = {str(part["cid"]): part for part in metadata["parts"]}
        watch_dirty = True
        for selected in current["parts"]:
            part = by_cid[selected["cid"]]
            if any(_needs_subtitle(interval, continuing_slice) for interval in selected["ranges"]):
                subtitle, failure = await _subtitle(browsing, row, metadata, part, material)
                for interval in selected["ranges"]:
                    url = _source_url(bvid, selected["part"], interval["start"])
                    interval["subtitle"] = failure or _interval_report(subtitle, interval, url)
                    interval["highlights"] = _highlights(material, selected, interval, bvid)
                save()
        for selected in current["parts"]:
            for interval in selected["ranges"]:
                await _watch_interval(browsing, row, identity, material, selected, interval, choices, scans, save,
                                      retry=bool(prior) and not continuing_slice)
            metrics["prepared"] = int(bool(material["parts"]))
        current["viewing"] = _viewing(current)
        usable = any(interval["subtitle"]["state"] == "available" or interval["visual"].get("samples")
                     for selected in current["parts"] for interval in selected["ranges"])
        material["missing_parts"] = [{"cid": selected["cid"], "reason": "no_subtitles"}
                                     for selected in current["parts"] if not any(
                                         interval["subtitle"].get("track_complete") for interval in selected["ranges"])]
        metrics["viewed"] = int(usable)
        payload["phase"] = "comments"
        save()
        watch_dirty = False
        browsing.repo.mark_watch_result(row, bvid, coverage=current["viewing"] if not selection["ranges"] else "partial")
        lane = operation_lane.set("reply")
        try:
            await _read_comments(browsing, row, identity, payload, material, save, metrics)
        finally:
            operation_lane.reset(lane)
        payload["phase"] = "complete"
        status = "completed" if usable and not _requires_resume(current) else "partial"
        save(status)
        return status
    except BrowsingBudgetDeferred as error:
        if any(current > previous for current, previous in zip(_checkpoint(payload), previous_checkpoint, strict=True)):
            payload["yielded"] = True
            save("checkpoint", public_error(error))
            return "waiting"
        save("partial", public_error(error))
        return "partial"
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
