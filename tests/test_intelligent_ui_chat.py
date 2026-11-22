import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_task_recovery import wait_status
from test_browsing import BV, ControlledPlatform
from test_browsing_evidence import PNG, TestVisual

from tuber_studio.agent import operator_facts, task_delegation
from tuber_studio.agent.assistant_output import AssistantTextStream, assistant_markdown
from tuber_studio.agent.operator_task_patch import TaskPatch, grounded_patch, patch_vocabulary
from tuber_studio.app import create_app
from tuber_studio.core.identity import provider_identity_messages
from tuber_studio.core.models import Reply
from tuber_studio.infra.inference.providers import Audio
from tuber_studio.live.game_livestream import GameLivestreamService
from tuber_studio.operations.browsing.models import BrowsingPolicy
from tuber_studio.operations.browsing.repository import BrowsingRepository


async def test_chat_delegation_survives_interleaved_conversation_and_delivers_latest_revision(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    goal = "核对演播间状态并给我一份交接记录"
    casual = "先别改角色身份。我只是好奇，住在钟楼里你会把窗开在哪边？"
    change = "刚才那份交接只留状态和检查时间"
    task_id, separate_id = str(uuid4()), str(uuid4())
    began, settle = asyncio.Event(), asyncio.Event()
    result_text = "演播间待命。检查时间 09:30。"

    async def generate(messages, schema, **options):
        if schema["title"] == "FrontDialogueReply":
            assert messages[-1]["content"] == casual
            return {"reply_markdown": "窗朝东，早上可以晒到太阳。"}
        assert schema["title"] == "TurnRoute"
        packet = json.loads(messages[-1]["content"])
        message = packet["message"]
        if message == goal:
            return {"route": "delegate_task", "action": "agent_task",
                    "brief": {"goal": "核对演播间状态", "deliverable": "一份私有交接记录"}}
        if message == change:
            assert task_id in json.dumps(packet.get("recent_task_cards", []))
            return {"route": "steer_task", "action": "steer_task", "task_id": task_id,
                    "task_effect": "revise", "change_quote": "只留状态和检查时间", "replacement_goal": "",
                    "brief": {"goal": "核对演播间状态", "deliverable": "附检查时间的交接记录", "read_tools": ["live_status"]}}
        assert message == casual
        return {"route": "chat", "action": "chat"}

    async def step(task):
        if task["id"] == separate_id:
            assert runtime.tasks._save(task, status="completed", message="独立工作已完成。")
        elif task["revision"] == 1:
            assert task["plan"]["delegation_brief"]["deliverable"] == "一份私有交接记录"
            began.set()
            try:
                await settle.wait()
            finally:
                assert not runtime.tasks._persist(task, {"status": "completed", "message": "过期结果"})
        else:
            assert task["revision"] == 2 and task["goal"] == goal + "\n号主改口：" + change
            assert task["plan"]["delegation_brief"]["read_tools"] == ["live_status"]
            assert runtime.tasks._save(task, status="completed", message=result_text)

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    monkeypatch.setattr(runtime.tasks, "_step", step)
    path = "/api/chat/intelligent"
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1",
    ) as client:
        async def send(message, identity=None):
            return (await client.post(path, json={"request_id": identity or str(uuid4()), "message": message})).raise_for_status().json()

        assert (await send(goal, task_id))["card"]["data"]["revision"] == 1
        await asyncio.wait_for(began.wait(), 5)
        runtime.tasks.create(separate_id, "单独核对另一个对象")
        await wait_status(runtime.tasks, separate_id, "completed")
        assert (await send(casual))["reply_text"] == "窗朝东，早上可以晒到太阳。"
        assert len(runtime.tasks.activity()) == 2
        # Presentation of the original acknowledgement is independent of the delivery address.
        assert (await send(change))["card"]["data"]["revision"] == 2
        origin = json.loads(runtime.store.one("SELECT result FROM operator_chat_turns WHERE request_id=?", (task_id,))["result"])
        assert origin.pop("card")["data"]["id"] == origin["task_origin"] == task_id
        runtime.store.execute("UPDATE operator_chat_turns SET result=? WHERE request_id=?", (json.dumps(origin), task_id))
        await wait_status(runtime.tasks, task_id, "completed")
        for _ in range(2):
            turns = (await client.get(path)).raise_for_status().json()["turns"]
            assert len(turns) == 4
            deliveries = [turn for turn in turns if turn.get("assistant_only")]
            assert len(deliveries) == 1 and deliveries[0]["reply_text"] == result_text
            assert deliveries[0]["task_delivery"]["revision"] == 2
            assert deliveries[0] == (await client.get(path + "/turns/" + deliveries[0]["request_id"])).json()
            assert "过期结果" not in str(turns)


async def test_clear_private_chat_removes_other_login_placeholders_and_rejects_old_requests(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    owner, char = runtime.tasks.identity.principal, runtime.character
    account = "123"
    runtime.browsing.discovery.remember_account(account)
    path = "/api/chat/intelligent"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        request_id = str(uuid4())
        original = {"request_id": request_id, "message": "整理舞台交接记录", "intent": {"action": "agent_task"}}
        (await client.post(path, json=original)).raise_for_status()
        for character, principal, login in ((char["id"], owner, "456"), (char["id"], "another-owner", account),
                                           ("another-character", owner, account)):
            runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
                str(uuid4()), character, char["version"], "别处历史",
                json.dumps({"account_id": login, "owner_source": principal, "reply_text": "别处内容"}), time.time(),
            ))
        before = (await client.get(path)).raise_for_status().json()
        assert [turn["request_id"] for turn in before["turns"]] == [request_id]
        cleared = (await client.delete(path)).raise_for_status().json()
        assert before["conversation_id"] != cleared["conversation_id"]
        assert not (await client.get(path)).json()["turns"]
        assert (await client.post(path, json=original)).status_code == 409
        assert (await client.post(path, json=original | {"request_id": str(uuid4()),
                                  "conversation_id": before["conversation_id"]})).status_code == 409
        active = runtime.tasks.executable(request_id)
        assert runtime.tasks._save(active, status="completed", message="原任务结果")
        runtime.browsing.discovery.remember_account("456")
        assert not (await client.get(path)).json()["turns"]
        assert len(runtime.store.all("SELECT * FROM operator_chat_turns")) == 2


async def test_omni_uses_workbench_fields_without_changing_conversational_routing(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    calls = []

    async def model(messages, schema, **options):
        if schema["title"] == "TurnRoute":
            calls.append(schema["properties"]["route"]["enum"])
            message = json.loads(messages[-1]["content"])["message"]
            if message == "今天过得好吗？":
                return {"route": "chat", "action": "chat"}
            return {"route": "chat", "action": "chat", "clock_fields": ["local_date"]}
        assert schema["title"] == "FrontDialogueReply"
        current = messages[-1]["content"]
        if current == "今天过得好吗？":
            return {"reply_markdown": "蛮好，刚想去看海。"}
        assert any("本轮查询的工坊日历值：" in row["content"] for row in messages)
        return {"reply_markdown": "今天是 {{local_date}}。"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", model, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        path = "/api/chat/intelligent"
        for message in ("今天过得好吗？", "日历上的日期是什么？"):
            result = (await client.post(path, json={"request_id": str(uuid4()), "message": message})).raise_for_status().json()
            if message.startswith("今天"):
                assert not result.get("tool_receipt")
            else:
                clock = operator_facts.now(app.state.browsing)
                assert result["reply_text"] == f"今天是 {clock.year}年{clock.month}月{clock.day}日。"
        assert len(calls) == 2 and calls[0] == calls[1]


def test_incremental_assistant_output_handles_json_escapes_and_excludes_tool_fields():
    for wire, expected in [
        (json.dumps({"action": "chat", "reply_markdown": '鲸鲸 🌊\n"白饭"', "topic": "内部字段"}), '鲸鲸 🌊\n"白饭"'),
        (json.dumps({"action": "chat", "reply_markdown": '快来啦。”}”}'}), '快来啦。'),
        (json.dumps({"action": "stop", "reply_markdown": "不应显示"}), ""),
    ]:
        fragments = []
        stream = AssistantTextStream(fragments.append)
        for char in wire:
            stream.feed(char)
        assert "".join(fragments) == expected
    assert assistant_markdown('这是对象 {"值": 1}') == '这是对象 {"值": 1}'
    assert assistant_markdown('```js\n}\n```') == '```js\n}\n```'


def test_plain_assistant_stream_sends_prose_without_transport_framing():
    visible = []
    stream = AssistantTextStream(visible.append, plain=True)
    stream.feed("半碗，")
    stream.feed("还给你留了一口。")
    stream.feed("”}")
    assert visible == ["半碗，", "还给你留了一口。"]


def test_partial_discriminator_or_reply_first_cannot_open_chat_stream():
    visible = []
    stream = AssistantTextStream(visible.append)
    stream.feed('{"action":"ch')
    assert not visible
    stream.feed('at","reply_markdown":"可以')
    assert visible == ["可以"]
    visible.clear()
    stream = AssistantTextStream(visible.append)
    stream.feed('{"reply_markdown":"还不能显示",')
    assert not visible
    stream.feed('"action":"agent_task"}')
    assert not visible


def test_assistant_stream_persists_only_final_text_and_retries_under_same_id(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    calls = []

    async def generate(messages, schema, *, max_tokens, on_content, on_stage):
        assert all("旧报告的第三件" not in message["content"] and "旧模型刚才的错答" not in message["content"]
                   for message in messages)
        if schema["title"] == "TurnRoute":
            assert on_content is None
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        calls.append(messages)
        assert messages[-1] == {"role": "user", "content": "鱼鱼今天吃了几碗饭"}
        assert "reply_markdown" in schema["properties"]
        assert not any("可用能力" in row["content"] or "WorkLifecycle" in row["content"] for row in messages)
        on_stage("generating")
        on_content('{"reply_markdown":"**白饭')
        on_content('**，分你半碗。"}')
        return {"reply_markdown": "**白饭**，分你半碗。"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        char = app.state.runtime.character
        for message, saved, when in [
            ("今天运营发生什么", {"card": {"type": "operation_events", "data": {"items": [
                {"title": "旧报告的第三件", "reference": "old/1", "source": {"kind": "comment", "text": "旧原话"}},
            ]}}}, 1),
            ("之前的聊天", {"private": True, "reply_text": "旧模型刚才的错答"}, 2),
        ]:
            app.state.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
                str(uuid4()), char["id"], char["version"], message, json.dumps(saved | {"account_id": ""}),
                time.time() - 50 + when,
            ))
        payload = {"request_id": str(uuid4()), "message": "鱼鱼今天吃了几碗饭"}
        response = client.post(path + "/stream", json=payload)
        assert response.status_code == 200
        assert 'event: delta\ndata: "**白饭"' in response.text
        assert 'event: delta\ndata: "**，分你半碗。"' in response.text
        assert "event: done" in response.text
        assert client.post(path, json=payload).json()["reply_text"] == "**白饭**，分你半碗。"
        assert len(calls) == 1
        assert len(client.get(path).json()["turns"]) == 3


def test_assistant_rejects_invalid_tool_payload_before_dispatch(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())

    async def generate(messages, schema, *, max_tokens, on_content, on_stage):
        assert schema["title"] == "TurnRoute"
        return {"route": "direct_action", "action": "inspect_video_frame", "part": 0, "seconds": 1}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 52000)) as client:
        response = client.post("/api/chat/intelligent/stream", json={
            "request_id": str(uuid4()), "message": "帮我看一下视频中的一帧",
        })
        assert "event: error" in response.text
        assert "event: done" not in response.text
        assert not app.state.store.one("SELECT 1 FROM browsing_rounds")


