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


def test_dafeiyu_model_hub_status_and_download(settings, monkeypatch):
    app = create_app(settings)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        res = client.get("/api/models/dafeiyu/status", headers=headers)
        assert res.status_code == 200
        status = res.json()
        assert status["model_name"] == "MiniCPM-V-4.7-1B (大肥鱼专属微调)"
        assert status["filename"] == "MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf"
        assert status["repo_id"] == "4everImmortality/dafeiyu-minicpmv"
        assert "installed" in status

        # Trigger cancel
        cancel_res = client.post("/api/models/dafeiyu/cancel", headers=headers)
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "cancelled"


def test_model_hub_module_logic(tmp_path):
    from tuber_studio import model_hub

    # 1. Non-existent file
    assert not model_hub.is_model_installed(tmp_path)
    status = model_hub.get_model_status(tmp_path)
    assert status["installed"] is False
    assert status["file_size"] == 0

    # 2. Corrupt / small file (<600MB)
    fake_model = tmp_path / model_hub.DEFAULT_MODEL_FILENAME
    fake_model.write_bytes(b"too small")
    assert not model_hub.is_model_installed(tmp_path)

    # 3. Simulate installed model (>= 600MB via mock stat)
    import os
    import unittest.mock as mock
    fake_stat = os.stat_result((0o100644, 0, 0, 0, 0, 0, 672003776, 0, 0, 0))
    with mock.patch("pathlib.Path.stat", return_value=fake_stat):
        assert model_hub.is_model_installed(tmp_path)
        status = model_hub.get_model_status(tmp_path)
        assert status["installed"] is True

    # 4. ensure_model_on_first_run returns True if already installed
    with mock.patch.object(model_hub, "is_model_installed", return_value=True):
        assert model_hub.ensure_model_on_first_run(tmp_path) is True

    # 5. ensure_model_on_first_run starts download if missing
    with mock.patch.object(model_hub, "is_model_installed", return_value=False), \
         mock.patch.object(model_hub, "start_model_download") as mock_dl:
        assert model_hub.ensure_model_on_first_run(tmp_path) is False
        mock_dl.assert_called_once_with(tmp_path, force=False)


