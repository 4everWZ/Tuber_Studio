import asyncio
import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_agent_task_recovery import wait_status

from tuber_studio.agent import operator_facts, task_tools
from tuber_studio.agent.operator_chat import ChatIntent, OperatorChat
from tuber_studio.agent.owner_memory import history_preference
from tuber_studio.agent.personal_agent import (
    PublicSearchTerms,
    object_scope,
    search_terms,
)
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


async def test_identity_reads_project_only_the_selected_account_and_fields(studio, monkeypatch):
    runtime, client = studio
    runtime.store.replace_character(runtime.character | {"bilibili_owner": {"uid": "123", "name": "海灯岸"}})
    account = {"state": "logged_in", "verification_stale": False, "profile": {"uid": "456", "name": "潮汐便笺"}}
    monkeypatch.setattr(runtime.account, "status", lambda: account)
    lookups = iter([
        {"subject": "bilibili_owner", "fields": ["name"]},
        {"subject": "operating_account", "fields": ["uid"]},
        {"subject": "operating_account", "fields": ["uid"]},
    ])

    async def model(messages, schema, **kwargs):
        assert schema["title"] == "WorkLifecycle"
        return {"action": "identity_read", "identities": [next(lookups)]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    owner = await turn(client, "绑给你的号主名字叫什么呀？")
    assert owner["tool_receipt"]["values"] == {"bilibili_owner": {"name": "海灯岸"}}
    assert "海灯岸" in owner["reply_text"] and "123" not in owner["reply_text"]
    operating = await turn(client, "我想要你自己运营账号的数字 UID。", stream=True)
    assert operating["tool_receipt"]["values"] == {"operating_account": {"uid": "456"}}
    assert "456" in operating["reply_text"] and "123" not in operating["reply_text"]
    account["state"] = "unverified"
    missing = await turn(client, "那个 UID 再说一下？")
    assert not missing["tool_receipt"]["values"] and "456" not in missing["reply_text"]
    assert not runtime.tasks.activity()


async def test_bound_person_resolves_a_new_projection_and_expires_on_rebind(studio, monkeypatch):
    runtime, client = studio
    card = {key: value for key, value in runtime.character.items() if not key.startswith("_")}
    card["bilibili_owner"] = {"uid": "123", "name": "号主甲"}
    runtime.store.replace_character(card)
    chat = []

    async def model(messages, schema, **kwargs):
        name = schema["title"]
        message = messages[-1]["content"]
        chat.append(name)
        actions = schema["properties"]["action"]["enum"]
        if name == "AssistantDecision":
            assert set(actions) == {"owner_binding", "owner_profile", "operating_account", "clarify"}
            return {"action": "owner_binding"}
        if message == "给我能点过去的地址和数字编号":
            return {"action": "direct_tool", "operation": {
                "mode": "read", "domain": "identity", "request": message, "target": "地址和数字编号",
            }}
        if message == "是谁养你的呀？":
            assert any("号主甲" in row["content"] for row in messages)
            return {"action": "chat", "reply_markdown": "我的主人是号主甲。"}
        assert all("obj1" not in row["content"] for row in messages)
        if message == "现在你的主人是谁呀？":
            assert any("号主乙" in row["content"] and "号主甲" not in row["content"]
                       for row in messages if row["role"] == "system")
            return {"action": "chat", "reply_markdown": "我的主人是号主乙。"}
        return {"action": "chat", "reply_markdown": "先确认你指的是哪位。"}

    async def network(*args):
        pytest.fail("已核验的 UID 查询访问了外网")

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    monkeypatch.setattr(runtime.account, "owner_profile", network)
    assert "号主甲" in (await turn(client, "是谁养你的呀？", stream=True))["reply_text"]
    result = await turn(client, "给我能点过去的地址和数字编号")
    assert result["card"]["data"] == {"name": "号主甲", "uid": "123", "url": "https://space.bilibili.com/123"}
    assert "号主甲" in result["reply_text"] and "123" in result["reply_text"]
    assert chat == ["WorkLifecycle", "WorkLifecycle", "AssistantDecision"]
    card["bilibili_owner"] = {"uid": "456", "name": "号主乙"}
    runtime.store.replace_character(card)
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


async def test_unavailable_research_keeps_its_readback_in_chat(studio, monkeypatch):
    runtime, client = studio

    async def generate(messages, schema, **kwargs):
        assert schema["title"] == "PublicSearchTerms"
        return {"terms": [], "localized": ""}

    async def unavailable(query, *, news):
        raise OSError("search unavailable")

    async def select_tool(*args, **kwargs):
        return ChatIntent(action="web_lookup"), None

    monkeypatch.setattr(OperatorChat, "_assistant", select_tool)
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", generate, raising=False)
    monkeypatch.setattr(task_tools, "search_public_web", unavailable)
    result = await turn(client, "联网查一下今儿有什么新闻", stream=True)
    assert result["tool_receipt"]["state"] == "unavailable"
    assert result["reply_text"] == "这次公开检索没有读到可用来源。"
    assert result["sources"] == []


async def test_direct_read_binds_a_current_workshop_object_when_model_paraphrases(studio, monkeypatch):
    runtime, client = studio
    calls = []

    async def model(messages, schema, **kwargs):
        calls.append(schema["title"])
        if schema["title"] == "WorkLifecycle":
            assert "owner_identity" not in schema["properties"]["action"]["enum"]
            return {"action": "direct_tool", "operation": {
                "mode": "read", "request": "查看当前账号信息", "target": "账号信息",
            }}
        assert "operating_account" in schema["properties"]["action"]["enum"]
        assert "owner_binding" not in schema["properties"]["action"]["enum"]
        assert "stop" not in schema["properties"]["action"]["enum"]
        assert any("请帮我查 Ｂ 站登录账号" in row["content"] for row in messages)
        return {"action": "operating_account", "read_target": None}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    result = await turn(client, "请帮我查 Ｂ 站登录账号", stream=True)
    assert result["tool_receipt"] == {"action": "read", "group": "account", "values": {"state": "not_configured"}}
    assert result["reply_text"] == "工坊还没连接 B 站运营账号。"
    assert calls == ["WorkLifecycle", "AssistantDecision"]


async def test_work_plan_uses_capabilities_and_current_request_instead_of_repeated_answers(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character
    principal = runtime.tasks.identity.principal
    old_reply = "同一句旧答复，已经在不同话题中出现过。"
    for index, old in enumerate(("上回查过的事", "另一个早先的话题")):
        saved = {"account_id": "", "private": True, "assistant_context": True, "owner_source": principal,
                 "private_owner": principal, "reply_text": old_reply}
        runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
            str(uuid4()), char["id"], char["version"], old, json.dumps(saved), 100 + index,
        ))
    calls = []

    async def model(messages, schema, *, on_content, **kwargs):
        actions = schema["properties"]["action"]["enum"]
        text = messages[-1]["content"]
        assert all(old_reply not in row["content"] for row in messages)
        if schema["title"] == "AssistantDecision":
            calls.append("read")
            assert set(actions) == {"game_status", "clarify"}
            return {"action": "game_status"}
        if "direct_tool" in actions:
            calls.append("omni")
            assert "reply_markdown" in schema["properties"]
            packet = "\n".join(row["content"] for row in messages if row["role"] == "system")
            assert all(word in packet for word in ("局面", "待发草稿", "公开网页", "角色"))
            if text == "田里还有什么活？":
                assert {"role": "assistant", "content": "聊会儿呀。"} in messages
                return {"action": "direct_tool", "operation": {
                    "mode": "read", "domain": "game", "request": text, "target": "田里",
                }}
            return {"action": "chat", "reply_markdown": "聊会儿呀。"}
        raise AssertionError("游戏状态直接取执行回执")

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    monkeypatch.setattr(runtime.live.game_livestream, "status",
                        lambda: {"active_status": {"summary": "还有 7 格没浇水。"}})
    assert (await turn(client, "陪我说会儿话", stream=True))["reply_text"] == "聊会儿呀。"
    assert (await turn(client, "田里还有什么活？"))["reply_text"] == "还有 7 格没浇水。"
    assert calls == ["omni", "omni", "read"]
    assert (await client.get(CHAT)).json()["turns"][0]["reply_text"] == old_reply


