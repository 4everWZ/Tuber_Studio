"""Persistent readiness for the existing automatic publication entry points."""

import asyncio
import json
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from ...core.errors import (
    PlatformRejected,
    SendLimitReached,
    UnknownPublication,
    is_media_permission_rejection,
    is_terminal_target_rejection,
)
from ...core.models import content_hash
from ...core.store import dump
from ...infra.inference.providers import public_error
from ...infra.platforms.bilibili.account import record_comment_capability_restriction
from ...infra.platforms.bilibili.client import BilibiliAccountExpired, PlatformReadError
from .control import COMMENT_WRITE_FILTER, REACTION_CHANNELS
from .quotas import DailyReplyLimitReached, publication_time

current_send = ContextVar("current_operation_send", default=None)

VISIBILITY_RISK_THRESHOLD = 2
VISIBILITY_RISK_COOLDOWN_SECONDS = 1800
VISIBILITY_INVISIBLE_STATES = frozenset({"self_only", "shadow_deleted", "not_visible"})
VISIBILITY_PUBLIC_STATES = frozenset({"public_visible", "visible"})


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
        self, account, channel, limits, *, retry_seconds, recipient="", video_reply=None, min_interval=0, direct=None,
    ):
        blocked = self.preparation_block(
            account, channel, limits, retry_seconds=retry_seconds,
            recipient=recipient, video_reply=video_reply, min_interval=min_interval, direct=direct,
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

    def _account_non_reaction_writes(self, account, now, *, min_interval=0):
        return self.store.all(
            "SELECT channel,recipient,started_at,resolved_status FROM operation_write_attempts "
            "WHERE account_id=? AND (started_at>? OR resolved_status='sending') "
            f"AND channel NOT IN (SELECT value FROM json_each(?)) "
            f"AND (channel<>'comments' OR {COMMENT_WRITE_FILTER}) ORDER BY started_at DESC",
            (account, now - max(86400, min_interval), json.dumps(sorted(REACTION_CHANNELS))),
        )

    @staticmethod
    def _is_comment_head(item):
        return item["channel"] == "comments" or (item["channel"] == "browsing" and not item["reaction"])

    def comments_cooldown_until(self, account):
        if not account:
            return 0.0
        value = self.store.get(f"operation_comments_resume:{account}", 0)
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def comments_pacing_until(self, account):
        if not account:
            return 0.0
        value = self.store.get(f"operation_comments_pacing:{account}", 0)
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def reactions_pacing_until(self, account):
        if not account:
            return 0.0
        value = self.store.get(f"operation_reactions_pacing:{account}", 0)
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def last_comment_target(self, account):
        if not account:
            return None
        saved = self.store.get(f"operation_last_comment_target:{account}")
        return saved if isinstance(saved, dict) else None

    @staticmethod
    def _pacing_jitter(base_seconds):
        return random.uniform(0.0, float(base_seconds) * 0.35) if base_seconds > 0 else 0.0

    def _item_bvid(self, item):
        if item["channel"] == "browsing":
            row = self.store.one(
                "SELECT json_extract(target,'$.bvid') AS bvid FROM browsing_intents WHERE id=?",
                (item["source_id"],),
            )
            return str((row or {}).get("bvid") or "")
        if item["channel"] == "comments":
            row = self.store.one(
                "SELECT c.video_id AS bvid FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE d.id=?",
                (item["source_id"],),
            )
            return str((row or {}).get("bvid") or "")
        return ""

    def _record_send_pacing(self, account, item, *, sent_at, item_bvid=""):
        if not account:
            return
        policy = self.control.policy()
        if item["reaction"]:
            rbase = int(policy.get("min_reaction_interval_seconds", 0) or 0)
            if rbase > 0:
                due = max(self.reactions_pacing_until(account), sent_at + rbase + self._pacing_jitter(rbase))
                self.store.set(f"operation_reactions_pacing:{account}", due)
        elif self._is_comment_head(item):
            cbase = int(policy.get("min_comment_interval_seconds", 0) or 0)
            if cbase > 0:
                due = max(self.comments_pacing_until(account), sent_at + cbase + self._pacing_jitter(cbase))
                self.store.set(f"operation_comments_pacing:{account}", due)
                if item_bvid:
                    self.store.set(f"operation_last_comment_target:{account}", {"bvid": item_bvid, "at": sent_at})

    def visibility_risk_status(self, account, *, now=None):
        if not account:
            return None
        current = time.time() if now is None else now
        saved = self.store.get(f"operation_visibility_risk:{account}")
        if not isinstance(saved, dict):
            return None
        streak = int(saved.get("streak") or 0)
        cooldown_until = saved.get("cooldown_until")
        try:
            cooldown_until = float(cooldown_until) if cooldown_until is not None else None
        except (TypeError, ValueError):
            cooldown_until = None
        cooldown_active = bool(cooldown_until and cooldown_until > current)
        downgraded = bool(saved.get("downgraded", False))
        if streak <= 0 and not cooldown_active and not downgraded:
            return None
        reason = saved.get("reason") or ""
        if cooldown_until and not cooldown_active:
            reason = f"此前 {streak} 条评论未公开可见，评论冷却已结束"
        return {
            "account_id": str(account),
            "streak": streak,
            "threshold": int(saved.get("threshold") or VISIBILITY_RISK_THRESHOLD),
            "active": cooldown_active,
            "cooldown_until": cooldown_until if cooldown_active else None,
            "downgraded": downgraded,
            "last_status": saved.get("last_status"),
            "last_intent_id": saved.get("last_intent_id"),
            "last_bvid": saved.get("last_bvid"),
            "reason": reason,
            "updated_at": saved.get("updated_at"),
        }

    def record_visibility_outcome(
        self, account, status, *, intent_id=None, bvid=None, detail=None, now=None, published_at=None
    ):
        if not account:
            return None
        current = time.time() if now is None else now
        observed_publication = current if published_at is None else float(published_at)
        account = str(account)
        key = f"operation_visibility_risk:{account}"
        prev = self.store.get(key) or {}
        if not isinstance(prev, dict):
            prev = {}
        last_publication = prev.get("last_publication_at") or prev.get("updated_at") or 0
        if observed_publication < last_publication and str(intent_id or "") != prev.get("last_intent_id"):
            return self.visibility_risk_status(account, now=current)
        if status in VISIBILITY_PUBLIC_STATES:
            if prev:
                self.store.set(key, None)
            return None
        if status not in VISIBILITY_INVISIBLE_STATES:
            return self.visibility_risk_status(account, now=current)
        recent_ids = list(prev.get("recent_intent_ids") or [])
        if intent_id and str(intent_id) in recent_ids:
            return self.visibility_risk_status(account, now=current)
        if current - observed_publication >= VISIBILITY_RISK_COOLDOWN_SECONDS:
            return self.visibility_risk_status(account, now=current)
        same_window = observed_publication - last_publication < VISIBILITY_RISK_COOLDOWN_SECONDS
        streak = int(prev.get("streak") or 0) + 1 if same_window else 1
        if not same_window:
            recent_ids = []
        if intent_id:
            recent_ids.append(str(intent_id))
        triggered = streak >= VISIBILITY_RISK_THRESHOLD
        downgraded = bool(prev.get("downgraded", False))
        cooldown_until = prev.get("cooldown_until")
        if triggered:
            due = max(self.comments_cooldown_until(account), current + VISIBILITY_RISK_COOLDOWN_SECONDS)
            self.store.set(f"operation_comments_resume:{account}", due)
            cooldown_until = due
            browsing_policy = self.store.get("browsing_policy") or {}
            if isinstance(browsing_policy, dict) and browsing_policy.get("auto_downgrade_on_error", True):
                if browsing_policy.get("top_level_auto_publish") or browsing_policy.get("reply_auto_publish"):
                    updated_policy = browsing_policy | {
                        "top_level_auto_publish": False,
                        "reply_auto_publish": False,
                    }
                    self.store.set("browsing_policy", updated_policy)
                    downgraded = True
            self.changed.set()
        status_label = {
            "self_only": "仅自己可见",
            "shadow_deleted": "疑似被吞",
            "not_visible": "平台隐藏",
        }.get(status, status)
        reason = (
            f"连续 {streak} 条评论未公开可见（最近：{status_label}），已暂停评论发送 30 分钟"
            + ("并降级为人工审核" if downgraded else "")
            if triggered
            else f"检测到评论未公开可见（{status_label}，连续 {streak}/{VISIBILITY_RISK_THRESHOLD} 次）"
        )
        state = {
            "account_id": account,
            "streak": streak,
            "threshold": VISIBILITY_RISK_THRESHOLD,
            "active": triggered or (bool(cooldown_until) and float(cooldown_until) > current),
            "cooldown_until": cooldown_until,
            "downgraded": downgraded,
            "last_status": status,
            "last_intent_id": str(intent_id) if intent_id is not None else prev.get("last_intent_id"),
            "last_bvid": bvid or prev.get("last_bvid"),
            "recent_intent_ids": recent_ids[-10:],
            "reason": reason,
            "updated_at": current,
            "last_publication_at": observed_publication,
        }
        self.store.set(key, state)
        return self.visibility_risk_status(account, now=current)

    def direct_reserve_block(
        self, account, *, pending_count=0, retry_seconds=60, now=None, writes=None,
    ):
        """Protect direct=1 (DM/Inbox/direct reply) quota by deferring direct=0 sends when remaining <= reserve."""
        policy = self.control.policy()
        reserve = int(policy.get("direct_quota_reserve", 2) or 0)
        if reserve <= 0:
            return None
        current = time.time() if now is None else now
        quotas = self.control.account_limits()
        if writes is None:
            writes = self._account_non_reaction_writes(account, current)
        blocks = []
        for seconds, field, label in ((3600, "max_per_hour", "每小时"), (86400, "max_per_day", "24 小时")):
            maximum = quotas.get(field, 0)
            if not maximum:
                continue
            recent = [item for item in writes if item["started_at"] > current - seconds]
            in_flight = sum(
                item["resolved_status"] == "sending" and item["started_at"] <= current - seconds for item in writes
            )
            used = len(recent) + pending_count + in_flight
            if maximum - used <= reserve:
                allowed_non_direct = max(0, maximum - reserve)
                if allowed_non_direct == 0:
                    due = (recent[0]["started_at"] + seconds) if recent else (current + retry_seconds)
                elif len(recent) >= allowed_non_direct:
                    due = recent[allowed_non_direct - 1]["started_at"] + seconds
                else:
                    due = current + retry_seconds
                blocks.append(SendLimitReached(f"账号{label}剩余发送额度已预留给私信与直接回复，暂缓探索发送", due))
        return max(blocks, key=lambda error: error.next_at, default=None)

    def preparation_block(
        self, account, channel, limits, *, retry_seconds, recipient="", video_reply=None, min_interval=0, direct=None,
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
        if channel == "comments" and (comments_due := self.comments_cooldown_until(account)) > now:
            blocks.append(SendLimitReached("平台评论发送上限冷却中，暂缓评论拟稿", comments_due))
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
        is_direct = (channel == "messages") if direct is None else bool(direct)
        if not is_direct:
            non_reaction_attempts = [item for item in attempts if item["channel"] not in REACTION_CHANNELS]
            if reserve_block := self.direct_reserve_block(
                account,
                pending_count=len(pending),
                retry_seconds=retry_seconds,
                now=now,
                writes=non_reaction_attempts,
            ):
                blocks.append(reserve_block)
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
                policy = self.control.policy()
                cbase = int(policy.get("min_comment_interval_seconds", 0) or 0)
                rbase = int(policy.get("min_reaction_interval_seconds", 0) or 0)
                reaction_due = max(
                    float(self.store.get(f"operation_reactions_resume:{scope[0]}", 0) or 0),
                    self.reactions_pacing_until(scope[0]) if rbase > 0 else 0.0,
                )
                comments_due = max(
                    self.comments_cooldown_until(scope[0]),
                    self.comments_pacing_until(scope[0]) if cbase > 0 else 0.0,
                )
                last_target = self.last_comment_target(scope[0]) if cbase > 0 else None
                same_video_due = 0.0
                # Keep background FIFO and preserve order within each direct
                # channel. An image wait leaves the sender free for other heads.
                heads = []
                for head in self.ready_heads(scope, now):
                    if head["reaction"] and reaction_due > now:
                        continue
                    if self._is_comment_head(head):
                        if comments_due > now:
                            continue
                        if cbase > 0 and isinstance(last_target, dict) and last_target.get("bvid"):
                            head_bvid = self._item_bvid(head)
                            sv_due = float(last_target.get("at") or 0) + cbase * 2
                            if head_bvid and head_bvid == last_target["bvid"] and sv_due > now:
                                same_video_due = sv_due if same_video_due == 0.0 else min(same_video_due, sv_due)
                                continue
                    heads.append(head)
                reserve_block = self.direct_reserve_block(scope[0], retry_seconds=60, now=now)
                eligible_heads = []
                for head in heads:
                    if not head["reaction"] and not head["direct"] and reserve_block is not None:
                        self._defer(head, reserve_block, max(now + 1, reserve_block.next_at), "wait")
                        continue
                    eligible_heads.append(head)
                heads = eligible_heads
                item = next((head for head in heads if not self.waiting_for_image(head)), None)
                if item:
                    async with self.control.automatic_work() as admitted:
                        if admitted:
                            self.active = item
                            token = current_send.set(item)
                            item_bvid = self._item_bvid(item) if self._is_comment_head(item) else ""
                            try:
                                if await self.dispatch(item):
                                    self._record_send_pacing(
                                        scope[0], item, sent_at=time.time(), item_bvid=item_bvid
                                    )
                                    self._remove(item)
                            except ReconciliationPending as error:
                                self._defer(item, error, error.next_at, "reconciliation")
                            except (SendLimitReached, DailyReplyLimitReached, httpx.TransportError, TimeoutError) as error:
                                self._defer(item, error, max(now + 1, getattr(error, "next_at", now + 60)), "wait")
                            except BilibiliAccountExpired as error:
                                self._defer(item, error, now + 60, "login")
                            except PlatformReadError as error:
                                if is_unavailable_read_error(error):
                                    if item["channel"] == "browsing":
                                        mark_intent_source_unavailable(
                                            self.store, item["source_id"], public_error(error)
                                        )
                                    self._remove(item)
                                else:
                                    self._defer(
                                        item, error, max(now + 1, getattr(error, "next_at", now + 60)), "wait"
                                    )
                            except PlatformRejected as error:
                                if error.wait_for == "rate_limit":
                                    cooldown = (
                                        900
                                        if getattr(error, "code", None) in {12014, 12015, -352, -412}
                                        else 60
                                    )
                                    due = now + cooldown
                                    if item["reaction"]:
                                        due = now + 60
                                        self.store.set(f"operation_reactions_resume:{scope[0]}", due)
                                    elif self._is_comment_head(item):
                                        self.store.set(f"operation_comments_resume:{scope[0]}", due)
                                    self._defer(item, error, due, "rate_limit" if item["approval"] else "wait")
                                elif is_terminal_target_rejection(error.code):
                                    if item["channel"] == "browsing":
                                        mark_intent_source_unavailable(
                                            self.store, item["source_id"], public_error(error)
                                        )
                                    self._remove(item)
                                elif is_media_permission_rejection(error.code):
                                    self._remove(item)
                                elif item["approval"] and error.wait_for:
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
                if comments_due > now:
                    delay = min(delay, comments_due - now) if delay is not None else comments_due - now
                if same_video_due > now:
                    delay = min(delay, same_video_due - now) if delay is not None else same_video_due - now
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


def schedule_intent_visibility_recheck(publisher, identity: str, *, delay: float | None = None):
    wait = float(publisher.visibility_recheck_delay if delay is None else delay)
    existing = publisher._visibility_tasks.get(identity)
    if existing and not existing.done():
        existing.cancel()

    async def _run():
        try:
            if wait > 0:
                await asyncio.sleep(wait)
            if getattr(publisher.browsing, "closed", False):
                return
            await publisher.verify_intent_visibility(identity)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            now = time.time()
            detail = {"status": "check_failed", "checked_at": now, "reason": public_error(error)}
            try:
                publisher.browsing.store.execute(
                    "UPDATE browsing_intents SET visibility='check_failed',visibility_checked_at=?,visibility_detail=? WHERE id=?",
                    (now, dump(detail), identity),
                )
            except Exception:
                pass

    task = asyncio.create_task(_run())
    publisher._visibility_tasks[identity] = task
    task.add_done_callback(
        lambda done: publisher._visibility_tasks.pop(identity, None)
        if publisher._visibility_tasks.get(identity) is done
        else None
    )
    return task


async def verify_intent_visibility_now(publisher, identity: str, *, comment_kinds=("top_level", "reply_public")):
    row = publisher.browsing.repo.decode(
        publisher.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
    )
    if not row:
        raise ValueError("候选不存在")
    if row["status"] != "published":
        raise ValueError("仅已提交成功的评论支持可见性复查")
    if row["kind"] not in comment_kinds:
        return row
    receipt = row.get("receipt")
    if not isinstance(receipt, dict) or not receipt.get("rpid"):
        raise ValueError("评论缺少有效回执 rpid，无法复查可见性")
    verifier = getattr(publisher.browsing.adapter, "verify_publication", None)
    if not callable(verifier):
        raise ValueError("当前平台适配器不支持可见性复查")
    try:
        result = await verifier(row["target"], receipt)
        if not isinstance(result, dict):
            result = {"status": "check_failed", "checked_at": time.time(), "reason": "可见性返回格式无效"}
    except Exception as error:
        result = {"status": "check_failed", "checked_at": time.time(), "reason": public_error(error)}
    status = str(result.get("status") or "check_failed")
    checked_at = float(result.get("checked_at") or time.time())
    publisher.browsing.store.execute(
        "UPDATE browsing_intents SET visibility=?,visibility_checked_at=?,visibility_detail=? WHERE id=?",
        (status, checked_at, dump(result), identity),
    )
    sends = getattr(publisher.browsing, "sends", None)
    if sends is not None and hasattr(sends, "record_visibility_outcome"):
        sends.record_visibility_outcome(
            row["account_id"],
            status,
            intent_id=identity,
            bvid=(row.get("target") or {}).get("bvid"),
            detail=result,
            now=checked_at,
            published_at=publication_time(receipt, row["updated_at"]),
        )
    return publisher.browsing.repo.decode(
        publisher.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
    )


async def recheck_pending_intent_visibility(publisher, *, limit: int = 10):
    rows = publisher.browsing.store.all(
        "SELECT id FROM browsing_intents WHERE status='published' AND kind IN ('top_level','reply_public') "
        "AND visibility='pending_recheck' ORDER BY updated_at ASC LIMIT ?",
        (limit,),
    )
    results = []
    for row in rows:
        if getattr(publisher.browsing, "closed", False):
            break
        try:
            results.append(await publisher.verify_intent_visibility(row["id"]))
        except ValueError:
            continue
    return results


def is_unavailable_read_error(error) -> bool:
    """Return True when a PlatformReadError indicates deleted/closed/missing video or comment source."""
    return isinstance(error, PlatformReadError) and (
        error.kind in {"not_found", "closed"} or (error.kind == "invalid" and "已删除" in str(error))
    )


def mark_intent_source_unavailable(store, identity: str, error_message: str):
    """Settle a `browsing_intents` row when its target video or comment no longer exists / is closed."""
    store.execute(
        "UPDATE browsing_intents SET status='rejected_by_platform',auto_authorized=0,error=?,updated_at=? WHERE id=?",
        (str(error_message or "平台目标不存在或已关闭")[:240], time.time(), identity),
    )


def degrade_intent_text_only(store, row: dict, *, reason: str = "", reset_status: str | None = None) -> dict:
    """Replace `target.image_ref` with `text_only` cleanly for disallowed media fallback."""
    target = dict(row["target"]) | {"image_ref": {"text_only": True}}
    if reason:
        target["image_degraded_reason"] = str(reason)[:240]
    if reset_status:
        store.execute(
            "UPDATE browsing_intents SET status=?,target=?,error=NULL,updated_at=? WHERE id=?",
            (reset_status, dump(target), time.time(), row["id"]),
        )
        return dict(row) | {"status": reset_status, "target": target}
    store.execute(
        "UPDATE browsing_intents SET target=?,error=NULL,updated_at=? WHERE id=?",
        (dump(target), time.time(), row["id"]),
    )
    return dict(row) | {"target": target}


def settle_browsing_rejection(browsing, row: dict, error: PlatformRejected, *, automatic: bool, is_like: bool, had_image: bool) -> str:
    """Record capability restrictions, target termination, or review parking after a `PlatformRejected`."""
    identity = row["id"]
    if is_media_permission_rejection(error.code):
        record_comment_capability_restriction(
            browsing.store,
            row["account_id"],
            gif_allowed=False if error.code == 49044 else None,
            image_allowed=False if error.code == 49045 else None,
            reason=public_error(error),
        )
        if automatic and had_image:
            degrade_intent_text_only(browsing.store, row, reason=public_error(error), reset_status="pending_review")
            return "retry_text_only"
        browsing.store.execute(
            "UPDATE browsing_intents SET status='pending_review',auto_authorized=0,error=?,updated_at=? WHERE id=?",
            (public_error(error) + "（已记录账号不支持此类评论配图，请改用静态图或纯文字后重发）", time.time(), identity),
        )
        return "pending_review"
    if is_terminal_target_rejection(error.code):
        mark_intent_source_unavailable(browsing.store, identity, public_error(error))
        return "rejected_by_platform"
    status = row["status"] if not is_like else "rejected_by_platform"
    browsing.store.execute(
        "UPDATE browsing_intents SET status=?,error=?,updated_at=? WHERE id=?",
        (status, public_error(error), time.time(), identity),
    )
    return status
