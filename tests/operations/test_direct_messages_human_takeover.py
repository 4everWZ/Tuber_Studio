import json
import time

import pytest

from tests.support.operations_helpers import messages as messages
from tuber_studio.operations.kernel.generation import GenerationInvalid


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


@pytest.mark.parametrize("clock_drift", [False, True])
async def test_platform_sequence_orders_takeover_and_cancels_when_operator_replied_last(messages, monkeypatch, clock_drift):
    account, viewer = "900", "123"
    messages.operations.configure({"enabled": True}, account_id=account, character=messages.runtime.character)
    await messages.configure({"enabled": True, "auto_send": True})
    await messages.resume_pending()
    now = int(time.time()) + 1
    answer = "八点开播，节目是深海电台，会唱三首歌。"
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

    async def fail_if_called(*_args, **_kwargs):
        calls.append(True)
        pytest.fail("号主已人工接管回复最新一条，不应再调用模型生成重复草稿")

    monkeypatch.setattr(messages.runtime, "generate_reply", fail_if_called)
    monkeypatch.setattr(messages.runtime.operation_llm, "generate_json", fail_if_called)
    await messages._generate(identity, True)
    assert not calls and not messages.adapter.sent
    status = messages.store.one("SELECT status,error FROM dm_drafts WHERE id=?", (identity,))
    assert status["status"] == "cancelled" and "手动回复" in status["error"]
    settled = messages.store.all("SELECT incoming_id,outgoing_id FROM dm_incoming_settlements ORDER BY incoming_id")
    assert len(settled) == 3 and all(row["outgoing_id"] == "1004" for row in settled)


async def test_chronological_dm_after_operator_takeover_includes_operator_turn_and_labels(messages, monkeypatch):
    from tuber_studio.core.models import OperationReply

    account, viewer = "900", "123"
    messages.operations.configure({"enabled": True}, account_id=account, character=messages.runtime.character)
    await messages.configure({"enabled": True, "auto_send": True})
    await messages.resume_pending()
    now = int(time.time()) + 1
    rows = [
        platform_message(1, viewer, account, "几点开播？", now),
        platform_message(2, account, viewer, "八点准时开播。", now + 1),
        platform_message(3, viewer, account, "那今天主要玩什么呀？", now + 2),
    ]
    messages._ingest_history(rows, viewer, account, "观众")
    latest = messages.store.one("SELECT id FROM dm_inbox WHERE message_id='1003'")["id"]
    identity = messages.enqueue("message", latest, automatic=True, schedule=False)
    seen_conversations = []

    async def fake_reply(scope, character, event, relationship=None, mode="direct_message"):
        seen_conversations.append(event["conversation"])
        return OperationReply(spoken_text="今晚玩潜水员戴夫，八点见！"), {
            "generation_identity": messages.runtime.identity_snapshot(character, event["account_id"]),
            "output": {"needs_review": False},
        }

    monkeypatch.setattr(messages.runtime, "generate_reply", fake_reply)
    await messages._generate(identity, True)
    await messages._generate(identity, True)
    assert len(seen_conversations) == 1
    conv = seen_conversations[0]
    assert [item["text"] for item in conv] == ["几点开播？", "八点准时开播。", "那今天主要玩什么呀？"]
    assert [item["speaker"] for item in conv] == ["viewer", "operator", "viewer"]
    assert [item["role_label"] for item in conv] == ["对方", "号主人工接管", "对方"]
    assert conv[2].get("open") is True
    assert messages.adapter.sent == [(viewer, "今晚玩潜水员戴夫，八点见！")]
    settled = messages.store.all("SELECT incoming_id,outgoing_id FROM dm_incoming_settlements ORDER BY incoming_id")
    assert [row["outgoing_id"] for row in settled] == ["1002", "1"]


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
