"""Exercise image review and real adapter forms through the normal operations pages."""

import asyncio
import hashlib
import io
import json
import re
import socket
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from PIL import Image
from tuber_studio import operation_images, operation_attachments
from tuber_studio.artwork_references import Reference
from playwright.sync_api import sync_playwright, expect

from tuber_studio.app import create_app
from tuber_studio.platforms.bilibili_browsing import BrowsingAdapter
from tuber_studio.platforms.bilibili_messages import BilibiliMessagesAdapter


BV = "BV1d8pU6xEZX"
ORIGINAL = "把视频里这段灯光变化也画出来吧。"
BODY = "灯光转蓝的那一刻我也看见了。"
OWNER, RECIPIENT, AUTHOR = "900", "123", "124"


class PublicFixture(BrowsingAdapter):
    async def read_video_thread(self, target):
        assert (target["bvid"], target["root"], target["parent"]) == (BV, "11", "13")
        return {"source": {"text": ORIGINAL, "author_uid": AUTHOR}, "items": []}

    async def self_uid(self):
        return OWNER


def browse(origin, token):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="msedge", chromium_sandbox=True, args=["--disable-background-networking"]
        )
        try:
            page = browser.new_page()
            page.set_default_timeout(12000)
            page.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if urlsplit(route.request.url).hostname == "127.0.0.1"
                    else route.abort()
                ),
            )
            page.goto(origin)
            page.locator("#token").fill(token)
            page.locator("#login-form button").click()
            page.locator('a[data-page="automation"]').click()
            page.locator('a[data-operations-tab="comments"]').click()
            page.locator("#queue-list a.queue-row").first.click()
            panel = page.locator("#queue-detail")
            expect(panel).to_contain_text(AUTHOR)
            expect(panel.locator("textarea")).to_have_value(BODY)
            panel.get_by_role("button", name="选择配图").click()
            page.locator("dialog[open] [data-picker-grid] button").first.click()
            expect(panel.locator("img")).to_be_visible()
            panel.get_by_role("button", name="保存", exact=True).click()
            panel.get_by_role("button", name="发布", exact=True).click()
            page.wait_for_function(
                "!document.querySelector('#queue-detail textarea') || document.querySelector('#queue-detail textarea').readOnly"
            )
            page.locator('a[data-operations-tab="messages"]').click()
            page.locator("[data-drafts] button").first.click()
            expect(page.locator("[data-source]")).to_contain_text(RECIPIENT)
            page.locator("[data-attach-image]").click()
            page.locator("dialog[open] [data-picker-grid] button").first.click()
            expect(page.locator("[data-meme-image]")).to_be_visible()
            page.locator('[data-review="approve"]').click()
            with page.expect_response(
                lambda r: r.url.endswith("/send") and r.request.method == "POST"
            ) as send:
                page.locator("button[data-send]").click()
            assert send.value.ok, send.value.text()
            assert send.value.json()["status"] == "published", send.value.json()
            page.wait_for_function(
                "document.querySelector('button[data-send]').hidden || document.querySelector('[data-drafts]').innerText.includes('暂无待处理')"
            )
            page.locator('a[data-operations-tab="dynamics"]').click()
            page.locator("#dynamic-posts button").first.click()
            expect(page.locator("#dynamic-post-text")).to_have_value(BODY)
            expect(page.locator("#dynamic-meme-image img")).to_be_visible()
            preview = page.locator("#dynamic-meme-image img")
            expect(preview).to_have_attribute("src", re.compile("/api/operations/illustrations/dynamics/"))
            assert preview.evaluate("(img) => img.complete && img.naturalWidth === 1024")
            page.locator("#approve-dynamic").click()
            page.locator("#publish-dynamic").click()
            page.wait_for_function(
                "document.querySelector('#dynamic-post-receipt').innerText.includes('99001') || !document.querySelector('#dynamic-posts button')"
            )
        finally:
            browser.close()


