"""Memory recall, source citations and revocable snapshots for private owner requests."""

from typing import Annotated

from pydantic import Field, StringConstraints

from ..models import StrictModel
from ..operation_continuations import content_hash
from .owner_memory_use import MemoryUse as MemoryUse, check_use as check_use, read_stamp as read_stamp


QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
OwnerNote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


class Search(StrictModel):
    query: QueryText


class Get(StrictModel):
    id: int = Field(gt=0, strict=True)


def history_preference(runtime, enabled=None):
    key = "owner_chat_history:" + content_hash(runtime.tasks.identity.reader())
    value = runtime.store.get(key, {"history_enabled": True, "history_revision": 0})
    if enabled is not None and value["history_enabled"] != enabled:
        value = {"history_enabled": enabled, "history_revision": value["history_revision"] + 1}
        runtime.store.set(key, value)
    return value


def private_rows(runtime, query):
    memory = runtime.daily_memory
    scope = runtime.memory_organizer.scope()
    if not memory.preference(scope)["enabled"] or not query.strip():
        return []
    matched = memory.recall_organized(scope, "operator", query, specific_experiences=True)
    indexed = {}
    for hit in memory.lookup_source(scope, "operator", query):
        rows = memory.organized(scope, item_id=hit["id"])
        if rows and (row := memory.owner_projection(rows[0])):
            indexed[row["id"]] = row | {"exact_source": hit}
    for item in matched:
        indexed.setdefault(item["id"], item)
    return list(indexed.values())[:8]


def citation(row):
    source = row.get("exact_source") or {"quote": row["quote"], "source_key": row["source"]["key"]}
    return {"id": row["id"], "revision": row["revision"], "headline": row["headline"],
            "note": row["anecdote"], "quote": source["quote"], "source_key": source["source_key"]}