@pytest.mark.parametrize(("lifecycle", "tool", "expected_card"), [
    ("delegate_task", None, "agent_task"), ("instant_read", "live_status", "live_studio_hud"),
    ("live_status", None, None), ("direct_action", "agent_task", None),
])
async def test_chat_lifecycle_separates_live_status_from_persistent_work(settings, monkeypatch, lifecycle, tool, expected_card):
    app = create_app(settings, adapter=ControlledPlatform())
    calls = []

    async def generate(messages, schema, **options):
        calls.append(schema["title"])
        assert schema["title"] == "TurnRoute"
        assert options["on_content"] is None
        if lifecycle == "delegate_task":
            return {"route": "delegate_task", "action": "agent_task",
                    "brief": {"goal": "核对直播状态", "deliverable": "交接记录"}}
        if lifecycle == "instant_read":
            return {"route": "instant_read", "action": tool}
        if tool is None:
            return {"route": lifecycle}
        return {"route": lifecycle, "action": tool}

    monkeypatch.delenv("TUBER_NATIVE_PROGRAM_STATUS_URL", raising=False)
    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        identity = str(uuid4())
        response = await client.post("/api/chat/intelligent", json={
            "request_id": identity, "message": "核对一下直播状态" + ("，给我留份交接记录" if lifecycle == "delegate_task" else ""),
        })
        assert calls == ["TurnRoute"]
        if expected_card:
            result = response.raise_for_status().json()
            assert result["card"]["type"] == expected_card
            if tool == "live_status":
                assert result["reply_text"] == "直播间当前未开播。"
                assert result["tool_receipt"]["values"]["state"] == "stopped"
        else:
            assert response.status_code == 503
            assert not app.state.store.one("SELECT 1 FROM operator_chat_turns")
        assert bool(app.state.runtime.tasks.current(identity)) == (expected_card == "agent_task")


@pytest.mark.parametrize(("route", "card_type"), [
    ("delegate_task", "agent_task"), ("steer_task", "agent_task"),
    ("instant_read", "live_studio_hud"), ("clarify", None),
])
async def test_nonchat_acknowledgement_is_neither_streamed_nor_saved(settings, monkeypatch, route, card_type):
    app = create_app(settings, adapter=ControlledPlatform())
    hidden = "这段未核验的应答不能对号主显示"
    previous = str(uuid4())

    async def generate(messages, schema, *, on_content, **options):
        assert schema["title"] == "TurnRoute" and on_content is None
        if route == "delegate_task":
            return {"route": "delegate_task", "action": "agent_task",
                    "brief": {"goal": "核对 P2 的直播素材", "deliverable": "素材核对记录"}}
        if route == "steer_task":
            return {"route": "steer_task", "action": "steer_task", "task_id": previous,
                    "task_effect": "revise", "change_quote": "核对 P2"}
        if route == "instant_read":
            return {"route": "instant_read", "action": "live_status"}
        return {"route": "clarify", "action": "clarify", "question": "你指的是哪段素材？"}

    monkeypatch.delenv("TUBER_NATIVE_PROGRAM_STATUS_URL", raising=False)
    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    path = "/api/chat/intelligent"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        if route == "steer_task":
            (await client.post(path, json={"request_id": previous, "message": "预备素材",
                                           "intent": {"action": "agent_task"}})).raise_for_status()
        identity = str(uuid4())
        result = await client.post(path + "/stream", json={"request_id": identity, "message": "核对 P2 的直播素材"})
        assert "event: done" in result.text and "event: error" not in result.text
        assert "event: delta" not in result.text and hidden not in result.text
        turn = (await client.get(path + "/turns/" + identity)).raise_for_status().json()
        assert (turn["card"] or {}).get("type") == card_type
        if route == "steer_task":
            assert turn["card"]["data"]["id"] == previous and turn["card"]["data"]["revision"] == 2
        assert hidden not in str(turn)


@pytest.mark.parametrize("replace", [False, True])
async def test_chat_projects_only_active_goal_and_evidence_after_steer(settings, monkeypatch, replace):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    identity = str(uuid4())
    path = "/api/chat/intelligent"
    original = (f"写一份便笺，取 {BV} P2，给没看过的观众，不能剧透。" if not replace else
                "交给后台准备一份私有交接便笺，先查工坊当前的直播状态。")
    latest = "改为只检查自主运营是否启动，写进同一份便笺" if replace else "把第二段写短一点"
    message = f"继续刚才的那份便笺，{latest}。"

    async def decide(messages, schema, **options):
        assert schema["title"] == "TurnRoute"
        return {
            "route": "steer_task",
            "action": "steer_task",
            "task_id": identity,
            "task_effect": "revise",
            "change_quote": latest,
            "replacement_goal": latest if replace else "",
        }

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", decide, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "message": original,
                                       "intent": {"action": "agent_task"}})).raise_for_status()
        old = tasks.current(identity)
        read = {"id": "old-source", "tool": "web_page", "revision": 1, "arguments": {"web_fetch": "https://example.org"},
                "result": {"excerpt": "旧来源正文", "url": "https://example.org"}}
        tasks._save(old, plan={"backend": "responses", "prior_goals": []},
                    steps=[read], status="completed", message="已保存的原版正文")
        reply = (await client.post(path, json={"request_id": str(uuid4()), "message": message})).raise_for_status()
        assert reply.json()["card"]["data"]["revision"] == 2

    current = tasks.current(identity)
    context = task_delegation.visible_steps(tasks, current)
    packet = json.loads(task_delegation.model_input(current, context)[1]["content"])
    assert current["steps"] == [read] and tasks.workspace.latest(identity)["body"] == "已保存的原版正文"
    if replace:
        assert current["goal"] == packet["goal"] == latest
        assert packet["prior_goals"] == [] and not packet["previous_delivery"] and not packet["saved_evidence"]
        assert current["plan"]["superseded_goals"][-1]["goal"] == original
        await tasks.control(identity, 2, "steer", "再压缩成一段")
        current = tasks.current(identity)
        assert current["plan"]["prior_goals"] == [latest]
        assert task_delegation.visible_steps(tasks, current) == []
    else:
        assert packet["prior_goals"] == [original] and latest in packet["goal"]
        assert task_delegation.video_binding(current) == {BV: {2: None}}
        assert "不能剧透" in packet["goal"] and context == [read]
        assert packet["previous_delivery"] == "已保存的原版正文"


