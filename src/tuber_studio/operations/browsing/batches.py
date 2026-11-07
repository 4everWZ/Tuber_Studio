"""Durable retrieval batches and their independent video/comment work items."""

import asyncio
import hashlib
import json
import time
from contextlib import nullcontext

import psutil

from ...generation_meter import BrowsingBudgetDeferred
from ...operation_turns import operation_lane
from ...providers import public_error
from ...store import dump
from .round_context import discovery_retry, read_round


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

    def save_plan(self, row, plan):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._save_plan(row, plan)

    def _save_plan(self, row, plan):
        identity = row["config"]["batch_id"]
        selected = {bvid: position for position, bvid in enumerate(plan["videos"])}
        choices = {item["bvid"]: item for item in plan.get("choices", [])}
        for candidate in self.candidates(row):
            bvid = candidate["bvid"]
            choice = choices.get(bvid) or {
                "bvid": bvid,
                "action": "watch" if bvid in selected else "skip",
                "explanation": plan.get("explanation", ""),
            }
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

    def task(self, row, bvid, task_id):
        identity = row["config"].get("batch_id")
        if identity:
            self.store.execute(
                "UPDATE browsing_batch_items SET task_id=?,status='running' WHERE batch_id=? AND bvid=?",
                (task_id, identity, bvid),
            )

    def comment_visit(self, row, bvid):
        item = self.store.one(
            "SELECT payload FROM browsing_batch_items WHERE batch_id=? AND bvid=?",
            (row["config"]["batch_id"], bvid),
        )
        if not item:
            return None
        candidate = json.loads(item["payload"]) | {"task_kind": "comment_visit"}
        self.store.execute(
            "UPDATE browsing_batch_items SET kind='comment_visit',status='selected',task_id=NULL,payload=? WHERE batch_id=? AND bvid=?",
            (dump(candidate), row["config"]["batch_id"], bvid),
        )
        return candidate

    def waiting_until(self, row, bvid):
        value = self.store.one(
            "SELECT MAX(json_extract(i.payload,'$.next_at')) AS next_at FROM browsing_batch_items i "
            "JOIN browsing_batches b ON b.id=i.batch_id JOIN browsing_rounds r ON r.id=b.round_id "
            "WHERE r.account_id=? AND r.character_id=? AND i.bvid=? AND i.status='daily_wait'",
            (row["account_id"], row["character_id"], bvid),
        )
        return value["next_at"] if value and value["next_at"] and value["next_at"] > time.time() else None

    def outcome(self, task_id, status, error=None, payload=None):
        item = self.store.one("SELECT * FROM browsing_batch_items WHERE task_id=?", (task_id,))
        if not item:
            return
        state = (
            "completed"
            if status in {"selected", "viewed_without_comment", "comments_checked", "skipped"}
            else "paused"
        )
        if status == "deferred_daily":
            state = "daily_wait"
        current = json.loads(item["payload"])
        if payload:
            current |= payload
        self.store.execute(
            "UPDATE browsing_batch_items SET status=?,error=?,payload=? WHERE task_id=?",
            (state, error, dump(current), task_id),
        )
        if error:
            signature = hashlib.sha256(error.encode()).hexdigest()
            self.store.execute(
                "INSERT INTO browsing_batch_failures VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(batch_id,bvid,signature) DO UPDATE SET count=count+1,updated_at=excluded.updated_at",
                (item["batch_id"], item["bvid"], signature, error, time.time()),
            )

    def finish(self, row, paused=False):
        identity = row["config"].get("batch_id")
        if not identity:
            return False
        remaining = self.store.one(
            "SELECT COUNT(*) AS n FROM browsing_batch_items WHERE batch_id=? AND status NOT IN ('completed','skipped','later','daily_wait')",
            (identity,),
        )["n"]
        batch = self.get(row)
        state = "paused" if remaining or batch["plan"] is None else "completed"
        self.store.execute(
            "UPDATE browsing_batches SET state=?,updated_at=? WHERE id=?", (state, time.time(), identity)
        )
        return state == "paused"


