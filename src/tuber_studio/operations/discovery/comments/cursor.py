"""Discovery comment pagination cursors, backlog queueing, and old-page revisit handoffs."""

import inspect
import json
import time

from ....core.models import content_hash
from ....core.store import dump
from ....infra.inference.providers import public_error
from ...channels.comments.scanner import low_context_skip_reason
from .admission import claims_manager
from .timeline import _extract_timestamp, comment_source, merge_continuous_comments, observe_thread

PENDING_VISITS = "'received','source_verified','context_ready','reply_deferred'"


def comment_cursor_key(row, bvid):
    return f"browsing_comment_page:{row['account_id']}:{row['character_id']}:{bvid}"


def commit_comment_context(browsing, context):
    if progress := (context or {}).get("_scroll_progress"):
        if browsing.store.get(progress["key"], 1) == progress.get("page", 1):
            browsing.store.set(progress["key"], progress["next_page"])


async def _fetch_comment_context(browsing, row, bvid, *, scroll=False, newest=True, latest_only=False):
    fetch = browsing.adapter.fetch
    parameters = inspect.signature(fetch).parameters
    if latest_only:
        if "newest" in parameters:
            return await browsing._read(fetch, bvid, 1, newest=True)
        return await browsing._read(fetch, bvid, 1)
    if row["preview"] and not scroll or "start_page" not in parameters:
        return await browsing._read(fetch, bvid, 1)
    cursor_key = comment_cursor_key(row, bvid)
    page = max(1, int(browsing.store.get(cursor_key, 1)))
    arrivals = await browsing._read(fetch, bvid, 1, newest=True) if newest and "newest" in parameters else None
    context = await browsing._read(fetch, bvid, 1, start_page=page)
    items, seen = [], set()
    for source in (arrivals, context):
        if source and source.get("video_id", bvid) != bvid:
            raise ValueError("评论页与当前视频不一致")
        for item in (source or {}).get("items", []):
            identity = str(item.get("comment_id", ""))
            if identity and identity not in seen:
                items.append(item)
                seen.add(identity)
    return context | {
        "items": items,
        "_scroll_progress": {"key": cursor_key, "page": page, "next_page": context.get("next_page") or 1},
    }


async def round_comment_page(browsing, row, bvid):
    """Read and freeze the first successful newest page for this batch."""
    batch_id = row["config"]["batch_id"]
    saved = browsing.store.one(
        "SELECT document FROM browsing_batch_comment_pages WHERE batch_id=? AND bvid=?",
        (batch_id, bvid),
    )
    if saved:
        return json.loads(saved["document"])
    if row["config"].get("review_session_id"):
        context = await _fetch_comment_context(browsing, row, bvid, scroll=True, latest_only=True)
    else:
        context = await browsing._read_proactively(
            _fetch_comment_context, browsing, row, bvid, scroll=True, latest_only=True
        )
    if context.get("video_id", bvid) != bvid:
        raise ValueError("评论页与当前视频不一致")
    if context.get("comment_fetch_status") in {"unavailable", "blocked", "failed"}:
        raise TimeoutError("最新评论页暂不可用")
    with browsing.store.db:
        browsing.store.db.execute(
            "INSERT OR IGNORE INTO browsing_batch_comment_pages VALUES(?,?,?)",
            (batch_id, bvid, dump(context)),
        )
        document = json.loads(
            browsing.store.one(
                "SELECT document FROM browsing_batch_comment_pages WHERE batch_id=? AND bvid=?", (batch_id, bvid)
            )["document"]
        )
        if document.get("next_page") is not None:
            scope = (
                row["account_id"],
                row["character_id"],
                row["character_version"],
                row["config"].get("review_session_id", ""),
                bvid,
            )
            page = max(1, int(browsing.store.get(comment_cursor_key(row, bvid), 1)))
            browsing.store.db.execute(
                "INSERT OR IGNORE INTO browsing_comment_revisits VALUES(?,?,?,?,?,?,?,?,?)",
                (*scope, row["id"], page, time.time(), None),
            )
    return document


