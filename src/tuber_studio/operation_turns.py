"""Arbitrate the single model opportunity shared by chat and operations."""

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

operation_lane = ContextVar("operation_lane", default=None)
operation_ready_at = ContextVar("operation_ready_at", default=None)
operation_ticket = ContextVar("operation_ticket", default=None)
operation_checkpoint = ContextVar("operation_checkpoint", default=None)


@dataclass(eq=False)
class Ticket:
    lane: str
    ready_at: float
    queued_at: float
    calls: int = 0
    owns_lock: bool = True


class OperationTurns:
    def __init__(self, lock, blocked):
        self.lock, self.blocked = lock, blocked
        self.queues = {lane: deque() for lane in ("direct", "priority", "background", "chat")}
        self.last_was_chat = self.last_was_priority = False
        self.direct_streak = 0
        self.changed = asyncio.Event()
        self.stats = {lane: {"calls": 0, "queue_seconds": 0.0, "max_queue_seconds": 0.0}
                      for lane in self.queues}

    def _next(self):
        chat = self.queues["chat"]
        direct = self.queues["direct"]
        priority = self.queues["priority"]
        background = self.queues["background"]
        if chat and not self.last_was_chat:
            return chat[0]
        if direct and (self.direct_streak < 2 or not (priority or background)):
            return min(direct, key=lambda ticket: ticket.ready_at)
        if priority and (not self.last_was_priority or not background):
            return priority[0]
        if background:
            return min(background, key=lambda ticket: ticket.ready_at)
        return chat[0] if chat else None

    async def acquire(self, lane, ready_at=None):
        now = monotonic()
        ticket = Ticket(lane, now if ready_at is None else ready_at, now)
        await self._acquire(ticket)
        if lane == "chat":
            self.last_was_chat = True
        return ticket

    async def _acquire(self, ticket):
        queue = self.queues[ticket.lane]
        queue.append(ticket)
        self.changed.set()
        try:
            while True:
                if checkpoint := operation_checkpoint.get():
                    checkpoint()
                self.changed.clear()
                if self._next() is ticket and not self.blocked():
                    try:
                        await asyncio.wait_for(self.lock.acquire(), timeout=0.2)
                    except TimeoutError:
                        continue
                    if self._next() is ticket and not self.blocked():
                        ticket.owns_lock = True
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

    def release(self, ticket):
        if ticket.owns_lock:
            ticket.owns_lock = False
            self.lock.release()
            self.changed.set()

    async def next_request(self, ticket):
        if ticket.calls and ticket.lane != "chat":
            self.release(ticket)
            ticket.ready_at = ticket.queued_at = monotonic()
            await self._acquire(ticket)

    def started(self, ticket):
        ticket.calls += 1
        sample = self.stats[ticket.lane]
        elapsed = max(0, monotonic() - ticket.queued_at)
        sample["calls"] += 1
        sample["queue_seconds"] += elapsed
        sample["max_queue_seconds"] = max(sample["max_queue_seconds"], elapsed)
        if ticket.lane != "chat":
            self.last_was_chat = False
            if ticket.lane == "direct":
                self.direct_streak = min(self.direct_streak + 1, 2)
            else:
                self.direct_streak = 0
                self.last_was_priority = ticket.lane == "priority"


async def prepare_model_request():
    if context := operation_ticket.get():
        turns, ticket = context
        await turns.next_request(ticket)
    if checkpoint := operation_checkpoint.get():
        checkpoint()


def mark_model_request():
    if context := operation_ticket.get():
        turns, ticket = context
        turns.started(ticket)
