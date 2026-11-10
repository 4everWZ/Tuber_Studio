"""One OC/user relationship across channels; updates are program-owned and bounded."""

import hashlib
import json
import time
from collections import OrderedDict
from typing import Annotated

from pydantic import Field, StrictBool, StringConstraints, field_validator

from .models import StrictModel
from .store import dump

LIVE_SCORE_TTL = 7200
LIVE_SCORE_LIMIT = 2000
SCORE_COOLDOWN = 60
SCORE_DAILY_LIMIT = 10

def detect_negative_sentiment(text: str) -> int:
    """Detect hostile, toxic, or abusive input in live chat or comments.
    Returns a negative delta (-2 to -10) or 0 if neutral/positive.
    """
    text_clean = "".join(text.casefold().split())
    severe = ("去死", "滚出去", "nmsl", "cnm", "傻逼", "弱智", "恶心死了", "垃圾主播", "死肥鱼", "废物")
    if any(pattern in text_clean for pattern in severe):
        return -10
    mild = ("真菜", "好菜", "真下饭", "退订", "取关", "别叫了", "闭嘴", "真无聊", "关播吧")
    if any(pattern in text_clean for pattern in mild):
        return -2
    return 0


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
        self._live_scores = OrderedDict()
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
        if affinity <= -60:
            return "宿敌"
        if affinity <= -20:
            return "恶感"
        if affinity < 30:
            return "陌生"
        if affinity < 60:
            return "认识"
        if affinity < 90:
            return "熟悉"
        return "特别熟悉"

    def view(self, character_id, platform, uid, name="", *, account_id=""):
        if not uid:
            return {}
        data = self._user_data(account_id, character_id, platform, uid)
        return self._view_data(data)

    def _view_data(self, data):
        if data.get("personalization_enabled") is False:
            return {}
        affinity = max(-100, min(100, int(data.get("affinity", 0))))
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
        affinity = max(-100, min(100, int(data.get("affinity", 0))))
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
        if profile is None or not profile.personalization_enabled:
            prefix = (str(account_id), character_id, platform, uid)
            for key in [key for key in self._live_scores if key[1:] == prefix]:
                del self._live_scores[key]
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

    def observe(
        self,
        character_id,
        platform,
        uid,
        name,
        source_id,
        text,
        *,
        account_id="",
        delta=None,
    ):
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
            if delta is None:
                delta = self._bounded_delta(
                    self._recent_scores(recent), now, source_id, content_hash,
                    detect_negative_sentiment(text) or int(len(set(text.strip())) > 1),
                )
            current_aff = int(data.get("affinity", 0))
            new_affinity = min(100, max(-100, current_aff + delta))
            data.update(affinity=new_affinity, updated_at=now)

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

    def clear_live(self):
        self._live_scores.clear()

    def _live_record(self, key, now):
        record = self._live_scores.get(key)
        if record is not None and now - record["at"] < LIVE_SCORE_TTL:
            self._live_scores.move_to_end(key)
            return record
        self._live_scores.pop(key, None)
        return {"at": now, "score": 0, "recent": []}

    def live_view(self, session_id, character_id, platform, uid, *, account_id=""):
        if not uid:
            return {}
        data = self._user_data(account_id, character_id, platform, uid) if account_id else {}
        if data:
            view = self._view_data(data)
            return {key: view[key] for key in ("affinity", "stage") if key in view}
        key = (session_id, str(account_id), character_id, platform, uid)
        return self._view_data({"affinity": self._live_record(key, time.time())["score"]})

    def observe_live(self, session_id, character_id, platform, uid, name, source_id, text, *,
                     account_id="", confirmed=False, support=False, delta=None):
        """Score an admitted interaction, retaining only approved identities across sessions."""
        if not uid:
            return {}
        now = time.time()
        key = (session_id, str(account_id), character_id, platform, uid)
        record = self._live_record(key, now)
        recent = [item for item in record["recent"] if now - item[0] < 86400]
        text_hash = hashlib.sha256("".join(text.casefold().split()).encode()).hexdigest()
        if delta is None:
            delta = detect_negative_sentiment(text) or int(len(set(text.strip())) >= 4)
        if account_id:
            user = self.store.one("SELECT id FROM users WHERE platform=? AND platform_user_id=?", (platform, uid))
            data = self._data(account_id, character_id, user["id"]) if user else {}
            if data.get("personalization_enabled") is False:
                return {}
            if data or confirmed or support:
                user_id = user["id"] if user else self.store.ensure_user(platform, uid, name)
                view = self._observe_live_durable(
                    account_id, character_id, user_id, source_id, text_hash,
                    record, now, 0 if support else delta,
                )
                self._live_scores.pop(key, None)
                return view
        delta = self._bounded_delta(recent, now, source_id, text_hash, 0 if support else delta)
        if delta:
            recent.append((now, source_id, text_hash, delta))
            record["score"] = max(-100, min(100, record["score"] + delta))
        record.update(at=now, recent=recent)
        self._live_scores[key] = record
        if len(self._live_scores) > LIVE_SCORE_LIMIT:
            self._live_scores.popitem(last=False)
        return self._view_data({"affinity": record["score"]})

    def _observe_live_durable(self, account_id, character_id, user_id, source_id, text_hash, record, now, delta):
        where, values = self._scope(account_id, character_id, user_id)
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            data = self._data(account_id, character_id, user_id)
            if data.get("personalization_enabled") is False:
                return {}
            rows = self.store.all(
                f"SELECT created_at,source_id,content_hash,delta FROM account_relation_events WHERE {where}"
                " AND created_at>?",
                (*values, now - 86400),
            )
            live_recent = [item for item in record["recent"] if now - item[0] < 86400]
            delta = self._bounded_delta(
                self._recent_scores(rows) + live_recent, now, source_id, text_hash, delta
            )
            # Live receipts are bounded by a rolling day; the live source includes its unique session.
            self.store.db.execute(
                f"DELETE FROM account_relation_events WHERE {where} AND source_id LIKE 'live:%' AND created_at<=?",
                (*values, now - 86400),
            )
            affinity = max(-100, min(100, int(data.get("affinity", record["score"])) + delta))
            self.store.db.executemany(
                "INSERT OR IGNORE INTO account_relation_events VALUES(?,?,?,?,?,?,?)",
                ((*values, old_source, old_hash, old_delta, at) for at, old_source, old_hash, old_delta in live_recent),
            )
            if delta:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO account_relation_events VALUES(?,?,?,?,?,?,?)",
                    (*values, source_id, text_hash, delta, now),
                )
            if delta or "affinity" not in data:
                data.update(affinity=affinity, updated_at=now)
                self.store.db.execute("INSERT OR REPLACE INTO account_relations VALUES(?,?,?,?)", (*values, dump(data)))
        return self._view_data(data)

    @staticmethod
    def _recent_scores(rows):
        return [(row["created_at"], row["source_id"], row["content_hash"], row["delta"]) for row in rows]

    @staticmethod
    def _bounded_delta(recent, now, source_id, text_hash, delta):
        if not delta or any(source == source_id or content == text_hash for _, source, content, _ in recent):
            return 0
        # Both directions share the cooldown, and have independent daily caps.
        if any(change and now - at < SCORE_COOLDOWN for at, _, _, change in recent):
            return 0
        used = sum(abs(change) for _, _, _, change in recent if change * delta > 0)
        amount = min(abs(delta), max(0, SCORE_DAILY_LIMIT - used))
        return amount if delta > 0 else -amount
