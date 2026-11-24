"""L4 agent routes (`/api/v1/agent/*`): chat, private-call voice/guards, owner memory, reply selections, tasks."""

from __future__ import annotations

import asyncio
import json
import time
from secrets import token_urlsafe
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ...agent.api.owner_memory import (
    Confirmation,
    Proposal,
    SourceLookup,
    build_memory_proposal,
)
from ...agent.chat import OperatorChat
from ...agent.chat.lookup.reply_operations import ReplySelection, attach_reply_operations
from ...agent.chat.models import ChatTurn, ControlConfirmation
from ...agent.memory_organizer import MemorySuggestion
from ...agent.memory_organizer.owner_memory import (
    MemoryUse,
    Search,
    check_use,
    citation,
    private_rows,
)
from ...agent.memory_organizer.writer import connect_private_writer
from ...agent.skills import register_remotion_tools
from ...agent.voice import avatar as voice_avatar
from ...agent.voice import origin as voice_origin
from ...agent.voice import private_call as voice_private_call
from ...agent.voice import work as voice_work
from ...agent.voice.guard import MAX_ACTIVE_CALL_GUARDS, CallGuard, CallUse, check_call_use
from ...core.store import dump
from ...persona.memory.daily import MemoryChanged
from ...persona.memory.sources import memory_transaction
from .agent_tasks import register_task_routes
from .system import ERROR_RESPONSES

__all__ = ["register_agent_routes", "register_task_routes"]


