"""Transactional round/batch persistence, shared event/cursor tables, and account history scope."""

import asyncio
import json
import time
from uuid import uuid4

from ...core.meter import (
    BrowsingBudgetDeferred,
    GenerationBudgetExceeded,
    reserve_counters,
    settle_counters,
)
from ...core.store import dump
from ...infra.platforms.bilibili.account import PROFILE_CACHE_KEY, signature
from .quotas import BrowsingQuotas

ACTIVE_ROUND_STATUSES = ("queued", "running")


def eligible_event_heads():
    """Return CTE selecting the oldest actionable head per thread/floor."""
    return """
        WITH pending AS (
            SELECT id,kind,created_at,timestamp,error,
                   json_extract(payload,'$.resource_type') AS resource_type,
                   COALESCE(CAST(json_extract(payload,'$.oid') AS TEXT),
                            json_extract(payload,'$.bvid'),source_key,id) AS resource,
                   COALESCE(NULLIF(NULLIF(CAST(json_extract(payload,'$.root') AS TEXT),'0'),''),
                            json_extract(payload,'$.comment_id'),source_key,id) AS root
            FROM browsing_events
            WHERE account_id=:account AND status IN ('received','source_verified','context_ready','reply_deferred')
              AND (kind<>'mention' OR :mentions)
              AND (kind<>'comment_visit' OR (json_extract(payload,'$.character_id')=:character
                   AND json_extract(payload,'$.review_session_id') IS :review))
        ), heads AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY resource_type,resource,root
                                         ORDER BY (kind='comment_visit'),timestamp,created_at,id) AS position
            FROM pending
        )
    """


def event_scope(row):
    """Build parameter binding dict for `eligible_event_heads()`."""
    return {
        "account": row["account_id"],
        "character": row["character_id"],
        "mentions": row["config"]["mention_reply_enabled"],
        "review": row["config"].get("review_session_id"),
    }


def resolve_history_scope(store, bilibili_account, character):
    """Resolve current `(account_id, character_id, character_version)` without blocking network calls."""
    account = bilibili_account.display_account_id() if bilibili_account else ""
    if not account and bilibili_account is not None:
        credential_signature = signature(bilibili_account.credentials)
        cached = store.get("browsing_discovery_account", {}) or {}
        profile = store.get(PROFILE_CACHE_KEY, {}) or {}
        if cached.get("credential_signature") == credential_signature:
            account = cached["account_id"]
        elif profile.get("credential_signature") == credential_signature:
            account = profile["profile"]["uid"]
    return account, character["id"], character["version"]


def remember_discovery_account(store, bilibili_account, account_id):
    """Persist account-to-credential binding so historical records survive restarts."""
    store.set(
        "browsing_discovery_account",
        {
            "account_id": account_id,
            "credential_signature": signature(bilibili_account.credentials),
        },
    )


async def restore_history_scope(store, bilibili_account, character, checked_signature=None):
    """Verify an account once for older histories saved before account binding was persisted."""
    account, character_id, version = resolve_history_scope(store, bilibili_account, character)
    credential = bilibili_account.credentials
    key = signature(credential)
    if account or not credential.get("sessdata") or checked_signature == key:
        return checked_signature
    if not store.one(
        """SELECT 1 FROM browsing_rounds WHERE character_id=? AND character_version=?
           AND (json_extract(config,'$.discovery_publish')=1 OR json_extract(config,'$.autonomous_batch')=1) LIMIT 1""",
        (character_id, version),
    ):
        return checked_signature
    try:
        async with asyncio.timeout(4):
            verified = await bilibili_account.account_id()
        remember_discovery_account(store, bilibili_account, verified)
    except (TimeoutError, ValueError):
        pass
    return key


def infer_round_pipeline(config, preview, explicit=None):
    """Determine canonical source_pipeline ('discovery'|'inbox'|'manual') for a round."""
    if explicit in {"discovery", "inbox", "manual"}:
        return explicit
    if config.get("interaction_only"):
        return "inbox"
    if preview and not config.get("autonomous_batch"):
        return "manual"
    return "discovery"


