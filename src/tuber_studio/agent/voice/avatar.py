"""Compatibility discovery for retired private-call video clients."""
from fastapi import Depends

from .guard import register_call_guards

PREFIX = "/api/chat/private-call/avatars"


def register_private_avatar(app, runtime, require_admin, *, operator_chat=None, route_path_fn=None):
    register_call_guards(app, runtime, require_admin, operator_chat=operator_chat, route_path_fn=route_path_fn)

    @app.get(PREFIX, dependencies=[Depends(require_admin)])
    async def choices():
        return {"profiles": []}
