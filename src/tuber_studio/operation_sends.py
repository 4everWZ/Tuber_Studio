"""Persistent readiness for the existing automatic publication entry points."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from .operations.browsing.quotas import DailyReplyLimitReached
from .providers import public_error
from .publication_errors import SendLimitReached

current_send = ContextVar("current_operation_send", default=None)


class OperationSends:
    def __init__(self, store, control, scope, dispatch, *, waiting_for_image):
        self.store, self.control, self.scope, self.dispatch = store, control, scope, dispatch
        self.waiting_for_image = waiting_for_image
        self.changed = asyncio.Event()
        self.worker = None
        self.active = None
        self.preparing = {}
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

    @contextmanager
    def preparation_slot(
        self, account, channel, limits, *, retry_seconds, recipient="", video_reply=None, min_interval=0,
    ):
        blocked = self.preparation_block(
            account, channel, limits, retry_seconds=retry_seconds,
            recipient=recipient, video_reply=video_reply, min_interval=min_interval,
        )
        if blocked:
            yield blocked
            return
        character, bvid, _, _ = video_reply or (None, None, 0, None)
        key = object()
        self.preparing[key] = (account, {
            "channel": channel, "recipient": recipient, "character_id": character, "bvid": bvid,
        })
        try:
            yield None
        finally:
            del self.preparing[key]

    def preparation_block(
        self, account, channel, limits, *, retry_seconds, recipient="", video_reply=None, min_interval=0,
    ):
        policy = self.control.policy()
        now = time.time()
        retry = now + retry_seconds
        active = self.active or {}
        active_key = (active.get("channel"), active.get("source_id"))
        # A sender that has started the platform attempt is represented by the
        # write ledger. Until then, its queue row still holds the draft's place.
        pending = self.store.all(
            """
            SELECT CASE WHEN q.channel='browsing' THEN CASE i.kind
                WHEN 'comment_like' THEN 'comment_likes' WHEN 'video_like' THEN 'video_likes'
                WHEN 'video_fav' THEN 'video_favorites' ELSE 'comments' END ELSE q.channel END AS channel,
                q.character_id, json_extract(i.target,'$.bvid') AS bvid,
                COALESCE(json_extract(i.target,'$.recipient'),d.platform_user_id,'') AS recipient
            FROM operation_send_queue q
            LEFT JOIN browsing_intents i ON q.channel='browsing' AND i.id=q.source_id
            LEFT JOIN dm_drafts d ON q.channel='messages' AND CAST(d.id AS TEXT)=q.source_id
            LEFT JOIN dynamic_posts p ON q.channel='dynamics' AND CAST(p.id AS TEXT)=q.source_id
            WHERE q.account_id=? AND q.epoch=?
              AND NOT (q.channel IS ? AND q.source_id IS ? AND ?) AND (
                (i.status='pending_review' AND i.auto_authorized=1)
                OR d.status='pending_review' OR p.status='pending_review'
                OR (q.channel=? AND q.source_id=? AND (d.status='publishing' OR p.status='publishing')))
            """,
            (account, policy["epoch"], *active_key, bool(active.get("reserved")), *active_key),
        )
        pending.extend(item for actor, item in self.preparing.values() if actor == account)
        attempts = self.store.all(
            "SELECT channel,recipient,started_at,resolved_status FROM operation_write_attempts "
            "WHERE account_id=? AND (started_at>? OR resolved_status='sending') ORDER BY started_at DESC",
            (account, now - max(86400, min_interval)),
        )
        channel_pending = [item for item in pending if item["channel"] == channel]
        channel_attempts = [item for item in attempts if item["channel"] == channel]
        blocks = []
        for detail, quotas, queued, rows in (
            ("账号", policy, pending, attempts),
            ("渠道", limits, channel_pending, channel_attempts),
        ):
            for seconds, field, label in ((3600, "max_per_hour", "每小时"), (86400, "max_per_day", "24 小时")):
                maximum = quotas.get(field, 0)
                if not maximum:
                    continue
                recent = [item for item in rows if item["started_at"] > now - seconds]
                in_flight = sum(
                    item["resolved_status"] == "sending" and item["started_at"] <= now - seconds for item in rows
                )
                if len(recent) >= maximum:
                    due = recent[maximum - 1]["started_at"] + seconds
                elif len(recent) + len(queued) + in_flight >= maximum:
                    due = retry
                else:
                    continue
                blocks.append(SendLimitReached(f"{detail}{label}发送额度已占满，暂不拟稿", due))

        if min_interval:
            if channel_pending:
                blocks.append(SendLimitReached("已有待发动态，暂不拟稿", retry))
            if channel_attempts and (due := channel_attempts[0]["started_at"] + min_interval) > now:
                blocks.append(SendLimitReached("尚未达到动态最小发布间隔", due))
        cooldown = max(policy["recipient_cooldown_seconds"], limits.get("recipient_cooldown_seconds", 0))
        if recipient and cooldown:
            if any(str(item["recipient"]) == recipient for item in pending):
                blocks.append(SendLimitReached("该用户已有待发回应，暂不拟稿", retry))
            if last := next((item for item in attempts if item["recipient"] == recipient), None):
                if (due := last["started_at"] + cooldown) > now:
                    blocks.append(SendLimitReached("该用户回复冷却中，暂不拟稿", due))
        if video_reply and video_reply[2]:
            character, bvid, limit, timezone = video_reply
            status = self.control.browsing_quotas.reply_status(account, character, bvid, limit, timezone, now=now)
            waiting = sum(item["character_id"] == character and item["bvid"] == bvid for item in channel_pending)
            if status["remaining"] == 0:
                blocks.append(DailyReplyLimitReached(status, message="达到该视频今日回复上限，暂不拟稿"))
            elif waiting >= status["remaining"]:
                blocks.append(SendLimitReached("该视频剩余回复额度已预留，暂不拟稿", retry))
        return max(blocks, key=lambda error: error.next_at, default=None)

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
            item["reserved"] = True
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

    def due_background(self, scope, epoch, now):
        return self.store.all(
            "SELECT * FROM operation_send_queue WHERE account_id=? AND character_id=? AND character_version=? "
            "AND epoch=? AND direct=0 AND wake_at<=?", (*scope, epoch, now),
        )

    async def run(self):
        while True:
            self.changed.clear()
            delay = None
            if self.control.accepting_work:
                scope = self.scope()
                now = time.time()
                # Keep background FIFO and preserve order within each direct
                # channel. An image wait leaves the sender free for other heads.
                heads = self.store.all(
                    "SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
                    "(PARTITION BY direct, CASE WHEN direct=1 THEN channel END "
                    "ORDER BY ready_at,source_id) AS position FROM operation_send_queue "
                    "WHERE account_id=? AND character_id=? AND character_version=? AND wake_at<=?) "
                    "WHERE position=1 ORDER BY direct DESC,ready_at,source_id",
                    (*scope, now),
                )
                item = next((head for head in heads if not self.waiting_for_image(head)), None)
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
                    "AND character_id=? AND character_version=? AND wake_at>?", (*scope, now),
                )
                if next_item["wake_at"] is not None:
                    delay = max(0.05, next_item["wake_at"] - time.time())
                if heads:
                    delay = min(0.5, delay) if delay is not None else 0.5
            try:
                await asyncio.wait_for(self.changed.wait(), delay)
            except TimeoutError:
                pass

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
