import hashlib
import json
import time
from uuid import uuid4

import httpx
import pytest
from PIL import ImageFont

from test_agent_task_recovery import wait_status
from test_personal_agent import CHAT, studio as studio, turn
from tuber_studio import dynamic_comment_card
from tuber_studio.agent.dynamic_publication import PUBLICATION_TOOL
from tuber_studio.operation_identity import runtime_identity


REQUEST = "小鱼，把之前积压的站动态都发出去"
PROGRESS = "你发完了吗"
SMALLTALK = "今天真累"
ACCOUNT = "12345"
TASKS = "/api/agent/tasks/"


async def configure_publication(runtime, monkeypatch, *, disconnect=False, mixed=True):
    dynamics = runtime.browsing.dynamics
    comments = dynamics.comments
    runtime.browsing.discovery.remember_account(ACCOUNT)
    credentials = {"sessdata": "isolated", "csrf": "isolated"}
    monkeypatch.setattr(comments, "credentials", credentials)
    monkeypatch.setattr(comments, "verified_credential", credentials)
    monkeypatch.setattr(comments, "verified_account_id", ACCOUNT)
    monkeypatch.setattr(comments.adapter, "_sessdata", "isolated")
    monkeypatch.setattr(comments.adapter, "_csrf", "isolated")
    runtime.store.set("dynamic_policy", {"max_posts_per_day": 0, "min_post_interval_seconds": 0})
    writes, remote, reads = [], {}, []
    state = {"visible": not disconnect, "logged_in": True}

    async def account():
        from tuber_studio.platforms.bilibili import BilibiliAccountExpired

        if not state["logged_in"]:
            raise BilibiliAccountExpired("登录已失效")
        return ACCOUNT

    async def params(values):
        return values

    async def publish(request):
        assert request.method == "POST" and request.url.path == "/x/dynamic/feed/create/dyn"
        text = json.loads(request.content)["dyn_req"]["content"]["contents"][0]["raw_text"]
        writes.append(text)
        if mixed and text == "标题 C":
            return httpx.Response(200, json={"code": -400, "message": "发布参数错误"})
        number = str(990 + len(writes))
        remote[text] = number
        if disconnect:
            raise httpx.ReadTimeout("response lost after insert", request=request)
        return httpx.Response(200, json={"code": 0, "data": {"dyn_id_str": number}})

    async def read(target, *, author_uid, text, started_at, receipt):
        assert author_uid == ACCOUNT
        reads.append(text)
        if state["visible"] and text in remote:
            number = remote[text]
            return {"status": "visible", "receipt": {"dynamic_id": number, "url": f"https://t.bilibili.com/{number}"}}
        return {"status": "ambiguous"}

    await comments.adapter.client.aclose()
    monkeypatch.setattr(comments.adapter, "client", httpx.AsyncClient(transport=httpx.MockTransport(publish)))
    monkeypatch.setattr(comments, "account_id", account)
    monkeypatch.setattr(comments.adapter, "wbi_params", params)
    monkeypatch.setattr(comments.adapter, "reconcile_dynamic_publication", read)

    async def model(messages, schema, **kwargs):
        if schema["title"] == "FrontDialogueReply":
            return {"reply_markdown": "辛苦啦，要不要先歇一小会儿？"}
        assert schema["title"] == "TurnRoute"
        payload = json.loads(messages[-1]["content"])
        message = payload["message"]
        if message == SMALLTALK:
            return {"route": "chat", "action": "chat"}
        if message == PROGRESS:
            return {"route": "instant_read", "action": "task_status"}
        if message == REQUEST:
            return {"route": "prepare_publication", "action": "prepare_publication", "publication_channel": "dynamics"}
        return {"route": "instant_read", "action": "outgoing_queue", "publication_channel": "dynamics"}

    monkeypatch.setattr(runtime.llm, "generate_assistant_json", model, raising=False)
    char = runtime.character
    ids = []
    for title in (["标题 A"] if disconnect else ["标题 A", "标题 B", "标题 C"]):
        decision = dynamics._save_decision(char, False, {
            "action": "post", "content": title, "reason": "排队草稿", "source_ids": [], "next_evaluation_seconds": 30,
        }, [], schedule=False, account_id=ACCOUNT, decide_image=False,
            generation_identity=runtime_identity(runtime, char, ACCOUNT))
        ids.append(decision["post_id"])
    return ids, writes, remote, reads, state


async def history(client):
    return (await client.get(CHAT)).raise_for_status().json()["turns"]


