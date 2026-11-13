import datetime

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.context import ContextManager
from tuber_studio.daily_memory import DailyMemoryService


class TokenCounter:
    async def count_tokens(self, messages):
        return 50


class DummyPlatform:
    async def close(self):
        pass


def test_operator_public_incident_api_is_deduplicated_and_clearable(settings):
    app = create_app(settings, DummyPlatform())
    payload = {
        "incident_key": "operator-confirmed-20261107",
        "highlight_date": "2026-11-07",
        "headline": "公开评论接错话",
        "anecdote": "我没认清对话对象，观众问我是不是回错了。",
        "public_sources": [
            {
                "bvid": "BV13S4i6fE82",
                "thread_id": "323202459488",
                "comment_id": "323231188736",
                "quote": "你消息是不是回错了",
            }
        ],
        "tags": ["接错话"],
    }
    with TestClient(app) as client:
        assert client.post("/api/daily-memory/incidents", json=payload).status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token})
        response = client.post("/api/daily-memory/incidents", json=payload)
        assert response.status_code == 200
        item = response.json()
        assert item["public_sources"][0]["thread_id"] == "323202459488"
        assert client.post("/api/daily-memory/incidents", json=payload).json()["id"] == item["id"]
        assert len(client.get("/api/daily-memory").json()["highlights"]) == 1
        assert client.put(f"/api/daily-memory/{item['id']}", json={"status": "archived"}).status_code == 200
        assert client.post("/api/daily-memory/incidents", json=payload).json()["status"] == "archived"
        assert client.delete(f"/api/daily-memory/{item['id']}").status_code == 200
        assert client.get("/api/daily-memory").json()["highlights"] == []


def test_daily_highlight_crud(store):
    service = DailyMemoryService(store)
    today = datetime.date.today().isoformat()

    # Create
    item_id = service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=today,
        category="comment_anecdote",
        headline="尾巴太胖的抗议",
        quote="尾巴好胖呀",
        anecdote="被弹幕说尾巴胖，本鲸鱼当场气鼓鼓！",
        source_ref="video:BV1:comment:1",
        source_user="某弹幕",
        tags=["尾巴", "日常"],
        status="pending_review",
    )
    assert item_id > 0

    # List
    items = service.list_highlights("whale", 1, date_str=today)
    assert len(items) == 1
    assert items[0]["id"] == item_id
    assert items[0]["status"] == "pending_review"
    assert items[0]["tags"] == ["尾巴", "日常"]

    # Update
    updated = service.update_highlight(item_id, {"status": "active", "headline": "尾巴抗议事件"})
    assert updated["status"] == "active"
    assert updated["headline"] == "尾巴抗议事件"

    # Active retrieval
    active = service.get_active_anecdotes("whale", 1, query_text="尾巴")
    assert len(active) == 1
    assert active[0]["topic"] == "尾巴抗议事件"
    assert active[0]["memory"] == "被弹幕说尾巴胖，本鲸鱼当场气鼓鼓！"

    # Delete
    deleted = service.delete_highlight(item_id)
    assert deleted is True
    assert service.list_highlights("whale", 1) == []


@pytest.mark.asyncio
async def test_context_manager_injects_daily_memory(settings, store):
    service = DailyMemoryService(store)
    today = datetime.date.today().isoformat()

    # Record an active anecdote
    service.record_highlight(
        operator_confirmed=True,
        character_id="whale",
        character_version=1,
        highlight_date=today,
        category="comment_anecdote",
        headline="热饭传说",
        quote="今天吃了吗",
        anecdote="昨晚连吃了三碗米饭，体力充沛！",
        status="active",
    )

    provider = TokenCounter()
    ctx_mgr = ContextManager(settings, store, provider, daily_memory=service)

    char = store.character()
    event = {
        "id": "evt1",
        "event_id": "evt1",
        "type": "chat",
        "user": "u1",
        "name": "小明",
        "text": "大肥鱼你昨晚吃了吗？",
    }

    # Live mode should include daily anecdotes
    messages = await ctx_mgr.prepare("scope:live:1", char, event, mode="live")
    assert any("近期生活与社群新鲜事" in m.get("content", "") for m in messages)

    # Anecdotes should not bloat non-interaction modes
    messages_other = await ctx_mgr.prepare("scope:live:2", char, event, mode="other")
    assert not any("近期生活与社群新鲜事" in m.get("content", "") for m in messages_other)


def test_daily_memory_api_endpoints(settings):
    app = create_app(settings, DummyPlatform())
    with TestClient(app) as client:
        # 1. Require admin auth
        assert client.get("/api/daily-memory").status_code == 401

        # Login
        login_res = client.post("/api/auth", json={"token": settings.admin_token})
        assert login_res.status_code == 200

        # 2. Get list (initially empty)
        res = client.get("/api/daily-memory")
        assert res.status_code == 200
        assert res.json()["highlights"] == []

        service = app.state.runtime.daily_memory
        today = datetime.date.today().isoformat()
        item_id = service.record_highlight(
            operator_confirmed=True,
            character_id="whale",
            character_version=1,
            highlight_date=today,
            category="community_meme",
            headline="测试梗",
            quote="测试原句",
            anecdote="这是测试自述",
        )

        # Update via PUT
        put_res = client.put(f"/api/daily-memory/{item_id}", json={"headline": "更正后的标题", "anecdote": "更正后的内容"})
        assert put_res.status_code == 200
        assert put_res.json()["headline"] == "更正后的标题"
        assert put_res.json()["quote"] == "测试原句"

        # Delete via DELETE
        del_res = client.delete(f"/api/daily-memory/{item_id}")
        assert del_res.status_code == 200
        assert del_res.json()["ok"] is True
