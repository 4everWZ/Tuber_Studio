import json
from uuid import uuid4

from test_personal_agent import CHAT, studio as studio, turn

from tuber_studio.agent import operator_facts
from tuber_studio.operations.browsing.comment_timeline import ensure_comment_timeline
from tuber_studio.store import dump


ACCOUNT = "12345"
SOURCE = "BV1ab411c7mD"
OTHER = "BV1ab411c7mE"
PENDING = "BV1ab411c7mF"


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
        {"action": "workshop_lookup", "lookup": {"collection": "watch_count", "day": "today"}},
        {"action": "workshop_lookup", "lookup": {"collection": "video", "day": "today", "terms": ["蓝色灯塔"]}},
        {"action": "workshop_lookup", "object_ref": "obj1", "lookup": {"collection": "video"}},
        {"action": "chat", "reply_markdown": "想听你讲讲。"},
    ])

    async def model(messages, schema, **kwargs):
        calls.append(schema["title"])
        if schema["title"] == "LookupPresentation":
            if json.loads(messages[-1]["content"])["统计"]:
                return {"reply": "", "evidence": []}
            assert "蓝色灯塔" in messages[-1]["content"]
            assert "另一账号" not in messages[-1]["content"]
            return {"reply": "我喜欢画面里的蓝色灯塔。", "evidence": ["m1"]}
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
    assert calls == ["WorkLifecycle", "LookupPresentation", "WorkLifecycle", "LookupPresentation",
                     "WorkLifecycle", "LookupPresentation", "WorkLifecycle"]
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
        {"action": "workshop_lookup", "lookup": {"collection": "video", "terms": ["海边的日记"]}},
        {"action": "workshop_lookup", "object_ref": "obj2", "lookup": {"collection": "video"}},
    ])

    async def model(messages, schema, **kwargs):
        if schema["title"] == "LookupPresentation":
            choices = json.loads(messages[-1]["content"])["查到的材料"]
            if len(choices) == 2:
                return {"view": "clarify", "reply": "你想看哪个 UP 主的《海边的日记》？", "evidence": []}
            return {"reply": "是这段。", "evidence": ["m1"]}
        return next(plans)

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    ambiguous = await turn(client, "《海边的日记》是哪条？")
    candidates = [group["entries"][0]["href"] for group in ambiguous["sources"]]
    assert "哪个 UP 主" in ambiguous["reply_text"] and len(candidates) == 2
    selected = await turn(client, "列表第二条就好")
    assert [group["entries"][0]["href"] for group in selected["sources"]] == candidates[1:]


async def test_clear_retires_grounded_objects_and_keeps_task_history(studio, monkeypatch):
    runtime, client = studio
    seed_workshop(runtime)
    observed = False

    async def model(messages, schema, **kwargs):
        nonlocal observed
        if schema["title"] == "LookupPresentation":
            return {"reply": "我选了这一条。", "evidence": ["m1"]}
        if observed:
            assert not any("obj1" in row["content"] or "蓝色灯塔" in row["content"] for row in messages)
            return {"action": "clarify", "topic": "你想查哪条视频？"}
        return {"action": "workshop_lookup", "lookup": {"collection": "video", "terms": ["蓝色灯塔"]}}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    assert (await turn(client, "那部有蓝色灯塔的视频给我看看"))["sources"]
    assert (await client.delete(CHAT)).raise_for_status().json()["ok"]
    observed = True
    assert (await turn(client, "它的链接呢"))["reply_text"] == "你想查哪条视频？"


async def test_count_receipt_can_lead_to_another_read_in_same_sse_turn(studio, monkeypatch):
    runtime, client = studio
    seed_workshop(runtime)

    async def model(messages, schema, **kwargs):
        if schema["title"] == "WorkLifecycle":
            return {"action": "workshop_lookup", "lookup": {"collection": "watch_count", "day": "today"}}
        source = json.loads(messages[-1]["content"])
        if source["统计"]:
            assert source["统计"]["completed"] == 2
            return {"reply": "", "evidence": [],
                    "next_lookup": {"collection": "video", "day": "today", "terms": ["蓝色灯塔"]}}
        return {"reply": "画面里的灯塔很有意思。", "evidence": ["m1"]}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    selected = await turn(client, "今天刷到的东西里挑一段给我聊聊", stream=True)
    assert selected["tool_receipt"]["calls"][0]["values"]["completed"] == 2
    assert f"/video/{SOURCE}?p=2&t=19" in selected["sources"][0]["entries"][0]["href"]
    assert not runtime.tasks.activity()
