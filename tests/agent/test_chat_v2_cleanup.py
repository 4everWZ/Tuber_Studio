"""Tests for Phase A chat cleanup, calm delegation/SSE events, L2 bridge, and ops sync."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.chat import front_dialogue, l2_bridge
from tuber_studio.agent.chat import router as turn_router
from tuber_studio.agent.chat.dispatcher import CapabilityDispatcher
from tuber_studio.agent.chat.lookup.personal_agent import CAPABILITIES
from tuber_studio.agent.tasks import delegation as task_delegation
from tuber_studio.agent.tasks.delivery import DelegationError
from tuber_studio.agent.tasks.runtime import Tool
from tuber_studio.app import create_app
from tuber_studio.core.config import ROOT
from tuber_studio.core.models import StrictModel
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn

CHAT = "/api/v1/agent/chat"
CHAT_STREAM = "/api/v1/agent/chat/stream"


def _parse_sse(body: str) -> list[tuple[str, object]]:
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        event_name = ""
        data_raw = ""
        for line in lines:
            if line.startswith("event: "):
                event_name = line[7:].strip()
            elif line.startswith("data: "):
                data_raw = line[6:]
        if event_name:
            events.append((event_name, json.loads(data_raw) if data_raw else None))
    return events


def test_whale_preset_and_chat_prompts_drop_workshop_jargon():
    raw = json.loads((ROOT / "config" / "characters.json").read_text(encoding="utf-8"))
    whale = next(item for item in raw if item["id"] == "whale")
    assert whale["name"] == "Aqua" and whale["display_name"] == "Aqua"
    assert "大肥鱼" in whale["aliases"]
    assert len(whale["lore"]) == 3
    for field in ("self_description", "speaking_style", "core", "style", "rules", "lore"):
        text = json.dumps(whale[field], ensure_ascii=False)
        assert "工坊" not in text and "号主" not in text and "认知主权" not in text
        for prop in ("贝壳灯", "软垫", "女仆裙", "尾巴怕痒", "算力挂件", "电费", "散热风扇", "苦情剧本", "MCN"):
            assert prop not in text
    assert "Aqua" in whale["self_description"]
    assert "白饭" in whale["core"]["background"] and "摸鱼" in whale["core"]["background"]
    assert "讨白饭" in whale["core"]["blacklist"]
    assert len(whale["core"]["traits"]) == 4 and len(whale["core"]["examples"]) == 5
    assert len(whale["rules"]) == 2
    assert "大肥鱼" in whale["core"]["identity"] and "Aqua" in whale["core"]["identity"]

    custom_char = whale | {"owner_address": "船长"}
    sys_msgs = front_dialogue._thin_system_messages(
        custom_char,
        "req-1",
        "刚才聊了什么？",
        unified_memories=[{"content": "喜欢乌龙茶"}],
        history_enabled=False,
    )
    all_sys = "\n".join(msg["content"] for msg in sys_msgs)
    assert "工坊" not in all_sys and "号主" not in all_sys
    assert "船长" in sys_msgs[0]["content"] and "船长" in sys_msgs[1]["content"]

    router_msgs = turn_router._build_router_messages(
        custom_char,
        SimpleNamespace(message="帮我记住我喜欢乌龙茶"),
        {"local_calendar": "2026年11月24日 星期二 14:00", "recent_dialogue": []},
    )
    assert "工坊" not in router_msgs[0]["content"] and "号主" not in router_msgs[0]["content"]
    caps = json.dumps({k: [v.subject, v.reads, v.changes] for k, v in CAPABILITIES.items()}, ensure_ascii=False)
    assert "工坊" not in caps and "号主" not in caps


async def test_non_read_delegation_serializes_and_errors_stay_calm_without_banner(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    order = []

    async def write_step(request, arguments):
        order.append(("start", request.id))
        await asyncio.sleep(0.01)
        order.append(("end", request.id))
        return {"ok": True}

    runtime.tasks.tools["seq_write"] = Tool("Sequential write", StrictModel, write_step, parallel_read=False)
    task = runtime.tasks.create(str(uuid4()), "测试非只读步骤顺序执行")
    steps = [
        {"id": str(uuid4()), "tool": "seq_write", "revision": task["revision"], "arguments": {}},
        {"id": str(uuid4()), "tool": "seq_write", "revision": task["revision"], "arguments": {}},
    ]
    runtime.tasks._save(
        task,
        steps=steps,
        status="queued",
        plan={"backend": "responses", "prior_goals": []},
    )
    await task_delegation.run_pending(runtime.tasks, runtime.tasks.current(task["id"]), steps, [])
    assert [tag for tag, _ in order] == ["start", "end", "start", "end"]

    # Duplicate calls in one batch deduplicate; unoffered first call records calm invalid_call instead of crashing.
    planner_calls = []

    async def planner(*, input, tools, **kwargs):
        planner_calls.append(len(planner_calls))
        if len(planner_calls) == 1:
            return ResponseTurn("p1", (FunctionCall("c1", "unknown_tool", {}),), "", {})
        if len(planner_calls) == 2:
            return ResponseTurn(
                "p2",
                (
                    FunctionCall("c2a", "live_status", {}),
                    FunctionCall("c2b", "live_status", {}),
                ),
                "",
                {},
            )
        raise DelegationError("并行委派只允许独立读取。")

    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: planner)
    monkeypatch.setattr(runtime.tasks, "requires_responses", lambda: True)
    t2 = runtime.tasks.create(str(uuid4()), "测试委派异常安抚")
    runtime.tasks._save(t2, plan={"backend": "responses", "prior_goals": []})
    t2 = runtime.tasks.current(t2["id"])
    await runtime.tasks._advance(t2)
    after_unoffered = runtime.tasks.current(t2["id"])
    assert after_unoffered["status"] == "queued" and after_unoffered["steps"][-1]["invalid_call"] is True

    await runtime.tasks._advance(after_unoffered)
    after_dup = runtime.tasks.current(t2["id"])
    assert len([s for s in after_dup["steps"] if s["tool"] == "live_status"]) == 1

    await runtime.tasks._advance(after_dup)
    after_run = runtime.tasks.current(t2["id"])
    assert after_run["steps"][-1].get("result") is not None
    await runtime.tasks._advance(after_run)
    failed = runtime.tasks.current(t2["id"])
    assert failed["status"] == "failed"
    assert "并行委派只允许独立读取" not in failed["message"]

    # In chat stream, ValueError / DelegationError constraints become calm tool_activity + stage instead of event: error.
    async def router_model(messages, schema, **kwargs):
        if schema["title"] == "TurnRoute":
            return {"route": "direct_action", "action": "browse", "topic": "测试"}
        return {"reply_markdown": "好的。"}

    async def failing_browse(*args, **kwargs):
        raise DelegationError("并行委派只允许独立读取。")

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", router_model, raising=False)
    monkeypatch.setattr(CapabilityDispatcher, "execute", failing_browse)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        resp = await client.post(CHAT_STREAM, json={"request_id": str(uuid4()), "message": "帮我刷一下视频"})
    events = _parse_sse(resp.text)
    names = [name for name, _ in events]
    assert "error" not in names and "done" in names
    assert ("stage", "constrained") in events
    constrained = [data for name, data in events if name == "tool_activity" and data["status"] == "constrained"]
    assert constrained and "并行委派只允许独立读取" not in constrained[0]["summary"]
    done_turn = next(data for name, data in events if name == "done")
    assert "并行委派只允许独立读取" not in done_turn["reply_text"]
    app.state.store.close()


async def test_saved_memory_and_tool_activity_sse_plus_l2_contracts(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime

    async def router_model(messages, schema, **kwargs):
        if schema["title"] != "TurnRoute":
            return {"reply_markdown": "记好啦。"}
        msg = json.loads(messages[-1]["content"])["message"]
        if msg == "记住我喜欢乌龙茶":
            return {
                "route": "direct_action",
                "action": "save_memory",
                "memory_content": "喜欢喝无糖乌龙茶",
                "memory_tags": ["饮食"],
            }
        if msg == "把乌龙茶记忆改成冷泡绿茶":
            return {
                "route": "direct_action",
                "action": "update_memory",
                "topic": "乌龙茶",
                "memory_content": "喜欢喝冷泡绿茶",
            }
        if msg == "忘掉绿茶那条记忆":
            return {"route": "direct_action", "action": "delete_memory", "topic": "绿茶"}
        return {"route": "instant_read", "action": "recall_memory", "recall": {"topic": "绿茶"}}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", router_model, raising=False)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        save_resp = await client.post(CHAT_STREAM, json={"request_id": str(uuid4()), "message": "记住我喜欢乌龙茶"})
        save_events = _parse_sse(save_resp.text)
        assert any(name == "tool_activity" and data["tool"] == "save_memory" and data["status"] == "completed"
                   for name, data in save_events)
        created = next(data for name, data in save_events if name == "memory_updated")
        assert created["action"] == "created" and created["scope"] == "owner" and "乌龙茶" in created["snippet"]

        upd_resp = await client.post(CHAT_STREAM, json={"request_id": str(uuid4()), "message": "把乌龙茶记忆改成冷泡绿茶"})
        upd_events = _parse_sse(upd_resp.text)
        updated = next(data for name, data in upd_events if name == "memory_updated")
        assert updated["action"] == "updated" and "冷泡绿茶" in updated["snippet"]

        rec_resp = await client.post(CHAT_STREAM, json={"request_id": str(uuid4()), "message": "我还说过什么茶？"})
        rec_events = _parse_sse(rec_resp.text)
        assert any(name == "tool_activity" and data["tool"] == "recall_history" and data["status"] == "completed"
                   for name, data in rec_events)

        del_resp = await client.post(CHAT_STREAM, json={"request_id": str(uuid4()), "message": "忘掉绿茶那条记忆"})
        del_events = _parse_sse(del_resp.text)
        deleted = next(data for name, data in del_events if name == "memory_updated")
        assert deleted["action"] == "deleted" and deleted["scope"] == "owner"
        assert runtime.unified_memory.for_prompt("owner", character_id=runtime.character["id"]) == []

    # Verify l2_bridge invokes @kernel-core L2 APIs with exact #147/#152 contracts when present.
    called = []
    fake_self = SimpleNamespace(
        build_self_model_system_message=lambda char, **kw: (called.append(("self_model", kw)) or {"role": "system", "content": "ok"})
    )
    fake_tree = SimpleNamespace(
        build_base_node=lambda char, **kw: called.append(("base_node", kw)) or {"id": "base"},
        create_context_tree=lambda base: called.append(("create_tree", base)) or {"root": base},
        fork_owner_chat_branch=lambda tree, **kw: called.append(("fork_owner", kw)) or SimpleNamespace(node_id="b1"),
        render_openai_messages=lambda tree, nid: called.append(("render", nid)) or [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
    )
    fake_unified = SimpleNamespace(
        save_memory=lambda store, **kw: called.append(("l2_save", kw)) or {"ok": True, "action": "created", "scope": "owner", "snippet": kw["content"]},
        update_memory=lambda store, **kw: called.append(("l2_update", kw)) or {"ok": True, "action": "updated", "scope": "owner", "snippet": "upd"},
        delete_memory=lambda store, **kw: called.append(("l2_delete", kw)) or {"ok": True, "action": "deleted", "scope": "owner", "snippet": "del"},
    )
    fake_recall = SimpleNamespace(
        recall_history=lambda store, **kw: called.append(("l2_recall", kw)) or {"ok": True, "scope": "owner", "items": [], "count": 0},
    )
    monkeypatch.setattr(l2_bridge, "_persona_self_model", fake_self)
    monkeypatch.setattr(l2_bridge, "_persona_context_tree", fake_tree)
    monkeypatch.setattr(l2_bridge, "_persona_unified", fake_unified)
    monkeypatch.setattr(l2_bridge, "_persona_recall", fake_recall)

    l2_bridge.build_owner_system_prefix(runtime.character, "r1", "hi")
    l2_bridge.build_owner_chat_context(runtime.character, {"text": "hi"}, memories=["m1"])
    l2_bridge.save_owner_memory(runtime.unified_memory, content="x", character_id="whale")
    l2_bridge.update_owner_memory(runtime.unified_memory, memory_id="m1", content="y", character_id="whale")
    l2_bridge.delete_owner_memory(runtime.unified_memory, query="y", character_id="whale")
    l2_bridge.recall_owner_history(runtime.store, query="x", character_id="whale")
    assert [name for name, _ in called] == [
        "self_model", "base_node", "create_tree", "fork_owner", "render",
        "l2_save", "l2_update", "l2_delete", "l2_recall",
    ]
    assert called[5][1]["allowed_scopes"] == ("owner",)
    app.state.store.close()


async def test_chat_start_operations_syncs_starting_and_running_snapshot(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    preflight_entered = asyncio.Event()
    allow_preflight = asyncio.Event()
    orig_preflight = runtime.browsing.preflight

    async def slow_preflight(*, for_automation=False):
        preflight_entered.set()
        await allow_preflight.wait()
        return await orig_preflight(for_automation=for_automation)

    monkeypatch.setattr(runtime.browsing, "preflight", slow_preflight)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        first_call = asyncio.create_task(
            client.post(
                CHAT_STREAM,
                json={"request_id": str(uuid4()), "message": "启动自主运营", "intent": {"action": "start_operations"}},
            )
        )
        try:
            await asyncio.wait_for(preflight_entered.wait(), 3)
            assert runtime.status()["automation"]["state"] == "starting"
            mid_ops = (await client.get("/api/v1/operations/automation")).raise_for_status().json()
            assert mid_ops["state"] == "starting"
        finally:
            allow_preflight.set()

        first_resp = await first_call
        events = _parse_sse(first_resp.text)
        done_turn = next(data for name, data in events if name == "done")
        assert done_turn["card"]["type"] == "operation_control"
        assert done_turn["card"]["data"]["state"] == "running"
        assert done_turn["card"]["data"]["policy"]["enabled"] is True
        assert any(
            name == "tool_activity"
            and data["tool"] == "start_operations"
            and data["status"] == "completed"
            and (data.get("data") or {}).get("automation_state") == "running"
            for name, data in events
        )
        assert runtime.status()["automation"]["state"] == "running"
        assert (await client.get("/api/v1/operations/automation")).raise_for_status().json()["state"] == "running"

        # If Operations UI started preflight concurrently (`starting=True`), chat reports starting calmly.
        await runtime.browsing.stop()
        runtime.browsing.operations.starting = True
        try:
            concurrent = (
                await client.post(
                    CHAT,
                    json={"request_id": str(uuid4()), "message": "再开一次自主运营", "intent": {"action": "start_operations"}},
                )
            ).raise_for_status().json()
            assert concurrent["card"]["type"] == "operation_control"
            assert concurrent["card"]["data"]["state"] == "starting"
            assert "暂未能开启" not in concurrent["reply_text"]
        finally:
            runtime.browsing.operations.starting = False
    app.state.store.close()
