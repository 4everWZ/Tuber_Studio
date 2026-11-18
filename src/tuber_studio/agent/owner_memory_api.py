"""Authenticated owner-only memory lookup, proposals and explicit confirmations."""

import json
import time
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import Field

from ..daily_memory import MemoryChanged, contains_memory_credential
from ..models import StrictModel
from ..operation_continuations import content_hash
from .memory_organizer import MemorySuggestion
from .owner_memory import Get, MemoryUse, OwnerNote, Search, check_use, citation, private_rows


class Proposal(StrictModel):
    source: Literal["turn", "confirmed_voice", "settings", "task"]
    text: OwnerNote
    turn_id: UUID | None = None
    task_id: UUID | None = None
    source_id: str | None = Field(default=None, max_length=100)


class Confirmation(Proposal):
    stamp: str = Field(pattern=r"^[0-9a-f]{64}$")


def register_owner_memory(app, runtime, chat, require_admin):
    deps = [Depends(require_admin)]
    memory = runtime.daily_memory

    @app.post("/api/agent/memory.search", dependencies=deps)
    async def search(data: Search):
        return {"items": [citation(row) for row in private_rows(runtime, data.query)]}

    @app.post("/api/agent/memory.get", dependencies=deps)
    async def get(data: Get):
        scope = runtime.memory_organizer.scope()
        rows = memory.organized(scope, item_id=data.id) if memory.preference(scope)["enabled"] else []
        if not rows or rows[0]["status"] != "active" or not (row := memory.owner_projection(rows[0])):
            raise HTTPException(404, "当前没有可引用的这条记忆")
        return citation(row)

    @app.post("/api/agent/memory/validate", dependencies=deps)
    async def validate(data: MemoryUse):
        try:
            check_use(runtime, data)
        except MemoryChanged as error:
            raise HTTPException(409, str(error)) from error
        return {"valid": True}

    def proposal(data):
        scope = runtime.memory_organizer.scope()
        pref = memory.preference(scope)
        if not pref["enabled"] or contains_memory_credential(data.text):
            raise HTTPException(409, "当前设置或内容不允许保存这句话")
        if data.source != "task" and (data.task_id or data.source_id):
            raise HTTPException(422, "此来源不能指定任务")
        if data.source == "turn":
            if not data.turn_id:
                raise HTTPException(422, "请指定号主原话")
            turn = chat.current_turn(str(data.turn_id), read_only=True)
            if (json.loads(turn["result"]).get("owner_source") != memory.private_owner
                    or chat.visible_turn(turn)["message"] != data.text):
                raise HTTPException(409, "只能保存当前可见的号主原话")
            key, at = f"turn:{data.turn_id}", turn["created_at"]
        elif data.source == "task":
            task = runtime.tasks.current(str(data.task_id))
            if not task or not runtime.tasks.can_control(task) or data.turn_id:
                raise HTTPException(404, "没有可确认的任务建议")
            for step in task["steps"]:
                result = step.get("result") or {}
                if (step["revision"] == task["revision"] and
                        {"source_id": data.source_id, "quote": data.text} in result.get("memory_proposals", [])):
                    try:
                        check_use(runtime, MemoryUse.model_validate(result["memory_use"]))
                    except MemoryChanged as error:
                        raise HTTPException(409, str(error)) from error
                    break
            else:
                raise HTTPException(409, "原任务没有这条待确认建议")
            key = f"task:{task['id']}:{data.source_id}:" + content_hash(data.text)
            at = task["updated"]
        else:
            if data.turn_id:
                raise HTTPException(422, "手动确认的内容不能指定历史消息")
            key = data.source + ":" + content_hash([memory.private_owner, scope[1], data.text])
            at = time.time()
        source = {"kind": "private", "key": key, "text": data.text, "at": at,
                  "private_owner": memory.private_owner}
        return scope, source, content_hash([runtime.tasks.scope(), pref, key, data.text])

    @app.post("/api/agent/memory.propose", dependencies=deps)
    async def propose(data: Proposal):
        _, source, stamp = proposal(data)
        return {"text": source["text"], "stamp": stamp}

    @app.post("/api/agent/memory/confirm", dependencies=deps)
    async def confirm(data: Confirmation):
        scope, source, stamp = proposal(data)
        if data.stamp != stamp:
            raise HTTPException(409, "记忆设置已变化，请重新确认")
        existing = memory.lookup_source(scope, "operator", source["key"])
        if not existing:
            decision = MemorySuggestion(action="add", existing_id=None, headline=data.text[:40], note=data.text, tags=[])
            if not memory.save_organized(scope, source, decision, None):
                raise HTTPException(409, "该来源已撤回或无法核实")
            existing = memory.lookup_source(scope, "operator", source["key"])
        if not existing:
            raise HTTPException(409, "来源已变化，请重新读取")
        return {"id": existing[0]["id"]}
