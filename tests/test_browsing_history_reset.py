import time

from test_browsing import browsing as browsing

from tuber_studio.comment_workbench import list_queue
from tuber_studio.maintenance import archive_browsing_history


async def test_reset_retires_old_runs_and_unreviewed_drafts_without_losing_receipts_or_dedup(browsing):
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.policy() | {"autonomous_batch": True}, 0, False
    )
    browsing.repo.batches.ensure(row)
    task = browsing.repo.task(row["id"], {"bvid": "BV1d8pU6xEZX"})
    browsing.repo.finish_round(row["id"], "completed")
    for state in ("pending_review", "approved", "published", "publish_uncertain"):
        browsing.store.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,source_key,round_id,status,kind,material_keys) VALUES(?,'100','whale',?,?,?,'top_level','[]')",
            (state, state, row["id"], state),
        )
    result = archive_browsing_history(browsing.store, before=row["created_at"] + 10)
    assert result == {"drafts": 1, "tasks": 1, "rounds": 1}
    assert browsing.store.one("SELECT status FROM browsing_tasks WHERE id=?", (task,))["status"] == "archived"
    for state in ("approved", "published", "publish_uncertain"):
        assert (
            browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (state,))["status"] == state
        )
    assert (
        browsing.store.one("SELECT source_key FROM browsing_intents WHERE id='pending_review'")["source_key"]
        == "pending_review"
    )
    assert not browsing.tasks() and not browsing.rounds()
    assert {item["id"] for item in browsing.intents()} == {"approved", "published", "publish_uncertain"}
    assert {item["id"] for item in list_queue(browsing.store, "history", 20, 0)["items"]} == {"published"}
    assert {item["id"] for item in list_queue(browsing.store, "review", 20, 0)["items"]} == {"approved"}


async def test_archive_filters_old_failure_counts_before_combining_a_new_occurrence(browsing):
    browsing.discovery.remember_account("100")
    boundary = time.time()
    config = browsing.policy() | {"autonomous_batch": True, "discovery_publish": True}
    old, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, False)
    browsing.repo.batches.save_plan(old, {"videos": [], "workers": 1})
    browsing.repo.finish_round(old["id"], "completed")
    browsing.store.execute(
        "UPDATE browsing_rounds SET created_at=?,updated_at=? WHERE id=?",
        (boundary - 30, boundary - 30, old["id"]),
    )
    browsing.store.execute(
        "INSERT INTO browsing_batch_failures VALUES(?,?,?,?,?,?)",
        (old["config"]["batch_id"], "BV1d8pU6xEZX", "same-signature", "读取字幕超时", 5, boundary - 20),
    )
    archive_browsing_history(browsing.store, before=boundary)
    new, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, False)
    browsing.repo.batches.save_plan(new, {"videos": [], "workers": 1})
    browsing.repo.finish_round(new["id"], "completed")
    browsing.store.execute(
        "INSERT INTO browsing_batch_failures VALUES(?,?,?,?,?,?)",
        (new["config"]["batch_id"], "BV1d8pU6xEZX", "same-signature", "读取字幕超时", 1, boundary + 1),
    )
    issues = browsing.discovery.records()["issues"]
    assert len(issues) == 1 and issues[0]["occurrences"] == 1
    assert issues[0]["error"] == "读取字幕超时"
    assert (
        browsing.store.one(
            "SELECT count FROM browsing_batch_failures WHERE batch_id=?", (old["config"]["batch_id"],)
        )["count"]
        == 5
    )
