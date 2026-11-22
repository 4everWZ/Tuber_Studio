"""Send queue dispatch, backlog high/low water computation, and automatic send helpers."""

import asyncio
import math
import time

from ..core.errors import SendLimitReached
from ..core.turns import operation_lane
from .channels.attachments.attachments import (
    can_attach_comment_image,
    comment_payload,
    image_decision_pending,
)
from .kernel.control import COMMENT_WRITE_FILTER
from .kernel.sends import ReconciliationPending
from .kernel.telemetry import flow_window


def send_permissions(svc):
    """Return current auto/review capability permissions across channels for resume checks."""
    config = svc.configured()
    saved = svc.store.get("message_policy", {})
    dynamics = svc.store.get("dynamic_policy", {})
    return {
        "browsing": {key: value for key, value in config.items() if key.endswith(("_enabled", "_auto_publish"))},
        "messages": {key: saved.get(key) for key in ("enabled", "auto_send", "allow_comment_handoff")},
        "dynamics": {key: dynamics.get(key) for key in ("enabled", "auto_publish")},
    }


def waiting_round(svc):
    """Return the oldest paused batch round with unfinished non-visit items."""
    account, character, version = svc.operation_scope()
    return svc.store.one(
        """SELECT r.id, r.status,
                  (SELECT COUNT(*) FROM browsing_batch_items i WHERE i.batch_id=b.id
                   AND i.status NOT IN ('completed','skipped','later','daily_wait')) AS remaining
           FROM browsing_rounds r JOIN browsing_batches b ON b.round_id=r.id
           WHERE r.account_id=? AND r.character_id=? AND r.character_version=? AND r.status='paused'
           AND b.state='paused' AND COALESCE(json_extract(r.config,'$.review_session_id'),'')=?
           ORDER BY r.created_at LIMIT 1""",
        (account, character, version, svc.effective().get("review_session_id", "")),
    )


def operation_scope(svc):
    """Return active `(account_id, character_id, character_version)` scope tuple."""
    control = svc.operations.policy()
    if control["enabled"]:
        return control["account_id"], control["character_id"], control["character_version"]
    return svc.discovery.history_scope()


