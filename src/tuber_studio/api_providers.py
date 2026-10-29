"""OpenAI-compatible text and reserved image transports, independent of local workers."""

import asyncio
import base64
import hashlib
import json
from collections import OrderedDict
from typing import Protocol

from .compaction import ContextWindowExceeded, IncompleteCompaction
from .http_api import api_client, models_health, response_json
from .providers import DemoProvider, LlamaCppProvider, public_error, single_system_messages, validate_reply


def create_llm(settings):
    config = settings.data["llm"]
    if config["kind"] == "demo":
        return DemoProvider()
    if config["kind"] == "openai_compatible":
        return APIProvider(config, settings.provider_key("llm"))
    return LlamaCppProvider(config, settings.broker_key_for(config["base_url"]))


class APIProvider:
    capacity_verified = False
    capacity_source = "operator_declared"
    token_accounting = "api_usage_probe"
    is_sleeping = None

    def __init__(self, config, api_key=None):
        self.config = config
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

    async def _complete(self, messages, *, budget, summary=False, structured=False, probe=False):
        payload = {
            "model": self.config["model"],
            "messages": single_system_messages(messages),
            "max_tokens": budget,
            "stream": False,
        }
        if not probe:
            payload["temperature"] = self.config.get(
                "summary_temperature" if summary else "temperature", 0.2 if summary else 0.7
            )
        if structured:
            payload["response_format"] = {"type": "json_object"}
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
        if choice.get("finish_reason") not in ({"stop", "length"} if probe else {"stop"}):
            if summary:
                raise IncompleteCompaction("API摘要未完整结束；原上下文保留")
            raise ValueError("API回复未完整结束；未提交草稿")
        message = choice.get("message", {})
        if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call"):
            raise ValueError("API返回了不允许的工具调用")
        usage = data.get("usage", {})
        count = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if type(count) is not int or count <= 0:
            raise ValueError("API未提供有效prompt_tokens；不能可靠管理上下文")
        self.last_tokens = count
        if not probe and not isinstance(message.get("content"), str):
            raise ValueError("API未返回文本内容")
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
        content, _ = await self._complete(messages, budget=budget, summary=True)
        if not content.strip():
            raise IncompleteCompaction("API摘要为空；原上下文保留")
        return content

    async def generate(self, messages, character, event_id, source_text=None):
        budget = self.config.get("max_tokens", 1024)
        count = await self.count_tokens(messages)
        if count + budget > self.config["context_tokens"] * 95 // 100:
            raise ContextWindowExceeded("输入超出API窗口预算；禁止静默丢弃历史")
        content, _ = await self._complete(messages, budget=budget, structured=True)
        return validate_reply(content, character, event_id, source_text)


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
