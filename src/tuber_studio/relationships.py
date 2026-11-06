"""One OC/user relationship across channels; updates are program-owned and bounded."""

import hashlib
import json
import time
from typing import Annotated

from pydantic import Field, StrictBool, StringConstraints, field_validator

from .models import StrictModel
from .store import dump

ProfileEntry = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class UserProfile(StrictModel):
    personalization_enabled: StrictBool
    nickname: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
    preferences: list[ProfileEntry] = Field(max_length=10)
    important_events: list[ProfileEntry] = Field(max_length=10)

    @field_validator("preferences", "important_events")
    @classmethod
    def unique_entries(cls, entries):
        return list(dict.fromkeys(entries))


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
        data = self._user_data(account_id, character_id, platform, uid)
        if data.get("personalization_enabled") is False:
            return {}
        affinity = max(0, min(100, int(data.get("affinity", 20))))
        result = {"affinity": affinity, "stage": self.stage(affinity), "nickname": data.get("nickname", "")}
        if data.get("profile_confirmed_at"):
            for key in ("preferences", "important_events"):
                if data.get(key):
                    result[key] = data[key]
        return result

    def _user_data(self, account_id, character_id, platform, uid):
        user = self.store.one("SELECT id FROM users WHERE platform=? AND platform_user_id=?", (platform, uid))
        return self._data(account_id, character_id, user["id"]) if user else {}

    def profile(self, account_id, character_id, platform, uid):
        data = self._user_data(account_id, character_id, platform, uid)
        confirmed_at = data.get("profile_confirmed_at")
        affinity = max(0, min(100, int(data.get("affinity", 20))))
        return {
            "personalization_enabled": data.get("personalization_enabled", True),
            "nickname": data.get("nickname", ""),
            "preferences": data.get("preferences", []) if confirmed_at else [],
            "important_events": data.get("important_events", []) if confirmed_at else [],
            "affinity": affinity,
            "stage": self.stage(affinity),
            "last_interaction_at": data.get("updated_at"),
            "confirmed_at": confirmed_at,
            "source": "operator_confirmed" if confirmed_at else None,
        }

    def profile_revision(self, account_id, character_id, platform, uid):
        if account_id and uid:
            return self._user_data(account_id, character_id, platform, uid).get("profile_revision")

    def event_profile_revision(self, character_id, event):
        return self.profile_revision(
            event.get("account_id"), character_id, event.get("platform"), event.get("platform_user_id")
        )

    def set_profile(self, account_id, character_id, platform, uid, profile=None):
        if not account_id or not uid:
            raise ValueError("用户档案需要已核验的账号及用户身份")
        user_id = self.store.ensure_user(platform, uid, "")
        now = time.time()
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            data = self._data(account_id, character_id, user_id)
            if profile is None:
                data = {"personalization_enabled": data.get("personalization_enabled", True)}
            else:
                data.update(profile.model_dump(), profile_confirmed_at=now)
            data["profile_revision"] = now
            self.store.db.execute(
                "INSERT OR REPLACE INTO account_relations VALUES(?,?,?,?)",
                (str(account_id), character_id, user_id, dump(data)),
            )
        return self.profile(account_id, character_id, platform, uid)

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
            data = self._data(account_id, character_id, user_id)
            if data.get("personalization_enabled") is False:
                return {}
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
