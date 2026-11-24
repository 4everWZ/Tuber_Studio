"""Unified ChatGPT-Web-style Layer 1 Saved Memories with strict owner/public scope isolation."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, ValidationError

from ...core.agent_types import VALID_MEMORY_SCOPES, MemoryItem, MemoryScope, parse_memory_scope
from ...core.models import StrictModel, tool_function
from .daily.models import contains_memory_credential

DEFAULT_MEMORY_LIMIT = 100
VALID_SCOPES = VALID_MEMORY_SCOPES


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _table_exists(db, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _column_names(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


class UnifiedMemoryStore:
    """Single source of truth for Layer 1 Saved Memories (owner-private vs public-facing)."""

    DEFAULT_MEMORY_LIMIT = DEFAULT_MEMORY_LIMIT

    def __init__(self, store, *, limit: int = DEFAULT_MEMORY_LIMIT):
        self.store = store
        self.limit = max(1, int(limit))
        with store.db:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories(
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK(scope IN ('owner', 'public')),
                    character_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    source_key TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_memories_lookup
                    ON memories(scope, character_id, active, updated_at DESC);
                """
            )
            cols = _column_names(store.db, "memories")
            if "source_key" not in cols:
                store.db.execute("ALTER TABLE memories ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
            store.db.execute("CREATE INDEX IF NOT EXISTS idx_memories_source_key ON memories(source_key)")

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
            "source_key": row.get("source_key") or "",
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

    @staticmethod
    def _scope_clauses(
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
        active_only: bool = False,
        migrated_only: bool = False,
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope=?")
            params.append(str(parse_memory_scope(scope)))
        if character_id is not None:
            clauses.append("(character_id=? OR character_id='')")
            params.append(str(character_id).strip())
        if active_only:
            clauses.append("active=1")
        if migrated_only:
            clauses.append("(id LIKE 'mig:%' OR source_key LIKE 'mig:%')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_memories(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
        include_inactive: bool = True,
        limit: int = 200,
    ) -> list[dict]:
        where, params = self._scope_clauses(
            scope=scope,
            character_id=character_id,
            active_only=not include_inactive,
        )
        rows = self.store.all(
            f"SELECT * FROM memories {where} ORDER BY active DESC, updated_at DESC, rowid DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        )
        return [self._decode(row) for row in rows]

    def list_items(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
        include_inactive: bool = True,
        limit: int = 200,
    ) -> list[MemoryItem]:
        return [
            MemoryItem.from_dict(row)
            for row in self.list_memories(
                scope=scope,
                character_id=character_id,
                include_inactive=include_inactive,
                limit=limit,
            )
        ]

    def get(self, memory_id: str) -> dict | None:
        return self._decode(self.store.one("SELECT * FROM memories WHERE id=?", (memory_id,)))

    def get_item(self, memory_id: str) -> MemoryItem | None:
        row = self.get(memory_id)
        return MemoryItem.from_dict(row) if row is not None else None

    def count_memories(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
        active_only: bool = True,
    ) -> int:
        where, params = self._scope_clauses(
            scope=scope,
            character_id=character_id,
            active_only=active_only,
        )
        row = self.store.one(f"SELECT COUNT(*) AS n FROM memories {where}", tuple(params))
        return int(row["n"]) if row else 0

    def legacy_migrated_count(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
    ) -> int:
        where, params = self._scope_clauses(
            scope=scope,
            character_id=character_id,
            migrated_only=True,
        )
        row = self.store.one(f"SELECT COUNT(*) AS n FROM memories {where}", tuple(params))
        return int(row["n"]) if row else 0

    def stats(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
    ) -> dict[str, Any]:
        active_count = self.count_memories(scope=scope, character_id=character_id, active_only=True)
        total_count = self.count_memories(scope=scope, character_id=character_id, active_only=False)
        migrated = self.legacy_migrated_count(scope=scope, character_id=character_id)
        return {
            "count": active_count,
            "total_count": total_count,
            "limit": self.limit,
            "over_limit": active_count > self.limit,
            "remaining": max(0, self.limit - active_count),
            "legacy_migrated_count": migrated,
            "by_scope": {
                "owner": self.count_memories(scope="owner", character_id=character_id, active_only=True),
                "public": self.count_memories(scope="public", character_id=character_id, active_only=True),
            },
        }

    def find_duplicate(
        self,
        content: str,
        *,
        scope: str | MemoryScope = "owner",
        character_id: str = "",
    ) -> dict | None:
        scope_str = str(parse_memory_scope(scope))
        char = str(character_id or "").strip()
        norm = _normalize(content)
        existing_rows = self.store.all(
            "SELECT * FROM memories WHERE scope=? AND character_id=? AND active=1",
            (scope_str, char),
        )
        for existing in existing_rows:
            if _normalize(existing["content"]) == norm:
                return self._decode(existing)
        return None

    def add_memory(
        self,
        content: str,
        *,
        scope: str | MemoryScope = "owner",
        character_id: str = "",
        source: str = "manual",
        tags=(),
        memory_id: str | None = None,
        source_key: str = "",
        enforce_limit: bool = False,
    ) -> dict:
        scope_str = str(parse_memory_scope(scope))
        text = self._validate_content(content)
        char = str(character_id or "").strip()
        cleaned_tags = self._clean_tags(tags)
        ident = str(memory_id or uuid4())
        effective_source_key = str(source_key or "").strip() or (ident if ident.startswith("mig:") else "")

        if decoded := self.find_duplicate(text, scope=scope_str, character_id=char):
            merged_tags = self._clean_tags([*decoded["tags"], *cleaned_tags])
            now = time.time()
            need_source_key = bool(effective_source_key and not decoded.get("source_key"))
            if merged_tags != decoded["tags"] or need_source_key:
                self.store.execute(
                    "UPDATE memories SET tags=?, source_key=?, updated_at=? WHERE id=?",
                    (
                        json.dumps(merged_tags, ensure_ascii=False),
                        effective_source_key or decoded.get("source_key") or "",
                        now,
                        decoded["id"],
                    ),
                )
                return self.get(decoded["id"])
            return decoded

        if enforce_limit and self.count_memories(scope=scope_str, character_id=char, active_only=True) >= self.limit:
            raise ValueError(f"记忆数量已达到上限（{self.limit} 条），请先精简或删除旧记忆")

        now = time.time()
        self.store.execute(
            "INSERT OR IGNORE INTO memories(id,scope,character_id,content,source,source_key,tags,created_at,updated_at,active) "
            "VALUES(?,?,?,?,?,?,?,?,?,1)",
            (
                ident,
                scope_str,
                char,
                text,
                (source or "manual").strip()[:60],
                effective_source_key[:160],
                json.dumps(cleaned_tags, ensure_ascii=False),
                now,
                now,
            ),
        )
        return self.get(ident)

    def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        scope: str | MemoryScope | None = None,
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
            updates.append("scope=?")
            params.append(str(parse_memory_scope(scope)))
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

    def delete_matching(
        self,
        query: str,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
    ) -> int:
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

    def purge_migrated_memories(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
    ) -> int:
        """Delete only migrated (`mig:*`) rows from Layer 1 `memories`; legacy tables remain untouched."""
        where, params = self._scope_clauses(
            scope=scope,
            character_id=character_id,
            migrated_only=True,
        )
        cursor = self.store.execute(f"DELETE FROM memories {where}", tuple(params))
        return int(cursor.rowcount or 0)

    def clear_all(
        self,
        *,
        scope: str | MemoryScope | None = None,
        character_id: str | None = None,
    ) -> int:
        """Clear Layer 1 `memories` (optionally scoped); legacy archive tables remain untouched."""
        where, params = self._scope_clauses(scope=scope, character_id=character_id)
        cursor = self.store.execute(f"DELETE FROM memories {where}", tuple(params))
        return int(cursor.rowcount or 0)

    def for_prompt(
        self,
        scope: str | MemoryScope,
        *,
        character_id: str = "",
        limit: int = DEFAULT_MEMORY_LIMIT,
    ) -> list[dict]:
        """Return active memories strictly confined to `scope` (`owner` or `public`)."""
        scope_str = str(parse_memory_scope(scope))
        char = str(character_id or "").strip()
        rows = self.store.all(
            "SELECT * FROM memories WHERE scope=? AND active=1 "
            "AND (character_id=? OR character_id='') "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (scope_str, char, max(1, min(int(limit), self.limit))),
        )
        return [self._decode(row) for row in rows]

    def prompt_lines(
        self,
        scope: str | MemoryScope,
        *,
        character_id: str = "",
        limit: int = DEFAULT_MEMORY_LIMIT,
    ) -> list[str]:
        return [item["content"] for item in self.for_prompt(scope, character_id=character_id, limit=limit)]


# ---------------------------------------------------------------------------
# OpenAI Function Tool Schemas & Executors for Layer 1 Saved Memories
# ---------------------------------------------------------------------------


class SaveMemoryArgs(StrictModel):
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] = Field(
        description="需要长期记住的简洁事实、稳定偏好或约定（不含密码、密钥或临时闲聊）。"
    )
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] = Field(
        default_factory=list,
        max_length=10,
        description="可选简短标签。",
    )


class UpdateMemoryArgs(StrictModel):
    memory_id: str | None = Field(default=None, max_length=160, description="已知的记忆 ID。")
    match_query: str | None = Field(default=None, max_length=200, description="未传 memory_id 时用于定位唯一旧记忆的关键词。")
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None = Field(
        default=None,
        description="更新后的记忆正文。",
    )
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] | None = Field(
        default=None,
        max_length=10,
        description="更新后的标签列表。",
    )
    active: bool | None = Field(default=None, description="是否保持启用。")


