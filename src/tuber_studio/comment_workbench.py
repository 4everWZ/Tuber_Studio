"""Public queue across imported comments and browsing candidates."""

import json
import math
import re
from datetime import datetime

from .operation_feedback import continuation_view
from .operations.browsing.thread_context import COMMENT_ID, VIDEO_ID, public_discussion_url
from .providers import public_error

FILTERS = {
    "review": ("pending_review", "approved"),
    "verify": ("publish_uncertain",),
    "processing": ("pending", "queued", "running", "publishing"),
}
PUBLIC_KINDS = ("top_level", "reply_public", "private_handoff")
REVIEW_STATES = frozenset(FILTERS["review"])


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


def _publishable_import(row, can_write):
    return bool(can_write and row["status"] in REVIEW_STATES and row.get("platform") == "bilibili"
                and VIDEO_ID.fullmatch(row["video_id"] or "")
                and COMMENT_ID.fullmatch(row["platform_comment_id"] or ""))


def generation_task(store, identity, *, can_write=False):
    row = store.one("SELECT * FROM comment_tasks WHERE id=?", (identity,))
    if row:
        row["comment_ids"] = json.loads(row.get("comment_ids") or "[]")
        row["existing_drafts"] = store.all(
            "SELECT CAST(d.id AS TEXT) AS id,'comments' AS channel,'manual' AS source,d.status,d.comment_id,"
            "CASE WHEN d.status='private_handoff' THEN NULL ELSE c.video_id END AS video_id,"
            "CASE WHEN d.status='private_handoff' THEN '' ELSE substr(c.content,1,180) END AS excerpt,d.updated_at,"
            "c.platform,c.comment_id AS platform_comment_id "
            "FROM drafts d JOIN comments c ON c.id=d.comment_id JOIN json_each(?) selected ON selected.value=d.comment_id "
            "WHERE COALESCE(d.generation_task_id,'')<>? ORDER BY d.updated_at DESC,d.id DESC",
            (json.dumps(row["comment_ids"]), identity),
        )
        for draft in row["existing_drafts"]:
            draft["can_publish"] = _publishable_import(draft, can_write)
            del draft["platform"], draft["platform_comment_id"]
    return row