async def test_existing_outgoing_work_is_bound_to_account_and_shows_review_actions(studio, monkeypatch):
    runtime, client = studio
    char = runtime.character
    runtime.browsing.discovery.remember_account("123")
    for index, account, role, status in (
        (1, "123", char["id"], "approved"), (2, "123", char["id"], "pending_review"),
        (3, "567", char["id"], "approved"), (4, "123", "another-role", "approved"),
    ):
        runtime.store.execute(
            "INSERT INTO dynamic_posts(id,account_id,character_id,character_version,content,status,sources) "
            "VALUES (?,?,?,?,?,?,'[]')",
            (index, account, role, char["version"], f"草稿 {index}", status),
        )

    async def model(messages, schema, **kwargs):
        assert schema["title"] == "WorkLifecycle"
        return {"action": "direct_tool", "operation": {
            "mode": "change", "domain": "outgoing", "request": "检查待发内容然后确认发布", "target": "一组草稿",
        }}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    result = await turn(client, "把没发的帖子叫出来，我挑一条现在发", stream=True)
    assert result["card"]["type"] == "agent_task"
    task = runtime.tasks.current(result["card"]["data"]["id"])
    assert task["steps"][0]["arguments"] == {"post_ids": [1, 2]}
    assert not runtime.store.one("SELECT 1 FROM account_writes")
    runtime.browsing.discovery.remember_account("567")
    history = (await client.get(CHAT)).json()
    assert all(row["card"] is None for row in history["turns"])


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
