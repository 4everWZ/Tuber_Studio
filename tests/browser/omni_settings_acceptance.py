"""Exercise the shared connection and voice controls through the normal workbench."""
import asyncio
import base64
import socket

import uvicorn
from playwright.async_api import async_playwright, expect

from tuber_studio import omni_speech
from tuber_studio.app import create_app


async def test_shared_omni_settings_and_role_preview(settings, monkeypatch):
    token = "isolated-settings-fixture"
    monkeypatch.setenv("TUBER_ADMIN_TOKEN", token)
    settings.admin_token = token
    settings.data["features"]["live_enabled"] = False
    connect = socket.socket.connect

    def loopback(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("Unexpected external connection")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback)
    fixed_audio = settings.asset_path("assets/whale/sample1.wav").read_bytes()
    whale_audio = settings.asset_path("assets/whale/ref.wav").read_bytes()
    yaoyao = settings.runtime / "tts/ref/yaoyao-reference32k.wav"
    yaoyao.parent.mkdir(parents=True)
    yaoyao.write_bytes(fixed_audio)
    speech_inputs = []

    async def sentence(client, model, text, source):
        speech_inputs.append((text, base64.b64decode(source["data"])))
        return fixed_audio

    monkeypatch.setattr(omni_speech, "read_sentence", sentence)
    app = create_app(settings)
    store = app.state.store
    store.save_character(store.character() | {"voice_profile": "external_api"})
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("isolated Core stopped before accepting connections")
                await asyncio.sleep(0.02)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel="msedge", chromium_sandbox=True)
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{listener.getsockname()[1]}/settings")
                assert page.url.endswith("/#settings")
                await page.locator("#token").fill(token)
                async with (page.expect_response("**/api/account/bilibili") as account,
                            page.expect_response("**/api/automation") as permissions):
                    await page.locator("#login-form button").click()
                assert (await account.value).request.method == (await permissions.value).request.method == "GET"
                assert (await account.value).ok and (await permissions.value).ok
                await page.locator('[data-page="settings"]').click()
                await expect(page.get_by_label("网关 API 地址")).to_be_hidden()
                await page.get_by_text("共用连接与凭据", exact=True).click()
                await expect(page.get_by_label("网关 API 地址")).to_have_value(settings.data["omni"]["base_url"])
                await expect(page.locator('#providers-form input[type=password]')).to_have_count(1)
                await expect(page.locator("#llm-kind,#tts-kind,#background-base,#tts-form,#llm-model-select")).to_have_count(0)
                foreground = page.get_by_role("region", name="前台 Omni")
                background = page.get_by_role("region", name="后台 Agent")
                await expect(foreground.get_by_label("启用前台思考")).to_be_visible()
                await expect(foreground.locator('input[type="number"]')).to_have_count(0)
                await expect(background.locator("#llm-operations-tokens")).to_have_accessible_description(
                    "每次生成运营决定或草稿的上限，含思考。刷视频的选片决策在运营设置里调整。"
                )
                await expect(background.locator("#background-budget")).to_have_accessible_description(
                    "每轮生成计划、工具请求或成果文本的上限，含思考。一个任务可以有多轮。"
                )
                await expect(page.locator("#video-max-frames")).to_have_value("5")
                original_agent_budget = await background.locator("#background-budget").input_value()
                await page.locator("#video-max-frames").fill("7")
                await page.locator("#llm-operations-tokens").fill("2300")
                await page.get_by_role("button", name="保存推理设置").click()
                await expect(page.locator("#providers-save-state")).to_have_text("推理设置已保存。")
                await page.reload()
                await expect(page.locator("#llm-operations-tokens")).to_have_value("2300")
                await expect(background.locator("#background-budget")).to_have_value(original_agent_budget)
                await background.locator("#background-budget").fill("3072")
                async with page.expect_request("**/api/settings/providers") as saved:
                    await page.get_by_role("button", name="保存推理设置").click()
                body = (await saved.value).post_data_json
                assert set(body) == {"omni"} and body["omni"]["operations_max_tokens"] == 2300
                assert body["omni"]["video_options"] == {"max_frames": 7}
                assert body["omni"]["responses_options"]["max_output_tokens"] == 3072
                await expect(page.locator("#providers-save-state")).to_have_text("推理设置已保存。")
                await page.reload()
                await expect(page.locator("#llm-operations-tokens")).to_have_value("2300")
                await expect(background.locator("#background-budget")).to_have_value("3072")
                await expect(page.locator("#video-max-frames")).to_have_value("7")
                await page.get_by_text("共用连接与凭据", exact=True).click()
                await page.route("**/api/providers/models", lambda route: route.fulfill(json={"models": ["fixture-alias"]}))
                await page.get_by_role("button", name="读取已保存的前台模型别名").click()
                await expect(page.locator("#omni-model-options option")).to_have_attribute("value", "fixture-alias")
                await page.route("**/api/providers/check", lambda route: route.fulfill(json={
                    "foreground": {"available": True, "context_tokens": 8192},
                    "background": {"available": False, "error": "读取失败"},
                }))
                await page.get_by_role("button", name="检查已保存的连接").click()
                await expect(page.locator("#providers-check-result")).to_have_text("前台模型卡：已读取（8192 tokens） · 后台模型卡：读取失败")

                await page.set_viewport_size({"width": 390, "height": 844})
                await expect(background.locator("#llm-operations-tokens")).to_be_visible()
                await expect(background.locator("#background-budget")).to_be_visible()
                assert await background.evaluate(
                    """section => {
                        const [operations, agent] = section.querySelector('.form-grid').children;
                        return section.scrollWidth <= section.clientWidth &&
                               agent.getBoundingClientRect().top >= operations.getBoundingClientRect().bottom;
                    }"""
                )
                await page.set_viewport_size({"width": 1280, "height": 720})

                await page.locator('[data-page="characters"]').click()
                await page.get_by_role("tab", name="音色与形象绑定").click()
                await expect(page.locator('#character-voice-options option[value="yaoyao_reference"]')).to_have_count(1)
                await page.locator('[data-workspace-tab="voices"]').click()
                voice = page.get_by_label("音色选择")
                await expect(voice).to_have_value("external_api")
                await expect(page.locator("#preview-submit")).to_be_disabled()
                await voice.select_option("whale_sample_v1")
                await expect(page.locator("#preview-text")).to_be_hidden()
                await page.get_by_role("button", name="试听固定样本").click()
                await expect(page.locator("#preview-audio")).to_be_visible()
                await expect(page.locator("#preview-state")).to_contain_text("实际录音台词")
                assert not speech_inputs
                for profile, reference in [("whale_reference", whale_audio), ("yaoyao_reference", fixed_audio)]:
                    await voice.select_option(profile)
                    await expect(page.locator("#preview-audio")).to_be_hidden()
                    await page.get_by_label("试听文本").fill("角色试听。")
                    await page.get_by_role("button", name="生成试听音频").click()
                    await expect(page.locator("#preview-state")).to_contain_text("合成完成")
                    assert speech_inputs[-1] == ("角色试听。", reference)
                await page.get_by_label("试听文本").fill("瑶瑶的试听。")
                await page.route("**/api/tts/preview", lambda route: route.fulfill(status=503, json={"detail":"参考服务不可用"}))
                async with page.expect_request("**/api/tts/preview") as preview:
                    await page.get_by_role("button", name="生成试听音频").click()
                assert (await preview.value).post_data_json == {"voice_id": "yaoyao_reference", "text": "瑶瑶的试听。"}
                await expect(page.locator("#preview-state")).to_contain_text("合成失败")
                await expect(voice).to_have_value("yaoyao_reference")
                await page.locator('[data-page="automation"]').click()
                await expect(page.locator('[data-section="automation"]')).to_be_visible()
                for tab in ("automation-settings", "comments", "messages", "dynamics"):
                    await page.locator(f'[data-operations-tab="{tab}"]').click()
                    await expect(page.locator(f'[data-section="{tab}"]')).to_be_visible()
                assert not errors
                await browser.close()
                assert store.character()["voice_profile"] == "external_api"
        finally:
            server.should_exit = True
            await serving