def _inbox_owns_comment(browsing, row, *, oid, bvid, root, comment_id):
    claims = claims_manager(browsing)
    if claims:
        for rid in dict.fromkeys(str(v) for v in (bvid, oid) if v not in (None, "")):
            if claims.has_inbox_priority(
                account_id=str(row["account_id"]),
                character_id=str(row["character_id"]),
                resource_type="video",
                resource_id=rid,
                root_id=str(root),
            ):
                return True
    return False


def enqueue_comment_visit(browsing, row, task_id, metadata, items, *, interactive=True, staged=False):
    bvid, oid = metadata["bvid"], str(metadata["aid"])
    observe_thread(
        browsing.store,
        row,
        bvid,
        items,
        via="comment_visit",
        resource_author_uid=metadata.get("owner_uid"),
    )
    if not interactive:
        return []
    scope = [row["account_id"], row["character_id"], row["config"].get("review_session_id"), oid]
    groups = items if staged else merge_continuous_comments(items)
    live = not (row["preview"] or scope[2])
    capacity = browsing.backlog_status(persist=False) if live else None
    free = capacity["high_heads"] - capacity["total_heads"] if capacity else len(groups)
    remainder = []
    by_id = {str(x.get("comment_id")): x for x in items if x.get("comment_id")}
    own_uid = str(row["account_id"] or "")
    for item in groups:
        source = comment_source(item)
        if not source["author_uid"] or source["author_uid"] == own_uid:
            continue
        root = str(item.get("thread_id") or source["comment_id"])
        if _inbox_owns_comment(browsing, row, oid=oid, bvid=bvid, root=root, comment_id=source["comment_id"]):
            continue
        parent_row = by_id.get(str(item.get("parent_comment_id") or ""))
        parent_uid = str(comment_source(parent_row)["author_uid"]) if parent_row else ""
        is_direct = bool(own_uid and parent_uid == own_uid and item.get("parent_verified") is not False)
        combined_text = "\n".join(
            filter(None, [source["text"], *(comment_source(g)["text"] for g in item.get("grouped_comments", []))])
        )
        low_context_reason = low_context_skip_reason(
            combined_text,
            is_direct_to_self=is_direct,
            mentions=item.get("mentions") or (),
            attachments=item.get("attachments") or (),
            self_uid=own_uid,
        )
        identity = "visit:" + content_hash([*scope, source["comment_id"]])
        existing = browsing.store.one("SELECT status FROM browsing_events WHERE id=?", (identity,))
        if low_context_reason:
            if not existing:
                timestamp = _extract_timestamp(item) or 0
                skipped_event = {
                    "event_id": identity,
                    "kind": "comment_visit",
                    "account_id": row["account_id"],
                    "character_id": row["character_id"],
                    "review_session_id": scope[2],
                    "resource_type": "video",
                    "type": 1,
                    "oid": oid,
                    "bvid": bvid,
                    "root": root,
                    "parent": source["comment_id"],
                    "comment_id": source["comment_id"],
                    "sender_uid": source["author_uid"],
                    "sender_name": source["author_name"],
                    "source_text": source["text"],
                }
                browsing.store.db.execute(
                    "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        row["account_id"],
                        "comment_visit",
                        source["comment_id"],
                        timestamp,
                        "skipped",
                        dump(skipped_event),
                        low_context_reason,
                        time.time(),
                    ),
                )
            continue
        heads = (
            browsing.store.all(
                "SELECT id,source_key,timestamp,error,created_at FROM browsing_events WHERE account_id=? AND kind='comment_visit' "
                f"AND status IN ({PENDING_VISITS}) AND json_extract(payload,'$.character_id')=? "
                "AND json_extract(payload,'$.review_session_id') IS ? AND CAST(json_extract(payload,'$.oid') AS TEXT)=? "
                "AND CAST(json_extract(payload,'$.root') AS TEXT)=? ORDER BY timestamp DESC,created_at DESC,id DESC",
                (*scope, root),
            )
            if not existing and live
            else []
        )
        timestamp = _extract_timestamp(item) or 0
        newer = not heads or (timestamp, source["comment_id"]) > (heads[0]["timestamp"], heads[0]["source_key"])
        if not existing and live and not heads and free <= 0:
            remainder.append(item)
            continue
        event = {
            "event_id": identity,
            "kind": "comment_visit",
            "account_id": row["account_id"],
            "character_id": row["character_id"],
            "review_session_id": scope[2],
            "resource_type": "video",
            "type": 1,
            "oid": oid,
            "bvid": bvid,
            "root": root,
            "parent": source["comment_id"],
            "comment_id": source["comment_id"],
            "sender_uid": source["author_uid"],
            "sender_name": source["author_name"],
            "source_text": source["text"],
            "resource_author_uid": metadata.get("owner_uid"),
            "visit_task_id": task_id,
            "grouped_ids": [str(x["comment_id"]) for x in item.get("grouped_comments", [])],
            "items": [x for x in items if str(x.get("thread_id") or x["comment_id"]) == root],
        }
        if not existing:
            state = "received" if newer else "revisit_merged"
            if live and timestamp > 0 and timestamp < time.time() - 72 * 3600:
                state = "archived" if timestamp <= time.time() - 7 * 86400 else "revisit_parked"
            if state == "received" and not heads:
                free -= 1
            if state == "received" and heads:
                browsing.store.db.executemany(
                    "UPDATE browsing_events SET status='revisit_merged' WHERE id=?",
                    [(head["id"],) for head in heads if head["id"] not in browsing.event_active],
                )
            browsing.store.db.execute(
                "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    row["account_id"],
                    event["kind"],
                    source["comment_id"],
                    timestamp,
                    state,
                    dump(event),
                    heads[0]["error"] if heads and state == "received" else None,
                    min((head["created_at"] for head in heads), default=time.time()),
                ),
            )
        if browsing.store.one(
            "SELECT 1 FROM browsing_events WHERE account_id=? AND id<>? "
            "AND (status IN ('drafted','operator_replied') OR error='同一公开评论已处理') "
            "AND json_extract(payload,'$.resource_type')='video' "
            "AND CAST(json_extract(payload,'$.oid') AS TEXT)=? "
            "AND CAST(json_extract(payload,'$.comment_id') AS TEXT)=? "
            "AND CAST(json_extract(payload,'$.sender_uid') AS TEXT)=? "
            "AND (kind<>'comment_visit' OR json_extract(payload,'$.character_id')=?) LIMIT 1",
            (row["account_id"], identity, oid, source["comment_id"], source["author_uid"], row["character_id"]),
        ):
            browsing.store.db.execute(
                "UPDATE browsing_events SET status='skipped',error='同一公开评论已处理' WHERE id=? AND status='received'",
                (identity,),
            )
        if batch_id := row["config"].get("batch_id"):
            browsing.store.db.execute(
                "INSERT OR IGNORE INTO browsing_batch_comment_events VALUES(?,?)", (batch_id, identity)
            )
    return remainder


