"""Owner-only memory schemas and proposal validation helpers."""

import json
import time
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field

from ...core.models import StrictModel, content_hash
from ...persona.memory.daily import MemoryChanged, contains_memory_credential
from ..memory_organizer.owner_memory import (
    Get,
    MemoryUse,
    OwnerNote,
    check_use,
    correctable_preference,
)
from ..voice import origin as voice_origin
from ..voice import private_call
from ..voice.origin import VoiceOrigin


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


def build_memory_proposal(runtime, chat, data: Proposal):
    """Validate proposal provenance and compute its deterministic confirmation stamp."""
    memory = runtime.daily_memory
    scope = runtime.memory_organizer.scope()
    pref = memory.preference(scope)
    if not pref["enabled"] or contains_memory_credential(data.text):
        raise HTTPException(409, "当前设置或内容不允许保存这句话")
    if origin := data.voice_origin:
        service = private_call.private_service(runtime.settings)
        if data.source != "confirmed_voice" or not service or contains_memory_credential(origin.machine_text):
            raise HTTPException(409, "此转写来源不能保存")
        voice_origin.verify_origin(origin, service[1], runtime.tasks.scope())
    target = None
    if correction := data.correction:
        if data.source != "confirmed_voice" or correction.id not in correction.use.ids:
            raise HTTPException(422, "更正须选择本次通话引用的号主偏好")
        rows = memory.organized(scope, item_id=correction.id)
        if (
            not rows
            or not correctable_preference(rows[0], memory.private_owner)
            or rows[0]["revision"] != correction.revision
        ):
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
        if (
            json.loads(turn["result"]).get("owner_source") != memory.private_owner
            or chat.visible_turn(turn)["message"] != data.text
        ):
            raise HTTPException(409, "只能保存当前可见的号主原话")
        key, at = f"turn:{data.turn_id}", turn["created_at"]
    elif data.source == "task":
        task = runtime.tasks.current(str(data.task_id))
        if not task or not runtime.tasks.can_control(task) or data.turn_id:
            raise HTTPException(404, "没有可确认的任务建议")
        for step in task["steps"]:
            result = step.get("result") or {}
            if (
                step["revision"] == task["revision"]
                and {"source_id": data.source_id, "quote": data.text} in result.get("memory_proposals", [])
            ):
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
    source = {
        "kind": "private",
        "key": key,
        "text": data.text,
        "at": at,
        "private_owner": memory.private_owner,
        "owner_confirmed": True,
    }
    stamp = content_hash(
        [
            runtime.tasks.scope(),
            pref,
            key,
            data.text,
            data.correction.model_dump() if target else None,
        ]
    )
    return scope, source, stamp, target
