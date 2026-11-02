"""Recovery settings use the actual admin session and no broader policy patch."""

from fastapi.testclient import TestClient

from tuber_studio.app import create_app


def test_recovery_policy_is_authenticated_bounded_and_does_not_enable_sending(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/operations/recovery-policy").status_code == 401
        assert client.post("/api/auth", json={"token": settings.admin_token}).status_code == 200
        before = app.state.runtime.operations.policy()
        assert client.get("/api/operations/recovery-policy").json() == {"max_recoveries": 8}
        assert client.put("/api/operations/recovery-policy", json={"max_recoveries": 64}).json() == {
            "max_recoveries": 64
        }
        for data in (
            {"max_recoveries": 65},
            {"max_recoveries": True},
            {"max_recoveries": 8, "enabled": True},
        ):
            assert client.put("/api/operations/recovery-policy", json=data).status_code == 400
        assert client.get("/api/operations/recovery-policy").json() == {"max_recoveries": 64}
        after = app.state.runtime.operations.policy()
        assert after["enabled"] == before["enabled"] and after["max_per_day"] == before["max_per_day"]
        assert (
            client.post(
                "/api/operations/continuations/comments/999/resume", json={"continuation_id": "other-source"}
            ).status_code
            == 400
        )
        assert not app.state.store.all("SELECT * FROM account_writes")
