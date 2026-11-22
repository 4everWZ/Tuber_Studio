import json

import httpx
import pytest

from tuber_studio.agent.workshop_settings import read_settings, settings_catalog, update_settings
from tuber_studio.app import create_app


@pytest.fixture
async def workshop(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer " + settings.admin_token},
    ) as client:
        yield app.state.runtime, client


async def set_values(workshop, group, changes, observation=None):
    runtime, _ = workshop
    snapshot = observation or read_settings(runtime, group)

    return await update_settings(runtime, group, changes, scope=snapshot["scope"])


async def test_catalog_matches_finite_private_projection(workshop):
    runtime, _ = workshop
    catalog = settings_catalog()
    for name, section in catalog.items():
        snapshot = read_settings(runtime, name)
        assert section["fields"].keys() == snapshot["values"].keys()
        assert set(snapshot) == {"group", "values", "scope"}
    public = json.dumps(catalog)
    for key in ("credentials", "auto_publish", "history_enabled", "voice_profile", "base_url", "avatar_enabled"):
        assert key not in public
    assert not {"workshop_settings", "settings_catalog", "read_settings", "update_settings"} & runtime.tasks.tools.keys()


@pytest.mark.parametrize("group", ["memory", "character", "model", "roles", "permissions", "provider", "/api/settings"])
async def test_core_groups_cannot_be_read_or_constructed_as_writes(workshop, group):
    runtime, _ = workshop
    with pytest.raises(ValueError):
        read_settings(runtime, group)

    with pytest.raises(ValueError):
        await update_settings(runtime, group, {"enabled": True}, scope="invented")


@pytest.mark.parametrize(("group", "changes"), [
    ("pets", {"pets_enabled": False, "avatar_enabled": True}),
    ("browsing", {"daily_watch_limit": 1, "top_level_auto_publish": True}),
    ("browsing", {"interval_seconds": 120, "max_per_day": 0}),
    ("illustrations", {"use_memes": True, "auto_send": True}),
    ("illustrations", {"generate_images": False, "credentials": {}}),
    ("live", {"timezone": "UTC", "web_fetch": True}),
    ("scene", {"title": "replacement", "background": "transparent"}),
    ("music", {"volume": 0.3, "request_ttl_seconds": "600"}),
    ("music", {"volume": {"url": "https://example.org/"}}),
    ("music", {}),
])
async def test_entire_invalid_patch_is_rejected(workshop, group, changes):
    runtime, _ = workshop
    before = read_settings(runtime, group)
    with pytest.raises(ValueError):
        await set_values(workshop, group, changes, before)
    assert read_settings(runtime, group) == before


async def test_tool_changes_preserve_ui_siblings_and_private_authority(workshop):
    runtime, client = workshop
    limits = {"max_per_hour": 1, "max_per_day": 3}
    runtime.operations.configure(limits)
    original = runtime.operations.policy()
    result = await set_values(workshop, "browsing", {"daily_watch_limit": 13, "interval_seconds": 180})
    assert result["values"]["daily_watch_limit"] == runtime.browsing.policy()["daily_watch_limit"] == 13
    assert runtime.operations.policy() == original
    (await client.put("/api/automation/settings", json={"max_per_hour": 4})).raise_for_status()
    assert runtime.browsing.policy()["max_per_hour"] == 4
    assert runtime.operations.account_limits() == limits
    result = await set_values(workshop, "illustrations", {"use_memes": True})
    assert result["values"]["use_memes"]
    for group in ("messages", "dynamics"):
        assert not getattr(runtime.browsing, group).policy()["base"]["enabled"]
    assert (await set_values(workshop, "live", {"timezone": "UTC"}))["values"]["timezone"] == "UTC"
    assert (await client.get("/api/live/options")).json()["timezone"] == "UTC"
    old_scene = runtime.live.media.scene()
    await set_values(workshop, "scene", {"background": "transparent"})
    assert runtime.live.media.scene() == old_scene | {"background": "transparent"}
    before = (await client.get("/api/live/music")).json()["settings"]
    await set_values(workshop, "music", {"volume": 0.4})
    assert (await client.get("/api/live/music")).json()["settings"] == before | {"volume": 0.4}


