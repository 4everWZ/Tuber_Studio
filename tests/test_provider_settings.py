import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.deployment_profiles import business_configuration, mode_configuration
from tuber_studio.inference import RESOURCES
from tuber_studio.provider_settings import ProviderConfigurationError, apply_settings, proposal


def test_paired_provider_settings_survive_restart_and_live_round_trip(settings):
    settings.data = business_configuration(settings.data, "http://jetson.local:9012")
    live = json.loads(json.dumps(settings.data["work_mode_profiles"]["live"]))
    updated, _ = proposal(settings, {"llm": {"operations_max_tokens": 16384}})
    settings.save_public(updated)
    reloaded = Settings(runtime=settings.runtime)
    returned = mode_configuration(mode_configuration(reloaded.data, "live"), "text")
    assert returned["llm"]["operations_max_tokens"] == 16384
    assert returned["llm"]["model"] == live["llm"]["model"] == "dafeiyu-qwen35-9b:1.0.0"
    assert returned["llm"]["context_tokens"] == 262144
    assert returned["work_mode_profiles"]["live"] == live


def test_direct_provider_default_window_and_thinking_keep_paired_node_settings(settings):
    data = business_configuration(settings.data, "http://127.0.0.1:9012")
    direct = {"kind": "llama_cpp", "base_url": "http://127.0.0.1:11436", "model": "selected-9b",
              "native_context_tokens": 262144, "enable_thinking": True}
    settings.data = data | {"llm": data["llm"] | direct}
    proposed, _ = proposal(settings, {"llm": direct})
    assert proposed["llm"]["context_tokens"] == 262144
    assert proposed["llm"]["enable_thinking"] is True
    assert proposed["inference_broker"] == data["inference_broker"]
    assert proposed["work_mode_profiles"]["text"]["llm"] == proposed["llm"]


def api_patch(**extra):
    return {
        "llm": {
            "kind": "openai_compatible",
            "base_url": "https://api.test/v1",
            "model": "remote-model",
            "context_tokens": 8192,
            "native_context_tokens": 131072,
        },
        **extra,
    }


def test_authenticated_settings_save_masks_persists_and_clears_keys(settings):
    settings.data["features"]["live_enabled"] = False
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.put("/api/settings/providers", json=api_patch()).status_code == 401
        headers = {"Authorization": "Bearer " + settings.admin_token}
        response = client.put(
            "/api/settings/providers",
            headers=headers,
            json=api_patch(credentials={"llm": "private-api-test-key"}),
        )
        assert response.status_code == 200
        assert response.json()["provider_credentials"]["llm"]
        assert not response.json()["restart_required"]
        assert "private-api-test-key" not in response.text
        assert "private-api-test-key" not in settings.path.read_text()
        assert json.loads(settings.credentials_path.read_text())["llm"] == "private-api-test-key"
        if os.name != "nt":
            assert settings.credentials_path.stat().st_mode & 0o077 == 0
        response = client.get("/api/settings", headers=headers)
        assert "private-api-test-key" not in response.text
        assert (
            client.put(
                "/api/settings/providers", headers=headers, json={"credentials": {"llm": ""}}
            ).status_code
            == 200
        )
        assert settings.provider_key("llm") == "private-api-test-key"
        assert (
            client.put(
                "/api/settings/providers", headers=headers, json={"clear_credentials": ["llm"]}
            ).status_code
            == 200
        )
        assert settings.provider_key("llm") is None
        assert client.post("/api/live/start", headers=headers).status_code == 400
    reloaded = Settings(runtime=settings.runtime)
    assert reloaded.data["llm"]["context_tokens"] == 8192
    assert not reloaded.provider_key("llm")


@pytest.mark.asyncio
async def test_bad_config_or_changed_endpoint_does_not_leak_or_overwrite_credentials(runtime):
    settings = runtime.settings
    await apply_settings(runtime, api_patch(credentials={"llm": "private-api-test-key"}))
    original = settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm
    with pytest.raises(ValueError, match="旧凭证"):
        await apply_settings(runtime, {"llm": {"base_url": "https://different.test/v1"}})
    with pytest.raises(ValueError, match="上限"):
        await apply_settings(runtime, {"llm": {"context_tokens": 262144}})
    with pytest.raises(ValueError, match="HTTP"):
        await apply_settings(
            runtime,
            {"vision": {"kind": "openai_compatible", "base_url": "file:///etc/passwd", "model": "bad"}},
        )
    assert (settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm) == original


