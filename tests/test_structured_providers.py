"""Bounded JSON tasks use the real provider transport, not compaction output."""

import json

import httpx
import pytest

from tuber_studio.api_providers import APIProvider
from tuber_studio.compaction import ContextWindowExceeded
from tuber_studio.providers import DemoProvider, LlamaCppProvider

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
        kind,
        *,
        count=3,
        actual=3,
        finish="stop",
        content='{"entries":["甲"]}',
        message_extra=None,
        thinking=False,
    ):
        config = settings.data["llm"] | {
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
            if request.url.path.endswith("/props"):
                return httpx.Response(
                    200,
                    json={
                        "model_alias": "actual-model",
                        "default_generation_settings": {"n_ctx": 4096},
                    },
                )
            if request.url.path.endswith("/apply-template"):
                return httpx.Response(200, json={"prompt": "actual rendered chat template"})
            if request.url.path.endswith("/tokenize"):
                assert json.loads(request.content) == {
                    "content": "actual rendered chat template",
                    "add_special": True,
                }
                return httpx.Response(200, json={"tokens": list(range(count))})
            body = json.loads(request.content)
            probe = kind == "api" and body["max_tokens"] == 1
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

        provider = APIProvider(config) if kind == "api" else LlamaCppProvider(config)
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(
            base_url="https://model.test/v1/", transport=httpx.MockTransport(respond)
        )
        opened.append(provider)
        return provider, captured

    yield create
    for provider in opened:
        await provider.close()


@pytest.mark.parametrize("kind", ["local", "api"])
@pytest.mark.parametrize("thinking", [True, False])
async def test_structured_request_counts_actual_input_and_uses_supported_json_mode(providers, kind, thinking):
    provider, requests = await providers(kind, thinking=thinking)
    original = json.dumps(MESSAGES, ensure_ascii=False)
    assert await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32) == {"entries": ["甲"]}
    assert json.dumps(MESSAGES, ensure_ascii=False) == original
    payload = json.loads(requests[-1].content)
    assert payload["model"] == "actual-model" and payload["max_tokens"] == 32
    assert payload["stream"] is False and payload["temperature"] == 0.8
    assert "tools" not in payload and "functions" not in payload
    if kind == "local":
        assert payload["response_format"] == {"type": "json_object", "schema": SCHEMA}
        assert payload["chat_template_kwargs"] == {"enable_thinking": thinking}
        assert payload.get("reasoning_format") == ("deepseek" if thinking else None)
        assert payload["seed"] == 42
        rendered = json.loads(requests[1].content)
        assert rendered["messages"] == payload["messages"]
        assert [r.url.path.rsplit("/", 1)[-1] for r in requests[:3]] == [
            "props",
            "apply-template",
            "tokenize",
        ]
    else:
        assert payload["response_format"] == {"type": "json_object"}
        assert json.dumps(SCHEMA, ensure_ascii=False) in payload["messages"][0]["content"]
        probe = json.loads(requests[0].content)
        assert probe["max_tokens"] == 1
        assert probe["messages"] == payload["messages"]
        assert "response_format" not in probe
    assert provider.last_tokens == 3


@pytest.mark.parametrize("kind", ["local", "api"])
async def test_structured_capacity_is_checked_before_generating(providers, kind):
    provider, requests = await providers(kind, count=3880, actual=3880)
    with pytest.raises(ContextWindowExceeded):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
    assert not any("response_format" in json.loads(r.content) for r in requests if r.content)


@pytest.mark.parametrize("kind", ["local", "api"])
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
    providers, kind, finish, content, extra
):
    provider, _ = await providers(kind, finish=finish, content=content, message_extra=extra)
    with pytest.raises(ValueError):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)


@pytest.mark.parametrize("actual", [None, True, 2, 4])
async def test_local_json_requires_prompt_usage_to_confirm_complete_input(providers, actual):
    provider, _ = await providers("local", actual=actual)
    with pytest.raises(ValueError, match="完整计量"):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)


async def test_api_actual_usage_cannot_overrun_declared_window_after_small_probe(providers):
    provider, _ = await providers("api", actual=3880)
    with pytest.raises(ContextWindowExceeded):
        await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)


@pytest.mark.parametrize("kind", ["local", "api"])
@pytest.mark.parametrize(
    ("schema", "budget"),
    [({}, 32), ({"type": "array"}, 32), (SCHEMA, True), (SCHEMA, 0), ({"type": "object", "x": object()}, 32)],
)
async def test_invalid_task_contract_is_rejected_before_any_http(providers, kind, schema, budget):
    provider, requests = await providers(kind)
    with pytest.raises(ValueError):
        await provider.generate_json(MESSAGES, schema, max_tokens=budget)
    assert requests == []


@pytest.mark.parametrize("kind", ["local", "api"])
async def test_json_generation_does_not_change_existing_summary_wire(providers, kind):
    provider, requests = await providers(kind)
    await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
    assert await provider.summarize(MESSAGES) == '{"entries":["甲"]}'
    summary = json.loads(requests[-1].content)
    assert "response_format" not in summary
    assert summary["messages"] == MESSAGES
    assert summary["temperature"] == 0.2 and summary["max_tokens"] == 32


async def test_demo_does_not_fabricate_structured_model_output():
    with pytest.raises(ValueError, match="没有模型结构化生成能力"):
        await DemoProvider().generate_json(MESSAGES, SCHEMA, max_tokens=32)
