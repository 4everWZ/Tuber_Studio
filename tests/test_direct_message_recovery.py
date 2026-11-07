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
