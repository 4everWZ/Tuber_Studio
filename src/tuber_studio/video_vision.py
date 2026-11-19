"""Video-frame observations from a configured visual endpoint.

This module owns the API client and validates actual requests and source frames.
Resource measurements are used for deployment diagnostics and live admission.
"""

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from contextlib import nullcontext
from functools import partial
from heapq import nsmallest
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import Field, model_validator

from .api_providers import APIProvider, validate_image
from .generation_meter import GenerationMeter
from .http_api import response_json
from .models import Reply, StrictModel
from .providers import public_error, validate_reply

VIDEO_EVIDENCE_PROTOCOL = "sequential_captions_indexed_assessment_v2"


class VideoPartText:
    """Public text already read for one video part. Times are seconds on that part."""

    def __init__(self, metadata, cid, tracks):
        part = next(part for part in metadata["parts"] if str(part["cid"]) == cid)
        self.video = self.video_fields(metadata)
        self.part = {"CID": cid}
        if isinstance(part.get("title"), str) and part["title"].strip():
            self.part["分P名"] = part["title"].strip()[:160]
        rows = set()
        for track in tracks:
            if (track.get("visual") or not metadata.get("bvid") or track.get("bvid") != metadata["bvid"]
                    or str(track.get("cid")) != cid):
                continue
            source = "平台自动字幕" if track.get("is_ai") else "平台字幕"
            if track.get("asr"):
                source = "音频转写"
            elif track.get("retrieval") == "archive":
                source = "归档字幕"
            for segment in track.get("segments") or ():
                start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
                if (all(type(t) in (float, int) and math.isfinite(t) for t in (start, end))
                        and 0 <= start <= end and isinstance(text, str) and text.strip()):
                    rows.add((start, end, source, text.strip()[:160]))
        self.rows = sorted(rows)

    @staticmethod
    def video_fields(metadata):
        owner = metadata.get("owner")
        fields = [("标题", metadata.get("title"), 160), ("简介", metadata.get("description"), 400),
                  ("UP主", owner.get("name") if isinstance(owner, dict) else owner, 100)]
        video = {"BV": metadata.get("bvid"), "来源": "归档视频资料" if metadata.get("retrieval") == "archive" else "视频元数据"}
        video.update({key: value.strip()[:limit] for key, value, limit in fields if isinstance(value, str) and value.strip()})
        if metadata.get("retrieval") == "archive" and metadata.get("source_url"):
            video["投稿资料原址"] = metadata["source_url"]
            if origins := metadata.get("archived_programme_sources"):
                video["归档节目资料"] = [{"原网址": row["url"], "节目资料": row["credits"]} for row in origins]
        tags = metadata.get("tags")
        if isinstance(tags, list):
            labels = sorted({tag.strip()[:32] for tag in tags if isinstance(tag, str) and tag.strip()})[:6]
            if labels:
                video["标签"] = labels
        return video

    def version(self):
        data = json.dumps([self.video, self.part, self.rows], ensure_ascii=False).encode()
        return hashlib.sha256(data).hexdigest()

    def nearby(self, times):
        selected = set()
        for timepoint in times:
            candidates = ((max(0, row[0] - timepoint, timepoint - row[1]), abs(timepoint - row[0]), row)
                          for row in self.rows if (row[0] <= timepoint < row[1] if row[2] == "归档字幕"
                                                   else row[0] - 8 <= timepoint <= row[1] + 8))
            selected.update(row for _, _, row in nsmallest(2, candidates))
        return self.part | {"邻近文字": [
            {"开始秒": start, "结束秒": end, "来源": source, "正文": text}
            for start, end, source, text in sorted(selected)
        ]}


def frame_reading_context(sources, frames):
    if not sources:
        return {}
    times = {}
    for frame in frames:
        times.setdefault(str(frame["cid"]), []).append(frame["actual_time"])
    return {"视频资料": next(iter(sources.values())).video,
            "分P资料": [sources[cid].nearby(points) for cid, points in times.items()]}


