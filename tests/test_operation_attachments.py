"""New autonomous drafts bind decisions to their source. Old reviews keep their original payload."""

import asyncio
import json
import time

import httpx
import pytest

from test_browsing import BV, browsing, finished
from test_comments import comments
from test_dynamics import Model
from test_message_memes import image_http, meme_messages
from tuber_studio import operation_attachments, operation_images


@pytest.fixture
def text_only(monkeypatch):
    async def choose(settings, material):
        return None

    monkeypatch.setattr(operation_attachments, "gateway_base", lambda _: "http://fixture/v1")
    monkeypatch.setattr(operation_images, "choose", choose)


async def settled(runtime, channel, row):
    ref = operation_attachments.row_ref(row)
    assert set(ref) == {"job_id"}
    scope = operation_attachments.image_scope(channel, row)
    async with asyncio.timeout(4):
        while runtime.operation_images.status(scope, ref["job_id"])["status"] != "text_only":
            await asyncio.sleep(0.01)
    assert operation_attachments.decision(runtime, channel, row, ref) is None
    job = runtime.store.one("SELECT material FROM operation_image_jobs WHERE job_id=?", (ref["job_id"],))
    material = json.loads(job["material"])
    assert (
        material["channel"] == scope["channel"]
        and material["character"]["version"] == row["character_version"]
    )
    return material


async def test_comment_generation_worker_exposes_only_text_for_replies(comments, store, text_only):
    comments.credentials = {"sessdata": "fixture", "csrf": "fixture"}
    comments.import_items(
        BV, [{"comment_id": "13", "thread_id": "11", "platform_user_id": "124", "content": "楼层原话"}]
    )
    comments.enqueue([store.one("SELECT id FROM comments")["id"]])
    await asyncio.wait_for(comments.queue.join(), 4)
    row = comments._draft_source(store.one("SELECT id FROM drafts")["id"])
    assert operation_attachments.row_ref(row) is None
    assert store.one("SELECT COUNT(*) as n FROM operation_image_jobs")["n"] == 0
    with pytest.raises(ValueError, match="仅视频一级评论"):
        comments.review(row["id"], "edit", image_ref={"asset_id": "unavailable", "sha256": "x"})
    assert operation_attachments.comment_payload(row, comments.runtime) == {"text": row["text"]}
    assert not comments.adapter.sent


async def test_autonomous_video_comment_and_video_dynamic_use_scoped_material(
    browsing, text_only, monkeypatch
):
    browsing.comments.credentials = {"sessdata": "fixture", "csrf": "fixture"}
    await finished(browsing, preview=True)
    main = next(intent for intent in browsing.intents() if intent["kind"] == "top_level")
    comment = await settled(browsing.runtime, "browsing", main)
    assert comment["context"]["bvid"] == BV and comment["context"]["evidence"]
    assert {s["id"] for s in comment["context"]["evidence"]} <= set(main["evidence"])
    await browsing.comments.account_id()
    model = Model()
    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", model.generate_json)
    monkeypatch.setattr(browsing.runtime.operation_llm, "count_tokens", model.count_tokens)
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    browsing.store.set("dynamic_policy", {"enabled": True, "auto_publish": False})
    assert browsing.dynamics.policy()["enabled"]
    result = await browsing.dynamics.evaluate(manual=False)
    assert result.get("post_id"), result
    post = browsing.dynamics._post(result["post_id"])
    dynamic = await settled(browsing.runtime, "dynamics", post)
    assert dynamic["context"][0]["type"] == "watched_video"
    assert dynamic["context"][0]["source_url"].endswith("/" + BV)
    assert "CID " in dynamic["context"][0]["text"] and dynamic["draft"] == post["content"]
    assert not browsing.adapter.sent


async def test_autonomous_inbox_poll_uses_only_its_own_message(meme_messages, text_only, monkeypatch):
    messages, _, _, state = meme_messages
    now = int(time.time()) + 1
    private = "私信验收只在这一段可见"

    def respond(request):
        assert request.method == "GET"
        if request.url.path.endswith("/nav"):
            data = {"mid": 900}
        elif request.url.path.endswith("/get_sessions"):
            data = {"session_list": [{"talker_id": 123, "session_type": 1}]}
        else:
            assert request.url.path.endswith("/fetch_session_msgs")
            data = {
                "messages": [
                    {
                        "sender_uid": 123,
                        "receiver_id": 900,
                        "receiver_type": 1,
                        "msg_type": 1,
                        "msg_key": 765,
                        "msg_seqno": 7,
                        "timestamp": now,
                        "content": json.dumps({"content": private}),
                    }
                ]
            }
        return httpx.Response(200, json={"code": 0, "data": data})

    client = httpx.AsyncClient(base_url="https://api.bilibili.com", transport=httpx.MockTransport(respond))
    async with client:
        monkeypatch.setattr(messages.adapter, "client", client)
        messages.operations.configure(
            {"enabled": True}, account_id="900", character=messages.runtime.character
        )
        await messages.configure({"enabled": True, "auto_poll": True, "auto_send": False})
        await messages.resume_pending()
        result = await messages.sync(automatic=True)
        assert result["imported"] == 1 and not result["errors"]
        await asyncio.wait_for(messages.queue.join(), 4)
    row = messages.store.one("SELECT * FROM dm_drafts")
    assert row and row["status"] == "pending_review", row
    job = await settled(messages.runtime, "messages", row)
    assert job["context"] == {
        "incoming": private,
        "recipient_uid": "123",
        "reply_incoming_ids": [row["source_id"]],
    }
    assert job["draft"] == row["text"]
    assert not state["requests"]
