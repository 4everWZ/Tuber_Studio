"""Operations prose stays intact, while live speech and delivery identity stay bounded."""

import copy
import json
from urllib.parse import parse_qs

import httpx
import pytest
from test_bilibili import adapter_factory as adapter_factory
from test_browsing import browsing as browsing
from test_browsing import finished as browsing_finished
from test_comments import comments as comments
from test_comments import draft
from test_direct_messages import finished, incoming
from test_direct_messages import messages as messages
from test_dynamics import public_memory
from test_dynamics import service as service
from test_structured_providers import providers as providers

from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.models import OperationReply, Reply
from tuber_studio.operation_text import (
    MAX_OPERATION_TEXT_BYTES,
    operation_output_diagnostics,
    validate_operation_text,
)
from tuber_studio.provider_settings import proposal
from tuber_studio.providers import build_messages
from tuber_studio.video_vision import VideoVisionConfig

LONG_TEXT = "\n".join(f"第{i}段，鱼鱼顺着这件事聊一聊自己的想法，也听听你接下来怎么说。" for i in range(80))


def test_operations_keep_long_text_but_live_schema_and_style_limits_are_independent(store):
    card = store.character()
    snapshot = copy.deepcopy(card)
    assert OperationReply(spoken_text=LONG_TEXT).spoken_text == LONG_TEXT
    spaced = " \n鱼鱼 ~\n "
    assert OperationReply(spoken_text=spaced).spoken_text == spaced
    assert validate_operation_text(spaced) == spaced
    assert Reply(spoken_text=spaced).spoken_text == spaced.strip()
    with pytest.raises(ValueError):
        Reply(spoken_text=LONG_TEXT)
    for mode in ["comment_draft", "direct_message"]:
        payload = build_messages(card, {"event_id": "source", "text": "聊一聊"}, {}, mode)
        assert "max_chars" not in payload[0]["content"] and "min_chars" not in payload[0]["content"]
        assert "按当前交流自然决定篇幅" in payload[-1]["content"]
    live = build_messages(card, {"event_id": "source", "text": "直播"}, {}, "live")
    assert "max_chars" in live[0]["content"]
    assert card == snapshot
    assert not operation_output_diagnostics("哈哈" * 3000)["needs_review"]
    repeated = (("这是同一整段意外循环的输出。" * 5) + "\n") * 80
    assert operation_output_diagnostics(repeated)["needs_review"]
    assert validate_operation_text(repeated) == repeated  # Review signal, not an OC rejection.
    with pytest.raises(ValueError, match="256KiB"):
        validate_operation_text("鲸" * (MAX_OPERATION_TEXT_BYTES // 3 + 1))


@pytest.mark.parametrize("kind", ["local", "api"])
async def test_native_and_api_operation_generation_use_explicit_budget_and_no_text_maxlength(providers, kind):
    provider, requests = await providers(
        kind, content=json.dumps({"spoken_text": LONG_TEXT, "reply_to": ["source"]}), thinking=True
    )
    provider.config["operations_max_tokens"] = 3000
    live_budget = provider.config["max_tokens"]
    reply = await provider.generate_operation([{"role": "user", "content": "交流"}], {}, "source")
    assert reply.spoken_text == LONG_TEXT
    body = json.loads(requests[-1].content)
    assert body["max_tokens"] == 3000 and provider.config["max_tokens"] == live_budget
    if kind == "local":
        schema = body["response_format"]["schema"]
        assert "maxLength" not in schema["properties"]["spoken_text"]
        assert schema["properties"]["reply_to"]["items"]["enum"] == ["source"]
        assert body["chat_template_kwargs"]["enable_thinking"] is True
    else:
        schema_text = body["messages"][0]["content"]
        assert '"maxLength": 120' not in schema_text
    with pytest.raises(ValueError, match="角色JSON"):
        await provider.generate([{"role": "user", "content": "直播"}], {}, "source")


@pytest.mark.parametrize("kind", ["local", "api"])
async def test_length_finish_preserves_diagnostic_and_never_becomes_an_operation_reply(providers, kind):
    provider, _ = await providers(kind, finish="length", content='{"spoken_text":"未完成')
    provider.config["operations_max_tokens"] = 3000
    with pytest.raises(ValueError, match="finish_reason=length"):
        await provider.generate_operation([{"role": "user", "content": "交流"}], {}, "source")
    assert provider.last_finish_reason == "length" and provider.last_content_characters > 0


async def test_long_browsing_candidate_enters_review_without_rewrite_or_style_cap(browsing):
    original = browsing.runtime.llm.generate_json
    responses = []

    async def generate(messages, schema, *, max_tokens):
        value = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision":
            assert all(
                "maxLength" not in branch["properties"]["text"]
                for branch in schema["oneOf"]
                if branch["properties"]["action"]["enum"] == ["reply_public"]
            )
            assert "max_chars" not in messages[0]["content"]
            value["text"] = LONG_TEXT
            responses.append(value)
        return value

    browsing.runtime.llm.generate_json = generate
    browsing.store.set("browsing_policy", browsing.policy() | {"public_max_chars": 20})
    row = await browsing_finished(browsing, preview=True)
    intent = browsing.intents()[0]
    assert row["status"] == "completed" and intent["text"] == LONG_TEXT
    assert intent["status"] == "pending_review" and len(responses) == 1
    assert browsing.review(intent["id"], "approve")["text"] == LONG_TEXT
    assert "public_max_chars" not in browsing.policy()
    assert not browsing.adapter.sent


async def test_comment_and_private_drafts_keep_long_text_for_review(comments, messages):
    async def generate(*args, **kwargs):
        return OperationReply(spoken_text=LONG_TEXT)

    comments.runtime.llm.generate = generate
    row = await draft(comments, comments.store)
    assert row["status"] == "pending_review" and row["text"] == LONG_TEXT
    assert comments.review(row["id"], "approve")["text"] == LONG_TEXT
    identity = messages.enqueue("message", incoming(messages))
    message = await finished(messages, identity)
    assert message["status"] == "pending_review" and message["text"] == LONG_TEXT
    assert messages.review(identity, "approve")["text"] == LONG_TEXT
    assert not comments.adapter.sent and not messages.adapter.sent


async def test_dynamic_keeps_long_body_and_uses_shared_operations_budget(service):
    public_memory(service)
    previous = service.runtime.llm.generate_json
    budgets = []

    async def generate(messages, schema, *, max_tokens):
        value = await previous(messages, schema, max_tokens=max_tokens)
        if schema.get("properties", {}).get("content"):
            assert "maxLength" not in schema["properties"]["content"]
            value["content"] = LONG_TEXT
            budgets.append(max_tokens)
        return value

    service.runtime.llm.generate_json = generate
    service.settings.data["llm"]["operations_max_tokens"] = 16384
    result = await service.evaluate(manual=True)
    assert result["status"] == "pending_review" and result["content"] == LONG_TEXT
    assert budgets == [16384] and not service.sent


async def test_actual_platform_length_rejection_is_not_a_local_short_limit(adapter_factory):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"aid": 42, "bvid": "BV1d8pU6xEZX"}})
        assert parse_qs(request.content.decode())["message"] == [LONG_TEXT]
        return httpx.Response(200, json={"code": 12025, "message": "评论超过平台长度限制"})

    adapter = await adapter_factory(handle, {"sessdata": "fixture", "csrf": "fixture"})
    with pytest.raises(ValueError, match="长度"):
        await adapter.publish("BV1d8pU6xEZX", "11", LONG_TEXT)
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_high_explicit_budgets_are_visible_without_changing_live_budget(settings):
    live = settings.data["llm"]["max_tokens"]
    data, *_ = proposal(settings, {"llm": {"operations_max_tokens": 32768}})
    assert data["llm"]["operations_max_tokens"] == 32768 and data["llm"]["max_tokens"] == live
    assert settings.data["llm"]["operations_max_tokens"] == 8192
    assert BrowsingPolicy(decision_max_tokens=32768).decision_max_tokens == 32768
    assert VideoVisionConfig(max_output_tokens=32768).max_output_tokens == 32768
    with pytest.raises(ValueError):
        VideoVisionConfig(max_output_tokens=32768, context_tokens=4096)