async def test_semantic_task_cards_bind_clarified_steering_without_approval(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    path = "/api/chat/intelligent"
    ids = [str(uuid4()) for _ in range(4)]
    texts = ["给下场暖场留两段可选的，别发", "这周的选题也备一份", "第二段换成轻松点的", "我是指暖场那份"]

    async def decide(messages, schema, **options):
        assert schema["title"] == "TurnRoute" and options["on_content"] is None
        packet = json.loads(messages[-1]["content"])
        index = texts.index(packet["message"])
        if index == 0:
            assert not packet.get("recent_task_cards")
            return {"route": "delegate_task", "action": "agent_task",
                    "brief": {"goal": texts[0], "deliverable": "暖场两段可选内容"}}
        expected = [ids[0]] if index == 1 else [ids[1], ids[0]]
        assert [card["id"] for card in packet["recent_task_cards"]] == expected
        if index == 1:
            return {"route": "delegate_task", "action": "agent_task",
                    "brief": {"goal": texts[1], "deliverable": "本周选题"}}
        if index == 2:
            return {"route": "steer_task", "action": "steer_task", "task_id": None,
                    "task_effect": "revise", "change_quote": "换成轻松点的"}
        assert packet["clarification"]["request"] == texts[2]
        return {"route": "steer_task", "action": "steer_task", "task_id": ids[0],
                "task_effect": "revise", "change_quote": "换成轻松点的",
                "clarification_id": packet["clarification"]["id"]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", decide, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        for index, (identity, message) in enumerate(zip(ids, texts)):
            payload = {"message": message, "request_id": identity}
            response = await client.post(path, json=payload)
            assert response.status_code == 200, response.text
            if index == 2:
                assert response.json()["card"] is None
                assert runtime.tasks.current(ids[0])["revision"] == 1
        task = response.json()["card"]["data"]
        assert task["id"] == ids[0] and task["revision"] == 2 and task["steps"] == []
        assert all(text in task["goal"] for text in [texts[0], texts[2], texts[3]])
        assert runtime.tasks.current(ids[1])["revision"] == 1
        assert (await client.post(path, json=payload)).json()["card"]["data"]["revision"] == 2


@pytest.mark.parametrize("patch", [
    None,
    {"field": "goal", "subject": "便笺", "requirement": "改成逐日统计"},
])
async def test_unverified_card_revision_finishes_without_a_chat_success_claim(settings, monkeypatch, patch):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    path = "/api/chat/intelligent"
    identity, turn_id = str(uuid4()), str(uuid4())
    original = "交给后台准备一份私有交接便笺，先查工坊当前的直播状态。"
    request = {"request_id": turn_id, "message": "继续刚才的那份便笺，改为只检查自主运营是否启动，写进同一份便笺。"}
    stages = []

    async def frozen(messages, schema, **options):
        stages.append(schema["title"])
        assert schema["title"] == "TurnRoute"
        return {
            "route": "steer_task",
            "action": "steer_task",
            "task_id": identity,
            "task_effect": "revise",
            "change_quote": "" if patch is None else patch["requirement"],
        }

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", frozen, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "message": original,
                                       "intent": {"action": "agent_task"}})).raise_for_status()
        tasks._save(tasks.current(identity), status="running")
        reply = (await client.post(path, json=request)).raise_for_status().json()
        assert stages == ["TurnRoute"]
        assert reply["card"] is None and "没有修改" in reply["reply_text"] and "请" in reply["reply_text"]
        assert (await client.post(path, json=request)).raise_for_status().json() == reply
        assert stages == ["TurnRoute"]
    task = tasks.current(identity)
    assert (task["goal"], task["status"], task["revision"], task["steps"]) == (original, "running", 1, [])
    assert tasks.workspace.latest(identity) is None


@pytest.mark.parametrize("ask_about_card", [True, False])
async def test_discussing_a_card_or_chatting_without_material_does_not_steer(settings, monkeypatch, ask_about_card):
    app = create_app(settings, adapter=ControlledPlatform())
    path, identity = "/api/chat/intelligent", str(uuid4())
    stages = []

    async def generate(messages, schema, *, on_content, **options):
        stages.append(schema["title"])
        if schema["title"] == "TurnRoute":
            assert on_content is None
            if ask_about_card:
                return {"route": "steer_task", "action": "chat", "task_id": identity, "task_effect": "discuss"}
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        current = messages[-1]["content"]
        if ask_about_card:
            assert json.loads(current)["task"]["goal"] == "准备一份提纲"
        else:
            assert current == "陪我聊会儿"
        reply = {"reply_markdown": "我在呢。"}
        on_content(json.dumps(reply))
        return reply

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "intent": {"action": "agent_task"},
                                       "message": "准备一份提纲"})).raise_for_status()
        result = await client.post(path + "/stream", json={"request_id": str(uuid4()),
                                   "message": "那份提纲现在怎样了？" if ask_about_card else "陪我聊会儿"})
        assert "event: delta" in result.text and "event: done" in result.text
        assert stages == ["TurnRoute", "FrontDialogueReply"]
        assert app.state.runtime.tasks.current(identity)["revision"] == 1


@pytest.mark.parametrize("route", ["agent_task", "direct_tool", "shortcut"])
async def test_typed_video_selection_is_input_not_dispatch(settings, monkeypatch, route):
    app = create_app(settings, adapter=ControlledPlatform())
    selection = {"bvid": BV, "ranges": [{"part": 2, "start": 1, "end": 4}]}
    stages, reads = [], []

    async def generate(messages, schema, **options):
        stages.append(schema["title"])
        assert schema["title"] == "TurnRoute"
        assert json.loads(messages[-1]["content"])["video_selection"] == selection
        if route == "agent_task":
            return {"route": "delegate_task", "action": "agent_task",
                    "brief": {"goal": "准备一份提纲", "deliverable": "提纲"}}
        return {"route": "direct_action", "action": "full_video"}

    async def queue(identity, character, selected):
        reads.append(selected)
        return {"id": identity}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", generate, raising=False)
    monkeypatch.setattr(app.state.browsing, "queue_full_video", queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        identity = str(uuid4())
        result = await client.post("/api/chat/intelligent", json={"request_id": identity, "video_selection": selection,
            "message": "准备一份提纲" if route == "agent_task" else "看看这段", **(
                {"intent": {"action": "full_video"}} if route == "shortcut" else {})})
        card = result.raise_for_status().json()["card"]
        if route == "agent_task":
            assert card["type"] == "agent_task" and not reads and stages == ["TurnRoute"]
            assert app.state.runtime.tasks.current(identity)["plan"]["video_selection"] == selection
        else:
            assert card["type"] == "video_browsing" and reads == [selection]
            assert stages == ([] if route == "shortcut" else ["TurnRoute"])


async def test_chat_steer_rejects_concurrent_revision_and_non_card_tasks(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    tasks = app.state.runtime.tasks
    identity, hidden = str(uuid4()), str(uuid4())
    tasks.create(hidden, "不在对话里的另一项交办")

    async def decide(messages, schema, **options):
        assert hidden not in str(messages)
        assert schema["title"] == "TurnRoute"
        await tasks.control(identity, 1, "steer", goal="另一端的新要求")
        return {"route": "steer_task", "action": "steer_task", "task_id": identity,
                "task_effect": "revise", "change_quote": "留一段就好"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", decide, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        path = "/api/chat/intelligent"
        result = await client.post(path, json={"request_id": identity, "message": "准备提纲", "intent": {"action": "agent_task"}})
        assert result.status_code == 200, result.text
        changed = await client.post(path, json={"request_id": str(uuid4()), "message": "留一段就好"})
        assert changed.status_code == 409
        assert tasks.current(identity)["goal"] == "另一端的新要求"
        unbound = await client.post(path, json={"request_id": str(uuid4()), "message": "改一下",
                                               "intent": {"action": "steer_task", "reference": hidden}})
        assert unbound.json()["card"] is None
        assert tasks.current(hidden)["revision"] == 1


async def test_bound_private_chat_survives_version_and_account_but_not_role_switch(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, discovery = app.state.runtime, app.state.browsing.discovery
    store, path = runtime.store, "/api/chat/intelligent"
    prompts = []

    async def decide(messages, schema, **options):
        if schema["title"] == "TurnRoute":
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        prompts.append(messages)
        return {"reply_markdown": "后来它游回来了。"}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", decide, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        discovery.remember_account("100")
        original = runtime.character
        turn_id, task_id, hidden = (str(uuid4()) for _ in range(3))
        async def send(identity, text, action=None):
            return (await client.post(path, json={"request_id": identity, "message": text,
                                      **({"intent": {"action": action}} if action else {})})).raise_for_status().json()

        await send(turn_id, "小海龟去了哪里？")
        await send(task_id, "仅为旧账号准备便笺", "agent_task")
        store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
            hidden, original["id"], original["version"], "没有绑定的记录", '{"private":true}', time.time()))
        discovery.remember_account("200")
        await send(str(uuid4()), "它还记得回去的路吗？")
        assert "小海龟去了哪里" in str(prompts[-1])
        assert "仅为旧账号准备便笺" not in str(prompts[-1]) and "没有绑定的记录" not in str(prompts[-1])
        discovery.remember_account("100")
        await send(str(uuid4()), "刚才那份还在吗？")
        assert "仅为旧账号准备便笺" not in str(prompts[-1])
        expired = await client.post(path, json={"request_id": str(uuid4()), "message": "改一下",
                                                "intent": {"action": "steer_task", "reference": task_id}})
        assert expired.raise_for_status().json()["card"] is None
        discovery.remember_account("200")
        store.save_character(original)
        assert runtime.character["version"] != original["version"]
        assert (await client.get(path + "/turns/" + turn_id)).json()["message"] == "小海龟去了哪里？"
        assert (await client.get(path + "/turns/" + hidden)).status_code == 404
        assert (await client.get(path + "/turns/" + task_id)).status_code == 404
        stale = await client.post(path, json={"request_id": str(uuid4()), "message": "换个重点",
                                              "intent": {"action": "steer_task", "reference": task_id}})
        assert stale.json()["card"] is None and runtime.tasks.current(task_id)["revision"] == 1
        page = (await client.get(path)).json()
        assert turn_id in [turn["request_id"] for turn in page["turns"]]
        store.save_character(original | {"id": "another"})
        store.activate("another")
        assert (await client.get(path + "/turns/" + turn_id)).status_code == 404
        assert not (await client.get(path)).json()["turns"]


@pytest.mark.parametrize("change", ["account", "character", "memory"])
def test_assistant_rechecks_private_scope_before_delivery(settings, monkeypatch, change):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, browsing = app.state.runtime, app.state.browsing
    memory = runtime.daily_memory

    async def generate(messages, schema, *, max_tokens, on_content, on_stage):
        if schema["title"] == "TurnRoute":
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        on_stage("generating")
        if change == "account":
            browsing.discovery.remember_account("200")
        elif change == "character":
            char = runtime.character
            monkeypatch.setattr(type(runtime), "character", property(lambda self: char | {"version": 999}))
        else:
            assert on_content is None
            memory.set_preference(runtime.memory_organizer.scope(), False)
        if on_content:
            on_content('{"reply_markdown":"旧作用域的文字')
        return {"reply_markdown": "旧作用域的文字"}

    if change == "memory":
        monkeypatch.setattr(memory, "recall_organized", lambda *args, **kwargs: [{"headline": "私有", "anecdote": "私有文字"}])
        monkeypatch.setattr(memory, "organized_reference", lambda entry: {"id": "fixture", "version": 1})
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 52000)) as client:
        browsing.discovery.remember_account("100")
        response = client.post("/api/chat/intelligent/stream", json={
            "request_id": str(uuid4()), "message": "还记得刚才的事吗",
        })
        assert "event: error" in response.text
        assert "event: delta" not in response.text
        assert "旧作用域的文字" not in response.text
        assert not app.state.store.one("SELECT 1 FROM operator_chat_turns")


