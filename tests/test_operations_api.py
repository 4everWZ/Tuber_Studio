"""Authenticated workspace wiring, independent from real platform writes."""

from fastapi.testclient import TestClient

from tuber_studio.app import create_app


class ReadOnlyPlatform:
    async def close(self):
        pass


def test_dynamic_routes_require_admin_and_overlay_does_not_grant_access(settings):
    app = create_app(settings, ReadOnlyPlatform())
    with TestClient(app) as client:
        for path in ("policy", "posts", "decisions", "memories", "memory-summary"):
            assert client.get(f"/api/dynamics/{path}").status_code == 401
        assert client.post("/api/dynamics/evaluate").status_code == 401
        assert client.put("/api/dynamics/policy", json={"enabled": True}).status_code == 401
        client.post("/api/overlay/auth", json={"token": settings.overlay_token})
        assert client.post("/api/dynamics/posts/1/publish").status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        assert client.get("/api/dynamics/policy").json()["enabled"] is False
        assert client.get("/api/dynamics/policy").json()["auto_publish"] is True
        summary = client.get("/api/dynamics/memory-summary").json()
        assert summary["max_tokens"] == 2048
        assert summary["valid"] is False and summary["entries"] == []
        assert client.get("/api/dynamics/posts").json() == []
        assert client.get("/api/dynamics/decisions").json() == []
        assert client.get("/api/dynamics/memories").json() == []
        assert client.get("/api/status").json()["dynamic_policy"]["enabled"] is False
        result = client.post("/api/dynamics/evaluate").json()
        assert result["action"] == "wait" and result["post_id"] is None
        assert client.get("/api/dynamics/decisions").json()[0]["reason"] == result["reason"]
        assert (
            client.post("/api/dynamics/evaluate", headers={"Origin": "https://other.example"}).status_code
            == 403
        )


def test_dynamic_settings_cannot_enable_without_credentials_or_weaken_limits(settings):
    app = create_app(settings, ReadOnlyPlatform())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        for data in (
            {"enabled": True},
            {"enabled": "true"},
            {"max_posts_per_day": 3},
            {"min_post_interval_seconds": 60},
            {"memory_summary_max_tokens": 255},
            {"memory_summary_max_tokens": 4097},
            {"sessdata": "must-not-accept"},
        ):
            assert client.put("/api/dynamics/policy", headers=headers, json=data).status_code == 400
        result = client.put(
            "/api/dynamics/policy",
            headers=headers,
            json={"auto_publish": False, "max_posts_per_day": 1},
        )
        assert result.status_code == 200 and result.json()["auto_publish"] is False
        assert client.get("/api/dynamics/posts?limit=201", headers=headers).status_code == 422
        assert (
            client.put("/api/dynamics/memories/1", headers=headers, json={"public": "yes"}).status_code == 400
        )
        assert (
            client.post(
                "/api/dynamics/posts/1/review",
                headers=headers,
                json={"action": "approve", "source_ids": ["fake"]},
            ).status_code
            == 400
        )
        assert client.post("/api/dynamics/posts/1/publish", headers=headers).status_code == 400


def test_provider_reconfiguration_requires_stopping_autonomous_operations(settings):
    app = create_app(settings, ReadOnlyPlatform())
    # Seed the persisted policy directly; no background platform job is started.
    app.state.store.set("dynamic_policy", {"enabled": True, "auto_publish": False})
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/providers",
            headers={"Authorization": "Bearer " + settings.admin_token},
            json={},
        )
        assert response.status_code == 409
        assert "停止动态运营" in response.json()["detail"]
