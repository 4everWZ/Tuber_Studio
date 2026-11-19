"""OpenAI-compatible text and reserved image transports, independent of local workers."""

import asyncio
import base64
import hashlib
import json
import time
from collections import OrderedDict
from typing import Protocol

from .compaction import ContextWindowExceeded, IncompleteCompaction
from .config import uses_broker_llm
from .generation_meter import reserve_generation, settle_generation
from .http_api import InferenceBudgetExceeded, api_client, inference_model_card, models_health, response_json
from .image_preprocessing import prepare_image
from .llama_sampling import LlamaCppSampling, llama_cpp_sampling
from .operation_identity import provider_identity_messages
from .operation_turns import mark_model_request, prepare_model_request
from .providers import (
    DemoProvider,
    LlamaCppProvider,
    chat_stream_payload,
    generate_operation_reply,
    json_schema_text,
    parse_json_object,
    public_error,
    read_chat_stream,
    single_system_messages,
    validate_reply,
)


def create_llm(settings):
    config = settings.data["llm"]
    if config["kind"] == "demo":
        return DemoProvider()
    if config["kind"] == "openai_compatible":
        return APIProvider(config, settings.provider_key("llm"))
    return LlamaCppProvider(
        config, settings.endpoint_key("llm"), broker_model_routing=uses_broker_llm(settings.data)
    )