async def test_stop_generation_cancels_only_the_bound_chat_and_releases_model(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, browsing = app.state.runtime, app.state.browsing
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original = runtime.llm.generate

    async def wait_for_stop(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime.llm, "generate", wait_for_stop)
    path = "/api/chat/intelligent"
    request_id = str(uuid4())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1",
    ) as client:
        browsing.discovery.remember_account("100")
        request = asyncio.create_task(client.post(path, json={
            "request_id": request_id, "message": "你好呀", "intent": {"action": "chat"},
        }))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            url = f"{path}/turns/{request_id}/stop-generation"
            browsing.discovery.remember_account("200")
            assert (await client.post(url)).json() == {"stopping": False}
            assert not cancelled.is_set()
            browsing.discovery.remember_account("100")
            assert (await client.post(url)).json() == {"stopping": True}
            assert (await asyncio.wait_for(request, 3)).json() == {"stopped": True}
            assert cancelled.is_set() and not runtime.generation_lock.locked()
            assert (await client.get(path)).json()["turns"] == []
            assert (await client.post(url)).json() == {"stopping": False}
            monkeypatch.setattr(runtime.llm, "generate", original)
            followup = await client.post(path, json={
                "request_id": str(uuid4()), "message": "还在吗", "intent": {"action": "chat"},
            })
            assert followup.status_code == 200 and followup.json()["reply_text"]
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)


def test_preview_request_survives_completion_and_policy_edits(store):
    repo = BrowsingRepository(store)
    char, request = store.character(), str(uuid4())
    selection = {"video": None, "keywords": None}
    config = BrowsingPolicy().model_dump() | {"operator_selection": selection}
    result, created = repo.create_round("900", char, config, 1, True, request_id=request)
    assert created and result["id"] == request
    store.execute("UPDATE browsing_rounds SET status='completed' WHERE id=?", (request,))
    repeated, created = repo.create_round("900", char, config | {"sources": ["popular"]}, 2, True, request_id=request)
    assert not created and repeated["status"] == "completed"
    with pytest.raises(ValueError, match="其他浏览轮次"):
        repo.requested_preview(request, char, selection | {"keywords": ["其他视频"]}, "900")


def test_chat_persists_private_turns_and_resolves_real_task_cards(settings, monkeypatch):
    app = create_app(settings)
    runtime, store = app.state.runtime, app.state.store
    writes = []
    packets = []
    private_packets = []
    generate = runtime.llm.generate

    async def private_model(messages, character, event_id, source_text=None):
        private_packets.append(messages)
        return await generate(messages, character, event_id, source_text)

    async def dynamic_model(messages, schema, *, max_tokens):
        if schema.get("title") == "ChatIntent":
            message = json.loads(messages[1]["content"])["message"]
            return {"action": "live_status" if message == "准备开播" else "chat"}
        source = json.loads(messages[1]["content"])["public_sources"][0]
        packets.append(messages)
        return {
            "action": "post", "reason": "分享角色的一件小事",
            "content": "海底小剧场，今天白饭蒸得正好。", "source_ids": [source["id"]],
            "next_evaluation_seconds": 1800,
        }

    async def fake_preview(**params):
        writes.append(params)
        return {"id": params["request_id"], "status": "queued", "preview": True}

    monkeypatch.setattr(runtime.operation_llm, "generate_json", dynamic_model)
    monkeypatch.setattr(runtime.llm, "generate_json", dynamic_model)
    monkeypatch.setattr(runtime.llm, "generate", private_model)
    monkeypatch.setattr(app.state.browsing, "run", fake_preview)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        chat_id = str(uuid4())
        chat = {"request_id": chat_id, "message": "私聊专用内容 xyz123"}
        response = client.post(path, json=chat)
        assert response.status_code == 200, response.text
        assert response.json()["private"]
        assert client.post(path, json=chat).json()["private"]
        assert len(client.get(path).json()["turns"]) == 1
        assert len(private_packets) == 1
        assert "xyz123" not in json.dumps(app.state.dynamics._sources())
        assert client.post(path, json=chat | {"message": "另一个内容"}).status_code == 409
        assert client.post(path, json={"request_id": str(uuid4()), "message": "还记得吗"}).status_code == 200
        assert "xyz123" in str(private_packets[-1])

        dynamic = {"request_id": str(uuid4()), "message": "发个动态：聊白饭", "intent": {"action": "dynamic", "topic": "聊白饭"}}
        card = client.post(path, json=dynamic).json()["card"]
        post_id = card["data"]["id"]
        assert client.post(path, json=dynamic).json()["card"]["data"]["id"] == post_id
        assert store.one("SELECT status FROM dynamic_posts WHERE id=?", (post_id,))["status"] == "pending_review"
        assert "xyz123" not in str(packets)
        response = client.post(f"/api/dynamics/posts/{post_id}/publish", json={"text": "换成这句白饭。"})
        assert response.status_code == 400  # this isolated app has no platform credentials
        assert app.state.dynamics._post(post_id)["content"] == "换成这句白饭。"

        browse = {"request_id": str(uuid4()), "message": "去刷视频：原神", "intent": {"action": "browse", "topic": "原神"}}
        assert client.post(path, json=browse).json()["card"]["data"]["preview"]
        assert client.post(path, json=browse).json()["card"]["data"]["preview"]
        assert writes == [dict(preview=True, video_only=True, request_id=browse["request_id"], video=None, keywords=["原神"])]
        live = client.post(path, json={"request_id": str(uuid4()), "message": "准备开播"}).json()["card"]
        assert live["data"]["state"] == runtime.live.state
        assert client.delete(path).json()["ok"]
        assert client.get(path).json()["turns"] == []
        assert not store.one("SELECT 1 FROM grounded_memory WHERE scope LIKE 'operator:%'")
        assert not store.one("SELECT 1 FROM context_windows WHERE mode LIKE 'operator:%'")
        assert app.state.dynamics._post(post_id)["id"] == post_id
        assert not app.state.dynamics._sources()


