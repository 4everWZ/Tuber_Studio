"""Background language-model turns. The caller owns tools, history and side effects."""

import json
from dataclasses import dataclass

from pydantic import Field, field_validator

from .generation_meter import current_meter, reserve_generation, settle_generation
from .http_api import InferenceBudgetExceeded, InferenceQueueBusy, api_client, endpoint, response_json
from .models import StrictModel
from .operation_turns import defer_background_request, mark_model_request, prepare_model_request
from .providers import parse_json_object


class ResponsesConfig(StrictModel):
    base_url: str
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=4096, gt=0, strict=True)
    timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    parallel_reads: int = Field(default=2, ge=1, strict=True)

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


class EmptyResponseTurn(ValueError):
    """A completed generation with no action or visible answer to consume."""

    def __init__(self, response_id):
        super().__init__("Responses 没有工具请求或可交付正文")
        self.response_id = response_id


def _turn(data, allowed, output_budget):
    identity, output = data.get("id"), data.get("output")
    if not isinstance(identity, str) or not identity.strip() or not isinstance(output, list):
        raise ValueError("Responses 未返回有效的 ID 和输出列表")
    incomplete = data.get("incomplete_details")
    if (data.get("status") == "incomplete" and isinstance(incomplete, dict)
            and incomplete.get("reason") == "max_output_tokens"):
        raise InferenceBudgetExceeded(f"后台 Responses 已用完本轮思考和输出的 {output_budget} token 额度。")
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
        raise EmptyResponseTurn(identity)
    return ResponseTurn(identity, tuple(calls), "".join(text), data)


def thinking_budget(max_tokens):
    """Leave most of the output allowance for the tool call or visible text."""
    return min(1024, max_tokens // 4)


async def generate_image_json(client, model, messages, schema, max_tokens, *, thinking_tokens):
    """Read already bounded inline images through the background renderer."""
    items = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            converted = []
            for item in content:
                kind = item["type"]
                if kind == "text":
                    converted.append({"type": "input_text", "text": item["text"]})
                elif kind == "image_url" and item["image_url"]["url"].startswith("data:image/"):
                    converted.append({"type": "input_image", "image_url": item["image_url"]["url"], "detail": "auto"})
                else:
                    raise ValueError("后台视觉只接受内嵌图片和文本")
            content = converted
        items.append({"role": message["role"], "content": content})
    payload = {
        "model": model, "input": items, "store": False, "max_output_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_token_budget": thinking_tokens,
        "text": {"format": {"type": "json_schema", "name": "frame_observation", "schema": schema, "strict": True}},
    }
    await prepare_model_request()
    reservation = None
    if current_meter.get() is not None:
        measured = await response_json(client, "POST", "responses/prompt_tokens", payload=payload)
        count = measured.get("count")
        if type(count) is not int or count < 1:
            raise ValueError("后台 Responses 未返回有效的图文输入计量")
        reservation = reserve_generation(count, max_tokens)
    mark_model_request()
    try:
        data = await response_json(client, "POST", "responses", payload=payload)
    except InferenceQueueBusy as error:
        if not await defer_background_request(error.retry_after):
            raise
        mark_model_request()
        data = await response_json(client, "POST", "responses", payload=payload)
    usage = data.get("usage") or {}
    tokens = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}
    if any(type(count) is not int or count < 0 for count in tokens.values()):
        raise ValueError("后台 Responses 未返回有效图文用量")
    settle_generation(reservation, tokens)
    return parse_json_object(_turn(data, set(), max_tokens).text), tokens


class ResponsesClient:
    def __init__(self, config, api_key=None):
        self.config = ResponsesConfig.model_validate(config)
        self.client = api_client(self.config.model_dump(), api_key)

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.responses_config, settings.provider_key("omni"))

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
                     tool_choice=None, store=True):
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
                   "max_output_tokens": budget, "store": store,
                   "chat_template_kwargs": {"enable_thinking": True}, "thinking_token_budget": thinking_budget(budget)}
        if previous_response_id is not None:
            payload["previous_response_id"] = previous_response_id
        if instructions is not None:
            payload["instructions"] = instructions
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        return _turn(await response_json(self.client, "POST", "responses", payload=payload), names, budget)

    async def submit_results(self, turn, outputs, **options):
        if not turn.calls or {call.call_id for call in turn.calls} != outputs.keys():
            raise ValueError("工具结果必须与该轮的全部 call_id 一一对应")
        items = []
        for call in turn.calls:
            result = outputs[call.call_id]
            items.append({"type": "function_call_output", "call_id": call.call_id,
                          "output": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, allow_nan=False)})
        return await self.create(items, previous_response_id=turn.id, **options)