class DeleteMemoryArgs(StrictModel):
    memory_id: str | None = Field(default=None, max_length=160, description="要删除的记忆 ID。")
    query: str | None = Field(default=None, max_length=200, description="未传 memory_id 时用于匹配要删除记忆的关键词。")


def _format_tool_schema(
    name: str,
    description: str,
    model_cls: type[StrictModel],
    *,
    fmt: Literal["chat", "responses"] = "chat",
    require_all: bool = False,
) -> dict[str, Any]:
    spec = tool_function(name, description, model_cls, require_all=require_all)
    if fmt == "responses":
        return spec
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
            "strict": spec["strict"],
        },
    }


def saved_memory_tool_schemas(
    *,
    format: Literal["chat", "responses"] = "chat",
    allow_save: bool = True,
    allow_update: bool = True,
    allow_delete: bool = True,
) -> list[dict[str, Any]]:
    """Build OpenAI Chat Completions or Responses tool schemas for Saved Memories."""
    tools: list[dict[str, Any]] = []
    if allow_save:
        tools.append(
            _format_tool_schema(
                "save_memory",
                "将长期有效的稳定偏好、称呼习惯或重要事实写入已保存记忆（只写值得长期记住的精炼条目，不写一次性流水账）。",
                SaveMemoryArgs,
                fmt=format,
            )
        )
    if allow_update:
        tools.append(
            _format_tool_schema(
                "update_memory",
                "更新或修正一条已保存的长期记忆，避免旧记忆和当前事实冲突。",
                UpdateMemoryArgs,
                fmt=format,
            )
        )
    if allow_delete:
        tools.append(
            _format_tool_schema(
                "delete_memory",
                "删除一条不再成立、重复或对方要求忘掉的已保存记忆。",
                DeleteMemoryArgs,
                fmt=format,
            )
        )
    return tools


