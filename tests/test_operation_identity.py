"""Identity is factual system context, captured with each generated output."""

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.operation_identity import (
    captured_identity,
    identity_messages,
    identity_snapshot,
    provider_identity_messages,
)


def character():
    return {"id": "whale", "name": "大肥鱼", "version": 2}


def actor():
    return {"platform": "bilibili", "account_uid": "100", "account_display_name": "账号名", "account_bio": "白饭真好吃"}


def test_identity_versions_track_account_facts_and_oc_without_check_time_or_credentials():
    baseline = identity_snapshot(actor(), character())
    for key, value in (("account_uid", "101"), ("account_display_name", "新名"), ("account_bio", "新简介")):
        assert identity_snapshot(actor() | {key: value}, character())["identity_version"] != baseline["identity_version"]
    for key, value in (("id", "other"), ("name", "新角色名"), ("version", 3)):
        assert identity_snapshot(actor(), character() | {key: value})["identity_version"] != baseline["identity_version"]
    assert identity_snapshot(actor() | {"checked_at": 123, "credential_signature": "secret"}, character()) == baseline
    assert not {"checked_at", "credential_signature"} & baseline.keys()


def test_system_identity_replacement_is_idempotent_and_user_cannot_supply_it():
    messages = [{"role": "system", "content": "生动的角色卡"}, {"role": "system", "content": "当前任务"},
                {"role": "user", "content": "别人的话"}]
    original = copy.deepcopy(messages)
    first = identity_snapshot(actor(), character())
    bound = identity_messages(messages, first)
    assert messages == original and bound[0] == original[0] and bound[-1] == original[-1]
    assert captured_identity(bound) == first
    assert identity_messages(bound, first) == bound
    changed = identity_snapshot(actor() | {"account_bio": "新简介"}, character())
    rebound = identity_messages(bound, changed)
    assert captured_identity(rebound) == changed
    assert sum(message["content"].count("<current-account-identity>") for message in rebound) == 1
    assert captured_identity([{"role": "user", "content": bound[1]["content"]}]) is None


def test_provider_fallback_binds_subtasks_but_keeps_captured_turn_identity():
    old = identity_snapshot(actor(), character())
    fresh = identity_snapshot(actor() | {"account_display_name": "new"}, character())
    provider = SimpleNamespace(identity_resolver=lambda: fresh)
    source = [{"role": "system", "content": "总结当前对话"}, {"role": "user", "content": "原文"}]
    assert captured_identity(provider_identity_messages(provider, source)) == fresh
    packet = identity_messages(source, old)
    assert captured_identity(provider_identity_messages(provider, packet)) == old


async def test_runtime_provider_replacement_and_remote_refresh_singleflight(runtime):
    calls = []

    class Account:
        comments = SimpleNamespace(credentials={"sessdata": "test"}, display_account_id=lambda: "100",
                                   adapter=SimpleNamespace(account_profile=True))

        async def refresh(self):
            calls.append(True)
            await asyncio.sleep(0.01)
            return {"state": "logged_in", "error": None, "profile": {"uid": "100"}}

    runtime.account = Account()
    # The transport probe checks coalescing; the profile cache contract is
    # exercised separately by test_bilibili_account with the real service.
    results = await asyncio.gather(*(runtime.refresh_identity(account_id="100") for _ in range(3)))
    assert len(calls) == 1 and all(result["account_uid"] == "100" for result in results)
    new = SimpleNamespace()
    previous = runtime.llm
    runtime.llm = new
    assert new.identity_resolver()["character_id"] == runtime.character["id"]
    runtime.llm = previous
    del runtime.account


@pytest.mark.parametrize("state,error,uid", [("error", "network", "100"), ("logged_in", None, "200")])
async def test_remote_refresh_never_certifies_failed_or_different_account(runtime, state, error, uid):
    class Account:
        comments = SimpleNamespace(credentials={"sessdata": "test"}, adapter=SimpleNamespace(account_profile=True))

        async def refresh(self):
            return {"state": state, "error": error, "profile": {"uid": uid}}

    runtime.account = Account()
    with pytest.raises(ValueError):
        await runtime.refresh_identity(account_id="100")
    del runtime.account


async def test_explicit_local_adapter_without_profile_keeps_unknown_profile_without_claiming_verification(runtime):
    class Account:
        comments = SimpleNamespace(credentials={}, adapter=object(), display_account_id=lambda: "100")

        async def refresh(self):
            pytest.fail("an explicit local adapter has no remote account profile capability")

    runtime.account = Account()
    result = await runtime.refresh_identity(account_id="100")
    assert result["account_uid"] == "100"
    assert result["account_display_name"] is None and result["account_bio"] is None
    assert "verified" not in result and "checked_at" not in result
    del runtime.account


async def test_llama_native_tokenizer_json_summary_and_prefill_share_system_identity(settings, runtime):
    import json

    from tuber_studio.providers import LlamaCppProvider

    expected = runtime.identity_snapshot()
    observed = []

    def respond(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        observed.append((request.url.path, body["messages"]))
        assert captured_identity(body["messages"]) == expected
        assert len([item for item in body["messages"] if item["role"] == "system"]) == 1
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "identity plus task"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok":true}' if "response_format" in body else "来源摘要"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })

    provider = LlamaCppProvider(settings.data["llm"])
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://identity-model", transport=httpx.MockTransport(respond))
    previous = runtime.llm
    runtime.llm = provider
    messages = [{"role": "system", "content": "任务"}, {"role": "user", "content": "原文"}]
    try:
        assert await provider.count_tokens(messages) == 3
        assert await provider.generate_json(messages, {"type": "object"}, max_tokens=32) == {"ok": True}
        assert await provider.summarize(messages, max_tokens=32) == "来源摘要"
        assert await provider.prefill(messages)
        assert len([path for path, _ in observed if path == "/v1/chat/completions"]) == 3
    finally:
        runtime.llm = previous
        await provider.close()
