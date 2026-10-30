"""SQLite ownership of versions, identities, events and delivered interactions."""

import json
import sqlite3
import time
from pathlib import Path

from .models import Character


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path, seeds: list[dict]):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS characters(id TEXT PRIMARY KEY,active_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS character_versions(
                character_id TEXT,version INTEGER,config TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(character_id,version));
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,platform TEXT NOT NULL,platform_user_id TEXT NOT NULL,
                display_name TEXT NOT NULL,UNIQUE(platform,platform_user_id));
            CREATE TABLE IF NOT EXISTS relations(
                character_id TEXT,user_id INTEGER,data TEXT NOT NULL,PRIMARY KEY(character_id,user_id));
            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY,character_id TEXT,character_version INTEGER,started_at REAL,summary TEXT);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,event_id TEXT NOT NULL,
                session_id TEXT,user_id INTEGER,payload TEXT NOT NULL,status TEXT NOT NULL,
                reason TEXT,duplicates INTEGER DEFAULT 0,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,event_id INTEGER,session_id TEXT,character_id TEXT,
                character_version INTEGER,status TEXT NOT NULL,text TEXT,emotion TEXT,
                animation_id TEXT,audio_ref TEXT,error TEXT,llm_ms REAL,tts_ms REAL,
                playback_token TEXT,answer_check TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations(
                id INTEGER PRIMARY KEY,session_id TEXT,character_id TEXT,user_id INTEGER,
                event_text TEXT,reply_text TEXT,job_id TEXT UNIQUE,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS comments(
                id INTEGER PRIMARY KEY,platform TEXT DEFAULT 'bilibili',video_id TEXT,
                comment_id TEXT,thread_id TEXT,parent_comment_id TEXT,platform_user_id TEXT,display_name TEXT,content TEXT,
                category TEXT DEFAULT 'general',category_source TEXT DEFAULT 'auto',like_count INTEGER DEFAULT 0,created_at TEXT,
                imported_at REAL,UNIQUE(platform,video_id,comment_id));
            CREATE TABLE IF NOT EXISTS drafts(
                id INTEGER PRIMARY KEY,comment_id INTEGER UNIQUE NOT NULL,text TEXT NOT NULL,
                status TEXT NOT NULL,character_id TEXT,character_version INTEGER,model TEXT,
                input_summary TEXT,answer_check TEXT,error TEXT,receipt TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE IF NOT EXISTS comment_tasks(
                id TEXT PRIMARY KEY,status TEXT,total INTEGER,done INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,error TEXT,character_id TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE IF NOT EXISTS publish_attempts(
                id INTEGER PRIMARY KEY,draft_id INTEGER,started_at REAL,status TEXT,receipt TEXT,error TEXT);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(comments)")}
        if "category_source" not in columns:
            self.db.execute("ALTER TABLE comments ADD COLUMN category_source TEXT DEFAULT 'auto'")
        for column in ("thread_id", "parent_comment_id"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE comments ADD COLUMN {column} TEXT")
        self.db.execute("UPDATE comments SET thread_id=comment_id WHERE thread_id IS NULL OR thread_id=''")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS comments_thread ON comments(platform,video_id,thread_id,id)"
        )
        for table in ("jobs", "drafts"):
            if "answer_check" not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN answer_check TEXT")
        if "media" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN media TEXT")
        self.db.execute(
            "UPDATE jobs SET status='cancelled',error='服务重启，取消遗留任务' "
            "WHERE status NOT IN ('completed','failed','cancelled','expired')"
        )
        self.db.execute("UPDATE events SET status='cancelled',reason='服务重启' WHERE status='queued'")
        self.db.execute(
            "UPDATE comment_tasks SET status='cancelled',error='服务重启' "
            "WHERE status IN ('pending','running')"
        )
        # An interrupted HTTP write has an uncertain external outcome: never retry it automatically.
        self.db.execute(
            "UPDATE drafts SET status='publish_uncertain',error='发布中重启，请核对平台回执' "
            "WHERE status='publishing'"
        )
        self.db.execute(
            "UPDATE publish_attempts SET status='uncertain',error='发布中重启' WHERE status='sending'"
        )
        self.db.commit()
        for seed in seeds:
            if not self.one("SELECT id FROM characters WHERE id=?", (seed["id"],)):
                self.save_character(seed)
        if self.get("active_character") is None:
            self.set("active_character", seeds[0]["id"])

    def close(self):
        self.db.close()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.db:
            cursor = self.db.execute(sql, args)
        return cursor

    def get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, dump(value)))

    def character(self, character_id=None, version=None):
        character_id = character_id or self.get("active_character")
        if version is None:
            row = self.one("SELECT active_version FROM characters WHERE id=?", (character_id,))
            if row is None:
                raise ValueError("角色不存在")
            version = row["active_version"]
        row = self.one(
            "SELECT config FROM character_versions WHERE character_id=? AND version=?",
            (character_id, version),
        )
        if row is None:
            raise ValueError("角色版本不存在")
        return json.loads(row["config"])

    def characters(self):
        return [self.character(row["id"]) for row in self.all("SELECT id FROM characters ORDER BY id")]

    def versions(self, character_id):
        return [
            json.loads(r["config"])
            for r in self.all(
                "SELECT config FROM character_versions WHERE character_id=? ORDER BY version DESC",
                (character_id,),
            )
        ]

    def save_character(self, config):
        data = Character.model_validate(config).model_dump()
        row = self.one("SELECT MAX(version) AS v FROM character_versions WHERE character_id=?", (data["id"],))
        data["version"] = (row["v"] or 0) + 1
        with self.db:
            self.db.execute(
                "INSERT INTO character_versions VALUES(?,?,?,?)",
                (data["id"], data["version"], dump(data), time.time()),
            )
            self.db.execute("INSERT OR REPLACE INTO characters VALUES(?,?)", (data["id"], data["version"]))
        return data

    def activate(self, character_id, version=None):
        character = self.character(character_id, version)
        self.execute(
            "UPDATE characters SET active_version=? WHERE id=?", (character["version"], character_id)
        )
        self.set("active_character", character_id)
        return character

    def user(self, event):
        if not event.platform_user_id:
            return None
        self.execute(
            "INSERT INTO users(platform,platform_user_id,display_name) VALUES(?,?,?) "
            "ON CONFLICT(platform,platform_user_id) DO UPDATE SET display_name=excluded.display_name",
            (event.platform, event.platform_user_id, event.display_name),
        )
        return self.one(
            "SELECT id FROM users WHERE platform=? AND platform_user_id=?",
            (event.platform, event.platform_user_id),
        )["id"]

    def relationship(self, character_id, user_id):
        row = self.one(
            "SELECT data FROM relations WHERE character_id=? AND user_id=?", (character_id, user_id)
        )
        return json.loads(row["data"]) if row else {}

    def duplicate_event(self, event):
        key = dump([event.platform, event.room_id, event.event_id])
        existing = self.one("SELECT id FROM events WHERE event_key=?", (key,))
        if existing:
            self.execute("UPDATE events SET duplicates=duplicates+1 WHERE id=?", (existing["id"],))
            return existing["id"]
        return None

    def add_event(self, event, session_id, status, reason=None):
        if existing := self.duplicate_event(event):
            return existing, False
        key = dump([event.platform, event.room_id, event.event_id])
        user_id = self.user(event)
        cursor = self.execute(
            "INSERT INTO events(event_key,event_id,session_id,user_id,payload,status,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (key, event.event_id, session_id, user_id, event.model_dump_json(), status, reason, time.time()),
        )
        return cursor.lastrowid, True

    def event(self, event_id):
        row = self.one("SELECT * FROM events WHERE id=?", (event_id,))
        row["payload"] = json.loads(row["payload"])
        return row

    def job(self, job_id):
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def update_job(self, job_id, **fields):
        allowed = {
            "status",
            "text",
            "emotion",
            "animation_id",
            "audio_ref",
            "error",
            "llm_ms",
            "tts_ms",
            "playback_token",
            "answer_check",
            "media",
        }
        if not fields.keys() <= allowed:
            raise ValueError("未知任务字段")
        if isinstance(fields.get("answer_check"), (dict, list)):
            fields["answer_check"] = dump(fields["answer_check"])
        if isinstance(fields.get("media"), dict):
            fields["media"] = dump(fields["media"])
        fields["updated_at"] = time.time()
        self.execute(
            "UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
            (*fields.values(), job_id),
        )

    def recent(self, session_id, character_id, limit=4):
        rows = self.all(
            "SELECT event_text,reply_text FROM conversations WHERE session_id=? AND character_id=? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, character_id, limit),
        )
        return list(reversed(rows))

    def complete(self, job_id):
        job = self.job(job_id)
        if job["status"] != "playing":
            return False
        event = self.event(job["event_id"])
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='completed',updated_at=? WHERE id=?", (time.time(), job_id)
            )
            self.db.execute("UPDATE events SET status='completed' WHERE id=?", (event["id"],))
            if event["payload"]["type"] == "song":
                return True
            self.db.execute(
                "INSERT OR IGNORE INTO conversations(session_id,character_id,user_id,event_text,"
                "reply_text,job_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    job["session_id"],
                    job["character_id"],
                    event["user_id"],
                    event["payload"]["text"],
                    job["text"],
                    job_id,
                    time.time(),
                ),
            )
        return True