async def confirmation(runtime, client, origin):
    identity = origin["card"]["data"]["id"]
    task = await wait_status(runtime.tasks, identity, "awaiting_approval")
    approval, = [row for row in await history(client)
                 if row.get("task_delivery", {}).get("status") == "awaiting_approval"]
    assert approval["card"]["data"]["id"] == task["id"]
    return TASKS + identity, approval["card"]["data"]


async def test_completed_publication_returns_once_with_real_item_outcomes(studio, monkeypatch):
    runtime, client = studio
    ids, writes, remote, _, _ = await configure_publication(runtime, monkeypatch)
    request = {"request_id": str(uuid4()), "message": REQUEST}
    origin = (await client.post(CHAT, json=request)).raise_for_status().json()
    path, data = await confirmation(runtime, client, origin)
    assert data["approval"]["account_id"] == ACCOUNT
    assert [item["post_id"] for item in data["approval"]["posts"]] == ids
    assert "还没发" in (await turn(client, PROGRESS))["reply_text"]
    assert "歇" in (await turn(client, SMALLTALK))["reply_text"]
    assert not writes and len(runtime.tasks.activity()) == 1
    assert (await client.post(CHAT, json=request)).raise_for_status().json()["card"]["data"]["id"] == data["id"]
    confirm = {"revision": data["revision"], "step_id": data["step_id"], "action": "approve", "selected_ids": ids}
    (await client.patch(path, json=confirm)).raise_for_status()
    await wait_status(runtime.tasks, data["id"], "completed")
    assert (await client.patch(path, json=confirm)).status_code == 409
    assert writes == ["标题 A", "标题 B", "标题 C"] and len(remote) == 2
    results = []
    for _ in range(2):
        result, = [row for row in await history(client) if row.get("task_delivery", {}).get("status") == "completed"]
        assert result["reply_text"] == "已发 2 条，没发 1 条。"
        assert [(item["status"], item.get("number"), item.get("href")) for item in result["card"]["data"]["items"]] == [
            ("published", "991", "https://t.bilibili.com/991"),
            ("published", "992", "https://t.bilibili.com/992"),
            ("not_sent", None, None),
        ]
        results.append(result["request_id"])
    assert results[0] == results[1]


async def test_lost_reply_reconciles_original_post_and_smalltalk_does_not_repeat_it(studio, monkeypatch):
    runtime, client = studio
    ids, writes, remote, reads, state = await configure_publication(runtime, monkeypatch, disconnect=True)
    path, data = await confirmation(runtime, client, await turn(client, REQUEST))
    (await client.patch(path, json={"revision": data["revision"], "step_id": data["step_id"],
                                   "action": "approve", "selected_ids": ids})).raise_for_status()
    task = await wait_status(runtime.tasks, data["id"], "waiting")
    assert task["steps"][0]["checkpoint"]["items"][str(ids[0])]["status"] == "unconfirmed"
    assert "待核对 1 条" in (await turn(client, PROGRESS))["reply_text"]
    assert "歇" in (await turn(client, SMALLTALK))["reply_text"]
    state["visible"] = True
    runtime.tasks._save(runtime.tasks.current(data["id"]), due=0)
    runtime.tasks.wake.set()
    await wait_status(runtime.tasks, data["id"], "completed")
    assert writes == ["标题 A"] and remote == {"标题 A": "991"}
    assert reads and set(reads) == {"标题 A"} and len(runtime.tasks.activity()) == 1
    result, = [row for row in await history(client) if row.get("task_delivery", {}).get("status") == "completed"]
    assert result["card"]["data"]["items"][0]["href"] == "https://t.bilibili.com/991"


async def test_only_changed_content_needs_a_new_confirmation(studio, monkeypatch):
    runtime, client = studio
    ids, writes, _, _, _ = await configure_publication(runtime, monkeypatch)
    path, original = await confirmation(runtime, client, await turn(client, REQUEST))
    runtime.browsing.dynamics.review(ids[1], "edit", "标题 B 修改后")
    payload = {"revision": original["revision"], "step_id": original["step_id"], "action": "approve",
               "selected_ids": ids[:2]}
    saved = (await client.patch(path, json=payload)).raise_for_status().json()
    assert saved["status"] == "awaiting_approval" and not writes
    updated, = [row["card"]["data"] for row in await history(client)
                if row.get("task_delivery", {}).get("status") == "awaiting_approval" and not row.get("hidden")]
    assert updated["step_id"] != original["step_id"]
    assert updated["approval"]["carried_ids"] == ids[:1]
    assert [item["text"] for item in updated["approval"]["posts"]] == ["标题 A", "标题 B 修改后"]
    assert (await client.patch(path, json=payload)).status_code == 409
    (await client.patch(path, json=payload | {"step_id": updated["step_id"]})).raise_for_status()
    await wait_status(runtime.tasks, updated["id"], "completed")
    assert writes == ["标题 A", "标题 B 修改后"]


