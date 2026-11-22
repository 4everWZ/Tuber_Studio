"""Read-only unified activity projection across pipelines and issue log management."""

import datetime as dt
import json
import math
import time
from zoneinfo import ZoneInfo

from .quotas import publication_time
from .repository import resolve_history_scope

_AUTO = "r.preview=0 AND (json_extract(r.config,'$.discovery_publish')=1 OR json_extract(r.config,'$.autonomous_batch')=1)"
_PRIORITY = "json_extract(r.config,'$.full_video')=1"
_QUEUED_REPLY = (
    "r.preview=0 AND EXISTS(SELECT 1 FROM browsing_tasks q WHERE q.round_id=i.round_id "
    "AND q.bvid=json_extract(i.target,'$.bvid') AND json_type(q.payload,'$.comment_batch_ids')='array')"
)
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


def _confirmed_comment_id(status, receipt, comment_id_pattern):
    if status != "published" or not isinstance(receipt, dict) or receipt.get("code", 0) != 0:
        return None
    for field in ("rpid", "comment_id"):
        identifier = str(receipt.get(field))
        if comment_id_pattern.fullmatch(identifier):
            return identifier
    return None


def _history_scope(browsing):
    if hasattr(browsing, "history_scope") and callable(browsing.history_scope):
        return browsing.history_scope()
    discovery = getattr(browsing, "discovery", None)
    if discovery is not None and callable(getattr(discovery, "history_scope", None)):
        return discovery.history_scope()
    runtime = getattr(browsing, "runtime", None)
    character = getattr(runtime, "character", None) or browsing.store.character()
    return resolve_history_scope(browsing.store, getattr(browsing, "bilibili_account", None), character)


def _round_pipeline(store, round_id, cache):
    if not round_id:
        return "discovery"
    if round_id in cache:
        return cache[round_id]
    row = store.one("SELECT preview,config,source_pipeline FROM browsing_rounds WHERE id=?", (round_id,))
    if not row:
        cache[round_id] = "discovery"
        return "discovery"
    pipeline = row.get("source_pipeline")
    if pipeline not in {"discovery", "inbox", "manual"}:
        config = _json(row.get("config"), {}) or {}
        if config.get("interaction_only"):
            pipeline = "inbox"
        elif row.get("preview") and not config.get("autonomous_batch"):
            pipeline = "manual"
        else:
            pipeline = "discovery"
    cache[round_id] = pipeline
    return pipeline


