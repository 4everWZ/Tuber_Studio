"""Shared public evidence; byte-bounded cache, provenance and single-flight reads."""

import asyncio
import base64
import copy
import hashlib
import json
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from .api_providers import validate_image
from .bilibili_browsing import CREDIT
from .browsing_models import DisabledVideoEvidence, browsing_filter_reason
from .media_cache import linked


class EvidenceUnavailable(ValueError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


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

    def load(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise EvidenceUnavailable("invalid", "浏览材料标识无效")
        row = self.store.one("SELECT * FROM browsing_materials WHERE id=?", (identity,))
        if not row or row["expires_at"] < time.time():
            raise EvidenceUnavailable("expired", "原始材料已失效，需重新读取")
        path = self.directory / (identity + ".json")
        if linked(self.directory) or linked(path) or not path.is_file():
            raise EvidenceUnavailable("cleared", "原始材料已清理，旧笔记不能独立用作事实")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise EvidenceUnavailable("too_large", "浏览材料超出原始字节上限")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != identity:
            raise EvidenceUnavailable("changed", "原始材料版本不一致")
        self.runtime.cache.touch(path)
        return json.loads(raw)

    @staticmethod
    def source_version(material):
        metadata = material["metadata"]
        stable = {
            k: metadata.get(k)
            for k in (
                "bvid",
                "aid",
                "title",
                "description",
                "tags",
                "owner_uid",
                "parts",
                "duration",
                "published_at",
                "category_id",
            )
        }
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

    @asynccontextmanager
    async def _flight(self, key, factory):
        if self.closed:
            raise EvidenceUnavailable("cancelled", "浏览证据服务正在停止")
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
            and capabilities.get("available")
            and capabilities.get("admitted")
            and all(expected.values())
            and all(part.get("visual_provider") == expected for part in parts)
        )

    @staticmethod
    def _cc_only(material):
        result = copy.deepcopy(material)
        result["parts"] = [part for part in result["parts"] if not part.get("visual")]
        result.pop("visual_frames", None)
        result["coverage"] = "partial" if result["missing_parts"] else "all_available_cc"
        return result

    async def prepare(self, video, config, *, force=False, task=None, deadline=None, require_visual=False):
        # Sharing is only for public raw material. OC decisions never enter here.
        if self.closed:
            raise EvidenceUnavailable("cancelled", "浏览证据服务正在停止")
        if require_visual and not config["visual_enabled"]:
            raise EvidenceUnavailable("visual_unavailable", "当前浏览配置未启用视觉")
        cached_cc = None
        if not force:
            for row in self.store.all(
                "SELECT id,expires_at FROM browsing_materials WHERE bvid=? AND expires_at>? ORDER BY created_at DESC LIMIT 4",
                (video, time.time()),
            ):
                try:
                    material = self.load(row["id"])
                    if material["languages"] == config["languages"]:
                        reason = browsing_filter_reason(material["metadata"], config, complete=True)
                        if reason:
                            raise EvidenceUnavailable("filtered", reason)
                        has_visual = any(part.get("visual") for part in material["parts"])
                        usable_visual = config["visual_enabled"] and self._current_visual(material)
                        if usable_visual or not has_visual and not require_visual:
                            self.hits += 1
                            return row["id"], material
                        base = self._cc_only(material)
                        if base["parts"] and cached_cc is None:
                            cached_cc = (base, row["expires_at"])
                            if not require_visual:
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
                require_visual,
                self._visual_identity(),
                self.source_version(base),
            )
            async with self._flight(
                key, lambda: self._with_visual(base, task=task, deadline=deadline, expires_at=expires_at)
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
                config["visual_enabled"],
                require_visual,
                self._visual_identity(),
            )
            async with self._flight(
                key,
                lambda: self._prepare(
                    metadata, config, task=task, deadline=deadline, require_visual=require_visual
                ),
            ) as result:
                return result[0], copy.deepcopy(result[1])

    async def _prepare(self, metadata, config, *, task=None, deadline=None, require_visual=False):
        parts, missing = [], []
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
            except ValueError as error:
                kind = getattr(error, "kind", "invalid")
                if kind not in {"no_subtitles", "empty"}:
                    raise
                missing.append({"cid": part["cid"], "reason": kind})
        visual_frames = []
        if not parts or require_visual:
            if not config["visual_enabled"]:
                raise EvidenceUnavailable(
                    missing[0]["reason"] if missing else "no_subtitles", "无可用CC字幕，未执行视频发言"
                )
            visual_parts, visual_frames = await self._visual_parts(metadata, task, deadline)
            parts.extend(visual_parts)
        material = {
            "metadata": metadata,
            "parts": parts,
            "missing_parts": missing,
            "languages": config["languages"],
            "fetched_at": time.time(),
            "coverage": "sampled_visual" if visual_frames else "partial" if missing else "all_available_cc",
        }
        if visual_frames:
            material["visual_frames"] = visual_frames
        return await self._store(material)

    async def _with_visual(self, material, *, task, deadline, expires_at):
        # Only raw CC is shared across provider identities. New observations
        # belong to the current provider and preserve the original CC tracks.
        material = self._cc_only(material)
        parts, frames = await self._visual_parts(material["metadata"], task, deadline)
        material["parts"].extend(parts)
        material["visual_frames"] = frames
        material["coverage"] = "sampled_visual"
        material["fetched_at"] = time.time()
        return await self._store(material, expires_at=expires_at)

    async def _store(self, material, *, expires_at=None):
        material["source_version"] = self.source_version(material)
        raw = json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        if len(raw) > 2 * 1024 * 1024:
            raise EvidenceUnavailable("too_large", "字幕原始材料超过2MiB，未截断冒充完整读取")
        identity = hashlib.sha256(raw).hexdigest()
        path = self.directory / (identity + ".json")
        async with self.runtime.cache.write("browsing", len(raw)) as allowed:
            if len(raw) > allowed:
                raise EvidenceUnavailable("cache_full", "浏览缓存空间不足")
            temporary = self.directory / (".download-" + uuid4().hex)
            try:
                temporary.write_bytes(raw)
                temporary.replace(path)
                self.store.execute(
                    "INSERT OR REPLACE INTO browsing_materials VALUES(?,?,?,?,?,?)",
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

    async def _visual_parts(self, metadata, task, deadline):
        capabilities = dict(self.visual.capabilities())
        if not (
            capabilities.get("available")
            and capabilities.get("images")
            and capabilities.get("admitted")
            and capabilities.get("provider_id")
            and capabilities.get("version")
            and all(
                type(capabilities.get(key)) is int and capabilities[key] > 0
                for key in ("max_frames", "max_bytes", "max_pixels", "context_tokens")
            )
        ):
            raise EvidenceUnavailable("visual_unavailable", capabilities.get("reason", "视觉资源未准入"))
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
                frames = await self.reader(
                    self.adapter.video_frames, metadata, min(3, capabilities["max_frames"])
                )
                if (
                    not isinstance(frames, list)
                    or not frames
                    or len(frames) > min(6, capabilities["max_frames"])
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
                    cid = str(frame.get("cid"))
                    actual_time = frame.get("actual_time")
                    image = frame.get("image")
                    if (
                        not isinstance(identity, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", identity)
                        or identity in frame_map
                        or cid not in durations
                        or not self._valid_time(actual_time)
                        or actual_time > durations[cid]
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
                    frame_map[identity] = dict(frame, cid=cid)
                    retained.append(
                        {
                            "id": identity,
                            "cid": cid,
                            "actual_time": actual_time,
                            "width": frame["width"],
                            "height": frame["height"],
                            "mime_type": mime,
                            "content_hash": hashlib.sha256(image).hexdigest(),
                            "image_base64": base64.b64encode(image).decode("ascii"),
                        }
                    )
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
