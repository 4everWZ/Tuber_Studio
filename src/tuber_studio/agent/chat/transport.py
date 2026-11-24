"""HTTP/SSE transport, turn orchestration and push-back delivery (L1)."""

import asyncio
import json
import logging
import time
from uuid import uuid4

import httpx
from anyio import CancelScope
from fastapi import HTTPException

from ...core.identity import operating_account_facts, owner_address_label
from ...core.sse import encode_event
from ...core.store import dump
from ..memory_organizer.owner_memory import private_rows
from ..tasks.delivery import DelegationError
from ..tasks.history import history_preference, reason_reply, select_reason_lines
from . import front_dialogue
from . import router as turn_router
from .context import ChatContext
from .delivery import delivery_turn_id, record_chat_delivery
from .dispatcher import CapabilityDispatcher, build_delivery_packet
from .l2_bridge import build_owner_chat_context
from .lookup.personal_agent import object_scope
from .models import (
    _CURRENT_SCOPE_READS,
    _EVENT_ACTIONS,
    _NONCANCELLABLE_ACTIONS,
    _PRESENTED_READS,
    _SETTING_ACTIONS,
    _TASK_CONTROLS,
    _TASK_REFERENCE_ACTIONS,
    ChatIntent,
    PendingChat,
    chat_model_provider,
    verify_call,
)
from .sse_events import calm_constraint_message, emit_tool_activity

logger = logging.getLogger("uvicorn.error").getChild("chat_transport")


