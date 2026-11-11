"""Remove empty scheduler debris while retaining content and publication history."""

import hashlib
import json
import time
from collections import Counter, defaultdict

from .context_delivery import DELIVERY_SCOPES, delivered_history, operation_delivery
from .operations_control import canonical_intent
from .store import dump


def empty_interrupted_rounds(store):
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "browsing_rounds" not in tables:
        return []
    candidates = []
    for row in store.all("SELECT * FROM browsing_rounds WHERE status IN ('cancelled','failed','partial')"):
        if json.loads(row["config"]).get("batch_id"):
            continue
        if store.one("SELECT 1 FROM browsing_intents WHERE round_id=?", (row["id"],)):
            continue
        if "operation_continuations" in tables and store.one(
            "SELECT 1 FROM operation_continuations WHERE parent_round_id=?", (row["id"],)
        ):
            continue
        tasks = store.all("SELECT * FROM browsing_tasks WHERE round_id=?", (row["id"],))
        if any(
            task["material_key"]
            or json.loads(task["payload"]).get("decisions")
            or task["status"] not in {"cancelled", "failed"}
            for task in tasks
        ):
            continue
        task_ids = {task["id"] for task in tasks}
        if "browsing_video_retries" in tables and any(
            retry["task_id"] in task_ids for retry in store.all("SELECT task_id FROM browsing_video_retries")
        ):
            continue
        candidates.append(row["id"])
    return candidates


def prune_empty_interrupted_rounds(store):
    """Recheck under a writer transaction; never remove recoverable work."""
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        identities = empty_interrupted_rounds(store)
        for identity in identities:
            store.db.execute("DELETE FROM browsing_tasks WHERE round_id=?", (identity,))
            store.db.execute("DELETE FROM browsing_rounds WHERE id=?", (identity,))
    return identities


def archive_character_version(store, character_id, version, reason):
    """Hide retired cards from selection; historical provenance stays readable."""
    store.character(character_id, version)
    if store.get("active_character") == character_id:
        active = store.character(character_id)
        if active["version"] == version:
            raise ValueError("当前使用中的人设不能归档")
    store.execute(
        "INSERT OR IGNORE INTO character_archives VALUES(?,?,?,?)",
        (character_id, version, reason, time.time()),
    )


def archive_browsing_history(store, *, before=None):
    """Retire old execution records; keep source identities, receipts and quotas."""
    before = time.time() if before is None else before
    with store.db:
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        if store.one("SELECT 1 FROM browsing_rounds WHERE status IN ('running','queued')") or store.one(
            "SELECT 1 FROM browsing_intents WHERE status='publishing'"
        ):
            raise ValueError("请先停止当前浏览任务后清理记录")
        args = (before,)
        ids = "SELECT id FROM browsing_rounds WHERE created_at<?"
        counts = {}
        counts["drafts"] = db.execute(
            f"UPDATE browsing_intents SET status='archived' WHERE round_id IN ({ids}) "
            "AND status IN ('pending_review','automatic_pending','automatic_skipped','rejected')",
            args,
        ).rowcount
        db.execute(
            f"DELETE FROM browsing_video_retries WHERE task_id IN (SELECT id FROM browsing_tasks WHERE round_id IN ({ids}))",
            args,
        )
        db.execute(
            f"UPDATE operation_continuations SET state='abandoned',owner=NULL,lease_until=NULL "
            f"WHERE channel='generation.browsing' AND parent_round_id IN ({ids}) "
            "AND state NOT IN ('completed','abandoned')",
            args,
        )
        db.execute(f"UPDATE browsing_batches SET state='archived' WHERE round_id IN ({ids})", args)
        db.execute(
            f"UPDATE browsing_batch_items SET status='archived' WHERE batch_id IN "
            f"(SELECT id FROM browsing_batches WHERE round_id IN ({ids}))",
            args,
        )
        db.execute("DELETE FROM browsing_title_decisions WHERE updated_at<?", args)
        counts["tasks"] = db.execute(
            f"UPDATE browsing_tasks SET status='archived' WHERE round_id IN ({ids})", args
        ).rowcount
        counts["rounds"] = db.execute(
            "UPDATE browsing_rounds SET status='archived' WHERE created_at<?", args
        ).rowcount
        db.execute(
            "INSERT OR REPLACE INTO kv VALUES('browsing_history_visible_since',?)", (json.dumps(before),)
        )
    return counts


