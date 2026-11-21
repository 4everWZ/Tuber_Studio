import json
import time

from test_agent_task_recovery import wait_status
from test_owner_memory_entries import save
from test_personal_agent import CHAT, turn
from test_personal_agent import studio as studio
from test_workshop_lookup import ACCOUNT, SOURCE, seed_workshop

from tuber_studio.agent.recall_memory import NAME, RecallQuery, RecallSession, search
from tuber_studio.agent.task_delivery import PrivateDelivery, evidence_index
from tuber_studio.responses_provider import FunctionCall, ResponseTurn
from tuber_studio.store import dump

ORIGINAL = "录到了鲸鱼叫声"


def seed_history(runtime):
    seed_workshop(runtime)
    store = runtime.store
    char = runtime.character
    earlier = time.time() - 7 * 86400
    store.execute("UPDATE browsing_comment_observations SET observed_at=?", (earlier,))
    store.execute("UPDATE browsing_video_progress SET updated_at=?", (earlier,))
    for index in range(101, 128):
        key = str(index)
        store.execute("INSERT INTO browsing_comment_timeline VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (ACCOUNT, char["id"], SOURCE, key, key, None, "51", "观众", "今天的天气", time.time(), "52"))
        store.execute("INSERT INTO browsing_comment_observations VALUES(?,?,?,?,?,?,?,?)",
                      (ACCOUNT, char["id"], SOURCE, key, key, "test", "round", time.time()))
    store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,status,text,target,"
        "receipt,created_at,updated_at) VALUES(?,?,?,?,?,'reply_public','published',?,?,?,?,?)",
        ("reply-old", ACCOUNT, char["id"], char["version"], "old:12", "谢谢你分享鲸鱼的声音。",
         dump({"bvid": SOURCE, "root": "12", "parent": "12", "source_text": ORIGINAL}),
         dump({"rpid": "6789"}), earlier, earlier),
    )


async def test_chat_and_dynamic_use_same_older_sources_after_scoped_search(studio, monkeypatch):
    runtime, client = studio
    seed_history(runtime)
    dynamics = runtime.browsing.dynamics
    read_calls = []
    frontend = []
    expected = {f"video:{SOURCE}", f"comment:{SOURCE}:12:12", "reply:browsing:reply-old"}

    async def respond(*, input, tools, **options):
        read_calls.append(dump(input))
        outputs = [json.loads(item["output"]) for item in input if item.get("type") == "function_call_output"]
        if not outputs:
            objective = json.loads(input[0]["content"])
            if "本次话题" in objective:
                assert "近期" not in dump(objective)
            tool = FunctionCall("read", NAME, {"topic": "鲸鱼", "resource": SOURCE})
        elif len(outputs) == 1:
            assert {item["id"] for item in outputs[0]["items"]} == expected
            assert dump(outputs[0]).count(ORIGINAL) == 1
            tool = FunctionCall("again", NAME, {"source_id": f"comment:{SOURCE}:12:12"})
        else:
            assert outputs[1]["items"] == [] and outputs[1]["already_read"] == [f"comment:{SOURCE}:12:12"]
            tool = FunctionCall("select", "select_memory", {"source_ids": sorted(expected), "conclusion": "找到了当时的记录。"})
        return ResponseTurn("fixture", (tool,), "", {})

    async def omni(messages, schema, **kwargs):
        frontend.append(dump(messages))
        if schema["properties"]["action"]["enum"] == ["chat"]:
            assert frontend[-1].count(ORIGINAL) == 1
            return {"action": "chat", "reply_markdown": "当时确实有这条评论。"}
        if messages[-1]["content"] == "今天好困":
            return {"action": "chat", "reply_markdown": "歇一会吧。"}
        return {"action": NAME, "recall": {"topic": "鲸鱼"}}

    async def decide(messages, schema, **kwargs):
        packet = json.loads(messages[1]["content"])
        assert {item["id"] for item in packet["public_sources"]} == expected
        assert dump(packet).count(ORIGINAL) == 1
        return {"action": "wait", "content": "", "reason": "这次先等等。", "source_ids": sorted(expected),
                "next_evaluation_seconds": 1800}

    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: respond)
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", omni, raising=False)
    monkeypatch.setattr(runtime.operation_llm, "generate_json", decide)
    assert (await turn(client, "今天好困", stream=True))["reply_text"] == "歇一会吧。"
    assert not read_calls
    recalled = await turn(client, "回查鲸鱼声音相关的旧记录", stream=True)
    assert set(recalled["tool_receipt"]["values"]["source_ids"]) == expected
    assert any(entry["href"].endswith("?p=2&t=19") for group in recalled["sources"] for entry in group["entries"])
    assert (await dynamics.evaluate(manual=True, topic="鲸鱼"))["action"] == "wait"
    shown = (await client.get("/api/dynamics/materials")).raise_for_status().json()
    assert {item["id"] for item in shown["sources"]} == expected
    runtime.browsing.discovery.remember_account("65432")
    assert (await client.get("/api/dynamics/materials")).json()["sources"] == []
    assert ORIGINAL not in (await client.get(CHAT)).text


