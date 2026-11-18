"""Authenticated owner-only memory lookup, proposals and explicit confirmations."""

import json
import time
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import Field

from ..daily_memory import MemoryChanged, contains_memory_credential
from ..memory_sources import memory_transaction
from ..models import StrictModel
from ..operation_continuations import content_hash
from .memory_organizer import MemorySuggestion
from . import private_call, voice_origin
from .voice_origin import VoiceOrigin
from .owner_memory import Get, MemoryUse, OwnerNote, Search, check_use, citation, correctable_preference, private_rows


class VoiceCorrection(Get):
    revision: int = Field(gt=0, strict=True)
    use: MemoryUse


class SourceLookup(Get):
    source_key: str | None = Field(default=None, max_length=500)


class Proposal(StrictModel):
    voice_origin: VoiceOrigin | None = None
    correction: VoiceCorrection | None = None
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
    voice_origin.install(runtime.store.db)

    @app.post("/api/agent/memory.search", dependencies=deps)
    async def search(data: Search):
        return {"items": [citation(row) for row in private_rows(runtime, data.query)]}

    @app.post("/api/agent/memory.get", dependencies=deps)
    async def get(data: SourceLookup):
        scope = runtime.memory_organizer.scope()
        rows = memory.organized(scope, item_id=data.id) if memory.preference(scope)["enabled"] else []
        if not rows or rows[0]["status"] != "active" or not (row := memory.owner_projection(rows[0])):
            raise HTTPException(404, "当前没有可引用的这条记忆")
        if data.source_key:
            hits = memory.lookup_source(scope, "operator", data.source_key)
            exact = next((hit for hit in hits if hit["id"] == data.id and hit["source_key"] == data.source_key), None)
            if not exact:
                raise HTTPException(404, "当前没有可核对的这条来源")
            row = row | {"exact_source":exact}
        return voice_origin.attach(runtime.store, citation(row))

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
        if origin := data.voice_origin:
            service = private_call.private_service()
            if data.source != "confirmed_voice" or not service or contains_memory_credential(origin.machine_text):
                raise HTTPException(409, "此转写来源不能保存")
            voice_origin.verify_origin(origin, service[1], runtime.tasks.scope())
        target = None
        if correction := data.correction:
            if data.source != "confirmed_voice" or correction.id not in correction.use.ids:
                raise HTTPException(422, "更正须选择本次通话引用的号主偏好")
            rows = memory.organized(scope, item_id=correction.id)
            if (not rows or not correctable_preference(rows[0], memory.private_owner)
                    or rows[0]["revision"] != correction.revision):
                raise HTTPException(412, "原偏好已更改或撤回，请重新选择")
            try:
                check_use(runtime, correction.use)
            except MemoryChanged as error:
                raise HTTPException(409, str(error)) from error
            target = rows[0]
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
            if target:
                key = "confirmed_voice:" + content_hash([key, target["id"], target["revision"]])
            if origin:
                key = "confirmed_voice:" + content_hash([origin.ticket_hash, origin.capture_id])
            at = time.time()
        source = {"kind": "private", "key": key, "text": data.text, "at": at,
                  "private_owner": memory.private_owner}
        stamp = content_hash([runtime.tasks.scope(), pref, key, data.text,
                              data.correction.model_dump() if target else None])
        return scope, source, stamp, target

    @app.post("/api/agent/memory.propose", dependencies=deps)
    async def propose(data: Proposal):
        _, source, stamp, target = proposal(data)
        return {"text": source["text"], "stamp": stamp, **({"replaces": citation(target)} if target else {}),
                **({"machine_text":data.voice_origin.machine_text} if data.voice_origin else {})}

    @app.post("/api/agent/memory/confirm", dependencies=deps)
    async def confirm(data: Confirmation):
        scope, source, stamp, target = proposal(data)
        if data.stamp != stamp:
            raise HTTPException(409, "记忆设置已变化，请重新确认")
        if target:
            decision = MemorySuggestion(action="update", existing_id=target["id"], headline=data.text[:40],
                                        note=data.text, tags=[])
            with memory_transaction(runtime.store.db):
                if not memory.save_organized(scope, source, decision, target):
                    raise HTTPException(412, "原偏好或来源已变化，请重新选择")
                hits = memory.lookup_source(scope, "operator", source["key"])
                if len(hits) != 1 or hits[0]["id"] != target["id"] or hits[0]["quote"] != data.text:
                    raise HTTPException(409, "新原话与其他条目冲突，更正尚未保存")
                if data.voice_origin:
                    voice_origin.save(runtime.store.db, target["id"], source["key"], data.voice_origin)
                return {"id": target["id"], "revision": hits[0]["revision"]}
        with memory_transaction(runtime.store.db):
            existing = memory.lookup_source(scope, "operator", source["key"])
            if not existing:
                decision = MemorySuggestion(action="add", existing_id=None, headline=data.text[:40], note=data.text, tags=[])
                if not memory.save_organized(scope, source, decision, None):
                    raise HTTPException(409, "该来源已撤回或无法核实")
                existing = memory.lookup_source(scope, "operator", source["key"])
            if not existing or existing[0]["quote"] != data.text:
                raise HTTPException(409, "来源已变化，请重新读取")
            if data.voice_origin:
                voice_origin.save(runtime.store.db, existing[0]["id"], source["key"], data.voice_origin)
            return {"id": existing[0]["id"]}
