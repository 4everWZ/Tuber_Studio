import json
import time
from types import SimpleNamespace

import pytest

from tuber_studio.direct_messages import DirectMessages
from tuber_studio.store import Store


def test_operator_manual_reply_detection_and_cancellation(store):
    character = store.character()
    runtime = SimpleNamespace(
        character={"id": character["id"], "version": character["version"]},
        context=SimpleNamespace(),
    )
    comments = SimpleNamespace(
        operations=SimpleNamespace(policy=lambda: {"enabled": True, "epoch": 1, "enabled_at": 100})
    )
    dm = DirectMessages(
        settings=SimpleNamespace(data={"llm": {"model": "test"}}),
        store=store,
        runtime=runtime,
        comments=comments,
    )
    own_uid = "99999"
    user_uid = "12345"

    # 1. User sends a message
    user_msg_key = "1001"
    rows = [
        {
            "receiver_type": 1,
            "msg_type": 1,
            "msg_status": 0,
            "msg_key": user_msg_key,
            "sender_uid": user_uid,
            "receiver_id": own_uid,
            "content": json.dumps({"content": "大肥鱼，你今天吃了吗？"}),
            "timestamp": 1000,
        }
    ]
    dm._ingest_history(rows, user_uid, own_uid, "测试粉丝")

    inbox = store.one("SELECT * FROM dm_inbox WHERE message_id=?", (user_msg_key,))
    assert inbox is not None
    assert inbox["content"] == "大肥鱼，你今天吃了吗？"

    # Create a pending draft for this incoming message
    draft_id = store.execute(
        "INSERT INTO dm_drafts(platform,platform_user_id,display_name,source_kind,source_id,character_id,character_version,status,created_at,updated_at,account_id) "
        "VALUES('bilibili', ?, '测试粉丝', 'message', ?, ?, ?, 'queued', 1005, 1005, ?)",
        (user_uid, inbox["id"], character["id"], character["version"], own_uid),
    ).lastrowid

    # 2. Operator manually replies on Bilibili app (sender = own_uid, receiver = user_uid)
    operator_msg_key = "2001"
    history_with_operator = rows + [
        {
            "receiver_type": 1,
            "msg_type": 1,
            "msg_status": 0,
            "msg_key": operator_msg_key,
            "sender_uid": own_uid,
            "receiver_id": user_uid,
            "content": json.dumps({"content": "她吃了小鱼干，不用担心~"}),
            "timestamp": 1010,
        }
    ]
    dm._ingest_history(history_with_operator, user_uid, own_uid, "测试粉丝")

    # Verify operator reply was recorded in dm_operator_replies
    op_record = store.one("SELECT * FROM dm_operator_replies WHERE message_id=?", (operator_msg_key,))
    assert op_record is not None
    assert op_record["content"] == "她吃了小鱼干，不用担心~"

    # Verify pending draft was cancelled because operator already replied
    draft = store.one("SELECT * FROM dm_drafts WHERE id=?", (draft_id,))
    assert draft["status"] == "cancelled"
    assert "号主已在平台手动回复" in draft["error"]


def test_copilot_context_included_in_event(store):
    character = store.character()
    runtime = SimpleNamespace(
        character={"id": character["id"], "version": character["version"]},
        context=SimpleNamespace(),
    )
    comments = SimpleNamespace(
        operations=SimpleNamespace(policy=lambda: {"enabled": True, "epoch": 1, "enabled_at": 100})
    )
    dm = DirectMessages(
        settings=SimpleNamespace(data={"llm": {"model": "test"}}),
        store=store,
        runtime=runtime,
        comments=comments,
    )
    own_uid = "99999"
    user_uid = "12345"

    # User message 1
    dm.ingest({
        "message_id": "1001",
        "platform_user_id": user_uid,
        "display_name": "测试粉丝",
        "text": "大肥鱼，你今天吃了吗？",
        "timestamp": 1000,
        "account_id": own_uid,
    })

    # Operator reply recorded
    store.execute(
        "INSERT INTO dm_operator_replies(platform,account_id,platform_user_id,message_id,content,sent_at,character_id) "
        "VALUES('bilibili', ?, ?, '2001', '吃了小鱼干啦', 1010, ?)",
        (own_uid, user_uid, character["id"]),
    )

    # User follow-up message 2
    inbox_id_2, _ = dm.ingest({
        "message_id": "1002",
        "platform_user_id": user_uid,
        "display_name": "测试粉丝",
        "text": "那下午几点开播呢？",
        "timestamp": 1020,
        "account_id": own_uid,
    })

    # Create draft for message 2
    draft_id = store.execute(
        "INSERT INTO dm_drafts(platform,platform_user_id,display_name,source_kind,source_id,character_id,character_version,status,created_at,updated_at,account_id) "
        "VALUES('bilibili', ?, '测试粉丝', 'message', ?, ?, ?, 'queued', 1025, 1025, ?)",
        (user_uid, inbox_id_2, character["id"], character["version"], own_uid),
    ).lastrowid

    draft = store.one("SELECT * FROM dm_drafts WHERE id=?", (draft_id,))
    event = dm._event(draft, character)

    assert "operator_replies" in event
    assert len(event["operator_replies"]) == 1
    assert event["operator_replies"][0]["content"] == "吃了小鱼干啦"
    assert "co_pilot_guidance" in event
    assert "号主/饲养员此前已在平台手动回复过" in event["co_pilot_guidance"]
