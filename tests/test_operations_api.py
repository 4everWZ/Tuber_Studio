"""Authenticated workspace wiring, independent from real platform writes."""

import os

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app


class ReadOnlyPlatform:
    async def close(self):
        pass


@pytest.mark.skipif(os.getenv("TUBER_TEST_CHROMIUM") != "1", reason="requires a local Chromium browser")
async def test_dynamic_frequency_and_channel_image_permissions_in_browser(settings):
    from playwright.async_api import async_playwright, expect

    from evaluations.creative_browser import server

    app = create_app(settings, ReadOnlyPlatform())
    async with server(app) as origin, async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
        page = await browser.new_page()
        await page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort())
        await page.goto(origin + "/#dynamic-policy-settings")
        limit = page.get_by_label("24 小时最大尝试次数", exact=False)
        await expect(limit).to_be_enabled()
        await limit.fill("24")
        await page.locator("#dynamic-min-post-interval").fill("0")
        await page.get_by_label("允许动态选用图库图片").check()
        await page.get_by_label("允许动态调用生图模型").uncheck()
        async with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/dynamics/policy")) as saved:
            await page.get_by_role("button", name="保存动态设置").click()
        assert (await saved.value).ok
        await page.reload()
        await expect(limit).to_have_value("24")
        await expect(page.locator("#dynamic-min-post-interval")).to_have_value("0")
        await expect(page.get_by_label("允许动态选用图库图片")).to_be_checked()
        await expect(page.get_by_label("允许动态调用生图模型")).not_to_be_checked()
        await page.get_by_role("link", name="私信收取与回复").click()
        await expect(page.get_by_label("允许私信调用生图模型")).to_be_checked()
        await expect(page.get_by_label("允许私信选用图库图片")).not_to_be_checked()
        await expect(page.get_by_label("允许评论调用生图模型")).to_be_checked()
        await expect(page.get_by_label("允许评论选用图库图片")).not_to_be_checked()
        assert not app.state.store.one("SELECT 1 FROM dynamic_posts")
        await browser.close()


def test_dynamic_routes_require_admin_and_overlay_does_not_grant_access(settings, monkeypatch):
    app = create_app(settings, ReadOnlyPlatform())

    async def wait_decision(messages, schema, *, max_tokens):
        return {"action": "wait", "reason": "这次先不发", "content": "", "source_ids": [],
                "next_evaluation_seconds": 1800}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", wait_decision)
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


def test_dynamic_settings_validate_permissions_and_configured_limits(settings):
    app = create_app(settings, ReadOnlyPlatform())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        for data in (
            {"enabled": True},
            {"enabled": "true"},
            {"max_posts_per_day": -1},
            {"min_post_interval_seconds": -1},
            {"memory_summary_max_tokens": 0},
            {"memory_summary_max_tokens": True},
            {"sessdata": "must-not-accept"},
        ):
            assert client.put("/api/dynamics/policy", headers=headers, json=data).status_code == 400
        result = client.put(
            "/api/dynamics/policy",
            headers=headers,
            json={"auto_publish": False, "max_posts_per_day": 24, "min_post_interval_seconds": 60, "memory_summary_max_tokens": 5000},
        )
        assert result.status_code == 200 and result.json()["auto_publish"] is False
        assert client.get("/api/dynamics/policy", headers=headers).json()["max_posts_per_day"] == 24
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
        app.state.runtime.operations.configure(
            {"enabled": True}, account_id="injected-test-account", character=app.state.runtime.character
        )
        response = client.put(
            "/api/settings/providers",
            headers={"Authorization": "Bearer " + settings.admin_token},
            json={},
        )
        assert response.status_code == 409
        assert "停止自主运营" in response.json()["detail"]
        app.state.runtime.operations.configure({"enabled": False})
        assert app.state.store.get("dynamic_policy")["enabled"]
        assert client.put(
            "/api/settings/providers",
            headers={"Authorization": "Bearer " + settings.admin_token},
            json={},
        ).status_code == 200
