"""Actual ASGI authorization, persistence and evidence workflow; no live platform writes."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from test_browsing import ControlledModel, ControlledPlatform

from tuber_studio.app import create_app


class ControlledMessages:
    async def sessions(self):
        return "100", []

    async def history(self, uid):
        return []

    async def send(self, uid, text, **kwargs):
        raise AssertionError("These API boundary tests must not submit private messages")

    async def close(self):
        pass


def make_app(settings):
    # Only transport/model inference is controlled. Routes, authorization,
    # SQLite, workers, evidence storage and cancellation are production code.
    settings.data["llm"]["kind"] = "demo"
    app = create_app(settings, ControlledPlatform(), message_adapter=ControlledMessages())
    app.state.runtime.llm = ControlledModel()
    app.state.runtime.context.llm = app.state.runtime.llm
    settings.data["llm"]["kind"] = "llama_cpp"
    app.state.comments.credentials = {"sessdata": "controlled-session", "csrf": "controlled-csrf"}
    app.state.browsing.read_interval = 0
    return app


@pytest.fixture
def client(settings):
    with TestClient(make_app(settings)) as value:
        yield value


@pytest.fixture
def admin(client, settings):
    assert client.post("/api/auth", json={"token": settings.admin_token}).status_code == 200
    return client


READS = ["", "/tasks", "/rounds", "/intents", "/inbox"]
WRITES = [
    ("PUT", "/settings", {}),
    ("POST", "/preflight", {}),
    ("POST", "/enable", {"enabled": False}),
    ("POST", "/round", {"preview": True}),
    ("POST", "/stop", {"emergency": True}),
    ("POST", "/reset-stop", {}),
    ("POST", "/intents/missing/review", {"action": "approve"}),
    ("POST", "/intents/missing/publish", {}),
    ("POST", "/tasks/missing/reread", {}),
]


def test_browsing_close_failure_still_closes_runtime_and_store(settings):
    import sqlite3

    app = make_app(settings)
    original = app.state.browsing.close

    async def fail_after_close():
        await original()
        raise ValueError("browsing cleanup failed")

    app.state.browsing.close = fail_after_close
    with pytest.raises(ValueError, match="browsing cleanup failed"), TestClient(app):
        pass
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.one("SELECT 1")


def test_application_attaches_owned_visual_provider_and_keeps_it_disabled(settings):
    from tuber_studio.video_vision import ApiVideoEvidenceProvider

    owned = []

    def factory(runtime):
        provider = ApiVideoEvidenceProvider(runtime)
        owned.append(provider)
        return provider

    app = create_app(
        settings,
        ControlledPlatform(),
        message_adapter=ControlledMessages(),
        video_evidence_factory=factory,
    )
    assert owned[0].runtime is app.state.runtime
    with TestClient(app) as client:
        assert client.post("/api/auth", json={"token": settings.admin_token}).status_code == 200
        response = client.get("/api/automation").json()
        assert not response["vision"]["available"] and not response["vision"]["admitted"]
        assert app.state.browsing.evidence.visual is owned[0]
        assert not app.state.comments.adapter.sent
    assert owned[0].api is None and not owned[0]._active


@pytest.mark.parametrize(("method", "suffix", "body"), [("GET", suffix, None) for suffix in READS] + WRITES)
def test_every_automation_api_requires_admin_not_overlay(client, settings, method, suffix, body):
    assert client.post("/api/overlay/auth", json={"token": settings.overlay_token}).status_code == 200
    response = client.request(method, "/api/automation" + suffix, json=body)
    assert response.status_code == 401
    assert client.app.state.comments.adapter.sent == []
    assert client.app.state.browsing.rounds() == []


def test_all_automation_writes_enforce_same_origin(admin):
    for method, suffix, body in WRITES:
        response = admin.request(
            method, "/api/automation" + suffix, json=body, headers={"Origin": "https://foreign.example"}
        )
        assert response.status_code == 403, suffix
    assert (
        admin.post(
            "/api/automation/enable", json={"enabled": False}, headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )
    assert not admin.get("/api/automation").json()["policy"]["enabled"]
    assert admin.app.state.comments.adapter.sent == []


def test_default_disabled_visual_unavailable_and_semantics_still_require_review(admin):
    status = admin.get("/api/automation").json()
    assert status["policy"]["enabled"] is False
    assert status["policy"]["emergency"] is False
    assert status["settings"]["visual_enabled"] is False
    assert status["vision"]["available"] is False
    assert status["vision"]["reason"]
    assert status["automatic_sending_ready"] is False
    assert "审核" in status["sending_note"]
    assert not status["effective"]["dynamics"].get("enabled", False)
    assert status["active_round"] is None
    assert status["blockers"]


@pytest.mark.parametrize("patch", [{"enabled": False, "automatic": True}, {"enabled": "true"}, {}])
def test_enable_rejects_unknown_fields_and_non_boolean_authority(admin, patch):
    assert admin.post("/api/automation/enable", json=patch).status_code == 400
    assert not admin.get("/api/automation").json()["policy"]["enabled"]


def test_unknown_settings_cannot_install_an_unsafe_policy(admin):
    settings = admin.get("/api/automation").json()["settings"]
    response = admin.put("/api/automation/settings", json=settings | {"send_without_review": True})
    assert response.status_code == 400
    assert admin.get("/api/automation").json()["settings"] == settings


@pytest.mark.parametrize(
    "body",
    [
        {"preview": False},
        {"preview": "true"},
        {"preview": True, "automatic": True},
        {"preview": True, "auto_authorized": True},
    ],
)
def test_manual_round_body_cannot_escalate_to_automatic_authority(admin, body):
    assert admin.post("/api/automation/round", json=body).status_code == 400
    assert admin.get("/api/automation/rounds").json() == []
    assert admin.get("/api/automation/intents").json() == []
    assert admin.app.state.comments.adapter.sent == []


def wait_round(client, identity):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rows = client.get("/api/automation/rounds").json()
        row = next(item for item in rows if item["id"] == identity)
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(0.01)
    pytest.fail("The controlled bounded round did not reach a terminal API state")


def test_real_api_preview_evidence_and_approval_do_not_implicitly_submit(admin):
    config = admin.get("/api/automation").json()["settings"]
    assert admin.put("/api/automation/settings", json=config | {"top_level_enabled": True}).status_code == 200
    preflight = admin.post("/api/automation/preflight").json()
    assert preflight["account_id"] == "100" and preflight["blockers"] == []
    response = admin.post("/api/automation/round", json={"preview": True})
    assert response.status_code == 200
    assert wait_round(admin, response.json()["id"])["status"] == "completed"
    task = admin.get("/api/automation/tasks").json()[0]
    assert task["subtitle_tracks"][0]["is_ai"] is True
    assert task["subtitle_tracks"][0]["cid"] == "11"
    assert task["evidence"][0]["id"] == "11:0"
    intent = admin.get("/api/automation/intents").json()[0]
    assert intent["status"] == "pending_review" and not intent["auto_authorized"]
    assert admin.post(f"/api/automation/intents/{intent['id']}/publish").status_code == 400
    assert (
        admin.post(
            f"/api/automation/intents/{intent['id']}/review",
            json={"action": "approve", "automatic": True},
        ).status_code
        == 400
    )
    response = admin.post(
        f"/api/automation/intents/{intent['id']}/review", json={"action": "approve", "text": intent["text"]}
    )
    assert response.status_code == 200 and response.json()["status"] == "approved"
    assert admin.app.state.comments.adapter.sent == []
    assert admin.get("/api/automation").json()["automatic_sending_ready"] is False


def test_public_inbox_only_links_to_private_area_without_private_body(admin):
    assert admin.post("/api/automation/preflight").json()["account_id"] == "100"
    store = admin.app.state.store
    now = time.time()
    store.execute(
        "INSERT INTO browsing_events(id,account_id,kind,source_key,timestamp,status,payload,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "public-event",
            "100",
            "mention",
            "source-1",
            now,
            "historical",
            json.dumps({"source_text": "公开问候"}),
            None,
            now,
        ),
    )
    store.execute(
        "INSERT INTO dm_inbox(platform,message_id,platform_user_id,display_name,content,sent_at,character_id,received_at,account_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("bilibili", "private-message", "300", "用户", "PRIVATE_INBOX_SENTINEL", now, "whale", now, "100"),
    )
    inbox = admin.get("/api/automation/inbox").json()
    assert inbox["items"][0]["kind"] == "mention"
    assert inbox["private_count"] == 1 and inbox["private_href"] == "#messages"
    assert "PRIVATE_INBOX_SENTINEL" not in json.dumps(inbox)
    assert "content" not in inbox


def test_overlay_restore_uses_latest_base_settings_not_an_old_snapshot(admin):
    assert (
        admin.put(
            "/api/comments/policy",
            json={"auto_browse": False, "auto_reply": False, "max_replies_per_hour": 2},
        ).status_code
        == 200
    )
    assert admin.post("/api/automation/enable", json={"enabled": True}).status_code == 200
    during = admin.get("/api/comments/policy").json()
    assert during["auto_reply"] is True and during["base"]["auto_reply"] is False
    assert during["automation_override"] is True
    assert admin.put("/api/comments/policy", json={"max_replies_per_hour": 1}).status_code == 200
    message_base = admin.get("/api/messages").json()["policy"]["base"]
    assert admin.put("/api/messages/policy", json=message_base | {"max_per_hour": 3}).status_code == 200
    assert admin.post("/api/automation/enable", json={"enabled": False}).status_code == 200
    status = admin.get("/api/automation").json()
    assert status["base"]["comments"]["max_replies_per_hour"] == 1
    assert status["effective"]["comments"]["auto_reply"] is False
    assert status["base"]["messages"]["max_per_hour"] == 3
    assert status["effective"]["messages"]["auto_send"] is False
    assert admin.app.state.comments.adapter.sent == []


def test_emergency_stop_survives_app_recreation_and_blocks_base_automatic_flags(settings):
    with TestClient(make_app(settings)) as first:
        first.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        first.put("/api/comments/policy", json={"auto_reply": True}).raise_for_status()
        assert first.post("/api/automation/stop", json={"emergency": True}).status_code == 200
        policy = first.get("/api/comments/policy").json()
        assert policy["base"]["auto_reply"] is True and policy["auto_reply"] is False
    with TestClient(make_app(settings)) as second:
        second.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        status = second.get("/api/automation").json()
        assert status["policy"]["emergency"] is True and status["policy"]["enabled"] is False
        assert status["base"]["comments"]["auto_reply"] is True
        assert status["effective"]["comments"]["auto_reply"] is False
        assert status["effective"]["messages"]["auto_send"] is False
        assert second.post("/api/automation/enable", json={"enabled": True}).status_code == 400
        assert second.get("/api/automation/rounds").json() == []
        assert second.post("/api/automation/reset-stop").status_code == 200
        restored = second.get("/api/automation").json()
        assert restored["policy"]["emergency"] is False and restored["policy"]["enabled"] is False
        assert restored["effective"]["comments"]["auto_reply"] is True
        assert second.app.state.comments.adapter.sent == []
