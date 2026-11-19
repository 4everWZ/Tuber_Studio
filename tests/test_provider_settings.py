import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.http_api import endpoint
from tuber_studio.provider_settings import apply_settings, proposal


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
    def unexpected(*args, **kwargs):
        pytest.fail("Saving the connection should not contact a gateway")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=True) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        url = "/api/settings/providers"
        body = {"omni": {"base_url": "https://studio.test/proxy/v1", "model": "omni-a", "responses_model": "worker-a"},
                "credentials": {"omni": "unit-key"}}
        assert client.put(url, json=body).status_code == 401
        saved = client.put(url, headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        runtime = app.state.runtime
        for service in (runtime.llm, runtime.vision, runtime.voices, runtime.browsing.visual_provider.api):
            assert service.client.headers["Authorization"] == "Bearer unit-key"
            assert str(service.client.base_url).rstrip("/") == body["omni"]["base_url"]
        assert saved.json()["provider_credentials"] == {"omni": True}
        assert "unit-key" not in saved.text and "unit-key" not in settings.path.read_text()
        loaded = Settings(runtime=settings.runtime)
        assert loaded.provider_key("omni") == "unit-key"
        assert loaded.responses_config["base_url"] == "https://studio.test/proxy/llm/v1"
        assert loaded.video_config["model"] == "omni-a"
        assert json.loads(loaded.credentials_path.read_text()) == {
            "omni": {"base_url": "https://studio.test/proxy/v1", "key": "unit-key"}}
        if os.name != "nt":
            assert loaded.credentials_path.stat().st_mode & 0o077 == 0
        assert client.put(url, headers=headers, json={"omni": {"base_url": "https://new.test/v1"}}).status_code == 400
        assert client.put(url, headers=headers, json={"credentials": {"omni": ""}}).json()["provider_credentials"]["omni"]
        assert client.put(url, headers=headers, json={"clear_credentials": ["omni"]}).status_code == 200
        assert not client.get("/api/settings", headers=headers).json()["provider_credentials"]["omni"]
    assert not Settings(runtime=settings.runtime).provider_key("omni")


async def test_failed_config_save_restores_files_and_runtime(runtime, monkeypatch):
    settings = runtime.settings
    await apply_settings(runtime, {"credentials": {"omni": "original-key"}})
    before = settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm, runtime.voices, runtime.vision

    from tuber_studio import config
    write = config.atomic_json

    def fail_public(path, data, **options):
        if path == settings.path:
            raise OSError("simulated write failure")
        write(path, data, **options)

    monkeypatch.setattr(config, "atomic_json", fail_public)
    with pytest.raises(OSError):
        await apply_settings(runtime, {"credentials": {"omni": "replacement-key"}})
    assert before == (settings.path.read_bytes(), settings.credentials_path.read_bytes(), runtime.llm, runtime.voices, runtime.vision)
    assert settings.provider_key("omni") == "original-key"


async def test_active_work_blocks_provider_change(runtime):
    async with runtime.generation_lock:
        with pytest.raises(RuntimeError, match="生成"):
            await apply_settings(runtime, {"omni": {"enable_thinking": False}})
    runtime.operations.configure({"enabled": True}, account_id="100", character=runtime.character)
    with pytest.raises(RuntimeError, match="自主运营"):
        await apply_settings(runtime, {"omni": {"enable_thinking": False}})
