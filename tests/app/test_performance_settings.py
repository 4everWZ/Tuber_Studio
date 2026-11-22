from fastapi.testclient import TestClient

from tuber_studio.app import create_app


def test_shared_preferences_survive_restart_and_drive_single_live_fallback(settings):
    path = "/api/v1/system/settings/performance"
    auth = {"Authorization": "Bearer " + settings.admin_token}
    with TestClient(create_app(settings)) as client:
        assert client.get(path).status_code == 401
        assert client.put(path, json={"avatar_enabled": True}).status_code == 401
        assert client.get(path, headers=auth).json() == {"pets_enabled": True, "avatar_enabled": False}
        for body in ({}, {"typo": True}, {"pets_enabled": None}, {"avatar_enabled": "false"}):
            assert client.put(path, headers=auth, json=body).status_code in (400, 422)
        for pets, avatar in ((True, False), (True, True), (False, True), (False, False)):
            for key, value in (("pets_enabled", pets), ("avatar_enabled", avatar)):
                client.put(path, headers=auth, json={key: value}).raise_for_status()
            expected = {"pets_enabled": pets, "avatar_enabled": avatar}
            for view in ("/api/v1/live/view", "/api/v1/live/studio"):
                result = client.get(view, headers=auth).raise_for_status().json()
                assert result["performance"] == expected
                assert result["lip_sync"] == {"provider": "ditto", "status": "tbd" if avatar else "off",
                                              "renderer": "pet" if pets else "none"}
                assert bool(result["assets"]) == pets
                assert result["live2d"] is None
        client.put(path, headers=auth, json={"avatar_enabled": True}).raise_for_status()
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(path, headers=auth).json() == {"pets_enabled": False, "avatar_enabled": True}