def _validate_tool_scope(
    scope: str | MemoryScope,
    allowed_scopes: Iterable[str | MemoryScope] | None,
) -> tuple[str | None, str | None]:
    try:
        resolved = str(parse_memory_scope(scope))
    except ValueError as err:
        return None, str(err)
    if allowed_scopes is not None:
        allowed = {str(parse_memory_scope(item)) for item in allowed_scopes}
        if resolved not in allowed:
            return None, "当前上下文无权操作该作用域记忆"
    return resolved, None


def _tool_result(
    *,
    ok: bool,
    action: str,
    scope: str,
    character_id: str,
    item: dict | None,
    count: int,
    limit: int,
    memory_id: str | None = None,
    snippet: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "action": action,
        "scope": scope,
        "character_id": character_id,
        "item": item,
        "memory_id": memory_id if memory_id is not None else (item["id"] if item else None),
        "snippet": snippet or (item["content"][:120] if item and item.get("content") else ""),
        "count": count,
        "limit": limit,
        "soft_limit_reached": count >= limit,
    }
    if error:
        payload["error"] = error
    return payload


def _resolve_scoped_target(
    unified: UnifiedMemoryStore,
    *,
    scope_str: str,
    char: str,
    memory_id: str | None,
    query: str | None,
) -> tuple[dict | None, str | None]:
    if memory_id:
        existing = unified.get(memory_id)
        if existing is None:
            return None, "not_found"
        if existing["scope"] != scope_str or existing["character_id"] not in {char, ""}:
            return None, "rejected"
        return existing, None
    needle = _normalize(query or "")
    if not needle:
        return None, "not_found"
    candidates = unified.list_memories(scope=scope_str, character_id=char, include_inactive=True, limit=500)
    exact = [row for row in candidates if _normalize(row["content"]) == needle]
    if len(exact) == 1:
        return exact[0], None
    matched = [row for row in candidates if needle in _normalize(row["content"])]
    if len(matched) == 1:
        return matched[0], None
    if len(matched) > 1 or len(exact) > 1:
        return None, "ambiguous"
    return None, "not_found"