_RECEIPT = "COALESCE(receipt,'') NOT IN ('','null','{}')"
_IN_FLIGHT = "('published','uncertain','publish_uncertain','sending','publishing','reserved')"


def _protected_write(store, tables, table, row):
    attempts = {"drafts": "publish_attempts", "dm_drafts": "dm_attempts"}.get(table)
    if attempts in tables and store.one(
        f"SELECT 1 FROM {attempts} WHERE draft_id=? AND (status IN {_IN_FLIGHT} OR {_RECEIPT})", (row["id"],)
    ):
        return True
    if "operation_write_attempts" not in tables:
        return bool(row.get("attempted_at"))
    channel = "messages" if table == "dm_drafts" else "dynamics" if table == "dynamic_posts" else "comments"
    key = row.get("source_key")
    if table == "drafts":
        original = store.one("SELECT * FROM comments WHERE id=?", (row["comment_id"],))
        if original:
            key = canonical_intent(original["platform"], original["video_id"], original["comment_id"], row["character_id"])
    if table == "dm_drafts":
        if row["source_kind"] == "comment":
            original = store.one("SELECT * FROM comments WHERE id=?", (row["source_id"],))
            if original:
                key = canonical_intent(row["platform"], original["video_id"], original["comment_id"], row["character_id"])
        elif "dm_inbox" in tables:
            original = store.one("SELECT message_id FROM dm_inbox WHERE id=?", (row["source_id"],))
            if original:
                key = canonical_intent(row["platform"], "dm", original["message_id"], row["character_id"])
    if table == "dynamic_posts":
        key = canonical_intent("bilibili", "dynamic", row["content_hash"], row["character_id"])
        attempts = store.all(
            "SELECT w.resolved_status,w.receipt FROM operation_write_attempts w "
            "LEFT JOIN operation_write_bindings b ON b.root_token=w.root_token "
            "WHERE w.channel=? AND w.account_id=? AND w.character_id=? "
            "AND (w.intent=? OR CAST(json_extract(b.binding_json,'$.target.post_id') AS TEXT)=?)",
            (channel, row.get("account_id") or "", row["character_id"], key, str(row["id"])),
        )
        # A historical attempt without a provably final rejection is retained.
        return any(attempt["resolved_status"] != "rejected" or attempt["receipt"] for attempt in attempts) or bool(
            row.get("attempted_at") and not attempts
        )
    return bool(key and store.one(
        "SELECT 1 FROM operation_write_attempts WHERE channel=? AND account_id=? AND character_id=? AND intent=? "
        f"AND (resolved_status IN {_IN_FLIGHT} OR {_RECEIPT})",
        (channel, row.get("account_id") or "", row["character_id"], key),
    ))