def _video_data(browsing, *, day=None, visible_since=None, round_id=None):
    store = browsing.store
    timezone_name = browsing.policy().get("business_timezone", "Asia/Taipei")
    timezone = ZoneInfo(timezone_name)
    today = dt.datetime.fromtimestamp(time.time(), timezone).date().isoformat()
    day = day or today
    start, end = _day_window(day, timezone)
    cutoff = float(store.get("browsing_history_visible_since", 0) if visible_since is None else visible_since)
    if not math.isfinite(cutoff) or cutoff < 0:
        raise ValueError("历史展示起点无效")
    account, character = _history_scope(browsing)[:2]
    scope = (str(account or ""), character)
    tasks = store.all(
        "SELECT t.id,t.bvid,t.status,t.material_key,t.error,t.created_at,t.updated_at,r.id AS round_id,"
        "r.source_pipeline AS round_pipeline,r.preview AS round_preview,"
        "json_extract(t.payload,'$.task_kind') AS task_kind,"
        "json_extract(t.payload,'$.phase') AS phase,json_extract(t.payload,'$.full_coverage.viewing') AS viewing,"
        "EXISTS(SELECT 1 FROM browsing_watch_starts w WHERE w.account_id=r.account_id "
        "AND w.character_id=r.character_id AND w.task_id=t.id) AS metered_watch,"
        "COALESCE(json_extract(t.payload,'$.metadata.title'),json_extract(t.payload,'$.full_coverage.title'),"
        "json_extract(t.payload,'$.title')) AS title,"
        "COALESCE(json_extract(t.payload,'$.explanation'),json_extract(t.payload,'$.response_decision.explanation'),"
        "json_extract(t.payload,'$.decisions[0].explanation'),"
        "json_extract(t.payload,'$.selection.explanation')) AS explanation,"
        "json_extract(t.payload,'$.reason') AS decision_reason,json_extract(t.payload,'$.sources') AS sources "
        "FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id "
        "WHERE r.account_id=? AND r.character_id=? AND "
        + "(" + _AUTO + " OR " + _PRIORITY + " OR (r.preview=0 AND json_type(t.payload,'$.comment_batch_ids')='array'))"
        + " AND "
        + _TASK_RESULT
        + " AND t.updated_at>=? AND (? IS NULL OR r.id=?)",
        (*scope, cutoff, round_id, round_id),
    )
    selections = store.all(
        "SELECT i.bvid,i.kind,i.status,i.payload,b.round_id,r.source_pipeline AS round_pipeline,"
        "COALESCE(json_extract(i.payload,'$.selection.decided_at'),b.created_at) AS decided_at "
        "FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id "
        "JOIN browsing_rounds r ON r.id=b.round_id WHERE r.account_id=? AND r.character_id=? AND "
        + _AUTO
        + " AND (? IS NULL OR r.id=?)",
        (*scope, round_id, round_id),
    )
    starts = store.all(
        "SELECT w.bvid,w.started_at,t.round_id FROM browsing_watch_starts w LEFT JOIN browsing_tasks t ON t.id=w.task_id "
        "WHERE w.account_id=? AND w.character_id=? AND w.started_at>=? AND (? IS NULL OR t.round_id=?)",
        (*scope, cutoff, round_id, round_id),
    )
    intents = store.all(
        "SELECT i.id,i.kind,i.status,i.text,i.error,i.receipt,i.reason,i.target,i.updated_at,i.round_id,"
        "COALESCE(i.source_pipeline, r.source_pipeline, CASE WHEN json_extract(r.config,'$.interaction_only')=1 "
        "THEN 'inbox' WHEN r.preview=1 THEN 'manual' ELSE 'discovery' END) AS item_pipeline,"
        "CASE WHEN (" + _AUTO + " OR (" + _QUEUED_REPLY + ")) THEN 1 ELSE 0 END AS automatic_video "
        "FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id "
        "WHERE i.account_id=? AND i.character_id=? AND i.kind IN ('top_level','reply_public') "
        "AND (? IS NULL OR r.id=?)",
        (*scope, round_id, round_id),
    )
    failures = store.all(
        "SELECT f.bvid,f.error,f.count,f.updated_at,b.round_id FROM browsing_batch_failures f "
        "JOIN browsing_batches b ON b.id=f.batch_id JOIN browsing_rounds r ON r.id=b.round_id "
        "WHERE r.account_id=? AND r.character_id=? AND " + _AUTO + " AND f.updated_at>=? AND (? IS NULL OR r.id=?)",
        (*scope, cutoff, round_id, round_id),
    )
    entries, days = {}, {today}
    round_cache = {}

    def timestamp(value):
        return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= cutoff else None

    def include(value):
        value = timestamp(value)
        if value is None:
            return False
        days.add(dt.datetime.fromtimestamp(value, timezone).date().isoformat())
        return round_id is not None or start <= value < end

    def video(bvid, value, item_round_id=None, pipeline=None):
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
                "round_id": item_round_id,
                "source_pipeline": pipeline or _round_pipeline(store, item_round_id, round_cache),
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
                "_comments_read": False,
            }
        result = entries[bvid]
        if value >= result["updated_at"]:
            result.update(updated_at=value, round_id=item_round_id or result["round_id"])
            if pipeline:
                result["source_pipeline"] = pipeline
        return result

    for item in selections:
        if not include(item["decided_at"]):
            continue
        payload = _json(item["payload"], {})
        choice = payload.get("selection") or {}
        if not choice:
            continue
        result = video(item["bvid"], item["decided_at"], item["round_id"], item.get("round_pipeline") or "discovery")
        if result is None:
            continue
        result.update(title=payload.get("title") or result["title"], sources=payload.get("sources") or [])
        result["reason"] = choice.get("explanation") or result["reason"]
        result["status"] = {
            "skip": "skipped",
            "later": "later",
            "comments": "comments_checked" if item["status"] == "completed" else item["status"],
            "watch": "selected",
        }.get(choice.get("action"), "selected")
        result["_result_at"] = item["decided_at"]

    for item in tasks:
        if not include(item["updated_at"]):
            continue
        item_pipeline = item.get("round_pipeline") or ("manual" if item.get("round_preview") else "discovery")
        result = video(item["bvid"], item["updated_at"], item["round_id"], item_pipeline)
        if result is None:
            continue
        result["task_id"] = item["id"]
        result["title"] = item["title"] or result["title"]
        result["sources"] = _json(item["sources"], []) or result["sources"]
        if item["task_kind"] == "comment_visit" and item["status"] in _SUCCESS:
            result["_comments_read"] = True
        if item["updated_at"] >= result["_result_at"]:
            result.update(
                _result_at=item["updated_at"],
                _result_status=item["status"],
                status=item["status"],
                error=item["error"],
            )
            if item["task_kind"] == "full_video":
                result.update(phase=item["phase"], viewing=item["viewing"])
            result["reason"] = (
                item["explanation"]
                or _REASONS.get(item["decision_reason"])
                or (item["decision_reason"] if item["status"] in _SUCCESS else None)
                or result["reason"]
            )
        if (
            not item["metered_watch"]
            and item["task_kind"] != "comment_visit"
            and item["status"] in {"selected", "viewed_without_comment"}
            and item["material_key"]
        ):
            result["watched"] = True

    for item in starts:
        if include(item["started_at"]):
            result = video(item["bvid"], item["started_at"], item["round_id"])
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
            result = video(item["bvid"], item["updated_at"], item["round_id"], "discovery")
            if result is not None:
                result["occurrences"] += item["count"]
                if result["_result_status"] not in _SUCCESS and not result["reply_count"]:
                    result.update(error=item["error"], status="failed")

    for bvid, result in entries.items():
        progress = store.one(
            "SELECT read_state,main_decision,json_extract(material,'$.metadata.title') AS title "
            "FROM browsing_video_progress WHERE account_id=? AND character_id=? AND bvid=?",
            (*scope, bvid),
        )
        if progress:
            result["read_state"] = progress["read_state"]
            result["fully_read"] = progress["read_state"] == "complete"
            result["title"] = progress["title"] or result["title"]
            decision = _json(progress["main_decision"], {})
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
            if result["watched"] and decision.get("explanation"):
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
        elif result["_comments_read"]:
            result["read_state"] = "comments_only"
        result["replies"].sort(key=lambda reply: (reply["updated_at"], reply["id"]))
        del result["_result_at"], result["_result_status"], result["_comments_read"]

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


