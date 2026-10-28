import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app


class ReadOnlyPlatform:
    async def close(self):
        pass


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, ReadOnlyPlatform())) as value:
        yield value


def test_admin_api_requires_auth_and_cross_origin_writes_are_rejected(client, settings):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/inference/mode", json={"mode": "live"}).status_code == 401
    assert client.post("/api/auth", json={"token": settings.admin_token}).status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/live/start", headers={"Origin": "https://other.example"}).status_code == 403
    assert (
        client.post(
            "/api/inference/mode", json={"mode": "live"}, headers={"Origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/inference/mode", json={"mode": "arbitrary"}).status_code == 422
    assert client.post("/api/inference/mode", json={"mode": "live"}).status_code == 400
    assert client.post("/api/live/start").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/status").status_code == 401


def test_overlay_credential_never_grants_admin_access(client, settings):
    assert client.post("/api/overlay/auth", json={"token": settings.overlay_token}).status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/live/start").status_code == 401
    with client.websocket_connect("/ws/overlay") as socket:
        assert socket.receive_json()["type"] == "hello"
    assert client.post("/api/auth", json={"token": settings.overlay_token}).status_code == 401
