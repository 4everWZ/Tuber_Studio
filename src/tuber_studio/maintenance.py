"""Remove empty scheduler debris while retaining content and publication history."""

import json
import time


def empty_interrupted_rounds(store):
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "browsing_rounds" not in tables:
        return []
    candidates = []
    for row in store.all("SELECT * FROM browsing_rounds WHERE status IN ('cancelled','failed','partial')"):
        if json.loads(row["config"]).get("batch_id"):
            continue
        if store.one("SELECT 1 FROM browsing_intents WHERE round_id=?", (row["id"],)):
            continue
        if "operation_continuations" in tables and store.one(
            "SELECT 1 FROM operation_continuations WHERE parent_round_id=?", (row["id"],)
        ):
            continue
        tasks = store.all("SELECT * FROM browsing_tasks WHERE round_id=?", (row["id"],))
        if any(
            task["material_key"]
            or json.loads(task["payload"]).get("decisions")
            or task["status"] not in {"cancelled", "failed"}
            for task in tasks
        ):
            continue
        task_ids = {task["id"] for task in tasks}
        if "browsing_video_retries" in tables and any(
            retry["task_id"] in task_ids for retry in store.all("SELECT task_id FROM browsing_video_retries")
        ):
            continue
        candidates.append(row["id"])
    return candidates


def prune_empty_interrupted_rounds(store):
    """Recheck under a writer transaction; never remove recoverable work."""
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        identities = empty_interrupted_rounds(store)
        for identity in identities:
            store.db.execute("DELETE FROM browsing_tasks WHERE round_id=?", (identity,))
            store.db.execute("DELETE FROM browsing_rounds WHERE id=?", (identity,))
    return identities


def archive_character_version(store, character_id, version, reason):
    """Hide retired cards from selection; historical provenance stays readable."""
    store.character(character_id, version)
    if store.get("active_character") == character_id:
        active = store.character(character_id)
        if active["version"] == version:
            raise ValueError("当前使用中的人设不能归档")
    store.execute(
        "INSERT OR IGNORE INTO character_archives VALUES(?,?,?,?)",
        (character_id, version, reason, time.time()),
    )


def archive_browsing_history(store, *, before=None):
    """Retire old execution records; keep source identities, receipts and quotas."""
    before = time.time() if before is None else before
    with store.db:
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        if store.one("SELECT 1 FROM browsing_rounds WHERE status IN ('running','queued')") or store.one(
            "SELECT 1 FROM browsing_intents WHERE status='publishing'"
        ):
            raise ValueError("请先停止当前浏览任务后清理记录")
        args = (before,)
        ids = "SELECT id FROM browsing_rounds WHERE created_at<?"
        counts = {}
        counts["drafts"] = db.execute(
            f"UPDATE browsing_intents SET status='archived' WHERE round_id IN ({ids}) "
            "AND status IN ('pending_review','automatic_pending','automatic_skipped','rejected')",
            args,
        ).rowcount
        db.execute(
            f"DELETE FROM browsing_video_retries WHERE task_id IN (SELECT id FROM browsing_tasks WHERE round_id IN ({ids}))",
            args,
        )
        db.execute(
            f"UPDATE operation_continuations SET state='abandoned',owner=NULL,lease_until=NULL "
            f"WHERE channel='generation.browsing' AND parent_round_id IN ({ids}) "
            "AND state NOT IN ('completed','abandoned')",
            args,
        )
        db.execute(f"UPDATE browsing_batches SET state='archived' WHERE round_id IN ({ids})", args)
        db.execute(
            f"UPDATE browsing_batch_items SET status='archived' WHERE batch_id IN "
            f"(SELECT id FROM browsing_batches WHERE round_id IN ({ids}))",
            args,
        )
        db.execute("DELETE FROM browsing_title_decisions WHERE updated_at<?", args)
        counts["tasks"] = db.execute(
            f"UPDATE browsing_tasks SET status='archived' WHERE round_id IN ({ids})", args
        ).rowcount
        counts["rounds"] = db.execute(
            "UPDATE browsing_rounds SET status='archived' WHERE created_at<?", args
        ).rowcount
        db.execute(
            "INSERT OR REPLACE INTO kv VALUES('browsing_history_visible_since',?)", (json.dumps(before),)
        )
    return counts