def backlog_status(svc, *, persist=True):
    """Compute backlog metrics, high/low water discovery pause state, and latency summaries."""
    account, character, version = svc.operation_scope()
    review = svc.effective().get("review_session_id")
    now = time.time()
    scope = {
        "account": account,
        "character": character,
        "version": version,
        "review": review,
        "mentions": svc.effective()["mention_reply_enabled"],
        "now": now,
    }
    heads = svc.store.one(
        """WITH floors AS (
             SELECT MIN(created_at) AS waiting_since,
                    MIN(COALESCE(CASE WHEN json_valid(error) THEN json_extract(error,'$.next_at') END,0)) AS due
             FROM browsing_events WHERE account_id=:account AND kind='comment_visit'
               AND json_extract(payload,'$.character_id')=:character
               AND json_extract(payload,'$.review_session_id') IS :review
               AND status IN ('received','source_verified','context_ready','reply_deferred')
             GROUP BY json_extract(payload,'$.resource_type'),
               COALESCE(CAST(json_extract(payload,'$.oid') AS TEXT),json_extract(payload,'$.bvid'),source_key,id),
               COALESCE(NULLIF(NULLIF(CAST(json_extract(payload,'$.root') AS TEXT),'0'),''),source_key,id)
           ) SELECT COUNT(*) AS total,COUNT(CASE WHEN due<=:now THEN 1 END) AS count,
                    MIN(waiting_since) AS oldest,MIN(CASE WHEN due<=:now THEN waiting_since END) AS oldest_ready
             FROM floors""",
        scope,
    )
    rate = max(0.2, float(svc.store.get("operation_background_heads_per_minute", 2.9)))
    high, low = max(1, math.ceil(5 * rate)), max(0, math.ceil(2 * rate) - 1)
    age = max(0, now - heads["oldest"]) if heads["oldest"] is not None else 0
    ready_age = max(0, now - heads["oldest_ready"]) if heads["oldest_ready"] is not None else 0
    key = f"operation_discovery_paused:{account}:{character}:{version}:{review or ''}"
    old = svc.store.get(key, False)
    hourly_due = svc.comment_window_due(now=now)
    ready_sends = svc._ready_approved_comments(now=now)
    cooling = bool(hourly_due or svc.store.get(key + ":quota", False))
    if cooling and not hourly_due and ready_sends == 0 and heads["count"] == 0 and heads["total"] <= low:
        cooling = False
    paused = (
        (heads["total"] > low or ready_age > 120)
        if old
        else (heads["total"] >= high or heads["count"] >= high or ready_age >= 300)
    )
    if persist and bool(svc.store.get(key + ":quota", False)) != cooling:
        svc.store.set(key + ":quota", cooling)
    if persist and paused != old:
        svc.store.set(key, paused)
    sends = svc.store.one(
        """SELECT COUNT(CASE WHEN reaction=0 THEN 1 END) AS queued,
                  COUNT(CASE WHEN reaction=0 AND wake_at>:now AND blocked IS NOT 'reconciliation' THEN 1 END) AS deferred,
                  COUNT(CASE WHEN blocked='review' THEN 1 END) AS rejected,
                  COUNT(CASE WHEN reaction=1 THEN 1 END) AS reactions
           FROM operation_send_items WHERE account_id=:account AND character_id=:character
             AND character_version=:version""",
        scope,
    )
    discovery = svc.store.one(
        """SELECT COUNT(*) AS n, AVG(created_at-timestamp) AS mean_seconds,
                  MAX(created_at-timestamp) AS max_seconds FROM browsing_events
           WHERE account_id=:account AND kind<>'comment_visit' AND timestamp>0
             AND created_at>=timestamp AND created_at>=:now-86400""",
        scope,
    )
    inbox = svc.store.one(
        """SELECT COUNT(*) AS n, AVG(received_at-sent_at) AS mean_seconds,
                  MAX(received_at-sent_at) AS max_seconds FROM dm_inbox
           WHERE account_id=:account AND character_id=:character AND sent_at>0
             AND received_at>=sent_at AND received_at>=:now-86400""",
        scope,
    )
    uncertain = {}
    for channel, table in (("comments", "browsing_intents"), ("messages", "dm_drafts"), ("dynamics", "dynamic_posts")):
        uncertain[channel] = svc.store.one(
            f"SELECT COUNT(*) AS n FROM {table} WHERE account_id=? AND character_id=? AND status='publish_uncertain'",
            (account, character),
        )["n"]
    videos = svc.store.one(
        "SELECT COUNT(*) AS pending,COALESCE(SUM(history_count),0) AS history "
        "FROM browsing_video_work WHERE account_id=? AND character_id=? AND review=? AND preview=0",
        (account, character, review or ""),
    )
    latest = svc.store.one(
        "SELECT * FROM browsing_rounds WHERE account_id=? AND character_id=? AND preview=0 "
        "AND json_extract(config,'$.review_session_id') IS NULL ORDER BY created_at DESC LIMIT 1",
        (account, character),
    )
    sends["rejected"] += svc.store.one(
        """SELECT COUNT(*) AS n FROM operation_continuations c
         WHERE c.channel IN ('comments','browsing') AND c.state IN ('checkpoint','waiting','needs_material')
           AND json_extract(c.feedback,'$.kind')='platform_rejection' AND json_extract(c.binding,'$.account_id')=?
           AND json_extract(c.binding,'$.character.id')=?
           AND NOT EXISTS(SELECT 1 FROM operation_send_queue q WHERE q.channel=c.channel AND q.source_id=c.entity_id)""",
        (account, character),
    )["n"]
    return {
        "discovery_delay": {"direct_public": discovery, "direct_messages": inbox},
        "total_heads": heads["total"],
        "ready_heads": heads["count"],
        "oldest_seconds": int(age),
        "high_heads": high,
        "low_heads": low,
        "heads_per_minute": rate,
        "discovery_paused": paused or cooling,
        "comment_hourly_resume_at": hourly_due,
        "catching_up": cooling and not hourly_due,
        "waiting_comments": heads["total"] - heads["count"],
        "sends": sends,
        "review": svc.review_counts(account, character),
        "uncertain": uncertain,
        "video_work": videos,
        "saved_pages": svc.store.one(
            "SELECT COUNT(*) AS n FROM browsing_comment_page_handoffs WHERE account_id=? "
            "AND character_id=? AND review_session=?",
            (account, character, review or ""),
        )["n"],
        "last_round_flow": flow_window(svc.store, latest) if latest else {},
    }


