import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.agent.chat import OperatorChat
from tuber_studio.core.errors import ContextWindowExceeded, IncompleteCompaction
from tuber_studio.core.meter import GenerationMeter
from tuber_studio.core.turns import operation_ticket
from tuber_studio.infra.inference.api_providers import APIProvider, create_llm
from tuber_studio.operations.kernel.models import BrowsingPolicy
from tuber_studio.operations.kernel.turns import OperationTurns


async def test_workstation_congestion_releases_background_turn_for_chat(settings):
    settings.data["omni"].update(base_url="https://model.test/v1", model="remote-model")
    provider = create_llm(settings)
    refused = asyncio.Event()
    attempts = []
    turns = OperationTurns(asyncio.Lock(), lambda: False)

    def gateway(request):
        attempts.append(request.content)
        if len(attempts) == 1:
            refused.set()
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"code": "gateway_busy"}})
        return httpx.Response(200, json={"usage": {"prompt_tokens": 4}, "choices": [
            {"finish_reason": "stop", "message": {"content": "done"}}
        ]})

    async def watch():
        ticket = await turns.acquire("background")
        scope = operation_ticket.set((turns, ticket))
        try:
            return await provider._complete([{"role": "user", "content": "看这一帧"}], budget=20)
        finally:
            operation_ticket.reset(scope)
            turns.release(ticket)

    await provider.client.aclose()
    async with httpx.AsyncClient(base_url="http://model/v1/", transport=httpx.MockTransport(gateway)) as client:
        provider.client = client
        background = asyncio.create_task(watch())
        await asyncio.wait_for(refused.wait(), 2)
        chat = await asyncio.wait_for(turns.acquire("chat"), 0.5)
        assert len(attempts) == 1
        turns.release(chat)
        assert await asyncio.wait_for(background, 3) == ("done", 4)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert turns.stats["background"]["calls"] == 1


@pytest.fixture
def transport(monkeypatch, settings):
    settings.data["omni"].update(context_source="declared", prompt_token_endpoint=False, structured_output="json_object")
    real = httpx.AsyncClient
    captured = []
    state = {"finish": "stop", "count": 120}

    def respond(request):
        captured.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "remote-model"} | state.get("model_card", {})]})
        if request.url.path.endswith("/prompt_tokens"):
            return httpx.Response(200, json={"count": state["count"]})
        body = json.loads(request.content)
        probe = body["max_tokens"] == 1
        reply = json.dumps(
            {
                "spoken_text": "海底的小灯亮起来啦，先喝口热茶，我们再慢慢聊。",
                "emotion": "cheerful",
                "animation_id": "talk_happy",
                "reply_to": [],
            },
            ensure_ascii=False,
        )
        reply = state.get("text", reply)
        usage = {"prompt_tokens": state["count"] if probe else state.get("actual_count", state["count"])}
        if "completion" in state:
            usage["completion_tokens"] = 1 if probe else state["completion"]
        return httpx.Response(
            200,
            json={
                "usage": usage,
                "choices": [
                    {
                        "finish_reason": "length" if probe else state["finish"],
                        "message": {"content": None if probe else reply}
                        | ({} if probe else state.get("extra", {})),
                    }
                ],
            },
        )

    def client(**options):
        options.setdefault("transport", httpx.MockTransport(respond))
        return real(**options)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return captured, state


@pytest.mark.asyncio
async def test_external_provider_uses_bearer_and_real_usage_without_broker_headers(
    settings, store, transport
):
    captured, _ = transport
    settings.data["omni"].update(
        base_url="https://api.test/v1", model="remote-model", context_tokens=8192
    )
    settings.inference_token = "internal-must-not-leak"
    settings.provider_keys = {"omni": "external-test-key"}
    provider = create_llm(settings)
    messages = [{"role": "system", "content": "只返回JSON"}, {"role": "user", "content": "你好"}]
    try:
        assert (await provider.health())["capacity_source"] == "operator_declared"
        assert await provider.count_tokens(messages) == 120
        assert await provider.count_tokens(messages) == 120
        assert await provider.prefill(messages)
        reply = await provider.generate(messages, store.character(), "current")
        assert reply.emotion == "cheerful"
        posts = [r for r in captured if r.method == "POST"]
        assert len(posts) == 3
        assert json.loads(posts[0].content)["max_tokens"] == 1
        assert json.loads(posts[-1].content)["response_format"] == {"type": "json_object"}
        for request in captured:
            assert request.url.path.startswith("/v1/")
            assert request.headers["authorization"] == "Bearer external-test-key"
            assert "x-tuber-inference-key" not in request.headers
            assert b"internal-must-not-leak" not in request.content
    finally:
        await provider.close()


