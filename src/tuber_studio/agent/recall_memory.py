"""Scoped recall over source records. The models choose queries and citations."""

import time
from datetime import timedelta
from uuid import UUID, uuid5

from ..core.models import StrictModel
from ..persona.memory.cooperation import applicable_chat_memory
from ..persona.memory.recall import (
    DESCRIPTION,
    NAME,
    MaterialFilter,
    RecallQuery,
    Selection,
    canonical_id,
    citations,
    match_public_sources,
    model_material,
    originals,
    parse_recall_query_bounds,
    resolve_timezone,
    source_href,
    trim_repeated_quotes,
)
from ..persona.memory.recall import RecallSession as BaseRecallSession
from .operator_facts import now
from .owner_memory import citation, history_preference, private_rows, read_stamp
from .workshop_lookup import period

__all__ = [
    "DESCRIPTION",
    "MaterialFilter",
    "NAME",
    "RecallQuery",
    "RecallSession",
    "Selection",
    "StrictModel",
    "canonical_id",
    "check_sources",
    "citations",
    "continue_task",
    "delivery_citations",
    "model_material",
    "originals",
    "read_authority",
    "recent_query",
    "search",
    "source_href",
    "trim_repeated_quotes",
    "validate_read",
]


def check_sources(dynamics, items):
    public = [item for item in items if item["type"] != "private_memory"]
    char = dynamics._character()
    if public and not dynamics._valid_sources(
        {"character_id": char["id"], "character_version": char["version"], "sources": public}
    ):
        raise ValueError("引用的素材或授权已变化")


def read_authority(runtime, scope):
    return {"recall_scope": scope, "recall_history": history_preference(runtime)}


def validate_read(dynamics, receipt):
    runtime = dynamics.runtime
    if receipt["recall_scope"] != runtime.tasks.scope() or receipt["recall_history"] != history_preference(runtime):
        raise ValueError("记忆引用权限已变化")
    check_sources(dynamics, receipt["items"])


def search(dynamics, args, *, private=False):
    runtime = dynamics.runtime
    browsing = getattr(runtime, "browsing", None)
    if browsing is not None:
        tz = resolve_timezone(dynamics)
        terms, _, _, query = parse_recall_query_bounds(args, timezone=tz)
        start = period(browsing, args.start_day)["start_at"] if args.start_day else None
        end = period(browsing, args.end_day)["next_at"] if args.end_day else None
        if start != query.start or end != query.end:
            query = MaterialFilter(terms, start, end, args.resource, args.root_id)
    else:
        terms, start, end, query = parse_recall_query_bounds(args, timezone="Asia/Taipei")
    sources = dynamics._sources(bound_ids=[args.source_id] if args.source_id else [], query=query)
    matches = match_public_sources(sources, args, terms=terms, start=start, end=end)
    prefs = []
    if private and (args.topic or args.source_id or start is not None or end is not None or args.resource):
        scope = runtime.memory_organizer.scope()
        if args.source_id and args.source_id.startswith("private-memory:"):
            identity = args.source_id.removeprefix("private-memory:")
            rows = runtime.daily_memory.organized(scope, item_id=int(identity)) if identity.isdecimal() else []
        elif start is not None or end is not None or args.resource:
            rows = runtime.daily_memory.organized(scope, material_query=query)
        else:
            rows = private_rows(runtime, args.source_id or args.topic)
        candidates = [
            projected
            for row in rows
            if row["status"] == "active"
            and applicable_chat_memory(row)
            and (projected := runtime.daily_memory.owner_projection(row))
        ]
        if runtime.daily_memory.preference(scope)["enabled"]:
            for row in candidates:
                anchor = row["source"].get("thread") or {}
                seen_at = row.get("created_at") or row["updated_at"]
                if (
                    args.resource and anchor.get("resource") != args.resource
                    or args.root_id and str(anchor.get("root_id")) != args.root_id
                    or start is not None and seen_at < start
                    or end is not None and seen_at >= end
                ):
                    continue
                identity = f"private-memory:{row['id']}"
                note = citation(row)
                if args.source_id and args.source_id not in {identity, note["source_key"]}:
                    continue
                prefs.append(row)
                matches[identity] = (
                    len(args.topic) + 1,
                    seen_at,
                    {
                        "id": identity,
                        "type": "private_memory",
                        "text": note["quote"],
                        "observed_at": seen_at,
                        "revision": row["revision"],
                        "source_key": note["source_key"],
                    },
                )
    ranked = sorted(matches.values(), key=lambda entry: (-entry[0], -entry[1], entry[2]["id"]))
    items = [source for _, _, source in ranked[:8]]
    result = {"items": items, "state": "ok" if items else "empty"}
    use = [row for row in prefs if any(s["id"] == f"private-memory:{row['id']}" for s in items)]
    if use:
        result["memory_use"] = {"ids": [row["id"] for row in use], "stamp": read_stamp(runtime, use)}
    return result


