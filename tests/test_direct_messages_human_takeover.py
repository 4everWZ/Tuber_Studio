import json
import time

import pytest
from test_direct_messages import messages as messages

from tuber_studio.operation_generation import GenerationInvalid


def platform_message(seq, author, recipient, text, sent_at):
    return {
        "receiver_type": 1,
        "msg_type": 1,
        "msg_status": 0,
        "msg_key": str(seq + 1000),
        "sender_uid": author,
        "receiver_id": recipient,
        "content": json.dumps({"content": text}),
        "timestamp": sent_at,
        "msg_seqno": seq,
    }


@pytest.mark.parametrize("coverage", ["all", "partial", "unverified"])
@pytest.mark.parametrize("clock_drift", [False, True])
async def test_platform_sequence_orders_takeover_and_settles_only_bound_answers(messages, monkeypatch, coverage, clock_drift):
    account, viewer = "900", "123"
    messages.operations.configure({"enabled": True}, account_id=account, character=messages.runtime.character)
    await messages.configure({"enabled": True, "auto_send": True})
    await messages.resume_pending()
    now = int(time.time()) + 1
    answer = (
        "八点开播，节目是深海电台。" if coverage == "partial" else "八点开播，节目是深海电台，会唱三首歌。"
    )
    rows = [
        platform_message(3, viewer, account, "会唱几首歌？", now),
        platform_message(1, viewer, account, "几点开播？", now + 3 * clock_drift),
        platform_message(4, account, viewer, answer, now - 1 * clock_drift),
        platform_message(2, viewer, account, "今天是什么节目？", now + 2 * clock_drift),
    ]
    messages._ingest_history(rows, viewer, account, "观众")
    latest = messages.store.one("SELECT id FROM dm_inbox WHERE message_id='1003'")["id"]
    identity = messages.enqueue("message", latest, automatic=True, schedule=False)
    calls = []

    async def decide(packet, schema, *, max_tokens):
        assert schema["title"] == "ConversationReply"
        event = next(
            json.loads(message["content"].split("：", 1)[1])
            for message in packet
            if message["content"].startswith("当前观众消息：")
        )
        assert [item["text"] for item in event["conversation"]] == [
            "几点开播？",
            "今天是什么节目？",
            "会唱几首歌？",
            answer,
        ]
        assert [item["speaker"] for item in event["conversation"]] == [
            "viewer",
            "viewer",
            "viewer",
            "operator",
        ]
        calls.append(schema["title"])
        covered = [1, 2] if coverage == "partial" else [1, 2, 3]
        return {
            "text": "本鱼这次准备了三首歌。" if coverage == "partial" else "",
            "reply_to": [3] if coverage == "partial" else [],
            "reason": "核对号主已经回复的项目",
            "handled": [
                {
                    "incoming": ref,
                    "outgoing": 4,
                    "quote": "没写在原话里的回答" if coverage == "unverified" else answer,
                }
                for ref in covered
            ],
        }

    monkeypatch.setattr(messages.runtime.llm, "generate_json", decide)
    if coverage == "unverified":
        with pytest.raises(GenerationInvalid, match="实际号主回复"):
            await messages._generate(identity, True)
        assert not messages.adapter.sent and not messages.store.all("SELECT * FROM dm_incoming_settlements")
        return
    await messages._generate(identity, True)
    await messages._generate(identity, True)
    assert calls == ["ConversationReply"]
    status = messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"]
    assert status == ("published" if coverage == "partial" else "cancelled")
    assert messages.adapter.sent == ([(viewer, "本鱼这次准备了三首歌。")] if coverage == "partial" else [])
    settled = messages.store.all(
        "SELECT incoming_id,outgoing_id FROM dm_incoming_settlements ORDER BY incoming_id"
    )
    assert len(settled) == 3
    assert [row["outgoing_id"] for row in settled] == [
        "1004",
        "1004",
        "1" if coverage == "partial" else "1004",
    ]


async def test_history_gap_resumes_with_cursor_across_polls_without_skipping_a_page(messages):
    now = int(time.time())
    account, viewer = "900", "123"
    character = messages.runtime.character["id"]
    messages.store.execute("INSERT INTO dm_sync_cursors VALUES(?,?,?,100,0,0)", (account, character, viewer))

    class History:
        calls = []

        async def history_page(self, uid, *, before):
            assert uid == viewer
            self.calls.append(before)
            seq = before - 1 if before else 105
            rows = [platform_message(seq, viewer, account, f"来信 {seq}", now)]
            return rows, seq if seq > 100 else 0, seq

    history = History()
    assert await messages._sync_history(history, viewer, account, "观众", lambda: None) == (3, False)
    cursor = messages.store.one("SELECT newest,before_sequence FROM dm_sync_cursors")
    assert cursor == {"newest": 100, "before_sequence": 103}
    assert await messages._sync_history(history, viewer, account, "观众", lambda: None) == (3, True)
    assert history.calls == [0, 105, 104, 103, 102, 101]
    assert [
        row["sequence"] for row in messages.store.all("SELECT sequence FROM dm_inbox ORDER BY sequence")
    ] == list(range(100, 106))
    assert messages.store.one("SELECT newest,before_sequence FROM dm_sync_cursors") == {
        "newest": 105,
        "before_sequence": 0,
    }


async def test_pre_send_gap_completion_also_checks_the_latest_page(messages):
    now = int(time.time())
    account, viewer = "900", "123"
    character = messages.runtime.character["id"]
    messages.store.execute(
        "INSERT INTO dm_sync_cursors VALUES(?,?,?,100,102,105)", (account, character, viewer)
    )

    class History:
        calls = []

        async def history_page(self, uid, *, before):
            self.calls.append(before)
            if before:
                seq = before - 1
                return (
                    [platform_message(seq, viewer, account, f"来信 {seq}", now)],
                    seq if seq > 100 else 0,
                    seq,
                )
            return (
                [platform_message(seq, viewer, account, f"来信 {seq}", now) for seq in [106, 105]],
                105,
                106,
            )

    history = History()
    assert await messages._sync_history(
        history, viewer, account, "观众", lambda: None, require_latest=True
    ) == (4, True)
    assert history.calls == [102, 101, 0]
    assert messages.store.one("SELECT newest,before_sequence FROM dm_sync_cursors") == {
        "newest": 106,
        "before_sequence": 0,
    }