def save_memory(
    unified: UnifiedMemoryStore,
    *,
    content: str,
    scope: str | MemoryScope = "owner",
    character_id: str = "",
    tags: Iterable[str] = (),
    source: str = "tool",
    allowed_scopes: Iterable[str | MemoryScope] | None = None,
) -> dict[str, Any]:
    """Execute `save_memory` tool respecting scope isolation, deduplication, and soft cap."""
    char = str(character_id or "").strip()
    scope_str, scope_err = _validate_tool_scope(scope, allowed_scopes)
    if scope_err or scope_str is None:
        return _tool_result(
            ok=False,
            action="rejected",
            scope=str(scope),
            character_id=char,
            item=None,
            count=0,
            limit=unified.limit,
            error=scope_err or "无效记忆作用域",
        )
    try:
        cleaned_text = unified._validate_content(content)
    except ValueError as err:
        count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
        return _tool_result(
            ok=False,
            action="rejected",
            scope=scope_str,
            character_id=char,
            item=None,
            count=count,
            limit=unified.limit,
            error=str(err),
        )

    existing_dup = unified.find_duplicate(cleaned_text, scope=scope_str, character_id=char)
    if existing_dup is not None:
        item = unified.add_memory(
            cleaned_text,
            scope=scope_str,
            character_id=char,
            source=source,
            tags=tags,
        )
        count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
        return _tool_result(
            ok=True,
            action="deduplicated",
            scope=scope_str,
            character_id=char,
            item=item,
            count=count,
            limit=unified.limit,
        )

    current_count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
    if current_count >= unified.limit:
        return _tool_result(
            ok=False,
            action="limit_reached",
            scope=scope_str,
            character_id=char,
            item=None,
            count=current_count,
            limit=unified.limit,
            snippet=cleaned_text[:120],
            error=f"已保存记忆达到 {unified.limit} 条软上限，请先用 update_memory 合并或 delete_memory 删除旧条目。",
        )

    item = unified.add_memory(
        cleaned_text,
        scope=scope_str,
        character_id=char,
        source=source,
        tags=tags,
    )
    new_count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
    return _tool_result(
        ok=True,
        action="created",
        scope=scope_str,
        character_id=char,
        item=item,
        count=new_count,
        limit=unified.limit,
    )


