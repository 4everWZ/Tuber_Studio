"""Private history from authenticated chat through an exact stored artifact, never an action grant."""
import json
import os
from uuid import uuid4

import httpx
import pytest
from test_browsing import ControlledPlatform

from tuber_studio.agent.owner_memory import history_preference
from tuber_studio.app import create_app

CHAT = "/api/chat/intelligent"
TASKS = "/api/agent/tasks"
BV = "BV1A12345678"
URL = "https://archive.example/field-note"
REASON = "入场前有四秒静默，留出了报幕的时间。"
ORIGINAL = f"**入场**\n选材理由：{REASON}\n{BV} P2 · 24 秒\n<{URL}>"


@pytest.fixture
async def studio(settings):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        yield client, app.state.runtime
    assert not platform.sent
    app.state.store.close()


async def seed(client, runtime):
    root, task = str(uuid4()), str(uuid4())
    for identity, goal, parent in ((root, "秋季直播", None), (task, "入场画面的选择", root)):
        (await client.post(TASKS, json={"request_id": identity, "goal": goal, "creative_goal_id": parent})).raise_for_status()
        if not parent:
            (await client.put(f"{TASKS}/{root}/goal")).raise_for_status()
    tasks = runtime.tasks
    with runtime.store.db as db:
        tasks.workspace.record_delivery(db, tasks.current(task), ORIGINAL)
    (await client.patch(f"{TASKS}/{task}/artifact", json={"task_revision": 1, "edition": 1,
                                                           "body": "号主后来换成了尾声。"})).raise_for_status()
    return root, task


