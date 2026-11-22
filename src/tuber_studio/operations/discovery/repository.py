"""Discovery repository for autonomous batches, candidate plans, and comment-revisit progress."""

import hashlib
import json
import time

from ...core.store import dump
from ..kernel.models import PENDING_IMAGE_STATES
from ..kernel.repository import BrowsingRepository, eligible_event_heads, event_scope


class BrowsingBatchStore:
    """SQLite ledger for autonomous discovery batches, comment revisits, and failures."""

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
            CREATE VIEW IF NOT EXISTS browsing_video_work AS
                SELECT r.account_id,r.character_id,r.preview,COALESCE(json_extract(r.config,'$.review_session_id'),'') AS review,
                       i.bvid,i.kind,COUNT(*) AS history_count,MIN(b.created_at) AS first_at,
                       MAX(CASE WHEN i.status='daily_wait' THEN COALESCE(json_extract(i.payload,'$.next_at'),0) ELSE 0 END) AS wake_at
                FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id
                JOIN browsing_rounds r ON r.id=b.round_id
                WHERE b.state<>'invalid' AND i.status IN ('selected','running','paused','failed','later','daily_wait')
                GROUP BY r.account_id,r.character_id,r.preview,review,i.bvid,i.kind;
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
            """WITH recent AS (
                SELECT i.payload,b.updated_at,i.bvid,ROW_NUMBER() OVER(PARTITION BY i.bvid,i.kind ORDER BY b.updated_at DESC,b.id) AS position
                FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id
                JOIN browsing_rounds r ON r.id=b.round_id JOIN browsing_video_work w ON w.account_id=r.account_id
                AND w.character_id=r.character_id AND w.bvid=i.bvid AND w.kind=i.kind AND w.preview=r.preview
                AND w.review=COALESCE(json_extract(r.config,'$.review_session_id'),'')
                WHERE r.account_id=? AND r.character_id=? AND r.preview=? AND w.review=? AND w.wake_at<=?
                AND i.status='later' AND b.state='completed'
            ) SELECT payload FROM recent WHERE position=1 ORDER BY updated_at,bvid""",
            (row["account_id"], row["character_id"], row["preview"], row["config"].get("review_session_id", ""), time.time()),
        )
        return [json.loads(item["payload"]) for item in items]

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
            "AND json_extract(r.config,'$.autonomous_batch')=1 "
            "AND COALESCE(json_extract(r.config,'$.discovery_publish'),0)=0 "
            "AND r.preview=0 AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=? "
            "AND b.state IN ('retrieving','ready','paused') LIMIT 1",
            (row["account_id"], row["character_id"], row["character_version"],
             row["config"].get("review_session_id", "")),
        ))

    def pending_publication_preparation(self, row, now, *, process_handoffs):
        return bool(self.store.one(
            """
            WITH rounds AS (
                SELECT r.id FROM browsing_rounds r JOIN browsing_batches b ON b.round_id=r.id
                WHERE r.account_id=:account AND r.character_id=:character AND r.character_version=:version
                  AND r.preview=0 AND b.state<>'invalid'
                  AND r.status NOT IN ('cancelled','invalid')
                  AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=:review
            ), images AS (
                SELECT job_id FROM operation_image_jobs WHERE status IN (SELECT value FROM json_each(:image_states))
            )
            SELECT 1 FROM rounds r JOIN browsing_intents i ON i.round_id=r.id
            JOIN images j ON j.job_id=json_extract(i.target,'$.image_ref.job_id')
            WHERE i.status='pending_review' AND i.kind='top_level' AND NOT EXISTS (
                SELECT 1 FROM operation_send_queue q WHERE q.channel='browsing' AND q.source_id=i.id AND q.wake_at>:now
            )
            UNION ALL
            SELECT 1 FROM rounds r JOIN dm_drafts d ON r.id=json_extract(d.video_evidence,'$.origin_round_id')
            WHERE d.account_id=:account AND d.character_id=:character AND d.character_version=:version
              AND d.source_kind='comment' AND (
                (:handoffs AND d.status IN ('queued','generating') AND d.next_attempt_at<=:now)
                OR (d.status='pending_review' AND json_extract(d.image_ref,'$.job_id') IN (SELECT job_id FROM images)
                    AND NOT EXISTS (SELECT 1 FROM operation_send_queue q WHERE q.channel='messages'
                                    AND q.source_id=CAST(d.id AS TEXT) AND q.wake_at>:now))
            ) LIMIT 1
            """,
            {"account": row["account_id"], "character": row["character_id"], "version": row["character_version"],
             "review": row["config"].get("review_session_id", ""), "now": now,
             "image_states": dump(PENDING_IMAGE_STATES), "handoffs": process_handoffs},
        ))

    def pending_comment_events(self, row):
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
                JOIN browsing_video_work w ON w.account_id=r.account_id AND w.character_id=r.character_id
                AND w.preview=r.preview AND w.review=:review AND w.bvid=i.bvid AND w.kind=i.kind
                LEFT JOIN browsing_tasks t ON t.id=i.task_id
                LEFT JOIN browsing_rounds owner ON owner.id=t.round_id
                WHERE r.account_id=:account AND r.character_id=:character
                  AND r.character_version=:version AND r.preview=:preview
                  AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=:review
                  AND i.kind='comment_visit'
                  AND w.wake_at<=:now
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

    def waiting_until(self, row, bvid, kind=None):
        value = self.store.one(
            "SELECT MAX(wake_at) AS next_at FROM browsing_video_work WHERE account_id=? AND character_id=? AND bvid=? "
            "AND review=? AND preview=? AND (? IS NULL OR kind=?)",
            (row["account_id"], row["character_id"], bvid, row["config"].get("review_session_id", ""),
             row["preview"], kind, kind),
        )
        return value["next_at"] if value and value["next_at"] and value["next_at"] > time.time() else None

    def outcome(self, task_id, status, error=None, payload=None):
        items = self.store.all("SELECT * FROM browsing_batch_items WHERE task_id=?", (task_id,))
        for item in items:
            self._item_outcome(item, status, error, payload)

    def _item_outcome(self, item, status, error, payload):
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
            sig = hashlib.sha256(error.encode()).hexdigest()
            self.store.execute(
                "INSERT INTO browsing_batch_failures VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(batch_id,bvid,signature) DO UPDATE SET count=count+1,updated_at=excluded.updated_at",
                (item["batch_id"], item["bvid"], sig, error, time.time()),
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
                "UPDATE browsing_batch_items SET status='paused' WHERE batch_id=? AND status='running'",
                (identity,),
            )
        self.store.execute(
            "UPDATE browsing_batches SET state=?,updated_at=? WHERE id=?", (state, time.time(), identity)
        )
        return state == "paused"


class DiscoveryRepository(BrowsingRepository):
    """Repository facade exposing Discovery batch & progress persistence."""

    def __init__(self, store):
        super().__init__(store, batches=BrowsingBatchStore(store))


__all__ = ["BrowsingBatchStore", "BrowsingRepository", "DiscoveryRepository"]