def list_queue(store, view, limit, offset, *, video_id=None, comment_ids=None, task_id=None, can_write=False):
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
                   d.updated_at,d.comment_id,d.generation_task_id,c.platform,
                   c.comment_id AS platform_comment_id
              FROM drafts d JOIN comments c ON c.id=d.comment_id
            UNION ALL
            SELECT 'browsing', i.id, i.status,
                   CASE WHEN json_extract(i.target,'$.source_event') IS NOT NULL THEN 'interaction'
                        ELSE 'discovery' END,
                   CASE WHEN i.kind='private_handoff' OR i.status='private_handoff' THEN NULL
                        ELSE json_extract(i.target,'$.bvid') END,
                   CASE WHEN i.kind='private_handoff' OR i.status='private_handoff' THEN ''
                        ELSE substr(coalesce(json_extract(i.target,'$.source_text'),i.text),1,180) END,
                   i.updated_at,NULL,NULL,'bilibili',
                   json_extract(i.target,'$.parent')
              FROM browsing_intents i WHERE i.kind IN (?,?,?)
              AND i.status NOT IN ('automatic_pending','automatic_skipped','archived')
            UNION ALL
            SELECT 'jobs', id, status, 'manual', NULL, '评论生成任务', updated_at,NULL,NULL,NULL,NULL FROM comment_tasks
        ), filtered AS (SELECT * FROM queue WHERE """
        + condition
        + """)
        SELECT *, (SELECT COUNT(*) FROM filtered) AS total FROM filtered
        ORDER BY updated_at DESC, channel, id LIMIT ? OFFSET ?
        """,
        (*PUBLIC_KINDS, *args, limit, offset),
    )
    if not rows and offset:
        return list_queue(store, view, limit, 0, video_id=video_id, comment_ids=ids, task_id=task_id, can_write=can_write)
    total = rows[0]["total"] if rows else 0
    for row in rows:
        del row["total"]
        row["can_publish"] = (
            bool(can_write and row["status"] in REVIEW_STATES) if row["channel"] == "browsing"
            else _publishable_import(row, can_write)
        )
        del row["platform"], row["platform_comment_id"]
    result = {"items": rows, "total": total, "offset": offset}
    if task_id:
        result["task"] = generation_task(store, task_id, can_write=can_write)
    return result


def queue_selection(store, *, can_write, **scope):
    rows = list_queue(store, "review", -1, 0, can_write=can_write, **scope)["items"]
    items = [
        {key: row[key] for key in ("channel", "id", "excerpt", "updated_at")}
        for row in rows if row["can_publish"]
    ]
    return {"items": items, "total": len(items)}


def _publication_row(store, channel, identity):
    if channel == "comments":
        return store.one(
            "SELECT d.status,d.updated_at,d.created_at,d.receipt,c.video_id AS resource,c.thread_id AS root,"
            "c.comment_id AS parent,c.created_at AS source_at FROM drafts d JOIN comments c ON c.id=d.comment_id "
            "WHERE d.id=? AND c.platform='bilibili'", (identity,),
        )
    return store.one(
        "SELECT status,updated_at,created_at,receipt,"
        "COALESCE(json_extract(target,'$.bvid'),'dynamic:' || json_extract(target,'$.oid')) AS resource,"
        "json_extract(target,'$.root') AS root,json_extract(target,'$.parent') AS parent,"
        "json_extract(target,'$.source_event.timestamp') AS source_at FROM browsing_intents "
        "WHERE id=? AND kind IN ('top_level','reply_public')", (identity,),
    )


def _source_time(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo else None
        except (TypeError, ValueError, OverflowError):
            return None


def prepare_queue_selection(store, data):
    if set(data) != {"items"} or not isinstance(data["items"], list) or not data["items"]:
        raise ValueError("请选择要发布的回复")
    selection, seen = [], set()
    for item in data["items"]:
        if not isinstance(item, dict) or set(item) - {"channel", "id", "text", "updated_at"}:
            raise ValueError("批量发布条目格式无效")
        channel, identity = item.get("channel"), item.get("id")
        if channel not in ("comments", "browsing") or not isinstance(identity, str) or not 1 <= len(identity) <= 100:
            raise ValueError("批量发布需要原始草稿标识")
        if channel == "comments" and not re.fullmatch(r"[1-9][0-9]{0,17}", identity):
            raise ValueError("评论草稿标识无效")
        if (channel, identity) in seen:
            raise ValueError("同一草稿只能选一次")
        if "text" in item and not isinstance(item["text"], str):
            raise ValueError("修改后的正文须为字符串")
        if "updated_at" in item and (
            type(item["updated_at"]) not in (float, int) or _source_time(item["updated_at"]) is None
        ):
            raise ValueError("草稿版本无效")
        seen.add((channel, identity))
        selection.append((item, _publication_row(store, channel, identity)))

    # Preserve selection order across discussions, and platform source order within each one.
    groups = {}
    for index, (item, row) in enumerate(selection):
        scope = (row["resource"], row["root"]) if row and row["root"] not in (None, 0, "0", "") else (index,)
        groups.setdefault(scope, []).append(index)

    def order(index, use_time):
        row = selection[index][1]
        parent = str(row["parent"] or "")
        return (_source_time(row["source_at"]) if use_time else 0, int(parent) if COMMENT_ID.fullmatch(parent) else 0,
                row["created_at"])

    ordered = []
    for group in groups.values():
        if len(group) > 1:
            use_time = all(_source_time(selection[index][1]["source_at"]) for index in group)
            group = sorted(group, key=lambda index: order(index, use_time))
        ordered.extend((index, *selection[index]) for index in group)
    return ordered


async def publish_queue_selection(store, comments, browsing, selection):
    for index, item, snapshot in selection:
        channel, identity = item["channel"], item["id"]
        service = comments if channel == "comments" else browsing
        service_id = int(identity) if channel == "comments" else identity
        error = None
        try:
            current = _publication_row(store, channel, identity)
            if not current:
                raise ValueError("草稿不存在或无法公开发送")
            if channel == "comments" and (
                not VIDEO_ID.fullmatch(current["resource"] or "") or not COMMENT_ID.fullmatch(current["parent"] or "")
            ):
                raise ValueError("草稿须绑定实际的 B 站视频和评论")
            if current["status"] in REVIEW_STATES:
                expected = item.get("updated_at", snapshot["updated_at"] if snapshot else None)
                if current["updated_at"] != expected:
                    raise ValueError("草稿在选择后已变化，请检查最新正文")
                if current["status"] != "approved" or "text" in item:
                    service.review(service_id, "approve", item.get("text"))
                await service.publish(service_id)
        except Exception as exc:
            error = public_error(exc)
        current = _publication_row(store, channel, identity)
        result = {"channel": channel, "id": identity, "status": current["status"] if current else "failed"}
        if error:
            result["error"] = error
        if current and current["receipt"]:
            result["receipt"] = json.loads(current["receipt"])
        yield index, result


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
            row["source_url"] = public_discussion_url(row["video_id"], row["platform_comment_id"])
    else:
        row = browsing.public_comment_intent(identity)
    if row and (row.get("kind") == "private_handoff" or row["status"] == "private_handoff"):
        return {"id": row["id"], "status": "private_handoff"}
    if row and channel == "browsing":
        target = row["target"]
        row["source_url"] = public_discussion_url(target.get("bvid"), target.get("parent"), target.get("dynamic_id"))
    return row