async def test_voice_settings_save_without_touching_model_or_node(runtime, monkeypatch):
    settings = runtime.settings
    settings.data = business_configuration(settings.data, "http://jetson.local:9012", mode="live")
    settings.inference_token = "paired-node-test-key"

    def unexpected_rpc(request):
        pytest.fail(f"Voice settings sent an unexpected node request: {request.method} {request.url.path}")

    runtime.inference.client = httpx.AsyncClient(
        base_url="http://jetson.local:9012", transport=httpx.MockTransport(unexpected_rpc)
    )
    before = runtime.llm, runtime.vision, runtime.inference.client, runtime.context.provider
    profiles = settings.data["work_mode_profiles"]
    settings.provider_keys["llm"] = "existing-model-key"
    runtime.store.set("comment_policy", {"auto_browse": True})

    api_tts = {
        "kind": "openai_compatible",
        "base_url": "https://speech.test/v1",
        "model": "tts",
        "voice": "a",
    }
    result = await apply_settings(runtime, {"tts": api_tts, "credentials": {"tts": "speech-test-key"}})
    assert not result["context_recount_required"]
    assert runtime.voices.cache is runtime.cache
    assert runtime.voices.client.headers["authorization"] == "Bearer speech-test-key"
    assert "x-tuber-inference-key" not in runtime.voices.client.headers
    assert settings.data["work_mode_profiles"] == profiles
    assert (runtime.llm, runtime.vision, runtime.inference.client, runtime.context.provider) == before
    assert (
        business_configuration(settings.data, "http://replacement.local:9012")["tts"] == settings.data["tts"]
    )

    def fail(data):
        raise OSError("injected write failure")

    api_voices = runtime.voices
    with monkeypatch.context() as patch:
        patch.setattr(settings, "save_public", fail)
        with pytest.raises(OSError):
            await apply_settings(runtime, {"tts": {"kind": "gpt_sovits"}, "clear_credentials": ["tts"]})
    assert runtime.voices is api_voices
    assert Settings(runtime=settings.runtime).provider_key("tts") == "speech-test-key"

    result = await apply_settings(runtime, {"tts": {"kind": "gpt_sovits"}, "clear_credentials": ["tts"]})
    assert not result["context_recount_required"]
    assert result["tts"]["base_url"] == "http://jetson.local:9012/tts"
    assert result["tts"]["local_backend"] == "genie_onnx_int8"
    assert runtime.voices.client.headers["x-tuber-inference-key"] == "paired-node-test-key"
    assert "authorization" not in runtime.voices.client.headers
    assert api_voices.client.is_closed
    assert settings.provider_keys == {"llm": "existing-model-key"}
    assert (runtime.llm, runtime.vision, runtime.inference.client, runtime.context.provider) == before


@pytest.mark.asyncio
async def test_failed_public_save_rolls_back_both_files_and_runtime(runtime, monkeypatch):
    settings = runtime.settings
    await apply_settings(runtime, api_patch(credentials={"llm": "old-test-key"}))
    original = settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm

    def fail(data):
        raise OSError("injected write failure")

    monkeypatch.setattr(settings, "save_public", fail)
    with pytest.raises(OSError):
        await apply_settings(runtime, {"credentials": {"llm": "new-test-key"}})
    assert (settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm) == original
    assert settings.provider_key("llm") == "old-test-key"


@pytest.mark.asyncio
async def test_busy_generation_and_running_parent_block_reconfiguration_but_saved_preferences_do_not(runtime):
    async with runtime.generation_lock:
        with pytest.raises(RuntimeError, match="生成"):
            await apply_settings(runtime, api_patch())
    runtime.store.set("comment_policy", {"auto_reply": True})
    runtime.store.set("dynamic_policy", {"enabled": True})
    await apply_settings(runtime, api_patch())
    runtime.operations.configure({"enabled": True}, account_id="100", character=runtime.character)
    with pytest.raises(RuntimeError, match="自主运营"):
        await apply_settings(runtime, api_patch())


