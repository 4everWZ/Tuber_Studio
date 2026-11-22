"""Replaceable generation and synthesis; no provider has tool authority."""

import asyncio
import hashlib
import json
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx

from ...core.models import OperationReply, Reply
from ...core.sse import event_data
from . import omni_speech
from .http_api import api_client, check_response_status, models_health


def single_system_messages(messages):
    """Keep all system instructions, in order, ahead of unchanged dialogue."""
    instructions = [item["content"] for item in messages if item["role"] == "system"]
    dialogue = [dict(item) for item in messages if item["role"] != "system"]
    return ([{"role": "system", "content": "\n\n".join(instructions)}] if instructions else []) + dialogue


class LLMProvider(Protocol):
    async def generate(
        self, messages: list[dict], character: dict, event_id: str, source_text: str | None = None
    ) -> Reply: ...
    async def count_tokens(self, messages: list[dict]) -> int: ...
    async def prefill(self, messages: list[dict]) -> bool: ...
    async def summarize(self, messages: list[dict], *, max_tokens: int | None = None) -> str: ...
    async def generate_json(self, messages: list[dict], schema: dict, *, max_tokens: int) -> dict: ...
    async def health(self) -> dict: ...
    async def close(self) -> None: ...


def chat_stream_payload(payload):
    return payload | {"stream": True, "stream_options": {"include_usage": True}}


async def read_chat_stream(response, on_content):
    await check_response_status(response)
    if "text/event-stream" not in response.headers.get("content-type", ""):
        raise ValueError("推理服务没有返回模型流")
    chunks, usage, finish = [], {}, None
    async for fragment in event_data(response.aiter_lines()):
        if fragment == "[DONE]":
            return {"usage": usage, "choices": [{"finish_reason": finish, "message": {"content": "".join(chunks)}}]}
        packet = parse_json_object(fragment)
        if packet.get("error"):
            raise ValueError("模型流返回错误")
        if packet.get("usage"):
            usage = packet["usage"]
        choices = packet.get("choices", [])
        if not choices:
            continue
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("模型流返回多个结果")
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("tool_calls") or delta.get("function_call"):
            raise ValueError("模型流返回未授权的工具调用")
        content = delta.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)
            on_content(content)
        finish = choice.get("finish_reason") or finish
    raise ValueError("模型流意外中断")


def json_schema_text(schema, max_tokens):
    """Validate the task contract; remote API schemas remain instructions."""
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("结构化输出预算须为正整数")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("结构化任务需要object类型JSON Schema")
    try:
        return json.dumps(schema, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("结构化任务Schema无效") from None


def parse_json_object(content):
    """Do not strip fences or salvage a partial/embedded model result."""
    try:
        if not isinstance(content, str):
            raise ValueError
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError
        # Python's decoder accepts NaN/Infinity, which are not JSON values.
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, RecursionError):
        raise ValueError("模型未返回完整JSON对象；未采用结果") from None


def validate_reply(content, character, event_id, source_text=None):
    try:
        reply = Reply.model_validate_json(content)
    except ValueError:
        raise ValueError("模型回复不符合角色JSON结构；未提交草稿") from None
    if any(value != event_id for value in reply.reply_to):
        raise ValueError("模型引用了其他事件")
    return reply


async def generate_operation_reply(provider, messages, event_id, *, web_fetch=None):
    schema = OperationReply.model_json_schema()
    schema["properties"]["reply_to"]["items"] = {"type": "string", "enum": [event_id]}
    schema["properties"]["reply_to"]["maxItems"] = 1
    provider.last_finish_reason = None
    provider.last_content_characters = None
    maximum = provider.config.get("operations_max_tokens", 8192)

    async def invoke(packet, wire_schema):
        return await provider.generate_json(packet, wire_schema, max_tokens=maximum)

    try:
        value = (
            await invoke(messages, schema)
            if web_fetch is None
            else await web_fetch.complete(messages, schema, invoke)
        )
    except ValueError:
        finish = getattr(provider, "last_finish_reason", None)
        if finish and finish != "stop":
            usage = getattr(provider, "last_usage", {})
            raise ValueError(
                f"运营输出未完成：finish_reason={finish}，完成tokens={usage.get('completion_tokens')}，"
                f"content字符数={getattr(provider, 'last_content_characters', None)}；未采用正文"
            ) from None
        raise
    reply = OperationReply.model_validate(value)
    if any(reference != event_id for reference in reply.reply_to):
        raise ValueError("模型引用了其他事件")
    return reply


@dataclass
class Audio:
    path: Path
    duration: float
    text: str
    warning: str | None = None

    @property
    def ref(self):
        return "/media/audio/" + self.path.name


