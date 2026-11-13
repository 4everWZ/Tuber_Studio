"""Shared public evidence; byte-bounded cache, provenance and single-flight reads."""

import asyncio
import base64
import copy
import hashlib
import json
import math
import re
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from ...api_providers import validate_image
from ...generation_meter import BrowsingBudgetDeferred
from ...media_cache import linked
from ...platforms.bilibili_browsing import CREDIT
from ...providers import public_error
from .local_asr import LocalVideoASR
from .models import DisabledVideoEvidence, browsing_filter_reason
from .round_context import read_round


class EvidenceUnavailable(ValueError):
    def __init__(self, kind, message, *, missing_material=False):
        self.kind = kind
        self.missing_material = missing_material
        super().__init__(message)


VISUAL_SAMPLING_VERSION = "cid-aligned-v2"
STORYBOARD_STEP = 12
MAX_VISUAL_INTERVAL = 18
STORYBOARD_SCAN_QUESTION = (
    "按输入顺序逐一说明每张画面实际可见的内容，即使画面静止或仅有文字。"
    "used_frames列出所有能描述的画面序号。"
)


def storyboard_windows(start, end):
    count = max(1, round((end - start) / STORYBOARD_STEP), math.ceil((end - start) / 15))
    step = (end - start) / count
    return [(start + index * step, end if index == count - 1 else start + (index + 1) * step)
            for index in range(count)]


def visual_gaps(start, end, samples):
    """A boundary can be covered by the nearest frame within half a gap."""
    times = sorted({time for time in samples if start <= time <= end})
    if not times:
        return [(start, end)]
    gaps = [(left, right) for left, right in zip(times, times[1:]) if right - left > MAX_VISUAL_INTERVAL]
    if times[0] - start > MAX_VISUAL_INTERVAL / 2:
        gaps.insert(0, (start, times[0]))
    if end - times[-1] > MAX_VISUAL_INTERVAL / 2:
        gaps.append((times[-1], end))
    return gaps


def observed_times(parts, cid):
    return {segment["start"] for part in parts if part.get("visual") and str(part["cid"]) == str(cid)
            for segment in part["segments"]}


def frame_provenance(frame):
    return {key: frame[key] for key in ("kind", "requested_time", "presentation_end", "bytes_read", "elapsed_seconds", "fallback_reason")
            if key in frame}


def storyboard_changes(parts, cid, start, end):
    observations = sorted((segment for part in parts if part.get("visual") and str(part["cid"]) == cid
                           for segment in part["segments"] if start <= segment["start"] <= end),
                          key=lambda item: item["start"])
    return [(previous["start"] + current["start"]) / 2
            for previous, current in zip(observations, observations[1:])
            if current["start"] - previous["start"] >= 10 and current["text"] != previous["text"]]


