"""Actual workbench and platform adapter with an isolated Bilibili transport."""

import asyncio
import json
import os
import socket

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tuber_studio.app import create_app
from tuber_studio.infra.inference.api_providers import create_operation_llm
from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.operations.browsing.comment_timeline import observe_thread

VIDEO = "BV1d8pU6xEZX"
ACCOUNT = "900"
COMMENTS = ["灯光从蓝色变暖色以后，镜头看起来像海底突然有了日出。",
            "也喜欢从蓝到暖那段，切完就像在海底看到天亮。"]


def review_in_browser(origin, token, check_mention):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", args=["--disable-background-networking"])
        try:
            context = browser.new_context()
            def local_only(route):
                if route.request.url.startswith(origin):
                    return route.continue_()
                if route.request.url.rstrip("/") == "https://space.bilibili.com/73":
                    return route.fulfill(body="测试的 B 站空间 UID 73")
                return route.abort()

            context.route("**/*", local_only)
            page = context.new_page()
            page.goto(origin)
            page.locator("#token").fill(token)
            page.locator("#login-form button").click()
            page.evaluate("location.hash = '#dynamics'")
            page.get_by_role("button", name="生成动态草稿").click()
            rows = page.locator("#dynamic-posts tr")
            expect(rows.locator("button")).to_have_count(1, timeout=180000)
            rows.locator("button").click()
            expect(page.locator("#approve-dynamic")).to_be_enabled()
            page.get_by_text("素材来源原文", exact=True).click()
            source = page.locator("#dynamic-post-sources details")
            for detail in source.all():
                detail.locator("summary").click()
            for original in COMMENTS:
                expect(page.locator("#dynamic-post-sources").get_by_text(original, exact=True)).to_be_visible()
            posted = page.locator("#dynamic-post-text").input_value()
            if check_mention:
                mention = page.get_by_role("link", name="@珊瑚")
                expect(mention).to_have_attribute("href", "https://space.bilibili.com/73")
                with page.expect_popup() as destination:
                    mention.click()
                expect(destination.value.locator("body")).to_contain_text("UID 73")
                destination.value.close()
            page.get_by_role("button", name="审核通过").click()
            with page.expect_response(lambda response: response.url.endswith("/publish")) as submitted:
                page.locator("#publish-dynamic").click()
            assert submitted.value.ok and submitted.value.json()["status"] == "published"
            return posted
        finally:
            browser.close()


async def test_merge_sources_review_then_submit_once(settings, monkeypatch):
    token = "isolated-dynamic-browser"
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", token)
    settings.admin_token = token
    settings.data["features"]["live_enabled"] = False
    calls, requests = [], []

    def platform(request):
        path = request.url.path
        if path.endswith("/upload_bfs"):
            data = {"image_url": "https://i0.hdslb.com/bfs/fixture.png", "image_width": 960, "image_height": 560, "img_size": 1}
        elif request.method == "POST":
            assert path.endswith("/create/dyn")
            requests.append(json.loads(request.content)["dyn_req"])
            data = {"dyn_id_str": "99041"}
        elif path.endswith("/nav"):
            data = {"isLogin": True, "mid": int(ACCOUNT), "uname": "测试号主", "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
            }}
        elif path.endswith("/myinfo"):
            data = {"mid": int(ACCOUNT), "name": "测试号主"}
        elif path.endswith("/relation/stat"):
            data = {"follower": 0}
        elif path.endswith("/card"):
            assert request.url.params["mid"] == "73"
            data = {"card": {"mid": "73", "name": "珊瑚"}}
        elif path.endswith("/detail"):
            written = requests[0]
            contents = written["content"]["contents"]
            images = [{"src": picture["img_src"], "width": picture["img_width"], "height": picture["img_height"]}
                      for picture in written.get("pics", [])]
            data = {"item": {"id_str": "99041", "type": "DYNAMIC_TYPE_DRAW" if images else "DYNAMIC_TYPE_WORD", "modules": {
                "module_author": {"mid": ACCOUNT}, "module_dynamic": {"desc": {
                    "text": "".join(node["raw_text"] for node in contents), "rich_text_nodes": [
                        {"type": "RICH_TEXT_NODE_TYPE_AT" if node["type"] == 2 else "RICH_TEXT_NODE_TYPE_TEXT",
                         "text": node["raw_text"], "rid": node["biz_id"]} for node in contents
                    ],
                }, "major": {"draw": {"items": images}}},
            }}}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"code": 0, "data": data})

    credentials = {"sessdata": "fixture", "csrf": "fixture"}
    adapter = BrowsingAdapter(credentials)
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(base_url="https://api.bilibili.com", transport=httpx.MockTransport(platform))
    if gateway := os.environ.get("DYNAMIC_ACCEPTANCE_GATEWAY"):
        settings.data["omni"].update(
            base_url=gateway + "/v1", context_source="model_card",
            prompt_token_endpoint=True,
        )
        settings.provider_keys = {"omni": os.environ["DYNAMIC_ACCEPTANCE_TOKEN"]}
    app = create_app(settings, adapter=adapter)
    state = app.state
    state.bilibili_account.credentials.update(credentials)
    assert await state.bilibili_account.account_id() == ACCOUNT
    character = state.runtime.character
    await state.runtime.refresh_identity(character, ACCOUNT)
    await state.dynamics.set_policy({"auto_publish": False})
    observe_thread(state.store, {"id": "isolated", "account_id": ACCOUNT, "character_id": character["id"]}, VIDEO, [
        {"comment_id": str(41 + index), "user_id": str(71 + index), "display_name": f"观众{index + 1}", "text": body}
        for index, body in enumerate(COMMENTS)
    ], via="comment_visit")

    if gateway:
        state.runtime.operation_llm = create_operation_llm(settings)
    model = state.runtime.operation_llm
    real_generate = getattr(model, "generate_json", None)

    async def generate(messages, schema, *, max_tokens):
        prompt = json.loads(messages[1]["content"])
        sources = prompt["public_sources"]
        assert {item["text"] for item in sources} == set(COMMENTS)
        assert not requests
        calls.append(prompt)
        if gateway:
            return await real_generate(messages, schema, max_tokens=max_tokens)
        source_ids = [item["id"] for item in sources]
        return {"action": "post", "reason": "大家都喜欢同一段变光", "source_ids": source_ids,
                "points": [{"text": "蓝转暖像海底的日出", "source_ids": source_ids}],
                "content": "两个人把那段灯光都看成了海底日出。@珊瑚 下次一起看这段！",
                "mentions": [{"label": "@珊瑚", "account": "73"}],
                "next_evaluation_seconds": 1800}

    monkeypatch.setattr(model, "generate_json", generate, raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if running.done():
                    await running
                await asyncio.sleep(0.02)
            text = await asyncio.to_thread(review_in_browser, f"http://127.0.0.1:{listener.getsockname()[1]}", token, not gateway)
            assert len(calls) == 1 and len(requests) == 1
            assert "".join(node["raw_text"] for node in requests[0]["content"]["contents"]).startswith(text)
            post, = state.dynamics.list_posts("published")
            assert len(post["sources"]) == 2 and post["presentation"]["points"]
            if not gateway:
                assert post["receipt"]["mention_uids"] == ["73"]
            assert (await state.dynamics.publish(post["id"], expected_text=post["content"]))["status"] == "published"
            assert len(requests) == 1
        finally:
            server.should_exit = True
            await running