async def execute(browsing, row, notifications_only):
    identity, started = row["id"], time.monotonic()
    token = read_round.set(identity)
    lane = operation_lane.set("video")
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
            if row["config"].get("interaction_only"):
                await browsing._run_interactions(row, metrics)
                status = "partial" if metrics["failures"] else "completed"
                return
            discovery_only = bool(row["config"].get("discovery_id"))
            poll_interactions = not (discovery_only or row["config"].get("video_only")) and (
                not row["preview"] or notifications_only or not browsing.operations.policy()["enabled"]
            )
            if poll_interactions:
                await browsing.sync_notifications(row)
                await browsing._process_events(row, metrics)
            if not notifications_only or browsing.store.get(
                "browsing_discovery_pending:" + row["account_id"], False
            ):
                browsing.store.set("browsing_discovery_pending:" + row["account_id"], False)
                candidates = await browsing._discover(row)
                metrics["workers"] = row["config"].get("batch_workers", row["config"]["workers"])
                semaphore = asyncio.Semaphore(row["config"].get("batch_workers", row["config"]["workers"]))
                if discovery_only or row["config"].get("autonomous_batch"):
                    async with asyncio.TaskGroup() as group:
                        for candidate in candidates:
                            group.create_task(
                                browsing._process_discovery_video(row, candidate, metrics, semaphore)
                            )
                    await browsing._finish_comment_visits(row)
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
                        result_lane = operation_lane.set(
                            "reply" if result.get("task_kind") == "comment_visit" else "video"
                        )
                        try:
                            await browsing.decisions._evaluate_task(result, row, metrics)
                        finally:
                            operation_lane.reset(result_lane)
                if poll_interactions:
                    await browsing._process_events(row, metrics)
                await browsing._finish_comment_visits(row)
            status = "partial" if metrics["failures"] else "completed"
    except (asyncio.CancelledError, Exception) as error:
        stopping_discovery = (
            browsing.discovery.state == "stopping" and browsing.discovery.round_id == identity
        )
        if isinstance(error, asyncio.CancelledError) or stopping_discovery:
            # A candidate may see revoked authorization before TaskGroup delivers cancellation.
            status, task_error = "cancelled", "父轮取消"
        else:
            status = "partial" if metrics["viewed"] or metrics["prepared"] else "failed"
            browsing.last_error = round_error = task_error = public_error(error)
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='cancelled',error=? WHERE round_id=? AND status IN ('discovered','claimed','preparing','evidence_ready')",
            (task_error, identity),
        )
    finally:
        children = browsing.comment_children.pop(identity, set())
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
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
        metrics.update(browsing.repo.round(identity)["metrics"])
        browsing.repo.finish_round(identity, status, metrics, round_error)
        operation_lane.reset(lane)
        read_round.reset(token)


async def sample_resources(browsing, metrics):
    while True:
        metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
        used = psutil.virtual_memory().used
        metrics["peak_system_ram_bytes"] = max(metrics.get("peak_system_ram_bytes", 0), used)
        await asyncio.sleep(0.1)


async def send_discovery_intent(browsing, identity, row):
    browsing.discovery.verify(row["config"]["discovery_id"])
    with browsing.discovery.performing("publishing"):
        result = await browsing.publish(identity, automatic=True)
    state = browsing.store.one("SELECT status FROM browsing_intents WHERE id=?", (identity,))["status"]
    if state == "automatic_pending":
        raise ValueError("平台拒绝，已准备修订" if result["status"] == "revised" else "回应修订尚未完成")


