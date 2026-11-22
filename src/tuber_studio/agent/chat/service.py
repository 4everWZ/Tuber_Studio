"""Private operator conversation facade assembling L0-L3 chat modules."""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException

from ...core.identity import without_model_identity
from ...core.meter import budgeted_call
from ...core.store import dump
from . import dispatcher as capability_dispatcher
from . import router as turn_router
from .context import ChatContext
from .models import (
    ChatIntent,
    ChatTurn,
    ControlConfirmation,
    DelegationBrief,
    DeliveryPacket,
    EventSelection,
    IdentityRead,
    LivePreferenceSelection,
    OpenedArtifact,
    PendingChat,
    TemporaryMessage,
    TurnRoute,
    VideoRange,
    VideoSelection,
    VoiceRoute,
    chat_model_provider,
    text_generation_slot,
)
from .transport import ChatTransport, register_chat_routes

__all__ = [
    "ChatContext",
    "ChatIntent",
    "ChatTransport",
    "ChatTurn",
    "ControlConfirmation",
    "DelegationBrief",
    "DeliveryPacket",
    "EventSelection",
    "IdentityRead",
    "LivePreferenceSelection",
    "OpenedArtifact",
    "OperatorChat",
    "PendingChat",
    "TemporaryMessage",
    "TurnRoute",
    "VideoRange",
    "VideoSelection",
    "VoiceRoute",
    "register_operator_chat",
]


class OperatorChat:
    """Thin facade composing chat context (L2), capability dispatcher (L3) and transport (L1)."""

    def __init__(self, runtime, store, browsing, dynamics):
        self.context = ChatContext(runtime, store, browsing, dynamics)
        self.dispatcher = capability_dispatcher.CapabilityDispatcher(
            self.context,
            generation_slot=self._generation_slot,
            model_caller=self._model,
        )
        self.transport = ChatTransport(
            self.context,
            self.dispatcher,
            generation_slot=self._generation_slot,
            model_caller=self._model,
        )

    @property
    def runtime(self):
        return self.context.runtime

    @property
    def store(self):
        return self.context.store

    @property
    def browsing(self):
        return self.context.browsing

    @property
    def dynamics(self):
        return self.context.dynamics

    @property
    def private_owner(self):
        return self.context.private_owner

    @property
    def task_history(self):
        return self.context.task_history

    @property
    def lock(self):
        return self.context.lock

    @property
    def pending(self):
        return self.context.pending

    async def _model(self, instruction, content, model, *, max_tokens=512, schema=None, background=False):
        messages = [{"role": "system", "content": instruction}, {"role": "user", "content": dump(content)}]
        try:
            slot = self.runtime.offline_generation("background") if background else text_generation_slot(self.runtime)
            async with slot, asyncio.timeout(90):
                with without_model_identity():
                    provider = self.runtime.operation_llm if background else chat_model_provider(self.runtime)
                    generate = getattr(provider, "generate_operator_json", provider.generate_json)
                    value = await budgeted_call(
                        provider,
                        lambda: generate(messages, schema or model.model_json_schema(), max_tokens=max_tokens),
                        messages,
                        max_tokens=max_tokens,
                    )
            return model.model_validate(value)
        except (ValueError, TimeoutError, httpx.HTTPError) as error:
            raise HTTPException(503, "模型暂时无法理解这条消息，可重试或使用快捷操作。") from error

    @asynccontextmanager
    async def _generation_slot(self):
        async with text_generation_slot(self.runtime):
            # The call already has the private participants or a scoped tool packet.
            with without_model_identity():
                yield

    def _planning_client(self, provider):
        del provider
        return turn_router.routing_client(self.runtime)

    def _scope(self, character):
        return self.context.scope(character)

    def _account(self):
        return self.context.account()

    def _binding(self, char):
        return self.context.binding(char)

    def _check_scope(self, char, account, *, account_may_resolve=False):
        return self.context.check_scope(char, account, account_may_resolve=account_may_resolve)

    def _turn(self, request_id):
        return self.context.turn(request_id)

    def _conversation(self, char):
        return self.context.conversation(char)

    def _references(self, char):
        return self.context.references(char)

    def conversation_objects(self, char, account, turn_id=None):
        return self.context.conversation_objects(char, account, turn_id)

    @staticmethod
    async def _fit_assistant_history(provider, prefix, history, current, schema, output):
        return await ChatContext.fit_assistant_history(provider, prefix, history, current, schema, output)

    def current_turn(self, request_id, *, read_only=False):
        return self.context.current_turn(request_id, read_only=read_only)

    def visible_turn(self, row):
        return self.context.visible_turn(row)

    def history(self, limit, before):
        return self.transport.history(limit, before)

    async def send(self, data, emit=None, *, call_origin=None, call_task=None):
        return await self.transport.send(
            data,
            emit,
            call_origin=call_origin,
            call_task=call_task,
            action_runner=self._action,
        )

    def stop_generation(self, request_id):
        return self.transport.stop_generation(request_id)

    async def stream(self, data):
        async for chunk in self.transport.stream(data):
            yield chunk

    async def clear(self):
        return await self.transport.clear()

    async def _action(self, intent, char, request_id, data, events, tasks, focus, task_context, *, call_origin=None,
                      persist_turn=None):
        return await self.dispatcher.execute(
            intent,
            char,
            request_id,
            data,
            events,
            tasks,
            focus,
            task_context,
            call_origin=call_origin,
            persist_turn=persist_turn,
        )

    async def confirm_control(self, request_id, action, confirmation):
        return await self.dispatcher.confirm_control(request_id, action, confirmation)

    async def refresh_confirmation(self, request_id):
        return await self.dispatcher.refresh_confirmation(request_id)

    async def update_video(self, request_id, *, resume):
        return await self.dispatcher.update_video(request_id, resume=resume)

    def inspection_image(self, request_id):
        return self.dispatcher.inspection_image(request_id)

    def video_activity(self, period):
        return self.context.video_activity(period)


def register_operator_chat(app, runtime, store, browsing, dynamics, require_admin):
    service = OperatorChat(runtime, store, browsing, dynamics)
    return register_chat_routes(app, service, require_admin)