def activity(
    browsing,
    *,
    day=None,
    kind="all",
    comment_filter="all",
    source="all",
    page=1,
    page_size=10,
    visible_since=None,
    round_id=None,
    paginate=True,
    channels=None,
):
    """Operator-only daily outcomes across public, inbox, manual, and private channels."""
    channels = channels if channels is not None else getattr(browsing, "activity_channels", None) or {}
    comment_id_pattern = channels["COMMENT_ID"]
    comment_activity = channels["comment_activity"]
    comment_href = channels["comment_href"]
    comment_thread = channels["comment_thread"]
    resource_key = channels["resource_key"]
    contact_state = channels["contact_state"]

    if paginate:
        _validate_page(page, page_size)
    kinds = {
        "all": None,
        "videos": "video",
        "comments": "comment",
        "messages": "message",
        "dynamics": "dynamic",
        "public": None,
    }
    if kind not in kinds:
        raise ValueError("活动类型须为 all、videos、comments、messages、dynamics 或 public")
    if comment_filter not in {"all", "read", "replied", "pending_review"}:
        raise ValueError("无效的评论活动筛选")
    if source not in {"all", "discovery", "inbox", "manual"}:
        raise ValueError("活动来源须为 all、discovery、inbox 或 manual")
    if round_id and day is None:
        account, character = _history_scope(browsing)[:2]
        recent = browsing.store.one(
            "SELECT updated_at FROM browsing_rounds WHERE id=? AND account_id=? AND character_id=?",
            (round_id, str(account or ""), character),
        )
        if recent:
            timezone = ZoneInfo(browsing.policy().get("business_timezone", "Asia/Taipei"))
            day = dt.datetime.fromtimestamp(recent["updated_at"], timezone).date().isoformat()
    base = _video_data(browsing, day=day, visible_since=visible_since, round_id=round_id)
    store = browsing.store
    timezone = ZoneInfo(base["timezone"])
    start, end = _day_window(base["day"], timezone)
    cutoff = float(store.get("browsing_history_visible_since", 0) if visible_since is None else visible_since)
    account, character = _history_scope(browsing)[:2]
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
    round_cache = {}
    items, threads, listed_threads = [], {}, set()
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
        return round_id is not None or start <= at < end

    def add(row, *, channel, identity, text, title, href, source_pipeline, at=None, reason="", **details):
        at = row["updated_at"] if at is None else at
        if include(at):
            items.append(
                {
                    "id": identity,
                    "kind": channel,
                    "source_pipeline": source_pipeline,
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

    def thread_details(resource, root, comment, row, target=None):
        receipt = _json(row.get("receipt"), {})
        status = row["status"]
        confirmed_id = _confirmed_comment_id(status, receipt, comment_id_pattern)
        if not resource or not root:
            if confirmed_id:
                source_href = comment_href(resource, comment) if comment_id_pattern.fullmatch(str(comment)) else None
                return {
                    "comment_outcome": "new_reply",
                    "sent_href": comment_href(resource, confirmed_id),
                    "source_comment_id": str(comment) if source_href else None,
                    "source_href": source_href,
                }
            return {}
        root = str(root)
        key = (resource, root)
        if key not in threads:
            threads[key] = comment_thread(store, resource, root, account_id=account, character_id=character)
        thread = threads[key]
        observed = (target or {}).get("account_reply") or {}
        matched = next(
            (
                item
                for item in reversed(thread["comments"])
                if item["parent_comment_id"] == str(comment)
                and item["publication"]
                and (item["created_at"] is None or item["created_at"] <= row["updated_at"])
            ),
            None,
        )
        if status == "publish_uncertain":
            outcome = "pending_verification"
        elif confirmed_id:
            outcome = "new_reply"
        elif status == "operator_replied" and observed.get("comment_id"):
            outcome = "same_account"
        elif matched:
            outcome = "previous_reply" if matched["publication"] == "confirmed" else "same_account"
        else:
            outcome = None
        replied_id = (
            None
            if status == "publish_uncertain"
            else (confirmed_id or observed.get("comment_id") or (matched or {}).get("comment_id"))
        )
        source_href = comment_href(resource, comment) if comment_id_pattern.fullmatch(str(comment)) else None
        return {
            "thread": {key: thread[key] for key in ("resource", "root_id", "root_author_uid", "resource_author_uid")},
            "source_comment_id": str(comment) if source_href and comment else None,
            "source_href": source_href,
            "sent_href": comment_href(resource, replied_id) if comment_id_pattern.fullmatch(str(replied_id)) else None,
            "comment_outcome": outcome,
            "account_reply_text": (
                observed.get("text") or (matched or {}).get("text") if outcome == "same_account" else None
            ),
            "confirmed_reply_text": matched.get("text") if matched and outcome == "previous_reply" else None,
            "own_resource": thread["resource_author_uid"] == account if thread["resource_author_uid"] else None,
            "own_thread": thread["root_author_uid"] == account if thread["root_author_uid"] else None,
        }

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
    for row in base["_intents"]:
        target = _json(row["target"], {})
        if round_id and row["round_id"] != round_id:
            continue
        bvid = target.get("bvid")
        comment_kind = "top_level" if row["kind"] == "top_level" else "reply"
        recipient = target.get("recipient_name") or ""
        if row["kind"] == "top_level" and bvid in by_video:
            reason = (by_video[bvid].get("main_decision") or {}).get("explanation") or by_video[bvid]["reason"]
        else:
            reason = comment_reasons.get((bvid, str(target.get("parent") or ""))) or ""
        at = (
            publication_time(row["receipt"], row["updated_at"])
            if row["status"] == "published"
            else row["updated_at"]
        )
        if not include(at):
            continue
        resource = resource_key(target)
        root = target.get("root")
        if comment_kind == "top_level" and not root:
            root = _confirmed_comment_id(row["status"], _json(row.get("receipt"), {}), comment_id_pattern)
        details = thread_details(resource, root, target.get("parent") or root, row, target)
        if details:
            listed_threads.add((resource, str(root), row["round_id"]))
        add(
            row,
            channel="comment",
            identity=f"comment/browsing/{row['id']}",
            text=row["text"],
            title="视频主评论" if comment_kind == "top_level" else f"回复 {recipient or '评论'}",
            href=f"#comments/browsing/{row['id']}",
            source_pipeline=row.get("item_pipeline") or _round_pipeline(store, row["round_id"], round_cache),
            at=at,
            reason=reason,
            bvid=bvid,
            comment_kind=comment_kind,
            reply_kind=comment_kind,
            recipient_name=recipient,
            recipient_uid=str(target["recipient"])
            if comment_kind == "reply" and str(target.get("recipient", "")).isdigit()
            else None,
            round_id=row["round_id"],
            source_text=target.get("source_text") or "",
            **details,
        )

    if account and "drafts" in tables and not round_id and "account_id" in {
        column[1] for column in store.db.execute("PRAGMA table_info(drafts)")
    }:
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
            if not include(at):
                continue
            details = {} if handoff else thread_details(row["video_id"], row["thread_id"], row["comment_id"], row, check)
            add(
                row,
                channel="comment",
                identity=f"comment/manual/{row['id']}",
                text="" if handoff else row["text"],
                title=f"回复 {row['display_name'] or '评论'}",
                href=f"#comments/comments/{row['id']}",
                source_pipeline="manual",
                at=at,
                reason="" if handoff else check.get("explanation") or "",
                bvid=None if handoff else row["video_id"],
                comment_kind="reply",
                reply_kind="reply",
                recipient_name="" if handoff else row["display_name"] or "",
                source_text="" if handoff else row["source_text"],
                **details,
            )

    if kind in {"all", "comments", "public"}:
        lower, upper = (cutoff, 253402300800) if round_id else (max(start, cutoff), end)
        shown_replies = {
            (item["source_href"], item["sent_href"])
            for item in items
            if item.get("source_href") and item.get("sent_href")
        }
        shown_reads = set()
        for observed in comment_activity(store, account, character, start=lower, end=upper, round_id=round_id):
            resource, root, observed_round = (observed[key] for key in ("resource", "root_id", "round_id"))
            if (resource, root, observed_round) in listed_threads:
                continue
            if (resource, root) not in threads:
                threads[resource, root] = comment_thread(store, resource, root, account_id=account, character_id=character)
            thread = threads[resource, root]
            outcomes = [
                item
                for item in thread["comments"]
                if item["publication"]
                and item["parent_comment_id"]
                and (item["created_at"] is None or item["created_at"] <= observed["observed_at"])
            ]
            outcome = max(outcomes, key=lambda item: item["created_at"] or 0, default=None)
            target_id = outcome["parent_comment_id"] if outcome else root
            source_row = next((item for item in thread["comments"] if item["comment_id"] == target_id), None)
            row = {"status": "read", "updated_at": observed["observed_at"]}
            details = thread_details(resource, root, target_id, row)
            if details.get("sent_href"):
                reply_key = (details["source_href"], details["sent_href"])
                if reply_key in shown_replies:
                    continue
                shown_replies.add(reply_key)
            else:
                thread_key = (resource, root)
                if thread_key in shown_reads:
                    continue
                shown_reads.add(thread_key)
            add(
                row,
                channel="comment",
                identity=f"comment/thread/{resource}/{root}/{observed_round}",
                text="",
                source_text=(source_row or {}).get("text"),
                title="浏览评论楼",
                href=comment_href(resource, root),
                source_pipeline=_round_pipeline(store, observed_round, round_cache),
                round_id=observed_round,
                bvid=resource if not resource.startswith("dynamic:") else None,
                comment_kind="reply" if outcome else None,
                **details,
            )

    if kind != "public" and account and "dm_inbox" in tables:
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
                href=f"#messages/inbox/{row['id']}",
                source_pipeline="inbox",
                direction="incoming",
                source_kind="message",
                contact_type="incoming",
                recipient_name=row["display_name"] or row["platform_user_id"],
            )
    if kind != "public" and account and "dm_drafts" in tables:
        for row in store.all("SELECT * FROM dm_drafts WHERE account_id=? AND character_id=?", (account, character)):
            if not row.get("text") and row.get("message_kind") != "image":
                continue
            at = (
                publication_time(row["receipt"], row["updated_at"])
                if row["status"] == "published"
                else row["updated_at"]
            )
            source_kind = row.get("source_kind")
            contact_type = row.get("contact_type") or (
                "outreach" if source_kind == "comment" else "reply" if source_kind == "message" else None
            )
            label = "主动私信" if contact_type == "outreach" else "私信回复" if contact_type == "reply" else "私信草稿"
            video_ev = _json(row.get("video_evidence"), {}) or {}
            origin_round = video_ev.get("origin_round_id")
            dm_pipeline = (
                _round_pipeline(store, origin_round, round_cache)
                if origin_round
                else ("inbox" if source_kind in {"message", "comment"} else "manual")
            )
            add(
                row,
                channel="message",
                identity=f"message/draft/{row['id']}",
                text=row["text"],
                title=f"{label} {row['display_name'] or row['platform_user_id']}",
                href="#messages",
                source_pipeline=dm_pipeline,
                at=at,
                reason=row.get("reason") or "",
                direction="outgoing",
                source_kind=source_kind,
                contact_type=contact_type,
                outreach_status=contact_state(row | {"outreach_check": _json(row.get("outreach_check"), {})})
                if contact_type == "outreach"
                else None,
                recipient_name=row["display_name"] or row["platform_user_id"],
                message_kind=row.get("message_kind", "text"),
                meme_id=row.get("meme_id"),
            )

    if kind != "public" and account and "dynamic_posts" in tables:
        columns = {item[1] for item in store.db.execute("PRAGMA table_info(dynamic_posts)")}
        owned = set()
        if "operation_write_bindings" in tables and "account_writes" in tables:
            owned.update(
                str(item["post_id"])
                for item in store.all(
                    "SELECT json_extract(b.binding_json,'$.target.post_id') AS post_id "
                    "FROM operation_write_bindings b JOIN account_writes w ON w.token=b.root_token "
                    "WHERE w.account_id=? AND w.character_id=? AND w.channel='dynamics'",
                    (account, character),
                )
                if item["post_id"] is not None
            )
        if "operation_continuations" in tables:
            owned.update(
                str(item["entity_id"])
                for item in store.all(
                    "SELECT entity_id FROM operation_continuations WHERE channel='dynamics' "
                    "AND json_extract(binding,'$.account_id')=? AND json_extract(binding,'$.character.id')=?",
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
                source_pipeline="manual" if row.get("manual") else "discovery",
                at=at,
                reason=row.get("reason") or "",
            )

    if round_id:
        items = [item for item in items if item.get("round_id") == round_id]
    if source != "all":
        items = [item for item in items if item.get("source_pipeline") == source]
        videos = [video for video in videos if video.get("source_pipeline") == source]
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
            and item["status"] in {"pending_review", "approved", "queued", "generating", "publishing", "automatic_pending"}
            for item in items
        ),
        "failed": sum(item["status"] == "failed" for item in items),
        "replies": sum(item["kind"] == "comment" and item["status"] == "published" for item in items),
        **{key: value for key, value in base["counts"].items() if key not in {"videos", "failed", "replies"}},
    }
    if kind == "public":
        filtered = [item for item in items if item["kind"] in {"video", "comment"}]
    else:
        filtered = items if kind == "all" else [item for item in items if item["kind"] == kinds[kind]]
    if kind == "comments":
        replied = {"new_reply", "previous_reply", "same_account"}
        sent = {}
        for item in filtered:
            if start <= item["updated_at"] < end and (
                identifier := _confirmed_comment_id(item["status"], item["receipt"], comment_id_pattern)
            ):
                sent.setdefault(identifier, item["comment_kind"])
        sent_top_level = sum(value == "top_level" for value in sent.values())
        if comment_filter == "read":
            filtered = [item for item in filtered if item["status"] == "read" and not item.get("comment_outcome")]
        elif comment_filter == "replied":
            filtered = [item for item in filtered if item.get("comment_outcome") in replied]
        elif comment_filter == "pending_review":
            filtered = [item for item in filtered if item["status"] == "pending_review"]
        counts = {
            "total": len(filtered),
            "sent": len(sent),
            "sent_top_level": sent_top_level,
            "sent_replies": len(sent) - sent_top_level,
            "pending": sum(item["status"] == "pending_review" for item in filtered),
            "failed": sum(item["status"] == "failed" for item in filtered),
        }
    result = {
        "day": base["day"],
        "timezone": base["timezone"],
        "days": sorted(days, reverse=True),
        "kind": kind,
        "source": source,
        "counts": counts,
        "items": filtered,
    }
    if kind == "comments":
        result["comment_filter"] = comment_filter
    result = _paginate(result, page, page_size) if paginate else result
    comment_rows = [item for item in result["items"] if item["kind"] == "comment" and item.get("bvid")]
    if comment_rows:
        bvids = sorted({item["bvid"] for item in comment_rows})
        titles = _comment_video_titles(store, account, character, bvids, by_video, round_id)
        for item in comment_rows:
            item["video_title"] = titles.get(item["bvid"]) or item["bvid"]
    return result