def comment_window_due(svc, *, now=None):
    """Return timestamp when rolling hourly public comment quota clears, or None."""
    maximum = svc.comments.publication_limits().get("max_per_hour", 0)
    if not maximum:
        return None
    now = time.time() if now is None else now
    account, _, _ = svc.operation_scope()
    row = svc.store.one(
        "SELECT started_at FROM operation_write_attempts WHERE account_id=? AND channel='comments' "
        f"AND started_at>? AND {COMMENT_WRITE_FILTER} ORDER BY started_at DESC LIMIT 1 OFFSET ?",
        (account, now - 3600, maximum - 1),
    )
    return row["started_at"] + 3600 if row else None


def ready_approved_comments(svc, *, now=None):
    """Count ready manual-approved comment sends awaiting dispatch."""
    return sum(
        bool(item["approval"]) and item["blocked"] != "reconciliation"
        for item in svc.sends.ready_heads(svc.operation_scope(), time.time() if now is None else now)
    )


def send_reserved(svc):
    """Notify send queue and update comment quota snapshot after reserving a send attempt."""
    svc.sends.attempt_started()
    try:
        asyncio.get_running_loop().call_soon(svc._snapshot_comment_window)
    except RuntimeError:
        pass


def snapshot_comment_window(svc):
    """Persist quota cooling state once a comment hour limit is reached."""
    if not svc.closed and svc.operations.accepting_work and svc.comment_window_due():
        svc.backlog_status()


def send_settled(svc):
    """Wake runners when a queued send settles."""
    svc.interactions_changed.set()
    svc.changed.set()


def review_counts(svc, account, character):
    """Count pending-review drafts per channel not currently queued for send."""
    result = {}
    for channel, table in (("comments", "browsing_intents"), ("messages", "dm_drafts"), ("dynamics", "dynamic_posts")):
        policy = "AND kind IN ('reply_public','top_level')" if channel == "comments" else ""
        result[channel] = svc.store.one(
            f"SELECT COUNT(*) AS n FROM {table} AS d WHERE account_id=? AND character_id=? "
            f"AND status='pending_review' {policy} AND NOT EXISTS("
            "SELECT 1 FROM operation_send_queue AS q WHERE q.channel=? AND q.source_id=CAST(d.id AS TEXT))",
            (account, character, "browsing" if channel == "comments" else channel),
        )["n"]
    return result


def review_capacity(svc, channel, row=None):
    """Return True if review queue for `channel` has remaining daily review budget."""
    if row and row["preview"]:
        return True
    account, character, _ = svc.discovery.history_scope()
    if row:
        account, character = row["account_id"], row["character_id"]
        if row["config"].get("review_session_id"):
            return True
    if svc.effective().get("review_session_id"):
        return True
    limit = (
        svc.comments.publication_limits().get("max_per_day", 0)
        if channel == "comments"
        else svc.messages.policy()["max_per_day"]
        if channel == "messages"
        else svc.dynamics.policy()["max_per_day"]
    )
    return not limit or svc.review_counts(account, character)[channel] < limit


