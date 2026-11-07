import asyncio
import time

import httpx
import pytest
from test_direct_messages import finished, incoming
from test_direct_messages import messages as messages

from tuber_studio.direct_messages import DirectMessages


async def test_cold_start_keeps_private_generation_queued_until_explicit_resume_and_never_reuses_send_grant(
    messages,
):
    await messages.configure({"enabled": True, "auto_poll": True, "auto_send": True})
    source = incoming(messages, account_id="900")
    identity = messages.enqueue("message", source, automatic=True, schedule=False)
    messages.store.execute("UPDATE dm_drafts SET status='generating' WHERE id=?", (identity,))
    await messages.close()
    # Simulate a saved overlay being observed before its owning startup code
    # restores standby. Merely observing enabled cannot authorize the inbox.
    messages.operations.configure({"enabled": True}, account_id="900", character=messages.runtime.character)
    restored = DirectMessages(
        messages.settings, messages.store, messages.runtime, messages.comments, messages.adapter
    )
    try:
        await restored.open()
        await asyncio.sleep(0)
        await restored._resume_pending()
        assert restored.queue.empty()
        assert (
            messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"
        )
        assert not restored.adapter.sent
        await restored.resume_pending()
        assert (await finished(restored, identity))["status"] == "pending_review"
        assert not restored.adapter.sent
    finally:
        await restored.close()


async def test_network_interruption_preserves_private_source_and_resumes_for_review_without_busy_retry(
    messages, monkeypatch
):
    await messages.configure({"enabled": True, "auto_send": True})
    original = messages.runtime.generate_reply
    calls = 0

    async def disconnected(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("model offline")

    monkeypatch.setattr(messages.runtime, "generate_reply", disconnected)
    source = incoming(messages, account_id="900")
    identity = messages.enqueue("message", source, automatic=True)
    await asyncio.wait_for(messages.queue.join(), 3)
    saved = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert saved["status"] == "queued" and saved["resume_pending"] == 1
    assert saved["source_id"] == source and saved["next_attempt_at"] > time.time()
    await messages.resume_pending()
    assert calls == 1 and messages.queue.empty()
    monkeypatch.setattr(messages.runtime, "generate_reply", original)
    messages.store.execute("UPDATE dm_drafts SET next_attempt_at=0 WHERE id=?", (identity,))
    messages.operations.configure({"enabled": True}, account_id="900", character=messages.runtime.character)
    await messages.resume_pending()
    assert (await finished(messages, identity))["status"] == "pending_review"
    assert not messages.adapter.sent


async def test_pending_private_recovery_keeps_other_accounts_and_archived_character_versions_untouched(
    messages,
):
    first = messages.enqueue("message", incoming(messages, key="old", account_id="900"), schedule=False)
    other = messages.enqueue(
        "message", incoming(messages, key="other-account", account_id="901"), schedule=False
    )
    previous = messages.runtime.character
    current = messages.store.save_character(previous)
    messages.store.execute(
        "INSERT INTO character_archives VALUES(?,?,?,?)",
        (previous["id"], previous["version"], "retained old card", time.time()),
    )
    latest = messages.enqueue("message", incoming(messages, key="current", account_id="900"), schedule=False)
    messages.store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE status='queued'")
    messages.operations.configure({"enabled": True}, account_id="900", character=messages.runtime.character)
    await messages.resume_pending()
    assert (await finished(messages, latest))["status"] == "pending_review"
    assert current["version"] == messages.runtime.character["version"]
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (first,))["status"] == "queued"
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (other,))["status"] == "queued"
    with pytest.raises(ValueError, match="归档"):
        await messages._generate(first, False)
    assert (
        messages.store.one("SELECT character_version FROM dm_drafts WHERE id=?", (first,))[
            "character_version"
        ]
        == previous["version"]
    )
    assert not messages.adapter.sent


async def test_archived_queued_draft_does_not_block_polling_new_message_for_current_version(messages, monkeypatch):
    old = messages.enqueue("message", incoming(messages, key="archived-pending", account_id="900"), schedule=False)
    previous = messages.runtime.character
    current = messages.store.save_character(previous)
    messages.store.execute("INSERT INTO character_archives VALUES(?,?,?,?)", (previous["id"], previous["version"], "retained old card", time.time()))
    messages.operations.configure({"enabled": True}, account_id="900", character=current)
    await messages.configure({"enabled": True, "auto_poll": True, "auto_send": False})
    await messages.resume_pending()
    source = incoming(messages, key="current-new-input", account_id="900", timestamp=time.time() + 1)
    seen = []
    original = messages.runtime.generate_reply

    async def capture(scope, character, *args, **kwargs):
        seen.append(character["version"])
        return await original(scope, character, *args, **kwargs)

    monkeypatch.setattr(messages.runtime, "generate_reply", capture)
    messages._queue_newest_messages("900")
    await asyncio.wait_for(messages.queue.join(), 3)
    latest = messages.store.one("SELECT * FROM dm_drafts WHERE source_kind='message' AND source_id=?", (source,))
    assert latest and latest["status"] == "pending_review" and latest["character_version"] == current["version"]
    assert seen == [current["version"]]
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (old,))["status"] == "queued"
    assert not messages.adapter.sent


async def test_saving_private_permissions_while_parent_stopped_does_not_resume_old_work(messages):
    identity = messages.enqueue(
        "message", incoming(messages, account_id="900"), schedule=False
    )
    messages.store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE id=?", (identity,))
    await messages.configure({"enabled": True, "auto_poll": True, "auto_send": True})
    await messages.resume_pending()
    assert messages.queue.empty()
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"
    manual = messages.enqueue("message", incoming(messages, key="new-manual", account_id="900"))
    assert (await finished(messages, manual))["status"] == "pending_review"
    assert not messages.adapter.sent


def test_private_handoff_accepts_unbounded_parent_but_still_rejects_stopped_parent(messages):
    import json

    from tuber_studio.operations.browsing.models import BrowsingPolicy

    character = messages.runtime.character
    repository = messages.runtime.browsing_repository
    parent, _ = repository.create_round("900", character, BrowsingPolicy().model_dump(), 0, False)
    draft = {
        "video_evidence": json.dumps({"origin_round_id": parent["id"]}),
        "account_id": "900", "character_id": character["id"], "character_version": character["version"],
    }
    assert parent["deadline"] is None
    assert messages._origin_round(draft, active=True)["id"] == parent["id"]
    repository.finish_round(parent["id"], "paused")
    with pytest.raises(ValueError, match="已停止"):
        messages._origin_round(draft, active=True)
