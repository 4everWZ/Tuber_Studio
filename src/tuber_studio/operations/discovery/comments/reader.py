"""Discovery video comment reader and comment-visit evaluator."""

import asyncio
import json

from ....core.errors import ContextWindowExceeded, SendLimitReached
from ....core.meter import BrowsingBudgetDeferred
from ....infra.inference.providers import public_error
from ...kernel.quotas import DailyReplyLimitReached, day_window
from .cursor import _fetch_comment_context as video_comment_context
from .cursor import (
    admit_comment_page,
    comment_cursor_key,
    commit_comment_context,
    enqueue_comment_visit,
    read_old_comment_page,
    resume_comment_page,
    round_comment_page,
)
from .timeline import merge_continuous_comments, observe_thread, source_discussion


async def evaluate_comment_visit(browsing, result, row, metrics):
    """Browse a video's discussions without starting or updating its viewing task."""
    identity, metadata = result["id"], result["metadata"]
    material, key = result["material"], result["material_key"]
    payload = json.loads(browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"])
    if explanation := (payload.get("selection") or {}).get("explanation"):
        payload["reason"] = explanation
    status = "comments_checked"
    try:
        browsing._check_round(row)
        context = await browsing.video_comment_context(row, metadata["bvid"])
        items = context.get("items", [])
        if not row["preview"] and (harvester := getattr(browsing.runtime, "meme_harvester", None)):
            try:
                harvester.enqueue_comment_attachments(items, bvid=metadata.get("bvid"))
            except Exception:
                pass
        if (row["config"].get("interaction_only") or row["config"].get("autonomous_batch")) and not row["preview"]:
            with browsing.store.db:
                admit_comment_page(browsing, row, identity, metadata, items)
            browsing.repo.complete(identity, result["owner"], result["generation"], status, payload, key)
            browsing.commit_comment_context(context)
            return
        with browsing.store.db:
            observe_thread(
                browsing.store,
                row,
                metadata["bvid"],
                items,
                via="comment_visit",
                resource_author_uid=metadata.get("owner_uid"),
            )
        chosen = {segment["id"] for segment in browsing._segments(material)} if material else set()
        for item in merge_continuous_comments(items):
            if browsing.comment_admission.review_reply_limit_reached(row, metadata["bvid"]):
                raise DailyReplyLimitReached(day_window(row["config"].get("business_timezone", "Asia/Taipei")))
            intent = await browsing.decisions._reply_to_video_comment(
                row,
                material,
                key,
                chosen,
                item,
                items,
                metrics,
                diagnostics=payload.setdefault("repetition_decisions", []),
                metadata=metadata,
            )
            if intent:
                payload.setdefault("reply_intent_ids", []).append(intent["id"])
                status = "selected"
            if not payload.get("reason"):
                stored = browsing.store.one(
                    "SELECT decision FROM browsing_comment_progress "
                    "WHERE account_id=? AND character_id=? AND bvid=? AND comment_id=?",
                    (row["account_id"], row["character_id"], metadata["bvid"], str(item["comment_id"])),
                )
                if stored and (explanation := json.loads(stored["decision"]).get("explanation")):
                    payload["reason"] = explanation
        browsing.repo.complete(identity, result["owner"], result["generation"], status, payload, key)
        browsing.commit_comment_context(context)
    except (DailyReplyLimitReached, SendLimitReached) as error:
        browsing.repo.complete(
            identity,
            result["owner"],
            result["generation"],
            "deferred_daily",
            payload
            | {
                "retry_kind": "send_limit" if isinstance(error, SendLimitReached) else "daily_reply_limit",
                "next_at": error.next_at,
            },
            key,
        )
    except Exception as error:
        metrics["failures"] += 1
        continuation = getattr(error, "continuation", None)
        deferred = isinstance(error, BrowsingBudgetDeferred)
        if continuation:
            browsing.runtime.continuations.bind_source_view(
                continuation["id"], "browsing", identity, "评论回应生成记录 · " + metadata["bvid"]
            )
        browsing.repo.complete(
            identity,
            result["owner"],
            result["generation"],
            "invalid"
            if continuation and continuation["state"] == "invalid"
            else "checkpoint"
            if continuation or deferred or isinstance(error, ContextWindowExceeded)
            else "failed",
            payload
            | {
                **({"generation_continuation_id": continuation["id"]} if continuation else {}),
                **({"retry_kind": "round_budget"} if deferred else {}),
                **(
                    {"capacity_signature": browsing.decisions._capacity_signature(row["config"])}
                    if isinstance(error, ContextWindowExceeded)
                    else {}
                ),
            },
            key,
            public_error(error),
        )
    finally:
        result["heartbeat"].cancel()
        await asyncio.gather(result["heartbeat"], return_exceptions=True)


__all__ = [
    "admit_comment_page",
    "comment_cursor_key",
    "commit_comment_context",
    "enqueue_comment_visit",
    "evaluate_comment_visit",
    "merge_continuous_comments",
    "read_old_comment_page",
    "resume_comment_page",
    "round_comment_page",
    "source_discussion",
    "video_comment_context",
]
