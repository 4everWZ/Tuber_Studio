"""CapabilityDispatcher entry point coordinating domain dispatch modules."""

from ..context import ChatContext
from ..lookup import workbench_tools
from . import action_handlers, publication_handlers, read_handlers, scope_guard

_READ_KINDS = frozenset({
    "workbench_capabilities",
    "daily_settings",
    "pet_settings",
    "clock",
    "activity",
    "owner_binding",
    "identity_read",
    "workshop_lookup",
    "recall_memory",
    "owner_profile",
    "operating_account",
    "operation_status",
    "web_lookup",
    "public_experiences",
    "recall_experience",
    "video_activity",
    "live_status",
    "live_studio_hud",
    "meme_gallery_status",
})


class CapabilityDispatcher:
    """Executes resolved intents against domain services and builds DeliveryPackets."""

    def __init__(self, context: ChatContext, *, generation_slot, model_caller):
        self.ctx = context
        self._generation_slot = generation_slot
        self._model = model_caller

    def delegation_reads(self):
        return action_handlers.delegation_reads(self)

    def delegation(self, brief, context):
        return action_handlers.delegation(self, brief, context)

    async def delegated_reads(self, brief, request_id, request):
        return await action_handlers.delegated_reads(self, brief, request_id, request)

    async def lookup_workshop(self, query, request_id, request, *, position=None):
        return await read_handlers.lookup_workshop(self, query, request_id, request, position=position)

    async def recall_workshop(self, query, data, request_id, request, persist_turn, call_origin):
        return await read_handlers.recall_workshop(self, query, data, request_id, request, persist_turn, call_origin)

    def read_identities(self, char, requests):
        return read_handlers.read_identities(self, char, requests)

    async def activity(self, request_id, char):
        return await read_handlers.activity(self, request_id, char)

    def event_result(self, kind, event):
        return read_handlers.event_result(self, kind, event)

    async def public_text(self, request, sources, *, purpose="直播"):
        return await action_handlers.public_text(self, request, sources, purpose=purpose)

    async def live_handoff(self, intent, char, request_id, request, tasks):
        return await action_handlers.live_handoff(self, intent, char, request_id, request, tasks)

    async def save_live_preference(self, char, request_id, request):
        return await scope_guard.save_live_preference(self, char, request_id, request)

    def save_event_memory(self, event, request_id):
        return scope_guard.save_event_memory(self, event, request_id)

    def forget_memory(self, topic):
        return scope_guard.forget_memory(self, topic)

    def save_direct_memory(self, intent, request_text):
        return scope_guard.save_direct_memory(self, intent, request_text)

    def update_direct_memory(self, intent, request_text):
        return scope_guard.update_direct_memory(self, intent, request_text)

    def delete_direct_memory(self, intent, request_text):
        return scope_guard.delete_direct_memory(self, intent, request_text)

    def game_action(self, intent, request_id):
        return action_handlers.game_action(self, intent, request_id)

    def live_history(self, char, topic):
        return read_handlers.live_history(self, char, topic)

    async def stop(self, reference, tasks):
        return await action_handlers.stop(self, reference, tasks)

    async def confirm_control(self, request_id, action, confirmation):
        return await action_handlers.confirm_control(self, request_id, action, confirmation)

    async def refresh_confirmation(self, request_id):
        return await action_handlers.refresh_confirmation(self, request_id)

    async def update_video(self, request_id, *, resume):
        return await action_handlers.update_video(self, request_id, resume=resume)

    def inspection_image(self, request_id):
        return self.ctx.inspection_image(request_id)

    async def execute(
        self,
        intent,
        char,
        request_id,
        data,
        events,
        tasks,
        focus,
        task_context,
        *,
        call_origin=None,
        persist_turn=None,
    ):
        kind, topic, reference, selection, request, clarification = scope_guard.prepare_dispatch_inputs(
            self.ctx, intent, char, request_id, data, events, tasks, focus, task_context,
        )
        if kind == "clarify":
            if topic == workbench_tools.OWNER_CONTROL_REPLY:
                return {"reply_text": topic, "card": None}
            return {"reply_text": topic or "你指的是哪一件事？", "card": None, **clarification}
        if kind in {"outgoing_queue", "prepare_publication", "dynamic", "edit_dynamic"}:
            return await publication_handlers.dispatch_publication_action(
                self, intent, char, request_id, data, kind, topic, request, persist_turn=persist_turn,
            )
        if kind in _READ_KINDS:
            return await read_handlers.dispatch_read_action(
                self, intent, char, request_id, data, events, focus, kind, topic, reference, request,
                persist_turn=persist_turn, call_origin=call_origin,
            )
        return await action_handlers.dispatch_action_or_control(
            self, intent, char, request_id, data, events, tasks, focus, task_context,
            kind, topic, reference, selection, request, clarification,
            call_origin=call_origin, persist_turn=persist_turn,
        )
