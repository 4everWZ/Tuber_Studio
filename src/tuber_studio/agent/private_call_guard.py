"""Notify an active call when its reusable private memory snapshot changes."""
import asyncio
import json
from secrets import token_urlsafe
from uuid import UUID

from fastapi import Depends, HTTPException
from starlette.responses import StreamingResponse

from ..daily_memory import MemoryChanged
from ..models import StrictModel
from ..operation_continuations import content_hash
from ..web_entry import route_path
from .owner_memory import history_preference
from .owner_memory_use import MemoryUse, check_use, read_stamp
from .private_call import inherited_topic, shared_call_facts


class TopicSource(StrictModel):
    turn_id: UUID
    history_revision: int
    topic_stamp: str | None = None
    shared_stamp: str | None = None


class CallUse(MemoryUse):
    topic_source: TopicSource | None = None


def check_call_use(runtime, use, operator_chat=None):
    if use.ids or not (use.topic_source and use.topic_source.shared_stamp):
        check_use(runtime, use)
    elif read_stamp(runtime, []) != use.stamp:
        raise MemoryChanged("通话授权已变化")
    if source := use.topic_source:
        if history_preference(runtime)["history_revision"] != source.history_revision:
            raise MemoryChanged("私聊话题已失效")
        if source.topic_stamp or not source.shared_stamp:
            topic = inherited_topic(runtime, source.turn_id)
            if not topic or source.topic_stamp and content_hash(topic) != source.topic_stamp:
                raise MemoryChanged("私聊话题已失效")
        if source.shared_stamp and content_hash(shared_call_facts(runtime, operator_chat, source.turn_id)) != source.shared_stamp:
            raise MemoryChanged("通话引用的工作台事实已变化")


class CallGuard:
    def __init__(self, runtime, use, operator_chat=None):
        self.runtime = runtime
        self.use = use
        self.scope = runtime.tasks.scope()
        self.wake = asyncio.Event()
        self.active = True
        self.operator_chat = operator_chat

    def state(self):
        if not self.active or self.runtime.tasks.scope() != self.scope:
            return "closed"
        try:
            check_call_use(self.runtime, self.use, self.operator_chat)
        except MemoryChanged:
            return "changed"
        return "valid"


def register_call_guards(app, runtime, require_admin, *, operator_chat=None):
    guards = {}

    @app.middleware("http")
    async def wake_after_mutation(request, call_next):
        response = await call_next(request)
        if guards and request.method in ("POST", "PUT", "PATCH", "DELETE") and not route_path(request).startswith(
                ("/api/chat/private-call", "/api/agent/memory/validate")):
            for guard in guards.values():
                guard.wake.set()
        return response

    @app.post("/api/chat/private-call/memory-validate", dependencies=[Depends(require_admin)])
    async def validate(use: CallUse):
        try:
            check_call_use(runtime, use, operator_chat)
        except MemoryChanged as error:
            raise HTTPException(409, str(error)) from error
        return {"valid": True}

    @app.post("/api/chat/private-call/memory-watch", dependencies=[Depends(require_admin)])
    async def watch(use: CallUse):
        if len(guards) >= 32:
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
                        await asyncio.wait_for(guard.wake.wait(), .75)
                    except TimeoutError:
                        pass
            finally:
                guard.active = False
                guards.pop(identity, None)

        return StreamingResponse(records(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return guards
