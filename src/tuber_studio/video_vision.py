"""Explicitly probed, resource-admitted video-frame JSON evidence provider.

This module owns an API client, never a model process or a production setting.
It is disabled until configured and cannot route platform writes or call tools.
"""

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from contextlib import nullcontext
from uuid import uuid4

from pydantic import Field, model_validator

from .api_providers import APIProvider, validate_image
from .generation_meter import GenerationMeter
from .http_api import response_json
from .live_media import StrictModel
from .llama_sampling import LlamaCppSampling

VIDEO_EVIDENCE_PROTOCOL = "sequential_captions_indexed_assessment_v2"


class VideoVisionConfig(StrictModel):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = ""
    model_revision: str = ""
    projector_revision: str = ""
    runtime_revision: str = ""
    runtime_profile: str = ""
    context_tokens: int = Field(default=131072, ge=4096, le=1048576, strict=True)
    native_context_tokens: int = Field(default=131072, ge=4096, le=1048576, strict=True)
    max_frames: int = Field(default=3, ge=1, le=6, strict=True)
    max_pixels: int = Field(default=1024 * 1024, ge=1, le=4096 * 4096, strict=True)
    max_bytes: int = Field(default=1024 * 1024, ge=1, le=2 * 1024 * 1024, strict=True)
    max_output_tokens: int = Field(default=2048, ge=256, le=65536, strict=True)
    enable_thinking: bool | None = Field(default=None, strict=True)
    sampling: LlamaCppSampling = Field(default_factory=LlamaCppSampling)
    timeout_seconds: float = Field(default=60, gt=0, le=600)

    @model_validator(mode="after")
    def valid(self):
        if self.context_tokens > self.native_context_tokens:
            raise ValueError("视觉窗口超过已声明的原生上限；禁止静默缩窗")
        if self.max_output_tokens >= self.context_tokens * 95 // 100:
            raise ValueError("视觉输出预算没有留下输入空间")
        if self.enabled and not self.model.strip():
            raise ValueError("启用视觉须指定真实模型ID")
        if any(
            len(value) > 2048
            for value in (
                self.model,
                self.model_revision,
                self.projector_revision,
                self.runtime_revision,
                self.runtime_profile,
            )
        ):
            raise ValueError("视觉版本身份过长")
        return self


