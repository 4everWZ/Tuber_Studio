import asyncio

import pytest
from test_browsing import browsing as browsing


async def wait_for(event):
    await asyncio.wait_for(event.wait(), 2)


async def test_video_only_parent_start_does_not_enable_or_probe_private_messages_or_notifications(
    browsing, monkeypatch
):
    await browsing.configure(
        {
            "discovery_enabled": True,
            "top_level_enabled": True,
            "top_level_auto_publish": False,
            "reply_enabled": False,
            "mentions_enabled": False,
            "replies_enabled": False,
        }
    )
    before = browsing.store.get("browsing_policy")
    video = asyncio.Event()
    calls = []

    async def disabled(*args, **kwargs):
        raise AssertionError("Disabled channel must not be probed or started")

    async def run(**kwargs):
        calls.append(kwargs)
        video.set()
        return {"id": "video"}

    monkeypatch.setattr(browsing.messages, "_adapter", disabled)
    monkeypatch.setattr(browsing.adapter, "notifications", disabled)
    monkeypatch.setattr(browsing, "run", run)
    await browsing.open()
    status = await browsing.set_enabled(True)
    await wait_for(video)
    assert status["permissions"]["video_enabled"]
    assert status["permissions"]["private_messages"] == "off"
    assert status["permissions"]["top_level"] == "review"
    assert not browsing.messages.policy()["auto_poll"] and not browsing.messages.policy()["auto_send"]
    assert not hasattr(browsing.discovery, "worker")
    assert calls == [{"automatic": True, "video_only": True}]
    await browsing.set_enabled(False)
    assert browsing.store.get("browsing_policy") == before


async def test_comment_only_parent_start_does_not_probe_or_start_video_discovery(browsing, monkeypatch):
    await browsing.configure(
        {
            "discovery_enabled": False,
            "reply_enabled": True,
            "reply_auto_publish": False,
            "mentions_enabled": True,
            "replies_enabled": True,
        }
    )
    inbox = asyncio.Event()
    calls = []

    async def no_video(*args, **kwargs):
        raise AssertionError("Video discovery was not enabled")

    async def run(**kwargs):
        calls.append(kwargs)
        inbox.set()
        return {"id": "inbox"}

    monkeypatch.setattr(browsing.adapter, "discover", no_video)
    monkeypatch.setattr(browsing, "run", run)
    await browsing.open()
    status = await browsing.set_enabled(True)
    await wait_for(inbox)
    assert calls == [{"automatic": True, "interaction_only": True, "local_interactions": True}]
    assert status["permissions"]["replies"] == "review"
    assert not status["permissions"]["video_enabled"]
    assert status["policy"]["enabled"]


async def test_start_keeps_semantic_validation_as_diagnostics_and_sends_nothing_when_auto_publish_is_off(
    browsing,
):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    browsing.store.set("browsing_semantic_validation", {"passed": False, "signature": "old-diagnostic"})
    await browsing.configure({"discovery_enabled": True, "top_level_auto_publish": False})
    status = await browsing.set_enabled(True)
    assert status["policy"]["enabled"] and status["automatic_sending_ready"]
    assert status["semantic_validation"]["passed"] is False
    result = await browsing.run(automatic=True, video_only=True)
    await browsing.active[result["id"]]
    assert not browsing.adapter.sent
    assert all(item["status"] == "pending_review" for item in browsing.intents())


async def test_stop_during_startup_cannot_late_enable_the_parent(browsing, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def preflight(**kwargs):
        entered.set()
        await release.wait()
        browsing.blockers = []

    monkeypatch.setattr(browsing, "preflight", preflight)
    starting = asyncio.create_task(browsing.set_enabled(True))
    await wait_for(entered)
    await browsing.set_enabled(False)
    release.set()
    with pytest.raises(ValueError, match="已取消"):
        await starting
    assert not browsing.operations.policy()["enabled"]


async def test_parent_stop_preserves_settings_but_gates_every_automatic_channel(browsing):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"discovery_enabled": True})
    await browsing.messages.configure({"enabled": True, "auto_poll": True, "auto_send": True})
    browsing.store.set("dynamic_policy", {"enabled": True, "auto_publish": True})
    saved_messages = browsing.store.get("message_policy")
    await browsing.set_enabled(True)
    assert browsing.messages.policy()["auto_poll"] and browsing.dynamics.policy()["enabled"]
    await browsing.set_enabled(False)
    assert browsing.store.get("message_policy") == saved_messages
    assert not browsing.messages.policy()["auto_poll"] and not browsing.messages.policy()["auto_send"]
    assert not browsing.dynamics.policy()["enabled"] and browsing.dynamics.policy()["base"]["enabled"]
    assert browsing.permissions()["video_enabled"] and browsing.permissions()["dynamics"] == "auto"


