"""Read-only task DTOs for durable batch choices that need no worker."""

import json

from ...operation_feedback import continuation_view, generation_view
from .evidence import EvidenceUnavailable


def selection_tasks(store, *, status=None, round_id=None, task_id=None):
    items = []
    seen = set()
    for row in store.all(
        "SELECT i.*,b.round_id,b.updated_at,r.account_id,r.character_id,r.character_version,r.created_at "
        "FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id "
        "JOIN browsing_rounds r ON r.id=b.round_id "
        "WHERE i.task_id IS NULL AND i.status IN ('skipped','later') ORDER BY b.updated_at DESC LIMIT 100"
    ):
        if (
            round_id is None
            and task_id is None
            and row["created_at"] < store.get("browsing_history_visible_since", 0)
        ):
            continue
        identity = f"batch:{row['batch_id']}:{row['bvid']}"
        if (
            status is not None
            and row["status"] != status
            or round_id is not None
            and row["round_id"] != round_id
            or task_id is not None
            and identity != task_id
        ):
            continue
        candidate = json.loads(row["payload"])
        choice = candidate.get("selection", {})
        selection_key = (
            row["account_id"],
            row["character_id"],
            row["bvid"],
            candidate.get("title"),
            choice.get("action"),
        )
        if round_id is None and task_id is None and selection_key in seen:
            continue
        seen.add(selection_key)
        items.append(
            candidate
            | {
                "id": identity,
                "round_id": row["round_id"],
                "bvid": row["bvid"],
                "status": row["status"],
                "account_id": row["account_id"],
                "character_id": row["character_id"],
                "character_version": row["character_version"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "autonomous": True,
                "cache_status": "not_prepared",
                "phase": "title_selection",
                "reason": choice.get("explanation") or "本批标题筛选已保存，未开始观看",
            }
        )
    return items


def tasks(browsing, status=None, *, round_id=None, task_id=None):
    filters = [
        (key, value)
        for key, value in (("t.status", status), ("t.round_id", round_id), ("t.id", task_id))
        if value is not None
    ]
    where = " WHERE " + " AND ".join(key + "=?" for key, _ in filters) if filters else ""
    args = tuple(value for _, value in filters)
    rows = browsing.store.all(
        "SELECT t.*,r.account_id,r.character_id,r.character_version,"
        "(COALESCE(json_extract(r.config,'$.autonomous_batch'),0) OR "
        "COALESCE(json_extract(r.config,'$.discovery_publish'),0)) AS autonomous "
        "FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id"
        + where
        + " ORDER BY t.created_at DESC LIMIT 100",
        args,
    )
    result = []
    for record in rows:
        if (
            round_id is None
            and task_id is None
            and record["created_at"] < browsing.store.get("browsing_history_visible_since", 0)
        ):
            continue
        task = dict(browsing.repo.decode(record), **json.loads(record["payload"]))
        continuation_id = task.get("generation_continuation_id")
        task["continuation"] = generation_view(browsing.runtime, continuation_id, "browsing", task["id"])
        if task["continuation"] and task["continuation"].get("output_visibility") in {
            "private",
            "unresolved",
        }:
            task["error"] = "生成原文在私信工作区查看"
        task["cache_status"] = "not_prepared"
        if record["material_key"]:
            try:
                material = browsing.evidence.load(record["material_key"], allow_expired=True)
                task["cache_status"] = "available"
                task["metadata"] = material["metadata"]
                task["coverage"] = material.get("coverage", task.get("coverage"))
                task["missing_parts"] = material.get("missing_parts", [])
                task["evidence"] = [
                    segment
                    for segment in browsing._segments(material)
                    if segment["id"] in task.get("evidence_ids", [])
                ]
            except EvidenceUnavailable:
                task["cache_status"] = "missing"
                task["evidence"] = []
                task["subtitle_tracks"] = []
                task["missing_parts"] = []
        result.append(task)
    result.extend(selection_tasks(browsing.store, status=status, round_id=round_id, task_id=task_id))
    return sorted(result, key=lambda item: item["updated_at"], reverse=True)[:100]


def rounds(browsing):
    return [
        browsing.repo.decode(r)
        for r in browsing.store.all(
            """SELECT r.*, (SELECT COUNT(*) FROM browsing_intents i
                   WHERE i.round_id=r.id AND i.kind IN ('top_level','reply_public')) AS public_drafts
               FROM browsing_rounds r WHERE r.created_at>=? ORDER BY r.created_at DESC LIMIT 30""",
            (browsing.store.get("browsing_history_visible_since", 0),),
        )
    ]


def intents(browsing):
    return [
        continuation_view(browsing.runtime, "browsing", browsing.repo.decode(r))
        for r in browsing.store.all(
            "SELECT * FROM browsing_intents WHERE status<>'archived' ORDER BY created_at DESC LIMIT 100"
        )
    ]


def public_comment_intent(browsing, identity):
    row = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
    if not row:
        return None
    if row["kind"] not in {"top_level", "reply_public", "private_handoff"}:
        return None
    if row["kind"] == "private_handoff" or row["status"] == "private_handoff":
        return {"id": row["id"], "status": "private_handoff"}
    evidence, unavailable = [], 0
    references = set(row["evidence"] or [])
    for key in row["material_keys"] or []:
        try:
            material = browsing.evidence.load(key, allow_expired=True)
            evidence.extend(
                segment for segment in browsing._segments(material) if segment["id"] in references
            )
        except EvidenceUnavailable:
            unavailable += 1
    row["evidence_segments"] = evidence
    row["unavailable_materials"] = unavailable
    row["can_publish"] = browsing.comments.policy()["credentials_configured"] and not unavailable
    return continuation_view(browsing.runtime, "browsing", row)


def inbox(browsing):
    rows = [
        browsing.repo.decode(r)
        for r in browsing.store.all("SELECT * FROM browsing_events ORDER BY timestamp DESC LIMIT 100")
    ]
    for row in rows:
        saved = {}
        if row.get("error"):
            try:
                saved = json.loads(row["error"])
            except (ValueError, TypeError):
                pass
            if isinstance(saved, dict):
                row["error"] = saved.get("message", row["error"])
                row["next_at"] = saved.get("next_at")
                row["occurrences"] = saved.get("occurrences", 0)
            else:
                saved = {}
        if row["status"] == "checkpoint":
            row["continuation"] = generation_view(
                browsing.runtime, saved.get("generation_continuation_id"), "browsing", "event:" + row["id"]
            )
            row["error"] = saved.get("message")
    count = browsing.store.one("SELECT COUNT(*) AS n FROM dm_inbox")["n"]
    return {"items": rows, "private_count": count, "private_href": "#messages"}
