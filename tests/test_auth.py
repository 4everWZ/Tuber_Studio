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


def test_cookie_persists_across_app_restart_and_browser_clear_requires_login(settings):
    first_app = create_app(settings, ReadOnlyPlatform())
    with TestClient(first_app) as first:
        response = first.post("/api/auth", json={"token": settings.admin_token})
        cookie = first.cookies.get("tuber_admin")
        assert "Max-Age=315360000" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
    # Same runtime survives create_app/code reload; no process-local session map.
    with TestClient(create_app(settings, ReadOnlyPlatform())) as restarted:
        restarted.cookies.set("tuber_admin", cookie)
        assert restarted.get("/api/status").status_code == 200
        restarted.cookies.clear()
        assert restarted.get("/api/status").status_code == 401


def test_reset_token_revokes_all_clients_and_old_bearers_without_changing_overlay(settings):
    old, overlay = settings.admin_token, settings.overlay_token
    app_one, app_two = create_app(settings, ReadOnlyPlatform()), create_app(settings, ReadOnlyPlatform())
    with TestClient(app_one) as one, TestClient(app_two) as two:
        one.post("/api/auth", json={"token": old}).raise_for_status()
        two.post("/api/auth", json={"token": old}).raise_for_status()
        assert (
            one.post("/api/auth/reset-token", headers={"Origin": "https://other.example"}).status_code == 403
        )
        reset = one.post("/api/auth/reset-token")
        assert reset.status_code == 200
        new = reset.json()["token"]
        assert reset.json()["authenticated"] is False and new != old
        assert one.cookies.get("tuber_admin") is None
        assert two.get("/api/status").status_code == 401
        assert two.get("/api/status", headers={"Authorization": "Bearer " + old}).status_code == 401
        assert two.post("/api/auth", json={"token": old}).status_code == 401
        assert two.post("/api/auth", json={"token": new}).status_code == 200
        assert new not in two.get("/api/status").text
        assert two.post("/api/overlay/auth", json={"token": overlay}).status_code == 200


def test_external_token_replacement_is_observed_without_restart(client, settings):
    old = settings.admin_token
    client.post("/api/auth", json={"token": old}).raise_for_status()
    (settings.runtime / "admin-token").write_text("external-new-token")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer " + old}).status_code == 401
    assert (
        client.get("/api/status", headers={"Authorization": "Bearer external-new-token"}).status_code == 200
    )


def test_cookie_is_secure_on_https_and_rolls_only_when_due(settings):
    from tuber_studio.auth_sessions import COOKIE_REFRESH_SECONDS

    app = create_app(settings, ReadOnlyPlatform())
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post("/api/auth", json={"token": settings.admin_token})
        cookie = client.cookies.get("tuber_admin")
        assert "Secure" in login.headers["set-cookie"]
        assert "set-cookie" not in client.get("/api/status").headers
        app.state.store.execute(
            "UPDATE admin_sessions SET refreshed_at=refreshed_at-?", (COOKIE_REFRESH_SECONDS + 1,)
        )
        renewed = client.get("/api/status")
        assert "set-cookie" in renewed.headers and "Secure" in renewed.headers["set-cookie"]
        assert client.cookies.get("tuber_admin") == cookie
        assert "set-cookie" not in client.get("/api/status").headers
        logout = client.post("/api/logout")
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert client.get("/api/status").status_code == 401


def test_reset_needs_admin_and_environment_key_remains_externally_managed(settings, monkeypatch):
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "environment-fixture")
    with TestClient(create_app(settings, ReadOnlyPlatform())) as client:
        assert client.post("/api/auth/reset-token").status_code == 401
        client.post("/api/overlay/auth", json={"token": settings.overlay_token}).raise_for_status()
        assert client.post("/api/auth/reset-token").status_code == 401
        client.post("/api/auth", json={"token": "environment-fixture"}).raise_for_status()
        response = client.post("/api/auth/reset-token")
        assert response.status_code == 409 and "token" not in response.json()
        assert client.get("/api/status").status_code == 200
