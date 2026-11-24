import json
from uuid import uuid4

import httpx
import pytest

from tests.support.agent_helpers import call, gateway, until
from tuber_studio.agent.tasks.tools import create_task_runtime
from tuber_studio.app import create_app


@pytest.fixture
async def studio(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        headers={"Authorization": "Bearer " + settings.admin_token},
    ) as client:
        yield app.state.runtime, client


def choose_tools(runtime, monkeypatch, direct, domain=None):
    calls = []

    async def generate(messages, schema, **options):
        title = schema["title"]
        calls.append(title)
        if title == "TurnRoute":
            if direct.get("action") == "agent_task":
                return {"route": "delegate_task", "action": "agent_task"}
            mode = "instant_read" if (domain is None or direct.get("action") == "workbench_capabilities") else "direct_action"
            return {"route": mode, "settings_read_only": domain is None, **direct}
        if title == "FrontDialogueReply":
            receipt = next((row["content"].split("以下是本轮已经取得的读取回执：", 1)[1] for row in messages
                            if "以下是本轮已经取得的读取回执：" in row["content"]), None)
            if receipt:
                return {"reply_markdown": json.loads(receipt)["summary"]}
            return {"reply_markdown": "好。"}
        return domain(messages, schema) if callable(domain) else domain

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    return calls


def turn(text):
    return {"request_id": str(uuid4()), "message": text}


@pytest.mark.parametrize(("question", "tool", "group"), [
    ("小鱼，今天怎么样了", "pet_settings", None),
    ("你在干什么呀", "daily_settings", "scene"),
    ("你觉得你能做什么梦？", "pet_settings", None),
])
async def test_unbound_settings_proposal_returns_to_chat(studio, monkeypatch, question, tool, group):
    runtime, client = studio
    calls = []

    async def generate(messages, schema, **options):
        calls.append(schema["title"])
        if schema["title"] == "TurnRoute":
            assert "可选设置分组" not in str(messages)
            return {"route": "instant_read", "action": tool, "settings_group": group, "settings_read_only": True}
        assert schema["title"] == "FrontDialogueReply"
        return {"reply_markdown": "我在这儿陪你呢。"}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    result = (await client.post("/api/v1/agent/chat", json=turn(question))).raise_for_status().json()
    assert result["reply_text"] == "我在这儿陪你呢。" and not result.get("tool_receipt")
    assert calls == ["TurnRoute", "FrontDialogueReply"]
    assert not runtime.store.one("SELECT 1 FROM agent_tasks")


