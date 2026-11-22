import json
from unittest.mock import AsyncMock

import httpx

from evaluations.memory.owner_memory_editor_trace import editor_requests
from tuber_studio.infra.inference.api_providers import APIProvider


async def test_private_replay_observes_the_final_title_request_without_changing_what_is_sent(monkeypatch):
    provider = APIProvider({"base_url": "http://localhost/v1", "context_tokens": 8192, "model": "fixture"})
    await provider.close()
    payloads = []

    def service(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 25, "completion_tokens": 1}})

    monkeypatch.setattr(provider, "count_tokens", AsyncMock(return_value=25))
    provider.identity_resolver = lambda: {"identity_version": "isolated", "character_name": "测试角色"}
    receipts = []
    goal = "为这一帧写名词标题"
    messages = [{"role": "user", "content": [{"type": "text", "text": goal},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}}]}]
    async with httpx.AsyncClient(base_url="http://localhost/v1/", transport=httpx.MockTransport(service)) as client:
        provider.client = client
        schema = {"title": "ClipTitle", "type": "object", "properties": {"title": {"type": "string"}}}
        await provider.generate_json(messages, schema, max_tokens=64)
        with editor_requests(provider, goal, "测试角色", receipts.append):
            await provider.generate_json(messages, schema, max_tokens=64)
            field_schema = {"type": "object", "properties": {"items.0.title": {"type": "string"}}}
            await provider.generate_json(messages, field_schema, max_tokens=64)
            repair = {"draft": {"items": [{"title": goal}]}, "issues": {"items.0.title": "缺少依据"}}
            await provider.generate_json([{"role": "user", "content": [{"type": "text", "text": json.dumps(repair, ensure_ascii=False)},
                                                                         messages[0]["content"][1]]}],
                                         field_schema, max_tokens=64)
            await provider.generate_json(messages, schema | {"title": "PrivateDelivery"}, max_tokens=64)
            provider.identity_resolver = None
            await provider.generate_json(messages, schema, max_tokens=64)
            provider.identity_resolver = lambda: {"identity_version": "isolated", "character_name": "测试角色"}
        await provider.generate_json(messages, schema, max_tokens=64)
    assert payloads[0] == payloads[1] == payloads[-1]
    assert [r["schema"] for r in receipts] == ["ClipTitle", "field_review", "field_repair", "PrivateDelivery", "ClipTitle"]
    assert all(r["max_tokens"] == 64 and r["structured_output"] for r in receipts)
    assert all(r["account_identity_block"] and r["character_name_in_system"] and r["task_goal_in_user_text"]
               for r in receipts[:-1])
    assert not receipts[-1]["account_identity_block"] and not receipts[-1]["character_name_in_system"]
    assert all(r["image_count"] == 1 for r in receipts) and "base64" not in str(receipts)
    assert goal not in str(receipts) and "测试角色" not in str(receipts)