@pytest.fixture
async def managed_settings(runtime, monkeypatch):
    """Real settings/runtime plus a transport representing committed broker RPCs."""
    from tuber_studio import provider_settings

    settings = runtime.settings
    settings.data["llm"].update(kind="llama_cpp", model="MiniCPM5-2B", context_tokens=65536)
    settings.data["inference_broker"].update(enabled=True, model="MiniCPM5-2B", context_tokens=131072)
    settings.provider_keys = {"llm": "original-private-test-key"}
    settings.save_public(settings.data)
    settings.save_keys(settings.provider_keys)
    state = {
        "window": 65536,
        "resident": True,
        "selected": "llm2b",
        "busy": False,
        "mode": "text",
        "extra": set(),
        "calls": [],
        "capacity_error": False,
        "new_committed": asyncio.Event(),
        "restore_entered": asyncio.Event(),
        "restore_release": asyncio.Event(),
        "hold_restore": False,
        "lose_response": None,
    }

    async def handle(request):
        payload = json.loads(request.content) if request.content else None
        state["calls"].append((request.method, request.url.path, payload))
        if request.method == "GET":
            body = {
                "mode": state["mode"],
                "busy": state["busy"],
                "selected_provider": state["selected"],
                "providers": {
                    key: {
                        "running": (state["resident"] and key == state["selected"]) or key in state["extra"],
                        "context_tokens": state["window"],
                    }
                    for key in RESOURCES
                },
            }
            if not state.get("omit_window"):
                body["requested_context_tokens"] = state["window"]
            return httpx.Response(200, json=body)
        if state.get("reject_rpc") == request.url.path:
            return httpx.Response(422, json={"detail": "confirmed rejection before resource changes"})
        if request.url.path == "/context":
            state["window"], state["resident"] = payload["context_tokens"], True
            # A real broker commits its public config before sending the response.
            # This marker is intentionally not a second implementation of it.
            settings.path.write_bytes(b'{"committed_rpc_side_effect":true}')
            if state["window"] == 8192 and state["lose_response"]:
                state["new_committed"].set()
                if state["lose_response"] == "timeout":
                    raise httpx.ReadTimeout("simulated response loss", request=request)
                await asyncio.Event().wait()
            if state["window"] == 65536 and state["hold_restore"]:
                state["restore_entered"].set()
                await state["restore_release"].wait()
        elif request.url.path == "/idle":
            state["resident"] = False
        elif request.url.path == "/model":
            state["selected"], state["resident"] = payload["key"], True
        return httpx.Response(200, json={})

    runtime.inference.client = httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handle)
    )
    created = []

    class StagedClient:
        closed = 0
        server_tokens = None
        profile_error = None

        async def ensure_capacity(self):
            if state["capacity_error"]:
                raise ValueError("simulated new provider failure")

        async def close(self):
            self.closed += 1

    def factory(*args):
        client = StagedClient()
        created.append(client)
        return client

    monkeypatch.setattr(provider_settings, "create_llm", factory)
    monkeypatch.setattr(provider_settings, "VoiceService", factory)
    monkeypatch.setattr(provider_settings, "VisionService", factory)
    return runtime, state, created


def originals(runtime):
    return (
        runtime.settings.path.read_bytes(),
        runtime.settings.credentials_path.read_bytes(),
        runtime.llm,
        runtime.voices,
        runtime.vision,
        runtime.context.provider,
        runtime.settings.data,
        dict(runtime.settings.provider_keys),
    )


def assert_rollback(runtime, previous, created):
    assert runtime.settings.path.read_bytes() == previous[0]
    assert runtime.settings.credentials_path.read_bytes() == previous[1]
    assert runtime.llm is previous[2] and runtime.voices is previous[3] and runtime.vision is previous[4]
    assert runtime.context.provider is previous[5] and runtime.settings.data is previous[6]
    assert runtime.settings.provider_keys == previous[7]
    assert len(created) == 3 and all(client.closed == 1 for client in created)
    assert not runtime.inference.mode_lock.locked() and not runtime.generation_lock.locked()