def _comment_video_titles(store, account, character, bvids, by_video, round_id):
    titles = {
        bvid: by_video[bvid]["title"]
        for bvid in bvids
        if bvid in by_video and by_video[bvid].get("title") not in {None, "", bvid}
    }
    placeholders = ",".join("?" for _ in bvids)
    tasks = (
        "SELECT t.bvid,COALESCE(json_extract(t.payload,'$.metadata.title'),"
        "json_extract(t.payload,'$.full_coverage.title'),json_extract(t.payload,'$.title')) AS title "
        "FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id "
        f"WHERE r.account_id=? AND r.character_id=? AND t.bvid IN ({placeholders})"
    )
    sources = (
        (tasks + " AND r.id=? ORDER BY t.updated_at DESC", (account, character, *bvids, round_id)),
        (
            "SELECT bvid,json_extract(material,'$.metadata.title') AS title FROM browsing_video_progress "
            f"WHERE account_id=? AND character_id=? AND bvid IN ({placeholders})",
            (account, character, *bvids),
        ),
        (tasks + " ORDER BY t.updated_at DESC", (account, character, *bvids)),
    )
    for query, parameters in sources:
        if len(titles) == len(bvids):
            break
        for row in store.all(query, parameters):
            if row["title"] and row["title"] != row["bvid"]:
                titles.setdefault(row["bvid"], row["title"])
    return titles


