"""Dual-brain routing, layered DAG architecture and delivery push-back regressions."""

import ast
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from tests.support.operations_helpers import BV, ControlledPlatform
from tuber_studio.agent.chat import router as turn_router
from tuber_studio.agent.chat.front_dialogue import paraphrase_task_delivery
from tuber_studio.agent.chat.lookup import workshop_lookup
from tuber_studio.agent.chat.lookup.personal_agent import object_scope
from tuber_studio.agent.chat.models import DeliveryPacket
from tuber_studio.agent.tasks.delegation import function
from tuber_studio.agent.voice import work as private_call_work
from tuber_studio.app import create_app
from tuber_studio.core.config import ROOT
from tuber_studio.core.identity import runtime_identity
from tuber_studio.core.store import dump
from tuber_studio.infra.inference.http_api import InferenceBudgetExceeded
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponsesClient, ResponseTurn

CHAT = "/api/chat/intelligent"
CHAT_DIR = ROOT / "src/tuber_studio/agent/chat"


def _intra_chat_imports(unit_name: str, known: set[str]) -> set[str]:
    unit_py = CHAT_DIR / f"{unit_name}.py"
    files = [unit_py] if unit_py.is_file() else sorted((CHAT_DIR / unit_name).rglob("*.py"))
    deps = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        pkg_depth = 1 if path.parent == CHAT_DIR else 2
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == pkg_depth:
                if node.module:
                    top = node.module.split(".")[0]
                    if top in known and top != unit_name:
                        deps.add(top)
                else:
                    for alias in node.names:
                        if alias.name in known and alias.name != unit_name:
                            deps.add(alias.name)
    return deps


