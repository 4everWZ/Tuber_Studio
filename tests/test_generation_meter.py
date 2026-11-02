"""Real HTTP request admission and parent-owned private task cancellation."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.api_providers import APIProvider
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.browsing_store import BrowsingRepository
from tuber_studio.comments import Comments
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.generation_meter import GenerationMeter, current_meter
from tuber_studio.models import Reply
from tuber_studio.providers import LlamaCppProvider


def parent(store, *, calls=16):
    repository = BrowsingRepository(store)
    config = BrowsingPolicy(max_model_calls=calls).model_dump()
    row, _ = repository.create_round("900", store.character(), config, 0, True)
    return repository, row


def native_response(request, *, failure=None):
    if request.url.path == "/props":
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
    if request.url.path == "/apply-template":
        return httpx.Response(200, json={"prompt": "rendered"})
    if request.url.path == "/tokenize":
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})
    body = json.loads(request.content)
    if failure == "http":
        return httpx.Response(503, json={"error": "temporarily unavailable"})
    schema = body.get("response_format", {}).get("schema", {})
    if "spoken_text" in schema.get("properties", {}):
        event = schema["properties"]["reply_to"]["items"]["enum"][0]
        content = json.dumps({"spoken_text": "鱼宝，我们慢慢聊。", "reply_to": [event]})
    else:
        content = json.dumps({"ok": True}) if schema else "保留原文与来源的摘要。"
    if failure == "json":
        content = "{partial"
    return httpx.Response(
        200,
        json={
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
            "choices": [
                {
                    "finish_reason": "length" if failure == "length" else "stop",
                    "message": {"content": content},
                }
            ],
        },
    )


async def native(settings, handler):
    config = settings.data["llm"] | {"max_tokens": 64, "enable_thinking": True}
    provider = LlamaCppProvider(config)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(handler))
    return provider


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
MESSAGES = [{"role": "user", "content": "请根据来源回答。"}]


async def test_native_every_actual_generation_uses_real_tokens_and_thinking_completion_usage(settings, store):
    repo, row = parent(store)
    posts = []

    def respond(request):
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            assert payload["chat_template_kwargs"]["enable_thinking"] is True
            posts.append(payload)
            assert repo.round(row["id"])["budget"]["model_calls"] == len(posts)
        return native_response(request)

    provider = await native(settings, respond)
    try:
        with GenerationMeter(repo, row["id"]):
            assert await provider.count_tokens(MESSAGES) == 4
            assert repo.round(row["id"])["budget"]["model_calls"] == 0
            await provider.generate(MESSAGES, store.character(), "evt")
            await provider.summarize(MESSAGES, max_tokens=32)
            await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
        budget = repo.round(row["id"])["budget"]
        assert budget["model_calls"] == 3 and budget["input_tokens"] == 12
        assert budget["output_tokens"] == 21  # includes thinking, not only visible JSON
        assert current_meter.get() is None
    finally:
        await provider.close()


@pytest.mark.parametrize("failure", ["length", "json", "http"])
async def test_failed_generation_keeps_reservation_and_exhausted_parent_blocks_next_http(
    settings, store, failure
):
    repo, row = parent(store, calls=1)
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return native_response(request, failure=failure)

    provider = await native(settings, respond)
    try:
        with GenerationMeter(repo, row["id"]):
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
            with pytest.raises(ValueError, match="预算耗尽"):
                await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
        assert requests.count("/v1/chat/completions") == 1
        assert repo.round(row["id"])["budget"]["output_tokens"] == 32
    finally:
        await provider.close()


async def test_remote_usage_probe_is_bounded_generation_and_actual_input_is_settled(settings, store):
    repo, row = parent(store, calls=2)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert repo.round(row["id"])["budget"]["model_calls"] == len(requests)
        probe = body["max_tokens"] == 1
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 1 if probe else 9},
                "choices": [
                    {
                        "finish_reason": "length" if probe else "stop",
                        "message": {"content": None if probe else '{"ok":true}'},
                    }
                ],
            },
        )

    provider = APIProvider(settings.data["llm"] | {"base_url": "https://api.test/v1", "context_tokens": 8192})
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://api.test/v1/", transport=httpx.MockTransport(respond)
    )
    try:
        with GenerationMeter(repo, row["id"]):
            assert await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32) == {"ok": True}
        budget = repo.round(row["id"])["budget"]
        assert budget["model_calls"] == 2 and budget["input_tokens"] == 20 and budget["output_tokens"] == 10
        assert [request["max_tokens"] for request in requests] == [1, 32]
        with GenerationMeter(repo, row["id"]), pytest.raises(ValueError, match="预算耗尽"):
            await provider.generate_json(MESSAGES, SCHEMA, max_tokens=32)
        assert len(requests) == 2  # cached count is not another probe; generation is denied
    finally:
        await provider.close()


async def test_remote_probe_is_rejected_before_http_when_parent_cannot_reserve_declared_context(
    settings, store
):
    repo, row = parent(store)
    config = row["config"] | {"max_input_tokens": 1024}
    store.execute("UPDATE browsing_rounds SET config=? WHERE id=?", (json.dumps(config), row["id"]))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(500)

    provider = APIProvider(settings.data["llm"] | {"base_url": "https://api.test/v1", "context_tokens": 8192})
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://api.test/v1/", transport=httpx.MockTransport(respond)
    )
    try:
        with GenerationMeter(repo, row["id"]), pytest.raises(ValueError, match="预算耗尽"):
            await provider.count_tokens(MESSAGES)
        assert requests == []
    finally:
        await provider.close()


class Delivery:
    def __init__(self):
        self.sent = []

    async def close(self):
        pass


async def test_private_child_is_owned_and_cancelled_by_parent_without_independent_queue(
    settings, store, runtime, monkeypatch
):
    repo, row = parent(store)
    runtime.browsing_repository = repo
    comments = Comments(settings, store, runtime)
    messages = DirectMessages(settings, store, runtime, comments, Delivery())
    await messages.configure({"enabled": True})
    comments.import_items(
        "BV1d8pU6xEZX", [{"comment_id": "42", "platform_user_id": "123", "content": "私下继续聊"}]
    )
    source = store.one("SELECT id FROM comments")["id"]
    identity = messages.enqueue(
        "comment",
        source,
        account_id="900",
        schedule=False,
        video_evidence={"origin_round_id": row["id"], "material_keys": []},
    )
    entered = asyncio.Event()

    async def generate(*args, **kwargs):
        assert current_meter.get().round_id == row["id"]
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "generate_reply", generate)
    child = asyncio.create_task(messages._generate(identity, False))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert messages.queue.empty()
        repo.finish_round(row["id"], "cancelled")
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        assert store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "cancelled"
        assert not runtime.generation_lock.locked() and not messages.adapter.sent
        assert current_meter.get() is None
        assert not store.all("SELECT * FROM account_writes")
    finally:
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        await messages.close()
        await comments.close()


async def test_linked_private_video_preparation_happens_outside_generation_lock(
    settings, store, runtime, monkeypatch
):
    comments = Comments(settings, store, runtime)
    messages = DirectMessages(settings, store, runtime, comments, Delivery())
    await messages.configure({"enabled": True})
    source, _ = messages.ingest(
        {
            "message_id": "77",
            "account_id": "900",
            "platform_user_id": "123",
            "text": "上次那个BV视频里的实验。",
            "timestamp": 1,
        }
    )
    identity = messages.enqueue("message", source, schedule=False)
    linked = {"account_id": "900", "material_keys": ["verified-link"]}

    async def prepare(draft):
        assert not runtime.generation_lock.locked()
        return linked

    async def generate(scope, character, event, relationship, mode):
        assert event["video_evidence"] == linked
        return Reply(spoken_text="我们按原视频里的内容聊吧。"), {}

    runtime.browsing = SimpleNamespace(linked_message_evidence=prepare)
    monkeypatch.setattr(runtime, "generate_reply", generate)
    try:
        await messages._generate(identity, False)
        assert (
            json.loads(
                store.one("SELECT video_evidence FROM dm_drafts WHERE id=?", (identity,))["video_evidence"]
            )
            == linked
        )
        assert messages.queue.empty()
    finally:
        del runtime.browsing
        await messages.close()
        await comments.close()