def page_capacity(svc):
    """Return True when Discovery may fetch another old comment page within backpressure bounds."""
    state = svc.backlog_status()
    return not state["discovery_paused"] and not state["saved_pages"] and state["total_heads"] < state["high_heads"]


def send_draft(svc, item):
    """Load the source draft row for a queued `OperationSends` item."""
    identity, channel = item["source_id"], item["channel"]
    if channel == "browsing":
        return svc.repo.decode(svc.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
    if channel == "comments":
        return svc.comments._draft_source(identity)
    if channel == "messages":
        return svc.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    if channel == "dynamics":
        return svc.dynamics._post(identity)
    return None


def send_waiting_for_image(svc, item):
    """Return True if a queued auto-authorized draft is still waiting on async meme selection."""
    if item["approval"]:
        return False
    if item["epoch"] != svc.operations.policy()["epoch"]:
        return False
    draft, channel = svc._send_draft(item), item["channel"]
    if not draft or draft["status"] != "pending_review":
        return False
    if channel == "browsing" and (
        not draft["auto_authorized"]
        or not can_attach_comment_image(channel, draft)
        or not svc.publication._action_allowed(draft["kind"], svc.effective())
    ):
        return False
    if channel == "messages" and not svc.messages.policy()["auto_send"]:
        return False
    if channel == "dynamics" and not svc.dynamics.policy()["auto_publish"]:
        return False
    try:
        return image_decision_pending(svc.runtime, channel, draft)
    except (LookupError, ValueError):
        return False


def queue_publication(svc, identity, *, direct=None):
    """Enqueue a browsing intent onto `OperationSends` with explicit/derived `direct` priority."""
    if direct is None:
        intent = svc.repo.decode(svc.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        if intent and intent.get("source_pipeline"):
            direct = intent["source_pipeline"] == "inbox"
        else:
            direct = operation_lane.get() == "direct"
    svc.sends.enqueue("browsing", identity, direct=direct)


async def send_automatic(svc, item):
    """Dispatch one queued automatic or uncertain item across browsing/messages/dynamics."""
    identity, channel = item["source_id"], item["channel"]
    control = svc.operations.policy()
    if not svc.operations.accepting_work:
        return False
    if item["approval"]:
        return await svc._send_approved(item)
    if item["epoch"] != control["epoch"] and channel != "dynamics":
        return True
    draft = svc._send_draft(item)
    if item["epoch"] != control["epoch"] and not (
        channel == "dynamics" and draft and draft["status"] == "publish_uncertain"
    ):
        return True
    if channel == "browsing":
        candidate = draft if draft and draft["auto_authorized"] else None
        if candidate and candidate["status"] == "publish_uncertain":
            if not await svc.publication._reconcile(identity):
                raise ReconciliationPending()
        elif (
            candidate
            and candidate["status"] == "pending_review"
            and svc.publication._action_allowed(candidate["kind"], svc.effective())
        ):
            if (
                item["reaction"]
                and candidate["kind"] != "video_fav"
                and candidate["created_at"] <= time.time() - 72 * 3600
            ):
                svc.store.execute(
                    "UPDATE browsing_intents SET status='archived',auto_authorized=0 WHERE id=?",
                    (identity,),
                )
                return True
            origin = svc.repo.round(candidate["round_id"])
            if not origin or origin["preview"] or origin["status"] in {"cancelled", "invalid"}:
                return True
            svc.store.execute("UPDATE browsing_intents SET epoch=? WHERE id=?", (control["epoch"], identity))
            result = await svc.publish(identity, automatic=True)
            if result and result.get("status") == "publish_uncertain":
                raise ReconciliationPending()
    elif channel == "messages":
        if draft and draft["status"] == "publish_uncertain":
            if not await svc.messages._reconcile(int(identity)):
                raise ReconciliationPending()
        elif draft and draft["status"] == "pending_review" and svc.messages.policy()["auto_send"]:
            svc.messages.authorizations[int(identity)] = control["epoch"]
            await svc.messages.send(int(identity), automatic=True, authorization_epoch=control["epoch"])
    elif channel == "dynamics":
        post = draft
        if post and post["status"] == "publish_uncertain":
            if not await svc.dynamics._reconcile(post.get("duplicate_of") or post["id"]):
                raise ReconciliationPending()
        elif post and post["status"] == "pending_review" and svc.dynamics.policy()["auto_publish"]:
            svc.dynamics._automatic_post_id = post["id"]
            try:
                published = await svc.dynamics.publish(post["id"], automatic=True)
                if published.get("status") == "publish_uncertain":
                    raise ReconciliationPending()
            finally:
                svc.dynamics._automatic_post_id = None
    return True


async def send_approved(svc, item):
    """Dispatch one manually approved queued send or reconcile its `publish_uncertain` receipt."""
    identity, channel = item["source_id"], item["channel"]
    draft = svc._send_draft(item)
    if not draft:
        return True
    service = (
        svc.comments
        if channel == "comments"
        else svc.messages
        if channel == "messages"
        else svc.dynamics
        if channel == "dynamics"
        else svc.publication
    )
    target_id = int(identity) if channel == "messages" else identity
    if draft["status"] == "publish_uncertain":
        if not await service._reconcile(target_id):
            raise ReconciliationPending()
        return True
    if draft["status"] != "approved":
        return True
    svc.sends.assert_approval(channel, identity, draft, comment_payload(draft, svc.runtime, channel))
    if due := svc.comment_window_due():
        raise SendLimitReached("评论每小时发送额度已用完", due)
    await service.publish(target_id) if channel != "messages" else await svc.messages.send(target_id)
    return True


def resume_reactions(svc):
    """Re-enqueue at most one pending auto-authorized reaction intent if none is active."""
    if not svc.operations.accepting_work:
        return
    scope = svc.operation_scope()
    if svc.store.one(
        "SELECT 1 FROM operation_send_items WHERE account_id=? AND character_id=? "
        "AND character_version=? AND reaction=1 AND blocked IS NOT 'reconciliation' LIMIT 1",
        scope,
    ):
        return
    current = svc.effective()
    for kind in ("comment_like", "video_like", "dynamic_like"):
        if not svc.publication._action_allowed(kind, current):
            continue
        item = svc.store.one(
            """SELECT i.id FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id
            WHERE i.account_id=? AND i.character_id=? AND i.character_version=? AND i.kind=? AND i.auto_authorized=1
              AND i.status='pending_review' AND i.created_at>? AND r.preview=0 AND r.status NOT IN ('cancelled','invalid')
              AND json_extract(r.config,'$.review_session_id') IS NULL
              AND NOT EXISTS(SELECT 1 FROM account_writes w WHERE w.account_id=i.account_id AND w.intent=i.source_key)
            ORDER BY i.created_at,i.id LIMIT 1""",
            (*scope, kind, time.time() - 72 * 3600),
        )
        if item:
            svc.sends.enqueue("browsing", item["id"])
            return


def resume_publication(svc, row, intent, target):
    """Check send admission slot and enqueue `intent` for background settlement."""
    if svc.store.one(
        "SELECT 1 FROM operation_send_queue WHERE channel='browsing' AND source_id=?",
        (intent["id"],),
    ):
        return
    direct = bool(row["config"].get("interaction_only") or intent.get("source_pipeline") == "inbox")
    with svc.comment_admission.slot(
        row, intent["kind"], target.get("bvid"), target.get("recipient"), direct=direct
    ) as blocked:
        if blocked:
            raise blocked
        svc.queue_publication(intent["id"], direct=direct)
