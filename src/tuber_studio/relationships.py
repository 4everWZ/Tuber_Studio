"""One OC/user relationship across channels; updates are program-owned and bounded."""

import hashlib
import json
import time

from .store import dump


class Relationships:
    def __init__(self, store):
        self.store = store
        store.execute(
            "CREATE TABLE IF NOT EXISTS relation_events(character_id TEXT,user_id INTEGER,source_id TEXT,content_hash TEXT,delta INTEGER,created_at REAL,PRIMARY KEY(character_id,user_id,source_id))"
        )
        store.execute(
            "CREATE TABLE IF NOT EXISTS account_relations(account_id TEXT,character_id TEXT,user_id INTEGER,data TEXT NOT NULL,PRIMARY KEY(account_id,character_id,user_id))"
        )
        store.execute(
            "CREATE TABLE IF NOT EXISTS account_relation_events(account_id TEXT,character_id TEXT,user_id INTEGER,source_id TEXT,content_hash TEXT,delta INTEGER,created_at REAL,PRIMARY KEY(account_id,character_id,user_id,source_id))"
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

    def view(self, character_id, platform, uid, name="", *, account_id=""):
        if not uid:
            return {}
        user = self.store.one("SELECT id FROM users WHERE platform=? AND platform_user_id=?", (platform, uid))
        data = self._data(account_id, character_id, user["id"]) if user else {}
        affinity = max(0, min(100, int(data.get("affinity", 20))))
        return {"affinity": affinity, "stage": self.stage(affinity), "nickname": data.get("nickname", "")}

    @staticmethod
    def _scope(account_id, character_id, user_id):
        return (
            ("account_id=? AND " if account_id else "") + "character_id=? AND user_id=?",
            ((str(account_id),) if account_id else ()) + (character_id, user_id),
        )

    def _data(self, account_id, character_id, user_id):
        where, values = self._scope(account_id, character_id, user_id)
        table = "account_relations" if account_id else "relations"
        row = self.store.one(f"SELECT data FROM {table} WHERE {where}", values)
        return json.loads(row["data"]) if row else {}

    def observe(self, character_id, platform, uid, name, source_id, text, *, account_id=""):
        if not uid:
            return {}
        user_id = self.store.ensure_user(platform, uid, name)
        now = time.time()
        content_hash = hashlib.sha256("".join(text.casefold().split()).encode()).hexdigest()
        where, values = self._scope(account_id, character_id, user_id)
        event_table = "account_relation_events" if account_id else "relation_events"
        relation_table = "account_relations" if account_id else "relations"
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            if self.store.one(
                f"SELECT 1 FROM {event_table} WHERE {where} AND source_id=?", (*values, source_id)
            ):
                return self.view(character_id, platform, uid, name, account_id=account_id)
            recent = self.store.all(
                f"SELECT * FROM {event_table} WHERE {where} AND created_at>?",
                (*values, now - 86400),
            )
            delta = int(
                len(set(text.strip())) > 1
                and sum(row["delta"] for row in recent) < 10
                and not any(
                    row["content_hash"] == content_hash or (row["delta"] and row["created_at"] > now - 60)
                    for row in recent
                )
            )
            data = self._data(account_id, character_id, user_id)
            data.update(affinity=min(100, max(0, int(data.get("affinity", 20))) + delta), updated_at=now)
            event_values = (*values, source_id, content_hash, delta, now)
            relation_values = (*values, dump(data))
            self.store.db.execute(
                f"INSERT INTO {event_table} VALUES({','.join('?' for _ in event_values)})",
                event_values,
            )
            self.store.db.execute(
                f"INSERT OR REPLACE INTO {relation_table} VALUES({','.join('?' for _ in relation_values)})",
                relation_values,
            )
        return self.view(character_id, platform, uid, name, account_id=account_id)
