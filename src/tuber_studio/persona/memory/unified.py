"""Unified ChatGPT-Web-style editable long-term memory with strict owner/public scope isolation."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from .daily.models import contains_memory_credential

VALID_SCOPES = frozenset({"owner", "public"})


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_names(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


class UnifiedMemoryStore:
    """Single source of truth for editable owner-private and public-facing memories."""

    def __init__(self, store):
        self.store = store
        with store.db:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories(
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK(scope IN ('owner', 'public')),
                    character_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_memories_lookup
                    ON memories(scope, character_id, active, updated_at DESC);
                """
            )

    @staticmethod
    def _decode(row: dict | None) -> dict | None:
        if row is None:
            return None
        try:
            tags = json.loads(row.get("tags") or "[]")
            if not isinstance(tags, list):
                tags = []
        except (TypeError, ValueError):
            tags = []
        return {
            "id": row["id"],
            "scope": row["scope"],
            "character_id": row["character_id"] or "",
            "content": row["content"],
            "source": row["source"] or "manual",
            "tags": [str(tag) for tag in tags if str(tag).strip()],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "active": bool(row["active"]),
        }

    @staticmethod
    def _clean_tags(tags) -> list[str]:
        if not tags:
            return []
        cleaned = []
        for tag in tags:
            text = str(tag).strip()
            if text and len(text) <= 40 and text not in cleaned:
                cleaned.append(text)
        return cleaned[:10]

    @staticmethod
    def _validate_content(content: str) -> str:
        text = (content or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        if len(text) > 1000:
            raise ValueError("单条记忆最多 1000 字符")
        if contains_memory_credential(text):
            raise ValueError("记忆内容不能包含凭据或密钥")
        return text

    def list_memories(
        self,
        *,
        scope: str | None = None,
        character_id: str | None = None,
        include_inactive: bool = True,
        limit: int = 200,
    ) -> list[dict]:
        clauses, params = [], []
        if scope is not None:
            if scope not in VALID_SCOPES:
                raise ValueError("无效记忆作用域")
            clauses.append("scope=?")
            params.append(scope)
        if character_id is not None:
            clauses.append("(character_id=? OR character_id='')")
            params.append(character_id)
        if not include_inactive:
            clauses.append("active=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.store.all(
            f"SELECT * FROM memories {where} ORDER BY active DESC, updated_at DESC, rowid DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        )
        return [self._decode(row) for row in rows]

    def get(self, memory_id: str) -> dict | None:
        return self._decode(self.store.one("SELECT * FROM memories WHERE id=?", (memory_id,)))

    def add_memory(
        self,
        content: str,
        *,
        scope: str = "owner",
        character_id: str = "",
        source: str = "manual",
        tags=(),
        memory_id: str | None = None,
    ) -> dict:
        if scope not in VALID_SCOPES:
            raise ValueError("无效记忆作用域")
        text = self._validate_content(content)
        char = str(character_id or "").strip()
        cleaned_tags = self._clean_tags(tags)
        norm = _normalize(text)
        existing_rows = self.store.all(
            "SELECT * FROM memories WHERE scope=? AND character_id=? AND active=1",
            (scope, char),
        )
        for existing in existing_rows:
            if _normalize(existing["content"]) == norm:
                decoded = self._decode(existing)
                merged_tags = self._clean_tags([*decoded["tags"], *cleaned_tags])
                now = time.time()
                if merged_tags != decoded["tags"]:
                    self.store.execute(
                        "UPDATE memories SET tags=?, updated_at=? WHERE id=?",
                        (json.dumps(merged_tags, ensure_ascii=False), now, decoded["id"]),
                    )
                    return self.get(decoded["id"])
                return decoded
        now = time.time()
        ident = str(memory_id or uuid4())
        self.store.execute(
            "INSERT OR IGNORE INTO memories(id,scope,character_id,content,source,tags,created_at,updated_at,active) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (ident, scope, char, text, (source or "manual").strip()[:60], json.dumps(cleaned_tags, ensure_ascii=False), now, now),
        )
        return self.get(ident)

    def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        scope: str | None = None,
        active: bool | None = None,
        tags=None,
    ) -> dict | None:
        existing = self.get(memory_id)
        if existing is None:
            return None
        updates = ["updated_at=?"]
        params: list = [time.time()]
        if content is not None:
            updates.append("content=?")
            params.append(self._validate_content(content))
        if scope is not None:
            if scope not in VALID_SCOPES:
                raise ValueError("无效记忆作用域")
            updates.append("scope=?")
            params.append(scope)
        if active is not None:
            updates.append("active=?")
            params.append(1 if active else 0)
        if tags is not None:
            updates.append("tags=?")
            params.append(json.dumps(self._clean_tags(tags), ensure_ascii=False))
        params.append(memory_id)
        self.store.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id=?", tuple(params))
        return self.get(memory_id)

    def delete_memory(self, memory_id: str) -> bool:
        cursor = self.store.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return bool(cursor.rowcount)

    def delete_matching(self, query: str, *, scope: str | None = None, character_id: str | None = None) -> int:
        needle = _normalize(query)
        if not needle:
            return 0
        candidates = self.list_memories(scope=scope, character_id=character_id, include_inactive=True, limit=500)
        matched = [row["id"] for row in candidates if needle in _normalize(row["content"])]
        if len(matched) == 1:
            return 1 if self.delete_memory(matched[0]) else 0
        exact = [row["id"] for row in candidates if _normalize(row["content"]) == needle]
        if len(exact) == 1:
            return 1 if self.delete_memory(exact[0]) else 0
        return 0

    def for_prompt(self, scope: str, *, character_id: str = "", limit: int = 20) -> list[dict]:
        """Return active memories strictly confined to `scope` (`owner` or `public`)."""
        if scope not in VALID_SCOPES:
            raise ValueError("无效记忆作用域")
        char = str(character_id or "").strip()
        rows = self.store.all(
            "SELECT * FROM memories WHERE scope=? AND active=1 "
            "AND (character_id=? OR character_id='') "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (scope, char, max(1, min(int(limit), 100))),
        )
        return [self._decode(row) for row in rows]

    def prompt_lines(self, scope: str, *, character_id: str = "", limit: int = 20) -> list[str]:
        return [item["content"] for item in self.for_prompt(scope, character_id=character_id, limit=limit)]


def migrate_legacy_memories(store, *, unified: UnifiedMemoryStore | None = None) -> int:
    """One-time idempotent migration from legacy tables into `memories` (legacy tables remain read-only)."""
    state = store.get("unified_memories_migrated_v1")
    if isinstance(state, dict) and state.get("version") == 1:
        return 0
    target = unified or UnifiedMemoryStore(store)
    migrated = 0
    if _table_exists(store.db, "daily_highlights"):
        cols = _column_names(store.db, "daily_highlights")
        evidence_col = "source_evidence" if "source_evidence" in cols else "NULL AS source_evidence"
        source_ref_col = "source_ref" if "source_ref" in cols else "'' AS source_ref"
        rows = store.all(
            f"SELECT id, character_id, category, headline, {source_ref_col}, quote, anecdote, tags, {evidence_col} "
            "FROM daily_highlights WHERE status='active' ORDER BY id"
        )
        for row in rows:
            raw_text = (row.get("anecdote") or row.get("quote") or row.get("headline") or "").strip()
            if not raw_text or contains_memory_credential(raw_text):
                continue
            ref = row.get("source_ref") or ""
            ev = {}
            if row.get("source_evidence"):
                try:
                    parsed = json.loads(row["source_evidence"])
                    if isinstance(parsed, dict):
                        ev = parsed
                except (TypeError, ValueError):
                    pass
            is_public = (
                row.get("category") == "public_incident"
                or ref.startswith(("operator-public:", "system:downtime:"))
                or ev.get("memory_kind") == "experience"
                or ev.get("kind") in {"viewer", "live"}
            )
            scope = "public" if is_public else "owner"
            try:
                tags = json.loads(row.get("tags") or "[]")
            except (TypeError, ValueError):
                tags = []
            try:
                target.add_memory(
                    raw_text,
                    scope=scope,
                    character_id=row.get("character_id") or "",
                    source="legacy_daily_highlights",
                    tags=tags if isinstance(tags, list) else [],
                    memory_id=f"mig:dh:{row['id']}",
                )
                migrated += 1
            except ValueError:
                continue
    if _table_exists(store.db, "grounded_memory"):
        rows = store.all(
            "SELECT id, scope, character_id, text, kind, fictional "
            "FROM grounded_memory WHERE fictional=0 ORDER BY id"
        )
        for row in rows:
            raw_text = (row.get("text") or "").strip()
            if not raw_text or contains_memory_credential(raw_text):
                continue
            if row.get("kind") not in {"operator_note", "viewer_message"}:
                continue
            scope = "owner" if str(row.get("scope") or "").startswith("operator:") else "public"
            try:
                target.add_memory(
                    raw_text,
                    scope=scope,
                    character_id=row.get("character_id") or "",
                    source="legacy_grounded_memory",
                    memory_id=f"mig:gm:{row['id']}",
                )
                migrated += 1
            except ValueError:
                continue
    store.set("unified_memories_migrated_v1", {"version": 1, "migrated_at": time.time(), "count": migrated})
    return migrated