def test_operator_chat_module_hierarchy_and_line_budget():
    layers = {
        "models": 0,
        "context": 1,
        "front_dialogue": 2,
        "dispatcher": 2,
        "router": 2,
        "transport": 3,
        "service": 4,
    }
    known = set(layers)
    lines = (CHAT_DIR / "service.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 400

    graph = {}
    for module, rank in layers.items():
        deps = _intra_chat_imports(module, known)
        assert module not in deps
        for dep in deps:
            assert layers[dep] < rank, f"{module} (L{rank}) must not depend on {dep} (L{layers[dep]})"
        graph[module] = deps

    # Confirm topological sort succeeds (zero cycles).
    visiting, visited = set(), set()

    def visit(node):
        assert node not in visiting, f"Cycle detected at {node}"
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for module in graph:
        visit(module)
    assert visited == known


async def test_thin_foreground_omni_streams_plain_text_without_work_lifecycle(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    seen_front_messages = []

    async def fake_router(**kwargs):
        return ResponseTurn("resp_chat", (FunctionCall("c1", "chat", {}),), "", {})

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        seen_front_messages.append(messages)
        if on_stage:
            on_stage("generating")
        chunks = ["<think>私有推理不外露</think>", "鲸鱼", "正在冒泡。"]
        for chunk in chunks:
            if on_content:
                on_content(chunk)
        return "".join(chunks)

    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        response = await client.post(CHAT + "/stream", json={
            "request_id": str(uuid4()), "message": "海风今天怎么样？",
        })
        assert response.status_code == 200
        wire = json.dumps(seen_front_messages, ensure_ascii=False)
        assert "WorkLifecycle" not in wire
        assert "oneOf" not in wire
        assert "可用能力" not in wire
        assert "<think>" not in response.text
        assert "私有推理不外露" not in response.text
        assert 'event: delta\ndata: "鲸鱼"' in response.text
        assert 'event: delta\ndata: "正在冒泡。"' in response.text
        done = next(
            json.loads(part.split("\ndata: ", 1)[1])
            for part in response.text.split("\n\n")
            if part.startswith("event: done")
        )
        assert done["reply_text"] == "鲸鱼正在冒泡。"


async def test_background_router_uses_chat_lane_and_releases_before_foreground_dialogue(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime

    # (a) Verify chat lane preempts queued background turns.
    order = []
    bg_ticket = await runtime.operation_turns.acquire("background")
    runtime.operation_turns.started(bg_ticket)

    async def queued_bg():
        async with runtime.offline_generation("background"):
            order.append("bg")

    async def preempting_chat():
        async with runtime.chat_routing_generation():
            order.append("chat")

    t_bg = asyncio.create_task(queued_bg())
    await asyncio.sleep(0)
    t_chat = asyncio.create_task(preempting_chat())
    await asyncio.sleep(0)
    runtime.operation_turns.release(bg_ticket)
    await asyncio.wait_for(asyncio.gather(t_bg, t_chat), 3)
    assert order == ["chat", "bg"]

    # (b) Verify end-to-end lane handoff and routing parameters.
    lifecycle = []
    router_calls = []
    orig_route_slot = runtime.chat_routing_generation
    orig_op_slot = runtime.operator_generation

    @asynccontextmanager
    async def tracked_route_slot():
        lifecycle.append("enter_chat_route")
        async with orig_route_slot():
            yield
        lifecycle.append("exit_chat_route")

    @asynccontextmanager
    async def tracked_op_slot():
        lifecycle.append("enter_front_op")
        with orig_op_slot():
            yield
        lifecycle.append("exit_front_op")

    # operator_generation returns asyncio.Lock which uses `async with`, so wrap cleanly:
    @asynccontextmanager
    async def tracked_op_async_slot():
        lifecycle.append("enter_front_op")
        async with orig_op_slot():
            yield
        lifecycle.append("exit_front_op")

    async def fake_router(**kwargs):
        router_calls.append(kwargs)
        assert not runtime.generation_lock.locked()
        return ResponseTurn("resp_1", (FunctionCall("c1", "chat", {}),), "", {"usage": {"input_tokens": 50, "output_tokens": 10}})

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        assert runtime.generation_lock.locked()
        assert "exit_chat_route" in lifecycle
        if on_content:
            on_content("在这里。")
        return "在这里。"

    monkeypatch.setattr(runtime, "chat_routing_generation", tracked_route_slot)
    monkeypatch.setattr(runtime, "operator_generation", tracked_op_async_slot)
    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        reply = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": "聊聊海浪"})).raise_for_status().json()
        assert reply["reply_text"] == "在这里。"

    assert lifecycle == ["enter_chat_route", "exit_chat_route", "enter_front_op", "exit_front_op"]
    req, = router_calls
    assert req["thinking"] is True
    assert req["thinking_budget_tokens"] == 160
    assert req["parallel_tool_calls"] is False
    assert req["max_output_tokens"] == 640
    assert req["temperature"] == 0.0
    assert req["store"] is False
    assert req["tool_choice"] == "required"


async def test_prepare_publication_not_pruned_when_video_objects_bound(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime, dynamics = app.state.runtime, app.state.dynamics
    app.state.browsing.discovery.remember_account("100")
    char = runtime.character
    dynamics._save_decision(
        char,
        False,
        {"action": "post", "content": "准备发布一条小动态", "reason": "日常记录", "source_ids": [], "next_evaluation_seconds": 30},
        [],
        schedule=False,
        account_id="100",
        decide_image=False,
        generation_identity=runtime_identity(runtime, char, "100"),
    )
    bound_objects = [
        {"kind": "watched_video", "identity": BV, "label": "海边的日记（BV1ab411c7mD）", "part": 2, "seconds": 19},
        {"kind": "dynamic_post", "identity": "dyn_1", "label": "之前的动态"},
        {"kind": "watch_history", "identity": "2026-11-22", "label": "2026-11-22 的观看记录"},
    ]
    runtime.store.execute(
        "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
        (
            str(uuid4()),
            char["id"],
            char["version"],
            "看看刚才的视频和记录",
            dump({
                "reply_text": "已查看",
                "account_id": "100",
                "owner_source": runtime.tasks.identity.principal,
                "object_scope": object_scope(runtime),
                "conversation_objects": bound_objects,
            }),
            time.time(),
        ),
    )

    tools = turn_router.route_tools(
        runtime,
        candidates=[{"ref": f"obj{i}", "kind": o["kind"], "label": o["label"], "identity": o["identity"]}
                    for i, o in enumerate(bound_objects, 1)],
    )
    assert "prepare_publication" in {tool["name"] for tool in tools}

    offered_tools = []

    async def fake_router(**kwargs):
        offered_tools.append({tool["name"] for tool in kwargs["tools"]})
        return ResponseTurn("resp_pub", (FunctionCall("c_pub", "prepare_publication", {"publication_channel": "dynamics"}),), "", {})

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        return "待发动态已经准备好。"

    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        res = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": "准备把待发动态发出去"})).raise_for_status().json()
        assert "prepare_publication" in offered_tools[0]
        assert res["card"]["type"] in {"publication_queue", "agent_task"}
        if res["card"]["type"] == "publication_queue":
            assert any(group["channel"] == "dynamics" for group in res["card"]["data"]["groups"])


async def test_singular_deictic_over_multiple_same_kind_candidates_returns_clarify(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    char = runtime.character
    other_bv = "BV1ab411c7mE"

    def seed_videos(account):
        app.state.browsing.discovery.remember_account(account)
        now = time.time()
        for bvid, title, quote in ((BV, "海边的日记", "蓝色灯塔"), (other_bv, "台风天码头", "老码头浪花")):
            body = {
                "metadata": {"bvid": bvid, "title": title, "owner": bvid[-1], "parts": [{"cid": 100}, {"cid": 101}]},
                "parts": [{"cid": 101, "visual": True, "segments": [{"start": 19, "end": 22, "text": quote}]}],
            }
            runtime.store.execute(
                "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state)"
                " VALUES (?,?,?,?,'[]',?,'viewed')",
                (account, char["id"], bvid, dump(body), now),
            )
            round_id = str(uuid4())
            runtime.store.execute(
                "INSERT INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,config)"
                " VALUES (?,?,?,?,'completed',0,'{}')",
                (round_id, account, char["id"], char["version"]),
            )
            runtime.store.execute(
                "INSERT INTO browsing_tasks(id,round_id,bvid,status,material_key,payload,updated_at)"
                " VALUES (?,?,?,'selected','rec', '{}',?)",
                (str(uuid4()), round_id, bvid, now),
            )
        runtime.store.execute(
            "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
            (
                str(uuid4()),
                char["id"],
                char["version"],
                "刚看过哪两部？",
                dump({
                    "reply_text": "看了海边的日记和台风天码头。",
                    "account_id": account,
                    "owner_source": runtime.tasks.identity.principal,
                    "object_scope": object_scope(runtime),
                    "conversation_objects": [
                        {"kind": "watched_video", "identity": BV, "label": "海边的日记"},
                        {"kind": "watched_video", "identity": other_bv, "label": "台风天码头"},
                    ],
                }),
                now,
            ),
        )

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        return "这是你要的视频地址。"

    guessed_ref = "obj1"

    async def fake_router(**kwargs):
        return ResponseTurn(
            "resp_v",
            (FunctionCall("c_v", "instant_read", {
                "action": "workshop_lookup",
                "object_ref": guessed_ref,
                "lookup": {"collection": "video", "terms": []},
            }),),
            "",
            {},
        )

    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        # Case 1: Singular deictic over 2 same-kind candidates forces clarify despite model guess.
        seed_videos("acc1")
        clarify_res = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": "把那条地址给我"})).raise_for_status().json()
        assert clarify_res["card"] is None
        assert "1." in clarify_res["reply_text"] and "2." in clarify_res["reply_text"]

        # Case 2: Ordinal reference disambiguates cleanly.
        seed_videos("acc2")
        guessed_ref = "obj2"
        ordinal_res = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": "把第2条地址给我"})).raise_for_status().json()
        assert other_bv in json.dumps(ordinal_res, ensure_ascii=False)
        assert "1." not in ordinal_res["reply_text"]

        # Case 3: Explicit unique BV resolves cleanly without clarify.
        seed_videos("acc3")
        guessed_ref = "obj2"
        explicit_res = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": f"把 {other_bv} 的地址给我"})).raise_for_status().json()
        assert other_bv in json.dumps(explicit_res, ensure_ascii=False)
        assert "1." not in explicit_res["reply_text"]


async def test_nonblocking_delegation_push_back_and_task_paraphrase(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    step_started = asyncio.Event()
    release_step = asyncio.Event()
    router_actions = iter([
        FunctionCall("c_del", "delegate_task", {"goal": "查演播间", "deliverable": "状态摘要"}),
        FunctionCall("c_chat", "chat", {}),
    ])

    async def fake_router(**kwargs):
        return ResponseTurn("resp_d", (next(router_actions),), "", {})

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        user_msg = messages[-1]["content"]
        if "向我同步" in user_msg:
            return "大肥鱼帮你查完啦，演播间待命中。"
        if "查演播间" in user_msg:
            return "好咧，先让后台去查看。"
        return "海风吹得正舒服呢。"

    async def fake_step(task):
        step_started.set()
        await release_step.wait()
        runtime.tasks._save(task, status="completed", message="演播间待命")

    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(runtime.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)
    monkeypatch.setattr(runtime.tasks, "_step", fake_step)

    task_id = str(uuid4())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1",
    ) as client:
        accepted = (await client.post(CHAT, json={"request_id": task_id, "message": "帮我查演播间"})).raise_for_status().json()
        assert accepted["card"]["type"] == "agent_task"
        assert accepted["reply_text"] == "好咧，先让后台去查看。"
        await asyncio.wait_for(step_started.wait(), 3)

        # Foreground chat succeeds immediately while the delegated task step is blocked.
        chat_reply = (await client.post(CHAT, json={"request_id": str(uuid4()), "message": "先聊两句，你在看海吗"})).raise_for_status().json()
        assert chat_reply["reply_text"] == "海风吹得正舒服呢。"

        # Push back SSE stream receives completion delivery once the task finishes.
        events_route = next(r for r in app.routes if getattr(r, "path", None) == CHAT + "/events")
        service = next(cell.cell_contents for cell in events_route.endpoint.__closure__ if hasattr(cell.cell_contents, "transport"))
        sub_queue = service.transport.subscribe_deliveries()
        event_stream = service.transport.delivery_events()
        ready_chunk = await asyncio.wait_for(anext(event_stream), 2)
        assert "event: ready" in ready_chunk

        release_step.set()
        pushed = await asyncio.wait_for(sub_queue.get(), 5)
        assert pushed["task_id"] == task_id and pushed["status"] == "completed"
        delivery_chunk = await asyncio.wait_for(anext(event_stream), 5)
        await event_stream.aclose()
        service.transport.unsubscribe_deliveries(sub_queue)
        assert "event: delivery" in delivery_chunk
        payload = json.loads(delivery_chunk.split("data: ", 1)[1].strip())
        assert payload["task_delivery"]["task_id"] == task_id
        assert payload["reply_text"] == "大肥鱼帮你查完啦，演播间待命中。"

        packet = DeliveryPacket(kind="task_completed", summary="演播间待命", facts={"state": "ready"})
        voiced = await paraphrase_task_delivery(runtime, runtime.character, packet)
        assert voiced == "大肥鱼帮你查完啦，演播间待命中。"


async def test_voice_utterance_uses_unified_background_router(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    captured = []
    entered_chat_lane = False
    orig_slot = runtime.chat_routing_generation

    @asynccontextmanager
    async def tracked_slot():
        nonlocal entered_chat_lane
        entered_chat_lane = True
        async with orig_slot():
            yield

    async def fake_router(**kwargs):
        captured.append(kwargs)
        return ResponseTurn("resp_voice", (FunctionCall("c_v1", "delegate", {"quote": "帮我查一下今天的日志"}),), "", {})

    class _ChatStub:
        def __init__(self, rt):
            self.runtime = rt

    monkeypatch.setattr(runtime, "chat_routing_generation", tracked_slot)
    monkeypatch.setattr(runtime.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: fake_router)

    plan = await private_call_work.classify(
        _ChatStub(runtime),
        "帮我查一下今天的日志",
        [{"id": str(uuid4()), "goal": "旧任务", "status": "running"}],
    )
    assert plan == private_call_work.VoicePlan(action="delegate", quote="帮我查一下今天的日志", task_id=None)
    assert entered_chat_lane
    req, = captured
    assert req["thinking"] is False
    assert req["parallel_tool_calls"] is False
    assert req["max_output_tokens"] == 256
    assert req["temperature"] == 0.0
    assert req["store"] is False
    assert req["tool_choice"] == "required"


async def test_flat_tool_lowering_and_no_receipt_json_poisoning(settings):
    routed_lookup = turn_router._route_from_tool_call(
        "instant_read",
        {"action": "workshop_lookup", "collection": "dynamic", "day": "今天", "terms": ["海风"]},
    )
    assert routed_lookup.lookup == workshop_lookup.WorkshopLookup(collection="dynamic", day="today", terms=["海风"])
    routed_recall = turn_router._route_from_tool_call(
        "instant_read",
        {"action": "recall_memory", "topic": "鲸鱼声音"},
    )
    assert routed_recall.recall is not None and routed_recall.recall.topic == "鲸鱼声音"

    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    app.state.browsing.discovery.remember_account("100")
    char = rt.character
    from tuber_studio.agent.chat.context import ChatContext
    ctx = ChatContext(rt, rt.store, app.state.browsing, app.state.dynamics)
    rt.store.execute(
        "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
        (
            str(uuid4()),
            char["id"],
            char["version"],
            "今天看了几部视频？",
            dump({
                "reply_text": "今天看了 2 部视频。",
                "account_id": "100",
                "owner_source": rt.tasks.identity.principal,
                "memory_intent": "workshop_lookup",
                "assistant_read_scope": object_scope(rt),
                "tool_receipt": {"state": "ok", "group": "workshop_lookup", "values": {"completed": 2}},
                "conversation_objects": [{"kind": "watched_video", "identity": BV, "label": "海边的日记"}],
            }),
            time.time(),
        ),
    )
    history = ctx.assistant_history(char, "100", full_turns=True)
    assert len(history) == 2
    assert history[1]["content"] == "今天看了 2 部视频。"
    assert "工坊读取回执" not in history[1]["content"]


async def test_inference_budget_exceeded_logs_sanitized_warning_and_keeps_error_text(monkeypatch, caplog):
    tool = function("lookup", "Read ticket.", {"type": "object", "properties": {}, "additionalProperties": False})

    async def transport(*args, **kwargs):
        return {
            "id": "resp_budget",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"output_tokens": 1300, "output_tokens_details": {"reasoning_tokens": 1295}},
            "output": [],
        }

    monkeypatch.setattr("tuber_studio.infra.inference.responses_provider.response_json", transport)
    async with ResponsesClient({"base_url": "https://api.example/llm/v1", "model": "worker"}) as client:
        with caplog.at_level(logging.WARNING, logger="tuber_studio.infra.inference.responses_provider"):
            with pytest.raises(InferenceBudgetExceeded, match="1300 token"):
                await client.create("SECRET_PROMPT_XYZ", tools=[tool], max_output_tokens=1300)
    assert "max_output_tokens=1300" in caplog.text
    assert "output_tokens=1300" in caplog.text
    assert "reasoning_tokens=1295" in caplog.text
    assert "SECRET_PROMPT_XYZ" not in caplog.text


async def test_route_tools_schema_normalization_and_intent_guards(settings):
    from types import SimpleNamespace

    from tuber_studio.agent.chat.context import ChatContext
    from tuber_studio.agent.chat.lookup.personal_agent import ConversationObject
    from tuber_studio.agent.memory_organizer import recall as recall_memory

    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    tools = turn_router.route_tools(rt)
    names = [item["name"] for item in tools]
    assert names.index("delegate_task") < names.index("instant_read")
    instant = next(item for item in tools if item["name"] == "instant_read")
    direct = next(item for item in tools if item["name"] == "direct_action")
    delegate = next(item for item in tools if item["name"] == "delegate_task")
    assert "clock" not in instant["parameters"]["properties"]["action"]["enum"]
    assert "video_activity" not in instant["parameters"]["properties"]["action"]["enum"]
    assert instant["parameters"]["properties"]["action"]["enum"][0] == "workshop_lookup"
    assert instant["parameters"]["properties"]["identity_subject"]["enum"] == [
        "bilibili_owner",
        "operating_account",
        "both",
    ]
    assert "anyOf" not in json.dumps(delegate["parameters"]["properties"]["arrangement"], ensure_ascii=False)
    assert "arrangement" not in direct["parameters"]["properties"]
    assert "arrangement" in next(
        item for item in turn_router.route_tools(rt, cards=[{"id": "t1"}]) if item["name"] == "direct_action"
    )["parameters"]["properties"]

    owner_route = turn_router._route_from_tool_call(
        "instant_read",
        {"action": "identity_read", "identity_subject": "owner"},
    )
    assert [item.subject for item in owner_route.identities] == ["bilibili_owner"]
    setting_route = turn_router._route_from_tool_call(
        "instant_read",
        {"action": "daily_settings", "settings_group": "music", "setting_fields": '["cooldown_seconds"]'},
    )
    assert setting_route.setting_fields == ["cooldown_seconds"]

    ctx = ChatContext(rt, rt.store, app.state.browsing, app.state.dynamics)
    char = rt.character
    live_intent = await turn_router.route_to_chat_intent(
        ctx,
        turn_router._route_from_tool_call("instant_read", {"action": "owner_binding"}),
        SimpleNamespace(message="去看看你主人绑定的 B 站主页，告诉我账号是谁。"),
        char,
        {"objects": {}},
    )
    assert live_intent.action == "owner_profile"

    local_intent = await turn_router.route_to_chat_intent(
        ctx,
        turn_router._route_from_tool_call("instant_read", {"action": "owner_binding"}),
        SimpleNamespace(message="那我去哪能找到他的主页？数字编号也带上。"),
        char,
        {"objects": {"obj1": ConversationObject(kind="owner", identity="424242", label="号主（UID 424242）")}},
    )
    assert local_intent.action == "owner_binding"

    recalled_intent = await turn_router.route_to_chat_intent(
        ctx,
        turn_router._route_from_tool_call("instant_read", {"action": "recall_memory", "topic": "刚才那条评论"}),
        SimpleNamespace(message="刚才那条评论具体从哪里读到的？"),
        char,
        {
            "objects": {
                "obj1": ConversationObject(kind="recalled_source", identity="comment:BV1ab411c7mD:12:12", label="读到的评论"),
            }
        },
    )
    assert recalled_intent.action == recall_memory.NAME
    assert recalled_intent._recall is not None
    assert recalled_intent._recall.source_id == "comment:BV1ab411c7mD:12:12"

    invalid_day_route = turn_router._route_from_tool_call(
        "instant_read",
        {"action": "workshop_lookup", "collection": "comment", "day": "last_week", "terms": ["鲸鱼"]},
    )
    assert invalid_day_route.lookup == workshop_lookup.WorkshopLookup(collection="comment", day=None, terms=["鲸鱼"])

    unscheduled_route = turn_router._route_from_tool_call(
        "delegate_task",
        {
            "goal": "核实演播间是否开播",
            "deliverable": "私下的交接便笺",
            "arrangement": {"kind": "once", "quote": "替我核实演播间现在有没有开播，留份私下的交接便笺，等我回来再看。"},
        },
    )
    unscheduled_intent = await turn_router.route_to_chat_intent(
        ctx,
        unscheduled_route,
        SimpleNamespace(message="替我核实演播间现在有没有开播，留份私下的交接便笺，等我回来再看。"),
        char,
        {"objects": {}},
    )
    assert unscheduled_intent.action == "agent_task"
    assert unscheduled_intent.arrangement is None

    scheduled_intent = await turn_router.route_to_chat_intent(
        ctx,
        unscheduled_route,
        SimpleNamespace(message="明天帮我核实演播间开播状态，留份便笺。"),
        char,
        {"objects": {}},
    )
    assert scheduled_intent.action == "agent_task"
    assert scheduled_intent.arrangement is not None
    assert scheduled_intent.arrangement.kind == "once"


async def test_router_messages_use_native_history_and_recalled_source_subkind_disambiguation(settings):
    from types import SimpleNamespace

    char = {"id": "whale", "name": "大肥鱼"}
    packet = {
        "local_calendar": "2026年11月22日 星期日 12:00",
        "recent_dialogue": [
            {"role": "user", "content": "打个招呼"},
            {"role": "assistant", "content": "你好呀"},
        ],
    }
    msgs = turn_router._build_router_messages(char, SimpleNamespace(message="看下画布尺寸"), packet)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "打个招呼"
    assert msgs[2]["content"] == "你好呀"
    final_payload = json.loads(msgs[-1]["content"])
    assert "recent_dialogue" not in final_payload
    assert list(final_payload)[-1] == "message"
    assert final_payload["message"] == "看下画布尺寸"

    recall_route = turn_router._route_from_tool_call(
        "instant_read",
        {"action": "recall_memory", "topic": "刚才那条评论", "object_ref": "obj1"},
    )
    mixed_candidates = [
        {"ref": "obj1", "kind": "recalled_source", "label": "读到的评论", "identity": "comment:BV1ab411c7mD:12:12"},
        {"ref": "obj2", "kind": "recalled_source", "label": "观看片段", "identity": "video:BV1ab411c7mD"},
    ]
    resolved = turn_router._disambiguate_route(recall_route, "刚才那条评论具体从哪里读到的？", mixed_candidates)
    assert resolved.action == "recall_memory" and resolved.object_ref == "obj1"

    same_subkind = [
        {"ref": "obj1", "kind": "recalled_source", "label": "读到的评论A", "identity": "comment:BV1ab411c7mD:12:12"},
        {"ref": "obj2", "kind": "recalled_source", "label": "读到的评论B", "identity": "comment:BV1ab411c7mD:13:13"},
    ]
    ambiguous = turn_router._disambiguate_route(recall_route, "刚才那条评论具体从哪里读到的？", same_subkind)
    assert ambiguous.route == "clarify" and "1." in ambiguous.question and "2." in ambiguous.question

    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    no_clar = turn_router.route_tools(rt, has_clarification=False)
    with_clar = turn_router.route_tools(rt, has_clarification=True)
    assert "clarification_id" not in next(t for t in no_clar if t["name"] == "delegate_task")["parameters"]["properties"]
    assert "clarification_id" not in next(t for t in no_clar if t["name"] == "clarify")["parameters"]["properties"]
    assert "clarification_id" in next(t for t in with_clar if t["name"] == "delegate_task")["parameters"]["properties"]
    assert "clarification_id" in next(t for t in with_clar if t["name"] == "clarify")["parameters"]["properties"]
    assert "完全忽略 clarification" in msgs[0]["content"]