async def resume_discovery_writes(browsing, row, bvid):
    if not row["config"].get("discovery_id"):
        return
    for item in browsing.store.all(
        """SELECT i.id,i.status FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id
           WHERE i.account_id=? AND i.character_id=? AND i.character_version=?
           AND json_extract(i.target,'$.bvid')=? AND json_extract(r.config,'$.discovery_publish')=1
           AND (i.status='publish_uncertain' OR (i.status='automatic_pending' AND
               json_extract(r.config,'$.discovery_id')=?)) ORDER BY i.created_at""",
        (*browsing.repo.retry_scope(row), bvid, row["config"]["discovery_id"]),
    ):
        if item["status"] == "publish_uncertain":
            browsing.discovery.verify(row["config"]["discovery_id"])
            with browsing.discovery.performing("reconciling"):
                result = await browsing.publication._reconcile(item["id"])
            if result is None:
                raise ValueError("原提交回执尚待确认")
        else:
            await browsing._send_discovery_intent(item["id"], row)


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
        lane = operation_lane.set("reply" if revisit else "video")
        try:
            await browsing._run_discovery_video(row, candidate, metrics)
        finally:
            operation_lane.reset(lane)


async def defer_comment_visit(browsing, row, bvid, metrics):
    if not row["config"].get("batch_id"):
        return
    key = (row["account_id"], row["character_id"], bvid)
    existing = browsing.comment_active.get(key)
    if existing and not existing.done():
        return
    candidate = browsing.repo.batches.comment_visit(row, bvid)
    if not candidate:
        return
    task = asyncio.create_task(
        browsing._process_discovery_video(row, candidate, metrics, asyncio.Semaphore(1))
    )
    browsing.comment_active[key] = task
    browsing.comment_children.setdefault(row["id"], set()).add(task)
    task.add_done_callback(
        lambda done: (
            browsing.comment_active.pop(key, None) if browsing.comment_active.get(key) is done else None
        )
    )


async def finish_comment_visits(browsing, row):
    children = browsing.comment_children.get(row["id"], set())
    if children:
        await asyncio.gather(*children)


async def run_discovery_video(browsing, row, candidate, metrics):
    bvid = candidate["bvid"]
    saved = browsing.repo.video_retry(row, bvid)
    failures = saved["failures"] if saved else 0
    error = saved["error"] if saved else None
    while failures < 3:
        browsing._check_round(row)
        identity = browsing.repo.task(row["id"], candidate)
        token = discovery_retry.set(
            {"bvid": bvid, "failures": failures, "error": error, "resume": bool(saved or failures)}
        )
        try:
            try:
                if failures or saved or row["config"].get("batch_id"):
                    await browsing._resume_discovery_writes(row, bvid)
            except BrowsingBudgetDeferred as failure:
                browsing.store.execute(
                    "UPDATE browsing_tasks SET status='checkpoint',error=? WHERE id=?",
                    (public_error(failure), identity),
                )
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
        finally:
            discovery_retry.reset(token)
        browsing._check_round(row)
        task = browsing.store.one("SELECT status,error,payload FROM browsing_tasks WHERE id=?", (identity,))
        payload = json.loads(task["payload"])
        reason = payload.get("reason")
        if payload.get("retry_kind") == "round_budget":
            browsing.repo.defer_video(row, bvid, identity, failures)
            return
        if task["status"] == "checkpoint":
            browsing.repo.finish_video_attempt(row, candidate, identity, 0)
            return
        if task["status"] == "deferred_daily":
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
        if failures >= 2:
            if failures == 3 and row["config"].get("discovery_id"):
                browsing.store.execute(
                    """UPDATE browsing_intents SET status='automatic_skipped',error=?
                       WHERE account_id=? AND character_id=? AND character_version=? AND status='automatic_pending'
                       AND json_extract(target,'$.bvid')=? AND round_id IN
                       (SELECT id FROM browsing_rounds WHERE json_extract(config,'$.discovery_id')=?)""",
                    (error, *browsing.repo.retry_scope(row), bvid, row["config"]["discovery_id"]),
                )
            return
