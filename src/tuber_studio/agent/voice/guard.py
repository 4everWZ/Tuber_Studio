"""Notify an active call when its reusable private memory snapshot changes."""
import asyncio
from uuid import UUID

from ...core.models import StrictModel, content_hash
from ...persona.memory.daily import MemoryChanged
from ..memory_organizer.use import MemoryUse, check_use, read_stamp
from ..tasks.history import history_preference
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


MAX_ACTIVE_CALL_GUARDS = 32
