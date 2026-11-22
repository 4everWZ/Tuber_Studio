import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.core.config import Settings
from tuber_studio.core.validation import endpoint
from tuber_studio.infra.inference.settings import apply_settings, proposal


def test_internal_gateway_http_exception_does_not_include_public_hosts(monkeypatch):
    monkeypatch.setenv("OMNI_GATEWAY_URL", "http://studio-service:8000")
    assert endpoint("http://studio-service:8000/v1") == "http://studio-service:8000/v1"
    with pytest.raises(ValueError, match="HTTPS"):
        endpoint("http://studio-service:9000/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        endpoint("http://omni-gateway.example:8000/v1")


def test_settings_reject_independent_routes_and_keys(tmp_path):
    settings = Settings(runtime=tmp_path)
    for request in ({"tts": {}}, {"vision": {}}, {"llm": {}}, {"responses": {}},
                    {"use_omni_video": True}, {"credentials": {"tts": "old-key"}},
                    {"omni": {"kind": "demo"}}, {"omni": {"enable_thinking": 1}},
                    {"omni": {"base_url": "file:///wrong"}}, {"omni": {"operations_max_tokens": 0}}):
        with pytest.raises(ValueError):
            proposal(settings, request)


def test_authenticated_single_connection_save_restart_and_clear(settings, monkeypatch):
    async def models(client, request, **kwargs):
        assert request.method == "GET" and request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "omni-a", "max_model_len": 8192}]})

    monkeypatch.setattr(httpx.AsyncClient, "send", models)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=True) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        url = "/api/v1/system/settings/providers"
        body = {"omni": {"base_url": "https://studio.test/proxy/v1", "model": "omni-a", "responses_model": "worker-a", "video_options": {"max_frames": 24}},
                "credentials": {"omni": "unit-key"}}
        assert client.put(url, json=body).status_code == 401
        saved = client.put(url, headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        runtime = app.state.runtime
        for service in (runtime.llm, runtime.voices):
            assert service.client.headers["Authorization"] == "Bearer unit-key"
            assert str(service.client.base_url).rstrip("/") == body["omni"]["base_url"]
        for service in (runtime.operation_llm, runtime.browsing.visual_provider.api):
            assert service.client.headers["Authorization"] == "Bearer unit-key"
            assert str(service.client.base_url).rstrip("/") == "https://studio.test/proxy/llm/v1"
            assert service.config["model"] == "worker-a"
        assert runtime.operation_llm.client.headers["Authorization"] == "Bearer unit-key"
        assert str(runtime.operation_llm.client.base_url).rstrip("/") == "https://studio.test/proxy/llm/v1"
        assert runtime.operation_llm.config["model"] == "worker-a"
        assert runtime.operation_llm.config["structured_output"] == "json_schema"
        assert runtime.operation_context.provider is runtime.operation_llm
        assert runtime.browsing.evidence.visual_batch_size() == 24
        assert saved.json()["provider_credentials"] == {"omni": True}
        assert "unit-key" not in saved.text and "unit-key" not in settings.path.read_text()
        loaded = Settings(runtime=settings.runtime)
        assert loaded.provider_key("omni") == "unit-key"
        assert loaded.responses_config["base_url"] == "https://studio.test/proxy/llm/v1"
        assert loaded.video_config["model"] == "worker-a"
        assert loaded.video_config["max_frames"] == 24
        assert json.loads(loaded.credentials_path.read_text()) == {
            "omni": {"base_url": "https://studio.test/proxy/v1", "key": "unit-key"}}
        if os.name != "nt":
            assert loaded.credentials_path.stat().st_mode & 0o077 == 0
        assert client.put(url, headers=headers, json={"omni": {"base_url": "https://new.test/v1"}}).status_code == 400
        assert client.put(url, headers=headers, json={"credentials": {"omni": ""}}).json()["provider_credentials"]["omni"]
        assert client.put(url, headers=headers, json={"clear_credentials": ["omni"]}).status_code == 200
        assert not client.get("/api/v1/system/settings", headers=headers).json()["provider_credentials"]["omni"]
    assert not Settings(runtime=settings.runtime).provider_key("omni")


async def test_failed_config_save_restores_files_and_runtime(runtime, monkeypatch):
    settings = runtime.settings
    await apply_settings(runtime, {"credentials": {"omni": "original-key"}})
    before = settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm, runtime.operation_llm, runtime.voices

    from tuber_studio.core import config as config
    write = config.atomic_json

    def fail_public(path, data, **options):
        if path == settings.path:
            raise OSError("simulated write failure")
        write(path, data, **options)

    monkeypatch.setattr(config, "atomic_json", fail_public)
    with pytest.raises(OSError):
        await apply_settings(runtime, {"credentials": {"omni": "replacement-key"}})
    assert before == (settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm, runtime.operation_llm, runtime.voices)
    assert settings.provider_key("omni") == "original-key"


async def test_active_work_blocks_provider_change(runtime):
    async with runtime.generation_lock:
        with pytest.raises(RuntimeError, match="生成"):
            await apply_settings(runtime, {"omni": {"enable_thinking": False}})
    runtime.operations.configure({"enabled": True}, account_id="100", character=runtime.character)
    with pytest.raises(RuntimeError, match="自主运营"):
        await apply_settings(runtime, {"omni": {"enable_thinking": False}})

def test_saved_foreground_vision_pin_does_not_block_background_vision(settings):
    saved = settings.data | {"omni": settings.data["omni"] | {
        "video_options": {"model_revision": "prior-omni-pin", "max_frames": 7},
    }}
    settings.path.write_text(json.dumps(saved), encoding="utf-8")
    reloaded = Settings(runtime=settings.runtime)
    assert reloaded.video_config["model"] == reloaded.responses_config["model"]
    assert reloaded.video_config["model_revision"] == ""
    assert reloaded.video_config["max_frames"] == 7
