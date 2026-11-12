"""Transactional round ownership, bounded budgets and fenced task leases."""

import json
import time
from uuid import uuid4

from ...generation_meter import (
    BrowsingBudgetDeferred,
    GenerationBudgetExceeded,
    reserve_counters,
    settle_counters,
)
from ...store import dump
from .batches import BrowsingBatches
from .quotas import BrowsingQuotas


class BrowsingRepository:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS browsing_rounds(
                id TEXT PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,
                status TEXT,preview INTEGER,epoch INTEGER,owner TEXT,deadline REAL,
                config TEXT,budget TEXT,metrics TEXT,error TEXT,created_at REAL,updated_at REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS browsing_round_lane ON browsing_rounds(
                account_id, COALESCE(json_extract(config,'$.interaction_only'),0))
                WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS browsing_tasks(
                id TEXT PRIMARY KEY,round_id TEXT,bvid TEXT,status TEXT,owner TEXT,generation INTEGER DEFAULT 0,
                lease_until REAL DEFAULT 0,attempts INTEGER DEFAULT 0,input_version TEXT,material_key TEXT,
                payload TEXT,error TEXT,created_at REAL,updated_at REAL);
            CREATE INDEX IF NOT EXISTS browsing_tasks_round ON browsing_tasks(round_id,status);
            CREATE TABLE IF NOT EXISTS browsing_video_progress(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,bvid TEXT NOT NULL,
                material TEXT NOT NULL,decisions TEXT NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(account_id,character_id,bvid));
            CREATE TABLE IF NOT EXISTS browsing_video_retries(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,character_version INTEGER NOT NULL,
                bvid TEXT NOT NULL,failures INTEGER NOT NULL,task_id TEXT NOT NULL,
                PRIMARY KEY(account_id,character_id,character_version,bvid));
            CREATE TABLE IF NOT EXISTS browsing_materials(
                id TEXT PRIMARY KEY,bvid TEXT,path TEXT,bytes INTEGER,expires_at REAL,created_at REAL);
            CREATE TABLE IF NOT EXISTS browsing_intents(
                id TEXT PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,
                source_key TEXT,kind TEXT,round_id TEXT,epoch INTEGER,auto_authorized INTEGER,
                status TEXT,text TEXT,reason TEXT,target TEXT,evidence TEXT,material_keys TEXT,
                input_version TEXT,receipt TEXT,visibility TEXT,error TEXT,created_at REAL,updated_at REAL,
                UNIQUE(account_id,character_id,source_key));
            CREATE INDEX IF NOT EXISTS browsing_intents_round ON browsing_intents(round_id,kind);
            CREATE TABLE IF NOT EXISTS browsing_events(
                id TEXT PRIMARY KEY,account_id TEXT,kind TEXT,source_key TEXT,timestamp REAL,
                status TEXT,payload TEXT,error TEXT,created_at REAL,
                UNIQUE(account_id,kind,id));
            CREATE TABLE IF NOT EXISTS browsing_cursors(
                account_id TEXT,kind TEXT,watermark REAL,cursor TEXT,initialized INTEGER,
                PRIMARY KEY(account_id,kind));
        """)
        store.execute("DROP INDEX IF EXISTS browsing_round_owner")
        self.batches = BrowsingBatches(store)
        self.quotas = BrowsingQuotas(store)
        if "main_decision" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(browsing_video_progress)")
        }:
            store.execute("ALTER TABLE browsing_video_progress ADD COLUMN main_decision TEXT")
        if "read_state" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(browsing_video_progress)")
        }:
            store.execute(
                "ALTER TABLE browsing_video_progress ADD COLUMN read_state TEXT NOT NULL DEFAULT 'reading'"
            )
        store.db.execute("""CREATE TABLE IF NOT EXISTS browsing_read_progress(
            account_id TEXT,character_id TEXT,bvid TEXT,chunk_key TEXT,result TEXT,updated_at REAL,
            PRIMARY KEY(account_id,character_id,bvid,chunk_key))""")

    def recover(self):
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "UPDATE browsing_rounds SET status=CASE WHEN json_extract(config,'$.batch_id') IS NOT NULL "
                "OR json_extract(config,'$.full_video')=1 THEN 'paused' ELSE 'cancelled' END,"
                "error='服务重启，进度已保存',updated_at=? WHERE status IN ('waiting','queued','running')",
                (now,),
            )
            self.store.db.execute(
                "UPDATE browsing_tasks SET status=CASE WHEN round_id IN (SELECT id FROM browsing_rounds WHERE json_extract(config,'$.batch_id') IS NOT NULL) THEN 'paused' ELSE 'cancelled' END,error='服务重启，进度已保存',updated_at=? WHERE status IN ('discovered','claimed','preparing','evidence_ready')",
                (now,),
            )
            self.store.db.execute(
                "UPDATE browsing_intents SET status='publish_uncertain',error='提交中重启，禁止重发' WHERE status='publishing'"
            )
            self.store.db.execute(
                "UPDATE browsing_intents SET status='pending_review',auto_authorized=0,error='旧运行已结束，保留待审草稿' "
                "WHERE status='automatic_pending'"
            )
            self.store.db.execute(
                "UPDATE browsing_events SET status='received' WHERE status IN ('source_verified','context_ready')"
            )

    @staticmethod
    def decode(row):
        if row is None:
            return None
        row = dict(row)
        for field in (
            "config",
            "budget",
            "metrics",
            "payload",
            "target",
            "evidence",
            "material_keys",
            "receipt",
        ):
            if row.get(field) is not None:
                row[field] = json.loads(row[field])
        return row

    def round(self, identity):
        return self.decode(self.store.one("SELECT * FROM browsing_rounds WHERE id=?", (identity,)))

    def requested_preview(self, request_id, character, selection, account=None):
        existing = self.round(request_id)
        if existing and (
            not existing["preview"]
            or account and existing["account_id"] != account
            or existing["character_id"] != character["id"]
            or existing["character_version"] != character["version"]
            or existing["config"].get("operator_selection") != selection
        ):
            raise ValueError("请求已用于其他浏览轮次")
        return existing

    def create_round(self, account, character, config, epoch, preview, *, request_id=None):
        now, identity = time.time(), request_id or str(uuid4())
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            if request_id and (
                existing := self.requested_preview(request_id, character, config["operator_selection"], account)
            ):
                return existing, False
            previous = self.store.one(
                "SELECT * FROM browsing_rounds WHERE account_id=? AND status IN ('queued','running') "
                "AND COALESCE(json_extract(config,'$.interaction_only'),0)=?",
                (account, int(bool(config.get("interaction_only")))),
            )
            if previous and not config.get("full_video"):
                if request_id:
                    raise ValueError("已有浏览轮次正在运行")
                return self.decode(previous), False
            if config.get("autonomous_batch"):
                previous = self.store.one(
                    "SELECT r.* FROM browsing_rounds r JOIN browsing_batches b ON b.round_id=r.id "
                    "WHERE r.account_id=? AND r.character_id=? AND r.character_version=? "
                    "AND r.status IN ('paused','failed','partial','completed') AND (b.state IN ('retrieving','ready','paused') OR "
                    "EXISTS(SELECT 1 FROM browsing_batch_items i WHERE i.batch_id=b.id AND i.kind<>'comment_visit' AND i.status='daily_wait' "
                    "AND COALESCE(json_extract(i.payload,'$.next_at'),0)<=?)) "
                    "AND json_extract(r.config,'$.autonomous_batch')=1 "
                    "AND COALESCE(json_extract(r.config,'$.discovery_publish'),0)=0 "
                    "AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=? "
                    "ORDER BY r.created_at LIMIT 1",
                    (
                        account,
                        character["id"],
                        character["version"],
                        now,
                        config.get("review_session_id", ""),
                    ),
                )
                if previous:
                    previous = self.decode(previous)
                    # Keep the original retrieval scope/cursor, but current
                    # permissions, quotas and resources govern resumed work.
                    retrieval = {
                        "sources", "keywords", "watch_videos", "autonomous_topics",
                        "candidate_limit", "max_search_terms", "max_pages", "batch_id",
                    }
                    restored = config | {
                        key: value for key, value in previous["config"].items() if key in retrieval
                    }
                    self.store.db.execute(
                        "UPDATE browsing_rounds SET status='queued',config=?,epoch=?,owner=?,deadline=NULL,error=NULL,updated_at=? WHERE id=?",
                        (dump(restored), epoch, str(uuid4()), now, previous["id"]),
                    )
                    return self.round(previous["id"]), True
                config = config | {"batch_id": str(uuid4())}
            budget = {k: 0 for k in ("reads", "model_calls", "input_tokens", "output_tokens", "top_levels")}
            self.store.db.execute(
                "INSERT INTO browsing_rounds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    account,
                    character["id"],
                    character["version"],
                    "waiting" if config.get("full_video") else "queued",
                    int(preview),
                    epoch,
                    str(uuid4()),
                    None if not preview or config.get("full_video") else now + config["deadline_seconds"],
                    dump(config),
                    dump(budget),
                    dump({}),
                    None,
                    now,
                    now,
                ),
            )
        row = self.round(identity)
        self.batches.ensure(row)
        return row, True

    def admit_full_video(self, identity):
        now = time.time()
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.round(identity)
            admitted = self.store.db.execute(
                "UPDATE browsing_rounds SET status='queued',deadline=?,error=NULL,updated_at=? "
                "WHERE id=? AND status='waiting' AND NOT EXISTS(SELECT 1 FROM browsing_rounds "
                "WHERE account_id=? AND status IN ('queued','running') "
                "AND COALESCE(json_extract(config,'$.interaction_only'),0)=0)",
                (now + row["config"]["deadline_seconds"], now, identity, row["account_id"]),
            )
            return self.round(identity) if admitted.rowcount else None

    def reserve_budget(self, identity, **amounts):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.round(identity)
            if (
                not row
                or row["status"] not in {"queued", "running"}
                or row["deadline"] is not None
                and time.time() >= row["deadline"]
            ):
                raise ValueError("轮次已取消或达到期限")
            budget, config = row["budget"], row["config"]
            caps = {
                "reads": config["max_reads"],
                "model_calls": config["max_model_calls"],
                "input_tokens": config["max_input_tokens"],
                "output_tokens": config["max_output_tokens"],
                "top_levels": config["top_level_limit"],
            }
            if config.get("autonomous_batch"):
                caps["top_levels"] = None
            # Resource caps may be disabled without disabling accounting. The
            # top-level count keeps its separate 0 = no top-level posts meaning.
            caps = {key: None if key != "top_levels" and value == 0 else value for key, value in caps.items()}
            try:
                budget = reserve_counters(budget, caps, amounts)
            except GenerationBudgetExceeded as error:
                if all(caps[key] is None or value <= caps[key] for key, value in amounts.items()):
                    raise BrowsingBudgetDeferred(str(error)) from None
                raise
            self.store.db.execute(
                "UPDATE browsing_rounds SET budget=?,updated_at=? WHERE id=?",
                (dump(budget), time.time(), identity),
            )

    def settle_output(self, identity, reserved, actual):
        self._settle_budget(identity, "output_tokens", reserved, actual)

    def settle_input(self, identity, reserved, actual):
        self._settle_budget(identity, "input_tokens", reserved, actual)

    def _settle_budget(self, identity, field, reserved, actual):
        # Backends without actual completion usage retain the full reservation.
        if actual is None:
            return
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.round(identity)
            if row is None:
                raise ValueError("模型计量的父轮不存在")
            budget = row["budget"]
            budget = settle_counters(budget, field, reserved, actual)
            self.store.db.execute("UPDATE browsing_rounds SET budget=? WHERE id=?", (dump(budget), identity))

    def task(self, round_id, candidate):
        parent = self.round(round_id)
        if parent["config"].get("batch_id"):
            existing = self.store.one(
                "SELECT id FROM browsing_tasks WHERE round_id=? AND bvid=? AND COALESCE(json_extract(payload,'$.task_kind'),'new_video')=? ORDER BY created_at LIMIT 1",
                (round_id, candidate["bvid"], candidate.get("task_kind", "new_video")),
            )
            if existing:
                self.store.execute(
                    "UPDATE browsing_tasks SET status='discovered',attempts=0,lease_until=0,error=NULL WHERE id=? AND status NOT IN ('claimed','preparing','evidence_ready')",
                    (existing["id"],),
                )
                self.batches.task(parent, candidate["bvid"], existing["id"])
                return existing["id"]
        identity, now = str(uuid4()), time.time()
        candidate = {
            key: candidate[key]
            for key in (
                "bvid",
                "aid",
                "title",
                "owner_uid",
                "owner",
                "duration",
                "category_id",
                "published_at",
                "sources",
                "task_kind",
                "selection",
                "comment_batch_ids",
            )
            if key in candidate
        }
        self.store.execute(
            "INSERT INTO browsing_tasks(id,round_id,bvid,status,payload,created_at,updated_at) VALUES(?,?,?,'discovered',?,?,?)",
            (identity, round_id, candidate["bvid"], dump(candidate), now, now),
        )
        self.batches.task(parent, candidate["bvid"], identity)
        return identity

    @staticmethod
    def retry_scope(round_row):
        return tuple(round_row[key] for key in ("account_id", "character_id", "character_version"))

    def video_retry(self, row, bvid):
        return self.store.one(
            "SELECT r.failures,t.error FROM browsing_video_retries r JOIN browsing_tasks t ON t.id=r.task_id "
            "WHERE r.account_id=? AND r.character_id=? AND r.character_version=? AND r.bvid=? "
            "AND (r.failures<3 OR t.updated_at>?)",
            (*self.retry_scope(row), bvid, time.time() - 21600),
        )

    def retry_candidates(self, row):
        return [
            json.loads(task["payload"]) | {"bvid": task["bvid"]}
            for task in self.store.all(
                "SELECT t.bvid,t.payload FROM browsing_video_retries r JOIN browsing_tasks t ON t.id=r.task_id "
                "JOIN browsing_rounds parent ON parent.id=t.round_id "
                "WHERE r.account_id=? AND r.character_id=? AND r.character_version=? AND r.failures<3 "
                "AND COALESCE(json_extract(parent.config,'$.review_session_id'),'')=? "
                "AND COALESCE(json_extract(t.payload,'$.next_at'),t.updated_at)<=? "
                "ORDER BY t.updated_at LIMIT ?",
                (*self.retry_scope(row), row["config"].get("review_session_id", ""), time.time(), row["config"]["candidate_limit"]),
            )
        ]

    def video_reply_limit_reached(self, row, bvid):
        return self.quotas.video_reply_limit_reached(
            row["account_id"],
            row["character_id"],
            bvid,
            row["config"].get("max_replies_per_video", 2),
            row["config"].get("business_timezone", "Asia/Taipei"),
        )

    def content_progress(self, row, bvid):
        value = self.store.one(
            "SELECT material,decisions,main_decision,read_state FROM browsing_video_progress "
            "WHERE account_id=? AND character_id=? AND bvid=?",
            (row["account_id"], row["character_id"], bvid),
        )
        return (
            (
                {
                    key: json.loads(value[key]) if value[key] else None
                    for key in ("material", "decisions", "main_decision")
                }
                | {"read_state": value["read_state"], "fully_read": value["read_state"] == "complete"}
            )
            if value
            else None
        )

    def save_content_progress(self, row, material, decisions):
        self.store.execute(
            "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(account_id,character_id,bvid) DO UPDATE SET material=excluded.material,decisions=excluded.decisions,updated_at=excluded.updated_at",
            (
                row["account_id"],
                row["character_id"],
                material["metadata"]["bvid"],
                dump(material),
                dump([decision.model_dump() for decision in decisions]),
                time.time(),
            ),
        )

    def save_main_decision(self, row, bvid, decision):
        self.store.execute(
            "UPDATE browsing_video_progress SET main_decision=?,updated_at=? WHERE account_id=? AND character_id=? AND bvid=?",
            (
                dump(decision.model_dump() if hasattr(decision, "model_dump") else decision),
                time.time(),
                row["account_id"],
                row["character_id"],
                bvid,
            ),
        )

    def mark_watch_result(self, row, bvid, *, fully_read=False):
        progress = self.content_progress(row, bvid)
        if not progress:
            return
        material = progress["material"]
        complete = bool(
            fully_read
            and not material.get("missing_parts")
            and material.get("coverage") in {"all_available_cc", "complete", "all_parts"}
        )
        state = "complete" if complete or progress["fully_read"] else "viewed"
        self.store.execute(
            "UPDATE browsing_video_progress SET read_state=?,updated_at=? WHERE account_id=? AND character_id=? AND bvid=?",
            (state, time.time(), row["account_id"], row["character_id"], bvid),
        )

    def read_chunk(self, row, bvid, chunk_key):
        value = self.store.one(
            "SELECT result FROM browsing_read_progress WHERE account_id=? AND character_id=? AND bvid=? AND chunk_key=?",
            (row["account_id"], row["character_id"], bvid, chunk_key),
        )
        return json.loads(value["result"]) if value else None

    def save_read_chunk(self, row, bvid, chunk_key, decision):
        self.store.execute(
            "INSERT OR REPLACE INTO browsing_read_progress VALUES(?,?,?,?,?,?)",
            (
                row["account_id"],
                row["character_id"],
                bvid,
                chunk_key,
                dump(decision.model_dump()),
                time.time(),
            ),
        )

    def finish_video_attempt(self, row, candidate, task_id, failures):
        key = (*self.retry_scope(row), candidate["bvid"])
        with self.store.db:
            if failures:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO browsing_video_retries VALUES(?,?,?,?,?,?)",
                    (*key, failures, task_id),
                )
                task = self.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (task_id,))
                payload = json.loads(task["payload"]) | {"consecutive_failures": failures}
                if failures < 3:
                    payload["next_at"] = time.time() + 60 * failures
                self.store.db.execute(
                    "UPDATE browsing_tasks SET status=?,payload=? WHERE id=?",
                    ("skipped" if failures == 3 else "retry_wait", dump(payload), task_id),
                )
                self.batches.outcome(task_id, "skipped" if failures == 3 else "retry_wait")
            else:
                self.store.db.execute(
                    "DELETE FROM browsing_video_retries WHERE account_id=? AND character_id=? AND character_version=? AND bvid=?",
                    key,
                )

    def defer_video(self, row, bvid, task_id, failures):
        self.store.execute(
            "INSERT OR REPLACE INTO browsing_video_retries VALUES(?,?,?,?,?,?)",
            (*self.retry_scope(row), bvid, failures, task_id),
        )

    def claim(self, task_id, owner, *, seconds=30):
        now = time.time()
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            task = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (task_id,))
            parent = self.round(task["round_id"]) if task else None
            if (
                not parent
                or parent["status"] not in {"queued", "running"}
                or parent["deadline"] is not None
                and parent["deadline"] <= now
            ):
                raise ValueError("父轮已停止，不能领取任务")
            if task["status"] != "discovered" and not (
                task["status"] in {"claimed", "preparing", "evidence_ready"} and task["lease_until"] < now
            ):
                raise ValueError("任务已占有或已完成")
            if task["attempts"] >= 1 + parent["config"]["max_takeovers"]:
                raise ValueError("任务接管次数耗尽")
            generation = task["generation"] + 1
            self.store.db.execute(
                "UPDATE browsing_tasks SET owner=?,generation=?,lease_until=?,attempts=attempts+1,status='claimed',updated_at=? WHERE id=?",
                (
                    owner,
                    generation,
                    min(parent["deadline"], now + seconds)
                    if parent["deadline"] is not None
                    else now + seconds,
                    now,
                    task_id,
                ),
            )
        return generation

    def check_owner(self, task_id, owner, generation):
        row = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (task_id,))
        parent = self.round(row["round_id"]) if row else None
        if (
            not row
            or row["status"] not in {"claimed", "preparing", "evidence_ready"}
            or row["owner"] != owner
            or row["generation"] != generation
            or row["lease_until"] <= time.time()
            or not parent
            or parent["status"] not in {"queued", "running"}
            or parent["deadline"] is not None
            and parent["deadline"] <= time.time()
        ):
            raise ValueError("旧Worker或已取消租约，禁止提交")
        return row

    def heartbeat(self, task_id, owner, generation):
        with self.store.db:
            row = self.check_owner(task_id, owner, generation)
            parent = self.round(row["round_id"])
            self.store.db.execute(
                "UPDATE browsing_tasks SET lease_until=? WHERE id=? AND owner=? AND generation=?",
                (
                    min(parent["deadline"], time.time() + 30)
                    if parent["deadline"] is not None
                    else time.time() + 30,
                    task_id,
                    owner,
                    generation,
                ),
            )

    def complete(self, task_id, owner, generation, status, payload, material_key=None, error=None):
        with self.store.db:
            self.check_owner(task_id, owner, generation)
            self.store.db.execute(
                "UPDATE browsing_tasks SET status=?,payload=?,material_key=?,input_version=COALESCE(?,input_version),error=?,updated_at=? WHERE id=?",
                (status, dump(payload), material_key, material_key, error, time.time(), task_id),
            )
            self.batches.outcome(task_id, status, error, payload)

    def finish_round(self, identity, status, metrics=None, error=None):
        row = self.round(identity)
        if row["config"].get("batch_id"):
            if self.batches.finish(row, status=status):
                status = "paused"
            elif status == "cancelled":
                status = "completed"
        self.store.execute(
            "UPDATE browsing_rounds SET status=?,metrics=?,error=?,updated_at=? WHERE id=?",
            (status, dump(metrics or {}), error, time.time(), identity),
        )

    def protected_materials(self):
        keys = {
            r["material_key"]
            for r in self.store.all(
                "SELECT material_key FROM browsing_tasks WHERE status IN ('claimed','preparing','evidence_ready')"
            )
            if r["material_key"]
        }
        for row in self.store.all(
            "SELECT material_keys FROM browsing_intents WHERE status IN ('pending_review','approved','publishing')"
        ):
            keys.update(json.loads(row["material_keys"]))
        # Runtime-only tests and older databases need not have initialized DMs.
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(dm_drafts)")}
        if "video_evidence" in columns:
            for row in self.store.all(
                "SELECT video_evidence FROM dm_drafts WHERE status IN "
                "('queued','generating','pending_review','approved','copied','publishing') "
                "AND video_evidence IS NOT NULL"
            ):
                keys.update(json.loads(row["video_evidence"]).get("material_keys", []))
        return keys
