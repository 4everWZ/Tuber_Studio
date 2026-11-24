"""L2 adapters bridging persona self-model, context tree, and unified memory."""

from __future__ import annotations

from ...core.identity import owner_address_label
from ...persona.context.message_builder import build_messages
from ...persona.memory import recall as _persona_recall
from ...persona.memory import unified as _persona_unified

try:
    from ...persona import self_model as _persona_self_model
except ImportError:
    _persona_self_model = None

try:
    from ...persona.context import tree as _persona_context_tree
except ImportError:
    _persona_context_tree = None

DEFAULT_MEMORY_LIMIT = getattr(_persona_unified, "DEFAULT_MEMORY_LIMIT", 100)


def _snippet(text: str, limit: int = 120) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _memory_receipt(
    store,
    *,
    ok: bool,
    action: str,
    character_id: str,
    item: dict | None = None,
    memory_id: str | None = None,
    snippet: str = "",
) -> dict:
    active = store.for_prompt("owner", character_id=character_id, limit=DEFAULT_MEMORY_LIMIT + 1)
    count = len(active)
    text = snippet or (item.get("content", "") if isinstance(item, dict) else "")
    return {
        "ok": ok,
        "action": action,
        "scope": "owner",
        "character_id": character_id,
        "item": item,
        "memory_id": memory_id or (item.get("id") if isinstance(item, dict) else None),
        "snippet": _snippet(text),
        "count": count,
        "limit": DEFAULT_MEMORY_LIMIT,
        "soft_limit_reached": count >= DEFAULT_MEMORY_LIMIT,
    }


def build_owner_system_prefix(
    char: dict,
    request_id: str,
    user_text: str,
    *,
    account_facts=None,
    scene: str | None = None,
    task_intent: str | None = None,
) -> list[dict[str, str]]:
    """Build owner-chat role prefix via L2 self_model when available."""
    builder = getattr(_persona_self_model, "build_self_model_system_message", None)
    if callable(builder):
        return [
            builder(
                char,
                channel="owner_chat",
                scene=scene,
                task_intent=task_intent,
                operating_account=account_facts,
            )
        ]
    event = {
        "event_id": "operator:" + request_id,
        "text": user_text,
        "sender_name": owner_address_label(char),
        "is_operator": True,
    }
    return build_messages(char, event, {}, "operator_chat", operating_account=account_facts)[:-2]


def build_owner_chat_context(
    char: dict,
    event: dict,
    *,
    account_facts=None,
    memories: tuple | list = (),
    history: tuple | list = (),
    extra_context=None,
    memory_notes: tuple | list = (),
) -> list[dict[str, str]]:
    """Assemble owner-chat prompt messages via L2 ContextTree when available."""
    build_base = getattr(_persona_context_tree, "build_base_node", None)
    create_tree = getattr(_persona_context_tree, "create_context_tree", None)
    fork_branch = getattr(_persona_context_tree, "fork_owner_chat_branch", None)
    render = getattr(_persona_context_tree, "render_openai_messages", None)
    if all(callable(fn) for fn in (build_base, create_tree, fork_branch, render)):
        base = build_base(
            char,
            channel="owner_chat",
            operating_account=account_facts,
            memories=list(memories),
            memory_scope="owner",
        )
        tree = create_tree(base)
        branch = fork_branch(
            tree,
            history=list(history),
            current_message=str(event.get("text") or ""),
            extra_context=extra_context,
        )
        messages = list(render(tree, branch.node_id))
        if memory_notes:
            note_text = "相关记忆资料：" + "\n".join(str(note) for note in memory_notes)
            messages.insert(-1, {"role": "system", "content": note_text})
        return messages

    messages = build_messages(
        char,
        event,
        {},
        "operator_chat",
        history=list(history),
        operating_account=account_facts,
    )
    if history:
        messages[-2:-2] = list(history)
    if memory_notes:
        messages.insert(-1, {"role": "system", "content": "相关记忆资料：" + "\n".join(str(note) for note in memory_notes)})
    if memories:
        lines = [f"- {m['content']}" if isinstance(m, dict) else f"- {m}" for m in memories]
        messages.insert(-1, {"role": "system", "content": "已记住的长期记忆：\n" + "\n".join(lines)})
    return messages


def save_owner_memory(unified_store, *, content: str, character_id: str = "", tags=(), source: str = "chat") -> dict:
    """Save an owner-scoped memory via L2 unified.save_memory (or store fallback)."""
    l2_save = getattr(_persona_unified, "save_memory", None)
    if callable(l2_save):
        return l2_save(
            unified_store,
            content=content,
            scope="owner",
            character_id=character_id,
            tags=tuple(tags),
            source=source,
            allowed_scopes=("owner",),
        )
    before = {row["id"] for row in unified_store.for_prompt("owner", character_id=character_id, limit=100)}
    item = unified_store.add_memory(
        content, scope="owner", character_id=character_id, source=source, tags=tuple(tags)
    )
    created = bool(item and item["id"] not in before)
    return _memory_receipt(
        unified_store,
        ok=bool(item),
        action="created" if created else "deduplicated",
        character_id=character_id,
        item=item,
    )