def delivery_citations(steps, selection):
    selected = {source for item in selection.get("items", []) for source in item.get("sources", [])}
    sources = {}
    for step in steps:
        if step["tool"] != NAME:
            continue
        for index, source in enumerate((step.get("result") or {}).get("items", []), 1):
            if f"{step['id']}:{index}" in selected:
                sources.setdefault(canonical_id(source), source)
    return citations(sources.values())


class RecallSession(BaseRecallSession):
    def __init__(self, dynamics, *, private, scope=None):
        super().__init__(dynamics, private=private, scope=scope)
        self.history_policy = history_preference(self.runtime) if private else None

    def check(self):
        if self.private:
            if self.runtime.tasks.scope() != self.scope or history_preference(self.runtime) != self.history_policy:
                raise PermissionError("记忆读取权限已变化")
            self.runtime.tasks._validate_reads(self.receipts, None)
            check_sources(self.dynamics, list(self.sources.values()))

    def _search(self, args):
        result = search(self.dynamics, args, private=self.private)
        if self.private:
            result |= read_authority(self.runtime, self.scope)
        return result


async def continue_task(tasks, task):
    client = tasks.responses_client()
    if client is None:
        tasks._save(task, status="waiting", due=time.time() + 30, message="等待后台记忆查询服务。")
        return
    runtime = tasks.recall_dynamics.runtime
    initial = [step for step in task["steps"] if step["tool"] == NAME and "items" in (step.get("result") or {})]
    session = RecallSession(tasks.recall_dynamics, private=True, scope=task["scope"])
    session.restore(initial)
    brief = task["plan"]["delegation_brief"]
    async with runtime.offline_generation("background"):
        selected = await session.collect(client, {"问题": task["goal"], "检索线索": brief["materials"]})
    session.check()
    steps = [
        *task["steps"],
        *[
            {
                "id": str(uuid5(UUID(task["id"]), f"recall:{task['revision']}:{index}")),
                "revision": task["revision"],
                **receipt,
            }
            for index, receipt in enumerate(session.receipts[len(initial) :], start=len(initial))
        ],
    ]
    if not selected:
        tasks._save(task, steps=steps, status="completed", message="这轮没有找到对应的记录。")
        return
    wanted = {canonical_id(source) for source in selected}
    refs = {}
    for step in steps:
        if step["tool"] == NAME:
            for index, source in enumerate((step.get("result") or {}).get("items", []), 1):
                key = canonical_id(source)
                if key in wanted:
                    refs.setdefault(key, f"{step['id']}:{index}")
    candidate = {
        "items": [{"title": "查到的记录", "rationale": session.conclusion or "找到了对应的记录。", "sources": list(refs.values())}],
        "gaps": [],
    }
    tasks._save(
        task,
        steps=steps,
        plan=task["plan"] | {"candidate": candidate},
        status="queued",
        due=time.time(),
        message="查到了相关记录，正在准备交付。",
    )


def recent_query(browsing):
    end = now(browsing).date()
    return RecallQuery(start_day=end - timedelta(days=2), end_day=end)