def admit_comment_page(browsing, row, task_id, metadata, items, *, cursor=None, page_key=None):
    remainder = enqueue_comment_visit(browsing, row, task_id, metadata, items)
    if remainder:
        browsing.store.db.execute(
            "INSERT INTO browsing_comment_page_handoffs VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,character_id,review_session,bvid,page_key) DO NOTHING",
            (
                row["account_id"],
                row["character_id"],
                row["config"].get("review_session_id", ""),
                metadata["bvid"],
                page_key
                or (f"old:{cursor['page']}" if cursor else "batch:" + str(row["config"].get("batch_id") or row["id"])),
                dump(
                    {
                        "metadata": metadata,
                        "items": remainder,
                        "cursor": cursor,
                        "task_id": task_id,
                        "batch_id": row["config"].get("batch_id"),
                    }
                ),
                time.time(),
            ),
        )
    return not remainder


def _advance_old_page(browsing, row, bvid, page, next_page):
    browsing.store.db.execute(
        "UPDATE browsing_comment_revisits SET page=?,next_at=?,error=NULL WHERE account_id=? "
        "AND character_id=? AND review_session=? AND bvid=? AND page=?",
        (
            next_page,
            time.time() + row["config"]["notification_interval_seconds"],
            row["account_id"],
            row["character_id"],
            row["config"].get("review_session_id", ""),
            bvid,
            page,
        ),
    )
    cursor = comment_cursor_key(row, bvid)
    if next_page is not None and browsing.store.get(cursor, 1) <= page:
        browsing.store.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (cursor, dump(next_page)))