async def test_private_preference_only_resolves_in_owner_chat_and_revokes(studio, monkeypatch):
    runtime, client = studio
    seed_workshop(runtime)
    text = "写稿时我长期喜欢用薄荷青标题。"
    memory_id = await save(client, text=text)
    key = f"private-memory:{memory_id}"
    query = RecallQuery(source_id=key)
    dynamics = runtime.browsing.dynamics
    assert search(dynamics, query)["items"] == []
    assert search(dynamics, query, private=True)["items"][0]["text"] == text

    async def respond(*, input, **kwargs):
        output = next((item for item in input if item.get("type") == "function_call_output"), None)
        if output:
            assert text in output["output"]
            call = FunctionCall("select", "select_memory", {"source_ids": [key], "conclusion": text})
        else:
            call = FunctionCall("read", NAME, {"source_id": key})
        return ResponseTurn("fixture", (call,), "", {})

    async def omni(messages, schema, **kwargs):
        if schema["properties"]["action"]["enum"] == ["chat"]:
            return {"action": "chat", "reply_markdown": text}
        return {"action": NAME, "recall": {"source_id": key}}

    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: respond)
    monkeypatch.setattr(runtime.llm, "generate_assistant_json", omni, raising=False)
    result = await turn(client, "还记得我喜欢什么标题吗？")
    assert result["reply_text"] == text and result["tool_receipt"]["values"]["source_ids"] == [key]
    (await client.delete(f"/api/daily-memory/organized/{memory_id}")).raise_for_status()
    assert search(dynamics, query, private=True)["items"] == []
    assert text not in (await client.get(CHAT)).text


def test_background_recall_passes_each_read_as_evidence_once(studio):
    runtime, _ = studio
    seed_history(runtime)
    result = search(runtime.browsing.dynamics, RecallQuery(topic="鲸鱼"))
    previous = {"id": "foreground", "revision": 0, "tool": NAME,
                "result": result | {"items": [s for s in result["items"] if s["type"] == "watched_video"]}}
    current = {"id": "backend", "revision": 1, "tool": NAME, "call_id": "read", "response_id": "turn",
               "arguments": {"topic": "鲸鱼"},
               "result": result | {"items": [s for s in result["items"] if s["type"] != "watched_video"]}}
    duplicate = current | {"id": "cached", "call_id": "read-again", "response_id": "retry"}
    invalid = current | {"id": "invalid", "call_id": "read-invalid", "response_id": "invalid", "result": {"error": "缺少对象"}}
    packet = dump(evidence_index([previous, current, duplicate, invalid]))
    assert packet.count(ORIGINAL) == 1
    assert packet.count("?p=2&t=19") == packet.count("#reply6789") == 1


async def test_slow_recall_continues_with_parallel_reads_and_delivers_once(studio, monkeypatch):
    runtime, client = studio
    seed_history(runtime)

    async def route(*args, **kwargs):
        return {"action": NAME, "recall": {"topic": "鲸鱼"}}

    collect = RecallSession.collect

    async def deadline(session, *args, **kwargs):
        if session.sources:
            return await collect(session, *args, **kwargs)
        session.read(RecallQuery(source_id=f"video:{SOURCE}"))
        raise TimeoutError

    async def respond(*, input, **kwargs):
        conversation = json.loads(input[1]["content"])
        saved = conversation["已读来源"]
        assert len(saved) == 1
        if len(input) == 2:
            calls = (FunctionCall("old", NAME, {"topic": "鲸鱼"}),
                     FunctionCall("same", NAME, {"topic": "鲸鱼"}),
                     FunctionCall("other", NAME, {"topic": "无关"}))
        else:
            assert dump(input).count(ORIGINAL) == 1
            calls = (FunctionCall("deliver", "select_memory", {"source_ids": [saved[0]["id"]],
                "conclusion": "鲸鱼记录里的旧视频，出处在这里。"}),)
        return ResponseTurn("fixture", calls, "", {})

    async def edit(task, evidence):
        return PrivateDelivery.model_validate(task["plan"]["candidate"])

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", route, raising=False)
    monkeypatch.setattr(RecallSession, "collect", deadline)
    monkeypatch.setattr(runtime.tasks, "responses_client", lambda: respond)
    monkeypatch.setattr(runtime.tasks, "edit_delivery", edit)
    accepted = await turn(client, "回查鲸鱼声音相关的旧记录", stream=True)
    identity = accepted["card"]["data"]["id"]
    done = await wait_status(runtime.tasks, identity, "completed")
    assert "?p=2&t=19" in done["message"]
    matches = runtime.store.all("SELECT result FROM operator_chat_turns "
                                "WHERE json_extract(result,'$.task_delivery.task_id')=?", (identity,))
    assert len(matches) == 1
    transcript = (await client.get(CHAT)).json()
    delivered = next(turn for turn in transcript["turns"] if turn.get("task_delivery", {}).get("task_id") == identity)
    assert "鲸鱼记录" in delivered["reply_text"]
    assert any(entry["href"].endswith("?p=2&t=19") for source in delivered["sources"] for entry in source["entries"])
