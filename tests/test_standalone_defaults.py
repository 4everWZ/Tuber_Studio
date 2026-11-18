import json

import httpx
import pytest

from tuber_studio.api_providers import create_llm
from tuber_studio.config import Settings


def test_fresh_install_uses_the_omni_service_without_pairing(tmp_path, monkeypatch):
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    settings = Settings(runtime=tmp_path)
    data = settings.data
    assert data["inference_broker"] == {"enabled": False}
    assert not (tmp_path / "inference-token").exists()
    assert data["llm"]["kind"] == "openai_compatible"
    assert data["llm"]["context_source"] == "model_card" and data["llm"]["enable_thinking"]
    assert data["llm"]["model"] == data["video_vision"]["model"] == "omni-default"
    assert data["llm"]["base_url"] == data["video_vision"]["base_url"] == "http://127.0.0.1:8000/v1"
    assert data["responses"] == {"model": "llm-default", "base_url": "http://127.0.0.1:8000/llm/v1"}
    assert data["tts"]["kind"] == "prerecorded"


@pytest.mark.parametrize("name", ["llm", "responses", "vision", "tts"])
def test_saved_key_never_follows_an_edited_service_address(tmp_path, monkeypatch, name):
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


def test_orphaned_legacy_credentials_are_not_assigned_to_the_new_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    (tmp_path / "inference-token").write_text("previous-node-key")
    (tmp_path / "provider-credentials.json").write_text(json.dumps({"llm": "previous-provider-key"}))
    settings = Settings(runtime=tmp_path)
    assert settings.provider_key("llm") is None
    assert settings.video_vision_key() is None
    assert settings.inference_token is None


@pytest.mark.asyncio
async def test_model_requests_only_send_the_key_to_its_bound_route(tmp_path, monkeypatch):
    monkeypatch.delenv("TUBER_CONFIG", raising=False)
    settings = Settings(runtime=tmp_path)
    settings.data["llm"]["base_url"] = "https://gateway.example/tenant/v1"
    settings.save_keys({"llm": "test-tenant-key"})
    settings.save_public(settings.data)
    headers = []

    def receive(request):
        headers.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(receive)
    monkeypatch.setattr("tuber_studio.http_api.inference_transport", lambda base: transport)
    for base in ("https://gateway.example/tenant/v1", "https://gateway.example/other/v1", "https://unknown.example/v1"):
        settings.data["llm"]["base_url"] = base
        settings.save_public(settings.data)
        client = create_llm(Settings(runtime=tmp_path))
        try:
            await client.client.get("models")
        finally:
            await client.close()
    assert headers == ["Bearer test-tenant-key", None, None]