async def test_operations_images_browser(settings, monkeypatch):
    token = "isolated-browser-fixture"
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", token)
    monkeypatch.delenv("OMNI_GATEWAY_URL", raising=False)
    settings.admin_token = token
    settings.data["features"]["live_enabled"] = False
    connect = socket.socket.connect

    def loopback(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("External network disabled in browser fixture")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback)
    posts = []

    def respond(request):
        path = request.url.path
        if request.method == "POST":
            posts.append(path)
        if path.endswith("/nav"):
            data = {
                "isLogin": True,
                "mid": int(OWNER),
                "uname": "测试号主",
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                },
            }
        elif path.endswith("/myinfo"):
            data = {"mid": int(OWNER), "name": "测试号主"}
        elif path.endswith("/relation/stat"):
            data = {"follower": 0}
        elif path.endswith("/view"):
            data = {"aid": 12, "bvid": BV}
        elif path.endswith("/fetch_session_msgs"):
            data = {"messages": []}
        elif path.endswith("/upload_bfs"):
            assert b'name="file_up"' in request.content
            if posts.count(path) == 3:
                assert png in request.content
            data = {
                "image_url": "https://i0.hdslb.com/bfs/fixture.png",
                "image_width": 64,
                "image_height": 64,
                "img_size": 1,
            }
        elif path.endswith("/reply/add"):
            fields = parse_qs(request.content.decode())
            assert fields["oid"] == ["12"] and fields["root"] == ["11"] and fields["parent"] == ["13"]
            assert fields["message"] == [BODY] and json.loads(fields["pictures"][0])[0]["img_width"] == 64
            data = {"rpid": 1001}
        elif path.endswith("/send_msg"):
            fields = parse_qs(request.content.decode())
            assert fields["msg[sender_uid]"] == [OWNER] and fields["msg[receiver_id]"] == [RECIPIENT]
            message = json.loads(fields["msg[content]"][0])
            if fields["msg[msg_type]"] == ["2"]:
                assert message["width"] == 64
                data = {"msg_key": "2001"}
            else:
                assert fields["msg[msg_type]"] == ["1"] and message["content"] == BODY
                data = {"msg_key": "2002"}
        elif path.endswith("/create/dyn"):
            body = json.loads(request.content)["dyn_req"]
            assert body["scene"] == 2 and body["pics"][0]["img_width"] == 64
            assert body["content"]["contents"][0]["raw_text"] == BODY
            data = {"dyn_id_str": "99001"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"code": 0, "data": data})

    credentials = {"sessdata": "fixture", "csrf": "fixture"}
    public, dm = PublicFixture(credentials), BilibiliMessagesAdapter(credentials)
    for adapter in (public, dm):
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(
            base_url="https://api.bilibili.com", transport=httpx.MockTransport(respond)
        )
    app = create_app(settings, adapter=public, message_adapter=dm)
    state = app.state
    store, char = state.store, state.runtime.character
    state.comments.credentials.update(credentials)
    assert await state.comments.account_id() == OWNER
    await state.runtime.refresh_identity(char, OWNER)
    audit = json.dumps({"generation_identity": state.runtime.identity_snapshot(char, OWNER)})
    now = time.time()
    source = store.execute(
        "INSERT INTO comments(video_id,comment_id,thread_id,platform_user_id,content) VALUES(?,?,?,?,?)",
        (BV, "13", "11", AUTHOR, ORIGINAL),
    ).lastrowid
    store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,answer_check,account_id,created_at,updated_at) "
        "VALUES(?,?,'pending_review',?,?,?,?,?,?)",
        (source, BODY, char["id"], char["version"], audit, OWNER, now, now),
    )
    await state.messages.configure({"enabled": True, "auto_poll": False, "auto_send": False})
    inbox, _ = state.messages.ingest(
        {
            "message_id": "450",
            "platform_user_id": RECIPIENT,
            "account_id": OWNER,
            "text": "发给我刚说的那张图。",
            "timestamp": now,
        }
    )
    store.execute(
        "INSERT INTO dm_drafts(platform,platform_user_id,source_kind,source_id,character_id,character_version,"
        "status,text,answer_check,account_id,created_at,updated_at) VALUES('bilibili',?,'message',?,?,?,"
        "'pending_review',?,?,?,?,?)",
        (RECIPIENT, inbox, char["id"], char["version"], BODY, audit, OWNER, now, now),
    )
    material = {
        "metadata": {"bvid": BV, "title": "灯光"},
        "parts": [{"cid": "12", "segments": [{"start": 1, "text": "灯光变蓝"}]}],
    }
    store.execute(
        "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state) "
        "VALUES(?,?,?,?,'[]',?,'complete')",
        (OWNER, char["id"], BV, json.dumps(material), now),
    )
    raster = io.BytesIO()
    Image.new("RGB", (1024, 1024), "navy").save(raster, "PNG")
    png = raster.getvalue()
    outbound = []

    async def choose(settings, material):
        prompt = json.loads(material)
        assert prompt["channel"] == "dynamic" and prompt["draft"] == BODY
        assert prompt["context"][0]["id"] == "video:" + BV
        return operation_images.ImageBrief(variant="q", prompt="A blue light")

    def image_service(request):
        outbound.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "b" * 32})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(200, json={"state": "completed", "seed": 123})

    monkeypatch.setattr(operation_attachments, "gateway_base", lambda _: "http://fixture/v1")
    monkeypatch.setattr(operation_images, "gateway_base", operation_attachments.gateway_base)
    monkeypatch.setattr(operation_images, "choose", choose)
    monkeypatch.setattr(
        operation_images,
        "resolve_reference",
        lambda *args, **kwargs: Reference(png, hashlib.sha256(png).hexdigest(), "upload"),
    )
    monkeypatch.setattr(
        operation_images,
        "gateway",
        lambda _: httpx.AsyncClient(
            base_url="http://fixture/v1/", transport=httpx.MockTransport(image_service)
        ),
    )
    sources = state.dynamics._sources()
    state.dynamics._save_decision(
        char,
        False,
        {
            "action": "post",
            "reason": "视频中的灯光",
            "source_ids": ["video:" + BV],
            "content": BODY,
            "next_evaluation_seconds": 1800,
        },
        sources,
        account_id=OWNER,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if running.done():
                    await running
                await asyncio.sleep(0.02)
            await asyncio.to_thread(browse, f"http://127.0.0.1:{listener.getsockname()[1]}", token)
            assert outbound == ["POST", "GET", "GET"]
            assert [p.rsplit("/", 1)[-1] for p in posts] == [
                "upload_bfs",
                "add",
                "upload_bfs",
                "send_msg",
                "send_msg",
                "upload_bfs",
                "dyn",
            ]
            assert store.one("SELECT COUNT(*) AS n FROM operation_image_uploads")["n"] == 3
            assert store.one("SELECT COUNT(*) AS n FROM dm_delivery_steps")["n"] == 2
        finally:
            server.should_exit = True
            await running
