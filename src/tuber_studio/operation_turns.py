"""Share one model among active operations without starving video discovery."""

import asyncio
from collections import deque
from contextvars import ContextVar

operation_lane = ContextVar("operation_lane", default=None)


class OperationTurns:
    rotation = ("reply", "dm", "video", "reply", "dm")

    def __init__(self, lock, blocked):
        self.lock, self.blocked = lock, blocked
        self.queues = {lane: deque() for lane in self.rotation}
        self.queues["chat"] = deque()
        self.last_was_chat = False
        self.cursor = 0
        self.changed = asyncio.Event()

    def _next(self):
        chat = self.queues["chat"]
        if chat and not self.last_was_chat:
            return None, chat[0]
        for step in range(len(self.rotation)):
            position = (self.cursor + step) % len(self.rotation)
            lane = self.rotation[position]
            if self.queues[lane]:
                return position, self.queues[lane][0]
        return (None, chat[0]) if chat else (None, None)

    async def acquire(self, lane):
        ticket = object()
        queue = self.queues[lane]
        queue.append(ticket)
        self.changed.set()
        try:
            while True:
                self.changed.clear()
                if self._next()[1] is ticket and not self.blocked():
                    await self.lock.acquire()
                    position, first = self._next()
                    if first is ticket and not self.blocked():
                        self.last_was_chat = position is None
                        if position is not None:
                            self.cursor = (position + 1) % len(self.rotation)
                        return
                    self.lock.release()
                    self.changed.set()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=0.2)
                except TimeoutError:
                    pass
        finally:
            queue.remove(ticket)
            self.changed.set()
