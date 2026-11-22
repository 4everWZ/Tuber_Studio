import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from test_personal_agent import CHAT, turn
from test_personal_agent import studio as studio

from tuber_studio.agent import operator_facts
from tuber_studio.agent.assistant_output import calendar_fields, clock_readout
from tuber_studio.core.store import dump
from tuber_studio.infra.inference.responses_provider import FunctionCall, ResponseTurn
from tuber_studio.operations.channels.comments.timeline import ensure_comment_timeline

ACCOUNT = "12345"
SOURCE = "BV1ab411c7mD"
OTHER = "BV1ab411c7mE"
PENDING = "BV1ab411c7mF"


@pytest.mark.parametrize("reply, expected", [
    ("贝壳灯都犯困啦。", "贝壳灯都犯困啦。\n\n现在是19:42。"),
    ("嗯，{{time}}到了。", "现在是19:42。"),
    ("星期日，19:42。", "现在是19:42。"),
    ("现在是 19:42。", "现在是 19:42。"),
])
def test_clock_readout_keeps_supported_values(reply, expected):
    clock = datetime(2026, 11, 21, 19, 42, tzinfo=ZoneInfo("Asia/Taipei"))
    assert clock_readout(reply, ["local_time"], clock) == expected


def test_calendar_projection_binds_the_requested_fields():
    assert calendar_fields("日期呀，年月日", []) == ("local_date",)
    assert calendar_fields("今儿过得还好吗？", ["local_date", "local_weekday"]) == ()
    assert calendar_fields("今天看了几部视频？", ["local_date"]) == ()
    assert calendar_fields("现在几点，请问星期几？", ["local_time"]) == ("local_time", "local_weekday")


