"""Review batches use normal scheduling without replaying an older failed run."""

import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing


async def test_review_runs_same_decisions_and_keeps_drafts_without_sending(browsing):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"discovery_enabled": True, "top_level_auto_publish": False})
    status = await browsing.start_review_session()
    identity = status["review_session"]["id"]
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["config"]["autonomous_batch"] is True
    assert row["config"]["review_session_id"] == identity
    assert row["config"]["max_model_calls"] == 0 and row["deadline"] is None
    assert browsing.intents()[0]["status"] == "pending_review"
    assert not browsing.adapter.sent
    assert not browsing.store.all("SELECT * FROM account_writes")


@pytest.mark.parametrize("channel", ["comments", "messages", "dynamics"])
@pytest.mark.parametrize("automatic", [False, True])
async def test_review_refuses_writes_even_if_settings_are_changed_after_start(browsing, channel, automatic):
    await browsing.configure({"discovery_enabled": True, "top_level_auto_publish": False})
    await browsing.start_review_session()
    # A stale worker or a concurrent configuration change cannot bypass review.
    await browsing.configure({"top_level_auto_publish": True})
    with pytest.raises(ValueError, match="review验收"):
        browsing.operations.reserve("100", browsing.runtime.character, "review-write", channel, automatic=automatic)
    assert not browsing.store.all("SELECT * FROM account_writes")
    await browsing.stop()
    assert browsing.store.get("browsing_review_session")["state"] == "stopped"
    assert "review_session_id" not in browsing.configured()
    token = browsing.operations.reserve("100", browsing.runtime.character, "manual-after-stop", channel)
    assert token


def test_review_resume_is_scoped_and_preserves_older_paused_round(browsing):
    repo, character = browsing.repo, browsing.runtime.character
    config = browsing.configured() | {"autonomous_batch": True}
    old, _ = repo.create_round("100", character, config, 1, False)
    task = repo.task(old["id"], {"bvid": BV, "title": "旧批次"})
    repo.defer_video(old, BV, task, 1)
    browsing.store.execute("UPDATE browsing_rounds SET status='paused' WHERE id=?", (old["id"],))
    review_config = config | {"review_session_id": "new-review"}
    current, created = repo.create_round("100", character, review_config, 2, False)
    assert created and current["id"] != old["id"]
    assert repo.round(old["id"])["status"] == "paused"
    assert repo.retry_candidates(current) == []
    browsing.store.execute("UPDATE browsing_rounds SET status='paused' WHERE id=?", (current["id"],))
    resumed, created = repo.create_round("100", character, review_config, 3, False)
    assert created and resumed["id"] == current["id"]
    assert repo.round(old["id"])["status"] == "paused"
    assert repo.retry_candidates(old)[0]["bvid"] == BV


async def test_review_refuses_saved_automatic_permissions_and_rolls_back_failed_start(browsing, monkeypatch):
    await browsing.configure({"top_level_auto_publish": True})
    with pytest.raises(ValueError, match="审核模式"):
        await browsing.start_review_session()
    assert not browsing.operations.policy()["enabled"]
    assert not browsing.store.get("browsing_review_session")

    await browsing.configure({"discovery_enabled": True, "top_level_auto_publish": False})
    previous = {"id": "previous", "account_id": "100"}
    browsing.store.set("browsing_review_session", previous)

    async def unavailable():
        raise ValueError("模型离线")

    monkeypatch.setattr(browsing, "check_browsing_model", unavailable)
    with pytest.raises(ValueError, match="模型离线"):
        await browsing.start_review_session()
    assert browsing.store.get("browsing_review_session") == previous
    assert not browsing.operations.policy()["enabled"]


async def test_stopped_review_can_resume_its_own_persistent_cohort_only(browsing):
    await browsing.configure({"discovery_enabled": True, "top_level_auto_publish": False})
    status = await browsing.start_review_session()
    session = status["review_session"]
    first = await finished(browsing, automatic=True, video_only=True)
    await browsing.stop()
    with pytest.raises(ValueError, match="不匹配"):
        await browsing.start_review_session(resume_session_id="another-cohort")
    resumed = await browsing.start_review_session(resume_session_id=session["id"])
    assert resumed["review_session"]["id"] == session["id"]
    assert resumed["review_session"]["started_at"] == session["started_at"]
    assert resumed["review_session"]["state"] == "running"
    assert browsing.repo.round(first["id"])["config"]["review_session_id"] == session["id"]


async def test_review_failed_after_parent_activation_disables_it_and_restores_previous_session(browsing, monkeypatch):
    await browsing.configure({"discovery_enabled": True, "top_level_auto_publish": False})
    previous = {"id": "old", "state": "stopped"}
    browsing.store.set("browsing_review_session", previous)

    async def failed_resume():
        raise ValueError("恢复队列失败")

    monkeypatch.setattr(browsing.messages, "resume_pending", failed_resume)
    with pytest.raises(ValueError, match="恢复队列失败"):
        await browsing.start_review_session()
    assert not browsing.operations.policy()["enabled"]
    assert browsing.store.get("browsing_review_session") == previous