class ChatTransport:
    """Manages turn execution, SSE streaming, cancellation and async push-back."""

    def __init__(self, context: ChatContext, dispatcher: CapabilityDispatcher, *, generation_slot, model_caller):
        self.ctx = context
        self.dispatcher = dispatcher
        self._generation_slot = generation_slot
        self._model = model_caller
        self._subscribers: set[asyncio.Queue] = set()

        def handle_chat_delivery(database, task, status, steps, created):
            record_chat_delivery(database, task, status, steps, created)
            identity, _ = delivery_turn_id(task, status, steps)
            self.notify_delivery(identity, task["id"], status)

        self.ctx.runtime.tasks.record_chat_delivery = handle_chat_delivery

    def subscribe_deliveries(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe_deliveries(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def notify_delivery(self, request_id: str, task_id: str, status: str) -> None:
        event = {"request_id": request_id, "task_id": task_id, "status": status}
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def history(self, limit, before):
        ctx = self.ctx
        char = ctx.runtime.character
        scope = (char["id"], ctx.private_owner, char["version"], ctx.account(), ctx.private_owner)
        boundary, cursor = "", ()
        if before:
            anchor = ctx.current_turn(before, read_only=True)
            boundary = " AND (created_at,request_id)<(?,?)"
            cursor = (anchor["created_at"], anchor["request_id"])
        rows = ctx.store.all(
            "SELECT * FROM operator_chat_turns WHERE character_id=? AND "
            "COALESCE(json_extract(result,'$.owner_source')=?,1) AND "
            "((character_version=? AND COALESCE(json_extract(result,'$.account_id'),'')=?) "
            "OR json_extract(result,'$.private_owner')=?) "
            + boundary + " ORDER BY created_at DESC,request_id DESC LIMIT ?",
            (*scope, *cursor, limit + 1),
        )
        avatar = ctx.runtime.settings.idle_avatar(char)
        return {
            "character": ctx.store.display_name(char["id"]),
            "avatar": avatar.get("url"),
            "avatar_still": avatar.get("still_url"),
            "pet": ctx.runtime.pets.state(char["id"]),
            "character_id": char["id"],
            "character_version": char["version"],
            "account_id": ctx.account(),
            "conversation_id": ctx.conversation(char),
            "turns": [ctx.visible_turn(row) for row in reversed(rows[:limit])],
            "next_cursor": rows[limit - 1]["request_id"] if len(rows) > limit else None,
        }

    async def send(self, data, emit=None, *, call_origin=None, call_task=None, action_runner=None):
        ctx = self.ctx
        request_id = str(data.request_id)
        if data.call_context:
            key = ctx.runtime.settings.provider_key("omni")
            if not key:
                raise HTTPException(409, "通话授权已变化，请重新开始通话")
            verify_call(data.call_context, key, ctx.runtime.tasks.scope())
            call_origin = {"id": data.call_context.ticket_hash, "utterance": request_id}
        if request_id in ctx.pending:
            raise HTTPException(409, "这条消息正在处理，请稍后重试。")
        char = ctx.runtime.character
        pending = ctx.pending[request_id] = PendingChat(
            tuple(ctx.binding(char)),
            asyncio.current_task(),
            data.conversation_id or ctx.conversation(char),
        )
        try:
            return await self._send(data, request_id, pending, emit, call_origin, call_task, action_runner=action_runner)
        except asyncio.CancelledError:
            if not pending.stopped:
                raise
            return {"stopped": True}
        finally:
            del ctx.pending[request_id]

    def stop_generation(self, request_id):
        ctx = self.ctx
        pending = ctx.pending.get(request_id)
        if not pending or pending.scope != tuple(ctx.binding(ctx.runtime.character)):
            return {"stopping": False}
        if pending.stopped:
            return {"stopping": True}
        if not pending.cancellable:
            return {"stopping": False}
        pending.stopped = True
        pending.task.cancel()
        return {"stopping": True}

    def _record_turn(self, database, result, request_id, message, intent, char, display_name, submission, call_origin):
        ctx = self.ctx
        result |= {
            "account_id": ctx.account(),
            "submission": submission,
            "memory_intent": intent.action,
            "character_name": display_name,
            "conversation_id": ctx.conversation(char),
        }
        if call_origin:
            result |= {
                "call_origin": call_origin,
                "owner_source": ctx.private_owner,
                "execution_scope": ctx.runtime.tasks.scope(),
            }
        if intent.action in _SETTING_ACTIONS and (receipt := result.get("tool_receipt")):
            result["direct_context"] = {
                "action": intent.action,
                "group": receipt["group"],
                "fields": list(receipt.get("values", {})),
            }
        if objects := ctx.result_objects(result, intent, char):
            result["conversation_objects"] = [obj.model_dump(mode="json") for obj in objects]
            result["object_scope"] = object_scope(ctx.runtime)
            result["owner_source"] = ctx.private_owner
        if result.get("task_origin") == request_id:
            result["owner_source"] = ctx.private_owner
        if result.get("clarification_request") or (result.get("card") or {}).get("type") in {
            "video_browsing", "live_confirmation", "operation_control",
        }:
            result["execution_scope"] = ctx.runtime.tasks.scope()
        if intent.action == "chat" and result.get("private"):
            result["owner_source"] = ctx.private_owner
            if not result.get("organized_memory"):
                result["private_owner"] = ctx.private_owner
        database.execute(
            "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
            (request_id, char["id"], char["version"], message, dump(result), time.time()),
        )

    async def _handle_task_history_discussion(self, char, account, data, intent, task_context, audit):
        ctx = self.ctx
        cards = list({c["id"]: c for c in [*task_context.get("recent_task_cards", []), *task_context.get("history_task_cards", [])]}.values())
        selected = next((c for c in cards if c["id"] == intent.reference), None)
        if not selected:
            return None, None, audit
        if (ctx.task_history.has_anchor(data.message)
                and not any(c["id"] == intent.reference for c in task_context.get("history_task_cards", []))):
            return {"clarify_reply": "当前资料中找不到对应版本，能再确认一下来源或任务吗？"}, None, audit
        coordinate = selected.get("original_edition") if intent._read_original else selected.get("artifact")
        opened = task_context.get("opened_artifact")
        use_opened = (
            opened and opened["id"] == intent.reference and not intent._read_original
            and not ctx.task_history.has_anchor(data.message)
        )
        if use_opened:
            coordinate = {k: opened[k] for k in ("task_revision", "edition")}
        facts = ctx.task_history.read(intent.reference, coordinate)
        if not facts:
            raise HTTPException(409, "任务权限已变化，请重新查看")
        artifact = facts.get("delivery")
        discussed = {
            "task_id": intent.reference,
            "artifact": {k: artifact[k] for k in ("task_revision", "edition")} if artifact else None,
        }
        updated_audit = (audit or {}) | {"task_history": discussed}
        if intent._recall_reason:
            editions = [artifact] if artifact else []
            if not use_opened and selected.get("original_edition"):
                alternate = selected["artifact"] if intent._read_original else selected["original_edition"]
                other = ctx.task_history.read(intent.reference, alternate)
                if other and other.get("delivery"):
                    editions.append(other["delivery"])
            source, excerpts = await select_reason_lines(chat_model_provider(ctx.runtime), data.message, editions, None)
            if source:
                discussed["artifact"] = {k: source[k] for k in ("task_revision", "edition")}
            discussed["reason_excerpts"] = excerpts
            return {"reason_reply": True}, discussed, updated_audit
        return facts, discussed, updated_audit

    async def _send(self, data, request_id, pending, emit, call_origin, call_task, *, action_runner=None):
        ctx = self.ctx
        runtime = ctx.runtime
        async with ctx.lock:
            char = runtime.character
            display_name = ctx.store.display_name(char["id"])
            account = ctx.account()
            if pending.scope != tuple(ctx.binding(char)):
                raise HTTPException(409, "账号或角色已切换，请重新发送")
            if not data.temporary and (
                pending.conversation != ctx.conversation(char)
                or ctx.store.one("SELECT 1 FROM operator_chat_cleared_requests WHERE request_id=?", (request_id,))
            ):
                raise HTTPException(409, "这段对话已清空，请重新发送消息。")
            if data.call_scope and data.call_scope != object_scope(runtime):
                raise HTTPException(409, "通话授权已变化，请重新开始通话")

            def report(value):
                if emit:
                    emit("stage", value)

            has_assistant_model = any(
                callable(getattr(chat_model_provider(runtime), attr, None))
                for attr in ("generate_assistant_json", "generate_text_stream", "generate_text")
            )

            if data.temporary:
                reply_format = "markdown"
                if has_assistant_model:
                    intent = await turn_router.route_chat_turn(
                        ctx, char, account, request_id, data,
                        {}, history=[], report=report, generation_slot=self._generation_slot, model_caller=self._model,
                    )
                    if intent.action != "chat":
                        raise HTTPException(503, "临时对话未生成回复")
                    if intent._identity_reply:
                        reply, reply_format = intent._reply_markdown, "plain"
                    else:
                        history = [item.model_dump() for item in data.window]
                        decision, _ = await front_dialogue.generate_chat_reply(
                            ctx, char, account, request_id, data,
                            notes=(), audit=None, history=history, emit=emit,
                            clock_fields=intent._clock_fields, report=report,
                            generation_slot=self._generation_slot,
                        )
                        reply = decision._reply_markdown.strip()
                else:
                    event = {"event_id": request_id, "text": data.message, "is_operator": True, "sender_name": owner_address_label(char)}
                    history = [item.model_dump() for item in data.window]
                    messages = build_owner_chat_context(
                        char,
                        event,
                        account_facts=operating_account_facts(runtime.account.status()),
                        history=history,
                    )
                    async with self._generation_slot():
                        generated = await chat_model_provider(runtime).generate(messages, char, request_id, source_text=data.message)
                    reply = generated.spoken_text
                ctx.check_scope(char, account)
                return {"temporary": True, "reply_text": reply, "reply_format": reply_format, "card": None}

            submission = data.model_dump(mode="json", exclude={"request_id", "message", "temporary", "window"})
            if row := ctx.turn(request_id):
                result = json.loads(row["result"])
                if (
                    (row["character_id"], row["character_version"]) != (char["id"], char["version"])
                    or row["message"] != data.message
                    or result.get("owner_source", ctx.private_owner) != ctx.private_owner
                    or result.get("account_id", account) != account
                    or result.get("call_origin") != call_origin
                    or result.get(
                        "submission",
                        {"intent": None, "video_selection": ((result.get("card") or {}).get("data") or {}).get("selection")},
                    ) != submission
                ):
                    raise HTTPException(409, "该请求已绑定其他对话")
                return ctx.visible(result)

            uses_model_card = getattr(chat_model_provider(runtime), "uses_model_card", False)
            routing_active = turn_router.routing_client(runtime) is not None or has_assistant_model
            task_context = {}
            if call_task:
                task_context["recent_task_cards"] = [call_task]
            elif (data.intent is None or data.intent.action in {"steer_task", "cancel_task", "task_status", *_TASK_CONTROLS}
                  or (data.intent.action == "chat" and routing_active)):
                task_context = ctx.task_context(char, account, data)
            if data.video_selection and data.intent is None:
                task_context["video_selection"] = data.video_selection.model_dump()

            notes, audit, recalled_sources, history = [], None, set(), []
            history_use, object_use, card_policy = None, None, None
            if data.intent is None or data.intent.action == "chat":
                memory = runtime.daily_memory
                scope = runtime.memory_organizer.scope()
                preference = memory.preference(scope)
                query = data.message
                from .models import memory_query
                cleaned_q = memory_query(query, char)
                notes = private_rows(runtime, cleaned_q) if preference["enabled"] and cleaned_q else []
                audit = {
                    "memory_scope": scope,
                    "memory_channel": "operator",
                    "memory_preference_revision": preference["revision"],
                    "organized_memory": [
                        memory.organized_reference(note)
                        | ({"source_key": note["exact_source"]["source_key"]} if note.get("exact_source") else {})
                        for note in notes
                    ],
                } if notes else None
                recalled_sources = {
                    ref["source_key"]
                    for ref in (audit or {}).get("organized_memory", [])
                    if "source_key" in ref
                }
                if routing_active:
                    history = ctx.assistant_history(char, account, full_turns=uses_model_card, recalled_sources=recalled_sources)
                    history_use = history_preference(runtime) if history else None
                    object_use = (object_scope(runtime), history_preference(runtime)) if task_context.get("objects") else None
                    card_policy = history_preference(runtime) if (task_context.get("recent_task_cards") or task_context.get("history_task_cards")) else None

            def validate_history():
                if object_use and object_use != (object_scope(runtime), history_preference(runtime)):
                    raise HTTPException(409, "已引用的对象权限变化，请重新查询")
                if card_policy and card_policy != history_preference(runtime):
                    raise HTTPException(409, "历史引用设置已变化，请重新发送")
                if history_use and (
                    history_use != history_preference(runtime)
                    or history != ctx.assistant_history(char, account, full_turns=uses_model_card, recalled_sources=recalled_sources)
                ):
                    raise HTTPException(409, "引用的私聊历史已变化，请重新生成")

            # Phase 1: Route via background llm-default (releases chat_routing_generation before front_dialogue!)
            intent = await turn_router.route_chat_turn(
                ctx,
                char,
                account,
                request_id,
                data,
                task_context,
                history=history,
                report=report,
                generation_slot=self._generation_slot,
                model_caller=self._model,
            )
            if data.intent is not None and data.intent.action == "chat" and intent.action != "chat":
                raise HTTPException(503, "这轮未生成可用的对话回复，可以重试。")

            ctx.check_scope(char, account)
            if data.call_scope and data.call_scope != object_scope(runtime):
                raise HTTPException(409, "通话授权已变化，请重新开始通话")

            recorded = False

            def persist_turn(database, result):
                nonlocal recorded
                ctx.check_scope(char, account, account_may_resolve=True)
                self._record_turn(database, result, request_id, data.message, intent, char, display_name, submission, call_origin)
                recorded = True

            def persist_task_origin(database, result):
                result["task_origin"] = request_id
                persist_turn(database, result)

            used_front_dialogue = False
            if intent.action == "chat":
                if intent._identity_reply:
                    validate_history()
                    result = {
                        "reply_text": intent._reply_markdown,
                        "reply_format": "plain",
                        "card": None,
                        "private": True,
                        "assistant_context": False,
                    }
                elif not routing_active:
                    scope = ctx.scope(char)
                    event = {"event_id": "operator:" + request_id, "text": data.message, "sender_name": owner_address_label(char), "is_operator": True}
                    async with self._generation_slot():
                        try:
                            history_policy = history_preference(runtime)
                            if history_policy["history_enabled"]:
                                reply_obj, offline_audit = await runtime.generate_reply(scope, char, event, mode="operator_chat")
                            else:
                                offline_notes = private_rows(runtime, data.message)
                                offline_audit = {}
                                memory_notes = ()
                                if offline_notes:
                                    mem = runtime.daily_memory
                                    binding = runtime.memory_organizer.scope()
                                    offline_audit = {
                                        "memory_scope": binding,
                                        "memory_channel": "operator",
                                        "memory_preference_revision": mem.preference(binding)["revision"],
                                        "organized_memory": [mem.organized_reference(note) for note in offline_notes],
                                    }
                                    memory_notes = (dump([note["anecdote"] for note in offline_notes]),)
                                unified = runtime.unified_memory.for_prompt("owner", character_id=char["id"], limit=15)
                                messages = build_owner_chat_context(
                                    char,
                                    event,
                                    account_facts=operating_account_facts(runtime.account.status()),
                                    memories=unified,
                                    memory_notes=memory_notes,
                                )
                                reply_obj = await chat_model_provider(runtime).generate(messages, char, event["event_id"], source_text=data.message)
                            if history_policy != history_preference(runtime):
                                raise HTTPException(409, "私聊历史设置已变化，请重新生成")
                            runtime.validate_memory_delivery(offline_audit)
                        except (ValueError, TimeoutError, httpx.HTTPError) as error:
                            raise HTTPException(503, "暂时无法生成回复，请重试。") from error
                        ctx.check_scope(char, account)
                        runtime.context.record(
                            scope, char, event,
                            None if "memory_preference_revision" in offline_audit else reply_obj.spoken_text,
                            "operator_chat", memory_kind="operator_private",
                        )
                    result = {"reply_text": reply_obj.spoken_text, "emotion": reply_obj.emotion, "card": None, "private": True}
                    if "memory_preference_revision" in offline_audit:
                        result["organized_memory"] = {
                            key: offline_audit[key]
                            for key in ("organized_memory", "memory_scope", "memory_channel", "memory_preference_revision")
                        }
                else:
                    # Handle task_history discussion / recall_reason if a task card was referenced
                    discussed = None
                    discussed_facts = None
                    chat_audit = audit
                    if intent.reference:
                        discussed_facts, discussed, chat_audit = await self._handle_task_history_discussion(
                            char, account, data, intent, task_context, audit
                        )
                        if isinstance(discussed_facts, dict) and "clarify_reply" in discussed_facts:
                            validate_history()
                            result = {"reply_text": discussed_facts["clarify_reply"], "card": None}
                            ctx.check_scope(char, account, account_may_resolve=True)
                            with ctx.store.db as database:
                                persist_turn(database, result)
                            return ctx.visible(result)
                    ref_history = (chat_audit or {}).get("task_history") or {}
                    if isinstance(discussed_facts, dict) and discussed_facts.get("reason_reply"):
                        validate_history()
                        facts = ctx.task_history.read(ref_history["task_id"], ref_history["artifact"])
                        if not facts:
                            raise HTTPException(409, "引用的成果已变化，请重新查看")
                        reply_text = reason_reply(facts.get("delivery"), ref_history["reason_excerpts"])
                        decision = ChatIntent(action="chat", reference=intent.reference)
                        decision._reply_markdown = reply_text
                        formatting = "plain"
                        used_front_dialogue = True
                    else:
                        decision, chat_audit = await front_dialogue.generate_chat_reply(
                            ctx,
                            char,
                            account,
                            request_id,
                            data,
                            notes=notes,
                            audit=chat_audit,
                            history=history,
                            emit=emit,
                            clock_fields=intent._clock_fields,
                            discussed=discussed,
                            discussed_facts=discussed_facts,
                            validate_history=validate_history,
                            report=report,
                            generation_slot=self._generation_slot,
                        )
                        formatting = "markdown"
                        used_front_dialogue = True
                    result = {
                        "reply_text": decision._reply_markdown.strip(),
                        "reply_format": formatting,
                        "card": None,
                        "private": True,
                        "assistant_context": True,
                    }
                    if fields := getattr(decision, "_clock_fields", ()):
                        result["clock_fields"] = list(fields)
                    if decision.reference:
                        result["task_reference"] = decision.reference
                        if ref_history:
                            result["task_artifact"] = ref_history["artifact"]
                    if chat_audit:
                        if memory_audit := {k: v for k, v in chat_audit.items() if k != "task_history"}:
                            result["organized_memory"] = memory_audit
                    audit = chat_audit
            else:
                if emit:
                    emit("stage", "working")
                if intent.action in _NONCANCELLABLE_ACTIONS:
                    pending.cancellable = False
                if intent.action in _EVENT_ACTIONS | _TASK_REFERENCE_ACTIONS:
                    events, tasks, focus = ctx.references(char)
                else:
                    events, tasks, focus = {}, {}, None
                source = {"call_origin": call_origin} if call_origin else {}
                present = (
                    routing_active
                    and intent.action in _PRESENTED_READS
                    and (intent.action not in _SETTING_ACTIONS or intent._settings_read_only)
                )
                permission = (object_scope(runtime), history_preference(runtime)) if present else None
                runner = action_runner or self.dispatcher.execute
                activity_id = f"act:{request_id}:{intent.action}"
                track_tool = intent.action != "clarify"
                if track_tool:
                    emit_tool_activity(emit, activity_id, intent.action, status="running")
                try:
                    result = await runner(
                        intent, char, request_id, data, events, tasks, focus, task_context,
                        persist_turn=persist_task_origin, **source,
                    )
                except PermissionError:
                    if track_tool:
                        emit_tool_activity(emit, activity_id, intent.action, status="constrained",
                                           summary="读取权限已变化，请重新查询。")
                    raise HTTPException(409, "读取权限已变化，请重新查询") from None
                except DelegationError as error:
                    calm = calm_constraint_message(error)
                    if track_tool:
                        emit_tool_activity(emit, activity_id, intent.action, status="constrained", summary=calm)
                    if emit:
                        emit("stage", "constrained")
                    result = {"reply_text": calm, "card": None}
                else:
                    for mem_evt in result.pop("memory_events", ()) or ():
                        if callable(emit):
                            emit("memory_updated", mem_evt)
                    if track_tool:
                        card_data = (result.get("card") or {}).get("data") or {}
                        tool_data = {"automation_state": card_data["state"]} if "state" in card_data else None
                        emit_tool_activity(
                            emit,
                            activity_id,
                            intent.action,
                            status="completed",
                            summary=str(result.get("reply_text") or "")[:120],
                            data=tool_data,
                        )

                packet = build_delivery_packet(intent, result)
                if routing_active and packet.kind == "task_accepted" and not call_origin:
                    task_id = ((result.get("card") or {}).get("data") or {}).get("id")
                    if not task_id or ctx.runtime.tasks.current(task_id) is None:
                        raise RuntimeError("Task must be persisted in runtime.tasks before foreground ack")
                    voiced_ack = await front_dialogue.acknowledge_delegation(
                        ctx, char, request_id, data.message, packet, generation_slot=self._generation_slot,
                    )
                    if voiced_ack and voiced_ack != result.get("reply_text"):
                        result["reply_text"] = voiced_ack
                        if recorded:
                            with ctx.store.db as database:
                                saved_row = ctx.turn(request_id)
                                if saved_row:
                                    saved_payload = json.loads(saved_row["result"])
                                    saved_payload["reply_text"] = voiced_ack
                                    database.execute(
                                        "UPDATE operator_chat_turns SET result=? WHERE request_id=?",
                                        (dump(saved_payload), request_id),
                                    )
                elif present and (result.get("card") or {}).get("type") != "agent_task":
                    if permission != (object_scope(runtime), history_preference(runtime)):
                        raise HTTPException(409, "读取权限已变化，请重新查询")
                    if intent.action not in (
                        _CURRENT_SCOPE_READS | _SETTING_ACTIONS | {"web_lookup", "owner_binding", "owner_profile"}
                    ):
                        result = await front_dialogue.paraphrase_delivery(
                            ctx, char, account, request_id, data, result, packet, audit,
                            generation_slot=self._generation_slot,
                        )
                    if permission != (object_scope(runtime), history_preference(runtime)):
                        raise HTTPException(409, "读取权限已变化，请重新查询")
                    if (result.get("tool_receipt") and (result.get("card") or {}).get("type") != "memory_summary"
                            and intent.action not in {"web_lookup", "memory_search"}):
                        result["assistant_read_scope"] = permission[0]

            ctx.check_scope(char, account, account_may_resolve=True)
            if used_front_dialogue:
                event = {"event_id": "operator:" + request_id, "text": data.message, "sender_name": owner_address_label(char), "is_operator": True}
                runtime.context.record(
                    ctx.scope(char), char, event, None if audit else result["reply_text"],
                    "operator_chat", memory_kind="operator_private",
                )
            if not recorded:
                with ctx.store.db as database:
                    persist_turn(database, result)
            return ctx.visible(result)

    async def clear(self):
        ctx = self.ctx
        async with ctx.lock, ctx.runtime.generation_lock:
            char = ctx.runtime.character
            conversation = str(uuid4())
            owner_turns = "character_id=? AND COALESCE(json_extract(result,'$.owner_source')=?,1)"
            scope = (char["id"], ctx.private_owner)
            with ctx.store.db:
                db = ctx.store.db
                db.execute(
                    "DELETE FROM context_history_audit WHERE window_key IN "
                    "(SELECT key FROM context_windows WHERE mode GLOB 'operator:*' AND character_id=?)",
                    (char["id"],),
                )
                db.execute("DELETE FROM context_windows WHERE mode GLOB 'operator:*' AND character_id=?", (char["id"],))
                db.execute("DELETE FROM grounded_memory WHERE scope GLOB 'operator:*' AND character_id=?", (char["id"],))
                db.execute(
                    "INSERT OR IGNORE INTO operator_chat_cleared_requests SELECT request_id FROM operator_chat_turns WHERE " + owner_turns,
                    scope,
                )
                db.execute("DELETE FROM operator_chat_turns WHERE " + owner_turns, scope)
                db.execute(
                    "INSERT OR REPLACE INTO kv VALUES (?,?)",
                    (f"operator_conversation:{ctx.private_owner}:{char['id']}", dump(conversation)),
                )
        return {"ok": True, "conversation_id": conversation}

    async def stream(self, data):
        queue = asyncio.Queue()
        request_id = str(data.request_id)

        async def produce():
            try:
                result = await self.send(data, lambda name, value: queue.put_nowait((name, value)))
                queue.put_nowait(("done", result))
            except HTTPException as error:
                queue.put_nowait(("error", {"status": error.status_code, "message": error.detail}))
            except Exception as error:
                logger.warning("operator assistant failure stage=delivery category=%s", type(error).__name__)
                queue.put_nowait(("error", {"status": 503, "message": "这轮回复未完成，可以重试。"}))

        task = asyncio.create_task(produce())
        try:
            yield encode_event("stage", "queued")
            while True:
                try:
                    name, value = await asyncio.wait_for(queue.get(), 12)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if name == "done" and value.get("memory_dependent") and not data.temporary:
                    try:
                        value = self.ctx.visible_turn(self.ctx.current_turn(request_id, read_only=True))
                    except HTTPException:
                        name, value = "error", {"status": 409, "message": "当前对话或引用已变化。"}
                yield encode_event(name, value)
                if name in {"done", "error"}:
                    break
        finally:
            pending = self.ctx.pending.get(request_id)
            if not task.done() and (not pending or pending.task is not task or pending.cancellable):
                task.cancel()
                with CancelScope(shield=True):
                    await asyncio.gather(task, return_exceptions=True)

    async def delivery_events(self):
        """Stream push-back background task completions and approval requests."""
        queue = self.subscribe_deliveries()
        try:
            yield encode_event("ready", {"ok": True})
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), 12)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                row = self.ctx.turn(item["request_id"])
                if not row:
                    continue
                visible = self.ctx.visible_turn(row)
                if visible.get("hidden"):
                    continue
                status = (visible.get("card") or {}).get("data", {}).get("status")
                if status in {"completed", "awaiting_approval", "failed"} and visible.get("reply_text"):
                    packet = build_delivery_packet(None, visible)
                    packet = packet.model_copy(update={
                        "kind": {
                            "completed": "task_completed",
                            "awaiting_approval": "task_waiting_approval",
                            "failed": "task_failed",
                        }[status],
                        "summary": visible["reply_text"],
                    })
                    voiced = await front_dialogue.paraphrase_task_delivery(
                        self.ctx.runtime, self.ctx.runtime.character, packet,
                    )
                    visible = visible | {"reply_text": voiced}
                yield encode_event("delivery", visible)
        finally:
            self.unsubscribe_deliveries(queue)
