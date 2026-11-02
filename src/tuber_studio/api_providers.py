"""OpenAI-compatible text and reserved image transports, independent of local workers."""

import asyncio
import base64
import hashlib
import json
from collections import OrderedDict
from typing import Protocol

from .compaction import ContextWindowExceeded, IncompleteCompaction
from .generation_meter import reserve_generation, settle_generation
from .http_api import api_client, models_health, response_json
from .llama_sampling import LlamaCppSampling, llama_cpp_sampling
from .providers import (
    DemoProvider,
    LlamaCppProvider,
    generate_operation_reply,
    json_schema_text,
    parse_json_object,
    public_error,
    single_system_messages,
    validate_reply,
)


def create_llm(settings):
    config = settings.data["llm"]
    if config["kind"] == "demo":
        return DemoProvider()
    if config["kind"] == "openai_compatible":
        return APIProvider(config, settings.provider_key("llm"))
    return LlamaCppProvider(config, settings.broker_key_for(config["base_url"]))


class APIProvider:
    generation_metered = True
    capacity_verified = False
    capacity_source = "operator_declared"
    token_accounting = "api_usage_probe"
    is_sleeping = None

    def __init__(self, config, api_key=None, *, adapter="openai_compatible"):
        if adapter not in {"openai_compatible", "llama_cpp"}:
            raise ValueError("未知API协议适配器")
        self.adapter = adapter
        self.config = config
        if adapter == "llama_cpp":
            LlamaCppSampling.model_validate(llama_cpp_sampling(config))
        self.client = api_client(config, api_key)
        self.last_tokens = self.server_tokens = None
        self.profile_error = None
        self.counts = OrderedDict()
        self.count_lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def ensure_capacity(self):
        window = self.config["context_tokens"]
        if type(window) is not int or not 4096 <= window <= self.config["native_context_tokens"]:
            self.profile_error = "API窗口超出配置的服务上限"
            raise ValueError(self.profile_error)
        self.profile_error = None

    async def health(self):
        try:
            await self.ensure_capacity()
            result = await models_health(self.client, self.config["model"])
            return result | {"capacity_source": self.capacity_source, "server_context_tokens": None}
        except Exception as error:
            self.profile_error = public_error(error)
            raise

    async def _complete(self, messages, *, budget, summary=False, structured=False, probe=False, schema=None):
        payload = {
            "model": self.config["model"],
            "messages": single_system_messages(messages),
            "max_tokens": budget,
            "stream": False,
        }
        thinking = self.config.get("enable_thinking")
        if self.adapter == "llama_cpp" and thinking is not None:
            if type(thinking) is not bool:
                raise ValueError("llama.cpp思考设置须为布尔值或None")
            payload["chat_template_kwargs"] = {"enable_thinking": thinking}
        if not probe:
            payload["temperature"] = self.config.get(
                "summary_temperature" if summary else "temperature", 0.2 if summary else 0.7
            )
        if self.adapter == "llama_cpp":
            sampling = llama_cpp_sampling(self.config, summary=summary)
            if probe:
                sampling.pop("temperature", None)
            payload.update(sampling)
            if payload.get("temperature") is None:
                payload.pop("temperature", None)
        if structured:
            payload["response_format"] = {"type": "json_object"}
            if self.adapter == "llama_cpp" and schema is not None:
                payload["response_format"]["schema"] = schema
        reservation = (
            reserve_generation(
                self.config["context_tokens"] * 95 // 100 - budget, budget, conservative_input=True
            )
            if probe
            else None
        )
        data = await response_json(
            self.client,
            "POST",
            "chat/completions",
            payload=payload,
            timeout=self.config.get(
                "compaction_timeout_seconds" if summary else "prefill_timeout_seconds", 600
            ),
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("API未返回唯一完整回答")
        choice = choices[0]
        if not probe:
            self.last_finish_reason = choice.get("finish_reason")
            self.last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            observed_message = choice.get("message")
            received = observed_message.get("content") if isinstance(observed_message, dict) else None
            self.last_content_characters = len(received) if isinstance(received, str) else None
            self.last_content = received if isinstance(received, str) else None
        if choice.get("finish_reason") not in ({"stop", "length"} if probe else {"stop"}):
            if summary:
                raise IncompleteCompaction("API摘要未完整结束；原上下文保留")
            raise ValueError(f"API回复未完整结束（finish_reason={choice.get('finish_reason')}）；未提交草稿")
        message = choice.get("message", {})
        if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call"):
            raise ValueError("API返回了不允许的工具调用")
        usage = data.get("usage", {})
        if not probe:
            self.last_usage = usage if isinstance(usage, dict) else {}
        count = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if type(count) is not int or count <= 0:
            raise ValueError("API未提供有效prompt_tokens；不能可靠管理上下文")
        self.last_tokens = count
        if not probe and not isinstance(message.get("content"), str):
            raise ValueError("API未返回文本内容")
        if probe:
            settle_generation(reservation, usage)
        return message.get("content"), count

    async def count_tokens(self, messages):
        await self.ensure_capacity()
        normalized = single_system_messages(messages)
        digest = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        async with self.count_lock:
            if digest in self.counts:
                self.counts.move_to_end(digest)
                return self.counts[digest]
            _, count = await self._complete(normalized, budget=1, probe=True)
            self.counts[digest] = count
            if len(self.counts) > 64:
                self.counts.popitem(last=False)
            return count

    async def prefill(self, messages):
        count = await self.count_tokens(messages)
        if count + 1 > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("重建原文超出API窗口预算")
        return True

    async def summarize(self, messages, *, max_tokens=None):
        budget = self.config.get("compaction_max_tokens", 2048) if max_tokens is None else max_tokens
        count = await self.count_tokens(messages)
        if count + budget > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("API压缩输入超窗")
        reservation = reserve_generation(
            self.config["context_tokens"] * 95 // 100 - budget, budget, conservative_input=True
        )
        content, _ = await self._complete(messages, budget=budget, summary=True)
        if not content.strip():
            raise IncompleteCompaction("API摘要为空；原上下文保留")
        settle_generation(reservation, self.last_usage)
        return content

    async def generate(self, messages, character, event_id, source_text=None):
        budget = self.config.get("max_tokens", 1024)
        count = await self.count_tokens(messages)
        if count + budget > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("输入超出API窗口预算；禁止静默丢弃历史")
        reservation = reserve_generation(
            self.config["context_tokens"] * 95 // 100 - budget, budget, conservative_input=True
        )
        content, _ = await self._complete(messages, budget=budget, structured=True)
        reply = validate_reply(content, character, event_id, source_text)
        settle_generation(reservation, self.last_usage)
        return reply

    async def generate_text(self, messages, *, max_tokens):
        """Generate visible natural text with the same real API usage budget."""
        self.last_content = None
        self.last_finish_reason = None
        self.last_usage = {}
        self.last_content_characters = None
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("文本输出预算须为正整数")
        count = await self.count_tokens(messages)
        if count + max_tokens > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("文本输入超出API窗口预算；禁止静默丢弃数据")
        reservation = reserve_generation(
            self.config["context_tokens"] * 95 // 100 - max_tokens,
            max_tokens,
            conservative_input=True,
        )
        content, actual = await self._complete(messages, budget=max_tokens)
        if actual + max_tokens > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("API文本实际计量超窗；未采用结果")
        settle_generation(reservation, self.last_usage)
        if not content.strip():
            raise ValueError("API未返回可见文本")
        return content

    async def generate_operation(self, messages, character, event_id, source_text=None):
        return await generate_operation_reply(self, messages, event_id)

    def prepare_json_messages(self, messages, schema, *, max_tokens):
        schema_text = json_schema_text(schema, max_tokens)
        # JSON-object mode is widely supported; do not assume a compatible API
        # implements json_schema. Domain/source validation stays with the caller.
        messages = [
            *messages,
            {
                "role": "system",
                "content": (
                    "仅返回一个符合以下JSON Schema的JSON对象，不使用代码围栏，不调用工具：" + schema_text
                ),
            },
        ]
        return single_system_messages(messages)

    async def generate_json(self, messages, schema, *, max_tokens):
        self.last_content = None
        self.last_finish_reason = None
        self.last_usage = {}
        self.last_content_characters = None
        messages = self.prepare_json_messages(messages, schema, max_tokens=max_tokens)
        count = await self.count_tokens(messages)
        if count + max_tokens > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("结构化输入超出API窗口预算；禁止静默丢弃数据")
        reservation = reserve_generation(
            self.config["context_tokens"] * 95 // 100 - max_tokens, max_tokens, conservative_input=True
        )
        content, actual = await self._complete(messages, budget=max_tokens, structured=True, schema=schema)
        if actual + max_tokens > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("API结构化实际计量超窗；未采用结果")
        value = parse_json_object(content)
        settle_generation(reservation, self.last_usage)
        return value


class VisionProvider(Protocol):
    async def describe(self, prompt: str, image: bytes, mime_type: str) -> str: ...
    async def close(self) -> None: ...


def validate_image(image, mime_type):
    if not isinstance(image, bytes) or not 0 < len(image) <= 8_000_000:
        raise ValueError("图片须为非空字节，最大8MB")
    valid = (
        mime_type == "image/png"
        and image.startswith(b"\x89PNG\r\n\x1a\n")
        or mime_type == "image/jpeg"
        and image.startswith(b"\xff\xd8\xff")
        or mime_type == "image/webp"
        and image[:4] == b"RIFF"
        and image[8:12] == b"WEBP"
    )
    if not valid:
        raise ValueError("仅接受与MIME匹配的PNG、JPEG或WebP字节；不接受URL或系统文件路径")


class VisionService:
    """Local and remote configured endpoints share a bounded inline-image contract."""

    def __init__(self, config, api_key=None):
        self.config = config
        self.client = api_client(config, api_key) if config.get("kind") == "openai_compatible" else None

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def health(self):
        if not self.client:
            return {"available": False, "detail": "多模态接口已保留；当前文本模型不具备图片理解能力"}
        return await models_health(self.client, self.config["model"])

    async def describe(self, prompt, image, mime_type):
        if not self.client:
            raise ValueError("尚未配置视觉服务；MiniCPM5-2B仅支持文本")
        if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 8000:
            raise ValueError("图片问题须为1到8000字")
        validate_image(image, mime_type)
        data_url = f"data:{mime_type};base64," + base64.b64encode(image).decode("ascii")
        result = await response_json(
            self.client,
            "POST",
            "chat/completions",
            payload={
                "model": self.config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 1024,
                "stream": False,
            },
        )
        choices = result.get("choices", [])
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
            or choices[0].get("finish_reason") != "stop"
        ):
            raise ValueError("视觉API未返回完整回答")
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            raise ValueError("视觉API未返回有效文本")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip() or message.get("tool_calls"):
            raise ValueError("视觉API未返回有效文本")
        return content