class ApiVideoEvidenceProvider:
    def __init__(self, runtime, config=None, *, api_key=None):
        self.runtime = runtime
        self._api_key = api_key
        self.config = VideoVisionConfig.model_validate(config or {})
        self.api = self._create_api()
        self._probe = None
        self._admission = None
        self._active = {}

    def _create_api(self):
        if not self.config.enabled:
            return None
        config = self.config.model_dump()
        config.update(
            {
                "kind": "openai_compatible",
                "max_tokens": config["max_output_tokens"],
                "prefill_timeout_seconds": config["timeout_seconds"],
                **config.pop("sampling"),
            }
        )
        return APIProvider(config, self._api_key, adapter="llama_cpp")

    def identity(self):
        """Public configuration bound to an external device benchmark record."""
        return self.config.model_dump(exclude={"enabled", "timeout_seconds"}) | {
            "protocol": VIDEO_EVIDENCE_PROTOCOL
        }

    @property
    def version(self):
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True).encode()).hexdigest()

    async def configure(self, config, *, api_key=None):
        parsed = VideoVisionConfig.model_validate(config)
        await self.close()
        self.config, self._api_key = parsed, api_key
        self.api = self._create_api()
        self._probe = self._admission = None

    def capabilities(self):
        probe = self._probe or {}
        available = bool(self.api and probe.get("images"))
        admitted = bool(available and self._admission)
        return {
            "available": available,
            "images": available,
            "admitted": admitted,
            "multiple_images": available and probe.get("tested_frames", 0) >= self.config.max_frames,
            "provider_id": self.config.model or "disabled",
            "version": self.version,
            "max_frames": self.config.max_frames,
            "max_pixels": self.config.max_pixels,
            "max_bytes": self.config.max_bytes,
            "context_tokens": self.config.context_tokens,
            "timeout_seconds": self.config.timeout_seconds,
            "runtime_context_tokens": probe.get("context_tokens"),
            "reason": ""
            if admitted
            else "视觉接口默认关闭"
            if not self.api
            else "未完成真实图像接口探测"
            if not available
            else "设备资源实测尚未准入",
        }

    async def _runtime_properties(self):
        if not self.api:
            raise ValueError("视觉接口未启用")
        await self.api.health()
        props = await response_json(
            self.api.client,
            "GET",
            str(self.api.client.base_url.copy_with(path="/props")),
            timeout=min(5, self.config.timeout_seconds),
        )
        # The configured llama.cpp endpoint must prove its actual window and
        # loaded vision projector; /models or a model name alone is insufficient.
        window = props.get("default_generation_settings", {}).get("n_ctx")
        if type(window) is not int or window < self.config.context_tokens:
            raise ValueError("视觉服务实际窗口小于要求或未提供可核验窗口")
        if not isinstance(props.get("modalities"), dict) or props["modalities"].get("vision") is not True:
            raise ValueError("视觉服务未加载图像投影器")
        aliases = props.get("model_alias")
        aliases = [aliases] if isinstance(aliases, str) else aliases
        if not isinstance(aliases, list) or self.config.model not in aliases:
            raise ValueError("视觉服务实际模型别名与配置不一致")
        return {
            "endpoint": str(self.api.client.base_url),
            "context_tokens": window,
            "model_alias": aliases,
            "model_path": props.get("model_path"),
            "build_info": props.get("build_info"),
            "modalities": props["modalities"],
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
    def _schema(frame_count):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["sufficient", "used_frames", "summary", "limitations"],
            "properties": {
                "sufficient": {"type": "boolean"},
                "used_frames": {
                    "type": "array",
                    "uniqueItems": True,
                    "maxItems": frame_count,
                    "items": {"type": "integer", "minimum": 1, "maximum": frame_count},
                },
                "summary": {"type": "string", "maxLength": 12000},
                "limitations": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "string", "maxLength": 1000},
                },
            },
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
            raise ValueError("视觉服务实际模型／窗口或配置已变化，须重新探测和准入")

    async def _stage(self, task, deadline, identity, expected, repository, operation):
        started = time.monotonic()
        async with self.runtime.offline_generation():
            self._check_task(task, deadline)
            try:
                props = await self._runtime_properties()
            except Exception:
                self._probe = self._admission = None
                raise
            self._check_properties(props, expected, identity)
            self._check_task(task, deadline)
            before = repository.round(task["round_id"])["budget"].copy() if repository else None
            value = await operation()
            self._check_task(task, deadline)
            usage = {"generation": dict(self.api.last_usage), "including_token_probes": None}
            if repository:
                after = repository.round(task["round_id"])["budget"]
                usage["including_token_probes"] = {
                    key: after[key] - before[key] for key in ("model_calls", "input_tokens", "output_tokens")
                }
        return value, props, usage | {"elapsed_ms": round((time.monotonic() - started) * 1000, 3)}

    async def _generate(self, task, metadata, frames, question, *, deadline, probe=False):
        self._check_task(task, deadline)
        # Freeze the exact validated source fields before the first await.
        frames = [dict(frame) for frame in frames] if isinstance(frames, list) else frames
        self._validate_frames(metadata, frames)
        if not isinstance(question, str) or not 0 < len(question) <= 8000:
            raise ValueError("视觉问题须为1至8000字")
        started = time.monotonic()
        identity = self.identity()
        expected = None if probe else self._probe["properties"]
        repository = self.runtime.browsing_repository if task.get("round_id") else None
        scope = GenerationMeter(repository, task["round_id"]) if repository else nullcontext()
        before = repository.round(task["round_id"])["budget"].copy() if repository else None
        captions, stages = [], []
        async with asyncio.timeout(min(self.config.timeout_seconds, max(0, deadline - time.time()))):
            with scope:
                for number, frame in enumerate(frames, 1):
                    messages = [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"这张画面来自分P {frame['cid']}，实际采样时间为"
                                    f" {frame['actual_time']} 秒。描述这张画面中的人物、物品和动作，"
                                    "结合当前问题给出相关所见。当前问题：" + question,
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:"
                                        + frame.get("mime_type", "image/jpeg")
                                        + ";base64,"
                                        + base64.b64encode(frame["image"]).decode("ascii")
                                    },
                                },
                            ],
                        }
                    ]
                    text, props, stage = await self._stage(
                        task,
                        deadline,
                        identity,
                        expected,
                        repository,
                        lambda: self.api.generate_text(messages, max_tokens=self.config.max_output_tokens),
                    )
                    expected = props
                    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                        raise ValueError("视觉逐帧观察须为非空且有界的自然文本")
                    captions.append({"frame": frame, "text": text})
                    stages.append(stage | {"stage": "caption", "number": number, "frame_id": frame["id"]})
                messages = [
                    {
                        "role": "system",
                        "content": "围绕当前问题，根据下列逐帧观察作答。"
                        "sufficient是这些观察是否足以回答问题的判断；"
                        "used_frames填写所用观察的整数编号，summary写自然回答，"
                        "limitations写还需补充的相关信息。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": question,
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
                        messages, self._schema(len(captions)), max_tokens=self.config.max_output_tokens
                    ),
                )
                stages.append(stage | {"stage": "assessment"})
                self._check_task(task, deadline)
                try:
                    final_props = await self._runtime_properties()
                except Exception:
                    self._probe = self._admission = None
                    raise
                self._check_properties(final_props, props, identity)
                self._check_task(task, deadline)
        result = self._bind_assessment(assessment, captions)
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
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
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
        if not self.api:
            raise ValueError("视觉接口未启用")
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
                for key in ("model_revision", "projector_revision", "runtime_revision")
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
        if not self.capabilities()["admitted"]:
            raise ValueError("视觉接口未完成真实探测与设备资源准入")
        if task.get("cancel_signal") is not None and task["cancel_signal"].is_set():
            raise asyncio.CancelledError
        result, _ = await self._owned(
            task, self._generate(task, metadata, frames, question, deadline=deadline)
        )
        return result

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
        if self.api:
            await self.api.close()
        self.api = None
        self._probe = self._admission = None
