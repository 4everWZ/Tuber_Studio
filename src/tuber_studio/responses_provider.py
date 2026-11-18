"""Background language-model turns. The caller owns tools, history and side effects."""

import json
from dataclasses import dataclass

from pydantic import Field, field_validator

from .http_api import api_client, endpoint, response_json
from .models import StrictModel


class ResponsesConfig(StrictModel):
    base_url: str
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=2048, gt=0, strict=True)
    timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value):
        return endpoint(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value):
        if not value.strip():
            raise ValueError("后台模型ID不能为空")
        return value


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ResponseTurn:
    id: str
    calls: tuple[FunctionCall, ...]
    text: str
    raw: dict


def _turn(data, allowed):
    identity, output = data.get("id"), data.get("output")
    if not isinstance(identity, str) or not identity.strip() or not isinstance(output, list):
        raise ValueError("Responses 未返回有效的 ID 和输出列表")
    if data.get("status") != "completed" or data.get("error"):
        raise ValueError("Responses 未完整结束；不得执行工具或交付结果")
    calls, text, seen = [], [], set()
    for item in output:
        if not isinstance(item, dict) or item.get("status") not in (None, "completed"):
            raise ValueError("Responses 输出未完整结束")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") == "message" and isinstance(item.get("content"), list):
            for part in item["content"]:
                if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise ValueError("Responses 未返回可交付的正文")
                text.append(part["text"])
        elif item.get("type") == "function_call":
            call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
            if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
                raise ValueError("Responses 工具调用 ID 缺失或重复")
            if not isinstance(name, str) or name not in allowed or not isinstance(arguments, str):
                raise ValueError("Responses 工具未声明或参数无效")
            parsed = json.loads(arguments)
            if not isinstance(parsed, dict):
                raise ValueError("Responses 工具参数须为 JSON 对象")
            calls.append(FunctionCall(call_id, name, parsed))
            seen.add(call_id)
        else:
            raise ValueError("Responses 返回了不支持的输出类型")
    if not calls and not any(part.strip() for part in text):
        raise ValueError("Responses 没有工具请求或可交付正文")
    return ResponseTurn(identity, tuple(calls), "".join(text), data)


class ResponsesClient:
    def __init__(self, config, api_key=None):
        self.config = ResponsesConfig.model_validate(config)
        self.client = api_client(self.config.model_dump(), api_key)

    @classmethod
    def from_settings(cls, settings):
        if not settings.data.get("responses"):
            raise ValueError("后台 Responses 服务未配置")
        return cls(settings.data["responses"], settings.provider_key("responses"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        await self.client.aclose()

    async def request(self, **options):
        """Return the validated wire response to a caller managing its own history."""
        return (await self.create(**options)).raw

    async def create(self, input, *, tools=(), previous_response_id=None, instructions=None, max_output_tokens=None,
                     tool_choice=None):
        budget = self.config.max_output_tokens if max_output_tokens is None else max_output_tokens
        if type(budget) is not int or budget <= 0:
            raise ValueError("Responses 输出额度须为正整数")
        definitions = list(tools)
        names = set()
        for tool in definitions:
            if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("name"), str):
                raise ValueError("Responses 仅支持具名 function 工具")
            if not tool["name"].strip() or tool["name"] in names:
                raise ValueError("Responses 工具名不能为空或重复")
            names.add(tool["name"])
        payload = {"model": self.config.model, "input": input, "tools": definitions,
                   "max_output_tokens": budget, "store": True}
        if previous_response_id is not None:
            payload["previous_response_id"] = previous_response_id
        if instructions is not None:
            payload["instructions"] = instructions
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        return _turn(await response_json(self.client, "POST", "responses", payload=payload), names)

    async def submit_results(self, turn, outputs, **options):
        if not turn.calls or {call.call_id for call in turn.calls} != outputs.keys():
            raise ValueError("工具结果必须与该轮的全部 call_id 一一对应")
        items = []
        for call in turn.calls:
            result = outputs[call.call_id]
            items.append({"type": "function_call_output", "call_id": call.call_id,
                          "output": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, allow_nan=False)})
        return await self.create(items, previous_response_id=turn.id, **options)
