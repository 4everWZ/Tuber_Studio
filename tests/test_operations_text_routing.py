import json

import httpx

from tuber_studio.api_providers import create_operation_llm
from tuber_studio.generation_meter import GenerationMeter


async def test_long_background_text_counts_and_generates_at_the_same_engine(settings, runtime, monkeypatch):
    paths = []

    def engine(request):
        paths.append(request.url.path)
        if request.url.path == "/llm/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llm-default", "max_model_len": 262144}]})
        body = json.loads(request.content)
        assert body["model"] == "llm-default"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        if request.url.path == "/llm/v1/chat/prompt_tokens":
            return httpx.Response(200, json={"count": 12000})
        assert request.url.path == "/llm/v1/chat/completions"
        assert body["max_tokens"] == 128
        assert body["response_format"]["type"] == "json_schema"
        assert "structured_outputs" not in body
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"action":"skip"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12000, "completion_tokens": 10},
        })

    client = create_operation_llm(settings)
    await client.client.aclose()
    client.client = httpx.AsyncClient(base_url="https://test.invalid/llm/v1", transport=httpx.MockTransport(engine))
    reserved, settled = [], []
    repo = runtime.browsing_repository
    monkeypatch.setattr(repo, "reserve_budget", lambda _id, **budget: reserved.append(budget))
    monkeypatch.setattr(repo, "settle_input", lambda _id, before, after: settled.append((before, after)))
    monkeypatch.setattr(repo, "settle_output", lambda _id, before, after: settled.append((before, after)))
    try:
        with GenerationMeter(repo, "recorded-round"):
            result = await client.generate_json(
                [{"role": "user", "content": "请按本轮公开材料判断。"}],
                {"type": "object", "properties": {"action": {"type": "string", "enum": ["skip"]}},
                 "required": ["action"], "additionalProperties": False},
                max_tokens=128,
            )
        assert result == {"action": "skip"}
        assert paths == ["/llm/v1/models", "/llm/v1/chat/prompt_tokens", "/llm/v1/chat/completions"]
        assert reserved == [{"model_calls": 1, "input_tokens": 12000, "output_tokens": 128}]
        assert settled == [(12000, 12000), (128, 10)]
    finally:
        await client.close()