def register_agent_routes(
    app,
    runtime,
    store,
    browsing,
    dynamics,
    comments,
    messages,
    require_admin,
    *,
    call_guards: dict | None = None,
):
    """Register all `/api/v1/agent/*` routes and return the initialized OperatorChat instance."""
    deps = [Depends(require_admin)]
    prefix = "/api/v1/agent"
    guards = call_guards if call_guards is not None else {}
    app.state.call_guards = guards

    connect_private_writer(runtime)
    voice_origin.install(runtime.store.db)
    operator_chat = OperatorChat(runtime, store, browsing, dynamics)
    reply_ops = attach_reply_operations(runtime.tasks, comments, messages)
    register_remotion_tools(runtime.tasks, runtime=runtime, store=store, browsing=browsing)
    register_task_routes(app, runtime.tasks, require_admin)

    # --- Operator Chat (13) ---
    @app.get(prefix + "/chat", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def chat_history(limit: int = Query(20, ge=1, le=100), before: UUID | None = None):
        return operator_chat.history(limit, str(before) if before else None)

    @app.post(prefix + "/chat", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def chat_send(data: ChatTurn):
        return await operator_chat.send(data)

    @app.delete(prefix + "/chat", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def chat_clear():
        return await operator_chat.clear()

    @app.get(prefix + "/chat/events", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def chat_delivery_events():
        return StreamingResponse(
            operator_chat.transport.delivery_events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post(prefix + "/chat/stream", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def chat_stream(data: ChatTurn):
        return StreamingResponse(
            operator_chat.stream(data),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get(prefix + "/chat/turns/{request_id}", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def get_turn(request_id: UUID):
        return operator_chat.visible_turn(operator_chat.current_turn(str(request_id), read_only=True))

    @app.get(
        prefix + "/chat/turns/{request_id}/inspection-image",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def inspection_image(request_id: UUID):
        return operator_chat.inspection_image(str(request_id))

    @app.get(
        prefix + "/chat/turns/{request_id}/artifact", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"]
    )
    async def cited_artifact(request_id: UUID):
        turn = operator_chat.visible_turn(operator_chat.current_turn(str(request_id), read_only=True))
        card = turn.get("card") or {}
        if card.get("type") != "history_artifact":
            raise HTTPException(404, "当前无法引用这份成果")
        return operator_chat.task_history.read(turn["task_reference"], card["data"])["delivery"]

    @app.post(
        prefix + "/chat/turns/{request_id}/cancel-video", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"]
    )
    async def cancel_video(request_id: UUID):
        return await operator_chat.update_video(str(request_id), resume=False)

    @app.post(
        prefix + "/chat/turns/{request_id}/resume-video", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"]
    )
    async def resume_video(request_id: UUID):
        return await operator_chat.update_video(str(request_id), resume=True)

    @app.post(
        prefix + "/chat/turns/{request_id}/stop-generation",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def stop_generation(request_id: UUID):
        return operator_chat.stop_generation(str(request_id))

    @app.post(
        prefix + "/chat/turns/{request_id}/confirm/{action}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def confirm_control(
        request_id: UUID, action: Literal["start", "save", "live"], data: ControlConfirmation
    ):
        return await operator_chat.confirm_control(str(request_id), action, data)

    @app.post(
        prefix + "/chat/turns/{request_id}/refresh-confirmation",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def refresh_confirmation(request_id: UUID):
        return await operator_chat.refresh_confirmation(str(request_id))

    # --- Private Call Voice, Avatars, Memory Guards, and Work Routing (10) ---
    call_prefix = prefix + "/private-call"

    @app.get(call_prefix + "/avatars", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def private_call_avatars():
        return voice_avatar.avatar_profiles()

    @app.get(call_prefix, dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def call_availability():
        return voice_private_call.call_availability(runtime)

    @app.get(
        call_prefix + "/recordings/{character}/{profile}.mp4",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def call_recording(character: str, profile: str):
        path = voice_private_call.call_recordings(runtime, character).get(profile)
        if path is None:
            raise HTTPException(404, "当前角色没有这段通话实录")
        return FileResponse(path, media_type="video/mp4")

    @app.get(call_prefix + "/reference", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def call_reference():
        _, reference = await voice_private_call.read_call_voice(runtime, runtime.character)
        return Response(reference, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.post(call_prefix + "/ticket", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def create_ticket(request: Request, options: voice_private_call.CallOptions = voice_private_call.CallOptions()):
        request_origin = f"{'wss' if request.url.scheme == 'https' else 'ws'}://{request.url.netloc}"
        root_path = request.scope.get("root_path", "")
        return await voice_private_call.create_call_ticket(request_origin, root_path, runtime, operator_chat, options)

    @app.post(call_prefix + "/memory-validate", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def validate_call_memory(use: CallUse):
        try:
            check_call_use(runtime, use, operator_chat)
        except MemoryChanged as error:
            raise HTTPException(409, str(error)) from error
        return {"valid": True}

    @app.post(call_prefix + "/memory-watch", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def watch_call_memory(use: CallUse):
        if len(guards) >= MAX_ACTIVE_CALL_GUARDS:
            raise HTTPException(429, "活跃的通话记忆订阅过多")
        identity = token_urlsafe(24)
        guard = CallGuard(runtime, use, operator_chat)
        if guard.state() != "valid":
            raise HTTPException(409, "通话记忆已变化")
        guards[identity] = guard

        async def records():
            try:
                while True:
                    guard.wake.clear()
                    state = guard.state()
                    guard.active = state == "valid"
                    yield json.dumps({"state": state, "guard": identity}) + "\n"
                    if not guard.active:
                        break
                    try:
                        await asyncio.wait_for(guard.wake.wait(), 0.75)
                    except TimeoutError:
                        pass
            finally:
                guard.active = False
                guards.pop(identity, None)

        return StreamingResponse(
            records(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post(call_prefix + "/utterances", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def call_utterance(data: voice_work.Utterance):
        return await voice_work.process_call_utterance(runtime, operator_chat, data)

    @app.post(call_prefix + "/work/status", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def call_work_status(call: voice_work.CallOrigin):
        return voice_work.list_call_work_status(runtime, operator_chat, call)

    @app.post(call_prefix + "/work/{task_id}/read", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def call_read_result(task_id: UUID, data: voice_work.Readback):
        path = await voice_work.synthesize_call_readback(runtime, operator_chat, task_id, data)
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # --- Owner Memory (5) ---
    memory_prefix = prefix + "/memory"

    @app.post(memory_prefix + "/search", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def search_owner_memory(data: Search):
        return {"items": [citation(row) for row in private_rows(runtime, data.query, inspection=True)]}

    @app.post(memory_prefix + "/get", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def get_owner_memory(data: SourceLookup):
        memory = runtime.daily_memory
        scope = runtime.memory_organizer.scope()
        rows = memory.organized(scope, item_id=data.id) if memory.preference(scope)["enabled"] else []
        if not rows or rows[0]["status"] != "active" or not (row := memory.owner_projection(rows[0])):
            raise HTTPException(404, "当前没有可引用的这条记忆")
        if data.source_key and row["memory_kind"] == "private" and data.source_key != row["source"]["key"]:
            raise HTTPException(404, "该来源已被更正")
        if data.source_key and row["memory_kind"] != "private":
            hits = memory.lookup_source(scope, "operator", data.source_key)
            exact = next(
                (hit for hit in hits if hit["id"] == data.id and hit["source_key"] == data.source_key), None
            )
            if not exact:
                raise HTTPException(404, "当前没有可核对的这条来源")
            row = row | {"exact_source": exact}
        return voice_origin.attach(runtime.store, citation(row))

    @app.post(memory_prefix + "/validate", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def validate_owner_memory(data: MemoryUse):
        try:
            check_use(runtime, data)
        except MemoryChanged as error:
            raise HTTPException(409, str(error)) from error
        return {"valid": True}

    @app.post(memory_prefix + "/propose", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def propose_owner_memory(data: Proposal):
        _, source, stamp, target = build_memory_proposal(runtime, operator_chat, data)
        return {
            "text": source["text"],
            "stamp": stamp,
            **({"replaces": citation(target)} if target else {}),
            **({"machine_text": data.voice_origin.machine_text} if data.voice_origin else {}),
        }

    @app.post(memory_prefix + "/confirm", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def confirm_owner_memory(data: Confirmation):
        memory = runtime.daily_memory
        scope, source, stamp, target = build_memory_proposal(runtime, operator_chat, data)
        if data.stamp != stamp:
            raise HTTPException(409, "记忆设置已变化，请重新确认")
        if target:
            decision = MemorySuggestion(
                action="update", existing_id=target["id"], headline=data.text[:40], note=data.text, tags=[]
            )
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
                decision = MemorySuggestion(
                    action="add", existing_id=None, headline=data.text[:40], note=data.text, tags=[]
                )
                if not memory.save_organized(scope, source, decision, None):
                    raise HTTPException(409, "该来源已撤回或无法核实")
                existing = memory.lookup_source(scope, "operator", source["key"])
            if not existing or existing[0]["quote"] != data.text:
                raise HTTPException(409, "来源已变化，请重新读取")
            if data.source == "turn":
                runtime.store.db.execute(
                    "UPDATE daily_highlights SET source_evidence=json_set(source_evidence,'$.owner_confirmed',1),"
                    "updated_at=? WHERE id=? AND owner_principal=? AND quote=? AND status='active' "
                    "AND json_extract(source_evidence,'$.memory_source.key')=? "
                    "AND json_extract(source_evidence,'$.owner_confirmed') IS NULL",
                    (time.time(), existing[0]["id"], memory.private_owner, data.text, source["key"]),
                )
            if data.voice_origin:
                voice_origin.save(runtime.store.db, existing[0]["id"], source["key"], data.voice_origin)
            return {"id": existing[0]["id"]}

    # --- Reply Selections (4) ---
    sel_prefix = prefix + "/reply-selections"

    @app.get(sel_prefix, dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def reply_selection_scope():
        account_id, character, version, _, _, epoch = runtime.tasks.scope()
        return {"account": account_id, "character": character, "version": version, "epoch": epoch}

    @app.post(sel_prefix, dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def create_reply_selection(body: ReplySelection):
        try:
            return reply_ops.select(body)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post(sel_prefix + "/{selection_id}/delegate", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def delegate_reply_selection(selection_id: UUID):
        identity = str(selection_id)
        row = runtime.tasks.store.one(
            "SELECT items FROM agent_reply_selections WHERE id=? AND scope=?",
            (identity, dump(runtime.tasks.scope())),
        )
        if not row:
            raise HTTPException(409, "选择已撤回或账号、角色授权已变化")
        count = len(json.loads(row["items"]))
        goal = f"处理我选定的 {count} 条回复（选择编号 {identity}）。只处理所选来源，逐条展示对象和正文，等我批准。"
        try:
            task = runtime.tasks.create(identity, goal, source_key=["reply_selection", identity])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"task_id": task["id"]}

    @app.delete(sel_prefix + "/{selection_id}", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def revoke_reply_selection(selection_id: UUID):
        deleted = runtime.tasks.store.execute(
            "DELETE FROM agent_reply_selections WHERE id=? AND scope=?",
            (str(selection_id), dump(runtime.tasks.scope())),
        )
        if not deleted.rowcount:
            raise HTTPException(404, "找不到这份选择")
        return {"revoked": True}

    return operator_chat