class BrowsingEvidence:
    def __init__(self, runtime, adapter, reader, visual=None):
        self.runtime, self.store, self.adapter, self.reader = runtime, runtime.store, adapter, reader
        self.visual = visual or DisabledVideoEvidence()
        self.pending = {}
        self._waiters = {}
        self.closed = False
        self.hits = self.merged = 0
        self.directory = runtime.settings.runtime / "browsing"
        if linked(self.directory):
            raise ValueError("浏览缓存目录不能使用链接")
        self.directory.mkdir(exist_ok=True)
        self.asr = LocalVideoASR(runtime.settings)
        self.store.execute("CREATE TABLE IF NOT EXISTS browsing_stream_frames (material_id TEXT PRIMARY KEY,bvid TEXT NOT NULL,"
                           "cid TEXT NOT NULL,version TEXT NOT NULL,provider TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL) "
                           "WITHOUT ROWID")
        self.store.execute("CREATE INDEX IF NOT EXISTS browsing_stream_frame_scope ON "
                           "browsing_stream_frames(bvid,cid,version,provider,start,end)")

    def load(self, identity, *, allow_expired=False):
        if not isinstance(identity, str) or not re.fullmatch(r"(?:[a-f0-9]{32}|[a-f0-9]{64})", identity):
            raise EvidenceUnavailable("invalid", "浏览材料标识无效")
        row = self.store.one("SELECT * FROM browsing_materials WHERE id=?", (identity,))
        if not row or (not allow_expired and row["expires_at"] < time.time()):
            raise EvidenceUnavailable("expired", "原始材料已失效，需重新读取")
        path = self.directory / (identity + ".json")
        if linked(self.directory) or linked(path) or not path.is_file():
            raise EvidenceUnavailable("cleared", "原始材料已清理，旧笔记不能独立用作事实")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise EvidenceUnavailable("too_large", "浏览材料超出原始字节上限")
        try:
            material = json.loads(path.read_bytes())
            if material["metadata"]["bvid"] != row["bvid"]:
                raise ValueError
        except (ValueError, KeyError, TypeError) as error:
            raise EvidenceUnavailable("changed", "浏览材料无法读取，需重新取材") from error
        self.runtime.cache.touch(path)
        return material

    @staticmethod
    def source_version(material, *, legacy=False):
        metadata = material["metadata"]
        tags = metadata.get("tags")
        if isinstance(tags, list) and not legacy:
            tags = sorted(tag for tag in tags if isinstance(tag, str))
        stable = {
            k: metadata.get(k)
            for k in (
                "bvid",
                "aid",
                "title",
                "description",
                "owner_uid",
                "parts",
                "duration",
                "published_at",
                "category_id",
            )
        }
        stable["tags"] = tags
        stable["parts_evidence"] = [
            {
                k: part.get(k)
                for k in (
                    "cid",
                    "track_id",
                    "language",
                    "is_ai",
                    "segments",
                    "coverage",
                    "visual",
                    "visual_provider",
                    "frame_evidence",
                    "limitations",
                )
            }
            for part in material["parts"]
        ]
        for part in stable["parts_evidence"]:
            if part["frame_evidence"] is not None:
                part["frame_evidence"] = [{key: frame[key] for key in ("id", "cid", "actual_time", "content_hash")}
                                          | ({"kind": "stream", "presentation_end": frame["presentation_end"]}
                                             if frame.get("kind") == "stream" else {})
                                          for frame in part["frame_evidence"]]
        return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def matches_source_version(self, material, version):
        return material["source_version"] == version or self.source_version(material, legacy=True) == version

    @asynccontextmanager
    async def _flight(self, key, factory):
        if self.closed:
            raise EvidenceUnavailable("cancelled", "浏览证据服务正在停止")
        key = (read_round.get(), key)
        if key in self.pending:
            self.merged += 1
            future = self.pending[key]
        else:
            future = asyncio.create_task(factory())
            self.pending[key] = future
        self._waiters[future] = self._waiters.get(future, 0) + 1
        try:
            yield await asyncio.shield(future)
        finally:
            remaining = self._waiters.get(future, 1) - 1
            if remaining:
                self._waiters[future] = remaining
            else:
                self._waiters.pop(future, None)
                if self.pending.get(key) is future:
                    self.pending.pop(key, None)
                if not future.done():
                    future.cancel()
                await asyncio.gather(future, return_exceptions=True)

    def _visual_identity(self):
        capabilities = dict(self.visual.capabilities())
        return (capabilities.get("provider_id"), capabilities.get("version"))

    def _current_visual(self, material):
        capabilities = dict(self.visual.capabilities())
        expected = {key: capabilities.get(key) for key in ("provider_id", "version")}
        parts = [part for part in material["parts"] if part.get("visual")]
        return bool(
            parts
            and material.get("visual_sampling_version") == VISUAL_SAMPLING_VERSION
            and capabilities.get("images")
            and all(expected.values())
            and all(part.get("visual_provider") == expected for part in parts)
        )

    def has_visual_provider(self):
        return bool(self.visual.capabilities().get("images"))

    def visual_batch_size(self):
        capabilities = self.visual.capabilities()
        maximum = capabilities.get("max_frames")
        return min(maximum, 6) if capabilities.get("multiple_images") and type(maximum) is int and maximum > 0 else 1

    @staticmethod
    def pending_visual_scan(material):
        return any(bool(scan["gaps"]) if "gaps" in scan else
                   any(window["state"] in {"pending", "retry"} for window in scan["windows"])
                   for scan in material.get("visual_scan", []))

    @staticmethod
    def material_mode(material):
        subtitles = any(not part.get("visual") and not part.get("asr") for part in material["parts"])
        visual = any(part.get("visual") for part in material["parts"])
        return "subtitle_visual" if subtitles and visual else "subtitle" if subtitles else "visual" if visual else "audio"

    @staticmethod
    def _cc_only(material):
        result = copy.deepcopy(material)
        result["parts"] = [part for part in result["parts"] if not part.get("visual")]
        result.pop("visual_frames", None)
        result.pop("visual_sampling_version", None)
        result.pop("visual_source_ids", None)
        result.pop("visual_requests", None)
        result.pop("frame_sampling", None)
        result.pop("visual_scan", None)
        result.pop("visual_change_cids", None)
        result.pop("visual_error", None)
        result["coverage"] = "partial" if result["missing_parts"] else "all_available_cc"
        return result

    async def prepare(
        self,
        video,
        config,
        *,
        force=False,
        task=None,
        deadline=None,
        visual_source_ids=(),
    ):
        # Sharing is only for public raw material. OC decisions never enter here.
        if self.closed:
            raise EvidenceUnavailable("cancelled", "浏览证据服务正在停止")
        visual = self.has_visual_provider()
        visual_requests = tuple(sorted({(item, "during", None) if isinstance(item, str) else tuple(item)
                                        for item in visual_source_ids}, key=repr))
        sampling = config.get("frame_sampling", "baseline")
        cached_cc = None
        if not force:
            for row in self.store.all(
                "SELECT id,expires_at FROM browsing_materials WHERE bvid=? AND expires_at>? ORDER BY created_at DESC LIMIT 4",
                (video, time.time()),
            ):
                try:
                    material = self.load(row["id"])
                    if material["languages"] == config["languages"]:
                        if any(part.get("asr") for part in material["parts"]) or (
                            self.asr.enabled and material["missing_parts"]
                        ):
                            continue  # Re-read opt-in local clips by their current content, never stale ASR.
                        reason = browsing_filter_reason(material["metadata"], config, complete=True)
                        if reason:
                            raise EvidenceUnavailable("filtered", reason)
                        has_visual = any(part.get("visual") for part in material["parts"])
                        needs_scan = bool(material["missing_parts"])
                        current_visual = visual and self._current_visual(material) and material.get("frame_sampling", "baseline") == sampling
                        if (current_visual and tuple(map(tuple, material.get("visual_requests", ()))) == visual_requests
                                and not self.pending_visual_scan(material)
                                or not visual_requests and not has_visual and (not visual or not needs_scan)):
                            self.hits += 1
                            return row["id"], material
                        base = material if current_visual and not material.get("visual_source_ids") else self._cc_only(material)
                        if base["parts"] and cached_cc is None:
                            cached_cc = (base, row["expires_at"])
                            if not visual_requests and (not visual or not needs_scan):
                                self.hits += 1
                                return await self._store(base, expires_at=row["expires_at"])
                except EvidenceUnavailable as error:
                    if error.kind == "filtered":
                        raise
                    continue
        if cached_cc is not None and (visual_requests or visual):
            self.hits += 1
            base, expires_at = cached_cc
            key = (
                "visual_followup",
                video,
                tuple(config["languages"]),
                visual_requests,
                self._visual_identity(),
                sampling,
                self.source_version(base),
            )
            async with self._flight(
                key,
                lambda: self._with_visual(
                    base,
                    task=task,
                    deadline=deadline,
                    expires_at=expires_at,
                    visual_requests=visual_requests,
                    sampling=sampling,
                ),
            ) as result:
                return result[0], copy.deepcopy(result[1])
        # Hold the metadata flight until content preparation finishes, so a late
        # waiter also shares it. Each caller applies its own exclusions before CC;
        # one OC's filter must not cancel another OC's permitted public read.
        async with self._flight(
            ("metadata", video), lambda: self.reader(self.adapter.video_metadata, video)
        ) as metadata:
            reason = browsing_filter_reason(metadata, config, complete=True)
            if reason:
                raise EvidenceUnavailable("filtered", reason)
            key = (
                "material",
                video,
                tuple(config["languages"]),
                visual,
                sampling,
                visual_requests,
                self._visual_identity(),
            )
            async with self._flight(
                key,
                lambda: self._prepare(
                    metadata,
                    config,
                    task=task,
                    deadline=deadline,
                    visual_requests=visual_requests,
                ),
            ) as result:
                return result[0], copy.deepcopy(result[1])

    async def _prepare(
        self, metadata, config, *, task=None, deadline=None, visual_requests=()
    ):
        parts, missing = [], []
        subtitle_error = None
        # Reading all parts is allowed only within the shared read/round budget.
        # A missing P remains an explicit limitation, never an empty complete video.
        for part in metadata["parts"]:
            try:
                parts.append(await self.subtitle_part(metadata, part, config["languages"]))
            except (ValueError, TimeoutError) as error:
                kind = getattr(error, "kind", "invalid")
                missing.append({"cid": part["cid"], "reason": kind})
                subtitle_error = subtitle_error or public_error(error)
        have_cc = bool(parts)
        if missing and self.asr.enabled:
            missing_cid = str(missing[0]["cid"])
            part = next(item for item in metadata["parts"] if str(item["cid"]) == missing_cid)
            try:
                transcription = await self.asr.transcribe(metadata, part, deadline=deadline)
                if transcription:
                    parts.append(transcription)
                    missing[0]["asr"] = "sampled_audio"
            except (ValueError, OSError, TimeoutError, wave.Error, EOFError):
                missing[0]["asr"] = "unavailable"
        sampling = config.get("frame_sampling", "baseline")
        material = {
            "metadata": metadata,
            "parts": parts,
            "missing_parts": missing,
            "languages": config["languages"],
            "fetched_at": time.time(),
            "coverage": "sampled_audio" if parts and not have_cc else "partial" if missing else "all_available_cc",
        }
        visual_failure = None
        if self.has_visual_provider() and (missing or visual_requests):
            visual_failure = await self._add_visual(material, task, deadline, visual_requests, sampling)
        if not material["parts"]:
            subtitle = subtitle_error or "无可用字幕"
            visual = material.get("visual_error") or "未配置多模态视觉接口"
            kind = getattr(visual_failure, "kind", None) or (missing[0]["reason"] if missing else "no_subtitles")
            raise EvidenceUnavailable(kind, f"取材失败 · 字幕：{subtitle} · 画面：{visual}", missing_material=True)
        return await self._store(material)

    async def subtitle_part(self, metadata, part, languages):
        evidence = await self.reader(self.adapter.subtitles, metadata["bvid"], part["cid"], languages)
        segments = evidence.get("segments", [])
        if not segments or not any(s.get("text", "").strip() for s in segments):
            raise EvidenceUnavailable("empty", "CC无有效正文")
        if all(CREDIT.match(s.get("text", "").strip()) for s in segments):
            raise EvidenceUnavailable("empty", "CC仅署名，不是有效视频内容")
        if str(evidence.get("cid")) != str(part["cid"]) or evidence.get("bvid") != metadata["bvid"]:
            raise EvidenceUnavailable("invalid", "字幕CID／视频不匹配")
        result = {key: value for key, value in evidence.items() if key != "url"}
        prefix = f"{part['cid']}:"
        result["segments"] = []
        for segment in segments:
            identity = str(segment["id"])
            result["segments"].append(dict(segment, id=identity if identity.startswith(prefix) else prefix + identity))
        return result

    @staticmethod
    def _bind_visual_sampling(material, frames, requests, sampling):
        material["visual_frames"] = frames
        material["visual_sampling_version"] = VISUAL_SAMPLING_VERSION
        material["visual_source_ids"] = list(dict.fromkeys(request[0] for request in requests))
        material["visual_requests"] = list(requests)
        material["frame_sampling"] = sampling

    def _visual_targets(self, material, requests):
        durations = {str(part["cid"]): part["duration"] for part in material["metadata"]["parts"]}
        segments = {segment["id"]: (str(part["cid"]), segment) for part in material["parts"]
                    if not part.get("visual") for segment in part["segments"]}
        targets = {}
        for identity, position, fraction, *options in requests:
            if identity not in segments:
                continue
            cid, segment = segments[identity]
            start, end = segment.get("start"), segment.get("end")
            if not self._valid_time(start) or not self._valid_time(end) or end < start:
                continue
            match position:
                case "before":
                    target = start - 3
                case "after":
                    target = end + 3
                case "within" if self._valid_time(fraction) and fraction <= 1:
                    target = start + fraction * (end - start)
                case _:
                    target = (start + end) / 2
            times = targets.setdefault(cid, [])
            limit = math.nextafter(float(durations[cid]), 0) if options == ["stream"] else durations[cid]
            target = min(limit, max(0, target))
            if target not in times:
                times.append(target)
        return targets

    async def _with_visual(self, material, *, task, deadline, expires_at, visual_requests, sampling):
        await self._add_visual(material, task, deadline, visual_requests, sampling)
        material["fetched_at"] = time.time()
        return await self._store(material, expires_at=expires_at)

    async def _add_visual(self, material, task, deadline, requests, sampling):
        metadata = material["metadata"]
        previous = {frame["id"]: frame for frame in material.get("visual_frames", [])}
        observed = {frame["id"] for part in material["parts"] for frame in part.get("frame_evidence", [])}
        scans = {scan["cid"]: scan for scan in material.get("visual_scan", [])}
        scan_index = {cid: scan["windows"] for cid, scan in scans.items()}
        batch_size = min(self.visual_batch_size(), 3 if sampling == "baseline" else 6)
        if requests:
            precise = [request for request in requests if len(request) == 4 and request[3] == "stream"]
            targets = self._visual_targets(material, [request for request in requests if request not in precise])
            batches = [(cid, times[offset:offset + batch_size], None, False)
                       for cid, times in targets.items() for offset in range(0, len(times), batch_size)]
            batches += [(cid, [timestamp], None, True) for cid, times in self._visual_targets(material, precise).items()
                        for timestamp in times]
        else:
            missing = {str(item["cid"]) for item in material["missing_parts"]}
            batches = []
            for part in metadata["parts"]:
                cid = str(part["cid"])
                if cid not in missing:
                    continue
                windows = scan_index.setdefault(cid, [
                    {"start": start, "end": end, "state": "pending", "samples": []}
                    for start, end in storyboard_windows(0, part["duration"])
                ])
                gaps = visual_gaps(0, part["duration"], observed_times(material["parts"], cid))
                pending = [window for window in windows if window["state"] in {"pending", "retry"}
                           and any(left < window["end"] and window["start"] < right for left, right in gaps)]
                batches.extend((cid, None, pending[offset:offset + batch_size], False)
                               for offset in range(0, len(pending), batch_size))
            material["visual_scan"] = [scans.setdefault(cid, {"cid": cid, "windows": windows})
                                       for cid, windows in scan_index.items()]
        if not batches and (requests or not scan_index):
            error = EvidenceUnavailable("no_frames", "没有可绑定到实际分P和时码的补看请求")
            material["visual_error"] = public_error(error)
            return error
        failure = None
        durations = {str(part["cid"]): part["duration"] for part in metadata["parts"]}
        for cid, timestamps, windows, precise in batches:
            try:
                if precise:
                    parts, frames = await self.inspect_frame(metadata, task, deadline, cid, timestamps[0], known_frames=observed)
                else:
                    parts, frames = await self._visual_parts(
                        metadata, task, deadline, cid=cid, timestamps=timestamps,
                        windows=[(window["start"], window["end"]) for window in windows] if windows else None,
                        known_frames=observed, question=STORYBOARD_SCAN_QUESTION if windows else None,
                    )
            except BrowsingBudgetDeferred as error:
                if not material["parts"]:
                    raise
                failure = error
                break
            except (ValueError, TimeoutError, OSError) as error:
                failure = error
                for window in windows or []:
                    window["state"] = "retry"
                if not material["parts"]:
                    break
                continue
            material["parts"].extend(parts)
            new_observations = {frame["id"] for part in parts for frame in part["frame_evidence"]}
            for frame in frames:
                if frame["id"] in new_observations and (requests or len(previous) < batch_size):
                    previous[frame["id"]] = frame
            observed.update(new_observations)
            samples = sorted({segment["start"] for part in parts for segment in part["segments"]})
            for window in windows or []:
                window["samples"] = [value for value in samples if window["start"] <= value < window["end"]
                                     or value == window["end"] == durations[cid]]
                window["state"] = "sampled" if window["samples"] else "gap"
        if not observed and failure is None and requests:
            failure = EvidenceUnavailable("no_frames", "指定时段没有可用故事板画面")
        if failure:
            material["visual_error"] = public_error(failure)
        else:
            material.pop("visual_error", None)
        if not requests and not failure:
            checked = set(material.get("visual_change_cids", []))
            for cid, duration in durations.items():
                if cid in checked or cid not in scan_index:
                    continue
                targets = storyboard_changes(material["parts"], cid, 0, duration)[:batch_size]
                if targets:
                    try:
                        parts, _ = await self._visual_parts(metadata, task, deadline, cid=cid,
                                                            timestamps=targets, known_frames=observed)
                    except (ValueError, TimeoutError, OSError) as error:
                        material["visual_error"] = public_error(error)
                        break
                    material["parts"].extend(parts)
                    observed.update(frame["id"] for part in parts for frame in part["frame_evidence"])
                checked.add(cid)
            material["visual_change_cids"] = sorted(checked)
        if not requests:
            for cid, scan in scans.items():
                def remaining():
                    return visual_gaps(0, durations[cid], observed_times(material["parts"], cid))

                attempts = set()
                while not failure and (gap := next((gap for gap in remaining() if gap not in attempts), None)):
                    attempts.add(gap)
                    timestamp = sum(gap) / 2
                    try:
                        parts, _ = await self._visual_parts(metadata, task, deadline, cid=cid,
                                                            timestamps=[timestamp], known_frames=observed)
                        material["parts"].extend(parts)
                        observed.update(frame["id"] for part in parts for frame in part["frame_evidence"])
                        if gap in remaining() and hasattr(self.adapter, "video_stream_frame"):
                            parts, _ = await self.inspect_frame(metadata, task, deadline, cid, timestamp,
                                                                known_frames=observed, allow_fallback=False)
                            material["parts"].extend(parts)
                            observed.update(frame["id"] for part in parts for frame in part["frame_evidence"])
                    except (ValueError, TimeoutError, OSError) as error:
                        failure = error
                        material["visual_error"] = public_error(error)
                scan["gaps"] = [{"start": left, "end": right} for left, right in remaining()]
            if any(scan["gaps"] for scan in scans.values()):
                material["visual_error"] = material.get("visual_error") or "部分时段画面间隔过大"
            if not observed and failure is None:
                failure = EvidenceUnavailable("no_frames", "指定时段没有可用画面")
        if observed:
            # Keep an illustrative subset of bitmaps. All observations and frame identities remain available.
            keep = []
            bitmap_size = 0
            for frame in previous.values():
                size = len(frame["image_base64"])
                if bitmap_size + size <= 512 * 1024 and (requests or len(keep) < batch_size):
                    keep.append(frame)
                    bitmap_size += size
            self._bind_visual_sampling(material, keep, requests, sampling)
            material["coverage"] = "sampled_visual"
        return failure

    async def _store(self, material, *, expires_at=None):
        material["source_version"] = self.source_version(material)
        raw = json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        if len(raw) > 2 * 1024 * 1024:
            raise EvidenceUnavailable("too_large", "字幕原始材料超过2MiB，未截断冒充完整读取")
        identity = uuid4().hex
        path = self.directory / (identity + ".json")
        async with self.runtime.cache.write("browsing", len(raw)) as allowed:
            if len(raw) > allowed:
                raise EvidenceUnavailable("cache_full", "浏览缓存空间不足")
            temporary = self.directory / (".download-" + uuid4().hex)
            try:
                temporary.write_bytes(raw)
                temporary.replace(path)
                self.store.execute(
                    "INSERT INTO browsing_materials VALUES(?,?,?,?,?,?)",
                    (
                        identity,
                        material["metadata"]["bvid"],
                        str(Path("browsing") / path.name),
                        len(raw),
                        min(expires_at, time.time() + 21600)
                        if expires_at is not None
                        else time.time() + 21600,
                        time.time(),
                    ),
                )
                self.runtime.cache.touch(path, seconds=60)
            finally:
                temporary.unlink(missing_ok=True)
        return identity, material

    @staticmethod
    def _valid_time(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    async def inspect_frame(self, metadata, task, deadline, cid, timestamp, *, allow_fallback=True, known_frames=frozenset()):
        part = next((item for item in metadata["parts"] if str(item["cid"]) == str(cid)), None)
        if part is None or not self._valid_time(timestamp) or timestamp >= part["duration"]:
            raise EvidenceUnavailable("invalid_frames", "目标时码超出已核验分 P")
        version = [str(metadata["aid"]), metadata.get("published_at"), str(cid), part["duration"]]
        provider = self._visual_identity()
        matches = self.store.all(
            "SELECT f.material_id FROM browsing_stream_frames f JOIN browsing_materials m ON m.id=f.material_id "
            "WHERE f.bvid=? AND f.cid=? AND f.version=? AND f.provider=? AND f.start<=? AND f.end>? AND m.expires_at>? "
            "ORDER BY m.created_at DESC LIMIT 4",
            (metadata["bvid"], str(cid), json.dumps(version), json.dumps(provider), timestamp, timestamp, time.time()),
        )
        for item in matches:
            try:
                saved = self.load(item["material_id"])
            except EvidenceUnavailable:
                continue
            if saved.get("precision_version") != version or not self._current_visual(saved):
                continue
            frame, = saved["visual_frames"]
            if frame["actual_time"] <= timestamp < frame["presentation_end"]:
                if frame["id"] in known_frames:
                    return [], []
                parts = copy.deepcopy(saved["parts"])
                for evidence in parts[0]["frame_evidence"]:
                    evidence["requested_time"] = timestamp
                    evidence["bytes_read"] = 0
                    evidence["elapsed_seconds"] = 0
                return parts, [frame | {"requested_time": timestamp, "bytes_read": 0, "elapsed_seconds": 0}]
        known_frames = frozenset(known_frames)
        async with self._flight(
            ("precise_frame", metadata["bvid"], tuple(version), timestamp, provider, allow_fallback, known_frames),
            lambda: self._inspect_frame(metadata, task, deadline, cid, timestamp, version, allow_fallback, known_frames),
        ) as (parts, frames):
            return copy.deepcopy(parts), copy.deepcopy(frames)

    async def _inspect_frame(self, metadata, task, deadline, cid, timestamp, version, allow_fallback, known_frames):
        parts, frames = await self._visual_parts(
            metadata, task, deadline, cid=cid, timestamps=[timestamp], precise=True, allow_fallback=allow_fallback,
            known_frames=known_frames,
            question="描述目标帧实际可见的动作、界面和可读文字。区分可辨认内容和看不清的细节。",
        )
        if parts and frames and frames[0].get("kind") == "stream":
            try:
                identity, _ = await self._store({"metadata": metadata, "parts": parts, "visual_frames": frames,
                    "precision_version": version, "visual_sampling_version": VISUAL_SAMPLING_VERSION,
                    "languages": [], "missing_parts": [], "coverage": "sampled_visual", "fetched_at": time.time()})
            except EvidenceUnavailable as error:
                if error.kind != "cache_full":
                    raise
            else:
                frame = frames[0]
                self.store.execute("DELETE FROM browsing_stream_frames WHERE material_id IN "
                                   "(SELECT id FROM browsing_materials WHERE expires_at<=?)", (time.time(),))
                self.store.execute("INSERT INTO browsing_stream_frames VALUES(?,?,?,?,?,?,?)",
                                   (identity, metadata["bvid"], str(cid), json.dumps(version),
                                    json.dumps(self._visual_identity()), frame["actual_time"], frame["presentation_end"]))
        return parts, frames

    async def _visual_parts(
        self, metadata, task, deadline, *, cid=None, timestamps=None, windows=None, known_frames=frozenset(), question=None,
        precise=False, allow_fallback=True,
    ):
        capabilities = dict(self.visual.capabilities())
        if not (
            capabilities.get("images")
            and capabilities.get("provider_id")
            and capabilities.get("version")
            and all(
                type(capabilities.get(key)) is int and capabilities[key] > 0
                for key in ("max_frames", "max_bytes", "max_pixels", "context_tokens")
            )
        ):
            raise EvidenceUnavailable("visual_unavailable", capabilities.get("reason", "视觉接口未配置"))
        if not hasattr(self.adapter, "video_frames"):
            raise EvidenceUnavailable("visual_unavailable", "平台未提供有实际时间的视频帧")
        task = dict(task or {})
        task["cancel_signal"] = asyncio.Event()
        frames = []
        timeout = capabilities.get("timeout_seconds", 60)
        if not self._valid_time(timeout) or not 0 < timeout <= 600:
            raise EvidenceUnavailable("visual_unavailable", "视觉超时须大于0且不超过600秒")
        try:
            async with asyncio.timeout(None if deadline is None else max(0, deadline - time.time())):
                import inspect

                sig = inspect.signature(self.adapter.video_frames)
                count = min(len(windows or timestamps or [None] * 3), self.visual_batch_size())
                call_args = [metadata, count]
                call_kwargs = {}
                if cid is not None:
                    if "cid" in sig.parameters:
                        call_kwargs["cid"] = cid
                    elif cid != str(metadata["parts"][0]["cid"]):
                        raise EvidenceUnavailable("visual_unavailable", "平台适配器无法指定要补读的分P")
                if windows and "windows" in sig.parameters:
                    call_kwargs["windows"] = windows
                elif windows:
                    timestamps = [(start + end) / 2 for start, end in windows]
                elif timestamps and "timestamps" not in sig.parameters and "windows" in sig.parameters and cid is not None:
                    duration = next(part["duration"] for part in metadata["parts"] if str(part["cid"]) == cid)
                    call_kwargs["windows"] = [(max(0, timestamp - 6), min(duration, timestamp + 6)) for timestamp in timestamps]
                if "timestamps" in sig.parameters and timestamps:
                    call_kwargs["timestamps"] = timestamps
                try:
                    async with asyncio.timeout(timeout):
                        failure = None
                        if precise:
                            try:
                                if not hasattr(self.adapter, "video_stream_frame"):
                                    raise EvidenceUnavailable("stream_unavailable", "当前来源未提供可跳转视频流")
                                frame = await self.reader(lambda: self.adapter.video_stream_frame(metadata, cid=cid, timestamp=timestamps[0]))
                                if not (frame.get("kind") == "stream" and self._valid_time(frame.get("presentation_end"))
                                        and frame["actual_time"] <= timestamps[0] < frame["presentation_end"]):
                                    raise EvidenceUnavailable("invalid_frames", "视频帧的呈现时间与目标不符")
                                frames = [frame]
                            except BrowsingBudgetDeferred:
                                raise
                            except (ValueError, TimeoutError, OSError) as error:
                                if not allow_fallback:
                                    raise
                                failure = public_error(error)
                        if (not precise or failure) and call_kwargs:
                            frames = await self.reader(
                                lambda *a: self.adapter.video_frames(*a, **call_kwargs), *call_args
                            )
                        elif not precise or failure:
                            frames = await self.reader(self.adapter.video_frames, *call_args)
                        if failure and frames:
                            frames = [frame | {"kind": "storyboard", "requested_time": timestamps[0], "fallback_reason": failure}
                                      for frame in frames]
                except TimeoutError:
                    raise EvidenceUnavailable("deadline", "读取视频画面超时") from None
                if frames == []:
                    return [], []
                if (
                    not isinstance(frames, list)
                    or len(frames) > count
                    or len(frames) > 1
                    and not capabilities.get("multiple_images")
                ):
                    raise EvidenceUnavailable("invalid_frames", "视频帧数量不符合Provider能力")
                durations = {str(part["cid"]): part["duration"] for part in metadata["parts"]}
                frame_map, retained, total_bytes = {}, [], 0
                for frame in frames:
                    if not isinstance(frame, dict):
                        raise EvidenceUnavailable("invalid_frames", "视频帧不是有效对象")
                    identity = frame.get("id")
                    frame_cid = str(frame.get("cid"))
                    actual_time = frame.get("actual_time")
                    image = frame.get("image")
                    if (
                        not isinstance(identity, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", identity)
                        or identity in frame_map
                        or frame_cid not in durations
                        or cid is not None
                        and frame_cid != cid
                        or not self._valid_time(actual_time)
                        or actual_time > durations[frame_cid]
                        or not isinstance(image, bytes)
                        or not image
                        or frame.get("kind") == "cover"
                        or any(
                            type(frame.get(key)) is not int or frame[key] <= 0 for key in ("width", "height")
                        )
                        or frame["width"] * frame["height"] > capabilities["max_pixels"]
                    ):
                        raise EvidenceUnavailable(
                            "invalid_frames", "只能使用有分P、实际时间和有效图像的有界帧"
                        )
                    if windows and not any(start <= actual_time < end or actual_time == end == durations[frame_cid]
                                           for start, end in windows):
                        continue
                    mime = (
                        "image/png"
                        if image.startswith(b"\x89PNG\r\n\x1a\n")
                        else "image/jpeg"
                        if image.startswith(b"\xff\xd8\xff")
                        else "image/webp"
                    )
                    try:
                        validate_image(image, mime)
                    except ValueError:
                        raise EvidenceUnavailable("invalid_frames", "视频帧不是受支持的内联图像") from None
                    total_bytes += len(image)
                    if total_bytes > capabilities["max_bytes"] or total_bytes > 2 * 1024 * 1024:
                        raise EvidenceUnavailable("invalid_frames", "视频帧字节超出Provider或原始材料限制")
                    frame_map[identity] = dict(frame, cid=frame_cid)
                    if identity in known_frames:
                        continue
                    retained.append(
                        {
                            "id": identity,
                            "cid": frame_cid,
                            "actual_time": actual_time,
                            "width": frame["width"],
                            "height": frame["height"],
                            "mime_type": mime,
                            "content_hash": hashlib.sha256(image).hexdigest(),
                            "image_base64": base64.b64encode(image).decode("ascii"),
                            **frame_provenance(frame),
                        }
                    )
                if known_frames:
                    frame_map = {identity: frame for identity, frame in frame_map.items() if identity not in known_frames}
                if not frame_map:
                    return [], []
                frames[:] = frame_map.values()
                result = await self.visual.analyze_video_evidence(
                    task,
                    metadata,
                    frames,
                    question or "描述这些画面中的人物、物品和动作，概括你看到的具体内容。",
                    deadline=deadline,
                )
                if not isinstance(result, dict):
                    raise EvidenceUnavailable("insufficient_visual", "视觉结果不是有效观察")
                observations = result.get("observations")
                refs = result.get("evidence_ids")
                per_frame = result.get("frame_observations")
                if per_frame is None and len(frames) == 1:
                    per_frame = {frames[0]["id"]: observations}
                limitations = result.get("limitations", [])
                if (
                    not isinstance(observations, str)
                    or not observations.strip()
                    or len(observations) > 100000
                    or not isinstance(refs, list)
                    or not refs
                    or len(refs) > len(frames)
                    or any(not isinstance(ref, str) or ref not in frame_map for ref in refs)
                    or len(set(refs)) != len(refs)
                    or not isinstance(per_frame, dict)
                    or set(per_frame) != set(refs)
                    or any(
                        not isinstance(text, str) or not text.strip() or len(text) > 100000
                        for text in per_frame.values()
                    )
                    or not isinstance(limitations, list)
                    or len(limitations) > 20
                    or any(not isinstance(value, str) or len(value) > 1000 for value in limitations)
                    or "sufficient" in result
                    and type(result["sufficient"]) is not bool
                    or result.get("sufficient") is False
                    or "sufficient" not in result
                    and any(result.get(key) is True for key in ("uncertain", "blurred", "unreadable"))
                    or any(
                        result.get(key, capabilities[key]) != capabilities[key]
                        for key in ("provider_id", "version")
                    )
                ):
                    raise EvidenceUnavailable(
                        "insufficient_visual", "视觉结果未能回答当前问题或缺少有效帧引用"
                    )
                provider = {key: capabilities[key] for key in ("provider_id", "version")}
                grouped = {}
                for ref in refs:
                    frame = frame_map[ref]
                    grouped.setdefault(frame["cid"], []).append(ref)
                parts = []
                for cid, part_refs in grouped.items():
                    parts.append(
                        {
                            "cid": cid,
                            "track_id": None,
                            "language": None,
                            "is_ai": None,
                            "visual": True,
                            "visual_provider": provider,
                            "limitations": limitations,
                            "segments": [
                                {
                                    "id": f"frame:{ref}",
                                    "start": frame_map[ref]["actual_time"],
                                    "end": frame_map[ref]["actual_time"],
                                    "text": per_frame[ref],
                                }
                                for ref in part_refs
                            ],
                            "coverage": {"complete": False, "kind": "sampled_frames", "frame_ids": part_refs},
                            "frame_evidence": [
                                {key: frame[key] for key in ("id", "cid", "actual_time", "content_hash")} | frame_provenance(frame)
                                for frame in retained
                                if frame["id"] in part_refs
                            ],
                        }
                    )
                return parts, retained
        except TimeoutError as error:
            raise EvidenceUnavailable("deadline", str(error) or "视觉证据任务达到期限") from None
        finally:
            task["cancel_signal"].set()
            try:
                await asyncio.wait_for(self.visual.cancel(task.get("id", "")), timeout=5)
            finally:
                if isinstance(frames, list):
                    frames.clear()

    async def cancel_pending(self):
        children = set(self.pending.values())
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        self.pending = {key: future for key, future in self.pending.items() if future not in children}

    async def close(self):
        self.closed = True
        await self.cancel_pending()
        await self.visual.close()
