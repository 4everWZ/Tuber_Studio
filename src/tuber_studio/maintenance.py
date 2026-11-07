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
