import json
import time

from test_owner_memory_entries import save
from test_personal_agent import CHAT, turn
from test_personal_agent import studio as studio
from test_workshop_lookup import ACCOUNT, SOURCE, seed_workshop

from tuber_studio.agent.recall_memory import NAME, RecallQuery, search
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
