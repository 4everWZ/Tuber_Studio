import json

import httpx
from fastapi.testclient import TestClient

from tuber_studio.agent import private_call
from tuber_studio.app import create_app


def test_voice_only_even_with_accepted_legacy_profiles_and_avatar_enabled(settings, monkeypatch):
    auth = {"Authorization": "Bearer " + settings.admin_token}
    origin = "https://voice.example"
    monkeypatch.setattr(private_call, "private_service", lambda: (origin, "fixture"))
    (settings.runtime / "private-call-avatars.json").write_text(json.dumps({
        "origin": origin, "character_id": "whale",
        "profiles": {key: {"avatar_id": "portrait_" + ch * 32, "accepted": True}
                     for key, ch in (("q", "a"), ("original", "b"))},
    }))
    issued = []

    async def send(client, request, **kwargs):
        assert request.url.path == "/v1/browser-tickets"
        assert json.loads(request.content) == {"path": "/v1/realtime"}
        issued.append(request.url.path)
        return httpx.Response(200, request=request, json={"ticket": "a" * 32})

    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr(httpx.AsyncClient, "send", send)
        path = "/api/chat/private-call/avatars"
        assert client.get(path).status_code == 401
        for enabled in (False, True):
            client.put("/api/settings/performance", headers=auth, json={"avatar_enabled": enabled}).raise_for_status()
            assert client.get(path, headers=auth).json() == {"profiles": []}
            assert client.post(path, headers=auth, json={"profile": "q"}).status_code == 405
            assert client.post(path + "/legacy/whep", headers=auth, content=b"offer").status_code == 404
            ticket = client.post("/api/chat/private-call/ticket", headers=auth).raise_for_status().json()
            assert ticket["socket"].endswith("/v1/realtime")
            assert ticket["avatar"]["speaking"] is None
        assert len(issued) == 2