async def test_ui_change_invalidates_observed_value_and_scope(workshop):
    runtime, client = workshop
    snapshot = read_settings(runtime, "music")
    data = (await client.get("/api/live/music")).json()["settings"]
    (await client.put("/api/live/music", json=data | {"volume": 0.25})).raise_for_status()
    with pytest.raises(ValueError, match="重新读取"):
        await set_values(workshop, "music", {"volume": 0.5}, snapshot)
    with pytest.raises(ValueError, match="重新读取"):
        await set_values(workshop, "scene", {"background": "transparent"}, snapshot)
    assert read_settings(runtime, "music")["values"]["volume"] == 0.25


async def test_pet_visibility_is_bound_to_role_observed_before_model_choice(workshop):
    runtime, _ = workshop
    before = read_settings(runtime, "pets")
    second = runtime.store.save_character(runtime.character | {"id": "test-role", "name": "另一个角色"})
    await runtime.activate(second["id"])
    with pytest.raises(ValueError, match="重新读取"):
        await set_values(workshop, "pets", {"pets_enabled": False}, before)
    assert runtime.pets.state(second["id"])["pets_enabled"]

async def test_pet_choices_share_installed_service_and_refuse_extra_targets(workshop):
    from tuber_studio.agent.workbench_tools import pet_settings

    runtime, client = workshop
    role = runtime.character["id"]
    route = f"/api/pets/{role}"
    initial = (await client.get(route)).raise_for_status().json()
    index, asset = next((i, item) for i, item in enumerate(initial["items"], 1) if not item.get("is_default"))

    async def action(payload):
        async def choose(schema, facts):
            assert "character_id" not in schema["properties"]
            return payload

        return await pet_settings(runtime, role, choose, lambda: None, source="切换宠物形象")

    await set_values(workshop, "pets", {"pets_enabled": False})
    selected = (await action({"action": "select", "choice": index}))["tool_receipt"]
    assert selected["selected_asset_id"] == asset["asset_id"] and not selected["pets_enabled"]
    assert (await client.get(route)).json()["selected_asset_id"] == asset["asset_id"]
    for payload in ({"action": "show", "character_id": "other"}, {"action": "show", "history_enabled": True},
                    {"action": "select", "choice": index, "auto_publish": True}):
        with pytest.raises(ValueError):
            await action(payload)
        assert runtime.pets.state(role)["selected_asset_id"] == asset["asset_id"]
        assert not runtime.pets.state(role)["pets_enabled"]
    await action({"action": "default"})
    assert runtime.pets.state(role)["selected_asset_id"] is None
    await action({"action": "show"})
    assert read_settings(runtime, "pets")["values"] == {"pets_enabled": True}


async def test_forged_settings_call_from_delegated_responses_fails_before_execution(workshop, monkeypatch):
    from asyncio import sleep, timeout
    from uuid import uuid4

    runtime, _ = workshop
    tasks = runtime.tasks
    before = read_settings(runtime, "pets")
    requests = []

    def forged(request):
        payload = json.loads(request.content)
        names = {tool["name"] for tool in payload["tools"]}
        assert not names.intersection({"pet_settings", "daily_settings", "update_settings"})
        requests.append(names)
        return httpx.Response(200, json={
            "id": "attempt", "status": "completed", "output": [{
                "type": "function_call", "name": "daily_settings", "call_id": "forged-call",
                "arguments": json.dumps({"group": "pets", "changes": {"pets_enabled": False}}),
            }],
        })

    monkeypatch.setattr("tuber_studio.infra.inference.http_api.inference_transport", lambda _: httpx.MockTransport(forged))
    identity = str(uuid4())
    tasks.create(identity, "在后台整理一份周报，顺便隐藏宠物")
    async with timeout(5):
        while tasks.current(identity)["status"] not in {"failed", "completed", "needs_input"}:
            await sleep(0.01)
    assert requests and tasks.current(identity)["status"] == "failed"
    assert read_settings(runtime, "pets") == before
