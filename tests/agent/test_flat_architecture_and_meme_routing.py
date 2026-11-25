"""Tests for Two-Layer Flat Architecture, narrow TaskProfiles, depth<=1 guard, and meme gallery routing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.chat import router as turn_router
from tuber_studio.agent.chat.context import ChatContext
from tuber_studio.agent.chat.lookup.personal_agent import object_scope
from tuber_studio.agent.chat.models import ChatIntent, DelegationBrief, TurnRoute
from tuber_studio.agent.chat.router.disambiguation import _disambiguate_route
from tuber_studio.agent.tasks import delegation as task_delegation
from tuber_studio.agent.tasks.runtime import ACTIVE_TASK_EXECUTION, Tool
from tuber_studio.app import create_app
from tuber_studio.core.models import StrictModel
from tuber_studio.core.store import dump
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn

CHAT = "/api/v1/agent/chat"


class _FakeHarvester:
    """Test double implementing both sync_all_sources/process_queue_once and list_inbox/list_sources."""

    def __init__(self, *, source_count: int = 2):
        self.sync_calls: list[str | None] = []
        self.queue_calls = 0
        self.source_count = source_count

    def list_inbox(self, *, status: str = "pending") -> dict:
        return {
            "items": [
                {"item_id": f"in_{i}", "blob_bytes": b"\x89PNG\r\n", "status": status}
                for i in range(3)
            ],
            "pending_count": 3,
            "pending_vision_count": 1,
        }

    def get_config(self) -> dict:
        return {"comment_harvest_enabled": True}

    def list_sources(self) -> dict:
        return {
            "items": [
                {
                    "source_id": f"sub_{i}",
                    "name": f"画师订阅{i}",
                    "kind": "bilibili_creator" if i % 2 else "remote_feed",
                    "enabled": i != 1,
                    "last_status": "ok" if i == 0 else "not_modified",
                    "last_synced_at": 1760000000.0 + i,
                    "last_error": "",
                    "secret_blob": b"RAW_IMAGE_BYTES",
                }
                for i in range(self.source_count)
            ]
        }

    async def sync_source(self, source_id: str) -> dict:
        self.sync_calls.append(source_id)
        return {
            "source_id": source_id,
            "status": "ok",
            "synced": 1,
            "added_inbox": 2,
            "auto_approved": 1,
            "discarded": 0,
            "raw_bytes": b"SHOULD_NOT_LEAK",
        }

    async def sync_all_sources(self) -> dict:
        self.sync_calls.append(None)
        return {
            "synced": self.source_count,
            "added_inbox": 4,
            "auto_approved": 1,
            "discarded": 2,
            "errors": [],
            "raw_bytes": b"SHOULD_NOT_LEAK",
        }

    async def process_queue_once(self) -> dict:
        self.queue_calls += 1
        return {"pending_vision": 1, "errors": []}


class _FakeCatalog:
    def list(self, character, *, include_disabled: bool = False) -> list[dict]:
        return [
            {"asset_id": "m1", "enabled": True},
            {"asset_id": "m2", "enabled": True},
            {"asset_id": "m3", "enabled": False},
        ]


def test_router_exposes_profile_enum_and_meme_gallery_status(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    tools = turn_router.route_tools(rt)
    delegate = next(item for item in tools if item["name"] == "delegate_task")
    profile_schema = delegate["parameters"]["properties"]["profile"]
    assert profile_schema["enum"] == [
        "meme_harvest",
        "video_production",
        "media_inspection",
        "general_research",
    ]
    instant = next(item for item in tools if item["name"] == "instant_read")
    assert "meme_gallery_status" in instant["parameters"]["properties"]["action"]["enum"]
    assert "meme_gallery_status" in turn_router.direct_tool_actions()

    # Disambiguation should never block meme_gallery_status even with singular deictic + multiple candidates.
    route = TurnRoute(route="instant_read", action="meme_gallery_status")
    candidates = [
        {"ref": "obj1", "index": 1, "kind": "watched_video", "label": "视频A", "identity": "BV1"},
        {"ref": "obj2", "index": 2, "kind": "watched_video", "label": "视频B", "identity": "BV2"},
    ]
    assert _disambiguate_route(route, "帮我看下那个待审箱状态", candidates).action == "meme_gallery_status"

    # Prompt explicitly documents bidirectional boundary.
    msgs = turn_router._build_router_messages(
        rt.character,
        SimpleNamespace(message="待审箱有几张图"),
        {"candidates": [], "recent_dialogue": []},
    )
    sys_prompt = msgs[0]["content"]
    assert "meme_gallery_status" in sys_prompt
    assert "meme_harvest" in sys_prompt
    assert "video_production" in sys_prompt


async def test_bidirectional_meme_routing_instant_read_vs_delegate_profile(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    harvester = _FakeHarvester(source_count=25)
    monkeypatch.setattr(rt, "meme_harvester", harvester, raising=False)
    monkeypatch.setattr(rt.pets, "catalog", _FakeCatalog(), raising=False)

    create_calls = []
    orig_create = rt.tasks.create

    def spy_create(*args, **kwargs):
        create_calls.append((args, kwargs))
        return orig_create(*args, **kwargs)

    monkeypatch.setattr(rt.tasks, "create", spy_create)

    captured_worker_tools: list[set[str]] = []

    async def fake_router(**kwargs):
        user_msg = json.loads(kwargs["input"][-1]["content"])["message"]
        if "待审箱" in user_msg or "同步状态" in user_msg:
            return ResponseTurn(
                "resp_read",
                (FunctionCall("c_read", "instant_read", {"action": "meme_gallery_status"}),),
                "",
                {},
            )
        return ResponseTurn(
            "resp_del",
            (
                FunctionCall(
                    "c_del",
                    "delegate_task",
                    {
                        "goal": user_msg,
                        "deliverable": "同步结果统计与 evidence_id",
                        "profile": "meme_harvest",
                    },
                ),
            ),
            "",
            {},
        )

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        return "收到，已经帮你查过也交代下去了。"

    monkeypatch.setattr(rt.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(rt.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(rt.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(rt.tasks, "responses_client", lambda: fake_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        # 1. Foreground instant read: executes immediately, no task created, no sync triggered, sources capped to 20.
        r1 = await client.post(
            CHAT,
            json={"request_id": str(uuid4()), "message": "待审箱有几张图？订阅源上次同步状态怎么样？"},
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["card"]["type"] == "meme_gallery_status"
        summary = body1["card"]["data"]
        assert summary["inbox_pending"] == 3
        assert summary["inbox_pending_vision"] == 1
        assert summary["gallery_total"] == 3
        assert summary["gallery_enabled"] == 2
        assert summary["sources_total"] == 25
        assert summary["sources_enabled"] == 24
        assert len(summary["sources"]) == 20
        assert "secret_blob" not in json.dumps(summary, ensure_ascii=False)
        assert "待审箱现有 3 张待审新图" in body1["reply_text"]
        assert not create_calls
        assert not harvester.sync_calls

        # 2. Heavy sync delegation: creates async task with profile=meme_harvest, does NOT sync inline.
        req2 = str(uuid4())
        r2 = await client.post(
            CHAT,
            json={"request_id": req2, "message": "同步所有大肥鱼订阅源并筛选新图"},
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["card"]["type"] == "agent_task"
        task_id = body2["card"]["data"]["id"]
        assert len(create_calls) == 1
        assert not harvester.sync_calls
        task = rt.tasks.current(task_id)
        assert task is not None
        assert task["plan"]["delegation_brief"]["profile"] == "meme_harvest"

        # 3. Advance the delegated task and verify ONLY meme_harvest tools (+ submit_materials / ask_owner) are mounted.
        async def fake_worker(**kwargs):
            captured_worker_tools.append({t["name"] for t in kwargs["tools"]})
            assert "meme_harvester_status" in kwargs["input"][0]["content"]
            return ResponseTurn("resp_w", (FunctionCall("w1", "sync_meme_sources", {}),), "", {})

        monkeypatch.setattr(rt.tasks, "responses_client", lambda: fake_worker)
        await task_delegation.advance(rt.tasks, rt.tasks.current(task_id))
        assert captured_worker_tools == [
            {"meme_harvester_status", "sync_meme_sources", "submit_materials", "ask_owner"}
        ]
        # Execute pending step through runtime._step() and verify compact sync result.
        await rt.tasks._step(rt.tasks.current(task_id))
        updated = rt.tasks.current(task_id)
        sync_step = next(s for s in updated["steps"] if s["tool"] == "sync_meme_sources")
        res = sync_step["result"]
        assert res["evidence_id"].startswith("meme_sync:")
        assert res["synced"] == 25
        assert res["added_inbox"] == 4
        assert res["auto_approved"] == 1
        assert res["discarded"] == 2
        assert res["pending_vision"] == 1
        assert res["errors"] == []
        assert "raw_bytes" not in json.dumps(res, ensure_ascii=False)
        assert harvester.sync_calls == [None]
        assert harvester.queue_calls == 1


@pytest.mark.parametrize(
    ("profile", "expected_tools"),
    [
        (
            "video_production",
            {
                "plan_video_script",
                "generate_remotion_project",
                "render_remotion_video",
                "stage_or_publish_bilibili_video",
                "workshop_lookup",
            },
        ),
        (
            "media_inspection",
            {"inspect_video", "web_search", "workshop_lookup", "delegate_text"},
        ),
        (
            "general_research",
            {
                "web_search",
                "workshop_lookup",
                "today_activity",
                "operation_status",
                "live_status",
                "game_status",
                "bilibili_owner",
                "operating_account",
                "delegate_text",
            },
        ),
    ],
)
async def test_narrow_task_profiles_mount_only_profile_tools(settings, monkeypatch, profile, expected_tools):
    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    seen_tools: list[set[str]] = []

    async def fake_worker(**kwargs):
        seen_tools.append({t["name"] for t in kwargs["tools"]})
        return ResponseTurn("resp_p", (FunctionCall("p1", "ask_owner", {"question": "ok?"}),), "", {})

    monkeypatch.setattr(rt.tasks, "responses_client", lambda: fake_worker)
    task_id = str(uuid4())
    task = rt.tasks.create(
        task_id,
        "按窄工具集执行",
        delegation={
            "goal": "按窄工具集执行",
            "deliverable": "完成",
            "constraints": [],
            "materials": [],
            "profile": profile,
        },
    )
    await task_delegation.advance(rt.tasks, task)
    assert seen_tools
    mounted = seen_tools[0] - {"submit_materials", "ask_owner"}
    assert mounted == expected_tools
    assert "delegate_task" not in seen_tools[0]


async def test_depth_one_anti_nesting_and_no_delegate_task_in_worker_menu(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime

    # Even if a delegate_task tool were registered in TaskRuntime, delegation.advance() never mounts it.
    rt.tasks.register(
        "delegate_task",
        Tool("forbidden nested delegate", StrictModel, lambda req, args: None, responses_only=True),
    )

    nested_errors: list[str] = []

    async def attempt_nested_create(req, args):
        assert ACTIVE_TASK_EXECUTION.get() is not None
        try:
            rt.tasks.create(str(uuid4()), "子任务套娃")
        except ValueError as err:
            nested_errors.append(str(err))
        return {"ok": True}

    rt.tasks.register(
        "probe_nested_tool",
        Tool("probe nested create", StrictModel, attempt_nested_create, responses_only=True),
    )

    seen_tools: list[set[str]] = []

    async def fake_worker(**kwargs):
        seen_tools.append({t["name"] for t in kwargs["tools"]})
        if ACTIVE_TASK_EXECUTION.get() is not None:
            try:
                rt.tasks.create(str(uuid4()), "advance阶段套娃")
            except ValueError as err:
                nested_errors.append(str(err))
        return ResponseTurn("resp_n", (FunctionCall("n1", "probe_nested_tool", {}),), "", {})

    monkeypatch.setattr(rt.tasks, "responses_client", lambda: fake_worker)
    task_id = str(uuid4())
    task = rt.tasks.create(
        task_id,
        "检查防套娃",
        delegation={"goal": "检查防套娃", "deliverable": "验证", "constraints": [], "materials": []},
    )
    await task_delegation.advance(rt.tasks, task)
    assert seen_tools and "delegate_task" not in seen_tools[0]

    await rt.tasks._step(rt.tasks.current(task_id))
    assert len(nested_errors) == 2
    assert all("最大派发深度为 1" in msg for msg in nested_errors)
    assert ACTIVE_TASK_EXECUTION.get() is None


async def test_instant_read_json_never_pollutes_subsequent_front_dialogue(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    rt = app.state.runtime
    harvester = _FakeHarvester(source_count=2)
    monkeypatch.setattr(rt, "meme_harvester", harvester, raising=False)
    monkeypatch.setattr(rt.pets, "catalog", _FakeCatalog(), raising=False)
    app.state.browsing.discovery.remember_account("100")

    captured_dialogue_messages: list[list[dict]] = []

    async def fake_router(**kwargs):
        user_msg = json.loads(kwargs["input"][-1]["content"])["message"]
        if "待审箱" in user_msg:
            return ResponseTurn(
                "resp_r1",
                (FunctionCall("c1", "instant_read", {"action": "meme_gallery_status"}),),
                "",
                {},
            )
        return ResponseTurn("resp_c2", (FunctionCall("c2", "chat", {}),), "", {})

    async def fake_front_stream(messages, *, max_tokens, on_content=None, on_stage=None, **kwargs):
        captured_dialogue_messages.append(messages)
        return "好的，我们继续聊。"

    monkeypatch.setattr(rt.llm, "uses_model_card", True, raising=False)
    monkeypatch.setattr(rt.llm, "capacity_window", 8192, raising=False)
    monkeypatch.setattr(rt.llm, "generate_text_stream", fake_front_stream, raising=False)
    monkeypatch.setattr(rt.tasks, "responses_client", lambda: fake_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        r1 = await client.post(CHAT, json={"request_id": str(uuid4()), "message": "待审箱有几张图？"})
        assert r1.status_code == 200
        r2 = await client.post(CHAT, json={"request_id": str(uuid4()), "message": "辛苦啦，聊点别的"})
        assert r2.status_code == 200

    assert captured_dialogue_messages
    wire_text = json.dumps(captured_dialogue_messages[-1], ensure_ascii=False)
    assert "待审箱现有 3 张待审新图" in wire_text
    assert "inbox_pending_vision" not in wire_text
    assert "comment_harvest_enabled" not in wire_text
    assert "meme_gallery" not in wire_text
    assert "画师订阅1" not in wire_text
    assert "remote_feed" not in wire_text
    assert "last_synced_at" not in wire_text

    # Guard against raw JSON leaked into reply_text on stored turns.
    ctx = ChatContext(rt, rt.store, app.state.browsing, app.state.dynamics)
    char = rt.character
    rt.store.execute(
        "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
        (
            str(uuid4()),
            char["id"],
            char["version"],
            "raw json leak probe",
            dump({
                "reply_text": '{"inbox_pending": 99, "raw": true}',
                "account_id": "100",
                "owner_source": rt.tasks.identity.principal,
                "memory_intent": "meme_gallery_status",
                "assistant_read_scope": object_scope(rt),
            }),
            9999999999.0,
        ),
    )
    history = ctx.assistant_history(char, "100", full_turns=True)
    assert not any("raw json leak probe" in msg["content"] or "inbox_pending" in msg["content"] for msg in history)