def _operation_cleanup_plan(store):
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_schema WHERE type IN ('table','view')")}
    counts, changes, traces = Counter(), [], set()

    def change(table, sql, args, count=1):
        if count:
            counts[table] += count
            changes.append((sql, args))

    def delete(table, clause, args):
        if table in tables:
            n = store.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {clause}", args)["n"]
            change(table, f"DELETE FROM {table} WHERE {clause}", args, n)

    def generation_trace(row, channel, event_id):
        if "operation_continuations" in tables:
            traces.update(item["id"] for item in store.all(
                "SELECT id FROM operation_continuations WHERE channel=? AND json_extract(binding,'$.source.event_id')=? "
                "AND json_extract(binding,'$.character.id')=? AND json_extract(binding,'$.character.version')=? "
                "AND COALESCE(json_extract(binding,'$.source.account_id'),'')=?",
                (channel, event_id, row["character_id"], row["character_version"], row.get("account_id") or ""),
            ))

    sources_to_expire, browsing_rejections = set(), defaultdict(set)
    for table, channel in (("drafts", "comments"), ("dm_drafts", "messages"),
                           ("dynamic_posts", "dynamics"), ("browsing_intents", "browsing")):
        if table not in tables:
            continue
        for row in store.all(f"SELECT * FROM {table} WHERE status='rejected' AND NOT ({_RECEIPT})"):
            identity = row["id"]
            if _protected_write(store, tables, table, row):
                continue
            if "operation_continuations" in tables:
                traces.update(item["id"] for item in store.all(
                    "SELECT id FROM operation_continuations WHERE channel=? AND entity_id=?", (channel, str(identity))
                ))
            if table == "drafts":
                sources_to_expire.add(f"reply:{identity}")
                delete("publish_attempts", "draft_id=?", (identity,))
                generation_trace(row, "generation.comment_draft", f"comment:{row['comment_id']}")
            elif table == "dm_drafts":
                delete("dm_attempts", "draft_id=?", (identity,))
                delete("meme_pins", "owner=?", (f"dm-draft:{identity}",))
                generation_trace(row, "generation.direct_message", f"dm:{identity}")
                traces.add(row.get("generation_continuation_id"))
            elif table == "dynamic_posts":
                delete("meme_pins", "owner=?", (f"dynamic-draft:{identity}",))
                decision = store.one("SELECT * FROM dynamic_decisions WHERE id=? AND post_id=?", (row["decision_id"], identity))
                if decision:
                    traces.add(decision.get("generation_continuation_id"))
                    change("dynamic_decisions", "UPDATE dynamic_decisions SET status='rejected',content='',post_id=NULL,"
                           "generation_continuation_id=NULL WHERE id=?", (decision["id"],))
            else:
                target = json.loads(row["target"] or "{}")
                traces.update(_clear_rejected_browsing_decision(store, tables, row, target, change))
                browsing_rejections[row["round_id"]].add(identity)
                delete("browsing_reply_coverage", "intent_id=?", (identity,))
            delete(table, "id=?", (identity,))

    if "browsing_tasks" in tables:
        for round_id, rejected in browsing_rejections.items():
            for task in store.all("SELECT id,payload FROM browsing_tasks WHERE round_id=?", (round_id,)):
                payload = json.loads(task["payload"])
                original = dict(payload)
                if payload.get("intent_id") in rejected:
                    payload.pop("intent_id")
                    payload.pop("response_decision", None)
                for field in ("like_intent_id", "favorite_intent_id"):
                    if payload.get(field) in rejected:
                        payload.pop(field)
                if "reply_intent_ids" in payload:
                    payload["reply_intent_ids"] = [value for value in payload["reply_intent_ids"] if value not in rejected]
                if payload != original:
                    change("browsing_task_draft_links", "UPDATE browsing_tasks SET payload=? WHERE id=?", (dump(payload), task["id"]))

    for identity in traces - {None, ""}:
        if "comments" in tables and any(row[1] == "generation_continuation_id" for row in store.db.execute("PRAGMA table_info(comments)")):
            n = store.one("SELECT COUNT(*) AS n FROM comments WHERE generation_continuation_id=?", (identity,))["n"]
            change("comment_resume_links", "UPDATE comments SET generation_continuation_id=NULL WHERE generation_continuation_id=?", (identity,), n)
        delete("operation_continuation_steps", "continuation_id=?", (identity,))
        delete("operation_continuation_budgets", "id=?", (identity,))
        delete("operation_continuations", "id=?", (identity,))

    unsent_by_scope = defaultdict(set)
    if "grounded_memory" in tables:
        for row in store.all("SELECT * FROM grounded_memory WHERE role='assistant'"):
            if row["scope"].startswith(DELIVERY_SCOPES) and not operation_delivery(
                store, row["scope"], {"id": row["character_id"], "version": row["character_version"]},
                row["event_id"], row["text"], legacy=True,
            ):
                identity = row["id"]
                sources_to_expire.add(f"memory:{identity}")
                unsent_by_scope[(row["scope"], row["character_id"], row["character_version"])].add(row["text"])
                delete("dynamic_memory_public", "memory_id=?", (identity,))
                delete("grounded_memory", "id=?", (identity,))

    for table, key in (("context_windows", "key"), ("context_history_audit", "window_key")):
        if table not in tables:
            continue
        for row in store.all(f"SELECT * FROM {table}"):
            scope, character, version = json.loads(row[key])
            history = json.loads(row["history"])
            cleaned = delivered_history(store, scope, {"id": character, "version": version}, history, legacy=True)
            # Old unbound summaries are excluded from model input. Only erase one
            # here when an unsent source is demonstrably copied into it.
            unsent = unsent_by_scope[(scope, character, version)] | {
                item["content"] for item in history if item.get("role") == "assistant" and item not in cleaned
            }
            cleaned = [item for item in history if item in cleaned or (
                item.get("role") != "assistant" and not any(body and body in item["content"] for body in unsent)
            )]
            if cleaned == history:
                continue
            raw = dump(cleaned)
            if table == "context_windows":
                change(table, "UPDATE context_windows SET history=?,input_tokens=0,carried_prefix_tokens=0 WHERE key=?", (raw, row[key]))
            else:
                digest = hashlib.sha256(raw.encode()).hexdigest()
                change(table, "UPDATE OR IGNORE context_history_audit SET history=?,history_sha256=? WHERE id=?", (raw, digest, row["id"]))
                changes.append(("DELETE FROM context_history_audit WHERE id=? AND history_sha256<>?", (row["id"], digest)))

    if "dynamic_memory_summaries" in tables and sources_to_expire:
        for row in store.all("SELECT * FROM dynamic_memory_summaries"):
            if any(entry.get("source_id") in sources_to_expire for entry in json.loads(row["entries"])):
                delete("dynamic_memory_summaries", "character_id=? AND character_version=?",
                       (row["character_id"], row["character_version"]))
    return dict(counts), changes


