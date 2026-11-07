"""Daily viewing starts and successful public replies, scoped to account and OC."""

import datetime as dt
import json
import time
from contextlib import contextmanager
from zoneinfo import ZoneInfo


class DailyReplyLimitReached(ValueError):
    def __init__(self, window):
        super().__init__("达到该视频今日回复上限，草稿保留，次日可继续")
        self.next_at = window["next_at"]
        self.business_day = window["day"]


def day_window(timezone="Asia/Taipei", now=None):
    now = time.time() if now is None else now
    local = dt.datetime.fromtimestamp(now, ZoneInfo(timezone))
    start = dt.datetime.combine(local.date(), dt.time(), tzinfo=local.tzinfo)
    following = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), tzinfo=local.tzinfo)
    return {"day": local.date().isoformat(), "start_at": start.timestamp(), "next_at": following.timestamp()}


def publication_time(receipt, confirmed_at):
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except ValueError:
            return confirmed_at
    if isinstance(receipt, dict):
        for key in ("published_at", "ctime", "timestamp"):
            value = receipt.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                value = value / 1000 if value > 1e12 else value
                if value <= confirmed_at + 300:
                    return value
    return confirmed_at


class BrowsingQuotas:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS browsing_watch_starts(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,task_id TEXT NOT NULL,
                bvid TEXT NOT NULL,business_day TEXT NOT NULL,started_at REAL NOT NULL,
                PRIMARY KEY(account_id,character_id,task_id));
            CREATE INDEX IF NOT EXISTS browsing_watch_day
                ON browsing_watch_starts(account_id,character_id,started_at);
            CREATE TABLE IF NOT EXISTS browsing_reply_quotas(
                account_id TEXT NOT NULL,character_id TEXT NOT NULL,source_key TEXT NOT NULL,
                bvid TEXT NOT NULL,business_day TEXT NOT NULL,token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,started_at REAL NOT NULL,updated_at REAL NOT NULL,published_at REAL,
                PRIMARY KEY(account_id,character_id,source_key));
            CREATE INDEX IF NOT EXISTS browsing_reply_day
                ON browsing_reply_quotas(account_id,character_id,bvid,started_at,status);
        """)
        if "published_at" not in {
            column[1] for column in store.db.execute("PRAGMA table_info(browsing_reply_quotas)")
        }:
            store.execute("ALTER TABLE browsing_reply_quotas ADD COLUMN published_at REAL")
        store.execute(
            "UPDATE browsing_reply_quotas SET published_at=updated_at WHERE status='published' AND published_at IS NULL"
        )
        store.execute(
            "CREATE INDEX IF NOT EXISTS browsing_reply_success_day ON browsing_reply_quotas(account_id,character_id,bvid,published_at)"
        )

    @contextmanager
    def _transaction(self):
        db = self.store.db
        if db.in_transaction:
            yield db
        else:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def start_watch(
        self, task_id, account_id, character_id, bvid, limit, timezone="Asia/Taipei", *, now=None
    ):
        now = time.time() if now is None else now
        window = day_window(timezone, now)
        day = window["day"]
        with self._transaction() as db:
            if db.execute(
                "SELECT 1 FROM browsing_watch_starts WHERE account_id=? AND character_id=? AND task_id=?",
                (str(account_id), character_id, task_id),
            ).fetchone():
                return True
            used = db.execute(
                "SELECT COUNT(*) FROM browsing_watch_starts WHERE account_id=? AND character_id=? AND started_at>=? AND started_at<?",
                (str(account_id), character_id, window["start_at"], window["next_at"]),
            ).fetchone()[0]
            if limit and used >= limit:
                return False
            db.execute(
                "INSERT INTO browsing_watch_starts VALUES(?,?,?,?,?,?)",
                (str(account_id), character_id, task_id, bvid, day, now),
            )
        return True

    def watch_status(self, account_id, character_id, limit, timezone="Asia/Taipei", *, now=None):
        window = day_window(timezone, now)
        used = self.store.one(
            "SELECT COUNT(*) AS n FROM browsing_watch_starts WHERE account_id=? AND character_id=? AND started_at>=? AND started_at<?",
            (str(account_id), character_id, window["start_at"], window["next_at"]),
        )["n"]
        return {
            **window,
            "started": used,
            "limit": limit,
            "remaining": max(0, limit - used) if limit else None,
        }

    def _sync_attempts(self, db, timezone, now):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='operation_write_attempts'").fetchone():
            return
        # Existing successful submissions count on deployment too. Drafts and
        # failures do not become successes merely because the feature is new.
        window = day_window(timezone, now)
        for attempt in db.execute(
            "SELECT account_id,character_id,intent,token,started_at,resolved_status,receipt FROM operation_write_attempts a "
            "WHERE channel='comments' AND (resolved_status IN ('uncertain','sending') OR (resolved_status='published' "
            "AND (started_at>=? OR EXISTS(SELECT 1 FROM operation_write_resolutions r WHERE r.token=a.token AND r.verified_at>=?))))",
            (window["start_at"], window["start_at"]),
        ).fetchall():
            try:
                platform, resource, source, _ = json.loads(attempt["intent"])
            except (ValueError, TypeError):
                continue
            if platform != "bilibili" or not str(resource).startswith("BV") or source in {"like", "favorite"}:
                continue
            status = "reserved" if attempt["resolved_status"] == "sending" else attempt["resolved_status"]
            db.execute(
                "INSERT OR IGNORE INTO browsing_reply_quotas VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt["account_id"],
                    attempt["character_id"],
                    attempt["intent"],
                    resource,
                    day_window(timezone, attempt["started_at"])["day"],
                    attempt["token"],
                    status,
                    attempt["started_at"],
                    now,
                    publication_time(attempt["receipt"], now) if status == "published" else None,
                ),
            )
        for attempt in db.execute(
            "SELECT q.token,a.resolved_status,a.receipt FROM browsing_reply_quotas q "
            "JOIN operation_write_attempts a ON a.token=q.token WHERE q.status IN ('reserved','uncertain') "
            "AND a.resolved_status IN ('published','uncertain','rejected','cancelled')"
        ).fetchall():
            outcome = {
                "published": "published",
                "uncertain": "uncertain",
                "rejected": "released",
                "cancelled": "released",
            }[attempt["resolved_status"]]
            db.execute(
                "UPDATE browsing_reply_quotas SET status=?,updated_at=?,published_at=? WHERE token=?",
                (
                    outcome,
                    now,
                    publication_time(attempt["receipt"], now) if outcome == "published" else None,
                    attempt["token"],
                ),
            )

    def reply_status(self, account_id, character_id, bvid, limit, timezone="Asia/Taipei", *, now=None):
        now = time.time() if now is None else now
        window = day_window(timezone, now)
        with self._transaction() as db:
            self._sync_attempts(db, timezone, now)
            counts = dict(
                db.execute(
                    "SELECT status,COUNT(*) FROM browsing_reply_quotas WHERE account_id=? AND character_id=? "
                    "AND bvid=? AND (status IN ('reserved','uncertain') OR (status='published' "
                    "AND published_at>=? AND published_at<?)) GROUP BY status",
                    (str(account_id), character_id, bvid, window["start_at"], window["next_at"]),
                ).fetchall()
            )
        published = counts.get("published", 0)
        reserved = counts.get("reserved", 0) + counts.get("uncertain", 0)
        return {
            **window,
            "published": published,
            "reserved": reserved,
            "limit": limit,
            "remaining": max(0, limit - published - reserved) if limit else None,
        }

    def video_reply_limit_reached(
        self, account_id, character_id, bvid, limit, timezone="Asia/Taipei", *, now=None
    ):
        if not limit:
            return False
        return self.reply_status(account_id, character_id, bvid, limit, timezone, now=now)["remaining"] == 0

    def reserve_reply(
        self, token, account_id, character_id, bvid, source_key, limit, timezone="Asia/Taipei", *, now=None
    ):
        now = time.time() if now is None else now
        window = day_window(timezone, now)
        with self._transaction() as db:
            self._sync_attempts(db, timezone, now)
            previous = db.execute(
                "SELECT * FROM browsing_reply_quotas WHERE account_id=? AND character_id=? AND source_key=?",
                (str(account_id), character_id, source_key),
            ).fetchone()
            if previous and previous["token"] == token:
                return token
            if previous and previous["status"] != "released":
                raise ValueError("该回复已有提交或待核对记录")
            count = db.execute(
                "SELECT COUNT(*) FROM browsing_reply_quotas WHERE account_id=? AND character_id=? AND bvid=? "
                "AND (status IN ('reserved','uncertain') OR (status='published' AND published_at>=? AND published_at<?))",
                (str(account_id), character_id, bvid, window["start_at"], window["next_at"]),
            ).fetchone()[0]
            if limit and count >= limit:
                raise DailyReplyLimitReached(window)
            db.execute(
                "INSERT INTO browsing_reply_quotas VALUES(?,?,?,?,?,?,'reserved',?,?,NULL) "
                "ON CONFLICT(account_id,character_id,source_key) DO UPDATE SET "
                "token=excluded.token,business_day=excluded.business_day,status='reserved',"
                "started_at=excluded.started_at,updated_at=excluded.updated_at,published_at=NULL",
                (str(account_id), character_id, source_key, bvid, window["day"], token, now, now),
            )
        return token

    def settle_reply(self, token, status, *, receipt=None, now=None):
        now = time.time() if now is None else now
        outcome = {
            "published": "published",
            "uncertain": "uncertain",
            "rejected": "released",
            "cancelled": "released",
        }.get(status)
        if outcome is None:
            raise ValueError("无效的每日回复结算状态")
        with self._transaction() as db:
            db.execute(
                "UPDATE browsing_reply_quotas SET status=?,updated_at=?,published_at=? WHERE token=? AND status!='published'",
                (outcome, now, publication_time(receipt, now) if outcome == "published" else None, token),
            )
