import datetime
import json

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.context import ContextManager
from tuber_studio.daily_memory import DailyMemoryService, desensitize_user


class DummyProvider:
    def __init__(self, reply_data=None):
        self.reply_data = reply_data or {
            "highlights": [
                {
                    "category": "comment_anecdote",
                    "headline": "被吐槽游得太慢",
                    "source_ref": "video:BVtest:comment:101",
                    "source_user": "热心潜水员",
                    "quote": "大肥鱼游得比乌龟还慢",
                    "anecdote": "昨天居然有人说我游得比海龟还慢！气得我昨晚吨吨吨多炫了一碗热白米饭！",
                    "tags": ["游速", "炫饭"],
                }
            ]
        }
        self.counted_tokens = 50

    async def generate_json(self, messages, schema, *, max_tokens=1024):
        return self.reply_data

    async def count_tokens(self, messages):
        return self.counted_tokens

    async def summarize(self, messages, *, max_tokens=1024):
        return json.dumps(self.reply_data, ensure_ascii=False)


class DummyPlatform:
    async def close(self):
        pass


def test_desensitize_user():
    assert desensitize_user(None) == "某位热心观众"
    assert desensitize_user("") == "某位热心观众"
    assert desensitize_user("   ") == "某位热心观众"
    assert desensitize_user("user_1234567") == "观众"
    assert desensitize_user("u987654321") == "观众"
    assert desensitize_user("潜水小鲸鱼") == "潜水小鲸鱼"
    long_name = "超长名字" * 10
    masked = desensitize_user(long_name)
    assert len(masked) <= 20
    assert masked.endswith("…")


def test_candidate_sources_and_desensitization(store):
    service = DailyMemoryService(store)
    today = datetime.date.today().isoformat()

    # Seed comments
    store.execute(
        """
        INSERT INTO comments(video_id, comment_id, display_name, content, like_count, category, imported_at)
        VALUES('BVtest1', 'c1', 'user_998877', '大肥鱼你的尾巴好胖呀！', 15, 'interaction', ?)
        """,
        (1730000000,),
    )
    # Update created_at so LIKE query matches today
    store.execute("UPDATE comments SET created_at = ? WHERE comment_id = 'c1'", (f"{today} 12:00:00",))

    # Seed live conversation
    store.execute(
        """
        INSERT INTO conversations(session_id, character_id, event_text, reply_text, created_at)
        VALUES('live_sess_1', 'whale', '今天吃什么？', '吃热白饭配秋刀鱼！', strftime('%s', 'now'))
        """
    )

    candidates = service.candidate_sources(today, "whale", limit=10)
    assert len(candidates) >= 1
    comment_cand = next(c for c in candidates if c["source_type"] == "comment")
    assert comment_cand["source_user"] == "观众"  # desensitized from user_998877
    assert "尾巴好胖" in comment_cand["content"]


def test_daily_highlight_crud(store):
    service = DailyMemoryService(store)
    today = datetime.date.today().isoformat()

    # Create
    item_id = service.record_highlight(
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
async def test_distillation_with_grounded_validation(store):
    service = DailyMemoryService(store)
    provider = DummyProvider()
    today = datetime.date.today().isoformat()

    candidates = [
        {
            "source_type": "comment",
            "source_ref": "video:BVtest:comment:101",
            "source_user": "热心潜水员",
            "content": "大肥鱼游得比乌龟还慢，快加速！",
            "like_count": 20,
            "replied": False,
        }
    ]

    char = {"id": "whale", "version": 1, "name": "大肥鱼"}
    extracted = await service.distill(char, today, provider, candidates=candidates, auto_approve=False)
    assert len(extracted) == 1
    item = extracted[0]
    assert item["status"] == "pending_review"
    assert item["headline"] == "被吐槽游得太慢"
    assert "热白米饭" in item["anecdote"]


@pytest.mark.asyncio
async def test_context_manager_injects_daily_memory(settings, store):
    service = DailyMemoryService(store)
    today = datetime.date.today().isoformat()

    # Record an active anecdote
    service.record_highlight(
        character_id="whale",
        character_version=1,
        highlight_date=today,
        category="comment_anecdote",
        headline="热饭传说",
        quote="今天吃了吗",
        anecdote="昨晚连吃了三碗米饭，体力充沛！",
        status="active",
    )

    provider = DummyProvider()
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

        # 3. Extract via API
        ext_res = client.post("/api/daily-memory/extract", json={"auto_approve": True})
        assert ext_res.status_code == 200
        assert "extracted" in ext_res.json()

        # 4. Insert manually via store to test PUT and DELETE
        _store = app.state.runtime.store
        service = app.state.runtime.daily_memory
        today = datetime.date.today().isoformat()
        item_id = service.record_highlight(
            character_id="whale",
            character_version=1,
            highlight_date=today,
            category="community_meme",
            headline="测试梗",
            quote="测试原句",
            anecdote="这是测试自述",
            status="pending_review",
        )

        # 5. Update via PUT
        put_res = client.put(f"/api/daily-memory/{item_id}", json={"status": "active"})
        assert put_res.status_code == 200
        assert put_res.json()["status"] == "active"

        # 6. Delete via DELETE
        del_res = client.delete(f"/api/daily-memory/{item_id}")
        assert del_res.status_code == 200
        assert del_res.json()["ok"] is True