class APIProvider:
    generation_metered = True
    supports_operation_web_fetch = True
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
        self.uses_model_card = config.get("context_source") == "model_card"
        self.window_observed_at = None
        if self.uses_model_card:
            self.capacity_source = "model_card"
        self.counts = OrderedDict()
        self.count_lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    def adopt_context_window(self, window):
        if not self.uses_model_card or type(window) is not int or not 4096 <= window <= 2_147_483_647:
            raise ValueError("模型卡未实报有效的上下文窗口")
        if window != self.server_tokens:
            self.counts.clear()
        self.server_tokens = window
        self.capacity_verified = True
        self.window_observed_at = time.monotonic()
        self.profile_error = None

    @property
    def capacity_window(self):
        if self.uses_model_card:
            if self.server_tokens is None:
                raise ValueError("推理端的模型窗口尚不可用")
            return self.server_tokens
        return self.config["context_tokens"] * 95 // 100

    async def ensure_capacity(self, *, refresh=False):
        if self.uses_model_card:
            if refresh or self.window_observed_at is None or time.monotonic() - self.window_observed_at >= 30:
                try:
                    card = await inference_model_card(self.client, self.config["model"])
                    self.adopt_context_window(card.get("max_model_len"))
                except Exception as error:
                    self.server_tokens = self.window_observed_at = None
                    self.capacity_verified = False
                    self.profile_error = public_error(error)
                    self.counts.clear()
                    raise
            self.profile_error = None
            return
        window = self.config["context_tokens"]
        if type(window) is not int or not 4096 <= window <= self.config["native_context_tokens"]:
            self.profile_error = "API窗口超出配置的服务上限"
            raise ValueError(self.profile_error)
        self.profile_error = None

    async def health(self):
        try:
            await self.ensure_capacity(refresh=True)
            if self.uses_model_card:
                return {"available": True, "detail": "API模型可达；窗口来自服务实报",
                        "capacity_source": self.capacity_source, "server_context_tokens": self.server_tokens}
            result = await models_health(self.client, self.config["model"])
            return result | {"capacity_source": self.capacity_source, "server_context_tokens": None}
        except Exception as error:
            self.profile_error = public_error(error)
            raise

    def chat_payload(self, messages, *, enable_thinking=None):
        payload = {
            "model": self.config["model"],
            "messages": single_system_messages(provider_identity_messages(self, messages)),
        }
        thinking = self.config.get("enable_thinking") if enable_thinking is None else enable_thinking
        if (self.adapter == "llama_cpp" or self.uses_model_card) and thinking is not None:
            if type(thinking) is not bool:
                raise ValueError("思考设置须为布尔值或None")
            payload["chat_template_kwargs"] = {"enable_thinking": thinking}
        return payload

    async def _complete(self, messages, *, budget, summary=False, structured=False, probe=False, schema=None,
                        enable_thinking=None, thinking_budget=None, on_content=None):
        payload = self.chat_payload(messages, enable_thinking=enable_thinking) | {"max_tokens": budget, "stream": False}
        if self.uses_model_card:
            payload["include_reasoning"] = False
        if not probe:
            payload["temperature"] = self.config.get(
                "summary_temperature" if summary else "temperature", 0.2 if summary else 0.7
            )
            if self.uses_model_card:
                payload.update({key: self.config[key] for key in ("top_k", "top_p") if key in self.config})
        if self.adapter == "llama_cpp":
            sampling = llama_cpp_sampling(self.config, summary=summary)
            if probe:
                sampling.pop("temperature", None)
            payload.update(sampling)
            if payload.get("temperature") is None:
                payload.pop("temperature", None)
        if structured:
            contract = self.config.get("structured_output")
            if self.adapter == "llama_cpp" and schema is not None:
                payload["response_format"] = {"type": "json_object", "schema": schema}
            elif schema is not None and contract == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "structured_reply", "schema": schema, "strict": True},
                }
            elif schema is not None and contract == "structural_tag":
                payload["structured_outputs"] = {"structural_tag": json.dumps({
                    "type": "structural_tag",
                    "format": {"type": "json_schema", "json_schema": schema, "max_whitespace_cnt": 1},
                }, ensure_ascii=False, separators=(",", ":"))}
            else:
                payload["response_format"] = {"type": "json_object"}
        if thinking_budget is not None:
            payload.update(thinking_budget_tokens=thinking_budget, reasoning_format="deepseek")
        await prepare_model_request()
        reservation = (
            reserve_generation(
                self.capacity_window - budget, budget, conservative_input=True
            )
            if probe
            else None
        )
        mark_model_request()
        timeout = self.config.get("compaction_timeout_seconds" if summary else "prefill_timeout_seconds", 600)
        if on_content is None:
            data = await response_json(self.client, "POST", "chat/completions", payload=payload, timeout=timeout)
        else:
            async with self.client.stream("POST", "chat/completions", json=chat_stream_payload(payload),
                                          timeout=timeout) as response:
                data = await read_chat_stream(response, on_content)
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
            if choice.get("finish_reason") == "length":
                raise InferenceBudgetExceeded(f"API回复未完整结束（finish_reason=length），本次输出额度为 {budget} tokens。")
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
        normalized = single_system_messages(provider_identity_messages(self, messages))
        digest = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        async with self.count_lock:
            if digest in self.counts:
                self.counts.move_to_end(digest)
                return self.counts[digest]
            if self.config.get("prompt_token_endpoint") and all(isinstance(message.get("content"), str) for message in normalized):
                result = await response_json(
                    self.client, "POST", "chat/prompt_tokens", payload=self.chat_payload(normalized), timeout=10,
                )
                count = result.get("count")
                if type(count) is not int or count < 1:
                    raise ValueError("推理端未返回有效的文本 token 计数")
            else:
                _, count = await self._complete(normalized, budget=1, probe=True)
            self.counts[digest] = count
            if len(self.counts) > 64:
                self.counts.popitem(last=False)
            return count

    async def prefill(self, messages):
        count = await self.count_tokens(messages)
        if count + 1 > self.capacity_window:
            raise ContextWindowExceeded("重建原文超出API窗口预算")
        return True

    async def summarize(self, messages, *, max_tokens=None):
        budget = self.config.get("compaction_max_tokens", 2048) if max_tokens is None else max_tokens
        count = await self.count_tokens(messages)
        if count + budget > self.capacity_window:
            raise ContextWindowExceeded("API压缩输入超窗")
        reservation = reserve_generation(
            self.capacity_window - budget, budget, conservative_input=True
        )
        content, _ = await self._complete(messages, budget=budget, summary=True)
        if not content.strip():
            raise IncompleteCompaction("API摘要为空；原上下文保留")
        settle_generation(reservation, self.last_usage)
        return content

    async def generate(self, messages, character, event_id, source_text=None):
        budget = self.config.get("max_tokens", 1024)
        count = await self.count_tokens(messages)
        if count + budget > self.capacity_window:
            raise ContextWindowExceeded("输入超出API窗口预算；禁止静默丢弃历史")
        reservation = reserve_generation(
            self.capacity_window - budget, budget, conservative_input=True
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
        if count + max_tokens > self.capacity_window:
            raise ContextWindowExceeded("文本输入超出API窗口预算；禁止静默丢弃数据")
        reservation = reserve_generation(
            self.capacity_window - max_tokens,
            max_tokens,
            conservative_input=True,
        )
        content, actual = await self._complete(messages, budget=max_tokens)
        if actual + max_tokens > self.capacity_window:
            raise ContextWindowExceeded("API文本实际计量超窗；未采用结果")
        settle_generation(reservation, self.last_usage)
        if not content.strip():
            raise ValueError("API未返回可见文本")
        return content

    async def generate_operation(self, messages, character, event_id, source_text=None, *, web_fetch=None):
        return await generate_operation_reply(self, messages, event_id, web_fetch=web_fetch)

    async def complete_admitted_live_image(self, messages, schema, budget, *, enable_thinking=None, thinking_budget=None):
        """Direct call for an already admitted device with context shifting disabled."""
        if self.adapter != "llama_cpp":
            raise ValueError("直播画面需要已准入的 llama.cpp 视觉节点")
        json_schema_text(schema, budget)
        return await self._complete(messages, budget=budget, structured=True, schema=schema,
                                    enable_thinking=enable_thinking, thinking_budget=thinking_budget)

    def prepare_json_messages(self, messages, schema, *, max_tokens):
        schema_text = json_schema_text(schema, max_tokens)
        # The prompt also describes the contract for providers that only support
        # JSON-object mode. Domain and source validation stay with the caller.
        messages = [
            *messages,
            {
                "role": "system",
                "content": "仅返回一个符合以下JSON Schema的JSON对象，省略代码围栏：" + schema_text,
            },
        ]
        return single_system_messages(provider_identity_messages(self, messages))

    async def generate_json(self, messages, schema, *, max_tokens, on_content=None, use_remaining_window=False):
        self.last_content = None
        self.last_finish_reason = None
        self.last_usage = {}
        self.last_content_characters = None
        messages = self.prepare_json_messages(messages, schema, max_tokens=max_tokens)
        count = await self.count_tokens(messages)
        requested_output = max_tokens
        if use_remaining_window:
            max_tokens = min(max_tokens, self.capacity_window - count)
        if max_tokens < 1 or count + max_tokens > self.capacity_window:
            raise ContextWindowExceeded(f"结构化输入 {count} tokens，请求输出 {requested_output} tokens，"
                                        f"有效窗口 {self.capacity_window} tokens。")
        reservation = reserve_generation(
            self.capacity_window - max_tokens, max_tokens, conservative_input=True
        )
        content, actual = await self._complete(messages, budget=max_tokens, structured=True, schema=schema,
                                               on_content=on_content)
        if actual + max_tokens > self.capacity_window:
            raise ContextWindowExceeded(f"结构化实际输入 {actual} tokens，分配输出 {max_tokens} tokens，"
                                        f"有效窗口 {self.capacity_window} tokens。")
        value = parse_json_object(content)
        settle_generation(reservation, self.last_usage)
        return value

    async def generate_assistant_json(self, messages, schema, *, max_tokens, on_content=None, on_stage=None):
        if on_stage:
            on_stage("generating")
        return await self.generate_json(
            messages, schema, max_tokens=max_tokens, use_remaining_window=self.uses_model_card,
            on_content=on_content if self.config.get("structured_output") in {"json_schema", "structural_tag"} else None,
        )

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
            return {"available": False, "detail": "尚未启用视觉 Provider"}
        return await models_health(self.client, self.config["model"])

    async def describe(self, prompt, image, mime_type):
        if not self.client:
            raise ValueError("尚未启用视觉 Provider")
        if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 8000:
            raise ValueError("图片问题须为1到8000字")
        validate_image(image, mime_type)
        prepared = (
            await asyncio.to_thread(
                prepare_image, image, mime_type, self.config.get("image_profile", "server_native")
            )
            if self.config.get("image_profile", "server_native") != "server_native"
            else {"data": image, "mime_type": mime_type}
        )
        image, mime_type = prepared["data"], prepared["mime_type"]
        data_url = f"data:{mime_type};base64," + base64.b64encode(image).decode("ascii")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        if self.config.get("image_order") == "image_first":
            content.reverse()
        result = await response_json(
            self.client,
            "POST",
            "chat/completions",
            payload={
                "model": self.config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
                "max_tokens": self.config.get("max_tokens", 1024),
                "stream": False,
                **(
                    {"chat_template_kwargs": {"enable_thinking": self.config["enable_thinking"]}}
                    if "enable_thinking" in self.config
                    else {}
                ),
                **llama_cpp_sampling(self.config),
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
