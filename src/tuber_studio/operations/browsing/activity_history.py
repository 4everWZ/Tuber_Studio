"""Read-only daily browsing outcomes, scoped to an account and its OC."""

import datetime as dt
import json
import math
import time
from zoneinfo import ZoneInfo

from .quotas import publication_time

_AUTO = "r.preview=0 AND (json_extract(r.config,'$.discovery_publish')=1 OR json_extract(r.config,'$.autonomous_batch')=1)"
_TASK_RESULT = "(t.status NOT IN ('discovered','claimed','preparing','cancelled','queued','paused') OR t.material_key IS NOT NULL)"
_SUCCESS = {"selected", "viewed_without_comment", "comments_checked"}
_REASONS = {
    "no_interest": "没有想参与这个话题",
    "nothing_to_add": "暂时没有想补充的话",
    "insufficient_evidence": "想回应的内容还缺少材料",
    "visual_required": "想回应的细节需要补看画面",
}


def _json(value, fallback=None):
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _day_window(day, timezone):
    local = dt.date.fromisoformat(day)
    if local.isoformat() != day:
        raise ValueError("日期须为 YYYY-MM-DD")
    start = dt.datetime.combine(local, dt.time(), tzinfo=timezone)
    end = dt.datetime.combine(local + dt.timedelta(days=1), dt.time(), tzinfo=timezone)
    return start.timestamp(), end.timestamp()


