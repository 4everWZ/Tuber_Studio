"""Conversation expiry, including derived summaries; source datasets are separate."""

import json
import time
from datetime import UTC, datetime

PRESERVED = ["comments", "drafts", "publish_attempts", "characters", "users", "relations"]
INTERVAL_SECONDS = 3600


def validate_days(days):
    if days is not None and (type(days) is not int or not 1 <= days <= 36500):
        raise ValueError("留存天数须为1到36500的整数，null表示不自动清理")
    return days


class RetentionManager:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.days = validate_days(
            store.get("conversation_retention_days", settings.data.get("conversation_retention_days", 30))
        )
        settings.data["conversation_retention_days"] = self.days
        self.last_result = store.get("retention_last_result")
        for table in ("conversations", "grounded_memory", "jobs", "events"):
            store.execute(f"CREATE INDEX IF NOT EXISTS {table}_age ON {table}(created_at)")
        store.execute("CREATE INDEX IF NOT EXISTS jobs_event ON jobs(event_id)")
        store.execute("CREATE INDEX IF NOT EXISTS jobs_session_age ON jobs(session_id,created_at)")
        store.execute(
            "CREATE INDEX IF NOT EXISTS conversations_session_age ON conversations(session_id,created_at)"
        )

    def policy(self):
        last = self.last_result.get("completed_at") if self.last_result else None
        return {
            "days": self.days,
            "enabled": self.days is not None,
            "last_run": last,
            "next_run": max(time.time(), (last or 0) + INTERVAL_SECONDS) if self.days is not None else None,
            "last_result": self.last_result,
            "cleanup_interval_seconds": INTERVAL_SECONDS,
            "preserved": PRESERVED,
        }

    def set_policy(self, days):
        days = validate_days(days)
        self.store.set("conversation_retention_days", days)
        self.days = self.settings.data["conversation_retention_days"] = days
        return self.policy()

    def cutoff(self, before=None, *, now=None):
        now = time.time() if now is None else now
        if before is None:
            if self.days is None:
                raise ValueError("自动清理已关闭；手动清理请指定截至时间")
            return datetime.fromtimestamp(max(0, now - self.days * 86400), UTC).timestamp()
        if not isinstance(before, str):
            raise ValueError("截至时间须为带时区的ISO日期时间")
        try:
            date = datetime.fromisoformat(before.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError
            cutoff = date.timestamp()
        except (ValueError, OverflowError):
            raise ValueError("截至时间须为带时区的ISO日期时间") from None
        if not 0 <= cutoff <= now:
            raise ValueError("截至时间不能早于1970年或晚于当前时间")
        return cutoff

    @staticmethod
    def _clauses():
        # Invalidate an entire derived window if any contributing source expires.
        # Keeping a recent summary of an old source would defeat retention.
        return {
            "context_windows": "(oldest_source_at < :cutoff OR updated_at < :cutoff OR "
            "(oldest_source_at IS NULL AND history != '[]') OR EXISTS "
            "(SELECT 1 FROM grounded_memory m WHERE m.scope=context_windows.mode "
            "AND m.character_id=context_windows.character_id "
            "AND m.character_version=context_windows.character_version AND m.created_at < :cutoff))",
            "grounded_memory": "created_at < :cutoff",
            "conversations": "created_at < :cutoff",
            "jobs": "created_at < :cutoff AND status IN ('completed','failed','cancelled','expired')",
            "events": "created_at < :cutoff AND status != 'queued' AND NOT EXISTS "
            "(SELECT 1 FROM jobs j WHERE j.event_id=events.id AND NOT "
            "(j.created_at < :cutoff AND j.status IN ('completed','failed','cancelled','expired')))",
            "sessions": "started_at < :cutoff AND NOT EXISTS "
            "(SELECT 1 FROM conversations c WHERE c.session_id=sessions.id AND c.created_at >= :cutoff) "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.session_id=sessions.id AND NOT "
            "(j.created_at < :cutoff AND j.status IN ('completed','failed','cancelled','expired'))) "
            "AND NOT EXISTS (SELECT 1 FROM context_windows w WHERE w.mode='live:' || sessions.id "
            "AND w.updated_at >= :cutoff AND w.oldest_source_at >= :cutoff AND NOT EXISTS "
            "(SELECT 1 FROM grounded_memory m WHERE m.scope=w.mode AND m.character_id=w.character_id "
            "AND m.character_version=w.character_version AND m.created_at < :cutoff))",
        }

    def preview(self, before=None, *, now=None):
        cutoff = self.cutoff(before, now=now)
        counts = {
            table: self.store.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {clause}", {"cutoff": cutoff})[
                "n"
            ]
            for table, clause in self._clauses().items()
        }
        return {
            "cutoff": datetime.fromtimestamp(cutoff, UTC).isoformat().replace("+00:00", "Z"),
            "counts": counts,
            "cleared_fields": {
                "session_summaries": self.store.one(
                    "SELECT COUNT(*) AS n FROM sessions WHERE started_at < ? AND summary IS NOT NULL AND summary != ''",
                    (cutoff,),
                )["n"]
            },
            "preserved": PRESERVED,
        }

    def cleanup(self, before=None, *, now=None, manual=False):
        now = time.time() if now is None else now
        cutoff = self.cutoff(before, now=now)
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            result = self.preview(datetime.fromtimestamp(cutoff, UTC).isoformat(), now=now)
            result.update(completed_at=now, manual=manual)
            self.store.db.execute("UPDATE sessions SET summary='' WHERE started_at < ?", (cutoff,))
            for table, clause in self._clauses().items():
                self.store.db.execute(f"DELETE FROM {table} WHERE {clause}", {"cutoff": cutoff})
            self.store.db.execute(
                "INSERT OR REPLACE INTO kv VALUES('retention_last_result',?)",
                (json.dumps(result, ensure_ascii=False),),
            )
        self.last_result = result
        return result
