import json
from uuid import uuid4

import httpx
import pytest
from front_effects import effect as route_effect
from test_agent_creative_delegation import call, gateway, until

from tuber_studio.agent.task_tools import create_task_runtime
from tuber_studio.app import create_app


@pytest.fixture
async def studio(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        headers={"Authorization": "Bearer " + settings.admin_token},
    ) as client:
        yield app.state.runtime, client


def choose_tools(runtime, monkeypatch, direct, domain=None, *, discover=True):
    calls = []

    async def select(messages, schema, **options):
        calls.append(schema["title"])
        effect = route_effect(direct["action"], direct.get("settings_group"))
        assert effect in schema["properties"]["effect"]["enum"]
        return {"effect": effect}

    async def generate(messages, schema, **options):
        calls.append(schema["title"])
        if schema["title"] == "WorkLifecycle":
            assert "agent_task" in schema["properties"]["action"]["enum"]
            return {"action": "direct_tool"}
        if schema["title"] == "AssistantDecision":
            menu = schema["properties"]["action"]["enum"]
            assert not {"operation_control", "start_operations", "agent_task"}.intersection(menu)
            return direct
        return domain(messages, schema) if callable(domain) else domain

    if discover:
        monkeypatch.setattr(runtime.llm, "choose_front_effect", select, raising=False)
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    return calls


def turn(text):
    return {"request_id": str(uuid4()), "message": text}


