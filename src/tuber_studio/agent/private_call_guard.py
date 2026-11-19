"""Notify an active call when its reusable private memory snapshot changes."""
import asyncio
import json
from secrets import token_urlsafe

from fastapi import Depends, HTTPException
from starlette.responses import StreamingResponse

from ..daily_memory import MemoryChanged
from .owner_memory_use import MemoryUse, check_use


class CallGuard:
    def __init__(self, runtime, use):
        self.runtime = runtime
        self.use = use
        self.scope = runtime.tasks.scope()
        self.wake = asyncio.Event()
        self.active = True

    def state(self):
        if not self.active or self.runtime.tasks.scope() != self.scope:
            return "closed"
        try:
            check_use(self.runtime, self.use)
        except MemoryChanged:
            return "changed"
        return "valid"


def register_call_guards(app, runtime, require_admin):
    guards = {}

    @app.middleware("http")
    async def wake_after_mutation(request, call_next):
        response = await call_next(request)
        if guards and request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.url.path.startswith(
                ("/api/chat/private-call", "/api/agent/memory/validate")):
            for guard in guards.values():
                guard.wake.set()
        return response

    @app.post("/api/chat/private-call/memory-watch", dependencies=[Depends(require_admin)])
    async def watch(use: MemoryUse):
        if len(guards) >= 32:
            raise HTTPException(429, "活跃的通话记忆订阅过多")
        identity = token_urlsafe(24)
        guard = CallGuard(runtime, use)
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
