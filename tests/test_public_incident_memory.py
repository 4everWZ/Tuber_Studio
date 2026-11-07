import datetime
import json

import pytest

from tuber_studio.agent_tools import RecentAnecdotesTool
from tuber_studio.context import ContextManager
from tuber_studio.daily_memory import DailyMemoryService


def incident(service, character):
    return service.record_public_incident(
        character["id"],
        character["version"],
        incident_key="reply-mixup-20261107",
        highlight_date=datetime.date.today().isoformat(),
        headline="评论区接错了三回话",
        anecdote="我在三处评论没分清对话对象，接了不相干的话，观众当场问我在说什么。",
        public_sources=[
            {
                "bvid": "BV13S4i6fE82",
                "thread_id": "323202459488",
                "comment_id": "323231188736",
                "quote": "你消息是不是回错了",
            }
        ],
        tags=["接错话", "回错", "三回话"],
    )


def test_incident_deduplication_provenance_and_clear(store):
    service = DailyMemoryService(store)
    char = store.character()
    identifier = incident(service, char)
    assert incident(service, char) == identifier
    assert len(service.list_highlights(char["id"], char["version"])) == 1
    source = service.public_incident_sources(char["id"], char["version"])[0]
    assert source["fictional"] is False
    assert source["public_sources"][0]["url"].endswith("BV13S4i6fE82#reply323231188736")
    assert "你消息是不是回错了" == source["public_sources"][0]["quote"]
    service.update_highlight(identifier, {"status": "archived"})
    assert service.context(char, "回错") == []
    assert incident(service, char) == identifier  # Reimport never silently reactivates it.
    assert service.public_incident_sources(char["id"], char["version"]) == []
    assert service.delete_highlight(identifier)
    assert service.recall_historical_highlight(char["id"], char["version"], "还记得三回话吗") is None


def test_source_linked_recall_does_not_require_matching_words(store):
    service = DailyMemoryService(store)
    char = store.character()
    incident(service, char)
    match = service.context(char, "你在干啥", video_id="BV13S4i6fE82", thread_id="323202459488")
    assert len(match) == 1
    assert service.context(char, "你在干啥", video_id="BV13S4i6fE82", thread_id="100") == []
    assert service.context(char, "你在干啥", video_id="BV1RLH367EQN", thread_id="323202459488") == []
    assert service.context(char, "还记得接错话吗")
    assert service.context(char, "晚上吃啥") == []
    tool = RecentAnecdotesTool(service)
    scope = "comments:" + json.dumps(["bilibili", "BV13S4i6fE82", "323202459488"])
    assert (
        tool.execute({"text": "你在干啥"}, {"character": char, "scope": scope})[0]["type"]
        == "public_incident"
    )
    assert tool.can_handle({}, {"mode": "direct_message"})
    assert tool.execute({"text": ""}, {"character": char, "scope": "live:any"}) == []


def test_incident_cannot_be_created_without_public_provenance(store):
    service = DailyMemoryService(store)
    char = store.character()
    with pytest.raises(ValueError):
        service.record_highlight(
            char["id"], char["version"], "2026-11-07", "public_incident", "事", "原话", "事"
        )
    identifier = incident(service, char)
    with pytest.raises(ValueError):
        service.update_highlight(identifier, {"category": "community_meme"})
    with pytest.raises(ValueError):
        service.record_public_incident(
            char["id"],
            char["version"],
            incident_key="private",
            highlight_date="2026-11-07",
            headline="事",
            anecdote="事",
            public_sources=[{"url": "dm:private", "quote": "不应公开"}],
        )


@pytest.mark.asyncio
async def test_real_context_path_passes_floor_without_keyword(store, settings):
    service = DailyMemoryService(store)
    char = store.character()
    incident(service, char)

    class Provider:
        async def count_tokens(self, messages):
            return 100

    manager = ContextManager(settings, store, Provider(), daily_memory=service)
    event = {"event_id": "public:1", "text": "你在干啥", "name": "观众", "user": "1", "type": "chat"}
    scope = 'comments:["bilibili","BV13S4i6fE82","323202459488"]'
    messages = await manager.prepare(scope, char, event, mode="comment_draft")
    assert any('"type": "public_incident"' in message["content"] for message in messages)