def update_memory(
    unified: UnifiedMemoryStore,
    *,
    memory_id: str | None = None,
    match_query: str | None = None,
    content: str | None = None,
    tags: Iterable[str] | None = None,
    active: bool | None = None,
    scope: str | MemoryScope = "owner",
    character_id: str = "",
    allowed_scopes: Iterable[str | MemoryScope] | None = None,
) -> dict[str, Any]:
    """Execute `update_memory` tool strictly within allowed scope & character."""
    char = str(character_id or "").strip()
    scope_str, scope_err = _validate_tool_scope(scope, allowed_scopes)
    if scope_err or scope_str is None:
        return _tool_result(
            ok=False,
            action="rejected",
            scope=str(scope),
            character_id=char,
            item=None,
            count=0,
            limit=unified.limit,
            error=scope_err or "无效记忆作用域",
        )
    current_count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
    target, err_kind = _resolve_scoped_target(
        unified, scope_str=scope_str, char=char, memory_id=memory_id, query=match_query
    )
    if err_kind is not None or target is None:
        return _tool_result(
            ok=False,
            action=err_kind or "not_found",
            scope=scope_str,
            character_id=char,
            item=None,
            memory_id=memory_id,
            count=current_count,
            limit=unified.limit,
            error="记忆不存在" if err_kind == "not_found" else "存在多条相似记忆，请指定准确编号" if err_kind == "ambiguous" else "无权修改该记忆",
        )
    try:
        updated = unified.update_memory(
            target["id"],
            content=content,
            active=active,
            tags=list(tags) if tags is not None else None,
        )
    except ValueError as err:
        return _tool_result(
            ok=False,
            action="rejected",
            scope=scope_str,
            character_id=char,
            item=target,
            count=current_count,
            limit=unified.limit,
            error=str(err),
        )
    new_count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
    return _tool_result(
        ok=True,
        action="updated",
        scope=scope_str,
        character_id=char,
        item=updated,
        count=new_count,
        limit=unified.limit,
    )


def delete_memory(
    unified: UnifiedMemoryStore,
    *,
    memory_id: str | None = None,
    query: str | None = None,
    scope: str | MemoryScope = "owner",
    character_id: str = "",
    allowed_scopes: Iterable[str | MemoryScope] | None = None,
) -> dict[str, Any]:
    """Execute `delete_memory` tool by memory_id or query strictly within allowed scope & character."""
    char = str(character_id or "").strip()
    scope_str, scope_err = _validate_tool_scope(scope, allowed_scopes)
    if scope_err or scope_str is None:
        return _tool_result(
            ok=False,
            action="rejected",
            scope=str(scope),
            character_id=char,
            item=None,
            count=0,
            limit=unified.limit,
            error=scope_err or "无效记忆作用域",
        )
    current_count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
    target, err_kind = _resolve_scoped_target(
        unified, scope_str=scope_str, char=char, memory_id=memory_id, query=query
    )
    if err_kind is not None or target is None:
        return _tool_result(
            ok=False,
            action=err_kind or "not_found",
            scope=scope_str,
            character_id=char,
            item=None,
            memory_id=memory_id,
            count=current_count,
            limit=unified.limit,
            error="未找到匹配的记忆" if err_kind == "not_found" else "匹配到多条记忆，请给出更精确的关键词" if err_kind == "ambiguous" else "无权删除该记忆",
        )
    unified.delete_memory(target["id"])
    new_count = unified.count_memories(scope=scope_str, character_id=char, active_only=True)
    return _tool_result(
        ok=True,
        action="deleted",
        scope=scope_str,
        character_id=char,
        item=target,
        memory_id=target["id"],
        snippet=target["content"][:120],
        count=new_count,
        limit=unified.limit,
    )


