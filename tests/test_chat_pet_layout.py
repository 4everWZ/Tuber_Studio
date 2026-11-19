"""Measure the painted Pet in the ordinary chat and call UI with an isolated Studio."""

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
async def pet_studio(settings, monkeypatch):
    import uvicorn

    from tuber_studio.agent import private_call
    from tuber_studio.app import create_app

    app = create_app(settings)
    runtime = app.state.runtime
    reply = {"private": True, "account_id": "", "reply_text": "好，先整理今天的开场和互动话题。"}
    runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
        str(uuid4()), runtime.character["id"], runtime.character["version"], "一起准备今天的直播吧。",
        json.dumps(reply), time.time(),
    ))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        monkeypatch.setattr(private_call, "private_service", lambda settings: (origin, "fixture"))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", lifespan="off",
                                               timeout_graceful_shutdown=2))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(.05)
            yield origin
        finally:
            server.should_exit = True
            await serving
            await app.state.comments.adapter.close()
            app.state.store.close()


async def capture(page, name):
    if folder := os.getenv("TUBER_PET_SCREENSHOTS"):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=path / f"{name}.png")


async def painted_size(page):
    from PIL import Image, ImageSequence

    from tuber_studio.config import ROOT

    image = page.locator("#chat-agent-avatar")
    await image.evaluate("img => img.decode()")
    src = await image.get_attribute("src")
    with Image.open(ROOT / src.lstrip("/")) as asset:
        w, h = asset.size
        frames = [f.convert("RGBA").getchannel("A").getbbox() for f in ImageSequence.Iterator(asset)]
    box = await image.bounding_box()
    button = await page.locator("#chat-pet").bounding_box()
    scale = min(box["width"] / w, box["height"] / h)
    x = box["x"] + (box["width"] - w * scale) / 2
    y = box["y"] + (box["height"] - h * scale) / 2
    for left, top, right, bottom in frames:
        assert button["x"] <= x + left * scale < x + right * scale <= button["x"] + button["width"]
        assert button["y"] <= y + top * scale < y + bottom * scale <= button["y"] + button["height"]
    return tuple(round(scale * size, 1) for size in (
        max(f[2] for f in frames) - min(f[0] for f in frames),
        max(f[3] for f in frames) - min(f[1] for f in frames),
    ))


async def check_visible_control(locator):
    assert await locator.evaluate("""el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= innerWidth &&
            r.top >= 0 && r.bottom <= innerHeight &&
            el.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
    }"""), await locator.text_content()


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Chromium")
async def test_pet_layout_in_chromium(pet_studio):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(pet_studio + "/") else r.abort())
        page = await context.new_page()
        await page.goto(pet_studio)
        pet = page.locator("#chat-pet")
        sizes = {}
        for name, width, height in (("desktop", 1366, 800), ("narrow", 390, 844), ("compact", 320, 568)):
            await page.set_viewport_size({"width": width, "height": height})
            await expect(page.get_by_text("一起准备今天的直播吧。", exact=True)).to_be_visible()
            await expect(pet).to_be_visible()
            sizes[name] = await painted_size(page)
            await capture(page, name)
            for selector in ("#chat-pet", "#operator-chat-title", "#operator-chat-input", "#chat-activity-toggle",
                             "#operator-chat-more-toggle", ".operator-chat-user"):
                await check_visible_control(page.locator(selector))
            for link in await page.locator(".operator-chat-heading-actions > a").all():
                await check_visible_control(link)
            await page.locator("#chat-activity-toggle").click()
            await check_visible_control(page.locator("#chat-activity-close"))
            await page.locator("#chat-activity-close").click()
            await page.locator("#operator-chat-more-toggle").click()
            await page.locator("#private-call-open").click()
            await expect(page.locator("#private-call-pet-slot #chat-pet")).to_be_visible()
            sizes[name + "-call"] = await painted_size(page)
            await capture(page, name + "-call")
            await check_visible_control(pet)
            for control in ("#private-call-start", "#private-call-end"):
                await check_visible_control(page.locator(control))
            assert await pet.count() == 1
            await page.locator("#private-call-end").click()
            await expect(page.locator(".chat-agent-identity #chat-pet")).to_be_visible()
        await page.get_by_role("link", name="角色权限").click()
        await expect(page.locator("#operations-permission-matrix")).to_be_in_viewport()
        await page.go_back()
        await expect(pet).to_be_visible()
        await page.emulate_media(reduced_motion="reduce")
        await expect(page.locator("#chat-agent-avatar")).to_have_attribute("src", "/assets/whale/memes/nap.png")
        sizes["reduced-motion"] = await painted_size(page)
        await pet.click()
        await expect(page.locator(".meme-picker[open]")).to_be_visible()
        await page.keyboard.press("Escape")
        await page.locator("#operator-chat-more-toggle").click()
        await page.locator("#chat-pet-toggle").click()
        await expect(pet).to_be_hidden()
        await page.reload()
        await expect(page.get_by_text("一起准备今天的直播吧。", exact=True)).to_be_visible()
        await expect(pet).to_be_hidden()
        await capture(page, "pets-off")
        await page.locator("#operator-chat-more-toggle").click()
        await page.locator("#private-call-open").click()
        await expect(page.locator("#private-call-dialog")).to_be_visible()
        await expect(pet).to_be_hidden()
        assert (await page.locator("#private-call-pet-slot").bounding_box() or {"height": 0})["height"] == 0
        await check_visible_control(page.locator("#private-call-start"))
        await capture(page, "pets-off-call")
        await browser.close()

    print("Pet painted bounds (CSS px):", sizes)
    assert sizes["desktop"][1] >= 100
    assert sizes["narrow"][1] >= 80
    assert sizes["compact"][1] >= 64
    assert sizes["narrow-call"][1] >= 145
