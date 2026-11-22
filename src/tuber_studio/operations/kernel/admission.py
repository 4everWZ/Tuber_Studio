"""Bind automatic browsing actions to the shared send admission."""

from contextlib import contextmanager


class CommentAdmission:
    """Admits automatic channel actions onto the shared OperationsSends queue."""

    def __init__(self, browsing):
        self.browsing = browsing

    def requires_slot(self, row, kind):
        return not row["preview"] and self.browsing.publication._action_allowed(kind, row["config"])

    def review_reply_limit_reached(self, row, bvid):
        return not self.requires_slot(row, "reply_public") and self.browsing.repo.video_reply_limit_reached(row, bvid)

    @contextmanager
    def slot(self, row, kind, bvid=None, recipient="", *, direct=None):
        if not self.requires_slot(row, kind):
            yield None
            return
        channel = {
            "comment_like": "comment_likes",
            "video_like": "video_likes",
            "video_fav": "video_favorites",
        }.get(kind, "comments")
        limits = self.browsing.comments.publication_limits() if channel == "comments" else {}
        video_reply = None
        if bvid and channel == "comments":
            policy = self.browsing.policy()
            video_reply = row["character_id"], bvid, policy["max_replies_per_video"], policy["business_timezone"]
        if direct is None:
            is_direct = bool(row.get("source_pipeline") == "inbox" or (row.get("config") or {}).get("interaction_only"))
        else:
            is_direct = bool(direct)
        with self.browsing.sends.preparation_slot(
            row["account_id"],
            channel,
            limits,
            retry_seconds=row["config"]["notification_interval_seconds"],
            recipient=str(recipient or ""),
            video_reply=video_reply,
            direct=is_direct,
        ) as blocked:
            yield blocked
