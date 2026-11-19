import asyncio
import json
import os
import shutil
import socket
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.pet_appearance import PetAppearances


async def test_selection_follows_bound_manifest_without_editing_the_card(settings, store):
    pets = PetAppearances(settings, store)
    whale = store.character()
    options = await pets.options("whale")
    flower = next(item for item in options["items"] if item["title"] == "花花")
    assert flower["style"] and flower["emotions"] and not flower["animated"]
    selected = await pets.select("whale", flower["asset_id"])
    assert selected["selected"] == flower
    assert PetAppearances(settings, store).state("whale") == selected
    assert pets.set_visible("whale", False)["selected_asset_id"] == flower["asset_id"]
    assert store.character() == whale

    store.save_character(whale | {"id": "other", "avatar_manifest": "assets/absent.json"})
    store.activate("other")
    assert (await pets.options("other"))["items"] == []
    with pytest.raises(ValueError):
        await pets.select("other", flower["asset_id"])
    with pytest.raises(ValueError):
        pets.set_visible("whale", True)
    assert pets.state("other")["selected_asset_id"] is None
    store.activate("whale")
    assert not pets.state("whale")["pets_enabled"]
    assert pets.state("whale")["selected_asset_id"] == flower["asset_id"]
    pets.catalog.enable(flower["asset_id"], False)
    assert pets.state("whale")["selected"] is None
    with pytest.raises(ValueError):
        await pets.select("whale", flower["asset_id"])
    assert (await pets.select("whale", None))["revision"] > selected["revision"]


async def test_original_character_has_only_its_manifest_images(settings, store, tmp_path):
    root = tmp_path / "project"
    assets = root / "assets" / "fish"
    assets.mkdir(parents=True)
    shutil.copyfile(settings.root / "assets/whale/memes/nap.png", assets / "sleep.png")
    settings.root = root
    entries = [{"id": "sleep", "tag": "idle", "title": "午睡", "path": "assets/fish/sleep.png",
                "source": "号主原创", "license_status": "owned", "style": "水彩", "emotions": ["idle"]}]
    entries.extend([entries[0] | {"id": "lost", "path": "assets/fish/missing.png"},
                    entries[0] | {"id": "voice", "path": "assets/fish/record.wav"},
                    entries[0] | {"id": "invalid", "path": "assets/fish/bad.png"}])
    (assets / "record.wav").write_bytes(b"fixture")
    (assets / "bad.png").write_bytes(b"not an image")
    (assets / "manifest.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    store.save_character(store.character() | {"id": "fish", "avatar_manifest": "assets/fish/manifest.json"})
    store.activate("fish")
    pets = PetAppearances(settings, store)
    item, = (await pets.options("fish"))["items"]
    assert (item["title"], item["style"], item["animated"]) == ("午睡", "水彩", False)
    assert item["source"]["note"] == "号主原创"
    assert item["manifest_owned"] and not item["source"]["url"]
    assert item["preview_url"] == "/assets/fish/sleep.png"


def test_pet_endpoint_rejects_foreign_resources_and_mixed_control_writes(settings):
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        path = "/api/pets/whale"
        choices = client.get(path).raise_for_status().json()["items"]
        first = next(item for item in choices if item["title"] == "开心")
        valid = {"asset_id": first["asset_id"]}
        state = client.put(path, json=valid).raise_for_status().json()
        runtime = app.state.runtime
        for extra in ({"history_enabled": True}, {"enabled": False}, {"character_id": "other"},
                      {"grant": {"auto_post": True}}, {"provider": "other"}):
            assert client.put(path, json={"asset_id": None, **extra}).status_code == 422
        for asset_id in ("/assets/whale/sample1.wav", "https://example.org/portrait.png", {"value": "idle"}):
            assert client.put(path, json={"asset_id": asset_id}).status_code in (400, 422)
        assert runtime.pets.state("whale") == state
        assert client.get("/api/chat/intelligent").raise_for_status().json()["pet"] == state
        assert runtime.store.get("performance", {}) == {}


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Chromium")
async def test_two_chat_browsers_share_manual_and_tool_selection(settings, monkeypatch):
    from playwright.async_api import async_playwright, expect

    app = create_app(settings)
    runtime = app.state.runtime
    instruction = "把宠物换成鲸鱼思考"

    async def choose(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle":
            return {"action": "direct_tool"}
        if schema["title"] == "AssistantDecision":
            return {"action": "pet_settings"}
        assert schema["title"] == "PetChoice" and messages[-1]["content"] == instruction
        packet = json.loads(messages[0]["content"].split("\n", 1)[1])
        match = next(row for row in packet["可选择的已有形象与表情"] if row["title"] == "鲸鱼思考")
        return {"action": "select", "choice": match["choice"]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", choose, raising=False)
    async with chat_server(app) as origin, async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
        pages = []
        for _ in range(2):
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort())
            page = await context.new_page()
            await page.goto(origin)
            await expect(page.locator("#chat-pet")).to_be_visible()
            pages.append(page)
        first, other = pages
        await first.locator("#chat-pet").click()
        await first.get_by_role("button", name="选用花花", exact=True).click()
        await expect(first.locator(".meme-picker[open]")).to_have_count(0)
        manual = runtime.pets.state("whale")["selected"]
        assert manual["title"] == "花花"
        await other.reload()
        for page in pages:
            await expect(page.locator("#chat-agent-avatar")).to_have_attribute("src", manual["preview_url"])
            assert await page.locator("#chat-agent-avatar").evaluate("async img => {await img.decode(); return img.naturalWidth > 0}")

        await first.locator("#operator-chat-input").fill(instruction)
        await first.locator("#operator-chat-send").click()
        await expect(first.locator(".operator-chat-reply").last).to_contain_text("鲸鱼思考", timeout=16000)
        tool = runtime.pets.state("whale")["selected"]
        assert tool["title"] == "鲸鱼思考"
        for page in pages:
            await expect(page.locator("#chat-agent-avatar")).to_have_attribute("src", tool["preview_url"], timeout=16000)
        await other.locator("#chat-pet").click()
        await other.get_by_role("button", name="恢复默认形象").click()
        await expect(other.locator("#chat-agent-avatar")).to_have_attribute("src", "/assets/whale/idle.gif")
        for context in browser.contexts:
            await context.unroute_all(behavior="ignoreErrors")
            await context.close()
        await browser.close()


@asynccontextmanager
async def chat_server(app):
    import uvicorn

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off",
                                               timeout_graceful_shutdown=2))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(.05)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await serving
            await app.state.comments.adapter.close()
            app.state.store.close()
