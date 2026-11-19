import json
import os
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.models import resident_character_profile


def test_custom_legacy_profile_projects_each_field_without_editing_the_card():
    card = {
        "name": "大肥鱼",
        "core": {"identity": "你是蓝发鲸鱼娘，住在海底小屋，机灵傲娇又爱摸鱼。", "background": "其他设定"},
        "style": {"instructions": "说话口语化，平常用「我」和「你」。"},
    }
    original = deepcopy(card)
    expected = {"name": "大肥鱼", "self_description": "你是蓝发鲸鱼娘", "speaking_style": "说话口语化"}
    assert resident_character_profile(card) == expected
    assert resident_character_profile(card | {"self_description": "我是船长。"}) == expected | {
        "self_description": "我是船长。"}
    assert resident_character_profile(card | {"speaking_style": "温和。"}) == expected | {"speaking_style": "温和。"}
    assert card == original


@pytest.mark.parametrize(("identity", "style", "extra"), [
    ("你是" + "蓝" * 80 + "鲸鱼娘。", "说话" + "轻" * 80, {}),
    ("示例：你是我的朋友吗？", ["如果遇到熟人，说话轻快。"], {}),
    ("你是谁？", [], {}),
    ("你是。", "说话", {}),
    ("我是领航员。", ["语气温和。", "其他细节"], {"self_description": "我是领航员", "speaking_style": "语气温和"}),
])
def test_legacy_profile_uses_only_bounded_declarations(identity, style, extra):
    card = {"name": "角色", "core": {"identity": identity}, "style": {"instructions": style}}
    assert resident_character_profile(card) == {"name": "角色", **extra}


def test_editor_api_persists_validated_short_fields_and_keeps_the_long_card(settings):
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        card = client.post("/api/characters", json={"name": "旧卡"}).raise_for_status().json()
        path = f"/api/characters/{card['id']}"
        card.update(core={"identity": "详细身份" * 100, "background": "我的来历"}, style={"instructions": ["完整旧指导"]})
        client.put(path, json=card).raise_for_status()
        updated = card | {"self_description": "鲸" * 120, "speaking_style": "语气平和。"}
        assert client.put(path, json=updated).raise_for_status().json() == updated
        for key in ("self_description", "speaking_style"):
            for invalid in ("鲸" * 121, "第一行\n第二行", ["旧式列表"]):
                response = client.put(path, json=updated | {key: invalid})
                assert response.status_code == 422 and response.json()["detail"][0]["field"] == key
        assert client.get(path).raise_for_status().json() == updated
        assert client.put(path, json=updated | {"self_description": "", "speaking_style": " "}).json() == card


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Chromium")
async def test_legacy_card_in_character_studio(settings):
    from playwright.async_api import async_playwright, expect
    from evaluations.creative_browser import server

    app = create_app(settings)
    card = app.state.store.character()
    for key in ("self_description", "speaking_style"):
        del card[key]
    card["style"]["instructions"] = ["说话随和。", "完整的第二条指导。"]
    app.state.store.replace_character(card)
    async with server(app) as origin, async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
        page = await browser.new_page()
        await page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort())
        await page.goto(origin)
        await page.get_by_role("link", name="角色工作室", exact=True).click()
        await expect(page.locator("#oc-identity")).to_have_value(card["core"]["identity"])
        summary = page.get_by_label("角色自述（常驻）")
        style = page.get_by_label("说话风格（常驻）")
        await expect(summary).to_have_value("")
        await expect(style).to_have_value("")
        await summary.fill("我是海边的领航员。")
        await style.fill("用简短的句子说话。")
        async with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/characters/whale")) as saved:
            await page.get_by_role("button", name="保存角色", exact=True).click()
        assert (await saved.value).ok
        await page.reload()
        await expect(summary).to_have_value("我是海边的领航员。")
        await expect(style).to_have_value("用简短的句子说话。")
        await page.get_by_role("tab", name="高级 JSON 源码").click()
        actual = json.loads(await page.locator("#character-editor").input_value())
        assert actual["core"] == card["core"] and actual["style"] == card["style"]
        await browser.close()
