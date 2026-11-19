import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.agent import private_avatar as avatar
from tuber_studio.app import create_app


@pytest.mark.parametrize("driver", ["ditto", "inochi2d"])
def test_only_both_accepted_ready_profiles_and_bound_sessions(settings, monkeypatch, driver):
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + settings.admin_token}
    origin, ordinary = "https://avatar.example", "fixture-private-application"
    monkeypatch.setenv("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN", "https://studio.example")
    monkeypatch.setattr(avatar, "private_service", lambda: (origin, ordinary))
    original, q = ("portrait_" + n * 32 for n in "ab")
    if driver == "inochi2d":
        original, q = "test-full-rig.v1", "test_q_rig"
    source, session = "s" * 32, "t" * 32
    profiles = {k: {"avatar_id": v, "accepted": True} for k, v in (("original", original), ("q", q))}
    if driver == "inochi2d":
        for profile in profiles.values():
            profile["driver"] = driver
    config = {"profiles": profiles, "origin": origin, "character_id": "whale"}
    path = settings.runtime / "private-call-avatars.json"
    calls = []
    q_state = "preparing"
    reported_driver = driver

    async def upstream(service, method, route, **kwargs):
        assert service == (origin, ordinary)
        calls.append((method, route))
        if route.startswith("/v1/avatar/avatars/"):
            identity = route.split("/")[-1]
            return httpx.Response(200, json={"state": q_state if identity == q else "ready",
                                             "id": identity, "driver": reported_driver})
        if route == "/v1/avatar/sessions":
            assert kwargs["json"] == {"avatar_id": q, "source": {"type": "realtime", "session_id": source}}
            return httpx.Response(201, json={"id": session, "source_session_id": source, "avatar_id": q,
                "state": "active", "format": {"transport": "whep", "driver": driver}, "generation": 0})
        if route == "/v1/browser-tickets":
            assert kwargs["json"] == {"path": f"/v1/avatar/sessions/{session}/events"}
            return httpx.Response(200, json={"ticket": "e" * 32, "extra": ordinary})
        if method == "POST":
            assert kwargs["content"] == b"offer"
            return httpx.Response(201, text="answer", headers={"Location": route + "/" + "p" * 22,
                                                               "X-Avatar-Timeline-Origin-Ms": "317"})
        return httpx.Response(204)

    monkeypatch.setattr(avatar, "upstream", upstream)
    with TestClient(app) as browser:
        base = avatar.PREFIX
        assert browser.get(base).status_code == 401
        assert browser.get(base, headers=headers).json() == {"profiles": []}
        for changed in ({"origin": "https://other.example"}, {"character_id": "clone"},
                        {"profiles": {"q": profiles["q"]}},
                        {"profiles": profiles | {"q": profiles["q"] | {"accepted": False}}},
                        {"profiles": profiles | {"original": profiles["q"]}},
                        {"profiles": profiles | {"q": profiles["q"] | {"avatar_id": "../sessions"}}}):
            path.write_text(json.dumps(config | changed))
            assert browser.get(base, headers=headers).json() == {"profiles": []}
        assert not calls
        path.write_text(json.dumps(config))
        assert browser.get(base, headers=headers).json() == {"profiles": []}
        q_state = "ready"
        reported_driver = "unexpected"
        assert browser.get(base, headers=headers).json() == {"profiles": []}
        reported_driver = driver
        options = browser.get(base, headers=headers).json()
        assert options["profiles"] == ["original", "q"]
        body = {"source_session_id": source, "profile": "q", "selection": options["selection"]}
        calls.clear()
        assert browser.post(base, headers=headers, json=body | {"guard": "stale_guard_for_this_call"}).status_code == 409
        assert not calls
        assert browser.post(base, headers=headers | {"Origin": "https://other.example"}, json=body).status_code == 403
        assert browser.post(base, headers=headers, json=body | {"profile": q}).status_code == 422
        q_state = "failed"
        assert browser.post(base, headers=headers, json=body).status_code == 409
        q_state = "ready"
        joined = browser.post(base, headers=headers, json=body)
        assert joined.status_code == 200 and ordinary not in joined.text
        assert joined.json()["events"].startswith("wss://studio.example/v1/avatar/sessions/")
        local = base + "/" + joined.json()["handle"]
        sdp = browser.post(local + "/whep", headers=headers | {"Content-Type": "application/sdp"}, content="offer")
        assert sdp.status_code == 201 and sdp.text == "answer"
        assert sdp.headers["Location"] == local + "/whep/" + "p" * 22
        assert sdp.headers["X-Avatar-Timeline-Origin-Ms"] == "317"
        assert browser.delete(sdp.headers["Location"], headers=headers).status_code == 204
        assert browser.delete(local + "bad", headers=headers).status_code == 404
        path.write_text(json.dumps(config | {"profiles": {}}))
        assert browser.get(local, headers=headers).status_code == 409
