"""End-to-end Microsoft Edge acceptance for Stage 2 dual-pipeline workbench & /dafeiyu/ subpath."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from tuber_studio.app import create_app
from tuber_studio.core.config import ROOT, Settings


class ReadOnlyPlatformAdapter:
    """Read-only Bilibili mock adapter asserting zero platform writes."""

    uid = "100"

    def __init__(self) -> None:
        self.writes: list[tuple[str, tuple, dict]] = []

    async def self_uid(self) -> str:
        return self.uid

    async def account_id(self) -> str:
        return self.uid

    async def discover(self, source: str, keyword: str = "", page: int = 1, limit: int = 20) -> dict:
        return {"items": [], "has_more": False}

    async def video_metadata(self, bvid: str) -> dict:
        return {"bvid": bvid, "title": "示例视频", "owner": {"uid": "200", "name": "示例作者"}}

    async def subtitles(self, bvid: str) -> list[dict]:
        return []

    async def read_public_thread(self, resource: str, root_id: str, **kwargs) -> dict:
        return {"resource": resource, "root_id": root_id, "comments": []}

    async def fetch(self, *args, **kwargs) -> dict:
        return {"items": [], "cursor": None}

    async def notifications(self, kind: str, cursor: str | None = None) -> dict:
        return {"account_id": self.uid, "items": [], "has_more": False, "cursor": None}

    async def publish_top_level(self, *args, **kwargs) -> None:
        self.writes.append(("publish_top_level", args, kwargs))
        raise AssertionError("Platform write attempted during zero-write Edge acceptance")

    async def publish_resource_reply(self, *args, **kwargs) -> None:
        self.writes.append(("publish_resource_reply", args, kwargs))
        raise AssertionError("Platform write attempted during zero-write Edge acceptance")

    async def reply(self, *args, **kwargs) -> None:
        self.writes.append(("reply", args, kwargs))
        raise AssertionError("Platform write attempted during zero-write Edge acceptance")

    async def like_comment(self, *args, **kwargs) -> None:
        self.writes.append(("like_comment", args, kwargs))
        raise AssertionError("Platform write attempted during zero-write Edge acceptance")

    async def like_dynamic(self, *args, **kwargs) -> None:
        self.writes.append(("like_dynamic", args, kwargs))
        raise AssertionError("Platform write attempted during zero-write Edge acceptance")

    async def send_dm(self, *args, **kwargs) -> None:
        self.writes.append(("send_dm", args, kwargs))
        raise AssertionError("Platform write attempted during zero-write Edge acceptance")


def _make_offline_gateway_handler(models: set[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": model_id, "max_model_len": 32768} for model_id in sorted(models)]},
            )
        if path.endswith("/chat/prompt_tokens"):
            return httpx.Response(200, json={"prompt_tokens": 64})
        raise AssertionError(f"Unexpected inference completion during Stage 2 UI acceptance: {path}")

    return handler


async def _attach_offline_inference(app) -> None:
    runtime = app.state.runtime
    models = {
        str(provider.config.get("model") or "").strip()
        for provider in (runtime.llm, runtime.chat_llm, runtime.operation_llm)
        if str(provider.config.get("model") or "").strip()
    }
    models.update({"omni-default", "text-default"})
    transport = httpx.MockTransport(_make_offline_gateway_handler(models))
    for provider in (runtime.llm, runtime.chat_llm, runtime.operation_llm):
        old_client = provider.client
        provider.client = httpx.AsyncClient(
            base_url=old_client.base_url,
            headers=old_client.headers,
            transport=transport,
            trust_env=False,
        )
        await old_client.aclose()


def _seed_stage2_data(app, uid: str) -> None:
    store = app.state.store
    char = app.state.runtime.character
    now = time.time()

    # Ensure account profile exists cleanly without network lookup
    app.state.bilibili_account.credentials.update(sessdata="fixture-sess", csrf="fixture-csrf")
    store.set(
        "bilibili_account_profile",
        {"uid": uid, "name": "测试号主", "verified_at": now},
    )

    # 1) inbox badge item (dm_inbox -> 收件箱消息 inbox)
    store.execute(
        "INSERT INTO dm_inbox(platform,message_id,platform_user_id,display_name,content,sent_at,character_id,received_at,account_id,sequence) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("bilibili", "dm_9001", "200", "来信观众", "晚上好，昨天那期海底厨房很有意思。", now - 120, char["id"], now - 120, uid, 1),
    )

    # 2) discovery badge item (browsing_rounds + browsing_intents -> 主动探索 discovery)
    round_id = str(uuid4())
    store.execute(
        "INSERT INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,epoch,owner,deadline,config,budget,metrics,error,created_at,updated_at,source_pipeline) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            round_id,
            uid,
            char["id"],
            char["version"],
            "completed",
            0,
            1,
            "scheduler",
            now + 60,
            json.dumps({"interaction_only": False}),
            json.dumps({}),
            json.dumps({"processed": 1}),
            None,
            now - 90,
            now - 90,
            "discovery",
        ),
    )
    store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,epoch,auto_authorized,status,text,reason,target,evidence,material_keys,input_version,receipt,visibility,error,created_at,updated_at,source_pipeline) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid4()),
            uid,
            char["id"],
            char["version"],
            "reply:BV1xx411c7mD:901",
            "reply_public",
            round_id,
            1,
            0,
            "pending_review",
            "感谢反馈，这一段资料放在置顶整理啦。",
            "回应观众对视频细节的提问",
            json.dumps(
                {
                    "bvid": "BV1xx411c7mD",
                    "root": "901",
                    "parent": "901",
                    "recipient_name": "探索观众",
                    "source_text": "这个分 P 的细节讲得很清楚！",
                },
                ensure_ascii=False,
            ),
            json.dumps([]),
            json.dumps([]),
            "v1",
            None,
            "visible",
            None,
            now - 80,
            now - 80,
            "discovery",
        ),
    )

    # 3) manual badge + pending comment draft for #comments edit & approve (手动 manual)
    comment_row = store.execute(
        "INSERT INTO comments(platform,video_id,comment_id,thread_id,parent_comment_id,platform_user_id,display_name,content,imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("bilibili", "BV1xx411c7mD", "902", "902", "", "201", "评论观众", "请问片尾提到的实验参考是哪一篇？", now - 60),
    )
    store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,model,input_summary,answer_check,created_at,updated_at,account_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            comment_row.lastrowid,
            "初始草稿正文：片尾备注里写了公开来源链接。",
            "pending_review",
            char["id"],
            char["version"],
            "text-default",
            json.dumps({"video_id": "BV1xx411c7mD"}, ensure_ascii=False),
            json.dumps({}, ensure_ascii=False),
            now - 50,
            now - 50,
            uid,
        ),
    )


async def _start_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=3))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
            raise RuntimeError("Isolated Stage 2 server failed to start")
        await asyncio.sleep(0.05)
    return port, server, serving, listener


async def _launch_edge(pw):
    executable = os.environ.get("CHROMIUM_EXECUTABLE")
    launch_opts: dict = {
        "headless": True,
        "args": ["--disable-background-networking", "--no-proxy-server"],
    }
    if executable and Path(executable).is_file():
        launch_opts["executable_path"] = executable
    else:
        launch_opts["channel"] = "msedge"
    return await pw.chromium.launch(**launch_opts)


async def _run_subpath_check(
    pw,
    runtime_dir: Path,
    platform: ReadOnlyPlatformAdapter,
    token: str,
    screenshot_dir: Path | None,
) -> dict:
    old_prefix = os.environ.get("TUBER_WEB_PREFIX")
    old_token = os.environ.get("TUBER_ADMIN_TOKEN")
    os.environ["TUBER_WEB_PREFIX"] = "/dafeiyu"
    os.environ["TUBER_ADMIN_TOKEN"] = token
    app = None
    listener = None
    serving = None
    server = None
    try:
        settings = Settings(runtime=runtime_dir)
        settings.admin_token = token
        settings.data["features"]["live_enabled"] = False
        app = create_app(settings, adapter=platform)
        await _attach_offline_inference(app)
        _seed_stage2_data(app, platform.uid)
        await app.state.browsing.preflight(for_automation=False)
        port, server, serving, listener = await _start_server(app)
        origin = f"http://127.0.0.1:{port}"

        browser = await _launch_edge(pw)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
            await context.route(
                "**/*",
                lambda route: route.continue_()
                if httpx.URL(route.request.url).host in {"127.0.0.1", "localhost"}
                else route.abort(),
            )
            page = await context.new_page()
            page_errors: list[str] = []
            console_errors: list[str] = []
            http_errors: list[str] = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            logged_in = False
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error" and (logged_in or "401 (Unauthorized)" not in msg.text)
                else None,
            )
            page.on(
                "response",
                lambda res: http_errors.append(f"{res.status} {res.url}")
                if res.status >= 400 and (logged_in or res.status != 401)
                else None,
            )

            subpath_responses: list[tuple[int, str]] = []
            page.on(
                "response",
                lambda res: subpath_responses.append((res.status, httpx.URL(res.url).path)),
            )

            resp = await page.goto(f"{origin}/dafeiyu/", wait_until="networkidle")
            assert resp is not None and resp.status == 200, f"Unexpected /dafeiyu/ status: {resp}"
            html = await page.content()
            assert '<base href="/dafeiyu/">' in html, "Missing WebMount <base href='/dafeiyu/'>"

            await expect(page.locator("#login-form")).to_be_visible()
            await page.locator("#token").fill(token)
            await page.locator("#login-form button[type=submit]").click()
            await expect(page.locator("#workspace-sidebar")).to_be_visible()
            logged_in = True

            nav_pages = await page.locator("#workspace-sidebar nav a[data-page]").evaluate_all(
                "els => els.map(el => el.dataset.page)"
            )
            assert nav_pages == ["chat", "automation", "live", "characters", "settings"], f"Unexpected nav: {nav_pages}"

            # Navigate to #automation via subpath and scroll activity feed into view
            await page.locator('#workspace-sidebar nav a[data-page="automation"]').click()
            await expect(page.locator('[data-section="automation"]')).to_be_visible()
            feed = page.locator("#discovery-activity-list")
            await expect(feed.locator("[data-activity-key]").first).to_be_visible()
            await feed.scroll_into_view_if_needed()

            # Verify /dafeiyu/ bundle scripts actually loaded under /dafeiyu/static/
            assert any(
                status == 200 and path.startswith("/dafeiyu/static/bundle/") and path.endswith(".js")
                for status, path in subpath_responses
            ), f"No /dafeiyu/static/bundle/*.js 200 response observed: {subpath_responses}"
            assert page.url.startswith(f"{origin}/dafeiyu/"), f"URL left subpath: {page.url}"

            if screenshot_dir:
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot_dir / "m5-subpath-dafeiyu.png"), full_page=False)

            assert not page_errors, f"Subpath page errors: {page_errors}"
            assert not console_errors, f"Subpath console errors: {console_errors}"
            assert not http_errors, f"Subpath HTTP >=400 responses: {http_errors}"
            await context.close()
        finally:
            await browser.close()
    finally:
        if server and serving:
            server.should_exit = True
            await serving
        if listener:
            listener.close()
        if app is not None:
            app.state.store.close()
        if old_prefix is None:
            os.environ.pop("TUBER_WEB_PREFIX", None)
        else:
            os.environ["TUBER_WEB_PREFIX"] = old_prefix
        if old_token is None:
            os.environ.pop("TUBER_ADMIN_TOKEN", None)
        else:
            os.environ["TUBER_ADMIN_TOKEN"] = old_token

    return {"subpath_prefix": "/dafeiyu", "http_errors": 0, "console_errors": 0, "page_errors": 0}


async def run_acceptance(screenshot_dir: Path | None = None) -> dict:
    token = f"m5-stage2-{secrets.token_urlsafe(12)}"
    platform = ReadOnlyPlatformAdapter()
    old_token = os.environ.get("TUBER_ADMIN_TOKEN")
    os.environ["TUBER_ADMIN_TOKEN"] = token

    (ROOT / "tmp").mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="m5-stage2-", ignore_cleanup_errors=True) as root_dir,
        tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="m5-subpath-", ignore_cleanup_errors=True) as sub_dir,
    ):
        app = None
        server = None
        serving = None
        listener = None
        try:
            settings = Settings(runtime=Path(root_dir))
            settings.admin_token = token
            settings.data["features"]["live_enabled"] = False
            app = create_app(settings, adapter=platform)
            await _attach_offline_inference(app)
            _seed_stage2_data(app, platform.uid)
            # Review-only channel configuration
            await app.state.browsing.configure(
                {
                    "top_level_enabled": True,
                    "top_level_auto_publish": False,
                    "reply_enabled": True,
                    "reply_auto_publish": False,
                    "mentions_enabled": True,
                    "replies_enabled": True,
                    "mention_reply_enabled": False,
                    "sources": ["recommend"],
                }
            )
            await app.state.browsing.preflight(for_automation=False)
            port, server, serving, listener = await _start_server(app)
            origin = f"http://127.0.0.1:{port}"

            async with async_playwright() as pw:
                browser = await _launch_edge(pw)
                try:
                    context = await browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
                    await context.route(
                        "**/*",
                        lambda route: route.continue_()
                        if httpx.URL(route.request.url).host in {"127.0.0.1", "localhost"}
                        else route.abort(),
                    )
                    page = await context.new_page()
                    page_errors: list[str] = []
                    console_errors: list[str] = []
                    http_errors: list[str] = []
                    page.on("pageerror", lambda err: page_errors.append(str(err)))
                    logged_in = False
                    page.on(
                        "console",
                        lambda msg: console_errors.append(msg.text)
                        if msg.type == "error" and (logged_in or "401 (Unauthorized)" not in msg.text)
                        else None,
                    )
                    page.on(
                        "response",
                        lambda res: http_errors.append(f"{res.status} {res.url}")
                        if res.status >= 400 and (logged_in or res.status != 401)
                        else None,
                    )

                    # 1. Login & 5 primary sidebar workspaces
                    await page.goto(origin + "/", wait_until="networkidle")
                    await expect(page.locator("#login-form")).to_be_visible()
                    await page.locator("#token").fill(token)
                    await page.locator("#login-form button[type=submit]").click()
                    await expect(page.locator("#workspace-sidebar")).to_be_visible()
                    logged_in = True
                    sidebar_pages = await page.locator("#workspace-sidebar nav a[data-page]").evaluate_all(
                        "els => els.map(el => ({page: el.dataset.page, label: el.textContent.trim()}))"
                    )
                    assert [item["page"] for item in sidebar_pages] == [
                        "chat",
                        "automation",
                        "live",
                        "characters",
                        "settings",
                    ], f"Unexpected primary workspaces: {sidebar_pages}"

                    # 2. #automation: independent Inbox & Discovery toggles, direct_quota_reserve, emergency stop, badges
                    await page.locator('#workspace-sidebar nav a[data-page="automation"]').click()
                    await expect(page.locator('[data-section="automation"]')).to_be_visible()
                    inbox_toggle = page.locator("#inbox-pipeline-toggle")
                    discovery_toggle = page.locator("#discovery-pipeline-toggle")
                    await expect(inbox_toggle).to_have_attribute("aria-checked", "false")
                    await expect(discovery_toggle).to_have_attribute("aria-checked", "false")

                    # Toggle Inbox independently (Discovery stays false)
                    async with page.expect_response("**/api/v1/operations/inbox/enable") as resp_inbox:
                        await inbox_toggle.click()
                    inbox_resp = await resp_inbox.value
                    assert inbox_resp.ok, f"inbox/enable failed {inbox_resp.status}: {await inbox_resp.text()}"
                    await expect(inbox_toggle).to_have_attribute("aria-checked", "true")
                    await expect(discovery_toggle).to_have_attribute("aria-checked", "false")

                    # Toggle Discovery independently (Inbox stays true)
                    async with page.expect_response("**/api/v1/operations/discovery/enable") as resp_disc:
                        await discovery_toggle.click()
                    disc_resp = await resp_disc.value
                    assert disc_resp.ok, f"discovery/enable failed {disc_resp.status}: {await disc_resp.text()}"
                    await expect(discovery_toggle).to_have_attribute("aria-checked", "true")
                    await expect(inbox_toggle).to_have_attribute("aria-checked", "true")

                    # Quota reserve: 0 (shared) vs >0 (reserved)
                    reserve_input = page.locator("#direct-quota-reserve-input")
                    save_reserve = page.locator("#save-direct-quota-reserve")
                    await reserve_input.fill("0")
                    async with page.expect_response("**/api/v1/operations/control/quotas") as resp_q0:
                        await save_reserve.click()
                    assert (await resp_q0.value).ok
                    await expect(page.get_by_text("0 · 不预留 / 共享额度", exact=True)).to_be_visible()

                    await reserve_input.fill("3")
                    async with page.expect_response("**/api/v1/operations/control/quotas") as resp_q3:
                        await save_reserve.click()
                    assert (await resp_q3.value).ok
                    await expect(page.get_by_text("已预留 3 条直连额度", exact=True)).to_be_visible()

                    # Verify OperationsActivityFeed displays inbox / discovery / manual badges
                    feed = page.locator("#discovery-activity-list")
                    await expect(feed.locator('[data-source-badge="inbox"]')).to_be_visible()
                    await expect(feed.locator('[data-source-badge="discovery"]')).to_be_visible()
                    await expect(feed.locator('[data-source-badge="manual"]')).to_be_visible()

                    # Emergency Stop -> frozen alert & disabled switches -> Reset Emergency Stop
                    async with page.expect_response("**/api/v1/operations/automation/stop") as resp_stop:
                        await page.locator("#op-dashboard-emergency").click()
                    assert (await resp_stop.value).ok
                    await expect(page.get_by_role("alert")).to_contain_text("全局紧急停止已生效")
                    await expect(inbox_toggle).to_be_disabled()
                    await expect(discovery_toggle).to_be_disabled()
                    await expect(inbox_toggle).to_have_attribute("aria-checked", "false")
                    await expect(discovery_toggle).to_have_attribute("aria-checked", "false")

                    async with page.expect_response("**/api/v1/operations/automation/reset-stop") as resp_reset:
                        await page.locator("#op-dashboard-reset-emergency").click()
                    assert (await resp_reset.value).ok
                    await expect(page.locator('[data-section="automation"] [role="alert"]')).to_have_count(0)
                    await expect(inbox_toggle).to_be_enabled()
                    await expect(discovery_toggle).to_be_enabled()
                    async with page.expect_response("**/api/v1/operations/inbox/enable") as resp_inbox_on:
                        await inbox_toggle.click()
                    assert (await resp_inbox_on.value).ok
                    await expect(inbox_toggle).to_have_attribute("aria-checked", "true")

                    if screenshot_dir:
                        screenshot_dir.mkdir(parents=True, exist_ok=True)
                        await page.evaluate("window.scrollTo(0, 0)")
                        await page.screenshot(path=str(screenshot_dir / "m5-stage2-automation.png"), full_page=False)

                    # 3. #comments: edit draft & save/approve without platform writes
                    await page.locator('[data-operations-tab="comments"]').click()
                    await expect(page.locator('[data-section="comments"]')).to_be_visible()
                    first_row = page.locator("#queue-list [data-queue-key]").first
                    await first_row.click()
                    textarea = page.locator("#queue-detail textarea")
                    await expect(textarea).to_be_visible()
                    revised_text = "已审核编辑正文：片尾备注给出了原始出处链接（M5零写验收）。"
                    await textarea.fill(revised_text)
                    async with page.expect_response("**/api/v1/operations/comments/**") as resp_approve:
                        await page.locator("#queue-detail button.primary").click()
                    assert (await resp_approve.value).ok
                    await expect(page.locator("#queue-detail")).to_contain_text("已审核")
                    await expect(textarea).to_have_value(revised_text)

                    if screenshot_dir:
                        await page.screenshot(path=str(screenshot_dir / "m5-comments-review.png"), full_page=False)

                    # 4. #settings: ProvidersPanel single Omni/vLLM gateway, 0 Ollama
                    await page.locator('#workspace-sidebar nav a[data-page="settings"]').click()
                    await expect(page.locator("#providers-form")).to_be_visible()
                    await page.get_by_text("共用连接与凭据", exact=True).click()
                    await expect(page.locator("#omni-model")).to_be_visible()
                    await expect(page.locator("#omni-base-url")).to_be_visible()
                    await expect(page.locator("#omni-key")).to_be_visible()
                    settings_text = await page.locator('[data-section="settings"]').inner_text()
                    assert "ollama" not in settings_text.lower(), "Unexpected legacy Ollama reference in #settings"

                    if screenshot_dir:
                        await page.screenshot(path=str(screenshot_dir / "m5-settings-providers.png"), full_page=False)

                    assert not page_errors, f"Page errors: {page_errors}"
                    assert not console_errors, f"Console errors: {console_errors}"
                    assert not http_errors, f"HTTP >=400 responses: {http_errors}"
                    await context.close()
                finally:
                    await browser.close()

                # 5. Subpath /dafeiyu/ acceptance in fresh isolated instance
                subpath_result = await _run_subpath_check(pw, Path(sub_dir), platform, token, screenshot_dir)

            write_rows = app.state.store.one("SELECT count(*) AS n FROM account_writes")["n"]
            assert write_rows == 0, f"Expected 0 account_writes, found {write_rows}"
            assert not platform.writes, f"Expected 0 platform writes, found {platform.writes}"

            return {
                "browser": "msedge",
                "workspaces": [item["page"] for item in sidebar_pages],
                "stage2_dual_pipeline": True,
                "quota_reserve_tested": [0, 3],
                "emergency_stop_and_reset": True,
                "activity_badges": ["inbox", "discovery", "manual"],
                "comments_draft_approved_offline": True,
                "ollama_references": 0,
                "platform_writes": 0,
                "account_write_rows": write_rows,
                **subpath_result,
            }
        finally:
            if server and serving:
                server.should_exit = True
                await serving
            if listener:
                listener.close()
            if app is not None:
                app.state.store.close()
            if old_token is None:
                os.environ.pop("TUBER_ADMIN_TOKEN", None)
            else:
                os.environ["TUBER_ADMIN_TOKEN"] = old_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot-dir", type=Path, default=None, help="Optional directory outside Git for PNG captures")
    args = parser.parse_args()
    result = asyncio.run(run_acceptance(args.screenshot_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
