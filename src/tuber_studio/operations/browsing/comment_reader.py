"""Per-comment reading decisions, independent of video and page progress."""

import asyncio
import inspect
import json
import time

from ...comment_materials import prepare_comment_materials
from ...comments import category
from ...compaction import ContextWindowExceeded
from ...generation_meter import BrowsingBudgetDeferred
from ...message_outreach import confirmed_invitation
from ...operation_continuations import content_hash
from ...operation_turns import operation_lane
from ...operations_control import canonical_intent
from ...providers import public_error
from ...publication_errors import SendLimitReached
from ...store import dump
from .models import ResponseDecision
from .quotas import DailyReplyLimitReached, day_window
from .thread_context import comment_source, comment_thread_context, operator_reply, read_scoped_thread


def source_discussion(source_row, context, items):
    def snapshot(row, parent):
        return comment_source(row) | {"created_at": row.get("created_at"), "parent_comment_id": parent}

    verified = context.get("parent") or {}
    parent_id = verified.get("comment_id")
    parent = next((item for item in items if verified.get("available")
                   and str(item["comment_id"]) == str(parent_id)), None)
    source = snapshot(source_row, context.get("addressing", {}).get("source_parent_comment_id"))
    if parent is None:
        return {"source": source}
    return {"source": source, "parent": snapshot(parent, verified.get("parent_comment_id"))}


def _extract_timestamp(item):
    for key in ("created_at", "ctime", "pubdate", "time", "timestamp"):
        val = item.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                pass
    return None