def test_chat_history_cursor_and_activity_are_bound_to_current_scope(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    store, browsing = app.state.store, app.state.browsing
    char = app.state.runtime.character
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        requests = sorted(str(uuid4()) for _ in range(3))
        for request in requests:
            store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
                request, char["id"], char["version"], "测试", '{"reply_text":"应答","private":true}', 1,
            ))
        page = client.get(path, params={"limit": 2}).json()
        assert (page["character_id"], page["character_version"]) == (char["id"], char["version"])
        assert [turn["request_id"] for turn in page["turns"]] == requests[1:]
        before = page["next_cursor"]
        previous = client.get(path, params={"limit": 2, "before": before}).json()
        assert [turn["request_id"] for turn in previous["turns"]] == requests[:1]
        assert previous["next_cursor"] is None
        assert client.get(path, params={"before": str(uuid4())}).status_code == 404

        browsing.discovery.remember_account("100")
        round_, _ = browsing.repo.create_round("100", char, browsing.policy(), 1, False)
        task_id = browsing.repo.task(round_["id"], {"bvid": BV})
        assert browsing.repo.quotas.start_watch(task_id, "100", char["id"], BV, 0)
        assert browsing.repo.quotas.start_watch("second-attempt", "100", char["id"], BV, 0)
        assert browsing.repo.quotas.start_watch("other-account", "200", char["id"], BV, 0)
        store.execute(
            "UPDATE browsing_tasks SET status='viewed_without_comment',material_key='valid',payload=? WHERE id=?",
            ('{"coverage":"all_available_cc","missing_parts":[]}', task_id),
        )
        query = {"request_id": str(uuid4()), "message": "今天刷了多少视频？", "intent": {"action": "video_activity"}}
        card = client.post(path, json=query).json()["card"]
        assert card["type"] == "video_activity"
        data = card["data"]
        assert (data["started"], data["completed"], data["fully_read"]) == (1, 1, 1)
        assert len(data["records"]) == 2
        assert any(entry["round_id"] == round_["id"] for entry in data["records"])
        browsing.discovery.remember_account("200")
        assert client.get(path + "/turns/" + query["request_id"]).json()["card"]["data"]["available"] is False
        assert client.post(path, json=query | {"request_id": str(uuid4())}).json()["card"]["data"]["started"] == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_chat_corrects_original_dynamic_without_reposting(settings, monkeypatch, unknown):
    from tuber_studio import dynamics as module

    app = create_app(settings, adapter=ControlledPlatform())
    studio = app.state.dynamics
    identifier = "1270460143781281810"
    source = {"id": "reply:example", "type": "published_reply", "text": "有人提到了赛博保险。"}
    monkeypatch.setattr(studio, "_sources", lambda **_: [source])
    old = "我在评论区连着认错对象。第三次才翻回原楼找人把话接对。"
    new = "评论区有人说要卖我赛博保险。"
    picture = {"img_src": "https://i0.hdslb.com/bfs/new_dyn/1.png", "img_width": 960, "img_height": 512}
    remote = {"text": old, "pictures": [picture]}
    writes = []

    async def read(adapter, target, account):
        assert target == identifier and account == "100"
        return dict(remote)

    async def write(adapter, target, text, pictures, *, before_send):
        before_send()
        writes.append((target, text, pictures))
        if unknown:
            raise module.PublishUncertain("编辑回执不明")
        remote["text"] = text

    async def generate(messages, schema, *, max_tokens):
        if schema.get("title") == "ChatIntent":
            return {"action": "edit_dynamic"}
        packet = json.loads(messages[1]["content"])
        assert packet["original"] == old
        assert packet["public_sources"] == [source]
        return {"caption": new}

    monkeypatch.setattr(module, "read_dynamic_caption", read)
    monkeypatch.setattr(module, "edit_dynamic_caption", write)
    monkeypatch.setattr(app.state.runtime.llm, "generate_json", generate)
    monkeypatch.setattr(app.state.runtime.operation_llm, "generate_json", generate)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        app.state.browsing.discovery.remember_account("100")
        store = app.state.runtime.store
        receipt = {"dynamic_id": identifier, "code": 0, "platform": "bilibili"}
        row = studio._save_decision(app.state.runtime.character, False, {
            "action": "post", "reason": "评论区互动", "content": old,
            "source_ids": [source["id"]], "next_evaluation_seconds": 1800,
        }, [source], schedule=False)
        post_id = row["post_id"]
        store.execute("UPDATE dynamic_posts SET account_id='100' WHERE id=?", (post_id,))
        studio._status(post_id, "published", receipt=receipt)
        posted_at = studio._post(post_id)["published_at"]
        request_id = str(uuid4())
        response = client.post("/api/chat/intelligent", json={
            "request_id": request_id, "message": f"修改 https://www.bilibili.com/opus/{identifier}，原评论里没有认错人。",
        })
        card = response.raise_for_status().json()["card"]
        assert card["type"] == "dynamic_edit" and card["data"]["caption"] == new
        assert not writes
        path = f"/api/dynamics/edits/{request_id}/apply"
        result = client.post(path).raise_for_status().json()
        if unknown:
            assert result["status"] == "pending_verification"
            assert client.post(path).raise_for_status().json()["status"] == "pending_verification"
            assert studio._post(post_id)["content"] == old
            remote["text"] = new
        assert client.post(path).raise_for_status().json()["status"] == "applied"
        assert writes == [(identifier, new, [picture])]
        post = studio._post(post_id)
        assert post["content"] == new and post["receipt"] == receipt and post["published_at"] == posted_at


