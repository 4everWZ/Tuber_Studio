import httpx
from fastapi.testclient import TestClient

from tuber_studio.app import create_app


def test_get_local_models_endpoint(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        res = client.get("/api/providers/local-models", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert "fine_tuned" in data
        assert "base" in data
        assert any("MiniCPM-V-4.7-1B" in m["name"] for m in data["fine_tuned"])
        assert any("MiniCPM5-2B" in m["name"] for m in data["base"])
        assert any(m["multimodal"] is True for m in data["fine_tuned"])
        assert any(m["id"] == "MiniCPM5-2B" and m["multimodal"] is False for m in data["base"])


def test_get_remote_models_endpoint_error_handling(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        # Invalid URL scheme
        res = client.get("/api/providers/models?base_url=invalid-url", headers=headers)
        assert res.status_code == 400


def test_get_remote_models_endpoint_parsing(settings, monkeypatch):
    app = create_app(settings)

    # Mock httpx.AsyncClient.get for remote model fetch
    async def mock_get(self, url, headers=None):
        if "openai" in str(url):
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
        elif "ollama" in str(url):
            return httpx.Response(200, json={"models": [{"name": "minicpm-v:latest"}]})
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        # 1. Test OpenAI format
        res1 = client.get("/api/providers/models?base_url=https://openai.test/v1", headers=headers)
        assert res1.status_code == 200
        assert res1.json()["models"] == ["deepseek-chat", "deepseek-reasoner"]

        # 2. Test Ollama format
        res2 = client.get("/api/providers/models?base_url=https://ollama.test/v1", headers=headers)
        assert res2.status_code == 200
        assert res2.json()["models"] == ["minicpm-v:latest"]


def test_ping_endpoints_success_and_tolerant_failure(settings, monkeypatch):
    app = create_app(settings)

    async def mock_get(self, url, headers=None):
        url_str = str(url)
        if "good-llm" in url_str:
            return httpx.Response(200)
        elif "offline" in url_str:
            raise httpx.ConnectError("Connection refused")
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        payload = {
            "llm_url": "http://good-llm.local:8000/v1",
            "tts_url": "http://offline.local:9880",
        }
        res = client.post("/api/providers/ping", headers=headers, json=payload)
        assert res.status_code == 200
        data = res.json()

        # LLM succeeded
        assert data["llm"]["ok"] is True
        assert data["llm"]["status_code"] == 200
        assert data["llm"]["latency_ms"] >= 0

        # TTS failed gracefully without 500 error
        assert data["tts"]["ok"] is False
        assert "连接被拒绝" in data["tts"]["detail"]


def test_diagnostics_scope_credentials_to_the_selected_service(settings, monkeypatch):
    settings.inference_token = "paired-token"
    settings.data["inference_broker"] = {"enabled": True, "base_url": "http://jetson.local:9012"}
    settings.data["llm"]["base_url"] = "https://saved.test/v1"
    settings.provider_keys["llm"] = "saved-api-key"
    requests = {}

    async def remote_get(self, url, headers=None):
        requests[url] = headers
        return httpx.Response(200, json={"data": [{"id": "model"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", remote_get)
    with TestClient(create_app(settings)) as client:
        admin = {"Authorization": "Bearer " + settings.admin_token}
        assert client.get("/api/models/dafeiyu/status", headers=admin).status_code == 404
        assert not requests
        assert (
            client.post(
                "/api/providers/ping",
                headers=admin,
                json={"llm_url": "http://jetson.local:9012/llm", "tts_url": "http://other.local:9012/tts"},
            ).status_code
            == 200
        )
        for payload in (
            {"base_url": "https://saved.test/v1"},
            {"base_url": "https://new.test/v1", "api_key": "new-api-key"},
        ):
            response = client.post("/api/providers/models", headers=admin, json=payload)
            assert response.json()["models"] == ["model"]

    assert requests == {
        "http://jetson.local:9012/llm/v1/models": {"X-Tuber-Inference-Key": "paired-token"},
        "http://other.local:9012/tts/docs": {},
        "https://saved.test/v1/models": {"Authorization": "Bearer saved-api-key"},
        "https://new.test/v1/models": {"Authorization": "Bearer new-api-key"},
    }


def test_retired_business_host_model_downloader_is_not_exposed(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        assert client.get("/api/models/dafeiyu/status", headers=headers).status_code == 404
        for action in ("download", "cancel"):
            assert client.post(f"/api/models/dafeiyu/{action}", headers=headers).status_code == 404
        assert not list(settings.models.iterdir())


def test_model_catalog_uses_current_profile_and_observed_inventory(settings):
    from tuber_studio.inference_profiles import GROUNDED_DAFEIYU_KEY, GROUNDED_DAFEIYU_MODEL

    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        data = client.get("/api/providers/local-models", headers=headers).json()
        assert data["fine_tuned"][0]["id"] == GROUNDED_DAFEIYU_MODEL
        assert all(item["installed"] is None for group in data.values() for item in group)
        app.state.runtime.inference.capabilities = {
            "profiles": {GROUNDED_DAFEIYU_KEY: {"installed": True}, "llm2b": {"installed": False}}
        }
        data = client.get("/api/providers/local-models", headers=headers).json()
        assert data["fine_tuned"][0]["installed"] is True
        assert next(item for item in data["base"] if item["id"] == "MiniCPM5-2B")["installed"] is False
        app.state.runtime.inference.capabilities["monitoring_error"] = "node disconnected"
        assert (
            client.get("/api/providers/local-models", headers=headers).json()["fine_tuned"][0]["installed"]
            is None
        )


def test_saving_provider_settings_has_no_implicit_download_result(settings, monkeypatch):
    async def apply(runtime, data):
        return {"status": "ok", "context_recount_required": True}

    monkeypatch.setattr("tuber_studio.model_runtime.api.apply_settings", apply)
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/api/settings/providers",
            json={"llm": {"model": "custom"}},
            headers={"Authorization": "Bearer " + settings.admin_token},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "context_recount_required": True}
        assert not list(settings.models.iterdir())
