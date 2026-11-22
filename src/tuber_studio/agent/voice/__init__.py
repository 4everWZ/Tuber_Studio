"""Private realtime Omni voice calls, call guards, avatar choices and voice utterance delegation."""

from .avatar import register_private_avatar
from .guard import register_call_guards
from .private_call import register_private_call
from .work import register_call_work

__all__ = [
    "register_call_guards",
    "register_call_work",
    "register_private_avatar",
    "register_private_call",
]
