import asyncio
import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_agent_task_recovery import wait_status

from tuber_studio.agent import operator_facts, task_tools
from tuber_studio.agent.owner_memory import history_preference
from tuber_studio.agent.personal_agent import PublicSearchTerms, object_scope, search_terms
from tuber_studio.agent.task_runtime import TaskDecision, TaskRuntime, Tool
from tuber_studio.app import create_app
from tuber_studio.models import StrictModel
from tuber_studio.store import Store
from tuber_studio.web_fetch import read_excerpt

CHAT = "/api/chat/intelligent"


@pytest.fixture
async def studio(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    ) as client:
        yield app.state.runtime, client


async def turn(client, message, *, stream=False, **fields):
    request = {"request_id": str(uuid4()), "message": message, **fields}
    response = (await client.post(CHAT + ("/stream" if stream else ""), json=request)).raise_for_status()
    if not stream:
        return response.json()
    return next(json.loads(part.split("\ndata: ")[1]) for part in response.text.split("\n\n")
                if part.startswith("event: done"))


async def test_bound_person_resolves_a_new_projection_and_expires_on_rebind(studio, monkeypatch):
    runtime, client = studio
    card = {key: value for key, value in runtime.character.items() if not key.startswith("_")}
    card["bilibili_owner"] = {"uid": "123", "name": "号主甲"}
    runtime.store.replace_character(card)
    read_from_binding, chat = True, []

    async def model(messages, schema, **kwargs):
        name = schema["title"]
        message = messages[-1]["content"]
        chat.append(name)
        actions = schema["properties"]["action"]["enum"]
        if name == "AssistantDecision":
            assert set(actions) == {"owner_binding", "owner_profile", "clarify"}
            return {"action": "owner_binding"}
        if "owner_identity" in actions:
            return {"action": "owner_identity"}
        if read_from_binding:
            return {"action": "direct_tool", "operation": {
                "mode": "read", "request": message, "target": None, "object_ref": "obj1", "followup": True,
            }}
        assert all("号主甲" not in row["content"] for row in messages)
        return {"action": "chat", "reply_markdown": "先确认你指的是哪位。"}

    async def network(*args):
        pytest.fail("已核验的 UID 查询访问了外网")

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    monkeypatch.setattr(runtime.account, "owner_profile", network)
    assert "号主甲" in (await turn(client, "是谁养你的呀？", stream=True))["reply_text"]
    result = await turn(client, "给我能点过去的地址和数字编号")
    assert result["card"]["data"] == {"name": "号主甲", "uid": "123", "url": "https://space.bilibili.com/123"}
    assert chat == ["WorkLifecycle", "AssistantDecision"]
    card["bilibili_owner"] = {"uid": "456", "name": "号主乙"}
    runtime.store.replace_character(card)
    read_from_binding = False
    assert not (await turn(client, "他还有其他地址吗？")).get("card")
    assert "号主乙" in (await turn(client, "现在你的主人是谁呀？"))["reply_text"]
    history_preference(runtime, enabled=False)
    assert not (await turn(client, "他还有其他地址吗？")).get("card")


async def test_public_news_cites_opened_sources_from_the_current_local_day(studio, monkeypatch):
    runtime, client = studio
    now = datetime(2026, 11, 20, 9, tzinfo=ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(operator_facts, "now", lambda browsing: now)
    calls = []

    async def search(query, *, news):
        assert news and "新闻" in query
        calls.append("search")
        return ["https://news.example/fresh", "https://news.example/stale", "https://news.example/no-date"]

    async def fetch(url):
        calls.append(url)
        published = {"fresh": "2026-11-19T21:00:00Z", "stale": "2026-11-19T13:00:00Z"}
        return {"title": url.rsplit("/", 1)[1], "url": url, "excerpt": "已打开的原文",
                "published_at": published.get(url.rsplit("/", 1)[1])}

    monkeypatch.setattr(task_tools, "search_public_web", search)
    monkeypatch.setattr(task_tools, "fetch_excerpt", fetch)
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", None, raising=False)
    result = await turn(client, "联网查一下今儿有什么新闻", stream=True)
    assert result["tool_receipt"]["local_date"] == "2026-11-20"
    assert [group["entries"][0]["href"] for group in result["sources"]] == ["https://news.example/fresh"]
    assert len(calls) == 4 and not runtime.tasks.activity()


async def test_publication_metadata_excludes_edit_time_and_reports_conflicts():
    url = httpx.URL("https://news.example/article")
    page = '<p>可阅读的文章正文</p><meta property="article:modified_time" content="2026-11-20">'
    meta = '<meta property="article:published_time" content="2026-11-19T20:30:00Z">'
    alternative = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-18"}</script>'
    for content, expected in [(page, None), (page + meta, "2026-11-19T20:30:00+00:00"),
                              (page + meta + alternative, None)]:
        reply = httpx.Response(200, text=content, headers={"Content-Type": "text/html"})
        assert (await read_excerpt(url, reply)).get("published_at") == expected


def test_search_uses_subjects_instead_of_the_question_and_call_to_action():
    request = "去搜一下今天有什么新闻"
    model = PublicSearchTerms(terms=["搜一下", "今天", "新闻", "另一个话题"], localized="新聞")
    assert search_terms(model, request) == (None, "")
    request = "帮我看看今天台湾的科技新闻"
    model = PublicSearchTerms(terms=["台湾", "科技", "新闻"], localized="台灣")
    assert search_terms(model, request) == ("台湾 科技", "台灣")


async def test_pause_at_checkpoint_survives_runtime_restart(tmp_path):
    path = tmp_path / "pause.sqlite3"
    identity = str(uuid4())
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def decide(goal, steps, names):
        return TaskDecision(action="tool", tool="operation_status") if not steps else TaskDecision(action="finish")

    async def read(request, args):
        calls.append(request.id)
        entered.set()
        await finish.wait()
        return {"state": "off"}

    def new_runtime():
        store = Store(path, [])
        runtime = TaskRuntime(store, lambda: ["fixture"], decide)
        runtime.register("operation_status", Tool("只读状态", StrictModel, read))
        return runtime

    runtime = new_runtime()
    runtime.create(identity, "读取一次状态")
    runtime.start()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending = await runtime.control(identity, 1, "pause")
        assert runtime.view(pending)["pause_requested"]
        finish.set()
        paused = await wait_status(runtime, identity, "paused")
        assert paused["steps"][0]["result"] == {"state": "off"}
    finally:
        await runtime.close()
        runtime.store.close()
    runtime = new_runtime()
    runtime.start()
    try:
        assert runtime.current(identity)["status"] == "paused"
        await runtime.control(identity, 1, "resume")
        complete = await wait_status(runtime, identity, "completed")
        assert complete["revision"] == 1 and len(calls) == 1
    finally:
        await runtime.close()
        runtime.store.close()


async def test_call_text_uses_same_task_receipt_and_scope(studio):
    runtime, client = studio
    scope = object_scope(runtime)
    result = await turn(client, "整理两行私有工作笔记", call_scope=scope, intent={"action": "agent_task"})
    identity = result["card"]["data"]["id"]
    assert (await client.get(f"/api/agent/tasks/{identity}")).raise_for_status().json()["id"] == identity
    response = await client.post(CHAT, json={
        "request_id": str(uuid4()), "message": "创建任务", "call_scope": "0" * 64, "intent": {"action": "agent_task"},
    })
    assert response.status_code == 409
    assert [task["id"] for task in runtime.tasks.activity()] == [identity]