def execute_saved_memory_tool(
    unified: UnifiedMemoryStore,
    name: str,
    arguments: dict[str, Any] | str,
    *,
    scope: str | MemoryScope = "owner",
    character_id: str = "",
    source: str = "tool",
    allowed_scopes: Iterable[str | MemoryScope] | None = None,
) -> dict[str, Any]:
    """Parse and execute `save_memory`, `update_memory`, or `delete_memory` tool calls."""
    char = str(character_id or "").strip()
    if isinstance(arguments, str):
        try:
            parsed_args = json.loads(arguments)
        except ValueError:
            parsed_args = None
    else:
        parsed_args = arguments
    if not isinstance(parsed_args, dict):
        return _tool_result(
            ok=False,
            action="rejected",
            scope=str(scope),
            character_id=char,
            item=None,
            count=0,
            limit=unified.limit,
            error="工具参数须为 JSON 对象",
        )
    try:
        if name == "save_memory":
            args = SaveMemoryArgs.model_validate(parsed_args)
            return save_memory(
                unified,
                content=args.content,
                scope=scope,
                character_id=char,
                tags=args.tags,
                source=source,
                allowed_scopes=allowed_scopes,
            )
        if name == "update_memory":
            args = UpdateMemoryArgs.model_validate(parsed_args)
            return update_memory(
                unified,
                memory_id=args.memory_id,
                match_query=args.match_query,
                content=args.content,
                tags=args.tags,
                active=args.active,
                scope=scope,
                character_id=char,
                allowed_scopes=allowed_scopes,
            )
        if name == "delete_memory":
            args = DeleteMemoryArgs.model_validate(parsed_args)
            return delete_memory(
                unified,
                memory_id=args.memory_id,
                query=args.query,
                scope=scope,
                character_id=char,
                allowed_scopes=allowed_scopes,
            )
    except ValidationError as err:
        return _tool_result(
            ok=False,
            action="rejected",
            scope=str(scope),
            character_id=char,
            item=None,
            count=0,
            limit=unified.limit,
            error=f"记忆工具参数无效：{err.errors()[0].get('msg', 'invalid')}",
        )
    return _tool_result(
        ok=False,
        action="rejected",
        scope=str(scope),
        character_id=char,
        item=None,
        count=0,
        limit=unified.limit,
        error=f"未知记忆工具：{name}",
    )


def migrate_legacy_memories(store, *, unified: UnifiedMemoryStore | None = None) -> int:
    """Explicit one-time idempotent migration from legacy tables into `memories` (legacy tables remain read-only)."""
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
                key = f"mig:dh:{row['id']}"
                target.add_memory(
                    raw_text,
                    scope=scope,
                    character_id=row.get("character_id") or "",
                    source="legacy_daily_highlights",
                    tags=tags if isinstance(tags, list) else [],
                    memory_id=key,
                    source_key=key,
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
                key = f"mig:gm:{row['id']}"
                target.add_memory(
                    raw_text,
                    scope=scope,
                    character_id=row.get("character_id") or "",
                    source="legacy_grounded_memory",
                    memory_id=key,
                    source_key=key,
                )
                migrated += 1
            except ValueError:
                continue
    store.set("unified_memories_migrated_v1", {"version": 1, "migrated_at": time.time(), "count": migrated})
    return migrated


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "DeleteMemoryArgs",
    "SaveMemoryArgs",
    "UnifiedMemoryStore",
    "UpdateMemoryArgs",
    "VALID_SCOPES",
    "delete_memory",
    "execute_saved_memory_tool",
    "migrate_legacy_memories",
    "save_memory",
    "saved_memory_tool_schemas",
    "update_memory",
]
