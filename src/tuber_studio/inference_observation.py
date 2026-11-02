"""Single-flight background snapshots; HTTP readers never await disk inventory."""

import asyncio
import time


class BackgroundObservation:
    def __init__(self, loader, *, interval, retry_interval=10, minimum_interval=0):
        self.loader = loader
        self.interval, self.retry_interval = interval, retry_interval
        self.minimum_interval = minimum_interval
        self.clock = time.monotonic
        self.last_started = None
        self.value = None
        self.checked_at = None
        self.error = None
        self.next_refresh = 0.0
        self.task = None
        self.closed = False

    def request(self):
        if self.closed or self.task is not None and not self.task.done():
            return
        earliest = self.last_started + self.minimum_interval if self.last_started is not None else 0
        if self.clock() < max(self.next_refresh, earliest):
            return
        self.last_started = self.clock()
        self.task = asyncio.create_task(self._refresh())

    async def _refresh(self):
        try:
            value = await self.loader()
        except Exception as error:
            self.error = type(error).__name__
            self.next_refresh = self.clock() + self.retry_interval
        else:
            self.value = value
            self.checked_at = time.time()
            self.error = None
            self.next_refresh = self.clock() + self.interval

    def invalidate(self):
        self.next_refresh = 0.0

    def status(self):
        scanning = self.task is not None and not self.task.done()
        earliest = self.last_started + self.minimum_interval if self.last_started is not None else 0
        return {
            "state": "scanning"
            if scanning
            else "error"
            if self.error
            else "cached"
            if self.value is not None
            else "unknown",
            "cached": self.value is not None,
            "checked_at": self.checked_at,
            "refresh_after_seconds": max(0, max(self.next_refresh, earliest) - self.clock()),
            "error": self.error,
        }

    async def close(self):
        self.closed = True
        if self.task is not None:
            # Cancelling a to_thread waiter does not stop its IO. Its owner drains
            # the single job during shutdown rather than starting detached work.
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except asyncio.CancelledError:
                    continue
            self.task.result()
