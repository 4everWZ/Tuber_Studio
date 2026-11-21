"""Persistent readiness for the existing automatic publication entry points."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from .operations.browsing.quotas import DailyReplyLimitReached
from .operation_continuations import content_hash
from .operations_control import COMMENT_WRITE_FILTER, REACTION_CHANNELS
from .providers import public_error
from .platforms.bilibili import BilibiliAccountExpired
from .publication_errors import PlatformRejected, SendLimitReached, UnknownPublication

current_send = ContextVar("current_operation_send", default=None)


class ReconciliationPending(SendLimitReached):
    def __init__(self):
        super().__init__("平台提交结果待核对", time.time() + 60)


def approved_version(row, payload):
    binding = ("account_id", "character_id", "character_version", "source_key", "target", "input_version",
               "platform", "video_id", "thread_id", "platform_comment_id", "platform_user_id", "content")
    return content_hash([[row.get(key) for key in binding], payload])


class OperationSends:
    def __init__(self, store, control, scope, dispatch, *, waiting_for_image, settled=None):
        self.store, self.control, self.scope, self.dispatch = store, control, scope, dispatch
        self.waiting_for_image = waiting_for_image
        self.settled = settled
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
            CREATE TABLE IF NOT EXISTS operation_send_details(
                channel TEXT NOT NULL,source_id TEXT NOT NULL,approval TEXT,blocked TEXT,
                PRIMARY KEY(channel,source_id));
            CREATE TRIGGER IF NOT EXISTS operation_send_remove_details AFTER DELETE ON operation_send_queue
            BEGIN
                DELETE FROM operation_send_details WHERE channel=OLD.channel AND source_id=OLD.source_id;
            END;
            CREATE VIEW IF NOT EXISTS operation_send_items AS
                SELECT q.*,q.rowid AS queue_order,d.approval,d.blocked,
                    CASE WHEN q.channel='browsing' AND i.kind IN
                         ('comment_like','video_like','video_fav','dynamic_like','comment_dislike','video_dislike')
                         THEN 1 ELSE 0 END AS reaction
                FROM operation_send_queue q LEFT JOIN operation_send_details d USING(channel,source_id)
                LEFT JOIN browsing_intents i ON q.channel='browsing' AND i.id=q.source_id;
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
        if channel in REACTION_CHANNELS:
            return None
        active = self.active or {}
        active_key = (active.get("channel"), active.get("source_id"))
        # A sender that has started the platform attempt is represented by the
        # write ledger. Until then, its queue row still holds the draft's place.
        pending = self.store.all(
            """
            SELECT CASE WHEN q.channel='browsing' THEN CASE i.kind
                WHEN 'comment_like' THEN 'comment_likes' WHEN 'video_like' THEN 'video_likes'
                WHEN 'video_fav' THEN 'video_favorites' ELSE 'comments' END ELSE q.channel END AS channel,
                q.character_id, COALESCE(json_extract(i.target,'$.bvid'),c.video_id) AS bvid,
                COALESCE(json_extract(i.target,'$.recipient'),d.platform_user_id,c.platform_user_id,'') AS recipient
            FROM operation_send_items q
            LEFT JOIN browsing_intents i ON q.channel='browsing' AND i.id=q.source_id
            LEFT JOIN dm_drafts d ON q.channel='messages' AND CAST(d.id AS TEXT)=q.source_id
            LEFT JOIN dynamic_posts p ON q.channel='dynamics' AND CAST(p.id AS TEXT)=q.source_id
            LEFT JOIN drafts m ON q.channel='comments' AND CAST(m.id AS TEXT)=q.source_id
            LEFT JOIN comments c ON c.id=m.comment_id
            WHERE q.account_id=? AND (q.epoch=? OR q.approval IS NOT NULL) AND q.reaction=0 AND q.blocked IS NOT 'review'
              AND NOT (q.channel IS ? AND q.source_id IS ? AND ?) AND (
                (i.status='pending_review' AND i.auto_authorized=1)
                OR (q.approval IS NOT NULL AND COALESCE(i.status,m.status) IN ('approved','publishing'))
                OR d.status='pending_review' OR p.status='pending_review'
                OR (q.channel=? AND q.source_id=? AND (d.status='publishing' OR p.status='publishing')))
            """,
            (account, policy["epoch"], *active_key, bool(active.get("reserved")), *active_key),
        )
        pending.extend(item for actor, item in self.preparing.values() if actor == account)
        attempts = self.store.all(
            "SELECT channel,recipient,started_at,resolved_status FROM operation_write_attempts "
            "WHERE account_id=? AND (started_at>? OR resolved_status='sending') "
            f"AND (channel<>'comments' OR {COMMENT_WRITE_FILTER}) ORDER BY started_at DESC",
            (account, now - max(86400, min_interval)),
        )
        channel_pending = [item for item in pending if item["channel"] == channel]
        channel_attempts = [item for item in attempts if item["channel"] == channel]
        blocks = []
        for detail, quotas, reservations, writes in (
            ("渠道", limits, channel_pending, channel_attempts),
            ("账号", self.control.account_limits(), pending, [item for item in attempts if item["channel"] not in REACTION_CHANNELS]),
        ):
            for seconds, field, label in ((3600, "max_per_hour", "每小时"), (86400, "max_per_day", "24 小时")):
                maximum = quotas.get(field, 0)
                if not maximum:
                    continue
                recent = [item for item in writes if item["started_at"] > now - seconds]
                in_flight = sum(item["resolved_status"] == "sending" and item["started_at"] <= now - seconds for item in writes)
                if len(recent) >= maximum:
                    due = recent[maximum - 1]["started_at"] + seconds
                elif len(recent) + len(reservations) + in_flight >= maximum:
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

    def enqueue(self, channel, identity, *, direct=False, approval=None, scope=None):
        policy = self.control.policy()
        if approval is None and (not policy["enabled"] or policy["emergency"]):
            return
        if scope is None:
            scope = (policy["account_id"], policy["character_id"], policy["character_version"])
        now = time.time()
        with self.store.db:
            added = self.store.db.execute(
                "INSERT OR IGNORE INTO operation_send_queue(channel,source_id,account_id,character_id,character_version,"
                "epoch,direct,ready_at,wake_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (channel, str(identity), *scope, policy["epoch"], int(direct), now, now),
            ).rowcount
            if added and approval:
                self.store.db.execute("INSERT INTO operation_send_details VALUES(?,?,?,NULL)",
                                      (channel, str(identity), approval))
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

    def assert_approval(self, channel, identity, row, payload):
        item = current_send.get()
        if not item or item["channel"] != channel or item["source_id"] != str(identity) or not item["approval"]:
            return
        self.assert_ready(channel, identity)
        if item["approval"] != approved_version(row, payload):
            raise ValueError("正文、附件或原楼已变化，请重新审核该条草稿")
        self.control.assert_allowed(automatic=True, channel="comments", account_id=row["account_id"],
                                    character={"id": row["character_id"], "version": row["character_version"]})

    def _remove(self, item):
        self.store.execute("DELETE FROM operation_send_queue WHERE channel=? AND source_id=?",
                           (item["channel"], item["source_id"]))

    def due_background(self, scope, epoch, now):
        return self.store.all(
            "SELECT * FROM operation_send_items WHERE account_id=? AND character_id=? AND character_version=? "
            "AND (epoch=? OR approval IS NOT NULL) AND direct=0 AND reaction=0 AND wake_at<=? "
            "AND blocked IS NOT 'review'", (*scope, epoch, now),
        )

    def _defer(self, item, error, next_at, blocked=None):
        key = (item["channel"], item["source_id"])
        with self.store.db:
            updated = self.store.db.execute(
                "UPDATE operation_send_queue SET wake_at=?,error=? WHERE channel=? AND source_id=?",
                (next_at, public_error(error), *key),
            ).rowcount
            if updated:
                self.store.db.execute("INSERT INTO operation_send_details VALUES(?,?,NULL,?) "
                                      "ON CONFLICT(channel,source_id) DO UPDATE SET blocked=excluded.blocked", (*key, blocked))

    def ready_heads(self, scope, now):
        return self.store.all("""
            WITH pending AS (
                SELECT q.*,
                       CASE WHEN q.reaction=0 AND q.channel IN ('browsing','comments') THEN
                         COALESCE(json_extract(i.target,'$.bvid'),c.video_id,
                                  'dynamic:'||json_extract(i.target,'$.oid')) || '/' ||
                         COALESCE(CAST(json_extract(i.target,'$.root') AS TEXT),c.thread_id)
                       END AS floor
                  FROM operation_send_items q
                  LEFT JOIN browsing_intents i ON q.channel='browsing' AND q.source_id=i.id
                  LEFT JOIN drafts d ON q.channel='comments' AND q.source_id=CAST(d.id AS TEXT)
                  LEFT JOIN comments c ON d.comment_id=c.id
                 WHERE q.account_id=? AND q.character_id=? AND q.character_version=?
                   AND q.blocked IS NOT 'review'
            ), ranked AS (
                SELECT *,ROW_NUMBER() OVER (PARTITION BY floor ORDER BY ready_at,queue_order) AS position FROM pending
            ) SELECT * FROM ranked WHERE (floor IS NULL OR position=1) AND wake_at<=?
              ORDER BY reaction,direct DESC,(approval IS NULL),ready_at,queue_order
        """, (*scope, now))

    async def run(self):
        while True:
            self.changed.clear()
            delay = None
            if self.control.accepting_work:
                scope = self.scope()
                now = time.time()
                reaction_due = self.store.get(f"operation_reactions_resume:{scope[0]}", 0)
                # Keep background FIFO and preserve order within each direct
                # channel. An image wait leaves the sender free for other heads.
                heads = [head for head in self.ready_heads(scope, now) if not head["reaction"] or reaction_due <= now]
                item = next((head for head in heads if not self.waiting_for_image(head)), None)
                if item:
                    async with self.control.automatic_work() as admitted:
                        if admitted:
                            self.active = item
                            token = current_send.set(item)
                            try:
                                if await self.dispatch(item):
                                    self._remove(item)
                            except ReconciliationPending as error:
                                self._defer(item, error, error.next_at, "reconciliation")
                            except (SendLimitReached, DailyReplyLimitReached, httpx.TransportError, TimeoutError) as error:
                                self._defer(item, error, max(now + 1, getattr(error, "next_at", now + 60)), "wait")
                            except BilibiliAccountExpired as error:
                                self._defer(item, error, now + 60, "login")
                            except PlatformRejected as error:
                                if item["reaction"] and error.wait_for == "rate_limit":
                                    self.store.set(f"operation_reactions_resume:{scope[0]}", now + 60)
                                if item["approval"] and error.wait_for:
                                    self._defer(item, error, now + 60, error.wait_for)
                                elif item["approval"]:
                                    self._defer(item, error, now, "review")
                                else:
                                    self._remove(item)
                            except UnknownPublication:
                                if item["approval"] or item["channel"] in {"browsing", "dynamics"}:
                                    waiting = ReconciliationPending()
                                    self._defer(item, waiting, waiting.next_at, "reconciliation")
                                else:
                                    self._remove(item)
                            except Exception as error:
                                self.control.last_send_error = public_error(error)
                                if not self.control.draining:
                                    if item["approval"]:
                                        self._defer(item, error, now, "review")
                                    else:
                                        self._remove(item)
                            finally:
                                current_send.reset(token)
                                self.active = None
                                if self.settled:
                                    self.settled()
                    if self.control.accepting_work:
                        continue
                next_item = self.store.one(
                    "SELECT MIN(wake_at) AS wake_at FROM operation_send_items WHERE account_id=? "
                    "AND character_id=? AND character_version=? AND wake_at>? AND blocked IS NOT 'review'", (*scope, now),
                )
                if next_item["wake_at"] is not None:
                    delay = max(0.05, next_item["wake_at"] - time.time())
                if reaction_due > now:
                    delay = min(delay, reaction_due - now) if delay is not None else reaction_due - now
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
