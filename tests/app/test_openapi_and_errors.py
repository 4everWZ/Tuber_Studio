"""Contract tests for offline OpenAPI export, `/api/v1/*` routes, error envelopes, and Stage 2 APIs."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from tools.ci.export_openapi import main as export_openapi_main
from tuber_studio.app import create_app
from tuber_studio.app.openapi import API_V1_DOMAINS, build_openapi_schema, validate_openapi_contract


def test_openapi_export_offline_and_validates_v1_namespace(tmp_path):
    schema = build_openapi_schema()
    assert validate_openapi_contract(schema) == []

    paths = schema["paths"]
    api_paths = [path for path in paths if path.startswith("/api")]
    assert len(api_paths) >= 190
    for path in api_paths:
        assert path.startswith("/api/v1/")
        domain = path.split("/")[3]
        assert domain in API_V1_DOMAINS

    # Stage 2 dual-pipeline endpoints and ErrorEnvelope schema must be present.
    assert "/api/v1/operations/discovery/settings" in paths and "put" in paths["/api/v1/operations/discovery/settings"]
    assert "/api/v1/operations/inbox/settings" in paths and "put" in paths["/api/v1/operations/inbox/settings"]
    assert "/api/v1/operations/control/quotas" in paths
    assert {"get", "put"} <= set(paths["/api/v1/operations/control/quotas"])
    assert "/api/v1/operations/claims" in paths
    assert "ErrorEnvelope" in (schema.get("components") or {}).get("schemas", {})

    exported = tmp_path / "openapi.json"
    assert export_openapi_main(["--output", str(exported)]) == 0
    assert exported.is_file() and '"openapi": "3.' in exported.read_text(encoding="utf-8")
    assert export_openapi_main(["--check", "--output", str(exported)]) == 0
    assert export_openapi_main(["--check"]) == 0


def test_unified_error_envelopes_and_zero_legacy_api_aliases(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}

        # Untouched non-API routes still respond cleanly.
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").json() == {"status": "ok"}

        # 401 UNAUTHORIZED without credentials
        r401 = client.get("/api/v1/system/status")
        assert r401.status_code == 401
        body401 = r401.json()
        assert set(body401) == {"code", "message", "details"}
        assert body401["code"] == "UNAUTHORIZED" and body401["message"]

        # 403 CROSS_ORIGIN_FORBIDDEN on mismatched Origin
        r403 = client.post(
            "/api/v1/system/auth/login",
            headers={"Origin": "https://attacker.example"},
            json={"token": settings.admin_token},
        )
        assert r403.status_code == 403
        body403 = r403.json()
        assert set(body403) == {"code", "message", "details"}
        assert body403["code"] == "CROSS_ORIGIN_FORBIDDEN"

        # 404 NOT_FOUND on missing resource AND retired legacy /api/* aliases
        for legacy in ("/api/status", "/api/automation", "/api/live/view", "/api/chat/intelligent", "/api/memes"):
            r_legacy = client.get(legacy, headers=headers)
            assert r_legacy.status_code == 404
            assert set(r_legacy.json()) == {"code", "message", "details"}

        r404 = client.get(f"/api/v1/agent/tasks/{uuid4()}", headers=headers)
        assert r404.status_code == 404
        assert set(r404.json()) == {"code", "message", "details"}
        assert r404.json()["code"] == "NOT_FOUND"

        # 409 CONFLICT on deleting active character without replacement
        active_id = client.app.state.runtime.character["id"]
        r409 = client.delete(f"/api/v1/persona/characters/{active_id}", headers=headers)
        assert r409.status_code == 409
        assert set(r409.json()) == {"code", "message", "details"}
        assert r409.json()["code"] == "CONFLICT"

        # 422 VALIDATION_ERROR on invalid Pydantic payload
        r422 = client.put(
            "/api/v1/operations/control/quotas",
            headers=headers,
            json={"direct_quota_reserve": -1},
        )
        assert r422.status_code == 422
        body422 = r422.json()
        assert set(body422) == {"code", "message", "details"}
        assert body422["code"] == "VALIDATION_ERROR"
        assert isinstance(body422["details"], dict) and isinstance(body422["details"].get("errors"), list)

        # 400 BAD_REQUEST on empty ValueError rejection
        r400 = client.put("/api/v1/operations/control/quotas", headers=headers, json={})
        assert r400.status_code == 400
        assert set(r400.json()) == {"code", "message", "details"}
        assert r400.json()["code"] == "BAD_REQUEST"


def test_stage2_dual_pipeline_settings_and_quotas_endpoints(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}

        quotas = client.get("/api/v1/operations/control/quotas", headers=headers).raise_for_status().json()
        assert quotas["direct_quota_reserve"] == 2
        assert quotas["discovery_deferred"] is False

        # Setting 0 explicitly disables reserve & sets hourly/daily limits to unlimited (remaining -> None).
        updated_quotas = (
            client.put(
                "/api/v1/operations/control/quotas",
                headers=headers,
                json={"direct_quota_reserve": 0, "max_per_hour": 0, "max_per_day": 0},
            )
            .raise_for_status()
            .json()
        )
        assert updated_quotas["direct_quota_reserve"] == 0
        assert updated_quotas["remaining_per_hour"] is None
        assert updated_quotas["remaining_per_day"] is None

        # Independent Discovery & Inbox settings endpoints validate and apply cleanly.
        disc = (
            client.put(
                "/api/v1/operations/discovery/settings",
                headers=headers,
                json={"daily_watch_limit": 7},
            )
            .raise_for_status()
            .json()
        )
        assert disc["daily_watch"]["limit"] == 7

        # Discovery settings rejects Inbox-only toggles; Inbox settings rejects Discovery-only toggles.
        assert (
            client.put(
                "/api/v1/operations/discovery/settings",
                headers=headers,
                json={"inbox_enabled": True},
            ).status_code
            == 400
        )
        assert (
            client.put(
                "/api/v1/operations/inbox/settings",
                headers=headers,
                json={"daily_watch_limit": 5},
            ).status_code
            == 400
        )

        inbox_state = (
            client.put(
                "/api/v1/operations/inbox/settings",
                headers=headers,
                json={"notification_interval_seconds": 120, "mentions_enabled": True},
            )
            .raise_for_status()
            .json()
        )
        assert "state" in inbox_state and Path(settings.runtime).is_dir()
