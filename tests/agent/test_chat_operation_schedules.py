"""Tests for conversational scheduling and status reporting of autonomous operations."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.chat.lookup import operator_facts
from tuber_studio.app import create_app


def _make_dual_router(route_fn):
    async def router_model(messages, schema, *, max_tokens):
        if schema["title"] == "TurnRoute":
            message = json.loads(messages[-1]["content"])["message"]
            return route_fn(message)
        if schema["title"] == "FrontDialogueReply":
            sys_msg = messages[-2]["content"]
            marker = "以下是本轮已经取得的读取回执："
            if marker in sys_msg:
                summary = json.loads(sys_msg.split(marker, 1)[1])["summary"]
                return {"reply_markdown": summary}
            return {"reply_markdown": "好呀。"}
        return {}

    return router_model


def test_chat_conversational_duration_window_daily_list_and_cancel(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())

    def route_for(message: str):
        if "看看" in message or "有哪些" in message:
            return (
                "direct_action",
                {"action": "schedule_operations", "operation_schedule": {"operation": "list"}},
            )
        if "取消" in message:
            return (
                "direct_action",
                {"action": "schedule_operations", "operation_schedule": {"operation": "cancel"}},
            )
        if "运营状态" in message:
            return ("instant_read", {"action": "operation_status"})
        # Simulate router picking start_operations on a timed phrase to exercise fallback promotion
        return ("direct_action", {"action": "start_operations"})

    async def fake_responses_client(**kwargs):
        user_payload = json.loads(kwargs["input"][-1]["content"])
        tool_name, arguments = route_for(user_payload["message"])
        return SimpleNamespace(
            calls=[SimpleNamespace(name=tool_name, arguments=arguments)],
            raw={},
        )

    fake_responses_client._requires_omni_key = False
    model = _make_dual_router(lambda m: route_for(m)[1])
    monkeypatch.setattr(app.state.runtime.llm, "generate_json", model)
    monkeypatch.setattr(app.state.runtime.llm, "generate_assistant_json", model, raising=False)
    monkeypatch.setattr(
        app.state.runtime.tasks,
        "responses_client",
        lambda: fake_responses_client,
    )
    app.state.runtime.llm.uses_model_card = True

    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"

        # 1. Start a 1-hour timed browsing/operations run via natural chat
        r1 = client.post(
            path,
            json={"request_id": str(uuid4()), "message": "让Aqua刷一个小时视频，自主运营"},
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["card"]["type"] == "operation_control"
        assert d1["card"]["data"]["policy"]["enabled"] is True
        active_sched = d1["card"]["data"]["active_schedule"]
        assert active_sched is not None
        assert active_sched["duration_minutes"] == 60
        assert "自动停止" in d1["reply_text"]

        # 2. Check operation_status includes active schedule & remaining time
        r_status = client.post(path, json={"request_id": str(uuid4()), "message": "现在自主运营状态怎么样"})
        assert r_status.status_code == 200
        assert "定时计划" in r_status.json()["reply_text"]
        assert "自动停止" in r_status.json()["reply_text"]

        # 3. Configure daily schedule via natural chat
        r2 = client.post(
            path,
            json={"request_id": str(uuid4()), "message": "每天 20:00 到 22:00 自动刷视频"},
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert "20:00–22:00" in d2["reply_text"]

        # 4. List scheduled operations
        r3 = client.post(path, json={"request_id": str(uuid4()), "message": "看看现在有哪些定时运营计划"})
        assert r3.status_code == 200
        assert "60 分钟" in r3.json()["reply_text"]
        assert "20:00–22:00" in r3.json()["reply_text"]

        # 5. Cancel all scheduled operations via chat
        r4 = client.post(path, json={"request_id": str(uuid4()), "message": "取消所有定时运营计划"})
        assert r4.status_code == 200
        assert "已取消" in r4.json()["reply_text"]
        assert app.state.browsing.schedules.list(include_finished=False) == []
        for _ in range(40):
            if not app.state.browsing.operations.policy()["enabled"]:
                break
            time.sleep(0.05)
        assert app.state.browsing.operations.policy()["enabled"] is False


def test_chat_once_window_schedule_rolls_overnight_and_future_day(settings, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    monkeypatch.setattr(operator_facts, "now", lambda _browsing: operator_facts.datetime(2026, 11, 27, 21, 0, tzinfo=operator_facts.ZoneInfo("Asia/Taipei")))

    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        path = "/api/v1/agent/chat"
        res = client.post(
            path,
            json={
                "request_id": str(uuid4()),
                "message": "今晚 23:00 到 00:30 开自主运营",
                "intent": {
                    "action": "schedule_operations",
                    "operation_schedule": {
                        "operation": "create",
                        "kind": "once_window",
                        "start_time": "23:00",
                        "end_time": "00:30",
                    },
                },
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert "2026-11-27 23:00" in body["reply_text"]
        assert "2026-11-28 00:30" in body["reply_text"]
        items = app.state.browsing.schedules.list(include_finished=False)
        assert len(items) == 1
        assert items[0]["status"] == "scheduled"