def test_operations_card_refreshes_permissions_and_confirmation(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    async def model(messages, schema, *, max_tokens):
        return {"action": "chat"}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        command = {"request_id": str(uuid4()), "message": "启动自主运营", "intent": {"action": "start_operations"}}
        control = client.post(path, json=command).json()["card"]
        assert control["type"] == "operation_control" and control["data"]["policy"]["enabled"] is False
        updated = client.put("/api/automation/settings", json={"reply_enabled": True}).json()
        current = client.get(path + "/turns/" + command["request_id"]).json()["card"]["data"]
        assert current["permissions"] == updated["permissions"] and current["permissions"]["replies"] == "review"
        assert client.post(path + f"/turns/{command['request_id']}/confirm/start", json={}).status_code == 409
        assert client.post(path + f"/turns/{command['request_id']}/refresh-confirmation").status_code == 200
        assert client.post(path + f"/turns/{command['request_id']}/confirm/save", json={}).status_code == 200


def test_semantic_chat_binds_displayed_event_order_and_memory_version(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    events = [
        {"bvid": BV, "title": name, "source": {"kind": "comment", "text": quote, "author": name, "uid": str(index),
         "href": f"https://www.bilibili.com/video/{BV}#reply{index}"}, "reply": {"state": "confirmed", "text": "知道了"}}
        for index, name, quote in [(1, "甲", "第一句"), (2, "乙", "第二句")]
    ]
    monkeypatch.setattr(operator_facts, "daily_events", lambda *args: [event.copy() for event in events])

    async def model(messages, schema, *, max_tokens):
        assert schema["title"] == "EventSelection"
        return {"items": [2, 1]}

    async def assistant(messages, schema, **options):
        if schema["title"] == "FrontDialogueReply":
            sys_msg = messages[-2]["content"]
            marker = "以下是本轮已经取得的读取回执："
            if marker in sys_msg:
                summary = json.loads(sys_msg.split(marker, 1)[1])["summary"]
                return {"reply_markdown": summary}
            return {"reply_markdown": "这就来。"}
        assert schema["title"] == "TurnRoute"
        message = json.loads(messages[-1]["content"])["message"]
        action = {"今天有什么趣事": "activity", "第二件是谁讲的": "event_author", "后来回了没": "event_reply",
                  "把它记成公开经历": "remember_event", "那件公开经历": "recall_experience",
                  "查看公开经历": "public_experiences", "你记得我什么": "memory_summary",
                  "现在去吃饭吧": "chat", "再查第一句是谁说的": "event_author"}[message]
        if action == "chat":
            return {"route": "chat", "action": "chat"}
        route_name = "direct_action" if action == "remember_event" else "instant_read"
        return {
            "route": route_name,
            "action": action,
            "position": 2 if message.startswith("第二件") else None,
            "topic": "第一句" if action == "recall_experience" or message.startswith("再查") else "",
        }

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", assistant, raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        app.state.browsing.discovery.remember_account("100")

        def send(text):
            request = str(uuid4())
            response = client.post(path, json={"request_id": request, "message": text})
            assert response.status_code == 200, response.text
            return request, response.json()

        _, activity = send("今天有什么趣事")
        assert [item["title"] for item in activity["card"]["data"]["items"]] == ["乙", "甲"]
        events.reverse()
        assert len(client.get(path).json()["turns"]) == 1
        assert send("第二件是谁讲的")[1]["reply_text"] == "是甲说的（UID 1）。"
        assert send("后来回了没")[1]["card"]["data"]["event"]["reply"]["text"] == "知道了"
        proposal, turn = send("把它记成公开经历")
        assert turn["card"]["data"]["text"] == "第一句"
        memory_path = "/api/daily-memory"
        saved, = client.get(memory_path).json()["organized"]
        assert saved["source"]["references"][0]["text"] == "第一句"
        assert saved["visibility"] == "shared"
        summary_id, turn = send("你记得我什么")
        assert turn["card"]["data"]["groups"] == client.get(memory_path).json()["summary"]
        listing, turn = send("查看公开经历")
        assert turn["card"]["data"]["items"] == [{
            "headline": saved["headline"], "anecdote": "第一句", "links": [events[1]["source"]["href"]],
        }]
        assert any(source["id"] == f"organized:{saved['id']}" for source in app.state.dynamics._sources())
        app.state.browsing.discovery.remember_account("200")
        assert not client.get(memory_path).json()["organized"]
        app.state.browsing.discovery.remember_account("100")
        recalled, turn = send("那件公开经历")
        assert turn["reply_text"] == "第一句" and turn["memory_dependent"]
        assert client.delete(f"{memory_path}/organized/{saved['id']}").status_code == 200
        assert client.get(f"{path}/turns/{summary_id}").json()["card"]["data"]["groups"] == []
        assert client.get(f"{path}/turns/{listing}").json()["card"]["data"]["items"] == []
        assert client.get(f"{path}/turns/{recalled}").json()["reply_text"] == "这条回复引用的记忆已变更。"
        assert send("第二件是谁讲的")[1]["reply_text"].startswith("是甲说的")
        send("现在去吃饭吧")
        assert send("后来回了没")[1]["reply_text"] == "你指的是哪一件事？"
        assert send("再查第一句是谁说的")[1]["reply_text"].startswith("是甲说的")

        legacy_request = str(uuid4())
        runtime = app.state.runtime
        runtime.store.execute(
            "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
            (legacy_request, runtime.character["id"], runtime.character["version"], "记下这件事", json.dumps({
                "account_id": "100", "reply_text": "请确认这件事。",
                "card": {"type": "experience_proposal", "data": {"event": events[1], "fact": "早期草稿"}},
            }), time.time()),
        )
        assert client.get(f"{path}/turns/{legacy_request}").json()["card"] == {
            "type": "operation_event", "data": {"event": events[1]},
        }
        app.state.browsing.discovery.remember_account("200")
        assert client.get(f"{path}/turns/{proposal}").json()["card"] is None
        assert client.get(f"{path}/turns/{legacy_request}").json()["card"] is None


def test_live_proposal_runs_existing_control_once_and_demo_shortcut_remains_available(settings, monkeypatch):
    settings.data["features"]["live_enabled"] = True
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    actions = []
    original = runtime.control

    async def control(action):
        actions.append(action)
        return await original(action)

    monkeypatch.setattr(runtime, "control", control)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        request = {"request_id": str(uuid4()), "message": "开始直播"}
        assert client.post(path, json=request).status_code == 503
        proposed = client.post(path, json=request | {"intent": {"action": "start_live"}}).json()
        assert proposed["card"]["data"]["state"] == "stopped" and actions == []
        url = f"{path}/turns/{request['request_id']}/confirm/live"
        assert client.post(url, json={}).json()["card"]["data"]["state"] == "live"
        assert client.post(url, json={}).status_code == 200
        assert actions.count("start") == 1

        async def model(messages, schema, *, max_tokens):
            return {"action": "stop"}

        monkeypatch.setattr(runtime.llm, "generate_json", model)
        runtime.operations_waiting = lambda: True
        stopped = client.post(path, json={"request_id": str(uuid4()), "message": "停一下"})
        assert stopped.json()["card"]["data"]["state"] == "stopped"


@pytest.mark.parametrize("stream_available", [True, False])
def test_precise_video_chat_uses_bound_part_and_reports_fallback(settings, monkeypatch, stream_available):
    class Platform(ControlledPlatform):
        async def video_metadata(self, video):
            metadata = await super().video_metadata(video)
            return metadata | {"parts": metadata["parts"] + [{"cid": "22", "title": "第二P", "duration": 180}]}

        async def video_stream_frame(self, metadata, *, cid, timestamp):
            assert cid == "22" and timestamp == 137.4
            self.calls.append("stream")
            if not stream_available:
                raise ValueError("播放来源未按请求返回有效 HTTP Range")
            return {"id": "frame-22", "cid": cid, "kind": "stream", "requested_time": timestamp,
                    "actual_time": 137.383, "presentation_end": 137.417, "width": 1, "height": 1, "image": PNG}

        async def video_frames(self, metadata, count, *, cid, timestamps):
            assert cid == "22" and timestamps == [137.4]
            self.calls.append("storyboard")
            return [{"id": "board-22", "cid": cid, "kind": "storyboard", "actual_time": 135,
                     "width": 1, "height": 1, "image": PNG}]

    platform, vision = Platform(), TestVisual()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: vision)
    app.state.browsing.read_interval = 0

    async def intent(messages, schema, *, max_tokens):
        return {"action": "inspect_video_frame", "part": 2, "seconds": 137}

    monkeypatch.setattr(app.state.runtime.llm, "generate_json", intent)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        for _ in range(2 if stream_available else 1):
            identity = str(uuid4())
            queued = client.post(path, json={"request_id": identity, "message": f"看一下 {BV} P2 的 02:17.400"})
            assert queued.status_code == 200, queued.text
            assert queued.json()["card"]["data"]["selection"]["frame"]["seconds"] == 137.4
            for _ in range(200):
                data = client.get(f"{path}/turns/{identity}").json()["card"]["data"]
                if data["status"] not in {"waiting", "queued", "running"}:
                    break
                time.sleep(0.01)
            assert data["status"] == "completed", data
            assert data["inspection"]["requested_time"] == 137.4
            assert data["inspection"]["actual_time"] == (137.383 if stream_available else 135)
            assert data["inspection"]["kind"] == ("stream" if stream_available else "storyboard")
            assert bool(data["inspection"].get("fallback_reason")) is not stream_available
            assert "preview" not in data["inspection"]
            picture = client.get(f"{path}/turns/{identity}/inspection-image")
            assert picture.status_code == 200 and picture.content == PNG
        assert platform.calls.count("stream") == vision.calls == 1
        assert platform.calls.count("storyboard") == (0 if stream_available else 1)
        assert not platform.sent


def test_specified_video_reuses_range_checkpoints_and_reconnects_under_same_id(settings, monkeypatch):
    class MultipartPlatform(ControlledPlatform):
        hold_second = True

        async def video_metadata(self, video):
            metadata = await super().video_metadata(video)
            return metadata | {"parts": metadata["parts"] + [{"cid": "22", "title": "第二P", "duration": 30}]}

        async def video_frames(self, metadata, count, *, cid, timestamps):
            if self.hold_second and cid == "22":
                await asyncio.Event().wait()
            return [
                {"id": f"{cid}-{index}", "cid": cid, "actual_time": timestamp,
                 "width": 1, "height": 1, "image": PNG, "kind": "storyboard"}
                for index, timestamp in enumerate(timestamps[:count])
            ]

    platform, vision = MultipartPlatform(), TestVisual()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: vision)
    browsing = app.state.browsing
    browsing.read_interval = 0
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/chat/intelligent"
        request = {"request_id": str(uuid4()), "message": "看完整视频：" + BV, "intent": {"action": "full_video"}, "video_selection": {
            "bvid": BV, "ranges": [{"part": 1, "start": 0, "end": 5}, {"part": 2, "start": 3, "end": 20}],
        }}
        turn_url = path + "/turns/" + request["request_id"]
        queued = client.post(path, json=request)
        assert queued.status_code == 200, queued.text
        assert queued.json()["card"]["data"]["selection"]["ranges"][1]["part"] == 2

        def until(predicate):
            for _ in range(200):
                data = client.get(turn_url).json()["card"]["data"]
                if predicate(data):
                    return data
                time.sleep(0.01)
            pytest.fail(f"视频任务没有进入期望状态：{data}")

        first = until(lambda data: data["coverage"] and data["coverage"]["parts"][0]["ranges"][0]["visual"])
        assert first["status"] == "running"
        waiting_request = request | {"request_id": str(uuid4()), "video_selection": {"bvid": BV}}
        waiting_url = path + "/turns/" + waiting_request["request_id"]
        assert client.post(path, json=waiting_request).json()["card"]["data"]["status"] == "waiting"
        assert client.post(waiting_url + "/cancel-video").json()["card"]["data"]["status"] == "cancelled"
        assert client.post(waiting_url + "/resume-video").json()["card"]["data"]["status"] == "waiting"
        assert client.post("/api/automation/stop", json={"emergency": True}).json()["state"] == "emergency"
        assert client.get(waiting_url).json()["card"]["data"]["status"] == "paused"
        cancelled = client.get(turn_url).json()["card"]["data"]
        assert cancelled["status"] == "cancelled" and cancelled["can_resume"]
        assert client.post(path, json=request).json()["card"]["data"]["status"] == "cancelled"
        assert client.post(path, json=request | {"video_selection": {"bvid": BV}}).status_code == 409
        assert client.post(turn_url + "/resume-video").status_code == 400
        assert client.post("/api/automation/reset-stop").status_code == 200
        platform.hold_second = False
        assert client.post(turn_url + "/resume-video").status_code == 200
        result = until(lambda data: data["status"] == "completed")
        first, second = [part["ranges"][0] for part in result["coverage"]["parts"]]
        assert first["subtitle"]["covered_seconds"] == 5 and first["subtitle"]["track_complete"]
        assert second["subtitle"]["covered_seconds"] == 7 and second["subtitle"]["gap_seconds"] == 10
        assert first["visual"]["state"] == second["visual"]["state"] == "sampled"
        assert len(first["visual"]["samples"]) == 1 and len(second["visual"]["samples"]) == 2
        assert result["phase"] == "complete" and result["comments"]["at_end"]
        assert second["audio"]["state"] == "unavailable"
        assert platform.calls.count(("subtitles", BV, "11")) == 1
        assert platform.calls.count(("subtitles", BV, "22")) == 1
        assert vision.calls == 2 and platform.sent == []
        assert app.state.store.one("SELECT COUNT(*) AS n FROM browsing_tasks WHERE round_id=?", (result["id"],))["n"] == 1
        browsing.bilibili_account.verified_account_id = "200"
        assert client.get(turn_url).json()["card"]["data"]["status"] == "unavailable"


