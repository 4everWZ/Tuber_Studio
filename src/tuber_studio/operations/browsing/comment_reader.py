"""Per-comment reading decisions, independent of video and page progress."""

import asyncio
import inspect
import json
import time

from ...comments import category
from ...compaction import ContextWindowExceeded
from ...generation_meter import BrowsingBudgetDeferred
from ...operation_continuations import content_hash
from ...operation_turns import operation_lane
from ...operations_control import canonical_intent
from ...providers import public_error
from ...publication_errors import SendLimitReached
from ...store import dump
from .models import ResponseDecision
from .quotas import DailyReplyLimitReached, day_window
from .thread_context import comment_thread_context


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

    def get(self, row, bvid, source):
        value = self.store.one(
            "SELECT decision,source_hash FROM browsing_comment_progress "
            "WHERE account_id=? AND character_id=? AND bvid=? AND comment_id=?",
            self.identity(row, bvid, source),
        )
        if value and value["source_hash"] == content_hash(source):
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
    if reply._generation_id:
        evidence["generation_continuation_id"] = reply._generation_id
    if event is not None:
        evidence["source_event"] = event
    if reply._generation_id:
        evidence["completion_evidence"] = [
            {"step_id": record["id"], "continuation_id": reply._generation_id, **json.loads(record["context"] or "{}")}
            for record in browsing.store.all(
                "SELECT id,context FROM operation_continuation_steps WHERE continuation_id=? AND phase='web_fetch' AND status='ok'",
                (reply._generation_id,),
            )
        ]
    automatic = bool(intent["auto_authorized"] and policy["auto_send"])
    dm_id = browsing.messages.enqueue(
        "comment", comment["id"], automatic=automatic, reason=reply.reason,
        character=browsing.store.character(row["character_id"], row["character_version"]),
        account_id=row["account_id"], video_evidence=evidence, schedule=False,
        purpose=reply.contact_purpose,
        contact_trigger=reply.contact_trigger,
        proposed_text=reply.text, proposal_identity=reply._generation_identity,
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
    if browsing.publication.source_intent(row, canonical):
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
    source_row = next((r for r in items_list if str(r.get("comment_id")) == str(source["comment_id"])), None)
    if source_row is None:
        source_row = {
            "comment_id": source["comment_id"], "thread_id": target["root"],
            "platform_user_id": source["author_uid"],
            "display_name": source.get("author_name") or source.get("display_name") or event.get("sender_name", ""),
            "content": source["text"],
        }
    discussion = comment_thread_context(source_row, items_list, target)
    # The original verified event source keeps its existing hash representation.
    thread.update({key: value for key, value in discussion.items() if key != "source"})
    if event_row["kind"] == "own_comment":
        thread["resource_author_uid"] = str(row["account_id"])
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
        intent = await handoff_comment(browsing, row, reply, target, thread, material, keys, event=event)
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
