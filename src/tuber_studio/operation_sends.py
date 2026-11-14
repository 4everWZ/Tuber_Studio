"""Persistent readiness for the existing automatic publication entry points."""

import asyncio
import time
from contextvars import ContextVar

import httpx

from .operations.browsing.quotas import DailyReplyLimitReached
from .providers import public_error
from .publication_errors import SendLimitReached

current_send = ContextVar("current_operation_send", default=None)

class OperationSends:
    def __init__(self, store, control, scope, dispatch):
        self.store, self.control, self.scope, self.dispatch = store, control, scope, dispatch
        self.changed = asyncio.Event()
        self.worker = None
        self.active = None
        self.stats = {"attempts": 0, "wait_seconds": 0.0, "max_wait_seconds": 0.0}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS operation_send_queue(
                channel TEXT NOT NULL,source_id TEXT NOT NULL,account_id TEXT NOT NULL,
                character_id TEXT NOT NULL,character_version INTEGER NOT NULL,epoch INTEGER NOT NULL,
                direct INTEGER NOT NULL,ready_at REAL NOT NULL,wake_at REAL NOT NULL,error TEXT,
                PRIMARY KEY(channel,source_id));
            CREATE INDEX IF NOT EXISTS operation_send_ready
                ON operation_send_queue(account_id,character_id,character_version,direct,ready_at,wake_at);
        """)

    def enqueue(self, channel, identity, *, direct=False):
        policy = self.control.policy()
        if not policy["enabled"] or policy["emergency"]:
            return
        now = time.time()
        self.store.execute(
            "INSERT OR IGNORE INTO operation_send_queue VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (channel, str(identity), policy["account_id"], policy["character_id"], policy["character_version"],
             policy["epoch"], int(direct), now, now),
        )
        self.changed.set()
        self.start()

    def start(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run())

    def attempt_started(self):
        if item := current_send.get():
            wait = max(0, time.time() - item["ready_at"])
            self.stats["attempts"] += 1
            self.stats["wait_seconds"] += wait
            self.stats["max_wait_seconds"] = max(self.stats["max_wait_seconds"], wait)

    def withdraw(self, channel, identity):
        self.store.execute("DELETE FROM operation_send_queue WHERE channel=? AND source_id=?",
                           (channel, str(identity)))
        self.changed.set()

    def assert_ready(self, channel, identity):
        if current_send.get() is not None and not self.store.one(
            "SELECT 1 FROM operation_send_queue WHERE channel=? AND source_id=?",
            (channel, str(identity)),
        ):
            raise ValueError("号主已修改发送候选，保留最新草稿")

    def _remove(self, item):
        self.store.execute("DELETE FROM operation_send_queue WHERE channel=? AND source_id=?",
                           (item["channel"], item["source_id"]))

    async def run(self):
        while True:
            self.changed.clear()
            delay = None
            if self.control.accepting_work:
                scope = self.scope()
                now = time.time()
                item = self.store.one(
                    "SELECT * FROM operation_send_queue WHERE account_id=? AND character_id=? "
                    "AND character_version=? AND wake_at<=? ORDER BY direct DESC,ready_at,source_id LIMIT 1",
                    (*scope, now),
                )
                if item:
                    async with self.control.automatic_work() as admitted:
                        if admitted:
                            self.active = item
                            token = current_send.set(item)
                            try:
                                if await self.dispatch(item):
                                    self._remove(item)
                            except (SendLimitReached, DailyReplyLimitReached, httpx.TransportError, TimeoutError) as error:
                                self.store.execute(
                                    "UPDATE operation_send_queue SET wake_at=?,error=? WHERE channel=? AND source_id=?",
                                    (max(now + 1, getattr(error, "next_at", now + 60)), public_error(error),
                                     item["channel"], item["source_id"]),
                                )
                            except Exception as error:
                                self.control.last_send_error = public_error(error)
                                if not self.control.draining:
                                    self._remove(item)
                            finally:
                                current_send.reset(token)
                                self.active = None
                    if self.control.accepting_work:
                        continue
                next_item = self.store.one(
                    "SELECT MIN(wake_at) AS wake_at FROM operation_send_queue WHERE account_id=? "
                    "AND character_id=? AND character_version=?", scope,
                )
                if next_item["wake_at"] is not None:
                    delay = max(0.05, next_item["wake_at"] - time.time())
            try:
                await asyncio.wait_for(self.changed.wait(), delay)
            except TimeoutError:
                pass

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