def _issue_key(browsing):
    return "browsing_issues_ack:" + json.dumps(_history_scope(browsing)[:2], ensure_ascii=False)


def _issue_identity(issue):
    return json.dumps([issue.get("bvid"), issue.get("error")], ensure_ascii=False)


def issue_history(browsing, issues):
    """Annotate discovery/browsing issues with unread occurrence counts and severity."""
    ack = browsing.store.get(_issue_key(browsing), {})
    version = _history_scope(browsing)[2]
    result = []
    for issue in issues:
        identity = _issue_identity(issue)
        seen = ack.get("counts", {}).get(identity, 0)
        total = issue.get("occurrences", 1)
        newer = issue.get("updated_at", 0) > ack.get("at", 0)
        selected = ack.get("selected", {}).get(identity)
        if selected:
            if (
                selected.get("version", version) != version
                or total < selected["count"]
                or (total == selected["count"] and issue.get("updated_at", 0) > selected.get("updated_at", float("inf")))
            ):
                count = total
            else:
                count = total - selected["count"]
        elif identity in ack.get("counts", {}) and total > seen:
            count = total - seen
        elif newer:
            count = total if total < seen else max(1, total - seen)
        else:
            count = 0
        result.append(
            issue
            | {
                "unread_occurrences": count,
                "severity": "warning" if issue.get("status") in {"retry_wait", "recovered"} else "error",
            }
        )
    return result