@pytest.mark.parametrize("fail_capacity", [False, True])
async def test_paired_legacy_model_proposal_uses_unified_address_and_transactional_switch(
    managed_settings, fail_capacity
):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    base = runtime.settings.data["inference_broker"]["base_url"]
    state["capacity_error"] = fail_capacity
    patch = {"llm": {"base_url": base + "/llm/dafeiyu", "model": "MiniCPM5-2B", "context_tokens": 8192}}
    expected = [
        ("/context", {"context_tokens": 8192}),
        ("/model", {"key": "llmdafeiyu"}),
    ]

    if fail_capacity:
        with pytest.raises(ValueError, match="simulated new provider failure"):
            await apply_settings(runtime, patch)
        expected += [("/model", {"key": "llm2b"}), ("/context", {"context_tokens": 65536})]
        assert state["selected"] == "llm2b" and state["window"] == 65536 and state["resident"]
        assert_rollback(runtime, previous, created)
    else:
        await apply_settings(runtime, patch)
        assert state["selected"] == "llmdafeiyu" and state["window"] == 8192 and state["resident"]
        persisted = json.loads(runtime.settings.path.read_text())["llm"]
        assert persisted["base_url"] == base + "/llm" and persisted["model"] == "MiniCPM-V-4.7-1B"

    assert [(path, payload) for method, path, payload in state["calls"] if method == "POST"] == expected


