"""Transactional round ownership, bounded budgets and fenced task leases."""

import json
import time
from uuid import uuid4

from .generation_meter import reserve_counters, settle_counters
from .store import dump


class BrowsingRepository:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS browsing_rounds(
                id TEXT PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,
                status TEXT,preview INTEGER,epoch INTEGER,owner TEXT,deadline REAL,
                config TEXT,budget TEXT,metrics TEXT,error TEXT,created_at REAL,updated_at REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS browsing_round_owner ON browsing_rounds(account_id)
                WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS browsing_tasks(
                id TEXT PRIMARY KEY,round_id TEXT,bvid TEXT,status TEXT,owner TEXT,generation INTEGER DEFAULT 0,
                lease_until REAL DEFAULT 0,attempts INTEGER DEFAULT 0,input_version TEXT,material_key TEXT,
                payload TEXT,error TEXT,created_at REAL,updated_at REAL);
            CREATE INDEX IF NOT EXISTS browsing_tasks_round ON browsing_tasks(round_id,status);
            CREATE TABLE IF NOT EXISTS browsing_materials(
                id TEXT PRIMARY KEY,bvid TEXT,path TEXT,bytes INTEGER,expires_at REAL,created_at REAL);
            CREATE TABLE IF NOT EXISTS browsing_intents(
                id TEXT PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,
                source_key TEXT,kind TEXT,round_id TEXT,epoch INTEGER,auto_authorized INTEGER,
                status TEXT,text TEXT,reason TEXT,target TEXT,evidence TEXT,material_keys TEXT,
                input_version TEXT,receipt TEXT,visibility TEXT,error TEXT,created_at REAL,updated_at REAL,
                UNIQUE(account_id,character_id,source_key));
            CREATE TABLE IF NOT EXISTS browsing_events(
                id TEXT PRIMARY KEY,account_id TEXT,kind TEXT,source_key TEXT,timestamp REAL,
                status TEXT,payload TEXT,error TEXT,created_at REAL,
                UNIQUE(account_id,kind,id));
            CREATE TABLE IF NOT EXISTS browsing_cursors(
                account_id TEXT,kind TEXT,watermark REAL,cursor TEXT,initialized INTEGER,
                PRIMARY KEY(account_id,kind));
        """)

    def recover(self):
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "UPDATE browsing_rounds SET status='cancelled',error='重启不追发旧轮次',updated_at=? WHERE status IN ('queued','running')",
                (now,),
            )
            self.store.db.execute(
                "UPDATE browsing_tasks SET status='cancelled',error='重启取消旧Worker',updated_at=? WHERE status IN ('discovered','claimed','preparing','evidence_ready')",
                (now,),
            )
            self.store.db.execute(
                "UPDATE browsing_intents SET status='publish_uncertain',error='提交中重启，禁止重发' WHERE status='publishing'"
            )
            self.store.db.execute(
                "UPDATE browsing_events SET status='skipped',error='重启不追发积压事件' WHERE status IN ('received','source_verified','context_ready')"
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

    def create_round(self, account, character, config, epoch, preview):
        now, identity = time.time(), str(uuid4())
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            previous = self.store.one(
                "SELECT * FROM browsing_rounds WHERE account_id=? AND status IN ('queued','running')",
                (account,),
            )
            if previous:
                return self.decode(previous), False
            budget = {k: 0 for k in ("reads", "model_calls", "input_tokens", "output_tokens", "top_levels")}
            self.store.db.execute(
                "INSERT INTO browsing_rounds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    account,
                    character["id"],
                    character["version"],
                    "queued",
                    int(preview),
                    epoch,
                    str(uuid4()),
                    now + config["deadline_seconds"],
                    dump(config),
                    dump(budget),
                    dump({}),
                    None,
                    now,
                    now,
                ),
            )
        return self.round(identity), True

    def reserve_budget(self, identity, **amounts):
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.round(identity)
            if not row or row["status"] not in {"queued", "running"} or time.time() >= row["deadline"]:
                raise ValueError("轮次已取消或达到期限")
            budget, config = row["budget"], row["config"]
            caps = {
                "reads": config["max_reads"],
                "model_calls": config["max_model_calls"],
                "input_tokens": config["max_input_tokens"],
                "output_tokens": config["max_output_tokens"],
                "top_levels": config["top_level_limit"],
            }
            budget = reserve_counters(budget, caps, amounts)
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
            )
            if key in candidate
        }
        self.store.execute(
            "INSERT INTO browsing_tasks(id,round_id,bvid,status,payload,created_at,updated_at) VALUES(?,?,?,'discovered',?,?,?)",
            (identity, round_id, candidate["bvid"], dump(candidate), now, now),
        )
        return identity

    def claim(self, task_id, owner, *, seconds=30):
        now = time.time()
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            task = self.store.one("SELECT * FROM browsing_tasks WHERE id=?", (task_id,))
            parent = self.round(task["round_id"]) if task else None
            if not parent or parent["status"] not in {"queued", "running"} or parent["deadline"] <= now:
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
                (owner, generation, min(parent["deadline"], now + seconds), now, task_id),
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
            or parent["deadline"] <= time.time()
        ):
            raise ValueError("旧Worker或已取消租约，禁止提交")
        return row

    def heartbeat(self, task_id, owner, generation):
        with self.store.db:
            row = self.check_owner(task_id, owner, generation)
            parent = self.round(row["round_id"])
            self.store.db.execute(
                "UPDATE browsing_tasks SET lease_until=? WHERE id=? AND owner=? AND generation=?",
                (min(parent["deadline"], time.time() + 30), task_id, owner, generation),
            )

    def complete(self, task_id, owner, generation, status, payload, material_key=None, error=None):
        with self.store.db:
            self.check_owner(task_id, owner, generation)
            self.store.db.execute(
                "UPDATE browsing_tasks SET status=?,payload=?,material_key=?,input_version=COALESCE(?,input_version),error=?,updated_at=? WHERE id=?",
                (status, dump(payload), material_key, material_key, error, time.time(), task_id),
            )

    def finish_round(self, identity, status, metrics=None, error=None):
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