def clear_issues(browsing):
    """Mark all currently surfaced issues as cleared for the active account/character scope."""
    records = browsing.discovery.records()
    old = browsing.store.get(_issue_key(browsing), {})
    counts = dict(old.get("counts", {}))
    for issue in records["issues"]:
        key = _issue_identity(issue)
        counts[key] = issue.get("total_occurrences", counts.get(key, 0) + issue.get("occurrences", 1))
    browsing.store.set(_issue_key(browsing), {"at": time.time(), "counts": counts})
    return browsing.discovery.records()


def acknowledge_issues(browsing, observations):
    """Acknowledge only the exact issue occurrences present in the operator's verified snapshot."""
    key = _issue_key(browsing)
    issues = browsing.discovery.records()["issue_history"]
    current = {issue["id"]: issue for issue in issues}
    ack = browsing.store.get(key, {})
    selected = dict(ack.get("selected", {}))
    version = _history_scope(browsing)[2]
    for observation in observations:
        issue = current.get(observation["id"])
        if not issue or observation["updated_at"] != issue.get("updated_at"):
            raise ValueError("告警记录已变化，请按新记录核对")
        count = observation["occurrences"]
        if count != issue.get("occurrences", 1):
            raise ValueError("告警发生次数已变化，请按新记录核对")
        selected[_issue_identity(issue)] = {"count": count, "version": version, "updated_at": issue["updated_at"]}
    browsing.store.set(key, ack | {"selected": selected})
    return browsing.discovery.records()
