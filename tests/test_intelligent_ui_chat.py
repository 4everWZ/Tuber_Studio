from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tuber_studio.agent.api import register_agent_api
from tuber_studio.models import Reply


def test_intelligent_chat_endpoints(store):
    app = FastAPI()
    char = store.character()

    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value=Reply(spoken_text="今天雨下得好大，本鱼尾巴都要泡浮肿了啦！", emotion="cheerful", animation_id="talk_happy", reply_to=[])
    )

    runtime = SimpleNamespace(
        character=char,
        store=store,
        llm=mock_llm,
        live=SimpleNamespace(session_id="test_live", busy=False, recent_barrage=[]),
        offline_generation=MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())),
        generation_lock=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
        generate_reply=AsyncMock(
            return_value=(Reply(spoken_text="饲养员今天辛苦啦！要记得按时吃白饭哦~", emotion="cheerful", animation_id="talk_happy", reply_to=[]), {})
        ),
    )
    comments = SimpleNamespace(
        account_context_scope=lambda v, t: f"comments:{v}:{t}",
        display_account_id=lambda: "acc_1",
        direct_messages=None,
    )
    settings = SimpleNamespace(
        voice=lambda v: None,
        assets=lambda a: ["dummy"],
    )

    register_agent_api(app, settings, runtime, store, comments, require_admin=lambda: True)
    client = TestClient(app)

    # 1. Empty message -> 400
    res = client.post("/api/chat/intelligent", json={"message": ""})
    assert res.status_code == 400

    # 2. Dynamic generation card
    res_dynamic = client.post("/api/chat/intelligent", json={"message": "发个动态吐槽今天下大雨"})
    assert res_dynamic.status_code == 200
    data_dyn = res_dynamic.json()
    assert "card" in data_dyn
    assert data_dyn["card"]["type"] == "dynamic_draft"
    assert "大雨" in data_dyn["card"]["data"]["topic"]
    assert "动态" in data_dyn["reply_text"]
    assert any(a["id"] == "publish" for a in data_dyn["card"]["actions"])

    # 3. Video browsing card
    res_video = client.post("/api/chat/intelligent", json={"message": "去刷视频看看原神新版本"})
    assert res_video.status_code == 200
    data_vid = res_video.json()
    assert data_vid["card"]["type"] == "video_browsing"
    assert "原神" in data_vid["card"]["data"]["keyword"]
    assert data_vid["card"]["data"]["status"] == "ready"

    # 4. Live studio HUD
    res_live = client.post("/api/chat/intelligent", json={"message": "准备开播"})
    assert res_live.status_code == 200
    data_live = res_live.json()
    assert data_live["card"]["type"] == "live_studio_hud"
    assert data_live["card"]["data"]["live_state"] == "running"

    # 5. Direct companion chat
    res_chat = client.post("/api/chat/intelligent", json={"message": "今天工作好累啊"})
    assert res_chat.status_code == 200
    data_chat = res_chat.json()
    assert "饲养员" in data_chat["reply_text"]
    assert data_chat["card"]["type"] == "conversation"
