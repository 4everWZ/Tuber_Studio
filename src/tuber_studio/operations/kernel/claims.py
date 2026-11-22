"""Atomic per-comment and per-floor claims ledger with Inbox preemption over Discovery."""

import time
from uuid import uuid4

VALID_CLAIM_KINDS = frozenset({"comment", "floor"})
VALID_PIPELINES = frozenset({"inbox", "discovery", "manual"})
DEFAULT_LEASE_SECONDS = 300.0


def comment_claim_key(account_id, character_id, resource_type, resource_id, target_comment_id):
    """Canonical claim key uniquely identifying a single target comment."""
    return f"comment:{account_id}:{character_id}:{resource_type}:{resource_id}:{target_comment_id}"


def floor_claim_key(account_id, character_id, resource_type, resource_id, root_id):
    """Canonical claim key enforcing mutual exclusion on a root comment thread."""
    return f"floor:{account_id}:{character_id}:{resource_type}:{resource_id}:{root_id}"


class OperationClaims:
    """SQLite-backed atomic lease ledger shared by Discovery, Inbox, and Manual entry points."""

    def __init__(self, store, *, default_ttl: float = DEFAULT_LEASE_SECONDS):
        self.store = store
        self.default_ttl = float(default_ttl)
        store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operation_claims(
                claim_key TEXT PRIMARY KEY,
                claim_kind TEXT NOT NULL,
                account_id TEXT NOT NULL,
                character_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                root_id TEXT NOT NULL,
                target_comment_id TEXT,
                owner_pipeline TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                lease_until REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS operation_claims_floor
                ON operation_claims(account_id, character_id, resource_type, resource_id, root_id, claim_kind);
            CREATE INDEX IF NOT EXISTS operation_claims_expiry
                ON operation_claims(owner_pipeline, lease_until);
            """
        )
        self.cleanup_stale()

    def cleanup_stale(self, *, now: float | None = None, owner_pipeline: str | None = None) -> int:
        """Delete expired leases (or all stale leases for a restarted pipeline)."""
        current = time.time() if now is None else now
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            if owner_pipeline is not None:
                if owner_pipeline not in VALID_PIPELINES:
                    raise ValueError(f"Unknown pipeline: {owner_pipeline}")
                cur = db.execute(
                    "DELETE FROM operation_claims WHERE owner_pipeline=? AND lease_until<=?",
                    (owner_pipeline, current),
                )
            else:
                cur = db.execute("DELETE FROM operation_claims WHERE lease_until<=?", (current,))
            return cur.rowcount

    def _validate_args(self, owner_pipeline: str, ttl_seconds: float | None) -> float:
        if owner_pipeline not in VALID_PIPELINES:
            raise ValueError(f"Unknown owner_pipeline: {owner_pipeline}")
        ttl = self.default_ttl if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        return ttl

    def _active_inbox_on_floor(
        self, db, *, account_id: str, character_id: str, resource_type: str, resource_id: str, root_id: str, now: float
    ):
        return db.execute(
            "SELECT claim_key, claim_kind, owner_pipeline, target_comment_id, lease_token, lease_until, updated_at "
            "FROM operation_claims WHERE account_id=? AND character_id=? AND resource_type=? AND resource_id=? "
            "AND root_id=? AND owner_pipeline='inbox' AND lease_until>? "
            "ORDER BY lease_until DESC LIMIT 1",
            (str(account_id), str(character_id), str(resource_type), str(resource_id), str(root_id), now),
        ).fetchone()

    def has_inbox_priority(
        self, *, account_id, character_id, resource_type, resource_id, root_id, now: float | None = None
    ) -> bool:
        """Return True if an active non-expired Inbox claim exists on the same floor."""
        current = time.time() if now is None else now
        row = self.store.one(
            "SELECT 1 FROM operation_claims WHERE account_id=? AND character_id=? "
            "AND resource_type=? AND resource_id=? AND root_id=? AND owner_pipeline='inbox' AND lease_until>? LIMIT 1",
            (str(account_id), str(character_id), str(resource_type), str(resource_id), str(root_id), current),
        )
        return row is not None

    def claim_comment(
        self,
        *,
        account_id,
        character_id,
        resource_type,
        resource_id,
        root_id,
        target_comment_id,
        owner_pipeline: str,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> dict:
        """Atomically claim a specific comment target, respecting Inbox preemption on discovery claims."""
        ttl = self._validate_args(owner_pipeline, ttl_seconds)
        current = time.time() if now is None else now
        acct, char, rtype, rid = str(account_id), str(character_id), str(resource_type), str(resource_id)
        root, target = str(root_id), str(target_comment_id)
        if not target:
            raise ValueError("target_comment_id is required for comment claim")
        key = comment_claim_key(acct, char, rtype, rid, target)
        token = uuid4().hex
        until = current + ttl
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM operation_claims WHERE account_id=? AND character_id=? "
                "AND resource_type=? AND resource_id=? AND root_id=? AND lease_until<=?",
                (acct, char, rtype, rid, root, current),
            )
            existing = db.execute("SELECT * FROM operation_claims WHERE claim_key=?", (key,)).fetchone()
            if existing is not None:
                holder = dict(existing)
                # Inbox may preempt an uncommitted Discovery claim on the exact target.
                if not (owner_pipeline == "inbox" and holder["owner_pipeline"] == "discovery"):
                    return {
                        "acquired": False,
                        "reason": "inbox_preempted" if holder["owner_pipeline"] == "inbox" else "already_claimed",
                        "claim_key": key,
                        "holder": holder,
                        "retry_at": holder["lease_until"],
                    }
            if owner_pipeline == "discovery":
                inbox_holder = self._active_inbox_on_floor(
                    db, account_id=acct, character_id=char, resource_type=rtype, resource_id=rid, root_id=root, now=current
                )
                if inbox_holder is not None:
                    holder = dict(inbox_holder)
                    return {
                        "acquired": False,
                        "reason": "inbox_floor_active",
                        "claim_key": key,
                        "holder": holder,
                        "retry_at": holder["lease_until"],
                    }
            preempted = dict(existing) if existing is not None else None
            db.execute(
                """
                INSERT INTO operation_claims(
                    claim_key, claim_kind, account_id, character_id, resource_type, resource_id,
                    root_id, target_comment_id, owner_pipeline, lease_token, lease_until, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_key) DO UPDATE SET
                    owner_pipeline=excluded.owner_pipeline,
                    lease_token=excluded.lease_token,
                    lease_until=excluded.lease_until,
                    updated_at=excluded.updated_at
                """,
                (key, "comment", acct, char, rtype, rid, root, target, owner_pipeline, token, until, current),
            )
        result = {
            "acquired": True,
            "claim_key": key,
            "claim_kind": "comment",
            "owner_pipeline": owner_pipeline,
            "lease_token": token,
            "lease_until": until,
        }
        if preempted:
            result["preempted_pipeline"] = preempted["owner_pipeline"]
            result["preempted_token"] = preempted["lease_token"]
        return result

    def acquire_floor_lease(
        self,
        *,
        account_id,
        character_id,
        resource_type,
        resource_id,
        root_id,
        owner_pipeline: str,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> dict:
        """Atomically acquire a floor lease; Discovery yields immediately whenever Inbox holds or claims the floor."""
        ttl = self._validate_args(owner_pipeline, ttl_seconds)
        current = time.time() if now is None else now
        acct, char, rtype, rid = str(account_id), str(character_id), str(resource_type), str(resource_id)
        root = str(root_id)
        key = floor_claim_key(acct, char, rtype, rid, root)
        token = uuid4().hex
        until = current + ttl
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM operation_claims WHERE account_id=? AND character_id=? "
                "AND resource_type=? AND resource_id=? AND root_id=? AND lease_until<=?",
                (acct, char, rtype, rid, root, current),
            )
            if owner_pipeline == "discovery":
                inbox_holder = self._active_inbox_on_floor(
                    db, account_id=acct, character_id=char, resource_type=rtype, resource_id=rid, root_id=root, now=current
                )
                if inbox_holder is not None:
                    holder = dict(inbox_holder)
                    return {
                        "acquired": False,
                        "reason": "inbox_preempted",
                        "claim_key": key,
                        "holder": holder,
                        "retry_at": holder["lease_until"],
                    }
            existing = db.execute("SELECT * FROM operation_claims WHERE claim_key=?", (key,)).fetchone()
            preempted = None
            if existing is not None:
                holder = dict(existing)
                if owner_pipeline == "inbox" and holder["owner_pipeline"] == "discovery":
                    preempted = holder
                else:
                    return {
                        "acquired": False,
                        "reason": "inbox_preempted" if holder["owner_pipeline"] == "inbox" else "floor_busy",
                        "claim_key": key,
                        "holder": holder,
                        "retry_at": holder["lease_until"],
                    }
            db.execute(
                """
                INSERT INTO operation_claims(
                    claim_key, claim_kind, account_id, character_id, resource_type, resource_id,
                    root_id, target_comment_id, owner_pipeline, lease_token, lease_until, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_key) DO UPDATE SET
                    owner_pipeline=excluded.owner_pipeline,
                    lease_token=excluded.lease_token,
                    lease_until=excluded.lease_until,
                    updated_at=excluded.updated_at
                """,
                (key, "floor", acct, char, rtype, rid, root, None, owner_pipeline, token, until, current),
            )
        result = {
            "acquired": True,
            "claim_key": key,
            "claim_kind": "floor",
            "owner_pipeline": owner_pipeline,
            "lease_token": token,
            "lease_until": until,
        }
        if preempted:
            result["preempted_pipeline"] = preempted["owner_pipeline"]
            result["preempted_token"] = preempted["lease_token"]
        return result

    def verify_lease(self, claim_key: str, lease_token: str, *, now: float | None = None) -> bool:
        """Check whether the given lease token still holds a valid non-expired claim."""
        current = time.time() if now is None else now
        row = self.store.one(
            "SELECT owner_pipeline, account_id, character_id, resource_type, resource_id, root_id, lease_until "
            "FROM operation_claims WHERE claim_key=? AND lease_token=?",
            (claim_key, lease_token),
        )
        if row is None or row["lease_until"] <= current:
            return False
        if row["owner_pipeline"] == "discovery" and self.has_inbox_priority(
            account_id=row["account_id"],
            character_id=row["character_id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            root_id=row["root_id"],
            now=current,
        ):
            return False
        return True

    def renew(
        self, claim_key: str, lease_token: str, *, ttl_seconds: float | None = None, now: float | None = None
    ) -> bool:
        """Extend an active lease inside BEGIN IMMEDIATE if still owned and unpreempted."""
        current = time.time() if now is None else now
        ttl = self.default_ttl if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner_pipeline, account_id, character_id, resource_type, resource_id, root_id, lease_until "
                "FROM operation_claims WHERE claim_key=? AND lease_token=?",
                (claim_key, lease_token),
            ).fetchone()
            if row is None or row["lease_until"] <= current:
                return False
            if row["owner_pipeline"] == "discovery" and self._active_inbox_on_floor(
                db,
                account_id=row["account_id"],
                character_id=row["character_id"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                root_id=row["root_id"],
                now=current,
            ):
                return False
            db.execute(
                "UPDATE operation_claims SET lease_until=?, updated_at=? WHERE claim_key=? AND lease_token=?",
                (current + ttl, current, claim_key, lease_token),
            )
            return True

    def release(self, claim_key: str, *, lease_token: str | None = None) -> bool:
        """Release a claim atomically; if lease_token is provided, only release if owned by that token."""
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            if lease_token is None:
                cur = db.execute("DELETE FROM operation_claims WHERE claim_key=?", (claim_key,))
            else:
                cur = db.execute(
                    "DELETE FROM operation_claims WHERE claim_key=? AND lease_token=?",
                    (claim_key, lease_token),
                )
            return cur.rowcount > 0

    def list_claims(self, *, now: float | None = None) -> list[dict]:
        current = time.time() if now is None else now
        return self.store.all(
            "SELECT * FROM operation_claims WHERE lease_until>? ORDER BY updated_at DESC",
            (current,),
        )