def resume_comment_page(browsing, row):
    state = browsing.backlog_status(persist=False)
    if state["comment_hourly_resume_at"] or state["total_heads"] >= state["high_heads"]:
        return False
    record = browsing.store.one(
        "SELECT rowid,document FROM browsing_comment_page_handoffs WHERE account_id=? AND character_id=? "
        "AND review_session=? ORDER BY created_at,rowid LIMIT 1",
        (row["account_id"], row["character_id"], row["config"].get("review_session_id", "")),
    )
    if not record:
        return False
    document = json.loads(record["document"])
    config = row["config"] | {"batch_id": document["batch_id"]}
    with browsing.store.db:
        remainder = enqueue_comment_visit(
            browsing,
            row | {"config": config},
            document["task_id"],
            document["metadata"],
            document["items"],
            staged=True,
        )
        if remainder:
            browsing.store.db.execute(
                "UPDATE browsing_comment_page_handoffs SET document=? WHERE rowid=?",
                (dump(document | {"items": remainder}), record["rowid"]),
            )
        else:
            if cursor := document["cursor"]:
                _advance_old_page(browsing, row, document["metadata"]["bvid"], cursor["page"], cursor["next_page"])
            browsing.store.db.execute("DELETE FROM browsing_comment_page_handoffs WHERE rowid=?", (record["rowid"],))
    return True


async def read_old_comment_page(browsing, row):
    scope = (
        row["account_id"],
        row["character_id"],
        row["character_version"],
        row["config"].get("review_session_id", ""),
    )
    visit = browsing.store.one(
        "SELECT bvid,round_id,page,next_at,error FROM browsing_comment_revisits "
        "WHERE account_id=? AND character_id=? AND character_version=? AND review_session=? "
        "AND page IS NOT NULL AND next_at<=? ORDER BY next_at,bvid LIMIT 1",
        (*scope, time.time()),
    )
    if not visit or not browsing.page_capacity():
        return False
    bvid, page = visit["bvid"], visit["page"]
    browsing.check_step(row)
    if "start_page" not in inspect.signature(browsing.adapter.fetch).parameters:
        return False
    try:
        context = await browsing._read_proactively(browsing._read, browsing.adapter.fetch, bvid, 1, start_page=page)
        metadata = {"bvid": bvid, "aid": context.get("aid"), "owner_uid": context.get("owner_uid")}
        if not metadata["aid"]:
            materials = browsing.repo.watch_material(row, bvid)
            metadata = (materials or {}).get("metadata") or await browsing._read(
                browsing.adapter.video_metadata, bvid
            )
        if metadata.get("bvid") != bvid or context.get("video_id", bvid) != bvid:
            raise ValueError("评论页与当前视频不一致")
        next_page = context.get("next_page")
        if next_page is not None and (type(next_page) is not int or next_page <= page):
            raise ValueError("评论分页没有前进")
        with browsing.store.db:
            if admit_comment_page(
                browsing,
                row,
                None,
                metadata,
                context.get("items", []),
                cursor={"page": page, "next_page": next_page},
            ):
                _advance_old_page(browsing, row, bvid, page, next_page)
    except (ValueError, TimeoutError) as error:
        browsing.store.execute(
            "UPDATE browsing_comment_revisits SET next_at=?,error=? WHERE account_id=? "
            "AND character_id=? AND character_version=? AND review_session=? AND bvid=?",
            (
                getattr(error, "next_at", time.time() + row["config"]["notification_interval_seconds"]),
                public_error(error),
                *scope,
                bvid,
            ),
        )
    return True
