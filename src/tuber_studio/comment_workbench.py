"""Read-only public queue across imported comments and browsing candidates."""

from .operation_feedback import continuation_view

FILTERS = {
    "review": ("pending_review", "approved"),
    "verify": ("publish_uncertain",),
    "processing": ("queued", "running", "publishing"),
}
PUBLIC_KINDS = ("top_level", "reply_public", "private_handoff")


def list_queue(store, view, limit, offset):
    if view == "history":
        states = tuple(status for statuses in FILTERS.values() for status in statuses)
        condition = "status NOT IN (" + ",".join("?" for _ in states) + ")"
    else:
        states = FILTERS[view]
        condition = "status IN (" + ",".join("?" for _ in states) + ")"
    rows = store.all(
        """
        WITH queue AS (
            SELECT 'comments' AS channel, CAST(d.id AS TEXT) AS id, d.status,
                   'manual' AS source, CASE WHEN d.status='private_handoff' THEN NULL ELSE c.video_id END,
                   CASE WHEN d.status='private_handoff' THEN '' ELSE substr(c.content,1,180) END AS excerpt,
                   d.updated_at
              FROM drafts d JOIN comments c ON c.id=d.comment_id
            UNION ALL
            SELECT 'browsing', i.id, i.status,
                   CASE WHEN json_extract(i.target,'$.source_event') IS NOT NULL THEN 'interaction'
                        ELSE 'discovery' END,
                   CASE WHEN i.kind='private_handoff' OR i.status='private_handoff' THEN NULL
                        ELSE json_extract(i.target,'$.bvid') END,
                   CASE WHEN i.kind='private_handoff' OR i.status='private_handoff' THEN ''
                        ELSE substr(coalesce(json_extract(i.target,'$.source_text'),i.text),1,180) END,
                   i.updated_at
              FROM browsing_intents i WHERE i.kind IN (?,?,?)
              AND i.status NOT IN ('automatic_pending','automatic_skipped')
            UNION ALL
            SELECT 'jobs', id, status, 'manual', NULL, '评论生成任务', updated_at FROM comment_tasks
        ), filtered AS (SELECT * FROM queue WHERE """
        + condition
        + """)
        SELECT *, (SELECT COUNT(*) FROM filtered) AS total FROM filtered
        ORDER BY updated_at DESC, channel, id LIMIT ? OFFSET ?
        """,
        (*PUBLIC_KINDS, *states, limit, offset),
    )
    if not rows and offset:
        return list_queue(store, view, limit, 0)
    total = rows[0]["total"] if rows else 0
    for row in rows:
        del row["total"]
    return {"items": rows, "total": total, "offset": offset}


def queue_detail(store, comments, browsing, channel, identity):
    if channel == "jobs":
        row = store.one("SELECT * FROM comment_tasks WHERE id=?", (identity,))
    elif channel == "comments":
        row = store.one(
            """SELECT d.id, d.text, d.status, d.character_id, d.character_version,
                      d.model, d.answer_check, d.error, d.receipt, d.updated_at,
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