@pytest.mark.parametrize("anchor", ["id", "bv", "source", "phrase", "video_url", "project"])
async def test_old_edition_from_private_chat_is_openable_and_read_only(studio, monkeypatch, anchor):
    client, runtime = studio
    root, task = await seed(client, runtime)
    for i in range(54):
        runtime.tasks.create(str(uuid4()), f"素材清点 {i}")
    assert task not in [t["id"] for t in (await client.get(TASKS)).json()["tasks"]]
    runtime.store.replace_character(runtime.character | {"name": "修订后的角色卡"})
    assert runtime.tasks.current(task) and not runtime.tasks.executable(task)
    query = {"id": f"说说 {task} r1 v1 的工作理由", "bv": f"我上次为什么选 {BV} 那段？",
             "source": f"当时为什么采纳 <{URL}>？", "phrase": "之前为什么想留出报幕的时间？",
             "video_url": f"https://www.bilibili.com/video/{BV} 的选段呢？",
             "project": "秋季直播当时为了报幕的时间怎么选？"}[anchor]
    routed = "查当初为何作出某个选择的原文"

    async def generate(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        packet = json.loads(messages[1 if schema["title"] == "CardTurnIntent" else -1]["content"])
        if schema["title"] == "CardTurnIntent":
            candidate, = [c for c in packet["history_task_cards"] if c["id"] == task]
            assert candidate["artifact"] == {"task_revision": 1, "edition": 1}
            assert REASON not in str(messages)
            return {"intent": routed, "reference": task, "patch": None}
        assert schema["title"] == "ReasonSelection"
        assert packet["versions"][0]["source"][1] == {"line": 2, "text": "选材理由：" + REASON}
        assert schema["properties"]["lines"]["items"]["enum"] == [2]
        assert kwargs["on_content"] is None
        return {"edition": 1, "evidence": "recorded", "lines": [2]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    turn_id = str(uuid4())
    request = {"request_id": turn_id, "message": query}
    if anchor != "project":
        request["creative_goal_id"] = root
    explicit = {"intent": {"action": "chat"}} if anchor == "id" else {}
    answer = (await client.post(CHAT, json=request | explicit)).raise_for_status().json()
    assert answer["reply_text"] == "角色交付稿 r1 v1原文\n第 2 行：选材理由：" + REASON
    assert answer["reply_format"] == "plain" and answer["card"]["type"] == "history_artifact"
    original = f"{CHAT}/turns/{turn_id}/artifact"
    assert (await client.get(original)).raise_for_status().json()["body"] == ORIGINAL
    monkeypatch.setattr(runtime.live, "state", "live")
    for action in ("dynamic", "live_handoff"):
        public = await client.post(CHAT, json={"request_id": str(uuid4()), "intent": {"action": action, "source": "discussion"},
                                               "message": "提取刚才讨论给观众听"})
        assert public.raise_for_status().json()["card"] is None and REASON not in public.text
    # A later deictic question retains the discussed edition, including after the account changes.
    old_account = runtime.tasks.scope()[0]
    runtime.browsing.discovery.remember_account("next-account")
    assert runtime.tasks.scope()[0] != old_account
    again = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": "我上次为什么选那段？"})).raise_for_status()
    assert again.json()["card"] == answer["card"]
    routed = "实际修改同一任务或成果的部分或全部"
    response = (await client.post(CHAT, json=request | {"request_id": str(uuid4())})).raise_for_status().json()
    assert response.get("task_reference") is None and not runtime.tasks.executable(task)
    assert runtime.tasks.current(task)["revision"] == 1
    history_preference(runtime, False)
    assert REASON not in (await client.get(f"{CHAT}/turns/{turn_id}")).text
    assert (await client.get(original)).status_code == 404
    history_preference(runtime, True)
    runtime.store.save_character(runtime.character | {"id": "another-role"})
    runtime.store.activate("another-role")
    assert (await client.get(original)).status_code == 404


async def test_history_boundaries_on_model_input_and_stream_delivery(studio, monkeypatch):
    client, runtime = studio
    root, task = await seed(client, runtime)
    seen = []
    revoke = False

    async def generate(messages, schema, **kw):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        seen.append(messages)
        if schema["title"] == "CardTurnIntent":
            return {"intent": "查当初为何作出某个选择的原文", "reference": task}
        if revoke:
            assert REASON in messages[-1]["content"] and schema["title"] == "ReasonSelection"
            history_preference(runtime, False)
            assert kw["on_content"] is None
            return {"edition": 1, "evidence": "recorded", "lines": [2]}
        return {"action": "chat", "reply_markdown": "好呀"}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)

    async def check_isolated(**options):
        seen.clear()
        (await client.post(CHAT, json={"message": URL, "request_id": str(uuid4()), **options})).raise_for_status()
        assert len(seen) == 1
        assert "history_task_cards" not in str(seen) and REASON not in str(seen) and task not in str(seen)

    await check_isolated(temporary=True)
    await check_isolated(creative_goal_id=str(uuid4()))
    history_preference(runtime, False)
    await check_isolated()
    history_preference(runtime, True)
    revoke = True
    identity = str(uuid4())
    stream = await client.post(CHAT + "/stream", json={"message": URL, "request_id": identity})
    assert "event: error" in stream.text and "event: delta" not in stream.text and REASON not in stream.text
    assert not runtime.store.one("SELECT 1 FROM operator_chat_turns WHERE request_id=?", (identity,))


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Chromium")
async def test_chromium_chat_opens_original_edition_and_source(studio, monkeypatch):
    import sys

    from playwright.async_api import async_playwright

    client, runtime = studio
    _, task = await seed(client, runtime)
    runtime.store.replace_character(runtime.character | {"name": "编辑之后"})

    async def generate(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        if schema["title"] == "CardTurnIntent":
            assert task in schema["properties"]["reference"]["anyOf"][0]["enum"]
            return {"intent": "查当初为何作出某个选择的原文", "reference": task}
        assert schema["title"] == "ReasonSelection"
        return {"edition": 1, "evidence": "recorded", "lines": [2]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    opened = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge" if sys.platform == "win32" else "chromium",
                                                   chromium_sandbox=True)
        context = await browser.new_context(service_workers="block")

        async def serve(route):
            url = httpx.URL(route.request.url)
            if url.host != "127.0.0.1":
                return await route.abort()
            response = await client.request(route.request.method, str(url), content=route.request.post_data_buffer,
                                            headers=route.request.headers)
            if url.path.endswith("/artifact"):
                opened.append((url.path, response.status_code))
            await route.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get("content-type"))

        await context.route("**/*", serve)
        page = await context.new_page()
        try:
            await page.goto("http://127.0.0.1")
            await page.locator("#operator-chat-input").fill(f"我上次为什么选 {BV} 那段？")
            await page.locator("#operator-chat-send").click()
            summary = page.get_by_text("查看引用原稿 · r1 v1", exact=True)
            await summary.click(timeout=15000)
            visible_reply = page.locator(".operator-chat-turn .operator-chat-reply").last
            assert await visible_reply.text_content() == "角色交付稿 r1 v1原文\n第 2 行：选材理由：" + REASON
            link = page.get_by_role("link", name=URL, exact=True)
            await link.wait_for()
            assert await link.get_attribute("href") == URL
            assert "角色交付" in await summary.locator("..").inner_text()
            assert REASON in await summary.locator("..").inner_text()
            assert "后来换成了尾声" not in await summary.locator("..").inner_text()
            assert len(opened) == 1 and opened[0][1] == 200
            await page.reload()
            await page.get_by_text("查看引用原稿 · r1 v1", exact=True).click()
            await link.wait_for()
            assert opened == [opened[0], opened[0]]
        finally:
            await browser.close()


def test_existing_business_records_are_indexed_without_mapping_unbound_scopes(store):
    from types import SimpleNamespace

    from test_agent_task_identity import studio_tasks

    from tuber_studio.agent.task_history import TaskHistory

    tasks = studio_tasks(store)
    identity = str(uuid4())
    task = tasks.create(identity, "最初交办")
    with store.db as db:
        tasks.workspace.record_delivery(db, task, ORIGINAL)
    index = TaskHistory(SimpleNamespace(store=store, tasks=tasks))
    assert index.candidates(URL)[0]["id"] == identity
    assert not index.candidates(URL + "-different")
    other = tasks.create(str(uuid4()), "不可归属")
    with store.db as db:
        tasks.workspace.record_delivery(db, other, ORIGINAL)
        db.execute("UPDATE agent_tasks SET scope='[]' WHERE id=?", (other["id"],))
    assert [card["id"] for card in index.candidates(BV)] == [identity]
    assert not index.read(other["id"])
    # Losing permission to recall an edition also suppresses exact matches in the index.
    store.set(f"private_edition_memory:{identity}:1", [{"ids": [7], "stamp": "a" * 64}])
    assert not index.candidates(URL) and not index.read(identity, {"task_revision": 1, "edition": 1})


async def test_unfinished_card_remains_discussable_after_only_account_switch(studio, monkeypatch):
    client, runtime = studio
    task = str(uuid4())
    (await client.post(CHAT, json={"request_id": task, "message": "等待下次彩排",
                                   "intent": {"action": "agent_task"}})).raise_for_status()
    runtime.browsing.discovery.remember_account("switched")
    assert runtime.tasks.current(task) and not runtime.tasks.executable(task)
    async def generate(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        packet = json.loads(messages[1 if schema["title"] == "CardTurnIntent" else -1]["content"])
        if schema["title"] == "CardTurnIntent":
            assert "recent_task_cards" not in packet
            assert [c["id"] for c in packet["history_task_cards"]] == [task]
            return {"intent": "只回答当前问题或假设", "reference": task}
        assert packet["task"]["goal"] == "等待下次彩排"
        return {"action": "chat", "reply_markdown": "还在等待。"}
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    response = await client.post(CHAT, json={"request_id": str(uuid4()), "message": "等待下次彩排那项进度呢？"})
    assert response.raise_for_status().json()["task_reference"] == task
    assert runtime.tasks.current(task)["revision"] == 1


async def test_reason_question_can_open_the_agent_original_after_an_owner_edit(studio, monkeypatch):
    client, runtime = studio
    _, task = await seed(client, runtime)

    async def choose(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        packet = json.loads(messages[1 if schema["title"] == "CardTurnIntent" else -1]["content"])
        if schema["title"] == "CardTurnIntent":
            card, = packet["history_task_cards"]
            assert card["artifact"]["edition"] == 2 and card["original_edition"]["author"] == "agent"
            assert "read_original" in schema["required"]
            assert card["matched_edition"]["author"] == "owner"
            assert not card["matched_edition"]["has_rationale"] and card["original_edition"]["has_rationale"]
            assert REASON not in str(packet)
            return {"intent": "查当初为何作出某个选择的原文", "reference": task, "read_original": False}
        assert {v["edition"] for v in packet["versions"]} == {1, 2}
        return {"edition": 1, "evidence": "recorded", "lines": [2]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", choose, raising=False)
    result = await client.post(CHAT, json={"request_id": str(uuid4()), "message": "我上次为什么选那段？"})
    assert result.raise_for_status().json()["card"]["data"] == {"task_revision": 1, "edition": 1}


@pytest.mark.parametrize(("edition", "state", "numbers", "expected"), [
    (1, "recorded", [2, 3], "角色交付稿 r1 v1里没有可核对的对应理由。"),
    (1, "missing", [2], "角色交付稿 r1 v1里没有可核对的对应理由。"),
    (2, "recorded", [1], "号主编辑稿 r1 v2原文\n第 1 行：我选择尾声，因为台词在这里才说完。"),
])
async def test_only_verified_reason_lines_from_the_selected_version(studio, monkeypatch, edition, state, numbers, expected):
    client, runtime = studio
    task_id = str(uuid4())
    task = runtime.tasks.create(task_id, "节目素材")
    with runtime.store.db as db:
        runtime.tasks.workspace.record_delivery(db, task, ORIGINAL)
    (await client.patch(f"{TASKS}/{task_id}/artifact", json={"task_revision": 1, "edition": 1,
                        "body": "我选择尾声，因为台词在这里才说完。"})).raise_for_status()

    async def select(messages, schema, **options):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        assert options["on_content"] is None
        if schema["title"] == "CardTurnIntent":
            return {"intent": "查当初为何作出某个选择的原文", "reference": task_id}
        assert schema["title"] == "ReasonSelection"
        assert schema["properties"]["edition"]["enum"] == [edition]
        return {"edition": edition, "evidence": state, "lines": numbers}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", select, raising=False)
    response = await client.post(CHAT, json={"request_id": str(uuid4()), "message": f"{task_id} r1 v{edition} 为什么这样选？"})
    answer = response.raise_for_status().json()
    assert answer["reply_text"] == expected
    assert answer["reply_format"] == "plain" and answer["card"]["data"]["edition"] == edition

async def test_chat_opened_edition_read_and_stale_write(studio, monkeypatch):
    client, runtime = studio
    identity = str(uuid4())
    (await client.post(CHAT, json={"request_id": identity, "message": "准备直播提纲",
                                   "intent": {"action": "agent_task"}})).raise_for_status()
    with runtime.store.db as db:
        runtime.tasks.workspace.record_delivery(db, runtime.tasks.current(identity), ORIGINAL)
    opened = {"task_id": identity, "task_revision": 1, "edition": 1}
    (await client.patch(f"{TASKS}/{identity}/artifact", json={"task_revision": 1, "edition": 1,
                                                              "body": "号主另存的第二版。"})).raise_for_status()
    change = "把这份的尾声缩短。"

    async def generate(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle" and "task_turn" in schema["properties"]["action"]["enum"]:
            return {"action": "task_turn"}
        if schema["title"] == "CardTurnIntent":
            editing = messages[-1]["content"] == change
            return {"intent": "实际修改同一任务或成果的部分或全部" if editing else "查当初为何作出某个选择的原文",
                    "patch": {"field": "goal", "subject": "这份", "requirement": "缩短"} if editing else None, "reference": identity, "read_original": False}
        versions = json.loads(messages[-1]["content"])["versions"]
        assert [v["edition"] for v in versions] == [1]
        return {"edition": 1, "evidence": "recorded", "lines": [2]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    request = {"request_id": str(uuid4()), "message": "这份当时为什么这么选？", "opened_artifact": opened}
    read = (await client.post(CHAT, json=request)).raise_for_status().json()
    assert read["task_artifact"] == {"task_revision": 1, "edition": 1} and REASON in read["reply_text"]
    stale = await client.post(CHAT, json=request | {"request_id": str(uuid4()), "message": change})
    assert stale.status_code == 409 and runtime.tasks.current(identity)["revision"] == 1