async def test_resumed_batch_uses_current_permissions_but_keeps_original_retrieval_cursor(browsing):
    config = browsing.policy() | {
        "autonomous_batch": True, "sources": ["search"], "keywords": ["鸡蛋"],
        "top_level_auto_publish": True, "reply_enabled": True,
    }
    original, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 1, False)
    browsing.repo.finish_round(original["id"], "paused")
    browsing.repo.batches.retrieval(original, {"page": 2, "complete": False})
    current = config | {
        "sources": ["recommend"], "keywords": [], "top_level_auto_publish": False,
        "reply_enabled": False, "workers": 1, "max_replies_per_video": 0,
    }
    restored, created = browsing.repo.create_round("100", browsing.runtime.character, current, 2, False)
    assert created and restored["id"] == original["id"] and restored["epoch"] == 2
    assert restored["config"]["sources"] == ["search"] and restored["config"]["keywords"] == ["鸡蛋"]
    assert not restored["config"]["top_level_auto_publish"] and not restored["config"]["reply_enabled"]
    assert restored["config"]["workers"] == 1 and restored["config"]["max_replies_per_video"] == 0
    assert browsing.repo.batches.get(restored)["retrieval"]["page"] == 2
    assert restored["deadline"] is None


async def test_interaction_round_has_no_implicit_whole_round_time_limit(browsing):
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.policy() | {"interaction_only": True}, 0, False
    )
    assert row["deadline"] is None


async def test_manual_notification_checkpoint_can_resume_while_parent_stays_stopped(browsing, monkeypatch):
    import json
    import time

    await browsing.configure({"reply_enabled": True, "mentions_enabled": True, "mention_reply_enabled": True})
    await browsing._current_account()
    character = browsing.runtime.character
    checkpoint = browsing.runtime.continuations.begin(
        "generation.browsing", "manual-mention",
        {"task": "generation", "character": {"id": character["id"], "version": character["version"]}},
        {"kind": "validation_error"},
    )
    browsing.runtime.continuations._state(checkpoint["id"], "checkpoint")
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES(?,?,?,?,?,?,?,?,?)",
        ("paused-mention", "100", "mention", "source", time.time(), "checkpoint", "{}",
         json.dumps({"generation_continuation_id": checkpoint["id"]}), time.time()),
    )
    calls = []

    async def preview(**args):
        calls.append(args)
        return {"status": "queued"}

    monkeypatch.setattr(browsing, "run", preview)
    assert not browsing.operations.policy()["enabled"]
    result = await browsing.resume_continuation("generation:event:paused-mention", checkpoint["id"])
    assert result["status"] == "queued" and calls == [{"preview": True, "notifications_only": True}]
    assert not browsing.operations.policy()["enabled"] and not browsing.adapter.sent


async def test_retired_automatic_draft_becomes_manual_review_and_legacy_binding_stays_readable(browsing):
    import json

    from test_browsing import finished

    row = await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    legacy = row["config"] | {"discovery_publish": True, "discovery_id": "retired-worker"}
    browsing.store.execute("UPDATE browsing_rounds SET config=? WHERE id=?", (json.dumps(legacy), row["id"]))
    browsing.store.execute(
        "UPDATE browsing_intents SET status='automatic_pending',auto_authorized=1 WHERE id=?", (intent["id"],)
    )
    browsing.repo.recover()
    retained = browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (intent["id"],))
    assert retained["status"] == "pending_review" and not retained["auto_authorized"]
    prepared = await browsing.publication._publication_context(intent["id"], False)
    assert prepared.binding["source"]["discovery"] is True
    browsing.publication._defer_revision(intent["id"], prepared, "waiting")
    assert browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (intent["id"],))["status"] == "pending_review"
    browsing.review(intent["id"], "approve")
    assert (await browsing.publish(intent["id"]))["status"] == "published"
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    assert len(browsing.adapter.sent) == 1
