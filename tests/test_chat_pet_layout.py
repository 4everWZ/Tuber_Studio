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
    for i in range(28):
        history = {"private": True, "account_id": "", "reply_text": f"第 {i} 条记录。\n\n" + "直播提纲。\n\n" * 3}
        runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
            str(uuid4()), runtime.character["id"], runtime.character["version"], f"准备记录 {i}",
            json.dumps(history), time.time() - 100 + i,
        ))
    task = runtime.tasks.create(str(uuid4()), "开场提纲")
    with runtime.store.db as db:
        runtime.tasks.workspace.record_delivery(db, task, "先介绍今天的直播主题。")
    runtime.tasks.create(str(uuid4()), "待整理的话题")

    async def pet_decision(messages, schema, **kwargs):
        if schema["title"] == "CardTurnIntent":
            return {"intent": "当场调用工具", "reference": None, "patch": None}
        if schema["title"] == "WorkLifecycle":
            return {"action": "direct_tool"}
        if schema["title"] == "AssistantDecision":
            return {"action": "pet_settings"}
        assert schema["title"] == "PetChoice"
        instruction = messages[-1]["content"]
        if instruction == "给宠物换个开心的表情":
            facts = json.loads(messages[0]["content"].split("\n", 1)[1])
            item = next(item for item in facts["可选择的已有形象与表情"] if "cheerful" in item["emotions"])
            return {"action": "select", "choice": item["choice"]}
        return {"action": {"藏起宠物": "hide", "显示宠物": "show", "恢复默认形象": "default"}[instruction]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", pet_decision, raising=False)
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
        feed = page.locator("#operator-chat-timeline")
        panel = page.locator("#chat-activity")
        sizes = {}
        for name, width, height in (("wide", 2560, 1419), ("desktop", 1366, 800),
                                    ("narrow", 390, 844), ("compact", 320, 568)):
            await page.set_viewport_size({"width": width, "height": height})
            await expect(page.get_by_text("一起准备今天的直播吧。", exact=True)).to_be_visible()
            await expect(pet).to_be_visible()
            sizes[name] = await painted_size(page)
            heading = await page.locator(".operator-chat-heading").bounding_box()
            timeline = await feed.bounding_box()
            bounds = await pet.bounding_box()
            assert heading["height"] <= 50
            assert abs(bounds["y"] - timeline["y"]) <= 1
            assert abs(bounds["x"] + bounds["width"] / 2 - timeline["x"] - timeline["width"] / 2) <= 10
            compose = await page.locator("#operator-chat-form").bounding_box()
            assert timeline["y"] + timeline["height"] <= compose["y"] + 1
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            print(name, "header", heading["height"], "timeline", timeline, "pet", sizes[name])
            await capture(page, name)
            for selector in ("#chat-pet", "#operator-chat-title", "#operator-chat-input", "#chat-activity-toggle",
                             "#operator-chat-more-toggle", "#private-call-open"):
                await check_visible_control(page.locator(selector))
            await page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
            before = await feed.evaluate("el => el.scrollTop")
            await page.mouse.wheel(0, -200)
            await page.wait_for_function("top => document.querySelector('#operator-chat-timeline').scrollTop < top",
                                         arg=before)
            assert await pet.bounding_box() == bounds
            await feed.evaluate("el => el.scrollTop = el.scrollHeight")
            await page.locator("#chat-activity-toggle").click()
            await check_visible_control(page.locator("#chat-activity-close"))
            await expect(panel.get_by_role("button", name="预约任务")).to_be_visible()
            await panel.get_by_role("button", name="权限", exact=True).click()
            await expect(panel.locator('[data-capability="top_level"]')).to_be_visible()
            await check_visible_control(panel.get_by_role("link", name="角色权限"))
            await panel.get_by_role("button", name="成果", exact=True).click()
            await panel.get_by_label("按任务查看成果").select_option(label="开场提纲")
            await expect(panel.get_by_text("先介绍今天的直播主题。", exact=True)).to_be_visible()
            await panel.get_by_role("button", name="选择其他任务").click()
            await panel.get_by_label("按任务查看成果").select_option(label="待整理的话题")
            await expect(panel.get_by_text("这项任务暂无成果。", exact=True)).to_be_visible()
            await page.locator("#chat-activity-close").click()
            await expect(page.locator("#chat-activity-toggle")).to_be_focused()
            await page.locator("#private-call-open").click()
            await expect(page.locator("#private-call-pet-slot #chat-pet")).to_be_visible()
            sizes[name + "-call"] = await painted_size(page)
            await capture(page, name + "-call")
            await check_visible_control(pet)
            for control in ("#private-call-start", "#private-call-end"):
                await check_visible_control(page.locator(control))
            assert await pet.count() == 1
            await page.locator("#private-call-end").click()
            await expect(page.locator(".chat-pet-float #chat-pet")).to_be_visible()
        await feed.evaluate("el => el.scrollTop = 0")
        anchor = page.locator(".operator-chat-turn").first
        anchor_id = await anchor.get_attribute("data-request-id")
        old_y = (await anchor.bounding_box())["y"]
        await page.get_by_role("button", name="加载更早消息").click()
        await expect(page.get_by_text("准备记录 0", exact=True)).to_be_attached()
        anchored = await page.locator(f'[data-request-id="{anchor_id}"]').bounding_box()
        assert abs(anchored["y"] - old_y) <= 1
        await page.locator("#operator-chat-input").fill("未发送草稿")
        await page.set_viewport_size({"width": 320, "height": 380})
        for control in (pet, page.locator("#operator-chat-input"), page.locator("#operator-chat-send")):
            await check_visible_control(control)
        await expect(page.locator("#operator-chat-input")).to_have_value("未发送草稿")
        await page.set_viewport_size({"width": 320, "height": 568})
        await page.locator("#chat-activity-toggle").click()
        await panel.get_by_role("button", name="权限", exact=True).click()
        await page.get_by_role("link", name="角色权限").click()
        await expect(page.locator("#operations-permission-matrix")).to_be_in_viewport()
        await page.go_back()
        await expect(pet).to_be_visible()
        avatar = page.locator("#chat-agent-avatar")
        for instruction in ("藏起宠物", "显示宠物", "给宠物换个开心的表情", "恢复默认形象"):
            await page.locator("#operator-chat-input").fill(instruction)
            await page.locator("#operator-chat-send").click()
            sent = page.locator(".operator-chat-turn[data-request-id]").filter(has=page.get_by_text(instruction, exact=True))
            await expect(sent.locator(".operator-chat-reply")).to_contain_text("宠物当前")
            if instruction == "藏起宠物":
                await expect(pet).to_be_hidden()
            else:
                await expect(pet).to_be_visible()
                if instruction == "给宠物换个开心的表情":
                    await expect(avatar).not_to_have_attribute("src", "/assets/whale/idle.gif")
                    await avatar.evaluate("img => img.decode()")
                else:
                    await expect(avatar).to_have_attribute("src", "/assets/whale/idle.gif")
        await page.emulate_media(reduced_motion="reduce")
        await expect(page.locator("#chat-agent-avatar")).to_have_attribute("src", "/assets/whale/memes/nap.png")
        sizes["reduced-motion"] = await painted_size(page)
        await pet.focus()
        await page.keyboard.press("Enter")
        await expect(page.locator(".meme-picker[open]")).to_be_visible()
        await page.keyboard.press("Escape")
        await page.locator("#operator-chat-more-toggle").click()
        await page.locator("#chat-pet-toggle").click()
        await expect(pet).to_be_hidden()
        await page.reload()
        await expect(page.get_by_text("一起准备今天的直播吧。", exact=True)).to_be_visible()
        await expect(pet).to_be_hidden()
        await capture(page, "pets-off")
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