def seed_workshop(runtime):
    browsing = runtime.browsing
    char = runtime.character
    now = operator_facts.now(browsing).timestamp()
    browsing.discovery.remember_account(ACCOUNT)
    store = runtime.store
    for account, bvid, citation in (
        (ACCOUNT, SOURCE, "今天的鲸鱼日记提到蓝色灯塔和小瓶子"),
        (ACCOUNT, OTHER, "台风天，记录老码头的浪花"),
        ("888", "BV1ab411c7mG", "另一账号的鲸鱼日记"),
    ):
        body = {"metadata": {"bvid": bvid, "title": "海边的日记", "owner": bvid[-1],
                             "parts": [{"cid": 100}, {"cid": 101}]},
                "parts": [{"cid": 101, "visual": True, "segments": [
                    {"start": 19, "end": 22, "text": citation},
                ]}]}
        store.execute(
            "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state)"
            " VALUES (?,?,?,?,'[]',?,'viewed')", (account, char["id"], bvid, dump(body), now),
        )
    round_id = str(uuid4())
    store.execute(
        "INSERT INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,config)"
        " VALUES (?,?,?,?,'completed',0,'{}')", (round_id, ACCOUNT, char["id"], char["version"]),
    )
    for index, (bvid, status, material) in enumerate([
        (SOURCE, "viewed_without_comment", "record-1"),
        (SOURCE, "selected", "record-2"),
        (OTHER, "selected", "record-3"),
        (PENDING, "running", None),
    ]):
        task_id = str(uuid4())
        store.execute(
            "INSERT INTO browsing_tasks(id,round_id,bvid,status,material_key,payload,updated_at) "
            "VALUES (?,?,?,?,?,'{}',?)", (task_id, round_id, bvid, status, material, now),
        )
        browsing.repo.quotas.start_watch(task_id, ACCOUNT, char["id"], bvid, 0, now=now + index * 0.001)
    ensure_comment_timeline(store)
    store.execute("INSERT INTO browsing_comment_timeline VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (ACCOUNT, char["id"], SOURCE, "12", "12", None, "51", "小灯", "录到了鲸鱼叫声", now, "52"))
    store.execute("INSERT INTO browsing_comment_observations VALUES(?,?,?,?,?,?,?,?)",
                  (ACCOUNT, char["id"], SOURCE, "12", "12", "test", round_id, now))


async def test_ledger_and_selected_video_use_same_read_and_original_coordinates(studio, monkeypatch):
    runtime, client = studio
    seed_workshop(runtime)
    calls = []
    plans = iter([
        {"route": "instant_read", "action": "watch_totals", "watch_day": "today"},
        {"route": "instant_read", "action": "workshop_lookup", "lookup": {"collection": "video", "day": "today", "terms": ["蓝色灯塔"]}},
        {"route": "instant_read", "action": "workshop_lookup", "object_ref": "obj1", "lookup": {"collection": "video"}},
        {"route": "chat", "action": "chat"},
    ])

    async def model(messages, schema, **kwargs):
        calls.append(schema["title"])
        if schema["title"] == "FrontDialogueReply":
            return {"reply_markdown": "想听你讲讲。"}
        assert "另一账号" not in json.dumps(messages, ensure_ascii=False)
        return next(plans)

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    count = await turn(client, "这天实际读了几部？", stream=True)
    values = count["tool_receipt"]["values"]
    assert (values["started"], values["completed"], values["in_progress"]) == (3, 2, 1)
    shared = await turn(client, "说说今天读到蓝色灯塔的那部")
    link = await turn(client, "把那段发个入口给我", stream=True)
    for result in (shared, link):
        entry, = result["sources"][0]["entries"]
        assert entry["href"] == f"https://www.bilibili.com/video/{SOURCE}?p=2&t=19"
        assert entry["kind"] == "visual"
        saved = runtime.store.one(
            "SELECT result FROM operator_chat_turns WHERE json_extract(result,'$.object_scope') IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert json.loads(saved["result"])["conversation_objects"][0]["identity"] == SOURCE
    chat = await turn(client, "最近我想学画画")
    assert chat["reply_text"] == "想听你讲讲。" and "tool_receipt" not in chat
    assert calls == ["TurnRoute", "TurnRoute", "TurnRoute", "TurnRoute", "FrontDialogueReply"]
    assert not runtime.tasks.activity()
    runtime.browsing.discovery.remember_account("888")
    old = (await client.get(CHAT)).raise_for_status().json()
    assert SOURCE not in dump(old)


async def test_backend_read_uses_the_same_indexes_and_source_scope(studio):
    runtime, _ = studio
    seed_workshop(runtime)
    tasks = runtime.tasks
    scope = tasks.scope()
    comments = await tasks.read("workshop_lookup", str(uuid4()), "今天的公开评论", scope,
                                {"collection": "comment", "day": "today", "terms": ["鲸鱼叫声"]})
    item, = comments["items"]
    assert item["identity"] == f"comment:{SOURCE}:12:12"
    assert item["entries"][0]["href"].endswith("#reply12")
    videos = await tasks.read("workshop_lookup", str(uuid4()), "找一条旧片段", scope,
                              {"collection": "video", "terms": ["鲸鱼日记"]})
    assert [item["identity"] for item in videos["items"]] == [SOURCE]


async def test_ambiguous_read_preserves_alternatives_until_selection(studio, monkeypatch):
    runtime, client = studio
    seed_workshop(runtime)
    plans = iter([
        {"route": "instant_read", "action": "workshop_lookup", "lookup": {"collection": "video", "terms": ["海边的日记"]}},
        {"route": "instant_read", "action": "workshop_lookup", "object_ref": "obj2", "lookup": {"collection": "video"}},
    ])

    async def model(messages, schema, **kwargs):
        assert schema["title"] == "TurnRoute"
        return next(plans)

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    ambiguous = await turn(client, "《海边的日记》是哪条？")
    candidates = [group["entries"][0]["href"] for group in ambiguous["sources"]]
    assert "你指哪一条" in ambiguous["reply_text"] and len(candidates) == 2
    selected = await turn(client, "列表第二条就好")
    assert [group["entries"][0]["href"] for group in selected["sources"]] == candidates[1:]


async def test_clear_retires_grounded_objects_and_keeps_task_history(studio, monkeypatch):
    runtime, client = studio
    seed_workshop(runtime)
    observed = False

    async def model(messages, schema, **kwargs):
        nonlocal observed
        assert schema["title"] == "TurnRoute"
        if observed:
            assert not any("obj1" in row["content"] or "蓝色灯塔" in row["content"] for row in messages)
            return {"route": "clarify", "action": "clarify", "question": "你想查哪条视频？"}
        return {"route": "instant_read", "action": "workshop_lookup", "lookup": {"collection": "video", "terms": ["蓝色灯塔"]}}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    assert (await turn(client, "那部有蓝色灯塔的视频给我看看"))["sources"]
    assert (await client.delete(CHAT)).raise_for_status().json()["ok"]
    observed = True
    assert (await turn(client, "它的链接呢"))["reply_text"] == "你想查哪条视频？"


@pytest.mark.parametrize("lookup_args, expected_source", [
    ({"collection": "video", "day": "today", "terms": ["蓝色灯塔"]}, True),
    ({"collection": "video", "day": "yesterday", "terms": ["蓝色灯塔"]}, False),
    ({"collection": "dynamic", "day": "today"}, False),
])
async def test_count_receipt_can_lead_to_scoped_read_in_same_sse_turn(studio, monkeypatch, lookup_args, expected_source):
    runtime, client = studio
    seed_workshop(runtime)

    async def model(messages, schema, **kwargs):
        assert schema["title"] == "TurnRoute"
        return {"route": "instant_read", "action": "workshop_lookup", "lookup": lookup_args}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    selected = await turn(client, "今天刷到的东西里挑一段给我聊聊", stream=True)
    receipt = selected["tool_receipt"]
    if expected_source:
        assert receipt["state"] == "ok"
        assert f"/video/{SOURCE}?p=2&t=19" in selected["sources"][0]["entries"][0]["href"]
    else:
        assert receipt["state"] == "empty"
        assert not selected.get("sources")
    assert not runtime.tasks.activity()


async def test_omni_chat_keeps_clock_readout_and_bound_sources_in_their_turn(studio, monkeypatch):
    runtime, client = studio
    clock = datetime(2026, 11, 21, 19, 42, tzinfo=ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(operator_facts, "now", lambda browsing: clock)
    seed_workshop(runtime)
    provider = runtime.llm
    monkeypatch.setattr(provider, "uses_model_card", True, raising=False)
    monkeypatch.setattr(provider, "config", {"max_tokens": 1024}, raising=False)
    monkeypatch.setattr(provider, "capacity_window", 16384, raising=False)
    monkeypatch.setattr(provider, "prepare_json_messages", lambda messages, *args, **kwargs: messages, raising=False)

    async def count(messages):
        return 128

    monkeypatch.setattr(provider, "count_tokens", count, raising=False)
    calls = []
    lookup_routes = {
        "你现在看过多少视频？": ("instant_read", {"action": "watch_totals", "watch_day": "today"}),
        "那些里面挑一条具体的聊聊": ("instant_read", {"action": "workshop_lookup", "lookup": {"collection": "video"}, "object_ref": "obj1"}),
        "刚才那段的链接呢？": ("instant_read", {"action": "workshop_lookup", "lookup": {"collection": "video"}, "object_ref": "obj1"}),
    }
    fields = {"小鱼，现在几点了呀": ["local_time"], "日期呢？": ["local_date"], "讲点你想讲的吧": []}
    natural = {
        "小鱼，现在几点了呀": "贝壳灯都犯困啦。",
        "日期呢？": "今天都到二十一号啦。",
        "讲点你想讲的吧": "今天想和你随便聊聊。",
    }

    async def respond(*, input, tools, **options):
        calls.append("TurnRoute")
        payload = json.loads(input[-1]["content"])
        utterance = payload["message"]
        if utterance in lookup_routes:
            name, args = lookup_routes[utterance]
        else:
            name, args = "chat", {"clock_fields": fields.get(utterance, [])}
        return ResponseTurn("router", (FunctionCall("route", name, args),), "", {})

    async def front_text(messages, *, max_tokens, on_content=None, on_stage=None, temperature=None):
        calls.append("FrontText")
        assert "可用能力" not in dump(messages)
        utterance = messages[-1]["content"]
        if any("这次工坊读取已经完成" in row["content"] for row in messages if row["role"] == "system"):
            return "我想聊这一条。"
        assert all("{{local_time}}" not in row["content"] for row in messages)
        if utterance == "讲点你想讲的吧":
            assert all("19:42" not in row["content"] for row in messages)
            assert all("贝壳灯都犯困啦" not in row["content"] for row in messages)
        text = natural[utterance]
        if on_content:
            on_content(text)
        return text

    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: respond)
    monkeypatch.setattr(provider, "generate_text_stream", front_text, raising=False)
    counted = await turn(client, "你现在看过多少视频？")
    assert counted["tool_receipt"]["values"]["completed"] == 2
    shared = await turn(client, "那些里面挑一条具体的聊聊", stream=True)
    link = await turn(client, "刚才那段的链接呢？")
    assert shared["sources"][0]["entries"][0]["href"] == link["sources"][0]["entries"][0]["href"]
    calls.clear()
    timed = await turn(client, "小鱼，现在几点了呀", stream=True)
    dated = await turn(client, "日期呢？")
    casual = await turn(client, "讲点你想讲的吧", stream=True)
    assert timed["reply_text"] == "贝壳灯都犯困啦。\n\n现在是19:42。"
    assert dated["reply_text"] == "今天都到二十一号啦。\n\n现在是2026年11月21日。"
    assert casual["reply_text"] == "今天想和你随便聊聊。"
    assert [result.get("tool_receipt") for result in (timed, dated, casual)] == [None, None, None]
    assert calls == ["TurnRoute", "FrontText"] * 3
    short_lived = await turn(client, "小鱼，现在几点了呀", temporary=True)
    assert short_lived["reply_text"] == timed["reply_text"] and short_lived["card"] is None