def _clear_rejected_browsing_decision(store, tables, row, target, change):
    bvid = target.get("bvid")
    traces = {target.get("generation_continuation_id")}
    scope = (row["account_id"], row["character_id"], bvid)
    where = "account_id=? AND character_id=? AND bvid=?"
    if row["kind"] == "top_level" and "browsing_video_progress" in tables:
        saved = store.one(f"SELECT main_decision FROM browsing_video_progress WHERE {where}", scope)
        if saved and saved["main_decision"]:
            decision = json.loads(saved["main_decision"])
            if decision and decision.get("action") == "reply_public":
                decision.update(action="read_comments", text="")
                change("browsing_video_decisions", f"UPDATE browsing_video_progress SET main_decision=? WHERE {where}", (dump(decision), *scope))
    if row["kind"] == "reply_public" and "browsing_comment_progress" in tables:
        parents = {str(target.get("parent") or ""), *map(str, target.get("covered_comment_ids") or [])}
        for parent in parents:
            saved = store.one(f"SELECT decision FROM browsing_comment_progress WHERE {where} AND comment_id=?", (*scope, parent))
            if saved:
                decision = json.loads(saved["decision"])
                if decision.get("action") == "reply_public":
                    decision.update(action="skip", text="")
                    traces.add((decision.pop("generation", None) or {}).get("id"))
                    change("browsing_comment_decisions", f"UPDATE browsing_comment_progress SET decision=? WHERE {where} AND comment_id=?", (dump(decision), *scope, parent))
    return traces


def preview_rejected_operations(store):
    """Count old rejected drafts and non-delivered operation memory changes without writing."""
    return _operation_cleanup_plan(store)[0]


def prune_rejected_operations(store):
    """Apply the same cleanup plan to an idle, backed-up operations database."""
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        counts, changes = _operation_cleanup_plan(store)
        for sql, args in changes:
            store.db.execute(sql, args)
    return counts
