"""Unified Memory Layer 1/2 tool bridge with pre-merge fallback matching Phase A spec."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ...persona.memory import recall as _recall_mod
from ...persona.memory import unified as _unified_mod

DEFAULT_MEMORY_LIMIT = 100

try:
    from ...core.agent_types import MemoryScope
except ImportError:
    from enum import StrEnum

    class MemoryScope(StrEnum):
        OWNER = "owner"
        PUBLIC = "public"


if hasattr(_unified_mod, "saved_memory_tool_schemas") and hasattr(_unified_mod, "execute_saved_memory_tool"):
    saved_memory_tool_schemas = _unified_mod.saved_memory_tool_schemas
    save_memory = _unified_mod.save_memory
    execute_saved_memory_tool = _unified_mod.execute_saved_memory_tool
else:

    def saved_memory_tool_schemas(
        *, format: str = "chat", allow_save: bool = True, allow_update: bool = True, allow_delete: bool = True
    ) -> list[dict[str, Any]]:
        defs: list[tuple[str, str, dict[str, Any]]] = []
        if allow_save:
            defs.append(
                (
                    "save_memory",
                    "把值得记住的事实、观众梗、自我观察或角色记忆保存为长期记忆。",
                    {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "清晰、独立的一条记忆内容。"},
                            "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签列表。"},
                        },
                        "required": ["content"],
                        "additionalProperties": False,
                    },
                )
            )
        if allow_update:
            defs.append(
                (
                    "update_memory",
                    "更新已有记忆内容。",
                    {
                        "type": "object",
                        "properties": {
                            "memory_id": {"type": "string"},
                            "match_query": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                )
            )
        if allow_delete:
            defs.append(
                (
                    "delete_memory",
                    "删除不再准确或作废的记忆。",
                    {
                        "type": "object",
                        "properties": {
                            "memory_id": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                )
            )
        if format == "responses":
            return [{"type": "function", "name": n, "description": d, "parameters": p} for n, d, p in defs]
        return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}} for n, d, p in defs]

    def _memory_table_columns(unified_store) -> set[str]:
        if hasattr(unified_store, "store"):
            return {r["name"] for r in unified_store.store.all("PRAGMA table_info(memories)")}
        return set()

    def save_memory(
        unified_store,
        *,
        content: str,
        scope: str = "owner",
        character_id: str = "",
        tags: tuple | list = (),
        source: str = "tool",
        allowed_scopes=None,
    ) -> dict[str, Any]:
        scope_val = str(scope.value if isinstance(scope, MemoryScope) else scope)
        allowed = {str(s.value if isinstance(s, MemoryScope) else s) for s in (allowed_scopes or {scope_val})}
        if scope_val not in allowed:
            return {
                "ok": False,
                "action": "rejected",
                "scope": scope_val,
                "character_id": character_id,
                "item": None,
                "memory_id": None,
                "snippet": "",
                "count": 0,
                "limit": DEFAULT_MEMORY_LIMIT,
                "soft_limit_reached": False,
            }
        text = " ".join(str(content or "").split())
        if not text:
            return {
                "ok": False,
                "action": "rejected",
                "scope": scope_val,
                "character_id": character_id,
                "item": None,
                "memory_id": None,
                "snippet": "",
                "count": 0,
                "limit": DEFAULT_MEMORY_LIMIT,
                "soft_limit_reached": False,
            }
        now = time.time()
        cols = _memory_table_columns(unified_store)
        if "memories" in {r["name"] for r in unified_store.store.all("SELECT name FROM sqlite_master WHERE type='table'")}:
            existing = unified_store.store.all(
                "SELECT * FROM memories WHERE scope=? AND character_id=? AND active=1",
                (scope_val, character_id),
            )
            for row in existing:
                row_text = " ".join(str(row.get("content") or row.get("text") or "").split())
                if row_text == text:
                    return {
                        "ok": True,
                        "action": "deduplicated",
                        "scope": scope_val,
                        "character_id": character_id,
                        "item": row,
                        "memory_id": str(row.get("id") or ""),
                        "snippet": text[:80],
                        "count": len(existing),
                        "limit": DEFAULT_MEMORY_LIMIT,
                        "soft_limit_reached": len(existing) >= DEFAULT_MEMORY_LIMIT,
                    }
            if len(existing) >= DEFAULT_MEMORY_LIMIT:
                return {
                    "ok": False,
                    "action": "limit_reached",
                    "scope": scope_val,
                    "character_id": character_id,
                    "item": None,
                    "memory_id": None,
                    "snippet": "",
                    "count": len(existing),
                    "limit": DEFAULT_MEMORY_LIMIT,
                    "soft_limit_reached": True,
                }
            mem_id = f"mem:{uuid.uuid4().hex[:12]}"
            if "content" in cols:
                unified_store.store.execute(
                    "INSERT INTO memories(id,scope,character_id,content,source,tags,created_at,updated_at,active) "
                    "VALUES(?,?,?,?,?,?,?,?,1)",
                    (mem_id, scope_val, character_id, text, source, json.dumps(list(tags or ()), ensure_ascii=False), now, now),
                )
            elif hasattr(unified_store, "add"):
                saved = unified_store.add(scope_val, character_id, text, source=source, tags=list(tags or ()))
                mem_id = str((saved or {}).get("id") or mem_id)
            item = {"id": mem_id, "scope": scope_val, "character_id": character_id, "content": text, "tags": list(tags or ()), "active": True}
            return {
                "ok": True,
                "action": "created",
                "scope": scope_val,
                "character_id": character_id,
                "item": item,
                "memory_id": mem_id,
                "snippet": text[:80],
                "count": len(existing) + 1,
                "limit": DEFAULT_MEMORY_LIMIT,
                "soft_limit_reached": (len(existing) + 1) >= DEFAULT_MEMORY_LIMIT,
            }
        if hasattr(unified_store, "add"):
            saved = unified_store.add(scope_val, character_id, text, source=source, tags=list(tags or ()))
            return {
                "ok": True,
                "action": "created",
                "scope": scope_val,
                "character_id": character_id,
                "item": saved,
                "memory_id": str((saved or {}).get("id") or ""),
                "snippet": text[:80],
                "count": 1,
                "limit": DEFAULT_MEMORY_LIMIT,
                "soft_limit_reached": False,
            }
        raise RuntimeError("UnifiedMemoryStore unavailable")

    def execute_saved_memory_tool(
        unified_store,
        name: str,
        arguments: dict[str, Any] | str,
        *,
        scope: str = "owner",
        character_id: str = "",
        source: str = "tool",
        allowed_scopes=None,
    ) -> dict[str, Any]:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if name == "save_memory":
            return save_memory(
                unified_store,
                content=str(args.get("content") or ""),
                scope=scope,
                character_id=character_id,
                tags=tuple(args.get("tags") or ()),
                source=source,
                allowed_scopes=allowed_scopes,
            )
        return {
            "ok": False,
            "action": "rejected",
            "scope": str(scope),
            "character_id": character_id,
            "item": None,
            "memory_id": None,
            "snippet": "",
            "count": 0,
            "limit": DEFAULT_MEMORY_LIMIT,
            "soft_limit_reached": False,
        }


if hasattr(_recall_mod, "recall_history_tool_schema") and hasattr(_recall_mod, "recall_history"):
    recall_history_tool_schema = _recall_mod.recall_history_tool_schema
    recall_history = _recall_mod.recall_history
else:

    def recall_history_tool_schema(*, format: str = "chat") -> dict[str, Any]:
        params = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的关键词或话题描述。"},
                "days": {"type": "integer", "description": "最近天数范围，默认14。"},
                "limit": {"type": "integer", "description": "返回条目上限，默认8。"},
                "resource": {"type": ["string", "null"], "description": "可选，限定视频BV号或动态ID。"},
                "root_id": {"type": ["string", "null"], "description": "可选，限定评论根楼层ID。"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        if format == "responses":
            return {
                "type": "function",
                "name": "recall_history",
                "description": "按主题回顾过去在当前公开作用域内发过的话、看过的视频或交互历史。",
                "parameters": params,
            }
        return {
            "type": "function",
            "function": {
                "name": "recall_history",
                "description": "按主题回顾过去在当前公开作用域内发过的话、看过的视频或交互历史。",
                "parameters": params,
            },
        }

    def recall_history(
        store,
        *,
        query: str = "",
        scope: str = "owner",
        character_id: str = "",
        account_id: str = "",
        days: int = 14,
        limit: int = 8,
        private_owner=None,
        resource=None,
        root_id=None,
    ) -> dict[str, Any]:
        scope_val = str(scope.value if isinstance(scope, MemoryScope) else scope)
        items: list[dict[str, Any]] = []
        keywords = [k.strip().lower() for k in str(query or "").split() if k.strip()]
        if hasattr(store, "all"):
            tables = {r["name"] for r in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" in tables:
                rows = store.all(
                    "SELECT * FROM memories WHERE scope=? AND character_id=? AND active=1 ORDER BY updated_at DESC LIMIT 60",
                    (scope_val, character_id),
                )
                for row in rows:
                    text = str(row.get("content") or row.get("text") or "")
                    if not keywords or any(k in text.lower() for k in keywords) or str(query or "").strip() in text:
                        items.append({"kind": "memory", "id": row.get("id"), "content": text, "source": row.get("source")})
            if "browsing_intents" in tables:
                since = time.time() - max(1, int(days or 14)) * 86400
                rows = store.all(
                    "SELECT id,kind,text,target,updated_at FROM browsing_intents "
                    "WHERE character_id=? AND status='published' AND updated_at>=? ORDER BY updated_at DESC LIMIT 40",
                    (character_id, since),
                )
                for row in rows:
                    if account_id and str(row.get("account_id") or account_id) != str(account_id):
                        continue
                    target = json.loads(row.get("target") or "{}")
                    if resource and target.get("bvid") != resource and target.get("dynamic_id") != resource:
                        continue
                    if root_id and str(target.get("root") or "") != str(root_id):
                        continue
                    text = str(row.get("text") or "")
                    if not text:
                        continue
                    if not keywords or any(k in text.lower() for k in keywords) or str(query or "").strip() in text:
                        items.append({"kind": row.get("kind"), "id": row.get("id"), "content": text, "target": target})
        items = items[: max(1, min(int(limit or 8), 24))]
        return {"ok": True, "scope": scope_val, "items": items, "count": len(items)}
