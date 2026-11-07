"""Per-comment reading decisions, independent of video and page progress."""

import inspect
import json
import time

from ...comments import category
from ...operation_continuations import content_hash
from ...operation_turns import operation_lane
from ...operations_control import canonical_intent
from ...providers import public_error
from ...store import dump
from .models import ResponseDecision
from .quotas import DailyReplyLimitReached, day_window


class CommentProgress:
    def __init__(self, store):
        self.store = store
        store.db.execute("""CREATE TABLE IF NOT EXISTS browsing_comment_progress(
            account_id TEXT,character_id TEXT,bvid TEXT,comment_id TEXT,source_hash TEXT,
            decision TEXT,updated_at REAL,
            PRIMARY KEY(account_id,character_id,bvid,comment_id))""")

    @staticmethod
    def identity(row, bvid, source):
        return (row["account_id"], row["character_id"], bvid, str(source["comment_id"]))

    def get(self, row, bvid, source):
        value = self.store.one(
            "SELECT decision,source_hash FROM browsing_comment_progress "
            "WHERE account_id=? AND character_id=? AND bvid=? AND comment_id=?",
            self.identity(row, bvid, source),
        )
        if value and value["source_hash"] == content_hash(source):
            decision = ResponseDecision.model_validate(json.loads(value["decision"]))
            # A deliberate wait remains eligible on the next visit.
            return decision if decision.action != "wait" else None
        return None

    def save(self, row, bvid, source, decision):
        self.store.execute(
            "INSERT OR REPLACE INTO browsing_comment_progress VALUES(?,?,?,?,?,?,?)",
            (
                *self.identity(row, bvid, source),
                content_hash(source),
                dump(decision.model_dump()),
                time.time(),
            ),
        )


async def video_comment_context(browsing, row, bvid):
    """Read new arrivals and advance through older pages independently."""
    fetch = browsing.adapter.fetch
    parameters = inspect.signature(fetch).parameters
    if row["preview"] or "start_page" not in parameters:
        return await browsing._read(fetch, bvid, 1)
    cursor_key = f"browsing_comment_page:{row['account_id']}:{row['character_id']}:{bvid}"
    page = max(1, int(browsing.store.get(cursor_key, 1)))
    newest = await browsing._read(fetch, bvid, 1, newest=True) if "newest" in parameters else None
    context = await browsing._read(fetch, bvid, 1, start_page=page)
    items, seen = [], set()
    for source in (newest, context):
        for item in (source or {}).get("items", []):
            identity = str(item.get("comment_id", ""))
            if identity and identity not in seen:
                items.append(item)
                seen.add(identity)
    return context | {
        "items": items,
        "_scroll_progress": {"key": cursor_key, "next_page": context.get("next_page") or 1},
    }


def commit_comment_context(browsing, context):
    if progress := (context or {}).get("_scroll_progress"):
        browsing.store.set(progress["key"], progress["next_page"])


async def run_interactions(browsing, row, metrics):
    if hasattr(browsing.adapter, "own_uploads") and row["config"]["reply_enabled"]:
        try:
            await browsing._collect_own_comments(row)
        except (ValueError, TimeoutError) as error:
            metrics["own_upload_error"] = public_error(error)
            metrics["failures"] += 1
    await browsing._process_events(row, metrics, own_only=True)
    try:
        await browsing.sync_notifications(row)
    except (ValueError, TimeoutError) as error:
        metrics["notification_error"] = public_error(error)
        metrics["failures"] += 1
    await browsing._process_events(row, metrics)


