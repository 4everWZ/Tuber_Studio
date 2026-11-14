"""Durable retrieval batches and their independent video/comment work items."""

import asyncio
import hashlib
import json
import time
from contextlib import nullcontext

import psutil

from ...generation_meter import BrowsingBudgetDeferred
from ...operation_turns import operation_checkpoint, operation_lane
from ...providers import public_error
from ...publication_errors import SendLimitReached
from ...store import dump
from . import full_video
from .quotas import DailyReplyLimitReached
from .round_context import read_round


class BrowsingBatches:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS browsing_batches(
                id TEXT PRIMARY KEY,round_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,
                retrieval TEXT NOT NULL DEFAULT '{}',plan TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS browsing_batch_items(
                batch_id TEXT NOT NULL,bvid TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
                status TEXT NOT NULL,position INTEGER NOT NULL,task_id TEXT,error TEXT,
                PRIMARY KEY(batch_id,bvid));
            CREATE INDEX IF NOT EXISTS browsing_batch_pending ON browsing_batch_items(batch_id,status,position);
            CREATE TABLE IF NOT EXISTS browsing_batch_comment_pages(
                batch_id TEXT NOT NULL,bvid TEXT NOT NULL,document TEXT NOT NULL,
                PRIMARY KEY(batch_id,bvid));
            CREATE TABLE IF NOT EXISTS browsing_comment_revisits(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,character_version INTEGER NOT NULL,
                review_session TEXT NOT NULL,bvid TEXT NOT NULL,round_id TEXT NOT NULL,
                page INTEGER,next_at REAL NOT NULL,error TEXT,
                PRIMARY KEY(account_id,character_id,character_version,review_session,bvid));
            CREATE TABLE IF NOT EXISTS browsing_batch_comment_events(
                batch_id TEXT NOT NULL,event_id TEXT NOT NULL,
                PRIMARY KEY(batch_id,event_id));
            CREATE TABLE IF NOT EXISTS browsing_title_decisions(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,bvid TEXT NOT NULL,
                title_hash TEXT NOT NULL,decision TEXT NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(account_id,character_id,bvid));
            CREATE TABLE IF NOT EXISTS browsing_batch_failures(
                batch_id TEXT NOT NULL,bvid TEXT NOT NULL,signature TEXT NOT NULL,
                error TEXT NOT NULL,count INTEGER NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(batch_id,bvid,signature));
        """)

    def ensure(self, row):
        identity = row["config"].get("batch_id")
        if not identity:
            return None
        now = time.time()
        self.store.execute(
            "INSERT OR IGNORE INTO browsing_batches VALUES(?,?,?,'{}',NULL,?,?)",
            (identity, row["id"], "retrieving", now, now),
        )
        return self.get(row)

    def get(self, row):
        identity = row["config"].get("batch_id")
        value = self.store.one("SELECT * FROM browsing_batches WHERE id=?", (identity,)) if identity else None
        if value:
            value["retrieval"] = json.loads(value["retrieval"])
            value["plan"] = json.loads(value["plan"]) if value["plan"] else None
        return value

    def retrieval(self, row, state):
        self.store.execute(
            "UPDATE browsing_batches SET retrieval=?,updated_at=? WHERE id=?",
            (dump(state), time.time(), row["config"]["batch_id"]),
        )

    @staticmethod
    def title_hash(candidate):
        return hashlib.sha256(str(candidate.get("title") or "").encode()).hexdigest()

    def title_skipped(self, row, candidate):
        if row["config"].get("force_read"):
            return False
        return bool(
            self.store.one(
                "SELECT 1 FROM browsing_title_decisions WHERE account_id=? AND character_id=? AND bvid=? "
                "AND title_hash=? AND decision='skipped'",
                (row["account_id"], row["character_id"], candidate["bvid"], self.title_hash(candidate)),
            )
        )

    def merge(self, row, candidates):
        batch_id = row["config"]["batch_id"]
        for candidate in candidates:
            existing = self.store.one(
                "SELECT payload FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
                (batch_id, candidate["bvid"]),
            )
            progress = self.store.one(
                "SELECT 1 FROM browsing_video_progress WHERE account_id=? AND character_id=? AND bvid=?",
                (row["account_id"], row["character_id"], candidate["bvid"]),
            )
            kind = "comment_visit" if progress else "new_video"
            if existing:
                prior = json.loads(existing["payload"])
                sources = [dump(source) for source in prior.get("sources", []) + candidate.get("sources", [])]
                candidate = (
                    prior | candidate | {"sources": [json.loads(value) for value in dict.fromkeys(sources)]}
                )
                self.store.execute(
                    "UPDATE browsing_batch_items SET payload=? WHERE batch_id=? AND bvid=?",
                    (dump(candidate), batch_id, candidate["bvid"]),
                )
            else:
                position = self.store.one(
                    "SELECT COUNT(*) AS n FROM browsing_batch_items WHERE batch_id=?", (batch_id,)
                )["n"]
                self.store.execute(
                    "INSERT INTO browsing_batch_items VALUES(?,?,?,?,?,?,NULL,NULL)",
                    (batch_id, candidate["bvid"], kind, dump(candidate), "candidate", position),
                )

    def candidates(self, row, *, pending=False):
        condition = (
            "AND (status IN ('selected','running','paused','failed') OR "
            "(status='daily_wait' AND COALESCE(json_extract(payload,'$.next_at'),0)<=?))"
            if pending
            else ""
        )
        return [
            json.loads(item["payload"]) | {"task_kind": item["kind"]}
            for item in self.store.all(
                "SELECT * FROM browsing_batch_items WHERE batch_id=? "
                + condition
                + " ORDER BY position,bvid",
                (row["config"]["batch_id"], time.time()) if pending else (row["config"]["batch_id"],),
            )
        ]

    def deferred_candidates(self, row):
        items = self.store.all(
            "SELECT i.payload FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id "
            "JOIN browsing_rounds r ON r.id=b.round_id WHERE r.account_id=? AND r.character_id=? "
            "AND i.status='later' AND b.state='completed' ORDER BY b.updated_at",
            (row["account_id"], row["character_id"]),
        )
        return list(
            {item["bvid"]: item for value in items if (item := json.loads(value["payload"]))}.values()
        )

    def save_plan(self, row, plan, *, presented=None):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._save_plan(row, plan, presented=presented)

    def _save_plan(self, row, plan, *, presented=None):
        identity = row["config"]["batch_id"]
        selected = {bvid: position for position, bvid in enumerate(plan["videos"])}
        choices = {item["bvid"]: item for item in plan.get("choices", [])}
        presented = set(presented) if presented is not None else None
        item_states = {
            item["bvid"]: item["status"]
            for item in self.store.all(
                "SELECT bvid,status FROM browsing_batch_items WHERE batch_id=?", (identity,)
            )
        }
        for candidate in self.candidates(row):
            bvid = candidate["bvid"]
            if presented is not None and bvid not in presented:
                if item_states[bvid] not in {"completed", "skipped", "later", "daily_wait"}:
                    candidate["queue_reason"] = "超出本次候选窗口，保留到后续批次"
                    self.store.db.execute(
                        "UPDATE browsing_batch_items SET status='later',payload=? WHERE batch_id=? AND bvid=?",
                        (dump(candidate), identity, bvid),
                    )
                continue
            choice = choices.get(bvid) or {
                "bvid": bvid,
                "action": "watch" if bvid in selected else "skip",
                "explanation": plan.get("explanation", ""),
            }
            choice = dict(choice)
            choice.setdefault("decided_at", time.time())
            state = "selected" if bvid in selected else "later" if choice["action"] == "later" else "skipped"
            kind = (
                "comment_visit" if choice["action"] == "comments" else candidate.get("task_kind", "new_video")
            )
            candidate["selection"] = choice
            self.store.db.execute(
                "UPDATE browsing_batch_items SET status=?,position=?,kind=?,payload=? WHERE batch_id=? AND bvid=?",
                (state, selected.get(bvid, len(selected)), kind, dump(candidate), identity, bvid),
            )
            if state != "later":
                self.store.db.execute(
                    "UPDATE browsing_batch_items SET status='completed' WHERE bvid=? AND status='later' "
                    "AND batch_id IN (SELECT b.id FROM browsing_batches b JOIN browsing_rounds r ON r.id=b.round_id WHERE r.account_id=? AND r.character_id=?)",
                    (bvid, row["account_id"], row["character_id"]),
                )
            self.store.db.execute(
                "INSERT OR REPLACE INTO browsing_title_decisions VALUES(?,?,?,?,?,?)",
                (
                    row["account_id"],
                    row["character_id"],
                    bvid,
                    self.title_hash(candidate),
                    state,
                    time.time(),
                ),
            )
        self.store.db.execute(
            "UPDATE browsing_batches SET plan=?,state=?,updated_at=? WHERE id=?",
            (dump(plan), "ready" if selected else "completed", time.time(), identity),
        )

    def omit_candidate(self, row, candidate, *, reason, next_at=None):
        payload = candidate | {"queue_reason": reason}
        if next_at is not None:
            payload["next_at"] = next_at
        self.store.execute(
            "UPDATE browsing_batch_items SET status=?,payload=? WHERE batch_id=? AND bvid=?",
            (
                "daily_wait" if next_at is not None else "skipped",
                dump(payload),
                row["config"]["batch_id"],
                candidate["bvid"],
            ),
        )

    def task(self, row, bvid, task_id, *, batch_ids=None):
        identities = batch_ids if batch_ids is not None else [row["config"].get("batch_id")]
        for identity in filter(None, identities):
            self.store.execute(
                "UPDATE browsing_batch_items SET task_id=?,status='running' WHERE batch_id=? AND bvid=?",
                (task_id, identity, bvid),
            )

    def has_open_round(self, row):
        return bool(self.store.one(
            "SELECT 1 FROM browsing_batches b JOIN browsing_rounds r ON r.id=b.round_id "
            "WHERE r.account_id=? AND r.character_id=? AND r.character_version=? "
            "AND r.preview=0 AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=? "
            "AND b.state IN ('retrieving','ready','paused') LIMIT 1",
            (row["account_id"], row["character_id"], row["character_version"],
             row["config"].get("review_session_id", "")),
        ))

    def pending_comment_events(self, row):
        from .comment_reader import eligible_event_heads, event_scope

        return self.store.one(
            eligible_event_heads() + """
            SELECT COUNT(*) AS n FROM heads
            WHERE position=1
              AND COALESCE(CASE WHEN json_valid(error) THEN json_extract(error,'$.next_at') END,0)<=:now
              AND EXISTS(
                  SELECT 1 FROM browsing_batch_comment_events item
                  JOIN browsing_events event ON event.id=item.event_id
                  WHERE item.batch_id=:batch AND event.status IN
                        ('received','source_verified','context_ready','reply_deferred')
                    AND json_extract(event.payload,'$.resource_type')=heads.resource_type
                    AND CAST(json_extract(event.payload,'$.oid') AS TEXT)=heads.resource
                    AND CAST(json_extract(event.payload,'$.root') AS TEXT)=heads.root
              )
            """, event_scope(row) | {"batch": row["config"]["batch_id"], "now": time.time()},
        )["n"]

    def pending_comment_visits(self, row):
        items = self.store.all(
            """
            WITH eligible AS (
                SELECT i.batch_id,i.bvid,i.payload,b.created_at,i.position
                FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id
                JOIN browsing_rounds r ON r.id=b.round_id
                LEFT JOIN browsing_tasks t ON t.id=i.task_id
                LEFT JOIN browsing_rounds owner ON owner.id=t.round_id
                WHERE r.account_id=:account AND r.character_id=:character
                  AND r.character_version=:version AND r.preview=:preview
                  AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=:review
                  AND i.kind='comment_visit'
                  AND NOT EXISTS(SELECT 1 FROM browsing_batch_comment_pages p
                                 WHERE p.batch_id=i.batch_id AND p.bvid=i.bvid)
                  AND (i.status IN ('selected','running','paused','failed')
                       OR i.status='daily_wait' AND COALESCE(json_extract(i.payload,'$.next_at'),0)<=:now)
                  AND (owner.id IS NULL OR owner.status NOT IN ('queued','running'))
            ), selected AS (
                SELECT bvid FROM eligible GROUP BY bvid ORDER BY MIN(created_at), MIN(position), bvid LIMIT :limit
            )
            SELECT e.* FROM eligible e JOIN selected s ON s.bvid=e.bvid ORDER BY e.created_at,e.position,e.bvid
            """,
            {"account": row["account_id"], "character": row["character_id"], "version": row["character_version"],
             "review": row["config"].get("review_session_id", ""), "preview": int(row["preview"]),
             "now": time.time(), "limit": row["config"]["workers"]},
        )
        candidates = {}
        for item in items:
            candidate = candidates.setdefault(item["bvid"], json.loads(item["payload"]) | {
                "task_kind": "comment_visit", "comment_batch_ids": [],
            })
            candidate["comment_batch_ids"].append(item["batch_id"])
        return list(candidates.values())

    def waiting_until(self, row, bvid):
        value = self.store.one(
            "SELECT MAX(json_extract(i.payload,'$.next_at')) AS next_at FROM browsing_batch_items i "
            "JOIN browsing_batches b ON b.id=i.batch_id JOIN browsing_rounds r ON r.id=b.round_id "
            "WHERE r.account_id=? AND r.character_id=? AND i.bvid=? AND i.status='daily_wait'",
            (row["account_id"], row["character_id"], bvid),
        )
        return value["next_at"] if value and value["next_at"] and value["next_at"] > time.time() else None

    def outcome(self, task_id, status, error=None, payload=None):
        items = self.store.all("SELECT * FROM browsing_batch_items WHERE task_id=?", (task_id,))
        for item in items:
            self._item_outcome(item, status, error, payload)

    def _item_outcome(self, item, status, error, payload):
        # A queue retries interrupted work, never a rejected model answer.
        # Keep the diagnostic on the task while settling its automatic work item.
        states = {
            "discovered": "selected",
            "claimed": "running", "preparing": "running", "evidence_ready": "running",
            "selected": "completed", "viewed_without_comment": "completed",
            "comments_checked": "completed", "skipped": "completed",
            "invalid": "completed", "awaiting_review": "completed",
            "deferred_daily": "daily_wait",
            "checkpoint": "paused", "failed": "paused", "cancelled": "paused",
            "paused": "paused", "retry_wait": "daily_wait",
        }
        state = states[status]
        current = json.loads(item["payload"])
        if payload:
            current |= payload
        self.store.execute(
            "UPDATE browsing_batch_items SET status=?,error=?,payload=? WHERE batch_id=? AND bvid=?",
            (state, error, dump(current), item["batch_id"], item["bvid"]),
        )
        if error:
            signature = hashlib.sha256(error.encode()).hexdigest()
            self.store.execute(
                "INSERT INTO browsing_batch_failures VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(batch_id,bvid,signature) DO UPDATE SET count=count+1,updated_at=excluded.updated_at",
                (item["batch_id"], item["bvid"], signature, error, time.time()),
            )

    def finish(self, row, *, status):
        identity = row["config"].get("batch_id")
        if not identity:
            return False
        remaining = self.store.one(
            "SELECT COUNT(*) AS n FROM browsing_batch_items WHERE batch_id=? "
            "AND status NOT IN ('completed','skipped','later','daily_wait')",
            (identity,),
        )["n"]
        batch = self.get(row)
        remaining += self.pending_comment_events(row)
        state = "invalid" if status == "invalid" else "paused" if remaining or batch["plan"] is None or status == "paused" else "completed"
        if state == "paused":
            self.store.execute(
                "UPDATE browsing_batch_items SET status='paused' WHERE batch_id=? "
                "AND status='running'",
                (identity,),
            )
        self.store.execute(
            "UPDATE browsing_batches SET state=?,updated_at=? WHERE id=?", (state, time.time(), identity)
        )
        return state == "paused"


async def execute(browsing, row, notifications_only):
    identity, started = row["id"], time.monotonic()
    token = read_round.set(identity)
    lane = operation_lane.set("priority" if row["config"].get("full_video") else "background")
    checkpoint = operation_checkpoint.set(lambda: browsing.check_step(row))
    metrics = {
        "workers": row["config"]["workers"],
        "prepared": 0,
        "failures": 0,
        "viewed": 0,
        "lock_wait_seconds": 0,
        "preparation_seconds": 0,
        "peak_rss_bytes": psutil.Process().memory_info().rss,
        "cache_hits_before": browsing.evidence.hits,
        "merged_before": browsing.evidence.merged,
    }
    browsing.store.execute("UPDATE browsing_rounds SET status='running' WHERE id=?", (identity,))
    round_error = None
    sampler = asyncio.create_task(browsing._sample_resources(metrics))
    try:
        async with asyncio.timeout_at(
            asyncio.get_running_loop().time() + max(0, row["deadline"] - time.time())
            if row["deadline"] is not None
            else None
        ):
            browsing._check_round(row)
            if row["config"].get("full_video"):
                status = await full_video.execute(browsing, row, metrics)
                return
            if row["config"].get("interaction_only"):
                await browsing._run_interactions(row, metrics)
                status = "partial" if metrics["failures"] else "completed"
                return
            video_only = bool(row["config"].get("video_only"))
            poll_interactions = not video_only and (
                not row["preview"] or notifications_only or not browsing.operations.policy()["enabled"]
            )
            if poll_interactions and not row["config"].get("autonomous_batch"):
                await browsing.sync_notifications(row)
                await browsing._process_events(row, metrics)
            if not notifications_only or browsing.store.get(
                "browsing_discovery_pending:" + row["account_id"], False
            ):
                browsing.store.set("browsing_discovery_pending:" + row["account_id"], False)
                candidates = await browsing._discover(row)
                metrics["workers"] = row["config"].get("batch_workers", row["config"]["workers"])
                semaphore = asyncio.Semaphore(row["config"].get("batch_workers", row["config"]["workers"]))
                if row["config"].get("autonomous_batch"):
                    async with asyncio.TaskGroup() as group:
                        for candidate in candidates:
                            group.create_task(
                                browsing._process_discovery_video(row, candidate, metrics, semaphore)
                            )
                    browsing.check_step(row)
                    while browsing.repo.batches.pending_comment_events(row):
                        browsing.event_changed.clear()
                        if not await browsing._process_events(row, metrics, batch_only=True):
                            browsing.check_step(row)
                            if not browsing.event_active:
                                break
                            await browsing.event_changed.wait()
                        browsing.check_step(row)
                    status = "partial" if metrics["failures"] else "completed"
                    return
                tasks = [browsing.repo.task(identity, c) for c in candidates]
                preparation_start = time.monotonic()
                async with asyncio.TaskGroup() as group:
                    children = [
                        group.create_task(browsing._prepare_task(i, row, semaphore, metrics)) for i in tasks
                    ]
                prepared = [child.result() for child in children]
                metrics["preparation_seconds"] = time.monotonic() - preparation_start
                for result in prepared:
                    if poll_interactions:
                        await browsing._process_events(row, metrics)
                    if result:
                        await browsing.decisions._evaluate_task(result, row, metrics)
                if poll_interactions:
                    await browsing._process_events(row, metrics)
            status = "partial" if metrics["failures"] else "completed"
    except (asyncio.CancelledError, Exception) as error:
        stopping = not row["preview"] and not browsing.operations.policy()["enabled"]
        if isinstance(error, asyncio.CancelledError) or stopping:
            status = "paused" if browsing.operations.draining and not browsing.operations.policy()["emergency"] else "cancelled"
            task_error = "运营已收尾，保留进度" if status == "paused" else "父轮取消"
        else:
            continuation = getattr(error, "continuation", None)
            status = (
                "invalid" if continuation and continuation["state"] == "invalid"
                else "partial" if metrics["viewed"] or metrics["prepared"] else "failed"
            )
            browsing.last_error = round_error = task_error = public_error(error)
        browsing.store.execute(
            "UPDATE browsing_tasks SET status=?,error=? WHERE round_id=? "
            "AND status IN ('discovered','claimed','preparing','evidence_ready')",
            ("paused" if status == "paused" else "cancelled", task_error, identity),
        )
    finally:
        browsing.comment_windows.pop(identity, None)
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        heartbeats = [task for task in browsing.heartbeats if task.get_name() == identity]
        for heartbeat in heartbeats:
            heartbeat.cancel()
        await asyncio.gather(*heartbeats, return_exceptions=True)
        browsing.heartbeats.difference_update(heartbeats)
        metrics["elapsed_seconds"] = time.monotonic() - started
        metrics["cache_hits"] = browsing.evidence.hits - metrics.pop("cache_hits_before")
        metrics["merged_reads"] = browsing.evidence.merged - metrics.pop("merged_before")
        metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
        metrics = browsing.repo.round(identity)["metrics"] | metrics
        browsing.repo.finish_round(identity, status, metrics, round_error)
        if row["config"].get("batch_id"):
            browsing.interactions_changed.set()
        operation_checkpoint.reset(checkpoint)
        operation_lane.reset(lane)
        read_round.reset(token)


async def sample_resources(browsing, metrics):
    while True:
        metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
        used = psutil.virtual_memory().used
        metrics["peak_system_ram_bytes"] = max(metrics.get("peak_system_ram_bytes", 0), used)
        await asyncio.sleep(0.1)


async def resume_batch_publications(browsing, row, bvid):
    for item in browsing.store.all(
        "SELECT id,status,kind FROM browsing_intents WHERE account_id=? AND character_id=? "
        "AND json_extract(target,'$.bvid')=? AND (status='publish_uncertain' OR "
        "(round_id=? AND character_version=? AND epoch=? AND status='pending_review' AND auto_authorized=1)) "
        "ORDER BY created_at",
        (row["account_id"], row["character_id"], bvid, row["id"], row["character_version"], row["epoch"]),
    ):
        if item["status"] == "publish_uncertain":
            try:
                await browsing.publication._reconcile(item["id"])
            except Exception as error:
                # The original intent stays fenced. A failed readback must not
                # block other comment targets on this video.
                browsing.store.execute(
                    "UPDATE browsing_intents SET error=? WHERE id=? AND status='publish_uncertain'",
                    (public_error(error), item["id"]),
                )
        elif browsing.publication._action_allowed(item["kind"], browsing.effective()):
            browsing.queue_publication(item["id"])


async def process_discovery_video(browsing, row, candidate, metrics, semaphore):
    revisit = candidate.get("task_kind") == "comment_visit" or (
        "task_kind" not in candidate and bool(browsing.repo.content_progress(row, candidate["bvid"]))
    )
    window = (
        browsing.comment_windows.setdefault(row["id"], asyncio.Semaphore(row["config"]["workers"]))
        if revisit
        else semaphore
    )
    async with window:
        browsing.check_step(row)
        lane = operation_lane.set("background")
        try:
            await browsing._run_discovery_video(row, candidate, metrics)
        finally:
            operation_lane.reset(lane)


async def process_comment_visits(browsing, row, metrics):
    if not (row["config"]["reply_enabled"] or row["config"].get("comment_like_enabled")):
        return
    async def visit(candidate, key):
        try:
            await browsing._process_discovery_video(row, candidate, metrics, asyncio.Semaphore(1))
        finally:
            browsing.comment_active.pop(key, None)

    for candidate in browsing.repo.batches.pending_comment_visits(row):
        browsing.check_step(row)
        if not browsing.page_capacity():
            break
        key = (row["account_id"], row["character_id"], row["character_version"],
               row["config"].get("review_session_id", ""), candidate["bvid"])
        if key not in browsing.comment_active:
            task = asyncio.create_task(visit(candidate, key))
            browsing.comment_active[key] = task
            await task
            break


async def run_discovery_video(browsing, row, candidate, metrics):
    """Execute one queue attempt; a later round may resume interrupted work."""
    bvid = candidate["bvid"]
    comment_batch_ids = candidate.get("comment_batch_ids")
    saved = None if comment_batch_ids else browsing.repo.video_retry(row, bvid)
    failures = saved["failures"] if saved else 0
    if failures >= 3:
        return
    browsing._check_round(row)
    identity = browsing.repo.task(row["id"], candidate)
    if comment_batch_ids:
        browsing.repo.batches.task(row, bvid, identity, batch_ids=comment_batch_ids)
    try:
        if failures or saved or row["config"].get("batch_id") or comment_batch_ids:
            await browsing._resume_batch_publications(row, bvid)
    except (DailyReplyLimitReached, SendLimitReached) as failure:
        payload = json.loads(
            browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                "payload"
            ]
        ) | {
            "retry_kind": "send_limit"
            if isinstance(failure, SendLimitReached)
            else "daily_reply_limit",
            "next_at": failure.next_at,
        }
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='deferred_daily',payload=?,error=NULL,updated_at=? WHERE id=?",
            (dump(payload), time.time(), identity),
        )
        browsing.repo.batches.outcome(identity, "deferred_daily", payload=payload)
        return
    except BrowsingBudgetDeferred as failure:
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='checkpoint',error=? WHERE id=?",
            (public_error(failure), identity),
        )
        if comment_batch_ids:
            browsing.repo.batches.outcome(identity, "checkpoint")
        else:
            browsing.repo.defer_video(row, bvid, identity, failures)
        return
    except Exception as failure:
        metrics["failures"] += 1
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='failed',error=? WHERE id=?",
            (public_error(failure), identity),
        )
    else:
        prepared = await browsing._prepare_task(identity, row, nullcontext(), metrics)
        if prepared:
            await browsing.decisions._evaluate_task(prepared, row, metrics)
    browsing._check_round(row)
    task = browsing.store.one("SELECT status,error,payload FROM browsing_tasks WHERE id=?", (identity,))
    payload = json.loads(task["payload"])
    reason = payload.get("reason")
    if comment_batch_ids:
        # A deferred reply remains on its original queue, never the watch retry registry.
        browsing.repo.batches.outcome(identity, task["status"], task["error"], payload)
        return
    if payload.get("retry_kind") == "round_budget":
        browsing.repo.defer_video(row, bvid, identity, failures)
        return
    if task["status"] in {"checkpoint", "invalid", "awaiting_review"}:
        browsing.repo.finish_video_attempt(row, candidate, identity, 0)
        return
    if task["status"] == "deferred_daily":
        return
    if payload.get("material_mode") == "unavailable":
        browsing.repo.finish_video_attempt(row, candidate, identity, failures + 1)
        return
    if task["status"] in {"selected", "viewed_without_comment", "comments_checked"} or (
        task["status"] == "skipped"
        and reason in {"no_subtitles", "empty", "filtered", "visual_unavailable"}
    ):
        browsing.repo.finish_video_attempt(row, candidate, identity, 0)
        return
    failures += 1
    error = task["error"]
    browsing.repo.finish_video_attempt(row, candidate, identity, failures)
    if failures == 3:
        browsing.store.execute(
            "UPDATE browsing_intents SET auto_authorized=0,error=? WHERE round_id=? "
            "AND json_extract(target,'$.bvid')=? AND status='pending_review'",
            (error, row["id"], bvid),
        )
