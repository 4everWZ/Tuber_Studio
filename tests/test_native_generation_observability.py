"""Rejected responses remain rejected and conservatively charged, with fresh diagnostics."""

import json

import httpx
import pytest

from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.browsing_store import BrowsingRepository
from tuber_studio.generation_meter import GenerationMeter
from tuber_studio.providers import LlamaCppProvider

MESSAGES = [{"role": "user", "content": "根据来源回答。"}]
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


@pytest.fixture
async def observed(settings, store):
    provider = LlamaCppProvider(settings.data["llm"] | {"max_tokens": 32, "enable_thinking": True})
    await provider.client.aclose()
    repository = BrowsingRepository(store)
    row, _ = repository.create_round("900", store.character(), BrowsingPolicy().model_dump(), 0, True)
    state = {"mode": "stop", "method": "json", "posts": 0}

    def respond(request):
        if request.url.path == "/props":
            assert provider.last_usage == {} and provider.last_finish_reason is None
            assert provider.last_http_status is None
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "source"})
        if request.url.path == "/tokenize":
            return (
                httpx.Response(503)
                if state["mode"] == "tokenizer_error"
                else httpx.Response(200, json={"tokens": [1, 2, 3, 4]})
            )
        assert request.url.path == "/v1/chat/completions"
        state["posts"] += 1
        assert provider.last_usage == {} and provider.last_finish_reason is None
        assert provider.last_http_status is None
        assert repository.round(row["id"])["budget"]["model_calls"] == state["posts"]
        if state["mode"] == "http_error":
            return httpx.Response(503, json={"error": "fixture unavailable"})
        if state["mode"] == "transport_error":
            raise httpx.ReadError("fixture disconnected", request=request)
        payload = json.loads(request.content)
        assert payload["chat_template_kwargs"]["enable_thinking"] is True
        maximum = payload["max_tokens"]
        if state["method"] == "reply":
            content = json.dumps({"spoken_text": "鱼宝，我们慢慢聊。", "reply_to": ["evt-1"]})
        elif state["method"] == "summary":
            content = "保留原始来源的摘要。"
        else:
            content = '{"ok":true}'
        if state["mode"] == "invalid_json":
            content = "{unfinished"
        data = {
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": min(7, maximum),
                "completion_tokens_details": {"reasoning_tokens": min(5, maximum)},
            },
            "choices": [
                {
                    "finish_reason": state["mode"] if state["mode"] in {"length", "tool_calls"} else "stop",
                    "message": {"content": content},
                }
            ],
        }
        if state["mode"] == "missing_choices":
            data["choices"] = []
        return httpx.Response(200, json=data)

    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    try:
        yield provider, repository, row, state
    finally:
        await provider.close()


async def invoke(provider, method, store):
    if method == "reply":
        return await provider.generate(MESSAGES, store.character(), "evt-1")
    if method == "summary":
        return await provider.summarize(MESSAGES, max_tokens=32)
    if method == "prefill":
        return await provider.prefill(MESSAGES)
    return await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)


@pytest.mark.parametrize("method", ["json", "reply", "summary"])
async def test_length_retains_observed_usage_without_refunding_failed_reservation(observed, store, method):
    provider, repository, row, state = observed
    state.update(mode="length", method=method)
    with GenerationMeter(repository, row["id"]):
        with pytest.raises(ValueError):
            await invoke(provider, method, store)
        assert provider.last_finish_reason == "length" and provider.last_http_status == 200
        assert provider.last_usage["completion_tokens"] == 7
        assert provider.last_usage["completion_tokens_details"]["reasoning_tokens"] == 5
        assert repository.round(row["id"])["budget"]["output_tokens"] == 32
        state["mode"] = "stop"
        await invoke(provider, method, store)
    assert provider.last_finish_reason == "stop" and provider.last_tokens == 4
    assert repository.round(row["id"])["budget"]["output_tokens"] == 39


@pytest.mark.parametrize("mode", ["http_error", "transport_error", "tokenizer_error"])
async def test_error_clears_previous_observation_and_next_request_starts_fresh(observed, store, mode):
    provider, repository, row, state = observed
    with GenerationMeter(repository, row["id"]):
        await invoke(provider, "json", store)
        assert provider.last_finish_reason == "stop" and provider.last_usage
        state["mode"] = mode
        with pytest.raises(httpx.HTTPError):
            await invoke(provider, "json", store)
        assert provider.last_usage == {} and provider.last_finish_reason is None
        assert provider.last_http_status == (503 if mode == "http_error" else None)
        assert provider.last_tokens == (None if mode == "tokenizer_error" else 4)
        expected = 7 + (0 if mode == "tokenizer_error" else 32)
        assert repository.round(row["id"])["budget"]["output_tokens"] == expected
        state["mode"] = "stop"
        await invoke(provider, "json", store)
    assert provider.last_finish_reason == "stop" and provider.last_http_status == 200
    assert repository.round(row["id"])["budget"]["output_tokens"] == expected + 7


@pytest.mark.parametrize("mode", ["invalid_json", "missing_choices"])
async def test_invalid_response_diagnostics_do_not_make_it_adopted(observed, store, mode):
    provider, repository, row, state = observed
    state["mode"] = mode
    with GenerationMeter(repository, row["id"]), pytest.raises(ValueError):
        await invoke(provider, "json", store)
    assert provider.last_usage["completion_tokens"] == 7
    assert provider.last_finish_reason == ("stop" if mode == "invalid_json" else None)
    assert repository.round(row["id"])["budget"]["output_tokens"] == 32


async def test_prefill_length_remains_accepted_but_invalid_finish_keeps_reservation(observed, store):
    provider, repository, row, state = observed
    state.update(method="prefill", mode="length")
    with GenerationMeter(repository, row["id"]):
        assert await invoke(provider, "prefill", store) is True
        assert provider.last_finish_reason == "length" and provider.last_usage["completion_tokens"] == 1
        state["mode"] = "tool_calls"
        with pytest.raises(ValueError):
            await invoke(provider, "prefill", store)
    assert provider.last_finish_reason == "tool_calls"
    assert repository.round(row["id"])["budget"]["output_tokens"] == 2
