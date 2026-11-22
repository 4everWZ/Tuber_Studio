"""Opt-in Chromium acceptance for both the owner Activity UI and its remote reading viewport."""

import sys
from uuid import uuid4

import httpx
import pytest
from playwright.async_api import async_playwright

from tuber_studio.agent import private_browser
from tuber_studio.app import create_app
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn


@pytest.mark.asyncio
async def test_owner_and_agent_share_chromium_reading_state(settings, monkeypatch):
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", "fixture-only")
    app = create_app(settings)
    tasks = app.state.runtime.tasks
    source = "https://public.example/guide"
    next_source = "https://public.example/appendix"
    reads, actions = [], []
    async def public_document(url, *, guard):
        guard()
        reads.append(url)
        if url == next_source:
            return "<h1>Appendix</h1><p>Second approved document.</p>"
        assert url == source
        return """<script>document.body.innerText='untrusted script executed'</script>
            <meta http-equiv="refresh" content="0;url=https://unselected.example/">
            <h1>Field notes</h1><details><summary>Evidence</summary><p>Visible after opening.</p></details>
            <a href="/appendix">Appendix</a><a href="https://unselected.example/">Unapproved</a>
            <form method="post" action="https://unselected.example/"><input type="submit"></form>
            <img src="https://unselected.example/pixel">"""

    monkeypatch.setattr(private_browser, "read_document", public_document)
    async def model(*, tools, **kwargs):
        schema = next(tool for tool in tools if tool["name"] == "private_browser")["parameters"]["properties"]
        identity, = schema["task_id"]["enum"]
        version, = schema["version"]["enum"]
        kind = "read"
        actions.append((kind, version))
        return ResponseTurn(str(uuid4()), (FunctionCall(str(uuid4()), "private_browser",
            {"task_id": identity, "version": version, "kind": kind, "index": 0, "text": ""}),), "", {})
    tasks.responses_client = lambda: model
    origin = "http://127.0.0.1"
    html = (settings.root / "src/tuber_studio/web/pages/chat.html").read_text(encoding="utf-8")
    html += """<script type="module">
      import {initTaskActivity} from '/static/modules/task-activity.js';
      document.querySelector('[data-section=chat]').hidden=false;
      const api=async (path,{method,body}={})=>{
        const response=await fetch(path,{method,headers:{"Content-Type":"application/json","Authorization":"Bearer fixture-only"},
          body:body===undefined?undefined:JSON.stringify(body)});
        const value=await response.json(); if(!response.ok) throw Error(value.detail); return value;
      };
      initTaskActivity(api,()=>crypto.randomUUID());
    </script>"""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client, async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge" if sys.platform == "win32" else "chromium", chromium_sandbox=True)
        context = await browser.new_context(service_workers="block")
        external = []
        async def serve(route):
            url = httpx.URL(route.request.url)
            if url.host != "127.0.0.1":
                external.append(url.host)
                return await route.abort()
            if url.path == "/":
                return await route.fulfill(content_type="text/html; charset=utf-8", body=html)
            if not url.path.startswith(("/static/", "/api/agent/")):
                return await route.abort()
            response = await client.request(route.request.method, str(url), headers=await route.request.all_headers(),
                                            content=route.request.post_data_buffer)
            await route.fulfill(status=response.status_code, content_type=response.headers.get("content-type"),
                                body=response.content)
        await context.route("**/*", serve)
        page = await context.new_page()
        page.set_default_timeout(25000)
        try:
            await page.goto(origin)
            await page.locator("#chat-activity-toggle").click()
            await page.get_by_role("button", name="用浏览器交办").click()
            await page.get_by_label("希望阅读这些来源做什么？").fill("查看证据和附录")
            await page.get_by_label("允许读取的公开来源").fill(source + "\n" + next_source)
            await page.get_by_role("button", name="交办阅读").click()
            await page.get_by_role("button", name="查看阅读浏览器").click()
            task = tasks.activity()[0]["id"]
            for _ in range(2):
                await tasks._advance(tasks.current(task))
            image = page.get_by_alt_text("当前 Chromium 阅读视口")
            await image.wait_for()
            assert await image.evaluate("el => el.decode().then(()=>el.naturalWidth)") == 960
            receipt = tasks.current(task)["steps"][-1]["result"]
            initial_page = tasks.private_browser.state(task)["view"]["page_id"]
            assert "Field notes" in receipt["excerpt"] and "Visible after opening" not in receipt["excerpt"]
            assert all(c["label"] != "Unapproved" for c in receipt["controls"])
            await page.get_by_role("button", name="接管浏览器").click()
            await page.get_by_role("button", name="展开或收起 · Evidence").click()
            await page.wait_for_function("document.querySelector('#chat-activity-content')?.textContent.includes('Visible after opening.')")
            await page.get_by_text("视口文字", exact=True).click()
            await page.get_by_text("Visible after opening.", exact=False).last.wait_for()
            assert tasks.private_browser.state(task)["view"]["page_id"] == initial_page
            await tasks.private_browser.readers.close()
            async with page.expect_response(lambda r: r.url.endswith("/browser/actions") and r.request.method == "POST"):
                await page.get_by_role("button", name="下一屏").click()
            recovered = tasks.private_browser.state(task)["view"]
            assert recovered["page_id"] != initial_page and "Visible after opening." in recovered["text"]
            await page.get_by_role("button", name="打开 · Appendix").click()
            await page.get_by_role("button", name="打开 · Appendix").wait_for(state="detached")
            assert tasks.private_browser.state(task)["view"]["page_id"] == recovered["page_id"]
            await page.get_by_role("button", name="交回角色").click()
            await page.get_by_role("button", name="接管浏览器").wait_for()
            for _ in range(2):
                await tasks._advance(tasks.current(task))
            result = tasks.current(task)["steps"][-1]["result"]
            assert result["url"] == next_source and "Second approved document" in result["excerpt"]
            assert tasks.private_browser.state(task)["view"]["page_id"] == recovered["page_id"]
            async with page.expect_response(lambda r: r.url.endswith("/browser") and r.request.method == "GET"):
                await image.wait_for()
            await page.get_by_role("button", name="终止浏览").click()
            await page.get_by_text("浏览会话已结束。").wait_for()
            assert reads == [source, next_source] and len(actions) == 2 and not external
            assert not tasks.store.all("SELECT * FROM account_writes") and not tasks.store.all("SELECT * FROM task_browser_documents")
        finally:
            await browser.close()
            await tasks.private_browser.close()
            app.state.store.close()
