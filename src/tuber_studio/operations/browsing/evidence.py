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


VISUAL_SAMPLING_VERSION = "cid-aligned-v1"


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

    @staticmethod
    def material_mode(material):
        subtitles = any(not part.get("visual") and not part.get("asr") for part in material["parts"])
        visual = bool(material.get("visual_frames"))
        return "subtitle_visual" if subtitles and visual else "subtitle" if subtitles else "visual" if visual else "audio"

    @staticmethod
    def _cc_only(material):
        result = copy.deepcopy(material)
        result["parts"] = [part for part in result["parts"] if not part.get("visual")]
        result.pop("visual_frames", None)
        result.pop("visual_sampling_version", None)
        result.pop("visual_source_ids", None)
        result.pop("frame_sampling", None)
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
        visual_source_ids = tuple(sorted(set(visual_source_ids)))
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
                        current_visual = visual and self._current_visual(material) and material.get("frame_sampling", "baseline") == sampling
                        if current_visual and tuple(material.get("visual_source_ids", ())) == visual_source_ids or not visual and not has_visual:
                            self.hits += 1
                            return row["id"], material
                        base = material if current_visual and visual_source_ids else self._cc_only(material)
                        if base["parts"] and cached_cc is None:
                            cached_cc = (base, row["expires_at"])
                            if not visual:
                                self.hits += 1
                                return await self._store(base, expires_at=row["expires_at"])
                except EvidenceUnavailable as error:
                    if error.kind == "filtered":
                        raise
                    continue
        if cached_cc is not None:
            self.hits += 1
            base, expires_at = cached_cc
            key = (
                "visual_followup",
                video,
                tuple(config["languages"]),
                visual_source_ids,
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
                    visual_source_ids=visual_source_ids,
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
                visual_source_ids,
                self._visual_identity(),
            )
            async with self._flight(
                key,
                lambda: self._prepare(
                    metadata,
                    config,
                    task=task,
                    deadline=deadline,
                    visual_source_ids=visual_source_ids,
                ),
            ) as result:
                return result[0], copy.deepcopy(result[1])

    async def _prepare(
        self, metadata, config, *, task=None, deadline=None, visual_source_ids=()
    ):
        parts, missing = [], []
        subtitle_error = None
        # Reading all parts is allowed only within the shared read/round budget.
        # A missing P remains an explicit limitation, never an empty complete video.
        for part in metadata["parts"]:
            try:
                evidence = await self.reader(
                    self.adapter.subtitles, metadata["bvid"], part["cid"], config["languages"]
                )
                segments = evidence.get("segments", [])
                if not segments or not any(s.get("text", "").strip() for s in segments):
                    raise EvidenceUnavailable("empty", "CC无有效正文")
                if all(CREDIT.match(s.get("text", "").strip()) for s in segments):
                    raise EvidenceUnavailable("empty", "CC仅署名，不是有效视频内容")
                if str(evidence.get("cid")) != str(part["cid"]) or evidence.get("bvid") != metadata["bvid"]:
                    raise EvidenceUnavailable("invalid", "字幕CID／视频不匹配")
                evidence = dict(evidence)
                evidence.pop("url", None)
                evidence["segments"] = [
                    dict(
                        s,
                        id=str(s["id"])
                        if str(s["id"]).startswith(f"{part['cid']}:")
                        else f"{part['cid']}:{s['id']}",
                    )
                    for s in segments
                ]
                parts.append(evidence)
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
        if self.has_visual_provider():
            visual_failure = await self._add_visual(material, task, deadline, visual_source_ids, sampling)
        if not material["parts"]:
            subtitle = subtitle_error or "无可用字幕"
            visual = material.get("visual_error") or "未配置多模态视觉接口"
            kind = getattr(visual_failure, "kind", None) or (missing[0]["reason"] if missing else "no_subtitles")
            raise EvidenceUnavailable(kind, f"取材失败 · 字幕：{subtitle} · 画面：{visual}", missing_material=True)
        return await self._store(material)

    @staticmethod
    def _bind_visual_sampling(material, frames, source_ids, sampling):
        material["visual_frames"] = frames
        material["visual_sampling_version"] = VISUAL_SAMPLING_VERSION
        material["visual_source_ids"] = list(source_ids)
        material["frame_sampling"] = sampling

    def _visual_targets(self, parts, source_ids, sampling="baseline"):
        groups = []
        selected = set(source_ids)
        for part in parts:
            segments = [
                segment
                for segment in part["segments"]
                if self._valid_time(segment.get("start"))
                and self._valid_time(segment.get("end"))
                and segment["end"] >= segment["start"]
            ]
            if segments:
                matched = [segment for segment in segments if segment["id"] in selected]
                groups.append((len(matched), part["cid"], matched or segments))
        if not groups:
            return None, None
        _, cid, segments = max(groups, key=lambda group: group[0])
        if sampling == "adaptive" and selected:
            if len(segments) > 3:
                segments = [segments[int(len(segments) * p)] for p in (0.15, 0.50, 0.85)]
            times = []
            for segment in segments:
                start, end = segment["start"], segment["end"]
                times.append((start + end) / 2)
                if end - start >= 4:
                    times.append(start + 0.75 * (end - start))
            return str(cid), list(dict.fromkeys(sorted(times)))
        if len(segments) > 3:
            segments = [segments[int(len(segments) * p)] for p in (0.15, 0.50, 0.85)]
        return str(cid), list(dict.fromkeys((s["start"] + s["end"]) / 2 for s in segments))

    async def _with_visual(self, material, *, task, deadline, expires_at, visual_source_ids, sampling):
        await self._add_visual(material, task, deadline, visual_source_ids, sampling)
        material["fetched_at"] = time.time()
        return await self._store(material, expires_at=expires_at)

    async def _add_visual(self, material, task, deadline, source_ids, sampling):
        cid, timestamps = self._visual_targets(material["parts"], source_ids, sampling) if source_ids else (None, None)
        previous = {frame["id"]: frame for frame in material.get("visual_frames", [])}
        observed = {frame["id"] for part in material["parts"] for frame in part.get("frame_evidence", [])}
        try:
            parts, frames = await self._visual_parts(
                material["metadata"], task, deadline, cid=cid, timestamps=timestamps, sampling=sampling,
                known_frames=observed,
            )
        except BrowsingBudgetDeferred:
            raise
        except Exception as error:
            material["visual_error"] = public_error(error)
            return error
        material.pop("visual_error", None)
        material["parts"].extend(parts)
        previous.update((frame["id"], frame) for frame in frames)
        self._bind_visual_sampling(material, list(previous.values()), source_ids, sampling)
        material["coverage"] = "sampled_visual"
        return None

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

    async def _visual_parts(
        self, metadata, task, deadline, *, cid=None, timestamps=None, sampling="baseline", known_frames=frozenset(),
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
        now = time.time()
        deadline = min(deadline if deadline is not None else now + timeout, now + timeout)
        try:
            async with asyncio.timeout(max(0, deadline - time.time())):
                import inspect

                sig = inspect.signature(self.adapter.video_frames)
                count = 3
                if sampling == "adaptive":
                    target_cid = str(cid or metadata["parts"][0]["cid"])
                    duration = next(part["duration"] for part in metadata["parts"] if str(part["cid"]) == target_cid)
                    count = len(timestamps) if timestamps else 3 if duration <= 45 else 4 if duration <= 180 else 6
                count = min(count, capabilities["max_frames"], 6) if capabilities.get("multiple_images") else 1
                call_args = [metadata, count]
                call_kwargs = {}
                if cid is not None:
                    if "cid" in sig.parameters:
                        call_kwargs["cid"] = cid
                    elif cid != str(metadata["parts"][0]["cid"]):
                        raise EvidenceUnavailable("visual_unavailable", "平台适配器无法指定要补读的分P")
                if "timestamps" in sig.parameters and timestamps:
                    call_kwargs["timestamps"] = timestamps
                if call_kwargs:
                    frames = await self.reader(
                        lambda *a: self.adapter.video_frames(*a, **call_kwargs), *call_args
                    )
                else:
                    frames = await self.reader(self.adapter.video_frames, *call_args)
                if (
                    not isinstance(frames, list)
                    or not frames
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
                        }
                    )
                if known_frames:
                    frame_map = {identity: frame for identity, frame in frame_map.items() if identity not in known_frames}
                    retained = [frame for frame in retained if frame["id"] in frame_map]
                if not frame_map:
                    return [], []
                frames[:] = frame_map.values()
                result = await self.visual.analyze_video_evidence(
                    task,
                    metadata,
                    frames,
                    "描述这些画面中的人物、物品和动作，概括你看到的具体内容。",
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
                                {key: frame[key] for key in ("id", "cid", "actual_time", "content_hash")}
                                for frame in retained
                                if frame["id"] in part_refs
                            ],
                        }
                    )
                return parts, retained
        except TimeoutError:
            raise EvidenceUnavailable("deadline", "视觉证据任务达到期限") from None
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