def update_owner_memory(
    unified_store,
    *,
    memory_id: str | None = None,
    match_query: str | None = None,
    content: str | None = None,
    tags=None,
    character_id: str = "",
) -> dict:
    """Update an existing owner-scoped memory via L2 unified.update_memory."""
    l2_update = getattr(_persona_unified, "update_memory", None)
    if callable(l2_update):
        return l2_update(
            unified_store,
            memory_id=memory_id,
            match_query=match_query,
            content=content,
            tags=tags,
            scope="owner",
            character_id=character_id,
            allowed_scopes=("owner",),
        )
    target_id = memory_id
    if not target_id and match_query:
        needle = " ".join(match_query.lower().split())
        candidates = [
            row
            for row in unified_store.for_prompt("owner", character_id=character_id, limit=100)
            if needle in " ".join(row["content"].lower().split())
        ]
        if len(candidates) > 1:
            return _memory_receipt(
                unified_store, ok=False, action="ambiguous", character_id=character_id, snippet=match_query
            )
        target_id = candidates[0]["id"] if candidates else None
    if not target_id:
        return _memory_receipt(
            unified_store, ok=False, action="not_found", character_id=character_id, snippet=match_query or ""
        )
    existing = unified_store.get(target_id)
    if not existing or existing.get("scope") != "owner":
        return _memory_receipt(
            unified_store, ok=False, action="not_found", character_id=character_id, memory_id=target_id
        )
    updated = unified_store.update_memory(target_id, content=content, tags=tags)
    return _memory_receipt(
        unified_store,
        ok=bool(updated),
        action="updated" if updated else "not_found",
        character_id=character_id,
        item=updated,
        memory_id=target_id,
    )


def delete_owner_memory(
    unified_store,
    *,
    memory_id: str | None = None,
    query: str | None = None,
    character_id: str = "",
) -> dict:
    """Delete an owner-scoped memory via L2 unified.delete_memory."""
    l2_delete = getattr(_persona_unified, "delete_memory", None)
    if callable(l2_delete):
        return l2_delete(
            unified_store,
            memory_id=memory_id,
            query=query,
            scope="owner",
            character_id=character_id,
            allowed_scopes=("owner",),
        )
    if memory_id:
        existing = unified_store.get(memory_id)
        if not existing or existing.get("scope") != "owner":
            return _memory_receipt(
                unified_store, ok=False, action="not_found", character_id=character_id, memory_id=memory_id
            )
        removed = unified_store.delete_memory(memory_id)
        return _memory_receipt(
            unified_store,
            ok=removed,
            action="deleted" if removed else "not_found",
            character_id=character_id,
            memory_id=memory_id,
            snippet=existing.get("content", ""),
        )
    needle = " ".join(str(query or "").lower().split())
    if not needle:
        return _memory_receipt(unified_store, ok=False, action="not_found", character_id=character_id)
    candidates = [
        row
        for row in unified_store.for_prompt("owner", character_id=character_id, limit=100)
        if needle in " ".join(row["content"].lower().split())
    ]
    if len(candidates) == 1:
        target = candidates[0]
        removed = unified_store.delete_memory(target["id"])
        return _memory_receipt(
            unified_store,
            ok=removed,
            action="deleted" if removed else "not_found",
            character_id=character_id,
            memory_id=target["id"],
            snippet=target["content"],
        )
    exact = [row for row in candidates if " ".join(row["content"].lower().split()) == needle]
    if len(exact) == 1:
        target = exact[0]
        removed = unified_store.delete_memory(target["id"])
        return _memory_receipt(
            unified_store,
            ok=removed,
            action="deleted" if removed else "not_found",
            character_id=character_id,
            memory_id=target["id"],
            snippet=target["content"],
        )
    return _memory_receipt(
        unified_store,
        ok=False,
        action="ambiguous" if len(candidates) > 1 else "not_found",
        character_id=character_id,
        snippet=str(query or ""),
    )


def recall_owner_history(
    store,
    *,
    query: str = "",
    character_id: str = "",
    account_id: str = "",
    days: int = 14,
    limit: int = 8,
    resource: str | None = None,
    root_id: str | None = None,
) -> dict:
    """Recall owner-scoped conversation history via L2 recall.recall_history."""
    l2_recall = getattr(_persona_recall, "recall_history", None)
    if callable(l2_recall):
        return l2_recall(
            store,
            query=query,
            scope="owner",
            character_id=character_id,
            account_id=account_id,
            days=days,
            limit=limit,
            resource=resource,
            root_id=root_id,
        )
    import json
    import time

    cutoff = time.time() - max(1, int(days)) * 86400
    needle = (query or "").strip().lower()
    rows = store.all(
        "SELECT request_id, character_id, message, result, created_at FROM operator_chat_turns "
        "ORDER BY created_at DESC, rowid DESC LIMIT 200"
    )
    items = []
    for row in rows:
        try:
            payload = json.loads(row.get("result") or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("hidden"):
            continue
        row_char = row.get("character_id") or payload.get("character_id")
        if character_id and row_char not in (None, "", character_id):
            continue
        if account_id and payload.get("account_id") not in (None, "", account_id):
            continue
        created = float(row.get("created_at") or payload.get("created_at") or 0)
        if created and created < cutoff:
            continue
        user_msg = str(row.get("message") or "")
        reply_text = str(payload.get("reply_text") or "")
        if needle and needle not in user_msg.lower() and needle not in reply_text.lower():
            continue
        items.append({"request_id": row["request_id"], "message": user_msg, "reply_text": reply_text})
        if len(items) >= limit:
            break
    return {"ok": True, "scope": "owner", "items": items, "count": len(items)}