def wav_duration(path: Path, *, require_speech=False) -> float:
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
        if not 0 < duration <= 90:
            raise ValueError("音频时长无效或超过90秒")
        if require_speech:
            pcm = audio.readframes(audio.getnframes())
            if len(pcm) != audio.getnframes() * audio.getsampwidth() * audio.getnchannels():
                raise ValueError("动态TTS返回截断音频")
            silent = all(value == 128 for value in pcm) if audio.getsampwidth() == 1 else not any(pcm)
            if silent:
                raise ValueError("动态TTS返回全静音，未作为合成成功")
        return duration


class VoiceService:
    def __init__(self, settings):
        self.settings = settings
        self.cache = None
        config = settings.data["omni"]
        key = settings.provider_key("omni")
        self.client = api_client(config, key)
        self.model = config["model"]
        self.credential_scope = hashlib.sha256((key or "").encode()).hexdigest()

    async def close(self):
        await self.client.aclose()

    async def health(self):
        return await models_health(self.client, self.model)

    async def prerecorded(self, relative, text):
        source = self.settings.asset_path(relative)
        duration = await asyncio.to_thread(wav_duration, source)
        # Asset bytes define identity for the copied media endpoint.
        data = await asyncio.to_thread(source.read_bytes)
        target = self.settings.audio / (hashlib.sha256(data).hexdigest()[:32] + ".wav")
        if not target.exists():
            await self._store_audio(target, data, require_speech=False)
        if self.cache:
            self.cache.touch(target, seconds=180)
        return Audio(target, duration, text)

    async def preview(self, text, voice):
        if voice["provider"] == "prerecorded":
            return await self.prerecorded(voice["sample_asset"], voice["sample_text"])
        if not isinstance(text, str) or not 1 <= len(text) <= 120:
            raise ValueError("试听台词为1到120字")
        return await self.synthesize(text, voice)

    async def synthesize_live(self, text, voice_id):
        return await self.synthesize(text, self.settings.voice(voice_id))

    def _read_reference(self, voice):
        try:
            path = self.settings.voice_reference_path(voice)
            with path.open("rb") as stream:
                reference = stream.read(8_000_001)
        except OSError:
            raise ValueError("所选音色的参考录音缺失或无法读取") from None
        if not 0 < len(reference) <= 8_000_000:
            raise ValueError("角色参考录音为空或过大")
        return reference

    async def synthesize(self, text, voice):
        if voice["provider"] != "omni_reference":
            raise ValueError("此音色没有用于 Omni 朗读的参考录音，请选择已配置参考录音的音色")
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError("合成文本须为1到4096字")
        reference = await asyncio.to_thread(self._read_reference, voice)
        identity = {
            "provider": "omni_reference",
            "endpoint": str(self.client.base_url),
            "model": self.model,
            "voice_profile": voice,
            "reference_bytes": hashlib.sha256(reference).hexdigest(),
            "reader_version": "audio-speech-readback-v1",
            "text": text,
            "credential_scope": self.credential_scope,
        }
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.settings.audio / (digest + ".wav")
        try:
            duration = await asyncio.to_thread(wav_duration, path, require_speech=True)
        except (FileNotFoundError, ValueError, EOFError, wave.Error):
            try:
                data = await omni_speech.render(self.client, self.model, text, reference)
                duration = await self._store_audio(path, data)
            except (EOFError, wave.Error):
                raise ValueError("Omni 须返回有效 WAV 录音") from None
        if self.cache:
            self.cache.touch(path, seconds=180)
        return Audio(path, duration, text)

    async def _store_audio(self, path, data, *, require_speech=True):
        if self.cache:
            async with self.cache.write("speech", len(data)) as allowed:
                if len(data) > allowed:
                    raise ValueError("语音缓存空间不足")
                return await self._write_audio(path, data, require_speech=require_speech)
        return await self._write_audio(path, data, require_speech=require_speech)

    async def _write_audio(self, path, data, *, require_speech):
        temporary = path.with_suffix(f".{uuid4().hex}.part")
        try:
            await asyncio.to_thread(temporary.write_bytes, data)
            duration = await asyncio.to_thread(wav_duration, temporary, require_speech=require_speech)
            temporary.replace(path)
            if self.cache:
                self.cache.touch(path, seconds=180)
            return duration
        finally:
            temporary.unlink(missing_ok=True)


def public_error(error):
    """Never echo request URLs, headers, tokens or a platform's arbitrary response body."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"上游HTTP {error.response.status_code}；请检查服务、权限或平台限制"
    if isinstance(error, httpx.TimeoutException):
        return "上游服务超时（网络连接或服务响应过慢，可重试）"
    if isinstance(error, httpx.RequestError):
        return "无法连接上游服务"
    if isinstance(error, (ValueError, KeyError)):
        return re.sub(r"https?://\S+", "[URL]", str(error))[:240]
    return f"{type(error).__name__}，详见服务运行环境"