@pytest.mark.asyncio
async def test_committed_context_with_lost_response_is_restored_before_return(managed_settings):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state["lose_response"] = "timeout"
    with pytest.raises(httpx.ReadTimeout):
        await apply_settings(
            runtime, {"llm": {"context_tokens": 8192}, "credentials": {"llm": "new-test-key"}}
        )
    assert state["window"] == 65536 and state["resident"]
    assert [call[2] for call in state["calls"] if call[1] == "/context"] == [
        {"context_tokens": 8192},
        {"context_tokens": 65536},
    ]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_cancel_after_commit_and_repeated_cancel_wait_for_confirmed_rollback(managed_settings):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state.update(lose_response="cancel", hold_restore=True)
    work = asyncio.create_task(apply_settings(runtime, {"llm": {"context_tokens": 8192}}))
    await state["new_committed"].wait()
    work.cancel()
    await state["restore_entered"].wait()
    work.cancel()
    await asyncio.sleep(0)
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done() and runtime.inference.mode_lock.locked() and runtime.generation_lock.locked()
    state["restore_release"].set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert state["window"] == 65536 and state["resident"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
@pytest.mark.parametrize("was_local", [True, False])
async def test_failed_local_setting_restores_original_idle_not_extra_mini_residency(
    managed_settings, was_local
):
    runtime, state, created = managed_settings
    state["resident"] = False
    if not was_local:
        runtime.settings.data["llm"].update(
            kind="openai_compatible", model="old-remote", context_tokens=262144, native_context_tokens=262144
        )
        runtime.settings.data["inference_broker"]["context_tokens"] = 65536
        runtime.settings.save_public(runtime.settings.data)
    state["capacity_error"] = True
    previous = originals(runtime)
    with pytest.raises(ValueError, match="provider failure"):
        await apply_settings(
            runtime, {"llm": {"kind": "llama_cpp", "model": "MiniCPM5-2B", "context_tokens": 8192}}
        )
    assert state["window"] == 65536 and not state["resident"]
    assert ("POST", "/idle", {}) in state["calls"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_local_fallback_window_ignores_stale_broker_field_on_write_failure(
    managed_settings, monkeypatch
):
    runtime, state, created = managed_settings
    state["omit_window"] = True
    previous = originals(runtime)

    def fail(_):
        raise OSError("public configuration write failure")

    monkeypatch.setattr(runtime.settings, "save_public", fail)
    with pytest.raises(OSError):
        await apply_settings(
            runtime, {"llm": {"context_tokens": 8192}, "credentials": {"llm": "changed-test-key"}}
        )
    assert state["window"] == 65536 and state["resident"]
    assert runtime.settings.data["inference_broker"]["context_tokens"] == 131072
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_failed_external_setting_restores_original_local_resident(managed_settings, monkeypatch):
    runtime, state, created = managed_settings
    previous = originals(runtime)

    def fail(_):
        raise OSError("public configuration write failure")

    monkeypatch.setattr(runtime.settings, "save_public", fail)
    with pytest.raises(OSError):
        await apply_settings(runtime, api_patch(credentials={"llm": "changed-test-key"}))
    assert state["window"] == 65536 and state["resident"]
    assert state["calls"][1][1] == "/idle"
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["live", "busy", "other_worker"])
async def test_provider_save_does_not_claim_active_resource_experiments(managed_settings, conflict):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    if conflict == "live":
        state["mode"] = "live"
    elif conflict == "busy":
        state["busy"] = True
    else:
        state["extra"].add("llm1bq8")
    with pytest.raises(RuntimeError):
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert not any(call[0] == "POST" for call in state["calls"])
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_unconfirmed_broker_rollback_is_explicit_but_restores_secret_and_runtime(managed_settings):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state["capacity_error"] = True
    # The restoration RPC can fail independently after the initial commit.
    post = runtime.inference.client.post

    async def fail_restore(path, **kwargs):
        if path == "/context" and kwargs["json"]["context_tokens"] == 65536:
            raise httpx.ConnectError("restoration connection failed")
        return await post(path, **kwargs)

    runtime.inference.client.post = fail_restore
    with pytest.raises(ProviderConfigurationError, match="恢复结果未确认"):
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert state["window"] == 8192
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/context", "/idle"])
async def test_confirmed_rejection_with_unchanged_resources_does_not_reload_mini(managed_settings, path):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    state["reject_rpc"] = path
    patch = (
        {"llm": {"context_tokens": 8192}}
        if path == "/context"
        else api_patch(credentials={"llm": "new-test-key"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await apply_settings(runtime, patch)
    posts = [call for call in state["calls"] if call[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == path
    assert state["window"] == 65536 and state["resident"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_external_runtime_error_is_not_marked_safe_operator_guidance(managed_settings, monkeypatch):
    runtime, state, created = managed_settings
    previous = originals(runtime)
    from tuber_studio import provider_settings

    factory = provider_settings.create_llm

    async def fail():
        raise RuntimeError("untrusted provider message must stay private")

    def new_client(settings):
        client = factory(settings)
        client.ensure_capacity = fail
        return client

    monkeypatch.setattr(provider_settings, "create_llm", new_client)
    with pytest.raises(RuntimeError) as failure:
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert not isinstance(failure.value, ProviderConfigurationError)
    assert state["window"] == 65536 and state["resident"]
    assert_rollback(runtime, previous, created)


@pytest.mark.asyncio
async def test_remote_endpoint_save_succeeds_even_when_offline(runtime):
    settings = runtime.settings
    original_broker = settings.data["inference_broker"].copy()
    original_token = settings.inference_token
    patch = {
        "credentials": {"llm": "test-endpoint-key"},
        "llm": {
            "kind": "llama_cpp",
            "base_url": "http://192.0.2.1:9012/llm",
            "model": "MiniCPM-V-4.7-1B",
            "context_tokens": 8192,
            "native_context_tokens": 131072,
        },
        "tts": {
            "kind": "gpt_sovits",
            "base_url": "http://192.0.2.1:9012/tts",
            "model": "",
            "voice": "whale_sovits_v2",
        },
    }
    result = await apply_settings(runtime, patch)
    assert result["llm"]["base_url"] == "http://192.0.2.1:9012/llm"
    assert result["tts"]["base_url"] == "http://192.0.2.1:9012/tts"
    assert settings.data["inference_broker"].get("base_url") == original_broker.get("base_url")
    assert settings.data["inference_broker"].get("deployment") == original_broker.get("deployment")
    assert settings.inference_token == original_token
    assert settings.provider_key("llm") == "test-endpoint-key"
    assert runtime.llm.profile_error is not None  # Warned as unreachable, not crashed!
