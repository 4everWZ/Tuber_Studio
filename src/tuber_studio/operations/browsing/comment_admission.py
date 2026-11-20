"""Admission for automatic comment drafting, before model or image work."""

import time
from contextlib import contextmanager

from ...publication_errors import SendLimitReached
from .quotas import DailyReplyLimitReached


class CommentAdmission:
    def __init__(self, browsing):
        self.browsing = browsing
        self.preparing = {}

    @contextmanager
    def slot(self, row, kind, bvid, recipient=""):
        if row["preview"] or not self.browsing.publication._action_allowed(kind, row["config"]):
            yield None
            return
        account, character = row["account_id"], row["character_id"]
        error = self._capacity(row, account, character, bvid, str(recipient or ""))
        if error:
            yield error
            return
        # Core has one account writer. Hold the slot across awaits, until the
        # synchronous draft/queue handoff or until no draft is needed.
        key = object()
        self.preparing[key] = account, character, bvid, str(recipient or "")
        try:
            yield None
        finally:
            del self.preparing[key]

    def _capacity(self, row, account, character, bvid, recipient):
        browsing = self.browsing
        store = browsing.store
        policy = browsing.operations.policy()
        config = row["config"]
        now = time.time()
        retry = now + config["notification_interval_seconds"]
        preparations = [(owner, video, uid) for actor, owner, video, uid in self.preparing.values()
                        if actor == account]
        queued = store.all(
            """
            SELECT CASE WHEN q.channel='browsing' THEN CASE i.kind
                WHEN 'comment_like' THEN 'comment_likes' WHEN 'video_like' THEN 'video_likes'
                WHEN 'video_fav' THEN 'video_favorites' ELSE 'comments' END ELSE q.channel END AS channel,
                i.kind, q.character_id, json_extract(i.target,'$.bvid') AS bvid,
                COALESCE(json_extract(i.target,'$.recipient'),d.platform_user_id,'') AS recipient
            FROM operation_send_queue q
            LEFT JOIN browsing_intents i ON q.channel='browsing' AND i.id=q.source_id
            LEFT JOIN dm_drafts d ON q.channel='messages' AND CAST(d.id AS TEXT)=q.source_id
            LEFT JOIN dynamic_posts p ON q.channel='dynamics' AND CAST(p.id AS TEXT)=q.source_id
            WHERE q.account_id=? AND q.epoch=? AND (
                (i.status='pending_review' AND i.auto_authorized=1)
                OR d.status='pending_review' OR p.status='pending_review')
            """,
            (account, policy["epoch"]),
        )
        attempts = store.all(
            "SELECT channel,recipient,started_at,resolved_status FROM operation_write_attempts "
            "WHERE account_id=? AND (started_at>? OR resolved_status='sending') ORDER BY started_at DESC",
            (account, now - 86400),
        )
        channel_limits = browsing.comments.publication_limits()
        for channel, limits in ((None, policy), ("comments", channel_limits)):
            waiting = len(preparations) + sum(channel is None or item["channel"] == channel for item in queued)
            for seconds, name in ((3600, "max_per_hour"), (86400, "max_per_day")):
                maximum = limits.get(name, 0)
                if not maximum:
                    continue
                running_or_recent = [item for item in attempts if (channel is None or item["channel"] == channel)
                                     and (item["started_at"] > now - seconds or item["resolved_status"] == "sending")]
                room = maximum - waiting
                if room <= 0 or len(running_or_recent) >= room:
                    detail = "账号" if channel is None else "评论渠道"
                    due = retry if waiting or any(item["resolved_status"] == "sending" for item in running_or_recent) else (
                        running_or_recent[room - 1]["started_at"] + seconds)
                    return SendLimitReached(f"{detail}发送额度已占满，暂不拟稿", due)

        if recipient and (cooldown := policy["recipient_cooldown_seconds"]):
            if any(uid == recipient for _, _, uid in preparations) or any(
                str(item["recipient"]) == recipient for item in queued
            ):
                return SendLimitReached("该用户已有待发回应，暂不拟稿", retry)
            if last := next((item["started_at"] for item in attempts
                             if item["recipient"] == recipient and item["started_at"] > now - cooldown), None):
                return SendLimitReached("该用户回复冷却中，暂不拟稿", last + cooldown)

        if bvid and (limit := config.get("max_replies_per_video", 2)):
            status = browsing.repo.quotas.reply_status(
                account, character, bvid, limit, config.get("business_timezone", "Asia/Taipei"), now=now,
            )
            if status["remaining"] == 0:
                return DailyReplyLimitReached(status, message="达到该视频今日回复上限，暂不拟稿")
            waiting = sum(owner == character and video == bvid for owner, video, _ in preparations) + sum(
                item["character_id"] == character and item["bvid"] == bvid
                and item["kind"] in {"top_level", "reply_public"} for item in queued
            )
            if waiting >= status["remaining"]:
                return SendLimitReached("该视频剩余回复额度已预留，暂不拟稿", retry)
        return None
