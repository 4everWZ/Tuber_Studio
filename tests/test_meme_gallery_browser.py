"""Read the same bundled image from the chat picker and the gallery in an isolated Studio."""

import asyncio
from copy import copy
import os
import shutil
import socket

import pytest
import uvicorn

from tuber_studio.app import create_app


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Playwright and Chromium")
async def test_gallery_shows_one_bundled_error_and_rejects_a_broken_upload(settings, tmp_path):
    from playwright.async_api import async_playwright, expect

    app = create_app(settings)
    catalog = app.state.memes
    catalog.settings = copy(settings)
    catalog.settings.root = tmp_path / "fixture"
    source = settings.root / "assets/whale"
    whale = catalog.settings.root / "assets/whale"
    shutil.copytree(source / "memes", whale / "memes")
    for path in [source / "manifest.json", *source.glob("*.gif")]:
        shutil.copyfile(path, whale / path.name)
    motion = whale / "memes/motion-dance.gif"
    original = motion.read_bytes()
    motion.write_bytes(original[:-1])

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(.05)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
                page = await browser.new_page(service_workers="block")
                await page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort())
                await page.goto(origin + "/#chat")
                await page.locator("#chat-pet").click()
                detail = "内置表情「摇摆一下」（motion-dance.gif）：GIF 缺少文件结束标记，未登记"
                picker = page.locator("dialog.meme-picker[open]")
                await expect(picker.locator("[data-picker-state]")).to_contain_text(detail)
                await picker.locator("[data-picker-manage]").click()
                await expect(page.locator("[data-meme-state]")).to_contain_text(detail)
                await page.locator("#workspace-issues").click()
                row = page.locator("#workspace-issue-list .issue-row").filter(has_text=detail)
                await expect(row).to_have_count(1)
                assert not catalog.store.one("SELECT 1 FROM meme_assets WHERE blob_name='motion-dance.gif'")

                motion.write_bytes(original)
                await row.get_by_role("link").click()
                await expect(page.locator("[data-meme-grid] .meme-card")).to_have_count(23)
                card = page.locator(".meme-card").filter(has=page.get_by_role("heading", name="摇摆一下"))
                await expect(card).to_contain_text("16 帧动图")
                assert await card.locator("img").evaluate("async img => { await img.decode(); return img.naturalWidth; }") == 256

                await page.get_by_text("导入表情包", exact=True).click()
                form = page.locator("[data-meme-upload]")
                await form.locator("[name=title]").fill("截断的动作")
                await form.locator("[name=author]").fill("测试")
                await form.locator("[name=license_name]").fill("本地测试")
                await form.locator("[name=file]").set_input_files({
                    "name": "broken.gif", "mimeType": "image/gif", "buffer": original[:len(original) // 2] + b";",
                })
                await form.get_by_role("button", name="上传到图库").click()
                await expect(page.locator("[data-meme-feedback]")).to_have_text("表情「截断的动作」：GIF 数据块不完整或无效，未登记")
                assert not catalog.store.one("SELECT 1 FROM meme_assets WHERE title='截断的动作'")
                await page.locator("#workspace-issues").click()
                await expect(row).to_have_count(1)
                await browser.close()
        finally:
            server.should_exit = True
            await serving
            await app.state.comments.adapter.close()
            app.state.store.close()
