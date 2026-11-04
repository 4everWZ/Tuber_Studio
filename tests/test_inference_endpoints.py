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
        assert any("Qwen" in m["name"] for m in data["base"])


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
        res1 = client.get("/api/providers/models?base_url=http://openai.test/v1", headers=headers)
        assert res1.status_code == 200
        assert res1.json()["models"] == ["deepseek-chat", "deepseek-reasoner"]

        # 2. Test Ollama format
        res2 = client.get("/api/providers/models?base_url=http://ollama.test/v1", headers=headers)
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
            "llm_url": "http://good-llm:8000/v1",
            "tts_url": "http://offline:9880",
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
