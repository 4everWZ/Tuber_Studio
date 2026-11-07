"""Read-only public queue across imported comments and browsing candidates."""

import json

from .operation_feedback import continuation_view

FILTERS = {
    "review": ("pending_review", "approved"),
    "verify": ("publish_uncertain",),
    "processing": ("pending", "queued", "running", "publishing"),
}
PUBLIC_KINDS = ("top_level", "reply_public", "private_handoff")


def selected_comment_ids(value):
    if value is None:
        return None
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, list) or not 1 <= len(parts) <= 1000:
        raise ValueError("comment_ids 须包含 1 到 1000 个正整数")
    result = []
    for part in parts:
        if isinstance(part, str):
            if not part.isdecimal():
                raise ValueError("comment_ids 须为逗号分隔的正整数")
            part = int(part)
        if type(part) is not int or part < 1:
            raise ValueError("comment_ids 须为正整数")
        if part not in result:
            result.append(part)
    return result


def generation_task(store, identity):
    row = store.one("SELECT * FROM comment_tasks WHERE id=?", (identity,))
    if row:
        row["comment_ids"] = json.loads(row.get("comment_ids") or "[]")
        row["existing_drafts"] = store.all(
            "SELECT CAST(d.id AS TEXT) AS id,'comments' AS channel,'manual' AS source,d.status,d.comment_id,"
            "CASE WHEN d.status='private_handoff' THEN NULL ELSE c.video_id END AS video_id,"
            "CASE WHEN d.status='private_handoff' THEN '' ELSE substr(c.content,1,180) END AS excerpt,d.updated_at "
            "FROM drafts d JOIN comments c ON c.id=d.comment_id JOIN json_each(?) selected ON selected.value=d.comment_id "
            "WHERE COALESCE(d.generation_task_id,'')<>? ORDER BY d.updated_at DESC,d.id DESC",
            (json.dumps(row["comment_ids"]), identity),
        )
    return row


def list_queue(store, view, limit, offset, *, video_id=None, comment_ids=None, task_id=None):
    if view == "history":
        states = tuple(status for statuses in FILTERS.values() for status in statuses)
        condition = "status NOT IN (" + ",".join("?" for _ in states) + ")"
    else:
        states = FILTERS[view]
        condition = "status IN (" + ",".join("?" for _ in states) + ")"
    args = list(states)
    if video_id:
        condition += (
            " AND ((channel<>'jobs' AND video_id=?) OR (channel='jobs' AND id IN ("
            "SELECT j.id FROM comment_tasks j JOIN json_each(j.comment_ids) s JOIN comments c ON c.id=s.value WHERE c.video_id=?)))"
        )
        args.extend((video_id, video_id))
    ids = selected_comment_ids(comment_ids)
    if ids is not None:
        placeholders = ",".join("?" for _ in ids)
        condition += (
            " AND ((channel='comments' AND comment_id IN (" + placeholders + ")) OR "
            "(channel='jobs' AND id IN (SELECT j.id FROM comment_tasks j JOIN json_each(j.comment_ids) s WHERE s.value IN ("
            + placeholders
            + "))))"
        )
        args.extend((*ids, *ids))
    if task_id:
        condition += " AND ((channel='comments' AND generation_task_id=?) OR (channel='jobs' AND id=?))"
        args.extend((task_id, task_id))
    rows = store.all(
        """
        WITH queue AS (
            SELECT 'comments' AS channel, CAST(d.id AS TEXT) AS id, d.status,
                   'manual' AS source, CASE WHEN d.status='private_handoff' THEN NULL ELSE c.video_id END AS video_id,
                   CASE WHEN d.status='private_handoff' THEN '' ELSE substr(c.content,1,180) END AS excerpt,
                   d.updated_at,d.comment_id,d.generation_task_id
              FROM drafts d JOIN comments c ON c.id=d.comment_id
            UNION ALL
            SELECT 'browsing', i.id, i.status,
                   CASE WHEN json_extract(i.target,'$.source_event') IS NOT NULL THEN 'interaction'
                        ELSE 'discovery' END,
                   CASE WHEN i.kind='private_handoff' OR i.status='private_handoff' THEN NULL
                        ELSE json_extract(i.target,'$.bvid') END,
                   CASE WHEN i.kind='private_handoff' OR i.status='private_handoff' THEN ''
                        ELSE substr(coalesce(json_extract(i.target,'$.source_text'),i.text),1,180) END,
                   i.updated_at,NULL,NULL
              FROM browsing_intents i WHERE i.kind IN (?,?,?)
              AND i.status NOT IN ('automatic_pending','automatic_skipped','archived')
            UNION ALL
            SELECT 'jobs', id, status, 'manual', NULL, '评论生成任务', updated_at,NULL,NULL FROM comment_tasks
        ), filtered AS (SELECT * FROM queue WHERE """
        + condition
        + """)
        SELECT *, (SELECT COUNT(*) FROM filtered) AS total FROM filtered
        ORDER BY updated_at DESC, channel, id LIMIT ? OFFSET ?
        """,
        (*PUBLIC_KINDS, *args, limit, offset),
    )
    if not rows and offset:
        return list_queue(store, view, limit, 0, video_id=video_id, comment_ids=ids, task_id=task_id)
    total = rows[0]["total"] if rows else 0
    for row in rows:
        del row["total"]
    result = {"items": rows, "total": total, "offset": offset}
    if task_id:
        result["task"] = generation_task(store, task_id)
    return result


def queue_detail(store, comments, browsing, channel, identity):
    if channel == "jobs":
        row = store.one("SELECT * FROM comment_tasks WHERE id=?", (identity,))
    elif channel == "comments":
        row = store.one(
            """SELECT d.id, d.text, d.status, d.character_id, d.character_version,
                      d.model, d.answer_check, d.error, d.receipt, d.updated_at,d.comment_id,d.generation_task_id,
                      c.video_id, c.content AS source_text, c.display_name AS author,
                      c.comment_id AS platform_comment_id, c.thread_id
               FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE d.id=?""",
            (identity,),
        )
        if row and row["status"] != "private_handoff":
            row = continuation_view(comments.runtime, channel, row)
            row["can_publish"] = comments.policy()["credentials_configured"]
    else:
        row = browsing.public_comment_intent(identity)
    if row and (row.get("kind") == "private_handoff" or row["status"] == "private_handoff"):
        return {"id": row["id"], "status": "private_handoff"}
    return row
