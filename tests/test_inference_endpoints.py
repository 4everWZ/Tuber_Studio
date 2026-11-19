import httpx
from fastapi.testclient import TestClient

from tuber_studio.app import create_app


def test_retired_engine_control_and_probe_routes_are_absent(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        for method, path in (
            ("POST", "/api/inference/mode"), ("GET", "/api/inference/node"),
            ("PUT", "/api/inference/node"), ("POST", "/api/inference/node/check"),
            ("POST", "/api/inference/node/vision-probe"), ("POST", "/api/inference/node/idle"),
            ("GET", "/api/providers/local-models"), ("POST", "/api/providers/ping"),
            ("GET", "/api/models/dafeiyu/status"), ("POST", "/api/models/dafeiyu/download"),
        ):
            assert client.request(method, path, headers=headers).status_code == 404


def test_model_discovery_uses_only_the_saved_gateway(settings, monkeypatch):
    settings.data["omni"]["base_url"] = "https://studio.test/v1"
    settings.provider_keys = {"omni": "fixture-app-key"}
    requests = []

    async def send(client, request, **kwargs):
        requests.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json={"data": [{"id": "configured-alias"}]}, request=request)

    app = create_app(settings)
    with TestClient(app) as client:
        monkeypatch.setattr(httpx.AsyncClient, "send", send)
        headers = {"Authorization": "Bearer " + settings.admin_token}
        url = "/api/providers/models"
        assert client.get(url).status_code == 401
        assert client.post(url, headers=headers, json={"base_url": "https://another.test/v1"}).status_code == 405
        response = client.get(url, headers=headers)
        assert response.json() == {"models": ["configured-alias"]}
    assert requests == [("https://studio.test/v1/models", "Bearer fixture-app-key")]
