import json

import httpx
import pytest

from tuber_studio.api_providers import create_llm
from tuber_studio.config import Settings


def test_fresh_install_uses_the_omni_service_without_pairing(tmp_path, monkeypatch):
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    settings = Settings(runtime=tmp_path)
    data = settings.data
    assert not {"inference_broker", "llm", "tts", "vision", "video_vision", "responses"} & data.keys()
    assert not (tmp_path / "inference-token").exists()
    assert data["omni"]["context_source"] == "model_card" and data["omni"]["enable_thinking"]
    assert data["omni"]["model"] == "omni-default"
    assert settings.video_config["model"] == settings.responses_config["model"] == "llm-default"
    assert settings.video_config["base_url"] == settings.responses_config["base_url"]
    assert settings.responses_config["base_url"] == "http://127.0.0.1:8000/llm/v1"
    assert data["voice_id"] == "whale_reference"


def test_saved_key_never_follows_an_edited_service_address(tmp_path, monkeypatch):
    name = "omni"
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    settings = Settings(runtime=tmp_path)
    settings.data[name]["base_url"] = "https://trusted.example/v1"
    settings.save_keys({name: "scoped-test-key"})
    settings.save_public(settings.data)
    settings = Settings(runtime=tmp_path)
    assert settings.provider_key(name) == "scoped-test-key"
    settings.data[name]["base_url"] = "https://unknown.example/v1"
    settings.save_public(settings.data)
    assert settings.provider_key(name) is None
    assert Settings(runtime=tmp_path).provider_key(name) is None


@pytest.mark.parametrize("old_key", ["previous-provider-key",
                                     {"base_url": "http://127.0.0.1:8000/v1", "key": "previous-provider-key"}])
def test_orphaned_legacy_credentials_are_not_assigned_to_the_new_default(tmp_path, monkeypatch, old_key):
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    (tmp_path / "inference-token").write_text("previous-node-key")
    (tmp_path / "provider-credentials.json").write_text(json.dumps({"llm": old_key}))
    settings = Settings(runtime=tmp_path)
    assert settings.provider_key("omni") is None
    assert not settings.provider_keys


@pytest.mark.asyncio
async def test_model_requests_only_send_the_key_to_its_bound_route(tmp_path, monkeypatch):
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    settings = Settings(runtime=tmp_path)
    settings.data["omni"]["base_url"] = "https://gateway.example/tenant/v1"
    settings.save_keys({"omni": "test-tenant-key"})
    settings.save_public(settings.data)
    headers = []

    def receive(request):
        headers.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(receive)
    monkeypatch.setattr("tuber_studio.http_api.inference_transport", lambda base: transport)
    for base in ("https://gateway.example/tenant/v1", "https://gateway.example/other/v1", "https://unknown.example/v1"):
        settings.data["omni"]["base_url"] = base
        settings.save_public(settings.data)
        client = create_llm(Settings(runtime=tmp_path))
        try:
            await client.client.get("models")
        finally:
            await client.close()
    assert headers == ["Bearer test-tenant-key", None, None]