class BrowsingRepository:
    """Shared SQLite repository for rounds, intents, notifications, cursors, and video progress."""

    def __init__(self, store, *, batches=None):
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
            CREATE TABLE IF NOT EXISTS browsing_comment_page_handoffs(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,review_session TEXT NOT NULL,bvid TEXT NOT NULL,
                page_key TEXT NOT NULL,document TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(account_id,character_id,review_session,bvid,page_key));
            CREATE TABLE IF NOT EXISTS browsing_title_decisions(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,bvid TEXT NOT NULL,
                title_hash TEXT NOT NULL,decision TEXT NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(account_id,character_id,bvid));
            CREATE TABLE IF NOT EXISTS browsing_batch_failures(
                batch_id TEXT NOT NULL,bvid TEXT NOT NULL,signature TEXT NOT NULL,
                error TEXT NOT NULL,count INTEGER NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(batch_id,bvid,signature));
            CREATE TABLE IF NOT EXISTS browsing_rounds(
                id TEXT PRIMARY KEY,account_id TEXT,character_id TEXT,character_version INTEGER,
                status TEXT,preview INTEGER,epoch INTEGER,owner TEXT,deadline REAL,
                config TEXT,budget TEXT,metrics TEXT,error TEXT,created_at REAL,updated_at REAL,
                source_pipeline TEXT);
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
                input_version TEXT,receipt TEXT,visibility TEXT,visibility_checked_at REAL,
                visibility_detail TEXT,error TEXT,created_at REAL,updated_at REAL,
                source_pipeline TEXT,
                UNIQUE(account_id,character_id,source_key));
            CREATE INDEX IF NOT EXISTS browsing_intents_round ON browsing_intents(round_id,kind);
            CREATE TABLE IF NOT EXISTS browsing_events(
                id TEXT PRIMARY KEY,account_id TEXT,kind TEXT,source_key TEXT,timestamp REAL,
                status TEXT,payload TEXT,error TEXT,created_at REAL,
                UNIQUE(account_id,kind,id));
            CREATE INDEX IF NOT EXISTS browsing_events_work
                ON browsing_events(account_id,status,kind,created_at);
            CREATE TABLE IF NOT EXISTS browsing_cursors(
                account_id TEXT,kind TEXT,watermark REAL,cursor TEXT,initialized INTEGER,
                PRIMARY KEY(account_id,kind));
            CREATE TABLE IF NOT EXISTS browsing_reply_coverage(
                account_id TEXT,character_id TEXT,source_key TEXT,
                intent_id TEXT NOT NULL REFERENCES browsing_intents(id) ON DELETE CASCADE,
                PRIMARY KEY(account_id,character_id,source_key));
            CREATE TABLE IF NOT EXISTS browsing_comment_progress(
                account_id TEXT,character_id TEXT,bvid TEXT,comment_id TEXT,source_hash TEXT,
                decision TEXT,updated_at REAL,
                PRIMARY KEY(account_id,character_id,bvid,comment_id));
        """)
        store.execute("DROP INDEX IF EXISTS browsing_round_owner")
        self.batches = batches
        self.quotas = BrowsingQuotas(store)
        for table in ("browsing_rounds", "browsing_intents"):
            cols = {row[1] for row in store.db.execute(f"PRAGMA table_info({table})")}
            if "source_pipeline" not in cols:
                store.execute(f"ALTER TABLE {table} ADD COLUMN source_pipeline TEXT")
        intent_cols = {row[1] for row in store.db.execute("PRAGMA table_info(browsing_intents)")}
        if "visibility_checked_at" not in intent_cols:
            store.execute("ALTER TABLE browsing_intents ADD COLUMN visibility_checked_at REAL")
        if "visibility_detail" not in intent_cols:
            store.execute("ALTER TABLE browsing_intents ADD COLUMN visibility_detail TEXT")
        progress_cols = {row[1] for row in store.db.execute("PRAGMA table_info(browsing_video_progress)")}
        if "main_decision" not in progress_cols:
            store.execute("ALTER TABLE browsing_video_progress ADD COLUMN main_decision TEXT")
        if "read_state" not in progress_cols:
            store.execute("ALTER TABLE browsing_video_progress ADD COLUMN read_state TEXT NOT NULL DEFAULT 'reading'")
        if "watch_evidence" not in progress_cols:
            store.execute("ALTER TABLE browsing_video_progress ADD COLUMN watch_evidence TEXT")
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
                "UPDATE browsing_intents SET status='publish_uncertain',error='服务在提交时重启，结果待核对' WHERE status='publishing'"
            )
            self.store.db.execute(
                "UPDATE browsing_intents SET status='pending_review',auto_authorized=0,error='上次运营已结束，草稿已保存' "
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
            "visibility_detail",
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

    def create_round(self, account, character, config, epoch, preview, *, request_id=None, source_pipeline=None):
        now, identity = time.time(), request_id or str(uuid4())
        pipeline = infer_round_pipeline(config, preview, explicit=source_pipeline)
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
                    retrieval = {
                        "sources", "keywords", "watch_videos", "autonomous_topics",
                        "candidate_limit", "max_search_terms", "max_pages", "batch_id",
                    }
                    restored = config | {
                        key: value for key, value in previous["config"].items() if key in retrieval
                    }
                    self.store.db.execute(
                        "UPDATE browsing_rounds SET status='queued',config=?,epoch=?,owner=?,deadline=NULL,error=NULL,updated_at=?,source_pipeline=? WHERE id=?",
                        (dump(restored), epoch, str(uuid4()), now, pipeline, previous["id"]),
                    )
                    return self.round(previous["id"]), True
                config = config | {"batch_id": str(uuid4())}
            budget = {k: 0 for k in ("reads", "model_calls", "input_tokens", "output_tokens", "top_levels")}
            self.store.db.execute(
                "INSERT INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,epoch,"
                "owner,deadline,config,budget,metrics,error,created_at,updated_at,source_pipeline) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    pipeline,
                ),
            )
        row = self.round(identity)
        if self.batches is not None:
            self.batches.ensure(row)
        return row, True

    def admit_full_video(self, identity):
        now = time.time()
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.round(identity)
            admitted = self.store.db.execute(
                "UPDATE browsing_rounds SET status='queued',deadline=?,error=NULL,updated_at=?,budget=? "
                "WHERE id=? AND status='waiting' AND NOT EXISTS(SELECT 1 FROM browsing_rounds "
                "WHERE account_id=? AND status IN ('queued','running') "
                "AND json_extract(config,'$.full_video')=1)",
                (now + row["config"]["deadline_seconds"], now, dump(dict.fromkeys(row["budget"], 0)),
                 identity, row["account_id"]),
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
        if actual is None:
            return
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.round(identity)
            if row is None:
                raise ValueError("模型计量的父轮不存在")
            budget = settle_counters(row["budget"], field, reserved, actual)
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
                if self.batches is not None:
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
        if self.batches is not None:
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

    def watch_evidence(self, row, bvid):
        value = self.store.one(
            "SELECT watch_evidence FROM browsing_video_progress WHERE account_id=? AND character_id=? AND bvid=?",
            (row["account_id"], row["character_id"], bvid),
        )
        return json.loads(value["watch_evidence"]) if value and value["watch_evidence"] else None

    def save_watch_evidence(self, row, bvid, evidence):
        material = evidence.get("material")
        if material is None or not material["parts"]:
            return
        self.store.execute(
            "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,watch_evidence) "
            "VALUES(?,?,?,?,'[]',?,?) ON CONFLICT(account_id,character_id,bvid) DO UPDATE SET "
            "material=CASE WHEN browsing_video_progress.decisions='[]' THEN excluded.material "
            "ELSE browsing_video_progress.material END,watch_evidence=excluded.watch_evidence,updated_at=excluded.updated_at",
            (row["account_id"], row["character_id"], bvid, dump(material), time.time(), dump(evidence)),
        )

    def watch_material(self, row, bvid):
        saved = self.watch_evidence(row, bvid)
        progress = self.content_progress(row, bvid)
        if saved:
            material = saved["material"]
            previous = progress["material"]["metadata"] if progress else material["metadata"]
            if all(previous.get(key) == material["metadata"].get(key) for key in ("aid", "parts", "published_at")):
                return material
        return progress["material"] if progress else None

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

    def mark_watch_result(self, row, bvid, *, fully_read=False, coverage=None):
        progress = self.content_progress(row, bvid)
        if not progress:
            return
        material = progress["material"]
        complete = coverage == "watched" if coverage is not None else bool(
            fully_read and not material.get("missing_parts")
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
                if self.batches is not None:
                    self.batches.outcome(task_id, "skipped" if failures == 3 else "retry_wait", payload=payload)
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
            if self.batches is not None:
                self.batches.outcome(task_id, status, error, payload)

    def finish_round(self, identity, status, metrics=None, error=None):
        row = self.round(identity)
        if row["config"].get("batch_id"):
            if self.batches is not None and self.batches.finish(row, status=status):
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
            "SELECT material_keys FROM browsing_intents "
            "WHERE status IN ('pending_review','approved','publishing') AND material_keys IS NOT NULL"
        ):
            keys.update(json.loads(row["material_keys"] or "[]"))
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(dm_drafts)")}
        if "video_evidence" in columns:
            for row in self.store.all(
                "SELECT video_evidence FROM dm_drafts WHERE status IN "
                "('queued','generating','pending_review','approved','copied','publishing') "
                "AND video_evidence IS NOT NULL"
            ):
                keys.update(json.loads(row["video_evidence"]).get("material_keys", []))
        return keys