async def test_approval_serves_only_the_bound_attachment_bytes(studio, monkeypatch):
    runtime, client = studio
    ids, writes, _, _, _ = await configure_publication(runtime, monkeypatch)
    monkeypatch.setattr(dynamic_comment_card, "_font", lambda size: ImageFont.load_default())
    card = {"legacy": True, "comments": [
        {"quote": "Original fixture comment", "url": "https://t.bilibili.com/100"},
    ]}
    runtime.store.execute("UPDATE dynamic_posts SET comment_card_snapshot=? WHERE id=?", (json.dumps(card), ids[0]))
    path, data = await confirmation(runtime, client, await turn(client, REQUEST))
    endpoint = f"{path}/approval/{data['step_id']}/dynamics/{ids[0]}/images/0"
    image = (await client.get(endpoint)).raise_for_status()
    assert image.headers["content-type"] == "image/png"
    assert hashlib.sha256(image.content).hexdigest() == data["approval"]["posts"][0]["image_hashes"][0]
    card["comments"][0]["quote"] = "Changed fixture attachment"
    runtime.store.execute("UPDATE dynamic_posts SET comment_card_snapshot=? WHERE id=?", (json.dumps(card), ids[0]))
    assert (await client.get(endpoint)).status_code == 404 and not writes


async def test_publish_interval_keeps_the_rest_of_the_confirmed_batch_pending(studio, monkeypatch):
    runtime, client = studio
    ids, writes, _, _, _ = await configure_publication(runtime, monkeypatch)
    runtime.store.set("dynamic_policy", {"max_posts_per_day": 0, "min_post_interval_seconds": 60})
    path, data = await confirmation(runtime, client, await turn(client, REQUEST))
    (await client.patch(path, json={"revision": data["revision"], "step_id": data["step_id"], "action": "approve",
                                   "selected_ids": ids[:2]})).raise_for_status()
    task = await wait_status(runtime.tasks, data["id"], "waiting")
    assert "额度恢复后继续" in task["message"] and task["due"] > time.time() + 30
    assert writes == ["标题 A"] and len(runtime.tasks.activity()) == 1
    runtime.store.execute("UPDATE dynamic_posts SET attempted_at=? WHERE id=?", (time.time() - 90, ids[0]))
    runtime.tasks._save(task, due=0)
    runtime.tasks.wake.set()
    await wait_status(runtime.tasks, data["id"], "completed")
    assert writes == ["标题 A", "标题 B"]


@pytest.mark.parametrize("case", ["disconnected", "cancel", "expired", "wrong_selection", "login"])
async def test_publication_waits_for_its_exact_grant(studio, monkeypatch, case):
    runtime, client = studio
    ids, writes, _, _, state = await configure_publication(runtime, monkeypatch)
    if case == "disconnected":
        monkeypatch.delitem(runtime.tasks.tools, PUBLICATION_TOOL)
        assert "这次没发" in (await turn(client, REQUEST))["reply_text"]
        assert "没查到" in (await turn(client, PROGRESS))["reply_text"]
        assert not runtime.tasks.activity() and not writes
        return
    path, data = await confirmation(runtime, client, await turn(client, REQUEST))
    change = {"revision": data["revision"], "step_id": data["step_id"],
              "action": "approve", "selected_ids": ids[:1]}
    if case == "cancel":
        (await client.patch(path, json={"revision": data["revision"], "action": "cancel"})).raise_for_status()
    elif case == "expired":
        task = runtime.tasks.current(data["id"])
        step, = task["steps"]
        step["approval"]["expires_at"] = time.time() - 1
        assert runtime.tasks._save(task, steps=[step])
    elif case == "wrong_selection":
        change["selected_ids"] = [999]
    elif case == "login":
        state["logged_in"] = False
        (await client.patch(path, json=change)).raise_for_status()
        await wait_status(runtime.tasks, data["id"], "waiting")
        event, = [row for row in await history(client) if row.get("task_delivery", {}).get("status") == "waiting"]
        assert event["card"]["data"]["interaction"]["href"] == "#accounts" and not writes
        state["logged_in"] = True
        runtime.tasks._save(runtime.tasks.current(data["id"]), due=0)
        runtime.tasks.wake.set()
        await wait_status(runtime.tasks, data["id"], "completed")
        assert writes == ["标题 A"]
        return
    assert (await client.patch(path, json=change)).status_code == 409
    assert not writes