@pytest.mark.parametrize("thinking", [True, False])
async def test_live_structured_reply_reads_only_visible_content(settings, store, transport, thinking):
    captured, state = transport
    state.update(model_card={"max_model_len": 40960}, extra={"reasoning": "private-reasoning-sentinel"})
    settings.data["omni"].update(
        model="remote-model", context_source="model_card", prompt_token_endpoint=True,
        enable_thinking=thinking, structured_output="structural_tag",
    )
    provider = create_llm(settings)
    messages = [{"role": "user", "content": "鱼鱼，接着聊吧"}]
    try:
        reply = await provider.generate(messages, store.character(), "live-event-42")
        assert "海底的小灯" in reply.spoken_text
        assert "private-reasoning" not in provider.last_content
        request = json.loads(captured[-1].content)
        assert request["model"] == "remote-model"
        assert request["chat_template_kwargs"] == {"enable_thinking": thinking}
        assert request["include_reasoning"] is thinking
        schema = json.loads(request["structured_outputs"]["structural_tag"])["format"]["json_schema"]
        assert schema["properties"]["reply_to"]["items"]["enum"] == ["live-event-42"]

        state["text"] = None
        with pytest.raises(ValueError, match="API未返回文本内容"):
            await provider.generate(messages, store.character(), "live-event-42")
        assert provider.last_content is None
    finally:
        await provider.close()


@pytest.mark.parametrize("structured_output", ["json_object", "json_schema", "structural_tag"])
async def test_structured_schema_uses_the_selected_provider_contract(settings, transport, structured_output):
    captured, _ = transport
    provider = APIProvider(
        settings.data["omni"] | {"base_url": "https://api.test/v1", "model": "remote-model",
                                "structured_output": structured_output},
    )
    schema = {
        "type": "object",
        "properties": {"spoken_text": {"type": "string"}},
        "required": ["spoken_text"],
    }
    try:
        result = await provider.generate_json(
            [{"role": "user", "content": "鱼鱼，接着聊吧"}], schema, max_tokens=512
        )
        assert isinstance(result["spoken_text"], str)
    finally:
        await provider.close()
    posts = [json.loads(request.content) for request in captured if request.method == "POST"]
    assert {"response_format", "structured_outputs"}.isdisjoint(posts[0])
    if structured_output == "structural_tag":
        assert "response_format" not in posts[-1]
        tag = json.loads(posts[-1]["structured_outputs"]["structural_tag"])
        assert tag == {"type": "structural_tag", "format": {
            "type": "json_schema", "json_schema": schema, "max_whitespace_cnt": 1,
        }}
        return
    assert "structured_outputs" not in posts[-1]
    expected = {"type": "json_object"}
    if structured_output == "json_schema":
        expected = {"type": "json_schema",
                    "json_schema": {"name": "structured_reply", "schema": schema, "strict": True}}
    assert posts[-1]["response_format"] == expected








async def test_public_text_preserves_natural_reply_normal_sampling_and_real_usage(
    settings, runtime, transport
):
    captured, state = transport
    text = "哼，红杯还在桌上。\n小女孩在看另一只手操作。"
    state.update(text=text, completion=12, extra={"reasoning_content": "private-reasoning-sentinel"})
    provider = APIProvider(
        settings.data["omni"] | {"enable_thinking": True, "reasoning_budget_tokens": 1024},
    )
    row, _ = runtime.browsing_repository.create_round(
        "text-test", runtime.character, BrowsingPolicy().model_dump(), 0, True
    )
    messages = [{"role": "user", "content": "观察画面"}]
    try:
        with GenerationMeter(runtime.browsing_repository, row["id"]):
            actual = await provider.generate_text(messages, max_tokens=128)
        assert actual == text and "private-reasoning" not in actual
        assert provider.last_usage == {"prompt_tokens": 120, "completion_tokens": 12}
        assert runtime.browsing_repository.round(row["id"])["budget"] == {
            "reads": 0,
            "model_calls": 2,
            "input_tokens": 240,
            "output_tokens": 13,
            "top_levels": 0,
        }
        posts = [json.loads(request.content) for request in captured]
        assert [post["max_tokens"] for post in posts] == [1, 128]
        assert posts[-1]["messages"] == messages
        assert posts[-1]["temperature"] == settings.data["omni"]["temperature"]
        for post in posts:
            assert "response_format" not in post and "tools" not in post
            assert "reasoning_budget_tokens" not in post
            assert "chat_template_kwargs" not in post
    finally:
        await provider.close()


@pytest.mark.parametrize("budget", [True, 0, -1, "128"])
async def test_public_text_invalid_budget_never_reaches_http(settings, transport, budget):
    captured, _ = transport
    provider = APIProvider(settings.data["omni"])
    try:
        with pytest.raises(ValueError):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=budget)
        assert not captured
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"finish": "length"},
        {"text": ""},
        {"text": "  "},
        {"extra": {"tool_calls": [{"id": "external"}]}},
        {"extra": {"function_call": {"name": "external"}}},
    ],
)
async def test_public_text_incomplete_empty_or_tool_output_is_rejected(settings, transport, patch):
    _, state = transport
    state.update(patch)
    provider = APIProvider(settings.data["omni"])
    try:
        with pytest.raises(ValueError):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=128)
    finally:
        await provider.close()