TEXT_SOURCE_NOTE = "以下视频资料与带时码文字仅供理解语境。画面所见须以图像为据。"


class VideoVisionAdmissionRequired(ValueError):
    kind = "visual_unavailable"


class VideoVisionConfig(StrictModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = Field(min_length=1)
    model_revision: str = ""
    runtime_revision: str = ""
    max_frames: int = Field(default=5, ge=1, le=16, strict=True)
    max_pixels: int = Field(default=1024 * 1024, ge=1, le=4096 * 4096, strict=True)
    max_bytes: int = Field(default=1024 * 1024, ge=1, le=4 * 1024 * 1024, strict=True)
    max_output_tokens: int = Field(default=2048, ge=256, le=65536, strict=True)
    enable_thinking: bool | None = Field(default=None, strict=True)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    image_order: Literal["text_first", "image_first"] = "text_first"
    frame_strategy: Literal["sequential", "joint"] = "sequential"

    @model_validator(mode="after")
    def valid(self):
        if not self.model.strip():
            raise ValueError("视觉模型 ID 不能为空")
        if self.frame_strategy == "joint" and self.max_frames > 8:
            raise ValueError("联合视觉请求最多使用八帧")
        if any(len(value) > 2048 for value in (self.model, self.model_revision, self.runtime_revision)):
            raise ValueError("视觉版本身份过长")
        return self


class ApiVideoEvidenceProvider:
    def __init__(self, runtime, config, *, api_key=None, background=False):
        self.runtime = runtime
        self.config = VideoVisionConfig.model_validate(config)
        self.api = APIProvider(
            self.config.model_dump() | {
                "max_tokens": self.config.max_output_tokens,
                "prefill_timeout_seconds": self.config.timeout_seconds,
                "structured_output": "json_schema",
                "context_source": "model_card",
                "prompt_token_endpoint": background,
            },
            api_key,
            background=background,
        )
        self._probe = None
        self._admission = None
        self._active = {}

    def identity(self):
        """Public configuration bound to an external device benchmark record."""
        configuration = self.config.model_dump(exclude={"timeout_seconds", "frame_strategy"})
        configuration.update(adapter="responses" if self.api.background else "omni", context_source="model_card")
        return configuration | {
            "protocol": (
                VIDEO_EVIDENCE_PROTOCOL
                if self.config.frame_strategy == "sequential"
                else "joint_captions_indexed_assessment_v1"
            )
        }

    @property
    def version(self):
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True).encode()).hexdigest()

    def capabilities(self):
        probe = self._probe or {}
        configured = not self.api.client.is_closed
        available = bool(configured and probe.get("images"))
        admitted = bool(available and self._admission)
        result = {
            "available": available,
            "images": configured,
            "admitted": admitted,
            "multiple_images": configured and self.config.max_frames > 1,
            "provider_id": self.config.model,
            "version": self.version,
            "max_frames": self.config.max_frames,
            "max_pixels": self.config.max_pixels,
            "max_bytes": self.config.max_bytes,
            "context_tokens": probe.get("context_tokens"),
            "timeout_seconds": self.config.timeout_seconds,
            "runtime_context_tokens": probe.get("context_tokens"),
            "reason": "" if configured else "视觉客户端已关闭",
        }
        result["provenance"] = (probe.get("properties") or {}).get("provenance")
        return result

    async def _runtime_properties(self):
        client = self.api.client
        timeout = min(20, self.config.timeout_seconds)
        registry = await response_json(client, "GET", "models", timeout=timeout)
        cards = registry.get("data")
        if not isinstance(cards, list):
            raise VideoVisionAdmissionRequired("网关未报告视觉模型窗口")
        window = next((card.get("max_model_len") for card in cards
                       if isinstance(card, dict) and card.get("id") == self.config.model), None)
        if type(window) is not int or window < 4096 or self.config.max_output_tokens >= window:
            raise VideoVisionAdmissionRequired("视觉模型未提供可核验的窗口或输出预算未留下输入空间")
        provenance = {"alias": self.config.model, "max_model_len": window}
        pins = {"checkpoint": self.config.model_revision, "engine_version": self.config.runtime_revision}
        if any(pins.values()):
            path = client.base_url.path.rstrip("/").removesuffix("/v1") + "/healthz"
            health = await response_json(client, "GET", client.base_url.copy_with(path=path), timeout=timeout)
            if health.get("alias", self.config.model) != self.config.model or any(
                value and health.get(key) != value for key, value in pins.items()
            ):
                raise VideoVisionAdmissionRequired("视觉服务未确认配置锁定的版本")
            provenance.update({key: health[key] for key in (
                "model", "checkpoint", "checkpoint_source", "checkpoint_root_verified", "engine_version", "engine_version_source",
            ) if key in health})
        return {"context_tokens": window, "provenance": provenance, "endpoint": str(client.base_url)}

    def _record_observation(self, properties, frame_count):
        self._probe = {
            "images": True, "properties": properties, "context_tokens": properties["context_tokens"],
            "tested_frames": frame_count,
        }

    def _validate_frames(self, metadata, frames):
        if not isinstance(frames, list) or not 0 < len(frames) <= self.config.max_frames:
            raise ValueError("视觉帧数量超出配置")
        durations = {str(part["cid"]): part["duration"] for part in metadata.get("parts", [])}
        identities, total = set(), 0
        for frame in frames:
            identity, actual, image = frame.get("id"), frame.get("actual_time"), frame.get("image")
            cid = str(frame.get("cid"))
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", identity)
                or identity in identities
                or cid not in durations
                or frame.get("kind") == "cover"
                or type(actual) not in (int, float)
                or not math.isfinite(actual)
                or not 0 <= actual <= durations[cid]
                or any(
                    type(frame.get(key)) is not int or not 0 < frame[key] <= 4096
                    for key in ("width", "height")
                )
                or frame["width"] * frame["height"] > self.config.max_pixels
            ):
                raise ValueError("视觉帧缺少可信CID、实际时间、唯一ID或有界尺寸")
            validate_image(image, frame.get("mime_type", "image/jpeg"))
            total += len(image)
            identities.add(identity)
        if total > self.config.max_bytes:
            raise ValueError("视觉帧总字节超限")
        return identities

    @staticmethod
    def _schema(frame_count, *, joint=False):
        properties = {
            "sufficient": {"type": "boolean"},
            "used_frames": {
                "type": "array",
                "maxItems": frame_count,
                "items": {"type": "integer", "minimum": 1, "maximum": frame_count},
            },
            "summary": {"type": "string", "maxLength": 12000},
            "limitations": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 1000},
            },
        }
        if joint:
            properties["captions"] = {
                "type": "array",
                "minItems": frame_count,
                "maxItems": frame_count,
                "items": {"type": "string", "minLength": 1, "maxLength": 4000},
            }
        return {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(properties),
                    "properties": properties
                    | {
                        "sufficient": {"const": sufficient},
                        "used_frames": properties["used_frames"] | {"minItems": int(sufficient)},
                        "summary": properties["summary"] | {"minLength": int(sufficient)},
                    },
                }
                for sufficient in (True, False)
            ],
        }

    @staticmethod
    def _bind_assessment(result, captions):
        if (
            not isinstance(result, dict)
            or set(result) != {"sufficient", "used_frames", "summary", "limitations"}
            or type(result.get("sufficient")) is not bool
        ):
            raise ValueError("视觉JSON缺少明确充分性")
        numbers = result["used_frames"]
        if (
            not isinstance(numbers, list)
            or any(type(number) is not int or not 1 <= number <= len(captions) for number in numbers)
            or len(numbers) != len(set(numbers))
            or not isinstance(result["summary"], str)
            or len(result["summary"]) > 12000
            or not isinstance(result["limitations"], list)
            or len(result["limitations"]) > 20
            or any(not isinstance(text, str) or len(text) > 1000 for text in result["limitations"])
        ):
            raise ValueError("视觉JSON存在未知序号或越界文本")
        if result["sufficient"] and (not numbers or not result["summary"].strip()):
            raise ValueError("视觉JSON声称充分但没有帧依据")
        selected = [captions[number - 1] for number in numbers]
        return {
            "sufficient": result["sufficient"],
            "observations": result["summary"],
            "evidence_ids": [item["frame"]["id"] for item in selected],
            "frame_observations": {item["frame"]["id"]: item["text"] for item in selected},
            "limitations": result["limitations"],
        }

    def _check_properties(self, props, expected, identity):
        if self.identity() != identity or expected is not None and props != expected:
            self._probe = self._admission = None
            raise VideoVisionAdmissionRequired("视觉服务实际模型、窗口或配置在取材时发生变化")
        if self._probe and props != self._probe["properties"]:
            self._probe = self._admission = None

    async def _confirmed_properties(self, expected, identity):
        try:
            props = await self._runtime_properties()
        except Exception:
            self._probe = self._admission = None
            raise
        self._check_properties(props, expected, identity)
        return props

    async def _stage(self, task, deadline, identity, expected, repository, operation):
        started = time.monotonic()
        slot = self.runtime.offline_generation() if self.api.background else self.runtime.operator_generation()
        async with slot:
            self._check_task(task, deadline)
            props = await self._confirmed_properties(expected, identity)
            self.api.adopt_context_window(props["context_tokens"])
            self._check_task(task, deadline)
            before = repository.round(task["round_id"])["budget"].copy() if repository else None
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    value = await operation()
            except TimeoutError:
                raise TimeoutError("视觉模型请求超时") from None
            self._check_task(task, deadline)
            usage = {"generation": dict(self.api.last_usage), "including_token_probes": None}
            if repository:
                after = repository.round(task["round_id"])["budget"]
                usage["including_token_probes"] = {
                    key: after[key] - before[key] for key in ("model_calls", "input_tokens", "output_tokens")
                }
        return value, props, usage | {"elapsed_ms": round((time.monotonic() - started) * 1000, 3)}

    async def _image_content(self, frame):
        mime_type = frame.get("mime_type", "image/jpeg")
        data = base64.b64encode(frame["image"]).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}, None

    async def generate_live_reply(self, scene, messages, llm, event_id):
        if (type(scene.width) is not int or type(scene.height) is not int
                or not 0 < scene.width <= 4096 or not 0 < scene.height <= 4096
                or scene.width * scene.height > self.config.max_pixels
                or not isinstance(scene.image, bytes) or len(scene.image) > self.config.max_bytes):
            return None
        validate_image(scene.image, scene.mime_type)
        if self.api.background:
            api = self.runtime.llm
        else:
            if (not self.capabilities()["admitted"] or llm.get("model") != self.config.model
                    or llm.get("base_url", "").rstrip("/") != self.config.base_url.rstrip("/")):
                return None
            api = self.api
            identity, expected = self.identity(), self._probe["properties"]
            try:
                props = await self._confirmed_properties(expected, identity)
            except (ValueError, httpx.HTTPError):
                return None
            api.adopt_context_window(props["context_tokens"])
        image, _ = await self._image_content({"image": scene.image, "mime_type": scene.mime_type})
        if not scene.current(scene.session_id):
            return None
        schema = Reply.model_json_schema()
        schema["properties"]["reply_to"] |= {"items": {"type": "string", "enum": [event_id]}, "maxItems": 1}
        async with asyncio.timeout(self.config.timeout_seconds):
            value = await api.generate_json(scene.annotate(messages, image=image), schema, max_tokens=llm["max_tokens"])
        if not scene.current(scene.session_id):
            return None
        if not self.api.background:
            self._check_properties(props, expected, identity)
        return validate_reply(json.dumps(value), None, event_id)

    async def _observe_sequential(self, task, frames, question, deadline, identity, expected, repository, sources):
        captions, stages = [], []
        caption_schema = {
            "type": "object", "required": ["caption"], "additionalProperties": False,
            "properties": {"caption": {"type": "string", "minLength": 1, "maxLength": 4000}},
        }
        budget = self.config.max_output_tokens
        for number, frame in enumerate(frames, 1):
            image, preprocessing = await self._image_content(frame)
            context = frame_reading_context(sources, [frame])
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"这张画面来自分P {frame['cid']}，实际采样时间为"
                            f" {frame['actual_time']} 秒。描述这张画面中的人物、物品和动作，"
                            "结合当前问题给出相关所见。当前问题：" + question
                            + ("\n" + TEXT_SOURCE_NOTE + json.dumps(context, ensure_ascii=False) if context else ""),
                        },
                        image,
                    ],
                }
            ]
            if self.config.image_order == "image_first":
                messages[0]["content"].reverse()
            text, props, stage = await self._stage(
                task,
                deadline,
                identity,
                expected,
                repository,
                partial(self.api.generate_json, messages, caption_schema, max_tokens=budget),
            )
            expected = props
            text = text.get("caption")
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise ValueError("视觉逐帧观察须为非空且有界的自然文本")
            captions.append({"frame": frame, "text": text})
            stages.append(
                stage
                | {
                    "stage": "caption",
                    "number": number,
                    "frame_id": frame["id"],
                    "preprocessing": preprocessing,
                }
            )
        messages = [
            {
                "role": "system",
                "content": "围绕当前问题，根据下列逐帧观察作答。"
                "sufficient是这些观察是否足以回答问题的判断；"
                "used_frames填写所用观察的整数编号，summary写自然回答，"
                "limitations写还需补充的相关信息。" + (TEXT_SOURCE_NOTE if sources else ""),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        **({"文字背景": frame_reading_context(sources, frames)} if sources else {}),
                        "observations": [
                            {
                                "number": number,
                                "cid": item["frame"]["cid"],
                                "actual_time": item["frame"]["actual_time"],
                                "text": item["text"],
                            }
                            for number, item in enumerate(captions, 1)
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        assessment, props, stage = await self._stage(
            task,
            deadline,
            identity,
            expected,
            repository,
            lambda: self.api.generate_json(
                messages, self._schema(len(captions)),
                max_tokens=self.config.max_output_tokens,
            ),
        )
        stages.append(stage | {"stage": "assessment"})
        return captions, assessment, props, stages

    async def _observe_joint(self, task, frames, question, deadline, identity, expected, repository, sources):
        content, preprocessing = [], []
        for number, frame in enumerate(frames, 1):
            image, metadata = await self._image_content(frame)
            label = {
                "type": "text",
                "text": f"画面 {number}｜分P {frame['cid']}｜{frame['actual_time']} 秒",
            }
            content.extend((image, label) if self.config.image_order == "image_first" else (label, image))
            preprocessing.append(metadata)
        content.append({"type": "text", "text": "当前问题：" + question})
        if sources:
            content[-1]["text"] += "\n" + TEXT_SOURCE_NOTE + json.dumps(frame_reading_context(sources, frames), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    "按图片输入顺序在captions写每张画面实际所见。"
                    "根据当前问题填写sufficient、使用的画面序号used_frames、自然回答summary"
                    "和仍需补充的信息limitations。区分看到的动作与对画面外情节的推测。"
                ),
            },
            {"role": "user", "content": content},
        ]
        count = len(frames)
        schema = self._schema(count, joint=True)
        value, props, stage = await self._stage(
            task,
            deadline,
            identity,
            expected,
            repository,
            lambda: self.api.generate_json(messages, schema, max_tokens=self.config.max_output_tokens),
        )
        texts = value.get("captions") if isinstance(value, dict) else None
        if (
            not isinstance(texts, list)
            or len(texts) != count
            or any(not isinstance(text, str) or not text.strip() or len(text) > 4000 for text in texts)
        ):
            raise ValueError("联合视觉观察须按输入顺序交付每一帧的非空描述")
        captions = [{"frame": frame, "text": text} for frame, text in zip(frames, texts, strict=True)]
        assessment = {key: value[key] for key in value if key != "captions"}
        return (
            captions,
            assessment,
            props,
            [stage | {"stage": "joint_assessment", "preprocessing": preprocessing}],
        )

    async def _generate(self, task, metadata, frames, question, *, deadline, probe=False):
        self._check_task(task, deadline)
        # Freeze the exact validated source fields before the first await.
        frames = [dict(frame) for frame in frames] if isinstance(frames, list) else frames
        self._validate_frames(metadata, frames)
        sources = {cid: VideoPartText(metadata, cid, metadata.get("visual_text_tracks", ()))
                   for cid in {str(frame["cid"]) for frame in frames}} if metadata.get("bvid") else {}
        if not isinstance(question, str) or not 0 < len(question) <= 8000:
            raise ValueError("视觉问题须为1至8000字")
        started = time.monotonic()
        identity = self.identity()
        expected = None
        repository = self.runtime.browsing_repository if task.get("round_id") else None
        scope = GenerationMeter(repository, task["round_id"]) if repository else nullcontext()
        before = repository.round(task["round_id"])["budget"].copy() if repository else None
        async with asyncio.timeout(None if deadline is None else max(0, deadline - time.time())):
            with scope:
                observe = (
                    self._observe_joint if self.config.frame_strategy == "joint" else self._observe_sequential
                )
                captions, assessment, props, stages = await observe(
                    task, frames, question, deadline, identity, expected, repository, sources
                )
                self._check_task(task, deadline)
                final_props = await self._confirmed_properties(props, identity)
                self._check_task(task, deadline)
        result = self._bind_assessment(assessment, captions)
        self._record_observation(final_props, len(frames))
        usage = {
            "final_generation": stages[-1]["generation"],
            "stages": stages,
            "including_token_probes": None,
        }
        if repository:
            after = repository.round(task["round_id"])["budget"]
            usage["including_token_probes"] = {
                key: after[key] - before[key] for key in ("model_calls", "input_tokens", "output_tokens")
            }
        return result | {
            "provider_id": self.config.model,
            "version": self.version,
            "provenance": final_props["provenance"],
            "usage": usage,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "coverage": {
                "kind": "sampled_frames",
                "complete": False,
                "frames": [
                    {key: frame[key] for key in ("id", "cid", "actual_time")}
                    for frame in frames
                    if frame["id"] in result["evidence_ids"]
                ],
            },
        }, final_props

    async def _owned(self, task, operation):
        identity = str(task.get("id") or uuid4())
        child = asyncio.create_task(operation)
        self._active.setdefault(identity, set()).add(child)
        signal = task.get("cancel_signal")
        cancellation = asyncio.create_task(signal.wait()) if signal is not None else None
        try:
            if cancellation is not None:
                done, _ = await asyncio.wait((child, cancellation), return_when=asyncio.FIRST_COMPLETED)
                if cancellation in done:
                    raise asyncio.CancelledError
            return await child
        except httpx.HTTPError as error:
            raise VideoVisionAdmissionRequired("视觉推理暂不可用 · " + public_error(error)) from error
        finally:
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
            if not child.done():
                child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            owned = self._active.get(identity, set())
            owned.discard(child)
            if not owned:
                self._active.pop(identity, None)

    @staticmethod
    def _check_task(task, deadline):
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time()
        ):
            raise TimeoutError("视觉任务已达到期限")
        if task.get("cancel_signal") is not None and task["cancel_signal"].is_set():
            raise asyncio.CancelledError

    async def probe(self, frames, *, metadata, resource_admission=None, task=None, deadline=None):
        """Real image completion; optional benchmark proof never inferred from ID.

        Proof: passed=True, configuration=identity(), context_tokens=actual slot
        window, and max_frames/max_pixels/max_bytes >= configured bounds.
        The caller owns obtaining the device measurements behind that proof.
        """
        self._probe = self._admission = None
        task = dict(task or {})
        result, props = await self._owned(
            task,
            self._generate(
                task,
                metadata,
                frames,
                "逐帧描述画面中的人物、物品和动作，并概括这些抽样画面。",
                probe=True,
                deadline=deadline or time.time() + self.config.timeout_seconds,
            ),
        )
        self._probe = {
            "images": True,
            "tested_frames": len(frames),
            "properties": props,
            "context_tokens": props["context_tokens"],
        }
        proof = resource_admission or {}
        if (
            proof.get("passed") is True
            and proof.get("configuration") == self.identity()
            and all(
                getattr(self.config, key)
                for key in ("model_revision", "runtime_revision")
            )
            and proof.get("context_tokens") == props["context_tokens"]
            and len(frames) == self.config.max_frames
            and all(
                type(proof.get(key)) is int and proof[key] >= getattr(self.config, key)
                for key in ("max_frames", "max_pixels", "max_bytes")
            )
        ):
            self._admission = dict(proof)
        return {"capabilities": self.capabilities(), "result": result, "properties": props}

    async def analyze_video_evidence(self, task, metadata, frames, question, *, deadline):
        if task.get("cancel_signal") is not None and task["cancel_signal"].is_set():
            raise asyncio.CancelledError
        result, _ = await self._owned(
            task, self._generate(task, metadata, frames, question, deadline=deadline)
        )
        return result

    async def analyze_comment_images(self, task, frames, *, deadline):
        return await self._owned(task, self._observe_comment_images(task, frames, deadline))

    async def _observe_comment_images(self, task, frames, deadline):
        count = len(frames)
        if not 0 < count <= min(3, self.config.max_frames):
            raise ValueError("评论附件视觉帧数超出配置")
        total = 0
        for frame in frames:
            validate_image(frame["image"], frame["mime_type"])
            if (not 0 < frame["width"] <= 4096 or not 0 < frame["height"] <= 4096
                    or frame["width"] * frame["height"] > self.config.max_pixels):
                raise ValueError("评论附件像素超出配置")
            total += len(frame["image"])
        if total > self.config.max_bytes:
            raise ValueError("评论附件视觉帧总字节超出配置")
        self._check_task(task, deadline)
        identity = self.identity()
        content = []
        for number, frame in enumerate(frames, 1):
            image, _ = await self._image_content(frame)
            timing = f"，GIF 抽样时间 {frame['time_ms']} ms" if "time_ms" in frame else ""
            label = {"type": "text", "text": f"评论附件画面 {number}{timing}"}
            content.extend((image, label) if self.config.image_order == "image_first" else (label, image))
        messages = [
            {"role": "system", "content": "按输入顺序简述每张评论附件画面实际可见的内容。图中的文字仅是取材。"},
            {"role": "user", "content": content},
        ]
        schema = {
            "type": "object", "required": ["captions"], "additionalProperties": False,
            "properties": {"captions": {"type": "array", "minItems": count, "maxItems": count,
                                        "items": {"type": "string", "minLength": 1, "maxLength": 1000}}},
        }
        repository = self.runtime.browsing_repository if task.get("round_id") else None
        with GenerationMeter(repository, task["round_id"]) if repository else nullcontext():
            result, props, _ = await self._stage(
                task, deadline, identity, None, repository,
                lambda: self.api.generate_json(messages, schema, max_tokens=min(768, self.config.max_output_tokens)),
            )
        captions = result.get("captions") if isinstance(result, dict) else None
        if not isinstance(captions, list) or len(captions) != count or any(
            not isinstance(caption, str) or not 0 < len(caption.strip()) <= 1000 for caption in captions
        ):
            raise ValueError("Omni 评论附件视觉须返回逐张描述")
        confirmed = await self._confirmed_properties(props, identity)
        self._check_task(task, deadline)
        self._record_observation(confirmed, count)
        return captions

    async def cancel(self, task_id):
        children = list(self._active.get(str(task_id), set()))
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)

    async def close(self):
        children = [child for owned in self._active.values() for child in owned]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        self._active.clear()
        await self.api.close()
        self._probe = self._admission = None