@pytest.mark.parametrize("case", ["success", "private-fragment", "account-switch"])
def test_operator_discussion_to_dynamic_uses_only_sanitized_material(settings, monkeypatch, case):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    runtime = app.state.runtime
    app.state.browsing.discovery.remember_account("100")
    private_name, private_url = "样例甲", "https://internal.invalid/fixture"
    safe = "用两盏小灯试出画面层次。"

    async def assistant(messages, schema, **options):
        if schema["title"] == "TurnRoute":
            return {"route": "chat", "action": "chat"}
        assert schema["title"] == "FrontDialogueReply"
        return {"reply_markdown": "可以先开一盏暖光，再用冷光补轮廓。"}

    async def sanitize(messages, schema, *, max_tokens):
        packet = json.loads(messages[-1]["content"])
        assert schema["title"] == "PublicText" and packet["用途"] == "动态"
        assert private_name in json.dumps(packet, ensure_ascii=False)
        assert any("补轮廓" in turn.get("content", "") for turn in packet["资料"])
        return {"text": f"{private_name}说：{safe}" if case == "private-fragment" else safe}

    async def draft(messages, schema, *, max_tokens):
        packet = json.loads(messages[1]["content"])
        source, = packet["public_sources"]
        assert source["text"] == safe
        wire = json.dumps(messages, ensure_ascii=False)
        assert private_name not in wire and private_url not in wire and "补轮廓" not in wire
        if case == "account-switch":
            app.state.browsing.discovery.remember_account("200")
        return {"action": "post", "reason": "想分享布光", "content": "小窝的两盏灯，各管一半层次。",
                "source_ids": [source["id"]], "next_evaluation_seconds": 1800}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", assistant, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_operator_json", sanitize, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_json", draft)
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        client.cookies.set("tuber_overlay", settings.overlay_token)
        path = "/api/chat/intelligent"
        client.post(path, json={"request_id": str(uuid4()), "message": f"姓名：{private_name}，聊聊布光 {private_url}"}).raise_for_status()
        command = {"request_id": str(uuid4()), "message": "把刚才聊的布光写成动态",
                   "intent": {"action": "dynamic", "source": "discussion"}}
        response = client.post(path, json=command)
        if case != "success":
            if case == "private-fragment":
                assert response.raise_for_status().json()["card"] is None
            else:
                assert response.status_code == 400 and "账号或角色已变化" in response.text
            assert app.state.dynamics.list_posts() == [] and platform.sent == []
            return
        result = response.raise_for_status().json()
        assert result["card"]["type"] == "dynamic_draft"
        assert client.post(path, json=command).raise_for_status().json() == result
        post, = app.state.dynamics.list_posts()
        assert post["sources"][0]["text"] == safe and post["comment_card"] is None
        assert post["status"] == "pending_review" and platform.sent == []


def test_operator_video_to_live_uses_only_handoff_text_and_player_receipt(settings, monkeypatch):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform, video_evidence_factory=lambda runtime: TestVisual())
    runtime, browsing = app.state.runtime, app.state.browsing
    browsing.read_interval = 0
    browsing.discovery.remember_account("100")
    seen = []
    safe_text = "加盐会提高水的密度，让鸡蛋浮起来。"
    spoken = "结论是，加盐后鸡蛋就浮起来了。"
    name = "验收姓名甲"
    private_url = "http://internal.invalid/secret"

    async def interpret(messages, schema, *, max_tokens):
        wire = json.loads(messages[-1]["content"])
        kind = schema["title"]
        if kind == "ChatIntent":
            action = {
                "本场讲一下这个视频的发现": {"action": "live_handoff", "source": "video"},
                "以后直播都先讲结果": {"action": "live_preference"},
                "忘掉直播先讲结果这条": {"action": "forget_memory", "topic": "直播先讲结果"},
                "本场讲一下刚才讨论的打法": {"action": "live_handoff", "source": "discussion"},
            }
            return action.get(wire["message"], {"action": "chat"})
        if kind == "LivePreferenceSelection":
            return {"preference": "result_first"}
        assert kind == "PublicText"
        seen.append(wire)
        if wire["要求"].endswith("打法"):
            assert name in json.dumps(wire, ensure_ascii=False)
        else:
            assert wire["资料"][0]["seconds"] == 0
            assert name not in json.dumps(wire, ensure_ascii=False)
        return {"text": safe_text}

    async def generate(messages, character, event_id, source_text):
        if source_text != safe_text:
            return Reply(spoken_text="记下了。")
        provider = type("Provider", (), {"identity_resolver": staticmethod(
            lambda: {"account_bio": name, "account_uid": "private-account", "identity_version": "test"}
        )})()
        wire = json.dumps(provider_identity_messages(provider, messages), ensure_ascii=False)
        assert name not in wire and private_url not in wire and BV not in wire
        assert "private-account" not in wire
        assert all(isinstance(message["content"], str) for message in messages)
        seen.append(wire)
        if "先讲结果" in wire:
            return Reply(spoken_text="这句来自删除前的直播偏好。")
        return Reply(spoken_text=spoken)

    async def synthesize(text, voice_profile):
        return Audio(settings.audio / "handoff-test.wav", 1, text)

    monkeypatch.setattr(runtime.llm, "generate_operator_json", interpret, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_operator_json", interpret, raising=False)
    monkeypatch.setattr(runtime.llm, "generate", generate)
    monkeypatch.setattr(runtime.voices, "synthesize_live", synthesize)
    path = "/api/chat/intelligent"

    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        client.cookies.set("tuber_overlay", settings.overlay_token)

        def send(message, **kwargs):
            req = {"message": message, "request_id": str(uuid4()), **kwargs}
            return req, client.post(path, json=req).raise_for_status().json()

        send(f"姓名：{name}，私下讨论加盐让鸡蛋浮起来，内部链接 {private_url}")
        request, queued = send(f"帮{name}看一下 {BV}，范围是 P1 开头五秒。", intent={"action": "full_video"}, video_selection={
            "bvid": BV, "ranges": [{"part": 1, "start": 0, "end": 5}],
        })
        assert queued["card"]["data"]["selection"]["bvid"] == BV
        turn_url = f"{path}/turns/{request['request_id']}"
        for _ in range(200):
            status = client.get(turn_url).json()["card"]["data"]
            if status["coverage"] and status["coverage"]["parts"][0]["ranges"][0]["highlights"]:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"指定视频没有完成取材：{status}")
        row = browsing.repo.round(request["request_id"])
        assert name not in json.dumps(row, ensure_ascii=False) and private_url not in json.dumps(row)
        assert ("subtitles", BV, "11") in platform.calls and platform.sent == []
        client.post("/api/live/start").raise_for_status()
        send("以后直播都先讲结果")
        req, handoff = send("本场讲一下这个视频的发现")
        assert handoff["card"]["data"]["text"] == safe_text
        assert handoff["card"]["data"]["sources"][0]["seconds"] == 0
        assert client.post(path, json=req).json()["card"]["data"]["job_id"] == handoff["card"]["data"]["job_id"]
        handoff_url = f"{path}/turns/{req['request_id']}"
        for _ in range(100):
            if client.get(handoff_url).json()["card"]["data"]["status"] == "ready_to_play":
                break
            time.sleep(0.01)
        else:
            pytest.fail("待播任务没有完成第一次合成")
        assert any(isinstance(entry, str) and "先讲结果" in entry for entry in seen)
        _, forgotten = send("忘掉直播先讲结果这条")
        assert forgotten["reply_text"] == "已删除这条记忆。"
        with client.websocket_connect("/ws/overlay") as player:
            assert player.receive_json()["type"] == "hello"
            play = player.receive_json()
            assert play["type"] == "play" and play["text"] == spoken
            ack = {key: play[key] for key in ("job_id", "playback_token")}
            player.send_json(ack | {"type": "started"})
            player.send_json(ack | {"type": "ended"})
            for _ in range(100):
                done = client.get(f"{path}/turns/{req['request_id']}").json()["card"]["data"]
                if done["status"] == "completed":
                    break
                time.sleep(0.01)
            assert done["spoken"] == spoken
        client.post("/api/live/stop").raise_for_status()
        client.post("/api/live/start").raise_for_status()
        assert client.get(f"{path}/turns/{req['request_id']}").json()["card"]["data"]["spoken"] == spoken
        _, history = send("刚才有说鸡蛋吗", intent={"action": "live_history", "topic": "鸡蛋"})
        assert [entry["text"] for entry in history["card"]["data"]["items"]] == [spoken]
        assert not runtime.live.queue
        assert any(isinstance(entry, str) and safe_text in entry for entry in seen)

        # The discussion variant sees the named private source only at extraction.
        with client.websocket_connect("/ws/overlay") as player:
            player.receive_json()
            _, discussion = send("本场讲一下刚才讨论的打法")
            assert discussion["card"]["data"]["text"] == safe_text
            assert player.receive_json()["text"] == spoken