@pytest.mark.parametrize("counts,requests", [({"count": 3800}, 1), ({"actual_count": 3800}, 2)])
async def test_public_text_checks_estimated_and_actual_context(settings, transport, counts, requests):
    captured, state = transport
    state.update(counts)
    provider = APIProvider(settings.data["omni"] | {"context_tokens": 4096})
    try:
        with pytest.raises(ContextWindowExceeded):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=128)
        assert len(captured) == requests
    finally:
        await provider.close()


async def test_omni_chat_uses_the_reported_full_window_and_remaining_output_budget(settings, transport):
    captured, state = transport
    state.update(model_card={"max_model_len": 40960}, count=40500, text='{"reply_markdown":"原文已读"}')
    provider = APIProvider(
        settings.data["omni"] | {"model": "remote-model", "context_source": "model_card",
                                "context_tokens": 12288, "enable_thinking": True, "prompt_token_endpoint": True,
                                "structured_output": "structural_tag"},
        "test-key",
    )
    try:
        assert (await provider.health())["server_context_tokens"] == 40960
        messages = [{"role": "user", "content": "请读完原文并复述结尾"}]
        schema = {"type": "object", "properties": {"reply_markdown": {"type": "string"}},
                  "required": ["reply_markdown"]}
        assert await provider.generate_assistant_json(messages, schema, max_tokens=4096) == {"reply_markdown": "原文已读"}
        assert [request.url.path.rsplit("/", 1)[-1] for request in captured] == ["models", "prompt_tokens", "completions"]
        requests = [json.loads(request.content) for request in captured if request.method == "POST"]
        assert "max_tokens" not in requests[0] and requests[-1]["max_tokens"] == 460
        assert {"response_format", "structured_outputs"}.isdisjoint(requests[0])
        assert "response_format" not in requests[-1]
        assert json.loads(requests[-1]["structured_outputs"]["structural_tag"])["format"]["json_schema"] == schema
        assert requests[-1]["chat_template_kwargs"] == {"enable_thinking": True}
        assert requests[-1]["messages"] == requests[0]["messages"] and requests[-1]["messages"][-1] == messages[-1]
        assert all(request.headers["authorization"] == "Bearer test-key" for request in captured)
        state["model_card"] = {"max_model_len": 32768}
        await provider.ensure_capacity(refresh=True)
        assert provider.capacity_window == 32768
        state["model_card"] = {"max_model_len": True}
        with pytest.raises(ValueError, match="实报"):
            await provider.ensure_capacity(refresh=True)
        assert provider.server_tokens is None and not provider.capacity_verified
    finally:
        await provider.close()


@pytest.mark.parametrize(
    ("older_size", "latest_size", "expected_history"),
    [(39916, 10, 4), (39917, 10, 2), (39000, 40950, 0)],
    ids=["history-fits-with-1024-output", "history-exceeds-reserve", "current-input-near-window-limit"],
)
async def test_omni_chat_reserves_output_without_shortening_current_input(older_size, latest_size, expected_history):
    async def count(messages):
        return sum(len(message["content"]) for message in messages)

    provider = SimpleNamespace(capacity_window=40960, count_tokens=count)
    prefix = [{"role": "system", "content": "设定"}]
    history = [
        {"role": "user", "content": "甲" * older_size},
        {"role": "assistant", "content": "收到"},
        {"role": "user", "content": "还是这里"},
        {"role": "assistant", "content": "明白"},
    ]
    current = {"role": "user", "content": "乙" * latest_size}
    fitted = await OperatorChat._fit_assistant_history(provider, prefix, history, current, None, 4096)
    assert fitted == prefix + history[len(history) - expected_history:] + [current]


async def test_public_text_parent_budget_denies_completion_before_request(settings, runtime, transport):
    captured, state = transport
    state["completion"] = 12
    row, _ = runtime.browsing_repository.create_round(
        "text-test", runtime.character, BrowsingPolicy(max_model_calls=1).model_dump(), 0, True
    )
    provider = APIProvider(settings.data["omni"])
    try:
        with GenerationMeter(runtime.browsing_repository, row["id"]), pytest.raises(ValueError, match="预算"):
            await provider.generate_text([{"role": "user", "content": "观察"}], max_tokens=128)
        assert len(captured) == 1 and json.loads(captured[0].content)["max_tokens"] == 1
        assert runtime.browsing_repository.round(row["id"])["budget"]["model_calls"] == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_incomplete_external_summary_and_missing_usage_are_rejected(settings, transport):
    _, state = transport
    config = settings.data["omni"] | {"base_url": "https://api.test/v1", "model": "remote-model"}
    provider = APIProvider(config)
    try:
        state["finish"] = "length"
        with pytest.raises(IncompleteCompaction):
            await provider.summarize([{"role": "user", "content": "请总结"}])
        state["count"] = None
        with pytest.raises(ValueError, match="prompt_tokens"):
            await provider.count_tokens([{"role": "user", "content": "新输入"}])
    finally:
        await provider.close()
