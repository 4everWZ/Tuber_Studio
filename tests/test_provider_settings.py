import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.deployment_profiles import business_configuration
from tuber_studio.http_api import endpoint
from tuber_studio.provider_settings import apply_settings, proposal


def test_omni_gateway_can_use_http_only_at_the_internal_alias():
    assert endpoint("http://omni-gateway:8000/v1") == "http://omni-gateway:8000/v1"
    with pytest.raises(ValueError, match="公网API须使用HTTPS"):
        endpoint("http://omni-gateway.example:8000/v1")


def test_direct_provider_default_window_and_thinking_keep_paired_node_settings(settings):
    data = business_configuration(settings.data, "http://127.0.0.1:9012")
    direct = {"kind": "llama_cpp", "base_url": "http://127.0.0.1:11436", "model": "selected-9b",
              "native_context_tokens": 262144, "enable_thinking": True}
    settings.data = data | {"llm": data["llm"] | direct}
    proposed, _ = proposal(settings, {"llm": direct})
    assert proposed["llm"]["context_tokens"] == 262144
    assert proposed["llm"]["enable_thinking"] is True
    assert proposed["inference_broker"] == data["inference_broker"]
    assert proposed["work_mode_profiles"] == data["work_mode_profiles"]


def test_live_thinking_preference_survives_restart(settings):
    updated, _ = proposal(settings, {"llm": {"live_thinking_tokens": 256}})
    settings.save_public(updated)
    reloaded = Settings(runtime=settings.runtime)
    assert reloaded.data["llm"]["live_thinking_tokens"] == 256
    with pytest.raises(ValueError, match="0.*8192"):
        proposal(reloaded, {"llm": {"live_thinking_tokens": 8193}})


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
        assert json.loads(settings.credentials_path.read_text())["llm"] == {
            "base_url": "https://api.test/v1", "key": "private-api-test-key",
        }
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

    monkeypatch.setattr(httpx.AsyncClient, "send", lambda client, request, **kwargs: unexpected_rpc(request))
    before = runtime.llm, runtime.vision, runtime.context.provider
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
    assert (runtime.llm, runtime.vision, runtime.context.provider) == before
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
    assert (runtime.llm, runtime.vision, runtime.context.provider) == before


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


@pytest.mark.asyncio
async def test_provider_change_with_legacy_broker_never_changes_server_resources(runtime, monkeypatch):
    runtime.settings.data = business_configuration(runtime.settings.data, "http://127.0.0.1:9012")
    requests = []

    async def unavailable(client, request, **kwargs):
        requests.append((request.method, request.url.path))
        return httpx.Response(503, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", unavailable)
    with pytest.raises(httpx.HTTPStatusError):
        await apply_settings(runtime, {"llm": {"context_tokens": 8192}})
    assert requests == [("GET", "/llm/props")]
    await apply_settings(runtime, api_patch())
    assert requests == [("GET", "/llm/props")]


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