def test_game_chat_handoff_uses_save_and_yields_to_operator(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    game = app.state.runtime.live.game_livestream
    path = "/api/chat/intelligent"
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        def handoff(action, *, request_id=None):
            request_id = request_id or str(uuid4())
            return client.post(path, json={"request_id": request_id, "message": "私聊背景：验收姓名乙和内部链接",
                                          "intent": {"action": "game_handoff", "game_action": action}}).raise_for_status().json()

        original = client.get("/api/games/status").json()["active_status"]
        identity = str(uuid4())
        first = handoff("water_crops", request_id=identity)
        assert first["card"]["type"] == "game_handoff"
        assert "验收姓名乙" not in json.dumps(game.last_handoff, ensure_ascii=False)
        watered = client.get("/api/games/status").json()["active_status"]
        assert watered["unwatered_crops"] < original["unwatered_crops"]
        assert handoff("water_crops", request_id=identity) == first
        assert client.post("/api/games/control", json={"owner": "operator"}).json()["owner"] == "operator"
        assert "号主接管" in handoff("sleep_day")["reply_text"]
        assert client.post("/api/games/step").status_code == 400
        assert client.post("/api/games/action", json={"action": "sleep_day"}).json()["success"]
        saved = GameLivestreamService(store=app.state.store)
        assert saved.status()["active_status"]["day"] == original["day"] + 1 and saved.owner == "operator"
        client.post("/api/games/control", json={"owner": "studio"}).raise_for_status()
        assert handoff("step")["card"]["type"] == "game_handoff"


def test_specified_video_can_resume_when_previously_unavailable_subtitles_appear(settings, monkeypatch):
    from tuber_studio.operations.browsing.evidence import EvidenceUnavailable

    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    browsing = app.state.browsing
    browsing.read_interval = 0
    original = browsing.evidence.subtitle_part
    available = False

    async def subtitles(*args):
        if not available:
            raise EvidenceUnavailable("no_subtitles", "当前没有字幕")
        return await original(*args)

    monkeypatch.setattr(browsing.evidence, "subtitle_part", subtitles)
    path, identity = "/api/chat/intelligent", str(uuid4())
    turn = f"{path}/turns/{identity}"
    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        assert client.post(path, json={"request_id": identity, "message": "看完整视频：" + BV,
                                      "intent": {"action": "full_video"}}).status_code == 200

        def wait_for(status):
            for _ in range(200):
                data = client.get(turn).json()["card"]["data"]
                if data["status"] == status:
                    return data
                time.sleep(0.01)
            pytest.fail(f"视频任务没有进入 {status}：{data}")

        partial = wait_for("partial")
        assert partial["coverage"]["parts"][0]["ranges"][0]["subtitle"]["state"] == "unavailable"
        available = True
        assert client.post(turn + "/resume-video").status_code == 200
        resumed = wait_for("completed")
        assert resumed["id"] == partial["id"] == identity
        assert resumed["coverage"]["parts"][0]["ranges"][0]["subtitle"]["state"] == "available"

@pytest.mark.parametrize(("operation", "quote", "expected", "revision"), [
    ("revise", "另挑一段", "queued", 2),
    ("cancel", "停止这项交办", "cancelled", 2),
    ("cancel", "另一个人的指令", "queued", 1),
    ("revise", "", "queued", 1),
])
async def test_opened_edition_routes_once_with_current_instruction(settings, monkeypatch, operation, quote, expected, revision):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    path = "/api/chat/intelligent"
    identity = str(uuid4())
    message = "原定的素材，另挑一段。" if operation == "revise" else "这项交办，停止这项交办，已有的稿子留着。"
    coord = {"task_revision": 1, "edition": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "message": "挑选两段素材",
                                       "intent": {"action": "agent_task"}})).raise_for_status()
        with runtime.store.db as db:
            runtime.tasks.workspace.record_delivery(db, runtime.tasks.current(identity), "这是首版。")

        async def route(messages, schema, **options):
            assert schema["title"] == "TurnRoute" and options["on_content"] is None
            packet = json.loads(messages[-1]["content"])
            assert packet["message"] == message
            assert packet["opened_artifact"] == {"id": identity, **coord}
            assert [card["id"] for card in packet["recent_task_cards"]] == [identity]
            assert not packet.get("history_task_cards")
            return {
                "route": "steer_task",
                "action": "cancel_task" if operation == "cancel" else "steer_task",
                "task_id": identity,
                "task_effect": operation,
                "change_quote": quote,
            }

        monkeypatch.setattr(runtime.llm, "generate_assistant_json", route, raising=False)
        payload = {"request_id": str(uuid4()), "message": message, "opened_artifact": {"task_id": identity, **coord}}
        invalid = await client.post(path, json=payload | {"opened_artifact": {**payload["opened_artifact"], "edition": 2}})
        assert invalid.status_code == 409
        for _ in range(2):
            response = await client.post(path, json=payload)
            assert response.status_code == 200, response.text
        task = runtime.tasks.current(identity)
        assert (task["status"], task["revision"]) == (expected, revision)
        if revision == 2 and operation == "revise":
            assert "引用成果 r1 v1" in task["goal"] and task["goal"].endswith(message)
        assert runtime.tasks.workspace.latest(identity)["body"] == "这是首版。"

@pytest.mark.parametrize("bad_route", [False, True])
async def test_unopened_running_task_cannot_absorb_unrelated_conversation(settings, monkeypatch, bad_route):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    identity = str(uuid4())
    goal = "为下场直播从视频取两个片段，保存提纲，写两句引言。"
    path = "/api/chat/intelligent"
    conversation = "辛苦啦，跟我聊两句吧。"

    async def generate(messages, schema, **options):
        if schema["title"] == "FrontDialogueReply":
            return {"reply_markdown": "我在呢，今天聊点什么好？"}
        assert schema["title"] == "TurnRoute"
        if bad_route:
            return {
                "route": "steer_task",
                "action": "steer_task",
                "task_id": identity,
                "task_effect": "revise",
                "change_quote": "两句",
            }
        return {"route": "chat", "action": "chat"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        (await client.post(path, json={"request_id": identity, "message": goal, "intent": {"action": "agent_task"}})).raise_for_status()
        runtime.store.db.execute("UPDATE agent_tasks SET status='running' WHERE id=?", (identity,))
        monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
        reply = (await client.post(path, json={"request_id": str(uuid4()), "message": conversation})).raise_for_status().json()
        task = runtime.tasks.current(identity)
        assert reply["card"] is None and reply["reply_text"] == "我在呢，今天聊点什么好？"
        assert (task["goal"], task["revision"], task["status"]) == (goal, 1, "running")

async def test_disputed_new_task_needs_an_independent_deliverable():
    from types import SimpleNamespace

    from tuber_studio.agent.operator_task_patch import patch_vocabulary, review_change

    card = {"goal": "备两段素材", "editable": {"goal": "备两段素材", "item:1": "甲", "item:2": "乙"}}
    message = "假设保留第一段、换掉第二段，你怎么看？先别改。"
    choices, bindings = patch_vocabulary([card], message)

    async def check(**request):
        assert [tool["name"] for tool in request["tools"]] == ["record_review"]
        assert request["tool_choice"] == "required" and "previous_response_id" not in request
        packet = json.loads(request["input"][0]["content"])
        assert packet["owner_message"] == message and packet["omni_proposal"] == "new_work"
        return SimpleNamespace(calls=[SimpleNamespace(name="record_review", arguments=assessment)])

    assessment = {"operation": "answer", "changes": {field: "沿用现有内容" for field, _ in choices if field.startswith("item:")},
                  "separate_deliverable": ""}
    assert await review_change(check, message, card, "new_work", choices, bindings) == ("discuss", None)
    assessment.update(operation="new_task", separate_deliverable="换掉第二段")
    assert await review_change(check, message, card, "new_work", choices, bindings) == ("clarify", None)
    assessment.update(operation="edit_existing", changes=["unknown"])
    with pytest.raises(ValueError, match="未知选项"):
        await review_change(check, message, card, "new_work", choices, bindings)


def test_revision_binds_new_requirement_after_existing_deliverable():
    card = {"editable": {"goal": "私下准备一份直播状态交接便笺。"}}
    source = "继续刚才的那份便笺，改为只检查自主运营是否启动，写进同一份便笺。"
    choices, _ = patch_vocabulary([card], source)
    requirement = "改为只检查自主运营是否启动，写进同一份便笺"
    assert choices["goal", "便笺"] == {requirement, "写进同一份"}
    assert grounded_patch(TaskPatch(field="goal", subject="便笺", requirement=requirement), card, source)
