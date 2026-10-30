"""One OC/user relationship across channels; updates are program-owned and bounded."""

import hashlib
import time

from .store import dump


class Relationships:
    def __init__(self, store):
        self.store = store
        store.execute(
            "CREATE TABLE IF NOT EXISTS relation_events(character_id TEXT,user_id INTEGER,source_id TEXT,content_hash TEXT,delta INTEGER,created_at REAL,PRIMARY KEY(character_id,user_id,source_id))"
        )

    @staticmethod
    def stage(affinity):
        return (
            "特别熟悉"
            if affinity >= 90
            else "熟悉"
            if affinity >= 60
            else "认识"
            if affinity >= 30
            else "陌生"
        )

    def view(self, character_id, platform, uid, name=""):
        if not uid:
            return {}
        user = self.store.one("SELECT id FROM users WHERE platform=? AND platform_user_id=?", (platform, uid))
        data = self.store.relationship(character_id, user["id"]) if user else {}
        affinity = max(0, min(100, int(data.get("affinity", 20))))
        return {"affinity": affinity, "stage": self.stage(affinity), "nickname": data.get("nickname", "")}

    def observe(self, character_id, platform, uid, name, source_id, text):
        if not uid:
            return {}
        user_id = self.store.ensure_user(platform, uid, name)
        now = time.time()
        content_hash = hashlib.sha256("".join(text.casefold().split()).encode()).hexdigest()
        if not self.store.one(
            "SELECT 1 FROM relation_events WHERE character_id=? AND user_id=? AND source_id=?",
            (character_id, user_id, source_id),
        ):
            recent = self.store.all(
                "SELECT * FROM relation_events WHERE character_id=? AND user_id=? AND created_at>?",
                (character_id, user_id, now - 86400),
            )
            delta = int(
                len(set(text.strip())) > 1
                and sum(row["delta"] for row in recent) < 10
                and not any(
                    row["content_hash"] == content_hash or (row["delta"] and row["created_at"] > now - 60)
                    for row in recent
                )
            )
            data = self.store.relationship(character_id, user_id)
            data.update(affinity=min(100, max(0, int(data.get("affinity", 20))) + delta), updated_at=now)
            with self.store.db:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO relation_events VALUES(?,?,?,?,?,?)",
                    (character_id, user_id, source_id, content_hash, delta, now),
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO relations VALUES(?,?,?)", (character_id, user_id, dump(data))
                )
        return self.view(character_id, platform, uid, name)