def _video_data(browsing, *, day=None, visible_since=None):
    """Return one row per video, with successful replies counted only once.

    Viewing starts come from the viewing ledger; a comment revisit contributes
    to replies and activity, but does not increment today's watched count.
    Archiving hides old events without changing their sources or deduplication.
    """
    store = browsing.store
    timezone_name = browsing.policy().get("business_timezone", "Asia/Taipei")
    timezone = ZoneInfo(timezone_name)
    today = dt.datetime.fromtimestamp(time.time(), timezone).date().isoformat()
    day = day or today
    start, end = _day_window(day, timezone)
    cutoff = float(store.get("browsing_history_visible_since", 0) if visible_since is None else visible_since)
    if not math.isfinite(cutoff) or cutoff < 0:
        raise ValueError("历史展示起点无效")
    account, character = browsing.discovery.history_scope()[:2]
    scope = (str(account or ""), character)
    # Only narrow public-result fields are read; original material and receipts
    # remain in their existing tables and are linked by task/round/intent IDs.
    tasks = store.all(
        "SELECT t.id,t.bvid,t.status,t.material_key,t.error,t.created_at,t.updated_at,r.id AS round_id,"
        "json_extract(t.payload,'$.task_kind') AS task_kind,"
        "EXISTS(SELECT 1 FROM browsing_watch_starts w WHERE w.account_id=r.account_id "
        "AND w.character_id=r.character_id AND w.task_id=t.id) AS metered_watch,"
        "COALESCE(json_extract(t.payload,'$.metadata.title'),json_extract(t.payload,'$.title')) AS title,"
        "COALESCE(json_extract(t.payload,'$.explanation'),json_extract(t.payload,'$.response_decision.explanation'),"
        "json_extract(t.payload,'$.decisions[0].explanation'),"
        "json_extract(t.payload,'$.selection.explanation')) AS explanation,"
        "json_extract(t.payload,'$.reason') AS decision_reason,json_extract(t.payload,'$.sources') AS sources "
        "FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id "
        "WHERE r.account_id=? AND r.character_id=? AND "
        + _AUTO
        + " AND "
        + _TASK_RESULT
        + " AND t.updated_at>=?",
        (*scope, cutoff),
    )
    selections = store.all(
        "SELECT i.bvid,i.kind,i.status,i.payload,b.round_id,"
        "COALESCE(json_extract(i.payload,'$.selection.decided_at'),b.created_at) AS decided_at "
        "FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id "
        "JOIN browsing_rounds r ON r.id=b.round_id WHERE r.account_id=? AND r.character_id=? AND " + _AUTO,
        scope,
    )
    starts = store.all(
        "SELECT bvid,started_at FROM browsing_watch_starts WHERE account_id=? AND character_id=? AND started_at>=?",
        (*scope, cutoff),
    )
    intents = store.all(
        "SELECT i.id,i.kind,i.status,i.text,i.error,i.receipt,i.reason,i.target,i.updated_at,i.round_id,"
        "CASE WHEN " + _AUTO + " THEN 1 ELSE 0 END AS automatic_video "
        "FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id "
        "WHERE i.account_id=? AND i.character_id=? AND i.kind IN ('top_level','reply_public')",
        scope,
    )
    failures = store.all(
        "SELECT f.bvid,f.error,f.count,f.updated_at,b.round_id FROM browsing_batch_failures f "
        "JOIN browsing_batches b ON b.id=f.batch_id JOIN browsing_rounds r ON r.id=b.round_id "
        "WHERE r.account_id=? AND r.character_id=? AND " + _AUTO + " AND f.updated_at>=?",
        (*scope, cutoff),
    )
    entries, days = {}, {today}

    def timestamp(value):
        return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= cutoff else None

    def include(value):
        value = timestamp(value)
        if value is None:
            return False
        days.add(dt.datetime.fromtimestamp(value, timezone).date().isoformat())
        return start <= value < end

    def video(bvid, value, round_id=None):
        if not bvid:
            return None
        if bvid not in entries:
            entries[bvid] = {
                "id": f"video/{bvid}",
                "bvid": bvid,
                "title": bvid,
                "status": "selected",
                "read_state": "new",
                "reason": "",
                "updated_at": value,
                "round_id": round_id,
                "main_replies": 0,
                "comment_replies": 0,
                "reply_count": 0,
                "replies": [],
                "error": None,
                "occurrences": 0,
                "watched": False,
                "fully_read": False,
                "sources": [],
                "_result_at": 0,
                "_result_status": None,
            }
        result = entries[bvid]
        if value >= result["updated_at"]:
            result.update(updated_at=value, round_id=round_id or result["round_id"])
        return result

    for item in selections:
        if not include(item["decided_at"]):
            continue
        payload = _json(item["payload"], {})
        choice = payload.get("selection") or {}
        # An unprocessed queue entry is not a model judgment.
        if not choice:
            continue
        result = video(item["bvid"], item["decided_at"], item["round_id"])
        if result is None:
            continue
        result.update(title=payload.get("title") or result["title"], sources=payload.get("sources") or [])
        result["reason"] = choice.get("explanation") or result["reason"]
        result["status"] = {
            "skip": "skipped",
            "later": "later",
            "comments": "comments_checked",
            "watch": "selected",
        }.get(choice.get("action"), "selected")
        result["_result_at"] = item["decided_at"]

    for item in tasks:
        if not include(item["updated_at"]):
            continue
        result = video(item["bvid"], item["updated_at"], item["round_id"])
        if result is None:
            continue
        result["task_id"] = item["id"]
        result["title"] = item["title"] or result["title"]
        result["sources"] = _json(item["sources"], []) or result["sources"]
        if item["updated_at"] >= result["_result_at"]:
            result.update(
                _result_at=item["updated_at"],
                _result_status=item["status"],
                status=item["status"],
                error=item["error"],
            )
            result["reason"] = (
                item["explanation"]
                or _REASONS.get(item["decision_reason"])
                or (item["decision_reason"] if item["status"] in _SUCCESS else None)
                or result["reason"]
            )
        # Legacy data predates the viewing-start ledger. Repeated comment
        # checks and scheduler states must not turn into additional watches.
        if (
            not item["metered_watch"]
            and item["task_kind"] != "comment_visit"
            and item["status"] in {"selected", "viewed_without_comment"}
            and item["material_key"]
        ):
            result["watched"] = True

    for item in starts:
        if include(item["started_at"]):
            result = video(item["bvid"], item["started_at"])
            if result is not None:
                result["watched"] = True

    for item in intents:
        if not item["automatic_video"]:
            continue
        at = (
            publication_time(item["receipt"], item["updated_at"])
            if item["status"] == "published"
            else item["updated_at"]
        )
        if not include(at):
            continue
        target = _json(item["target"], {})
        result = video(target.get("bvid"), at, item["round_id"])
        if result is None:
            continue
        reply = {key: item[key] for key in ("id", "kind", "status", "text", "error")}
        reply.update(
            recipient_name=target.get("recipient_name") or "",
            receipt=_json(item["receipt"], item["receipt"]),
            updated_at=at,
        )
        result["replies"].append(reply)
        if item["status"] == "published":
            result["main_replies" if item["kind"] == "top_level" else "comment_replies"] += 1
            result["reply_count"] += 1
            result["status"] = "published"
        elif item["status"] == "publish_uncertain":
            result["status"] = "publish_uncertain"

    for item in failures:
        if include(item["updated_at"]):
            result = video(item["bvid"], item["updated_at"], item["round_id"])
            if result is not None:
                result["occurrences"] += item["count"]
                if result["_result_status"] not in _SUCCESS and not result["reply_count"]:
                    result.update(error=item["error"], status="failed")

    for bvid, result in entries.items():
        progress = store.one(
            "SELECT read_state,main_decision,json_extract(material,'$.metadata.title') AS title FROM browsing_video_progress WHERE account_id=? AND character_id=? AND bvid=?",
            (*scope, bvid),
        )
        if progress:
            result["read_state"] = progress["read_state"]
            result["fully_read"] = progress["read_state"] == "complete"
            result["title"] = progress["title"] or result["title"]
            decision = _json(progress["main_decision"], {})
            # The video row describes the viewing decision. A retained draft's
            # body belongs to its comment entry, and must not reappear here
            # after its original activity has been archived.
            result["main_decision"] = {
                key: decision[key]
                for key in (
                    "action",
                    "reason",
                    "explanation",
                    "interest",
                    "video_preference",
                    "preference_reason",
                    "coverage_required",
                )
                if key in decision
            }
            if result["watched"] and decision.get("explanation") and not result["reason"]:
                result["reason"] = decision["explanation"]
            if (
                decision.get("action") == "skip"
                and result["watched"]
                and result["status"] in {"selected", "viewed_without_comment", "comments_checked"}
                and not result["reply_count"]
            ):
                result["status"] = "skipped"
        elif result["watched"]:
            result["read_state"] = "viewed"
        result["replies"].sort(key=lambda reply: (reply["updated_at"], reply["id"]))
        del result["_result_at"], result["_result_status"]

    items = sorted(entries.values(), key=lambda item: (-item["updated_at"], item["bvid"]))
    counts = {
        "videos": len(items),
        "watched": sum(item["watched"] for item in items),
        "fully_read": sum(item["watched"] and item["fully_read"] for item in items),
        "partial": sum(item["watched"] and not item["fully_read"] for item in items),
        "replied": sum(item["reply_count"] > 0 for item in items),
        "skipped": sum(item["status"] == "skipped" for item in items),
        "replies": sum(item["reply_count"] for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
    }
    return {
        "day": day,
        "timezone": timezone_name,
        "days": sorted(days, reverse=True),
        "counts": counts,
        "summary": {key: counts[key] for key in ("watched", "replied", "skipped", "replies")},
        "items": items,
        "_intents": intents,
    }


def _validate_page(page, page_size):
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("页码须大于 0，每页条数须为 1 到 100")


def _paginate(result, page, page_size):
    items = result["items"]
    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    offset = (page - 1) * page_size
    return {key: value for key, value in result.items() if not key.startswith("_")} | {
        "items": items[offset : offset + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


def history(browsing, *, day=None, page=1, page_size=10, visible_since=None):
    """Compatibility view of the shared video aggregation."""
    _validate_page(page, page_size)
    return _paginate(_video_data(browsing, day=day, visible_since=visible_since), page, page_size)


def activity(browsing, *, day=None, kind="all", page=1, page_size=10, visible_since=None):
    """Operator-only daily outcomes across public and private channels.

    Message bodies are read only for this view. They never become video source
    material or enter a public generation context.
    """
    _validate_page(page, page_size)
    kinds = {
        "all": None,
        "videos": "video",
        "comments": "comment",
        "messages": "message",
        "dynamics": "dynamic",
    }
    if kind not in kinds:
        raise ValueError("活动类型须为 all、videos、comments、messages 或 dynamics")
    base = _video_data(browsing, day=day, visible_since=visible_since)
    store = browsing.store
    timezone = ZoneInfo(base["timezone"])
    start, end = _day_window(base["day"], timezone)
    cutoff = float(store.get("browsing_history_visible_since", 0) if visible_since is None else visible_since)
    account, character = browsing.discovery.history_scope()[:2]
    account = str(account or "")
    tables = {
        item["name"] for item in store.all("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    days = set(base["days"])
    videos = base["items"]
    by_video = {item["bvid"]: item for item in videos}
    published_main_videos = {
        (_json(row["target"], {}) or {}).get("bvid")
        for row in base["_intents"]
        if row["kind"] == "top_level" and row["status"] == "published"
    }
    items = []
    for video in videos:
        main = video.get("main_decision") or {}
        items.append(
            video
            | {
                "kind": "video",
                "text": "",
                "interest": main.get("video_preference") or main.get("interest"),
                "video_preference": main.get("video_preference"),
                "preference_reason": main.get("preference_reason") or "",
                "has_main_reply": video["bvid"] in published_main_videos,
                "has_main_reply_today": bool(video["main_replies"]),
                "href": f"https://www.bilibili.com/video/{video['bvid']}",
            }
        )

    def include(at):
        if not isinstance(at, (int, float)) or not math.isfinite(at) or at < cutoff:
            return False
        days.add(dt.datetime.fromtimestamp(at, timezone).date().isoformat())
        return start <= at < end

    def add(row, *, channel, identity, text, title, href, at=None, reason="", **details):
        at = row["updated_at"] if at is None else at
        if include(at):
            items.append(
                {
                    "id": identity,
                    "kind": channel,
                    "title": title,
                    "text": text or "",
                    "status": row["status"],
                    "reason": reason or "",
                    "updated_at": at,
                    "error": row.get("error"),
                    "href": href,
                    "receipt": _json(row.get("receipt"), row.get("receipt")),
                    **details,
                }
            )

    comment_reasons = (
        {
            (row["bvid"], row["comment_id"]): row["explanation"]
            for row in store.all(
                "SELECT bvid,comment_id,json_extract(decision,'$.explanation') AS explanation "
                "FROM browsing_comment_progress WHERE account_id=? AND character_id=?",
                (account, character),
            )
        }
        if "browsing_comment_progress" in tables
        else {}
    )
    # Reuse the public intents already loaded by the video aggregation, including
    # normal operating rounds and manually reviewed preview results.
    for row in base["_intents"]:
        target = _json(row["target"], {})
        bvid = target.get("bvid")
        comment_kind = "top_level" if row["kind"] == "top_level" else "reply"
        recipient = target.get("recipient_name") or ""
        reason = ""
        if row["kind"] == "top_level" and bvid in by_video:
            reason = (by_video[bvid].get("main_decision") or {}).get("explanation") or by_video[bvid][
                "reason"
            ]
        else:
            reason = comment_reasons.get((bvid, str(target.get("parent") or ""))) or ""
        at = (
            publication_time(row["receipt"], row["updated_at"])
            if row["status"] == "published"
            else row["updated_at"]
        )
        add(
            row,
            channel="comment",
            identity=f"comment/browsing/{row['id']}",
            text=row["text"],
            title="视频主评论" if comment_kind == "top_level" else f"回复 {recipient or '评论'}",
            href=f"#comments/browsing/{row['id']}",
            at=at,
            reason=reason,
            bvid=bvid,
            comment_kind=comment_kind,
            reply_kind=comment_kind,
            recipient_name=recipient,
            round_id=row["round_id"],
            source_text=target.get("source_text") or "",
        )

    if account and "drafts" in tables:
        for row in store.all(
            "SELECT d.id,d.status,d.text,d.error,d.receipt,d.updated_at,d.answer_check,c.video_id,c.comment_id,c.thread_id,c.parent_comment_id,c.display_name,c.content AS source_text FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE d.account_id=? AND d.character_id=?",
            (account, character),
        ):
            handoff = row["status"] == "private_handoff"
            check = _json(row["answer_check"], {})
            at = (
                publication_time(row["receipt"], row["updated_at"])
                if row["status"] == "published"
                else row["updated_at"]
            )
            add(
                row,
                channel="comment",
                identity=f"comment/manual/{row['id']}",
                text="" if handoff else row["text"],
                title=f"回复 {row['display_name'] or '评论'}",
                href=f"#comments/comments/{row['id']}",
                at=at,
                reason="" if handoff else check.get("explanation") or "",
                bvid=None if handoff else row["video_id"],
                comment_kind="reply",
                reply_kind="reply",
                recipient_name="" if handoff else row["display_name"] or "",
                source_text="" if handoff else row["source_text"],
            )

    if account and "dm_inbox" in tables:
        for row in store.all(
            "SELECT id,display_name,platform_user_id,content,sent_at,received_at FROM dm_inbox WHERE account_id=? AND character_id=?",
            (account, character),
        ):
            row = row | {"status": "received", "updated_at": row["sent_at"] or row["received_at"]}
            add(
                row,
                channel="message",
                identity=f"message/inbox/{row['id']}",
                text=row["content"],
                title=f"收到 {row['display_name'] or row['platform_user_id']} 的私信",
                href="#messages",
                direction="incoming",
                recipient_name=row["display_name"] or row["platform_user_id"],
            )
    if account and "dm_drafts" in tables:
        for row in store.all(
            "SELECT * FROM dm_drafts WHERE account_id=? AND character_id=?", (account, character)
        ):
            if not row.get("text") and row.get("message_kind") != "image":
                continue
            at = (
                publication_time(row["receipt"], row["updated_at"])
                if row["status"] == "published"
                else row["updated_at"]
            )
            add(
                row,
                channel="message",
                identity=f"message/draft/{row['id']}",
                text=row["text"],
                title=f"私信回复 {row['display_name'] or row['platform_user_id']}",
                href="#messages",
                at=at,
                reason=row.get("reason") or "",
                direction="outgoing",
                recipient_name=row["display_name"] or row["platform_user_id"],
                message_kind=row.get("message_kind", "text"),
                meme_id=row.get("meme_id"),
            )

    if account and "dynamic_posts" in tables:
        columns = {item[1] for item in store.db.execute("PRAGMA table_info(dynamic_posts)")}
        owned = set()
        if "operation_write_bindings" in tables and "account_writes" in tables:
            owned.update(
                str(item["post_id"])
                for item in store.all(
                    "SELECT json_extract(b.binding_json,'$.target.post_id') AS post_id FROM operation_write_bindings b JOIN account_writes w ON w.token=b.root_token WHERE w.account_id=? AND w.character_id=? AND w.channel='dynamics'",
                    (account, character),
                )
                if item["post_id"] is not None
            )
        if "operation_continuations" in tables:
            owned.update(
                str(item["entity_id"])
                for item in store.all(
                    "SELECT entity_id FROM operation_continuations WHERE channel='dynamics' AND json_extract(binding,'$.account_id')=? AND json_extract(binding,'$.character.id')=?",
                    (account, character),
                )
            )
        for row in store.all("SELECT * FROM dynamic_posts WHERE character_id=?", (character,)):
            owner = row.get("account_id") if "account_id" in columns else None
            if owner and str(owner) != account or not owner and str(row["id"]) not in owned:
                continue
            at = (
                (row.get("published_at") or publication_time(row.get("receipt"), row["updated_at"]))
                if row["status"] == "published"
                else row["updated_at"]
            )
            add(
                row,
                channel="dynamic",
                identity=f"dynamic/{row['id']}",
                text=row["content"],
                title="发布动态" if row["status"] == "published" else "动态草稿",
                href="#dynamics",
                at=at,
                reason=row.get("reason") or "",
            )

    items.sort(key=lambda item: (-item["updated_at"], item["id"]))
    counts = {
        "videos": len(videos),
        "comments": sum(item["kind"] == "comment" for item in items),
        "messages": sum(item["kind"] == "message" for item in items),
        "dynamics": sum(item["kind"] == "dynamic" for item in items),
    }
    counts |= {
        "total": len(items),
        "sent": sum(item["kind"] != "video" and item["status"] == "published" for item in items),
        "pending": sum(
            item["kind"] != "video"
            and item["status"]
            in {"pending_review", "approved", "queued", "generating", "publishing", "automatic_pending"}
            for item in items
        ),
        "failed": sum(item["status"] == "failed" for item in items),
        "replies": sum(item["kind"] == "comment" and item["status"] == "published" for item in items),
        **{key: value for key, value in base["counts"].items() if key not in {"videos", "failed", "replies"}},
    }
    filtered = items if kind == "all" else [item for item in items if item["kind"] == kinds[kind]]
    return _paginate(
        {
            "day": base["day"],
            "timezone": base["timezone"],
            "days": sorted(days, reverse=True),
            "kind": kind,
            "counts": counts,
            "items": filtered,
        },
        page,
        page_size,
    )
