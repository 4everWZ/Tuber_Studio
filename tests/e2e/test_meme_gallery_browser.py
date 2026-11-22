"""Read the same bundled image from the chat picker and the gallery in an isolated Studio."""

import asyncio
import os
import shutil
import socket
from copy import copy

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
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error",
                                               timeout_graceful_shutdown=2))
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

                await page.get_by_text("导入图片", exact=True).click()
                form = page.locator("[data-meme-upload]")
                await form.locator("[name=file]").set_input_files({
                    "name": "broken.gif", "mimeType": "image/gif", "buffer": original[:len(original) // 2] + b";",
                })
                await form.locator("[name=title]").fill("截断的动作")
                await form.locator("[name=author]").fill("测试")
                await form.locator("[name=license_name]").fill("本地测试")
                await form.get_by_role("button", name="上传到图库").click()
                await expect(page.locator("[data-meme-feedback]")).to_have_text("表情「截断的动作」：GIF 数据块不完整或无效，未登记")
                assert not catalog.store.one("SELECT 1 FROM meme_assets WHERE title='截断的动作'")
                await page.locator("#workspace-issues").click()
                await expect(row).to_have_count(1)
                await row.get_by_role("link").click()
                if not await form.is_visible():
                    await page.get_by_text("导入图片", exact=True).click()
                await form.locator("[name=file]").set_input_files([
                    {"name": "静态立绘.png", "mimeType": "image/png", "buffer": (source / "memes/twirl.png").read_bytes()},
                    {"name": "跳舞动图.gif", "mimeType": "image/gif", "buffer": original},
                    {"name": "无效文件.gif", "mimeType": "image/gif", "buffer": b"GIF89a"},
                ])
                await form.locator("[name=author]").fill("测试")
                await form.locator("[name=license_name]").fill("本地测试")
                await form.locator("[name=tags]").fill("立绘、动作")
                await form.get_by_role("button", name="上传到图库").click()
                await expect(page.locator("[data-meme-feedback]")).to_contain_text("已导入 2 张，1 张失败")
                await expect(page.locator("[data-meme-failures]")).to_contain_text("无效文件.gif")
                await form.get_by_role("button", name="上传到图库").click()
                await expect(page.locator("[data-meme-feedback]")).to_contain_text("已导入 2 张，1 张失败")
                assert catalog.store.one("SELECT COUNT(*) AS n FROM meme_assets WHERE storage_kind='library'")["n"] == 2

                await page.set_viewport_size({"width": 430, "height": 880})
                await page.get_by_role("searchbox", name="搜索图库").fill("静态立绘")
                card = page.locator("[data-meme-grid] .meme-card")
                await expect(card).to_have_count(1)
                await card.get_by_text("编辑标签与描述").click()
                await card.get_by_label("题材标签").fill("庆祝、Q版")
                await card.get_by_label("画面描述").fill("大肥鱼转了个圈。")
                await card.get_by_role("button", name="保存标签").click()
                await expect(card).to_contain_text("大肥鱼转了个圈。")
                await page.reload()
                await page.get_by_role("searchbox", name="搜索图库").fill("大肥鱼转了个圈")
                await expect(card).to_have_count(1)
                await expect(card).to_contain_text("庆祝 · Q版")
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await browser.close()
        finally:
            server.should_exit = True
            await serving
            await app.state.bilibili_account.adapter.close()
            app.state.store.close()