async def test_owner_identity_uses_current_binding_with_stale_chat_history(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character
    runtime.store.replace_character(char | {"bilibili_owner": {"uid": "123", "name": "新号主"}})

    async def generate(messages, schema, **options):
        if schema["title"] == "TurnRoute":
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        if messages[-1]["content"] == "你住在哪里？":
            return {"reply_markdown": "我住在 Jetson 里。"}
        facts = next(row["content"] for row in messages if "私聊参与者：" in row["content"])
        assert "Jetson" not in facts
        owner = json.loads(facts.split("私聊参与者：", 1)[1])["bilibili_owner"]
        return {"reply_markdown": "我的主人是新号主呀。" if owner else "我还没有绑定 B 站主人呀。"}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    old = turn("你住在哪里？")
    (await client.post("/api/v1/agent/chat", json=old)).raise_for_status()
    for text in ("小鱼，你的主人是谁呀", "我说你的主人呀", "养你的那位叫什么名字呀？"):
        result = (await client.post("/api/v1/agent/chat", json=turn(text))).raise_for_status().json()
        assert result["reply_text"] == "我的主人是新号主呀。" and result["reply_format"] in {"plain", "markdown"}
    history = (await client.get("/api/v1/agent/chat/turns/" + old["request_id"])).raise_for_status().json()
    assert history["reply_text"] == "我住在 Jetson 里。"
    runtime.store.replace_character(char | {"bilibili_owner": None})
    result = (await client.post("/api/v1/agent/chat", json=turn("你的主人是谁？"))).raise_for_status().json()
    assert result["reply_text"] == "我还没有绑定 B 站主人呀。"


async def test_settings_followup_is_read_only_and_ends_when_topic_changes(studio, monkeypatch):
    runtime, client = studio
    first, followup, unrelated = "查一下直播画布的设置。", "那高度呢？", "今天过得怎样？"

    async def generate(messages, schema, **options):
        if schema["title"] == "FrontDialogueReply":
            if any("已经取得的读取回执" in row["content"] for row in messages):
                return {"reply_markdown": "当前直播画布设置已读到。"}
            if messages[-1]["content"] == unrelated:
                return {"reply_markdown": "还不错呀。"}
            return {"reply_markdown": "你想问哪样东西的高度呀？"}
        assert schema["title"] == "TurnRoute"
        payload = json.loads(messages[-1]["content"])
        text = payload["message"]
        if text == unrelated:
            return {"route": "chat", "action": "chat"}
        if text == followup and (payload.get("latest_tool") or {}).get("group") != "scene":
            return {"route": "chat", "action": "chat"}
        return {
            "route": "instant_read",
            "action": "daily_settings",
            "settings_group": "scene",
            "settings_read_only": True,
            "setting_fields": ["height"] if text == followup else [],
        }

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    before = runtime.live.media.scene()
    for index, text in enumerate((first, followup, unrelated, followup)):
        result = (await client.post("/api/v1/agent/chat", json=turn(text))).raise_for_status().json()
        if text == first:
            assert result["tool_receipt"]["action"] == "read"
        elif index == 1:
            assert result["tool_receipt"]["values"] == {"height": before["height"]}
        else:
            assert "tool_receipt" not in result
    assert "tool_receipt" not in result and runtime.live.media.scene() == before


async def test_capability_discovery_reads_catalog_without_settings_writes(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character["id"]
    before = runtime.pets.state(char), runtime.browsing.policy()
    calls = choose_tools(runtime, monkeypatch, {"action": "workbench_capabilities"})
    response = await client.post("/api/v1/agent/chat", json=turn("先告诉我你能调什么"))
    text = response.raise_for_status().json()["reply_text"]
    assert all(label in text for label in ("宠物", "每日观看", "点歌", "运营配图", "直播画布", "后台"))
    assert before == (runtime.pets.state(char), runtime.browsing.policy())
    assert calls == ["TurnRoute"]


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
    first = (await client.post("/api/v1/agent/chat", json=body)).raise_for_status().json()
    saved = runtime.pets.state(char)
    assert saved["selected_asset_id"] == first["tool_receipt"]["selected_asset_id"] == picked["asset_id"]
    assert picked["title"] in first["reply_text"]
    assert (await client.post("/api/v1/agent/chat", json=body)).raise_for_status().json() == first
    assert calls == ["TurnRoute", "PetChoice"] and runtime.pets.state(char) == saved


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
    response = await client.post("/api/v1/agent/chat", json=turn("收起宠物"))
    assert response.status_code == 400 and runtime.pets.state(char) == before


async def test_pet_rejects_a_settings_group_before_domain_selection(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character["id"]
    before = runtime.pets.state(char)
    calls = choose_tools(runtime, monkeypatch, {"action": "pet_settings", "settings_group": "memory"},
                         {"action": "hide"})
    response = await client.post("/api/v1/agent/chat", json=turn("让宠物歇会儿吧"))
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
    response = await client.post("/api/v1/agent/chat", json=turn("收起宠物"))
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
    response = await client.post("/api/v1/agent/chat", json=turn("每天最多看三十七个视频"))
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
async def test_original_owner_request_updates_supported_setting_without_ceremony_block(studio, monkeypatch, direct, subset, message):
    runtime, client = studio
    memory = runtime.daily_memory.preference(runtime.memory_organizer.scope())
    choose_tools(runtime, monkeypatch, direct, subset)
    result = (await client.post("/api/v1/agent/chat", json=turn(message))).raise_for_status().json()
    assert "tool_receipt" in result and "号主设置页" not in result["reply_text"]
    assert runtime.daily_memory.preference(runtime.memory_organizer.scope()) == memory
    assert not runtime.store.one("SELECT 1 FROM agent_tasks")


async def test_owner_setting_turn_cannot_bind_the_next_independent_task(studio, monkeypatch):
    runtime, client = studio
    choose_tools(runtime, monkeypatch, {"action": "daily_settings", "settings_group": "music"},
                 {"action": "update", "changes": {"cooldown_seconds": 47}})
    mixed = turn("把点歌冷却改成47秒，同时关闭历史引用。")
    updated = (await client.post("/api/v1/agent/chat", json=mixed)).raise_for_status().json()
    assert updated["tool_receipt"]["values"] == {"cooldown_seconds": 47}
    stored = runtime.store.one("SELECT result FROM operator_chat_turns WHERE request_id=?", (mixed["request_id"],))
    assert "clarification_request" not in json.loads(stored["result"])

    async def delegate(messages, schema, **kwargs):
        assert schema["title"] == "TurnRoute"
        assert json.loads(messages[-1]["content"])["message"] == goal
        return {"route": "delegate_task", "action": "agent_task"}

    goal = "交给后台准备一份私有交接便笺，先查工坊当前的直播状态。"
    body = turn(goal)
    choose_tools(runtime, monkeypatch, {"action": "agent_task"})
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", delegate)
    accepted = (await client.post("/api/v1/agent/chat", json=body)).raise_for_status().json()
    assert accepted["card"]["type"] == "agent_task" and runtime.tasks.current(body["request_id"])["goal"] == goal
