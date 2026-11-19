"""Bounded JSON tasks use the real provider transport, not compaction output."""

import json

import httpx
import pytest
from offline_chat import OfflineChat

from tuber_studio.api_providers import APIProvider
from tuber_studio.compaction import ContextWindowExceeded
from tuber_studio.http_api import InferenceBudgetExceeded

SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
    "required": ["entries"],
    "additionalProperties": False,
}
MESSAGES = [{"role": "system", "content": "只提取来源原文。"}, {"role": "user", "content": "来源甲"}]


@pytest.fixture
async def providers(settings):
    opened = []

    async def create(
        *,
        count=3,
        actual=3,
        finish="stop",
        content='{"entries":["甲"]}',
        message_extra=None,
        thinking=False,
        stream_response=None,
    ):
        config = settings.data["omni"] | {
            "context_source": "declared", "prompt_token_endpoint": False, "structured_output": "json_object",
            "base_url": "https://model.test/v1",
            "model": "actual-model",
            "context_tokens": 4096,
            "native_context_tokens": 131072,
            "enable_thinking": thinking,
            "seed": 42,
            "temperature": 0.8,
            "summary_temperature": 0.2,
            "compaction_max_tokens": 32,
        }
        captured = []

        def respond(request):
            captured.append(request)
            body = json.loads(request.content)
            if stream_response is not None and body.get("stream"):
                return stream_response(body)
            probe = body["max_tokens"] == 1
            message = {"content": None if probe else content, "reasoning_content": "私有推理"}
            if not probe:
                message.update(message_extra or {})
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": count if probe else actual},
                    "choices": [{"finish_reason": "length" if probe else finish, "message": message}],
                },
            )

        provider = APIProvider(config)
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(
            base_url="https://model.test/v1/", transport=httpx.MockTransport(respond)
        )
        opened.append(provider)
        return provider, captured

    yield create
    for provider in opened:
        await provider.close()


@pytest.mark.parametrize("thinking", [True, False])
async def test_structured_request_counts_actual_input_and_uses_supported_json_mode(providers, thinking):
    provider, requests = await providers( thinking=thinking)
    original = json.dumps(MESSAGES, ensure_ascii=False)
    assert await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32) == {"entries": ["甲"]}
    assert json.dumps(MESSAGES, ensure_ascii=False) == original
    payload = json.loads(requests[-1].content)
    assert payload["model"] == "actual-model" and payload["max_tokens"] == 32
    assert payload["stream"] is False and payload["temperature"] == 0.8
    assert "tools" not in payload and "functions" not in payload
    assert payload["response_format"] == {"type": "json_object"}
    assert json.dumps(SCHEMA, ensure_ascii=False) in payload["messages"][0]["content"]
    probe = json.loads(requests[0].content)
    assert probe["max_tokens"] == 1
    assert probe["messages"] == payload["messages"]
    assert "response_format" not in probe
    assert provider.last_tokens == 3




@pytest.mark.parametrize("contract", ["json_schema", "structural_tag"])
async def test_api_schema_assistant_streams_content_and_uses_final_accounting(providers, contract):
    fragments = ['{"entries":[', '"甲"]}']

    def respond(payload):
        assert payload["stream_options"] == {"include_usage": True}
        if contract == "structural_tag":
            assert "response_format" not in payload
            tag = json.loads(payload["structured_outputs"]["structural_tag"])
            assert tag["format"]["json_schema"] == SCHEMA
            assert tag["format"]["max_whitespace_cnt"] == 1
        else:
            assert payload["response_format"]["json_schema"]["schema"] == SCHEMA
        packets = [
            {"choices": [{"delta": {"content": part, "reasoning": "内部内容"}, "finish_reason": None}]}
            for part in fragments
        ]
        packets.extend([
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 9}},
        ])
        stream = "".join("data: " + json.dumps(packet) + "\n\n" for packet in packets) + "data: [DONE]\n\n"
        return httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})

    provider, requests = await providers( stream_response=respond)
    provider.config["structured_output"] = contract
    seen = []
    assert await provider.generate_assistant_json(MESSAGES, SCHEMA, max_tokens=32, on_content=seen.append) == {
        "entries": ["甲"],
    }
    assert seen == fragments
    assert len(requests) == 2 and provider.last_tokens == 11
    assert provider.last_usage["completion_tokens"] == 9




async def test_structured_capacity_is_checked_before_generating(providers):
    provider, requests = await providers( count=3880, actual=3880)
    with pytest.raises(ContextWindowExceeded):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
    assert not any("response_format" in json.loads(r.content) for r in requests if r.content)




@pytest.mark.parametrize(
    ("finish", "content", "extra"),
    [
        ("length", '{"entries":[]}', None),
        ("stop", '```json\n{"entries":[]}\n```', None),
        ("stop", '{"entries":[]} trailing', None),
        ("stop", '["甲"]', None),
        ("stop", '{"value":NaN}', None),
        ("stop", '{"entries":[]}', {"tool_calls": [{"function": {"name": "execute"}}]}),
        ("stop", '{"entries":[]}', {"function_call": {"name": "execute"}}),
    ],
)
async def test_incomplete_or_untrusted_output_never_becomes_a_task_result(
    providers, finish, content, extra
):
    provider, _ = await providers( finish=finish, content=content, message_extra=extra)
    error = InferenceBudgetExceeded if finish == "length" else ValueError
    with pytest.raises(error):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)




async def test_api_actual_usage_cannot_overrun_declared_window_after_small_probe(providers):
    provider, _ = await providers( actual=3880)
    with pytest.raises(ContextWindowExceeded):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)


@pytest.mark.parametrize(
    ("schema", "budget"),
    [({}, 32), ({"type": "array"}, 32), (SCHEMA, True), (SCHEMA, 0), ({"type": "object", "x": object()}, 32)],
)
async def test_invalid_task_contract_is_rejected_before_any_http(providers, schema, budget):
    provider, requests = await providers()
    with pytest.raises(ValueError):
        await provider.generate_json(MESSAGES, schema, max_tokens=budget)
    assert requests == []


async def test_json_generation_does_not_change_existing_summary_wire(providers):
    provider, requests = await providers()
    await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
    assert await provider.summarize(MESSAGES) == '{"entries":["甲"]}'
    summary = json.loads(requests[-1].content)
    assert "response_format" not in summary
    assert summary["messages"] == MESSAGES
    assert summary["temperature"] == 0.2 and summary["max_tokens"] == 32


async def test_demo_does_not_fabricate_structured_model_output():
    with pytest.raises(ValueError, match="没有模型结构化生成能力"):
        await OfflineChat().generate_json(MESSAGES, SCHEMA, max_tokens=32)