async def test_capability_discovery_reads_catalog_without_settings_writes(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character["id"]
    before = runtime.pets.state(char), runtime.browsing.policy()
    calls = choose_tools(runtime, monkeypatch, {"action": "workbench_capabilities"})
    response = await client.post("/api/chat/intelligent", json=turn("先告诉我你能调什么"))
    text = response.raise_for_status().json()["reply_text"]
    assert all(label in text for label in ("宠物", "每日观看", "点歌", "运营配图", "直播画布", "后台"))
    assert before == (runtime.pets.state(char), runtime.browsing.policy())
    assert calls == ["FrontToolDiscovery"]


async def test_pet_choice_from_real_catalog_is_bound_and_replayable(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character["id"]
    options = (await runtime.pets.options(char))["items"]
    picked = next(item for item in options if "cheerful" in item["emotions"] and not item["is_default"])

    def select(messages, schema):
        assert messages[-1]["content"] == "换个开心的鱼鱼"
        values = json.loads(messages[0]["content"].split("\n", 1)[1])
        candidate = next(row for row in values["可选择的已有形象与表情"] if row["title"] == picked["title"] and row["emotions"] == picked["emotions"])
        assert "character_id" not in schema["properties"]
        return {"action": "select", "choice": candidate["choice"]}

    calls = choose_tools(runtime, monkeypatch, {"action": "pet_settings"}, select)
    body = turn("换个开心的鱼鱼")
    first = (await client.post("/api/chat/intelligent", json=body)).raise_for_status().json()
    saved = runtime.pets.state(char)
    assert saved["selected_asset_id"] == first["tool_receipt"]["selected_asset_id"] == picked["asset_id"]
    assert picked["title"] in first["reply_text"]
    assert (await client.post("/api/chat/intelligent", json=body)).raise_for_status().json() == first
    assert calls == ["FrontToolDiscovery", "PetChoice"] and runtime.pets.state(char) == saved


@pytest.mark.parametrize("choice", [
    {"action": "select", "choice": 1, "character_id": "another"},
    {"action": "hide", "memory_enabled": True},
    {"action": "hide", "choice": 1},
])
async def test_pet_mixed_arguments_are_atomic(studio, monkeypatch, choice):
    runtime, client = studio
    char = runtime.character["id"]
    before = runtime.pets.state(char)
    choose_tools(runtime, monkeypatch, {"action": "pet_settings"}, choice)
    response = await client.post("/api/chat/intelligent", json=turn("收起宠物"))
    assert response.status_code == 400 and runtime.pets.state(char) == before


async def test_pet_rejects_a_settings_group_before_domain_selection(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character["id"]
    before = runtime.pets.state(char)
    calls = choose_tools(runtime, monkeypatch, {"action": "pet_settings", "settings_group": "memory"},
                         {"action": "hide"}, discover=False)
    response = await client.post("/api/chat/intelligent", json=turn("收起宠物"))
    assert response.status_code == 400 and runtime.pets.state(char) == before
    assert "PetChoice" not in calls


async def test_pet_choice_rejects_character_switch_during_inference(studio, monkeypatch):
    runtime, client = studio
    original = runtime.character["id"]
    other = runtime.store.save_character(runtime.character | {"id": "pet-layout-fixture"})
    before = runtime.pets.state(original)

    def switch_before_reply(messages, schema):
        runtime.store.activate(other["id"])
        return {"action": "hide"}

    choose_tools(runtime, monkeypatch, {"action": "pet_settings"}, switch_before_reply)
    response = await client.post("/api/chat/intelligent", json=turn("收起宠物"))
    assert response.status_code == 409
    assert runtime.pets.state(other["id"])["pets_enabled"] == before["pets_enabled"]
    runtime.store.activate(original)
    assert runtime.pets.state(original) == before


@pytest.mark.parametrize("extra", [None, "top_level_auto_publish", "history_enabled", "bilibili_owner", "credentials"])
async def test_daily_settings_readback_or_atomic_rejection(studio, monkeypatch, extra):
    runtime, client = studio
    before = runtime.browsing.policy()
    permissions = runtime.operations.policy()
    patch = {"daily_watch_limit": 37}
    if extra:
        patch[extra] = True
    choose_tools(runtime, monkeypatch, {"action": "daily_settings", "settings_group": "browsing"},
                 {"action": "update", "changes": patch})
    response = await client.post("/api/chat/intelligent", json=turn("每天最多看三十七个视频"))
    if extra:
        assert response.status_code == 400 and runtime.browsing.policy() == before
    else:
        receipt = response.raise_for_status().json()["tool_receipt"]
        assert receipt["values"] == patch and runtime.browsing.policy()["daily_watch_limit"] == 37
    assert runtime.operations.policy() == permissions


async def test_owner_settings_cannot_be_dispatched_by_responses(runtime, store, monkeypatch):
    def forged(payload):
        names = {tool["name"] for tool in payload["tools"]}
        assert not {"pet_settings", "daily_settings", "operation_control"}.intersection(names)
        assert {"web_search", "inspect_video"}.issubset(names)
        return [call("daily_settings", {"changes": {"history_enabled": True}}, "outside")]

    gateway(runtime.settings, monkeypatch, forged)
    tasks = create_task_runtime(runtime, store, None)
    identity = str(uuid4())
    tasks.create(identity, "研究一个视频并留份记录")
    tasks.start()
    try:
        await until(lambda: tasks.current(identity)["status"] == "failed")
        assert not tasks.current(identity)["steps"]
    finally:
        await tasks.close()


@pytest.mark.parametrize(("direct", "subset", "message"), [
    ({"action": "daily_settings", "settings_group": "music"}, {"action": "update", "changes": {"cooldown_seconds": 47}},
     "把点歌冷却改成47秒，同时关闭历史引用，两项必须一起生效。"),
    ({"action": "pet_settings"}, {"action": "hide"}, "收起宠物，同时关闭回复时引用以前的私聊。"),
])
async def test_original_owner_request_blocks_partial_model_write(studio, monkeypatch, direct, subset, message):
    runtime, client = studio
    before = runtime.live.media.config(), runtime.pets.state(runtime.character["id"])
    memory = runtime.daily_memory.preference(runtime.memory_organizer.scope())
    calls = choose_tools(runtime, monkeypatch, direct, subset)
    result = (await client.post("/api/chat/intelligent", json=turn(message))).raise_for_status().json()
    assert calls[-1] in {"PetChoice", "SettingChoice"}
    assert "号主设置页" in result["reply_text"] and "tool_receipt" not in result
    assert before == (runtime.live.media.config(), runtime.pets.state(runtime.character["id"]))
    assert runtime.daily_memory.preference(runtime.memory_organizer.scope()) == memory
    assert not runtime.store.one("SELECT 1 FROM agent_tasks")


async def test_declined_mixed_request_cannot_bind_the_next_independent_task(studio, monkeypatch):
    runtime, client = studio
    choose_tools(runtime, monkeypatch, {"action": "restricted"})
    mixed = turn("把点歌冷却改成47秒，同时关闭历史引用，两项必须一起生效。")
    refused = (await client.post("/api/chat/intelligent", json=mixed)).raise_for_status().json()
    assert "号主设置页" in refused["reply_text"]
    stored = runtime.store.one("SELECT result FROM operator_chat_turns WHERE request_id=?", (mixed["request_id"],))
    assert "clarification_request" not in json.loads(stored["result"])

    async def delegate(messages, schema, **kwargs):
        assert schema["title"] == "WorkLifecycle"
        assert messages[-1] == {"role": "user", "content": goal}
        return {"action": "agent_task"}

    goal = "交给后台准备一份私有交接便笺，先查工坊当前的直播状态。"
    body = turn(goal)
    choose_tools(runtime, monkeypatch, {"action": "agent_task"})
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", delegate)
    accepted = (await client.post("/api/chat/intelligent", json=body)).raise_for_status().json()
    assert accepted["card"]["type"] == "agent_task" and runtime.tasks.current(body["request_id"])["goal"] == goal
