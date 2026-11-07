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
        assert kwargs == {"automatic": True, "video_only": True}
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
    assert len(calls) == 1
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

    async def no_video(*args, **kwargs):
        raise AssertionError("Video discovery was not enabled")

    async def run(**kwargs):
        assert kwargs == {"automatic": True, "interaction_only": True}
        inbox.set()
        return {"id": "inbox"}

    monkeypatch.setattr(browsing.adapter, "discover", no_video)
    monkeypatch.setattr(browsing, "run", run)
    await browsing.open()
    status = await browsing.set_enabled(True)
    await wait_for(inbox)
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