async def collect_own_comments(browsing, row):
    account = row["account_id"]
    cutoff = max(int(browsing.started_at), int(browsing.operations.policy()["enabled_at"] or 0))
    for video in await browsing._read(browsing.adapter.own_uploads, account):
        result = await browsing._read(lambda bvid: browsing.adapter.fetch(bvid, newest=True), video["bvid"])
        browsing._check_round(row)
        for comment in result["items"]:
            timestamp = int(comment["created_at"])
            author = str(comment["platform_user_id"])
            if timestamp < cutoff or author == account or category(comment["content"]) == "spam":
                continue
            identity = f"own:{account}:{video['aid']}:{comment['comment_id']}"
            event = {
                "event_id": identity,
                "kind": "own_comment",
                "account_id": account,
                "sender_uid": author,
                "sender_name": comment["display_name"],
                "resource_type": "video",
                "oid": video["aid"],
                "bvid": video["bvid"],
                "root": comment["thread_id"],
                "parent": comment["comment_id"],
                "comment_id": comment["comment_id"],
                "timestamp": timestamp,
                "source_text": comment["content"],
            }
            browsing.store.execute(
                "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,'received',?,NULL,?)",
                (
                    identity,
                    account,
                    "own_comment",
                    str(comment["comment_id"]),
                    timestamp,
                    dump(event),
                    time.time(),
                ),
            )


async def process_events(browsing, row, metrics, *, own_only=False):
    async with browsing.sync_lock:
        lane = operation_lane.set("reply")
        try:
            await browsing._process_event_batch(row, metrics, own_only)
        finally:
            operation_lane.reset(lane)


async def process_event_batch(browsing, row, metrics, own_only):
    events = browsing.store.all(
        "WITH pending AS (SELECT *,CASE WHEN json_valid(payload) THEN payload ELSE '{}' END AS data "
        "FROM browsing_events WHERE account_id=? AND status='received' AND (?=0 OR kind='own_comment')), "
        "scoped AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY "
        "COALESCE(json_extract(data,'$.resource_type'),'unknown'),"
        "COALESCE(json_extract(data,'$.oid'),json_extract(data,'$.bvid'),source_key,id),"
        "COALESCE(NULLIF(NULLIF(CAST(json_extract(data,'$.root') AS TEXT),'0'),''),"
        "NULLIF(NULLIF(CAST(json_extract(data,'$.parent') AS TEXT),'0'),''),"
        "json_extract(data,'$.comment_id'),source_key,id) ORDER BY timestamp,id) AS position FROM pending), "
        "eligible AS (SELECT * FROM scoped WHERE position=1 AND COALESCE(CASE WHEN json_valid(error) THEN "
        "json_extract(error,'$.next_at') END,0)<=?), "
        "newest AS (SELECT id FROM eligible ORDER BY (kind='own_comment') DESC,timestamp DESC,id LIMIT 1), "
        "oldest AS (SELECT id FROM eligible ORDER BY (kind='own_comment') DESC,timestamp,id LIMIT 1) "
        "SELECT * FROM eligible WHERE id IN (SELECT id FROM newest UNION SELECT id FROM oldest) "
        "ORDER BY (kind='own_comment') DESC,timestamp DESC,id LIMIT 2",
        (row["account_id"], own_only, time.time()),
    )
    for event_row in events:
        try:
            await browsing._event(event_row, row, metrics)
        except Exception as error:
            continuation = getattr(error, "continuation", None)
            if continuation:
                browsing.runtime.continuations.bind_source_view(
                    error.continuation["id"],
                    "browsing",
                    "event:" + str(event_row["id"]),
                    "通知回应生成记录",
                )
                browsing.store.execute(
                    "UPDATE browsing_events SET status='checkpoint',error=? WHERE id=?",
                    (
                        dump(
                            {
                                "message": "生成进度已保存，可继续",
                                "generation_continuation_id": error.continuation["id"],
                            }
                        ),
                        event_row["id"],
                    ),
                )
                metrics["failures"] += 1
            elif isinstance(error, DailyReplyLimitReached) or getattr(error, "kind", None) in {
                "network",
                "timeout",
                "rate_limited",
                "anti_bot",
            }:
                previous = event_row.get("error")
                try:
                    occurrences = json.loads(previous or "{}").get("occurrences", 0) + 1
                except (ValueError, TypeError, AttributeError):
                    occurrences = 1
                browsing.store.execute(
                    "UPDATE browsing_events SET status='received',error=? WHERE id=?",
                    (
                        dump(
                            {
                                "message": public_error(error),
                                "occurrences": occurrences,
                                "next_at": getattr(
                                    error,
                                    "next_at",
                                    time.time() + row["config"]["notification_interval_seconds"],
                                ),
                            }
                        ),
                        event_row["id"],
                    ),
                )
            elif isinstance(error, ValueError):
                browsing.store.execute(
                    "UPDATE browsing_events SET status='skipped',error=? WHERE id=?",
                    (public_error(error), event_row["id"]),
                )
                metrics["failures"] += 1
            else:
                raise