def merge_continuous_comments(items, time_window_seconds=600):
    """Group adjacent sources without synthesizing a platform comment."""
    def addressing(item):
        mentions = item.get("mentions") or []
        uid = item.get("user_id") or item.get("platform_user_id")
        if not uid or item.get("parent_verified") is False or any(not mention.get("uid") for mention in mentions):
            return None
        return (
            str(uid), str(item.get("thread_id") or item["comment_id"]),
            str(item.get("parent_comment_id") or "0"),
            frozenset(str(mention["uid"]) for mention in mentions),
        )

    groups = []
    for item in items:
        timestamp = _extract_timestamp(item)
        group = groups[-1] if groups else []
        signature = addressing(item)
        if group and len(group) < 8 and timestamp is not None and signature and signature == addressing(group[-1]):
            times = [_extract_timestamp(previous) for previous in group]
            if all(value is not None and abs(timestamp - value) <= time_window_seconds for value in times):
                group.append(item)
                continue
        groups.append([item])
    result = []
    for group in groups:
        if len(group) == 1:
            result.append(dict(group[0]))
        else:
            group.sort(key=_extract_timestamp)
            result.append(dict(group[-1], grouped_comments=group))
    return result


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

    def get(self, row, bvid, source, *, legacy_source=None):
        value = self.store.one(
            "SELECT decision,source_hash FROM browsing_comment_progress "
            "WHERE account_id=? AND character_id=? AND bvid=? AND comment_id=?",
            self.identity(row, bvid, source),
        )
        if value and (value["source_hash"] == content_hash(source) or
                      legacy_source is not None and value["source_hash"] == content_hash(legacy_source)):
            saved = json.loads(value["decision"])
            generation = saved.pop("generation", {})
            decision = ResponseDecision.model_validate(saved)
            if decision.action in {"reply_public", "handoff_private"} and generation.get("source_id") != str(source["comment_id"]):
                # Legacy merged rows could copy a decision onto several IDs.
                # Existing intents still own their targets, but bare decisions
                # without an original anchor cannot authorize a new one.
                return None
            decision._generation_id = generation.get("id")
            decision._generation_identity = generation.get("identity")
            decision._comment_materials = generation.get("comment_materials", [])
            # A deliberate wait remains eligible on the next visit.
            return decision if decision.action != "wait" else None
        return None

    def save(self, row, bvid, source, decision):
        self.store.execute(
            "INSERT OR REPLACE INTO browsing_comment_progress VALUES(?,?,?,?,?,?,?)",
            (
                *self.identity(row, bvid, source),
                content_hash(source),
                dump(decision.model_dump() | {"generation": {
                    "id": decision._generation_id, "identity": decision._generation_identity,
                    "source_id": str(source["comment_id"]),
                    **({"comment_materials": decision._comment_materials} if decision._comment_materials else {}),
                }}),
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


def enqueue_comment_visit(browsing, row, task_id, metadata, items):
    bvid, oid = metadata["bvid"], str(metadata["aid"])
    scope = [row["account_id"], row["character_id"], row["config"].get("review_session_id"), oid]
    for item in merge_continuous_comments(items):
        source = comment_source(item)
        if not source["author_uid"] or source["author_uid"] == str(row["account_id"]):
            continue
        root = str(item.get("thread_id") or source["comment_id"])
        identity = "visit:" + content_hash([*scope, source["comment_id"]])
        event = {
            "event_id": identity, "kind": "comment_visit", "account_id": row["account_id"],
            "character_id": row["character_id"], "review_session_id": scope[2],
            "resource_type": "video", "type": 1, "oid": oid, "bvid": bvid,
            "root": root, "parent": source["comment_id"], "comment_id": source["comment_id"],
            "sender_uid": source["author_uid"], "sender_name": source["author_name"],
            "source_text": source["text"], "resource_author_uid": metadata.get("owner_uid"),
            "visit_task_id": task_id, "grouped_ids": [str(x["comment_id"]) for x in item.get("grouped_comments", [])],
            "items": [x for x in items if str(x.get("thread_id") or x["comment_id"]) == root],
        }
        browsing.store.db.execute(
            "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,'received',?,NULL,?)",
            (identity, row["account_id"], event["kind"], source["comment_id"], _extract_timestamp(item) or 0,
             dump(event), time.time()),
        )


async def evaluate_comment_visit(browsing, result, row, metrics):
    """Browse a video's discussions without starting or updating its viewing task."""
    identity, metadata = result["id"], result["metadata"]
    material, key = result["material"], result["material_key"]
    payload = json.loads(browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"])
    if explanation := (payload.get("selection") or {}).get("explanation"):
        payload["reason"] = explanation
    status, context = "comments_checked", None
    try:
        browsing._check_round(row)
        context = await browsing.video_comment_context(row, metadata["bvid"])
        items = context.get("items", [])
        if row["config"].get("interaction_only") and not row["preview"]:
            with browsing.store.db:
                enqueue_comment_visit(browsing, row, identity, metadata, items)
            browsing.repo.complete(identity, result["owner"], result["generation"], status, payload, key)
            browsing.commit_comment_context(context)
            return
        chosen = {segment["id"] for segment in browsing._segments(material)} if material else set()
        for item in merge_continuous_comments(items):
            if browsing.repo.video_reply_limit_reached(row, metadata["bvid"]):
                raise DailyReplyLimitReached(day_window(row["config"].get("business_timezone", "Asia/Taipei")))
            intent = await browsing.decisions._reply_to_video_comment(
                row, material, key, chosen, item, items, metrics,
                diagnostics=payload.setdefault("repetition_decisions", []), metadata=metadata,
            )
            if intent:
                payload.setdefault("reply_intent_ids", []).append(intent["id"])
                status = "selected"
            if not payload.get("reason"):
                stored = browsing.store.one(
                    "SELECT decision FROM browsing_comment_progress "
                    "WHERE account_id=? AND character_id=? AND bvid=? AND comment_id=?",
                    (row["account_id"], row["character_id"], metadata["bvid"], str(item["comment_id"])),
                )
                if stored and (explanation := json.loads(stored["decision"]).get("explanation")):
                    payload["reason"] = explanation
        browsing.repo.complete(identity, result["owner"], result["generation"], status, payload, key)
        browsing.commit_comment_context(context)
    except (DailyReplyLimitReached, SendLimitReached) as error:
        browsing.repo.complete(
            identity, result["owner"], result["generation"], "deferred_daily",
            payload | {
                "retry_kind": "send_limit" if isinstance(error, SendLimitReached) else "daily_reply_limit",
                "next_at": error.next_at,
            }, key,
        )
    except Exception as error:
        metrics["failures"] += 1
        continuation = getattr(error, "continuation", None)
        deferred = isinstance(error, BrowsingBudgetDeferred)
        if continuation:
            browsing.runtime.continuations.bind_source_view(
                continuation["id"], "browsing", identity, "评论回应生成记录 · " + metadata["bvid"],
            )
        browsing.repo.complete(
            identity, result["owner"], result["generation"],
            "invalid" if continuation and continuation["state"] == "invalid"
            else "checkpoint" if continuation or deferred or isinstance(error, ContextWindowExceeded) else "failed",
            payload | {
                **({"generation_continuation_id": continuation["id"]} if continuation else {}),
                **({"retry_kind": "round_budget"} if deferred else {}),
                **({"capacity_signature": browsing.decisions._capacity_signature(row["config"])}
                   if isinstance(error, ContextWindowExceeded) else {}),
            }, key, public_error(error),
        )
    finally:
        result["heartbeat"].cancel()
        await asyncio.gather(result["heartbeat"], return_exceptions=True)


async def handoff_comment(browsing, row, reply, target, thread, material, keys, *, event=None, created_at=""):
    """One private handoff path for verified notifications and video discussions."""
    policy = browsing.messages.policy()
    if not (row["config"]["comment_handoff_enabled"] and policy["enabled"] and policy["allow_comment_handoff"]):
        raise ValueError("评论转私信未授权")
    source = thread["source"]
    resource = target.get("bvid") or "dynamic:" + str(target["oid"])
    if str(source["comment_id"]) != str(target["parent"]) or str(source["author_uid"]) != str(target["recipient"]):
        raise ValueError("私信移交的评论与收件人不一致")
    invitation = confirmed_invitation(thread, row["account_id"], target["recipient"])
    if not invitation:
        return None
    intent = browsing.publication._intent(row, "private_handoff", target, reply, keys, reply.evidence_ids)
    if not intent or intent["kind"] != "private_handoff":
        return None
    browsing.store.execute(
        "INSERT OR IGNORE INTO comments(platform,video_id,comment_id,thread_id,parent_comment_id,platform_user_id,display_name,content,category,created_at,imported_at) VALUES('bilibili',?,?,?,?,?,?,?,'interaction',?,?)",
        (resource, str(source["comment_id"]), str(target["root"]),
         (thread.get("addressing") or {}).get("source_parent_comment_id"),
         str(source["author_uid"]), source.get("author_name") or source.get("display_name") or (event or {}).get("sender_name", ""),
         source["text"], str((event or {}).get("timestamp", created_at) or ""), time.time()),
    )
    comment = browsing.store.one(
        "SELECT * FROM comments WHERE platform='bilibili' AND video_id=? AND comment_id=?",
        (resource, str(source["comment_id"])),
    )
    if comment["content"] != source["text"] or str(comment["platform_user_id"]) != str(source["author_uid"]):
        raise ValueError("已导入评论的原文或作者与当前来源不一致")
    evidence = browsing.evidence_payload(keys, browsing._segments(material) if material else [])
    # The fallback is the real public comment, not a claim that its video was watched.
    public_thread = thread | {"target": target}
    if event is None:
        public_thread["public_text"] = thread.get("public_text") or source["text"]
    evidence.update(public_source_thread=public_thread, origin_round_id=row["id"])
    if event is not None:
        evidence["source_event"] = event
    automatic = bool(intent["auto_authorized"] and policy["auto_send"])
    dm_id = browsing.messages.enqueue(
        "comment", comment["id"], automatic=automatic, reason=reply.reason,
        character=browsing.store.character(row["character_id"], row["character_version"]),
        account_id=row["account_id"], video_evidence=evidence, schedule=False,
        purpose=invitation["quote"],
        contact_trigger=invitation["kind"],
    )
    private = browsing.store.one("SELECT status FROM dm_drafts WHERE id=?", (dm_id,))
    target = target | {"private_draft_id": dm_id, "contact_state": private["status"]}
    browsing.store.execute(
        "UPDATE browsing_intents SET target=? WHERE id=?",
        (dump(target), intent["id"]),
    )
    await browsing.messages._generate(dm_id, automatic=automatic)
    browsing.store.execute("UPDATE browsing_intents SET target=json_set(target,'$.contact_state',?) WHERE id=?",
                           (browsing.store.one("SELECT status FROM dm_drafts WHERE id=?", (dm_id,))["status"], intent["id"]))
    return browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (intent["id"],)))


async def run_interactions(browsing, row, metrics):
    public_enabled = row["config"]["reply_enabled"] or row["config"].get("comment_like_enabled")
    if hasattr(browsing.adapter, "own_uploads") and public_enabled:
        try:
            await browsing._collect_own_comments(row)
        except (ValueError, TimeoutError) as error:
            metrics["own_upload_error"] = public_error(error)
            metrics["failures"] += 1
    if hasattr(browsing.adapter, "own_dynamics") and public_enabled:
        try:
            await collect_own_dynamics(browsing, row)
        except (ValueError, TimeoutError) as error:
            metrics["own_dynamic_error"] = public_error(error)
            metrics["failures"] += 1
    try:
        await browsing.sync_notifications(row)
    except (ValueError, TimeoutError) as error:
        metrics["notification_error"] = public_error(error)
        metrics["failures"] += 1


async def collect_own_comments(browsing, row):
    account = row["account_id"]
    cutoff = max(int(browsing.started_at), int(browsing.operations.policy()["enabled_at"] or 0))
    for video in await browsing._read(browsing.adapter.own_uploads, account):
        result = await browsing._read(lambda bvid: browsing.adapter.fetch(bvid, newest=True), video["bvid"])
        browsing._check_round(row)
        enqueue_own_comments(browsing, account, cutoff, {"resource_type": "video", "oid": video["aid"],
                                                      "bvid": video["bvid"]}, result["items"])


async def collect_own_dynamics(browsing, row):
    account = row["account_id"]
    cutoff = max(int(browsing.started_at), int(browsing.operations.policy()["enabled_at"] or 0))
    for target in await browsing._read(browsing.adapter.own_dynamics, account):
        items = await browsing._read(browsing.adapter.fetch_dynamic_comments, target)
        browsing._check_round(row)
        enqueue_own_comments(browsing, account, cutoff, target, items)


def enqueue_own_comments(browsing, account, cutoff, target, items):
    for comment in items:
        timestamp = int(comment["created_at"])
        author = str(comment["platform_user_id"])
        if timestamp < cutoff or author == account or category(comment["content"], comment.get("attachments") or []) == "spam":
            continue
        comment_id = str(comment["comment_id"])
        identity = "own:" + content_hash([account, target["resource_type"], str(target["oid"]), comment_id])
        event = target | {
            "event_id": identity, "kind": "own_comment", "account_id": account,
            "sender_uid": author, "sender_name": comment["display_name"],
            "root": str(comment["thread_id"]), "parent": comment_id, "comment_id": comment_id,
            "timestamp": timestamp, "source_text": comment["content"],
        }
        browsing.store.execute(
            "INSERT OR IGNORE INTO browsing_events VALUES(?,?,?,?,?,'received',?,NULL,?)",
            (identity, account, "own_comment", comment_id, timestamp, dump(event), time.time()),
        )


async def process_events(browsing, row, metrics, *, own_only=False):
    async with browsing.sync_lock:
        lane = operation_lane.set("reply")
        try:
            await browsing._process_event_batch(row, metrics, own_only)
        finally:
            operation_lane.reset(lane)


async def process_event_batch(browsing, row, metrics, own_only):
    browsing.store.execute(
        "UPDATE browsing_events SET status='received' WHERE account_id=? AND status='reply_deferred' "
        "AND json_extract(error,'$.next_at')<=?",
        (row["account_id"], time.time()),
    )
    events = browsing.store.all(
        "WITH pending AS (SELECT *,CASE WHEN json_valid(payload) THEN payload ELSE '{}' END AS data "
        "FROM browsing_events WHERE account_id=? AND status='received' AND (?=0 OR kind='own_comment') "
        "AND (kind<>'mention' OR ?=1) AND (kind<>'comment_visit' OR (?=0 "
        "AND json_extract(payload,'$.character_id')=? AND json_extract(payload,'$.review_session_id') IS ?))), "
        "scoped AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY "
        "COALESCE(json_extract(data,'$.resource_type'),'unknown'),"
        "COALESCE(CAST(json_extract(data,'$.oid') AS TEXT),json_extract(data,'$.bvid'),source_key,id),"
        "COALESCE(NULLIF(NULLIF(CAST(json_extract(data,'$.root') AS TEXT),'0'),''),"
        "NULLIF(NULLIF(CAST(json_extract(data,'$.parent') AS TEXT),'0'),''),"
        "json_extract(data,'$.comment_id'),source_key,id) ORDER BY timestamp,created_at,id) AS position, "
        "MAX(kind='comment_visit') OVER (PARTITION BY json_extract(data,'$.resource_type'), "
        "CAST(json_extract(data,'$.oid') AS TEXT),CAST(json_extract(data,'$.root') AS TEXT)) AS has_visit FROM pending), "
        "eligible AS (SELECT * FROM scoped WHERE position=1 AND COALESCE(CASE WHEN json_valid(error) THEN "
        "json_extract(error,'$.next_at') END,0)<=?), "
        "priority AS (SELECT id,ROW_NUMBER() OVER (PARTITION BY (kind='own_comment') ORDER BY timestamp DESC,id) AS fresh, "
        "ROW_NUMBER() OVER (PARTITION BY (kind='own_comment') ORDER BY timestamp,id) AS aged "
        "FROM eligible WHERE kind<>'comment_visit') "
        "SELECT * FROM eligible WHERE has_visit=1 OR id IN (SELECT id FROM priority WHERE fresh=1 OR aged=1) "
        "ORDER BY CASE kind WHEN 'own_comment' THEN 0 WHEN 'comment_visit' THEN 2 ELSE 1 END,timestamp DESC,id",
        (row["account_id"], own_only, row["config"]["mention_reply_enabled"], row["preview"],
         row["character_id"], row["config"].get("review_session_id"), time.time()),
    )
    for event_row in events:
        await attempt_event(browsing, event_row, row, metrics)
        if event_row["has_visit"]:
            await continue_visit_thread(browsing, event_row, row, metrics)


async def continue_visit_thread(browsing, event_row, row, metrics):
    data = json.loads(event_row["payload"])
    current = browsing.store.one("SELECT status FROM browsing_events WHERE id=?", (event_row["id"],))
    if current["status"] == "received":
        return
    pending = browsing.store.all(
        "SELECT * FROM browsing_events WHERE account_id=? AND status='received' AND kind='comment_visit' "
        "AND json_extract(payload,'$.character_id')=? AND json_extract(payload,'$.review_session_id') IS ? "
        "AND CAST(json_extract(payload,'$.oid') AS TEXT)=? AND json_extract(payload,'$.root')=? "
        "ORDER BY timestamp,created_at,id",
        (row["account_id"], row["character_id"], row["config"].get("review_session_id"),
         str(data.get("oid")), str(data.get("root"))),
    )
    for queued in pending:
        error = queued.get("error") or ""
        if error.startswith("{") and json.loads(error).get("next_at", 0) > time.time():
            break
        # Notifications can arrive for earlier comments in this same discussion.
        earlier = browsing.store.one(
            "SELECT 1 FROM browsing_events WHERE account_id=? AND status='received' AND kind<>'comment_visit' "
            "AND (kind<>'mention' OR ?=1) AND timestamp<=? AND json_extract(payload,'$.resource_type')='video' "
            "AND CAST(json_extract(payload,'$.oid') AS TEXT)=? AND CAST(json_extract(payload,'$.root') AS TEXT)=? LIMIT 1",
            (row["account_id"], row["config"]["mention_reply_enabled"], queued["timestamp"],
             str(data.get("oid")), str(data.get("root"))),
        )
        if earlier:
            break
        await attempt_event(browsing, queued, row, metrics)
        status = browsing.store.one("SELECT status FROM browsing_events WHERE id=?", (queued["id"],))["status"]
        if status == "received":
            break


async def attempt_event(browsing, event_row, row, metrics):
    task_id = json.loads(event_row["payload"]).get("visit_task_id")
    intent, generation = None, None
    try:
        intent = await browsing._event(event_row, row, metrics)
    except Exception as error:
        continuation = getattr(error, "continuation", None)
        if continuation:
            generation = continuation
            browsing.runtime.continuations.bind_source_view(
                error.continuation["id"],
                "browsing",
                "event:" + str(event_row["id"]),
                "通知回应生成记录",
            )
            browsing.store.execute(
                "UPDATE browsing_events SET status=?,error=? WHERE id=?",
                (
                    "invalid" if continuation["state"] == "invalid" else "checkpoint",
                    dump(
                        {
                            "message": "输出无效，保留原答供检查" if continuation["state"] == "invalid"
                            else "生成进度已保存，可继续",
                            "generation_continuation_id": error.continuation["id"],
                        }
                    ),
                    event_row["id"],
                ),
            )
            if not (task_id and continuation["state"] == "invalid"):
                metrics["failures"] += 1
        elif isinstance(error, (DailyReplyLimitReached, SendLimitReached)) or getattr(error, "kind", None) in {
            "network",
            "timeout",
            "restricted",
            "rate_limited",
            "anti_bot",
            "thread_pending",
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
    if task_id and intent:
        field = "$.like_intent_ids" if intent["kind"] == "comment_like" else "$.reply_intent_ids"
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='selected', "
            "payload=json_insert(payload,?,json('[]')),updated_at=? WHERE id=?",
            (field, time.time(), task_id),
        )
        browsing.store.execute("UPDATE browsing_tasks SET payload=json_insert(payload,?,?) WHERE id=?",
                               (field + "[#]", intent["id"], task_id))
    elif task_id and generation:
        diagnostic = {"comment_id": json.loads(event_row["payload"])["comment_id"],
                      "continuation_id": generation["id"], "state": generation["state"],
                      "feedback": generation["feedback"], "original_candidate": generation["original_output"],
                      "current_candidate": generation["current_output"]}
        browsing.store.execute("UPDATE browsing_tasks SET payload=json_insert(payload,'$.repetition_decisions',json('[]')) WHERE id=?", (task_id,))
        browsing.store.execute("UPDATE browsing_tasks SET payload=json_insert(payload,'$.repetition_decisions[#]',json(?)) WHERE id=?",
                               (dump(diagnostic), task_id))


async def event(browsing, event_row, row, metrics):
    event = json.loads(event_row["payload"])
    event.setdefault("kind", event_row["kind"])
    config = row["config"]
    if event_row["kind"] == "mention" and not config["mention_reply_enabled"] or not (
        config["reply_enabled"] or config.get("comment_like_enabled")
    ):
        return
    thread = await read_event_thread(browsing, event)
    target = thread["target"]
    resource = target.get("bvid") or "dynamic:" + str(target["oid"])
    root = str(target.get("root") or thread["source"]["comment_id"])
    if root == "0":
        root = str(thread["source"]["comment_id"])
    scope = (str(row["account_id"]), row["character_id"], resource, root)
    async with browsing.runtime.conversation_turn(scope) as waited:
        if waited:
            browsing._check_round(row)
            thread = await read_event_thread(browsing, event)
        return await browsing._event_unlocked(event_row, row, metrics, event, thread)


async def read_event_thread(browsing, event):
    if event["kind"] != "comment_visit":
        return await read_scoped_thread(browsing.store, browsing.adapter.read_public_thread, event, event,
                                        invoke=browsing._read)
    target = {key: event[key] for key in ("type", "resource_type", "oid", "bvid", "root", "parent")}
    if hasattr(browsing.adapter, "read_video_thread"):
        thread = await read_scoped_thread(browsing.store, browsing.adapter.read_video_thread, target, event,
                                          invoke=browsing._read)
        thread = {"target": target} | thread
        thread["source"] = {"comment_id": target["parent"]} | thread["source"]
    else:
        item = next(item for item in event["items"] if str(item["comment_id"]) == target["parent"])
        thread = {"target": target, "source": comment_source(item), "items": event["items"]}
    return thread | {"account_id": event["account_id"], "resource_type": "video",
                     "resource_author_uid": event.get("resource_author_uid")}


def settle_aliases(browsing, event_row, row, target, source):
    if row["preview"]:
        return
    browsing.store.execute(
        "UPDATE browsing_events SET status='skipped',error='同一公开评论已处理' WHERE account_id=? "
        "AND status IN ('received','reply_deferred') AND id<>? AND json_extract(payload,'$.resource_type')=? "
        "AND CAST(json_extract(payload,'$.oid') AS TEXT)=? AND CAST(json_extract(payload,'$.parent') AS TEXT)=? "
        "AND CAST(json_extract(payload,'$.sender_uid') AS TEXT)=? AND (kind<>'comment_visit' OR "
        "(json_extract(payload,'$.character_id')=? AND json_extract(payload,'$.review_session_id') IS ?))",
        (row["account_id"], event_row["id"], target.get("resource_type") or ("video" if target.get("bvid") else "dynamic"),
         str(target["oid"]), str(source["comment_id"]), str(source["author_uid"]), row["character_id"],
         row["config"].get("review_session_id")),
    )


async def event_unlocked(browsing, event_row, row, metrics, event, thread):
    config = row["config"]
    source, target = thread["source"], dict(thread["target"])
    target["parent"] = str(source["comment_id"])
    if (
        str(thread["account_id"]) != str(row["account_id"])
        or str(source["author_uid"]) == str(row["account_id"])
    ):
        raise ValueError("账号来源无效、本人循环或广告互动")
    resource = target.get("bvid") or "dynamic:" + str(target["oid"])
    canonical = canonical_intent("bilibili", resource, source["comment_id"], row["character_id"])
    items_list = thread.get("items", [])
    existing = browsing.publication.source_intent(row, canonical)
    matched = operator_reply(items_list, target, row["account_id"])
    if matched and (not existing or existing["status"] != "published"):
        browsing.store.execute(
            "UPDATE browsing_events SET status='operator_replied',source_key=?,"
            "payload=json_set(payload,'$.account_reply',json(?)),error=NULL WHERE id=?",
            (canonical, dump(comment_source(matched)), event_row["id"]),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return
    if existing:
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='同源回应意图已存在' WHERE id=?",
            (event_row["id"],),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return
    if browsing.publication.has_manual_draft(
        row, resource, source["comment_id"],
    ):
        browsing.store.execute(
            "UPDATE browsing_events SET status='skipped',error='同源草稿已存在' WHERE id=?",
            (event_row["id"],),
        )
        return
    source_row = next((r for r in items_list if str(r.get("comment_id")) == str(source["comment_id"])), None)
    if source_row is None:
        source_row = {
            "comment_id": source["comment_id"], "thread_id": target["root"],
            "platform_user_id": source["author_uid"],
            "display_name": source.get("author_name") or source.get("display_name") or event.get("sender_name", ""),
            "content": source["text"],
        }
    checked_source = comment_source(source_row)
    if checked_source["text"] != source["text"] or checked_source["author_uid"] != str(source["author_uid"]) or (
        event.get("sender_uid") and str(source["author_uid"]) != str(event["sender_uid"])
    ):
        raise ValueError("评论原文或作者已变化，未使用旧候选")
    if category(source["text"], source_row.get("attachments") or []) == "spam":
        raise ValueError("账号来源无效、本人循环或广告互动")
    like_enabled = config.get("comment_like_enabled", False)
    like_intent = browsing.publication.comment_like_intent(row, target) if like_enabled else None
    if like_intent:
        if (like_intent["auto_authorized"] and like_intent["epoch"] == row["epoch"]
                and like_intent["status"] == "pending_review"
                and browsing.publication._action_allowed("comment_like", browsing.effective())):
            await browsing.publish(like_intent["id"], automatic=True)
        browsing.store.execute(
            "UPDATE browsing_events SET status='drafted',source_key=?,error=NULL WHERE id=?",
            (canonical, event_row["id"]),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return like_intent
    reply_limited = bool(target.get("bvid") and config["reply_enabled"] and
                         browsing.repo.video_reply_limit_reached(row, target["bvid"]))
    allow_public = config["reply_enabled"] and not (like_enabled and reply_limited)
    allow_like = like_enabled and not source_row.get("liked_by_self")
    group_ids = event.get("grouped_ids") or []
    if group_ids:
        grouped = [item for item in items_list if str(item["comment_id"]) in group_ids
                   and comment_source(item)["author_uid"] == str(source["author_uid"])
                   and not operator_reply(items_list, target | {"parent": item["comment_id"]}, row["account_id"])
                   and not browsing.publication.source_intent(
                       row, canonical_intent("bilibili", resource, item["comment_id"], row["character_id"])
                   )]
        if len(grouped) > 1:
            source_row = source_row | {"grouped_comments": grouped}
    discussion = comment_thread_context(source_row, items_list, target)
    thread.update({key: value for key, value in discussion.items() if key != "source"})
    legacy_source, source = source, discussion["source"]
    if grouped := thread.get("grouped_comments"):
        source = source | {"grouped_comments": grouped}
    if event_row["kind"] == "own_comment":
        thread["resource_author_uid"] = str(row["account_id"])
    reply = browsing.decisions.invited_private_response(row, thread)
    material, keys = None, []
    allowed = {"public:" + str(source["comment_id"])}
    if thread["resource_type"] == "video":
        progress = browsing.repo.content_progress(row, target["bvid"])
        if progress:
            key, material = await browsing.evidence._store(progress["material"])
            keys = [key]
            allowed.update(s["id"] for s in browsing._segments(material))
    elif thread["resource_type"] not in {"video", "dynamic"}:
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
    if reply is None:
        previous = browsing.decisions.comment_progress.get(row, resource, source, legacy_source=legacy_source)
        if previous and (
            previous.action == "reply_public" and allow_public
            or previous.action == "like_comment" and allow_like
            or previous.action == "skip" and not allow_like
        ):
            reply = previous
    if reply is None:
        thread["_allow_public"] = allow_public
        thread["_allow_comment_like"] = allow_like
        if allow_public or allow_like:
            attachments = await prepare_comment_materials(browsing, target, source_row, row=row)
            if attachments:
                thread["_comment_materials"] = attachments
            if not material and not source["text"].strip() and not any(item["status"] == "read" for item in attachments):
                reply = ResponseDecision(action="skip", reason="insufficient_evidence",
                                         explanation="评论没有可读正文，附件也未能读取")
                reply._comment_materials = attachments
            else:
                reply = await browsing.decisions._response(row, material, thread, allowed, "reply_public", metrics)
        else:
            reply = ResponseDecision(action="skip", reason="no_interest")
        if reply.action != "skip" or allow_public:
            browsing.decisions.comment_progress.save(row, resource, source, reply)
    if reply.action == "reply_public" and not allow_public or reply.action == "like_comment" and not allow_like:
        raise ValueError("回应动作未获当前任务授权")
    if like_enabled and reply_limited and reply.action in {"skip", "wait"}:
        limit = DailyReplyLimitReached(day_window(config.get("business_timezone", "Asia/Taipei")))
        browsing.store.execute(
            "UPDATE browsing_events SET status='reply_deferred',error=? WHERE id=?",
            (dump({"message": str(limit), "next_at": limit.next_at}), event_row["id"]),
        )
        settle_aliases(browsing, event_row, row, target, source)
        return
    if reply.action in {"skip", "wait"}:
        failed_materials = [item for item in reply._comment_materials if item["status"] != "read"]
        if failed_materials:
            browsing.store.execute("UPDATE browsing_events SET payload=json_set(payload,'$.unavailable_attachments',json(?)) WHERE id=?",
                                   (dump(failed_materials), event_row["id"]))
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
        if reply.action == "skip":
            settle_aliases(browsing, event_row, row, target, source)
        return
    target.update(recipient=str(source["author_uid"]), recipient_name=source.get("author_name") or "",
                  source_text=source["text"])
    if event_row["kind"] != "comment_visit":
        target["source_event"] = event
    if reply.action == "handoff_private":
        intent = await handoff_comment(browsing, row, reply, target, thread, material, keys,
                                       event=target.get("source_event"), created_at=event_row.get("timestamp", ""))
    else:
        kind = "comment_like" if reply.action == "like_comment" else "reply_public"
        if kind == "reply_public":
            target["source_discussion"] = source_discussion(source_row, discussion, items_list)
            if reply._comment_materials:
                target["comment_materials"] = reply._comment_materials
        intent = browsing.publication._intent(row, kind, target, reply, keys, reply.evidence_ids)
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
    settle_aliases(browsing, event_row, row, target, source)
    return intent
