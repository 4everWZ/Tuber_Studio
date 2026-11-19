import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent import private_call
from tuber_studio.app import create_app


@pytest.mark.parametrize("behind_edge", [True, False])
def test_ticket_is_admin_only_fixed_upstream_and_contains_no_service_credential(settings, tmp_path, monkeypatch, behind_edge):
    key = tmp_path / "voice.key"
    key.write_text("private-application-fixture")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_ORIGIN", "https://voice.example.org")
    if behind_edge:
        monkeypatch.setenv("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN", "https://192.0.2.10")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_TOKEN_FILE", str(key))
    issued = []

    async def issue(client, request, **kwargs):
        issued.append(request)
        assert not client.follow_redirects
        return httpx.Response(200, request=request, json={"ticket": "a" * 32, "expires_in": 30, "secret": key.read_text()})

    app = create_app(settings)
    with TestClient(app) as browser:
        monkeypatch.setattr(httpx.AsyncClient, "send", issue)
        path = "/api/chat/private-call/ticket"
        auth = {"Authorization": "Bearer " + settings.admin_token}
        assert browser.post(path).status_code == 401
        assert browser.post(path, headers=auth | {"Origin": "https://elsewhere.example"}).status_code == 403
        assert browser.get("/api/chat/private-call", headers=auth).json() == {"configured": True}
        assert not issued
        reply = browser.post(path, headers=auth, json={"path": "/api/mode", "origin": "https://elsewhere.example"})
        assert reply.status_code == 200
        assert reply.headers["cache-control"] == "no-store"
        assert key.read_text() not in reply.text and settings.admin_token not in reply.text
        browser_host = "192.0.2.10" if behind_edge else "voice.example.org"
        assert reply.json()["socket"] == f"wss://{browser_host}/v1/realtime"
        assert reply.json()["character"] == app.state.runtime.character["name"]
        assert reply.json()["avatar"]["speaking"] == "/assets/whale/sing_open.png"
        assert len(issued) == 1
        request = issued[0]
        assert request.method == "POST" and str(request.url) == "https://voice.example.org/v1/browser-tickets"
        assert request.headers["authorization"] == "Bearer " + key.read_text()
        assert request.content == b'{"path":"/v1/realtime"}'


def test_private_avatar_pair_is_not_inherited_by_name_or_manifest(settings):
    clone = {"id": "other", "name": "大肥鱼", "avatar_manifest": "assets/whale/manifest.json"}
    profile = private_call.call_avatar(settings, clone)
    assert profile["speaking"] is None and profile["still"] == "/assets/whale/idle.gif"
    for role in (clone, clone | {"id": "whale"}):
        assert private_call.call_avatar(settings, role | {"avatar_manifest": "missing.json"}) == {
            "still": None, "speaking": None, "fallback": None,
        }


@pytest.mark.parametrize("origin", ["", "http://192.0.2.3", "https://voice.example/llm", "https://user@voice.example"])
def test_unconfigured_or_insecure_service_is_not_offered(monkeypatch, tmp_path, origin):
    key = tmp_path / "token"
    key.write_text("test-app-token")
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_TOKEN_FILE", str(key))
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_ORIGIN", origin)
    assert private_call.private_service() is None


@pytest.mark.parametrize("data", [{"ticket": "app-credential-do-not-echo"}, {}, {"ticket": 5}])
async def test_bad_upstream_replies_fail_closed(data, monkeypatch):
    async def issue(client, request, **kwargs):
        return httpx.Response(200, request=request, json=data)
    monkeypatch.setattr(httpx.AsyncClient, "send", issue)
    with pytest.raises(ValueError):
        await private_call.mint_ticket("https://voice.example", "app-credential-do-not-echo")