async def event(browsing, event_row, row, metrics):
    event = json.loads(event_row["payload"])
    config = row["config"]
    if event_row["kind"] == "mention" and not config["mention_reply_enabled"] or not config["reply_enabled"]:
        return
    thread = await browsing._read(browsing.adapter.read_public_thread, event)
    target = thread["target"]
    resource = target.get("bvid") or "dynamic:" + str(target["oid"])
    root = str(target.get("root") or thread["source"]["comment_id"])
    if root == "0":
        root = str(thread["source"]["comment_id"])
    scope = (str(row["account_id"]), row["character_id"], resource, root)
    async with browsing.runtime.conversation_turn(scope) as waited:
        if waited:
            browsing._check_round(row)
            thread = await browsing._read(browsing.adapter.read_public_thread, event)
        return await browsing._event_unlocked(event_row, row, metrics, event, thread)


async def event_unlocked(browsing, event_row, row, metrics, event, thread):
    config = row["config"]
    source, target = thread["source"], dict(thread["target"])
    target["parent"] = str(source["comment_id"])
    if (
        str(thread["account_id"]) != str(row["account_id"])
        or str(source["author_uid"]) == str(row["account_id"])
        or category(source["text"]) == "spam"
    ):
        raise ValueError("账号来源无效、本人循环或广告互动")
    resource = target.get("bvid") or "dynamic:" + str(target["oid"])
    canonical = canonical_intent("bilibili", resource, source["comment_id"], row["character_id"])
    if browsing.store.one(
        "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
        (row["account_id"], row["character_id"], canonical),
    ):
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='同源回应意图已存在' WHERE id=?",
            (event_row["id"],),
        )
        return
    if browsing.publication.has_manual_draft(
        row, resource, source["comment_id"],
    ):
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='同源草稿已存在' WHERE id=?",
            (event_row["id"],),
        )
        return
    material, keys = None, []
    if event_row["kind"] == "own_comment":
        allowed = {"public:" + str(source["comment_id"])}
    elif thread["resource_type"] == "video":
        progress = browsing.repo.content_progress(row, target["bvid"])
        if progress:
            key, material = await browsing.evidence._store(progress["material"])
        else:
            key, material = await browsing._prepare_evidence(target["bvid"], config)
        keys = [key]
        allowed = {s["id"] for s in browsing._segments(material)}
    elif thread["resource_type"] == "dynamic":
        if not thread.get("public_text"):
            raise ValueError("原文字动态不可访问")
        allowed = {"public:" + str(source["comment_id"])}
    else:
        raise ValueError("未支持通知资源类型")
    browsing.store.execute(
        "UPDATE browsing_events SET status='context_ready',source_key=? WHERE id=?",
        (canonical, event_row["id"]),
    )
    browsing.runtime.relationships.observe(
        row["character_id"],
        "bilibili",
        str(source["author_uid"]),
        event.get("sender_name", ""),
        canonical,
        source["text"],
        account_id=row["account_id"],
    )
    items_list = thread.get("items", [])
    root_id = str(target.get("root") or "")
    parent_id = str(target.get("parent") or "")
    root_row = next((r for r in items_list if str(r.get("comment_id")) == root_id), None)
    parent_row = next((r for r in items_list if str(r.get("comment_id")) == parent_id), None)
    thread["root"] = {
        "comment_id": root_id,
        "author_name": root_row.get("display_name", "") if root_row else "",
        "text": root_row.get("content", "") if root_row else "",
    }
    if parent_id and parent_id != root_id:
        thread["parent"] = {
            "comment_id": parent_id,
            "author_name": parent_row.get("display_name", "") if parent_row else "",
            "text": parent_row.get("content", "") if parent_row else "",
        }
    thread["timeline"] = [
        {
            "author_name": r.get("display_name", "观众"),
            "text": r.get("content", ""),
            "is_root": str(r.get("comment_id")) == root_id,
            "is_target": str(r.get("comment_id")) == str(source.get("comment_id")),
        }
        for r in items_list[:8]
    ]
    reply = browsing.decisions.comment_progress.get(row, resource, source)
    if reply is None:
        reply = await browsing.decisions._response(row, material, thread, allowed, "reply_public", metrics)
        browsing.decisions.comment_progress.save(row, resource, source, reply)
    if reply.action in {"skip", "wait"}:
        browsing.store.execute(
            "UPDATE browsing_events SET status=?,error=? WHERE id=?",
            (
                "received" if reply.action == "wait" else "skipped",
                dump(
                    {
                        "message": reply.explanation or reply.reason,
                        "next_at": time.time() + config["notification_interval_seconds"],
                    }
                )
                if reply.action == "wait"
                else reply.explanation or reply.reason,
                event_row["id"],
            ),
        )
        return
    target.update(recipient=str(source["author_uid"]), source_text=source["text"], source_event=event)
    if reply.action == "handoff_private":
        if not config["comment_handoff_enabled"]:
            raise ValueError("评论转私信未授权")
        intent = browsing.publication._intent(row, "private_handoff", target, reply, keys, reply.evidence_ids)
        # Only normalized, freshly verified platform messages reach this
        # internal import. The operator CSV API keeps its original grammar.
        browsing.store.execute(
            "INSERT OR IGNORE INTO comments(platform,video_id,comment_id,thread_id,parent_comment_id,platform_user_id,display_name,content,category,created_at,imported_at) VALUES('bilibili',?,?,?,?,?,?,?,'interaction',?,?)",
            (
                resource,
                str(source["comment_id"]),
                str(target["root"]),
                str(target["parent"]),
                str(source["author_uid"]),
                event.get("sender_name", ""),
                source["text"],
                str(event["timestamp"]),
                time.time(),
            ),
        )
        comment = browsing.store.one(
            "SELECT * FROM comments WHERE video_id=? AND comment_id=?",
            (resource, str(source["comment_id"])),
        )
        evidence = browsing.evidence_payload(keys, browsing._segments(material) if material else [])
        evidence["public_source_thread"] = thread
        evidence["source_event"] = event
        evidence["origin_round_id"] = row["id"]
        dm_id = browsing.messages.enqueue(
            "comment",
            comment["id"],
            automatic=bool(intent["auto_authorized"]),
            reason=reply.reason,
            character=browsing.runtime.character,
            account_id=row["account_id"],
            video_evidence=evidence,
            schedule=False,
        )
        browsing.store.execute(
            "UPDATE browsing_intents SET reason=? WHERE id=?",
            ("已移交私信草稿 #" + str(dm_id), intent["id"]),
        )
        await browsing.messages._generate(dm_id, automatic=bool(intent["auto_authorized"]))
    else:
        intent = browsing.publication._intent(row, "reply_public", target, reply, keys, reply.evidence_ids)
        if not intent:
            if target.get("bvid") and browsing.repo.video_reply_limit_reached(row, target["bvid"]):
                raise DailyReplyLimitReached(day_window(config.get("business_timezone", "Asia/Taipei")))
            browsing.store.execute(
                "UPDATE browsing_events SET status='skipped',error='已达到单视频回复上限或同源候选已存在' WHERE id=?",
                (event_row["id"],),
            )
            return
        if intent["auto_authorized"]:
            await browsing.publish(intent["id"], automatic=True)
    browsing.store.execute("UPDATE browsing_events SET status='drafted' WHERE id=?", (event_row["id"],))
