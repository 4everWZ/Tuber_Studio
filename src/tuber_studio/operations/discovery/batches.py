"""Discovery batch lifecycle execution, worker limits, and round orchestration."""

import asyncio
import json
import time
from contextlib import nullcontext

import psutil

from ...core.errors import SendLimitReached
from ...core.meter import BrowsingBudgetDeferred
from ...core.store import dump
from ...core.turns import operation_checkpoint, operation_lane
from ...infra.inference.providers import public_error
from ..kernel.context import read_round
from ..kernel.quotas import DailyReplyLimitReached
from . import full_video
from .repository import BrowsingBatchStore


class BrowsingBatches(BrowsingBatchStore):
    """Durable batch ledger with discovery execution helpers."""


async def execute(browsing, row, notifications_only):
    identity, started = row["id"], time.monotonic()
    token = read_round.set(identity)
    lane = operation_lane.set("priority" if row["config"].get("full_video") else "background")
    checkpoint = operation_checkpoint.set(lambda: browsing.check_step(row))
    metrics = {
        "workers": row["config"]["workers"],
        "prepared": 0,
        "failures": 0,
        "viewed": 0,
        "lock_wait_seconds": 0,
        "preparation_seconds": 0,
        "peak_rss_bytes": psutil.Process().memory_info().rss,
        "cache_hits_before": browsing.evidence.hits,
        "merged_before": browsing.evidence.merged,
    }
    browsing.store.execute("UPDATE browsing_rounds SET status='running' WHERE id=?", (identity,))
    round_error = None
    sampler = asyncio.create_task(browsing._sample_resources(metrics))
    try:
        async with asyncio.timeout_at(
            asyncio.get_running_loop().time() + max(0, row["deadline"] - time.time())
            if row["deadline"] is not None
            else None
        ):
            browsing._check_round(row)
            if row["config"].get("full_video"):
                status = await full_video.execute(browsing, row, metrics)
                return
            if row["config"].get("interaction_only"):
                await browsing._run_interactions(row, metrics)
                status = "partial" if metrics["failures"] else "completed"
                return
            video_only = bool(row["config"].get("video_only"))
            poll_interactions = not video_only and (
                not row["preview"] or notifications_only or not browsing.operations.policy()["enabled"]
            )
            if poll_interactions and not row["config"].get("autonomous_batch"):
                await browsing.sync_notifications(row)
                await browsing._process_events(row, metrics)
            if not notifications_only or browsing.store.get(
                "browsing_discovery_pending:" + row["account_id"], False
            ):
                browsing.store.set("browsing_discovery_pending:" + row["account_id"], False)
                candidates = (
                    await browsing._read_proactively(browsing._discover, row)
                    if row["config"].get("autonomous_batch") and not row["config"].get("review_session_id")
                    else await browsing._discover(row)
                )
                metrics["workers"] = row["config"].get("batch_workers", row["config"]["workers"])
                semaphore = asyncio.Semaphore(row["config"].get("batch_workers", row["config"]["workers"]))
                if row["config"].get("autonomous_batch"):
                    async with asyncio.TaskGroup() as group:
                        for candidate in candidates:
                            group.create_task(
                                browsing._process_discovery_video(row, candidate, metrics, semaphore)
                            )
                    browsing.check_step(row)
                    while browsing.repo.batches.pending_comment_events(row):
                        browsing.event_changed.clear()
                        if not await browsing._process_events(row, metrics, batch_only=True):
                            browsing.check_step(row)
                            if not browsing.event_active:
                                break
                            await browsing.event_changed.wait()
                        browsing.check_step(row)
                    status = "partial" if metrics["failures"] else "completed"
                    return
                tasks = [browsing.repo.task(identity, c) for c in candidates]
                preparation_start = time.monotonic()
                async with asyncio.TaskGroup() as group:
                    children = [
                        group.create_task(browsing._prepare_task(i, row, semaphore, metrics)) for i in tasks
                    ]
                prepared = [child.result() for child in children]
                metrics["preparation_seconds"] = time.monotonic() - preparation_start
                for result in prepared:
                    if poll_interactions:
                        await browsing._process_events(row, metrics)
                    if result:
                        await browsing.decisions._evaluate_task(result, row, metrics)
                if poll_interactions:
                    await browsing._process_events(row, metrics)
            status = "partial" if metrics["failures"] else "completed"
    except (asyncio.CancelledError, Exception) as error:
        stopping = not row["preview"] and not browsing.operations.policy()["enabled"]
        if isinstance(error, asyncio.CancelledError) or stopping:
            status = (
                "paused"
                if browsing.operations.draining and not browsing.operations.policy()["emergency"]
                else "cancelled"
            )
            task_error = "运营已收尾，保留进度" if status == "paused" else "父轮取消"
        elif isinstance(error, SendLimitReached):
            status = "paused"
            task_error = public_error(error)
        else:
            continuation = getattr(error, "continuation", None)
            status = (
                "invalid"
                if continuation and continuation["state"] == "invalid"
                else "partial"
                if metrics["viewed"] or metrics["prepared"]
                else "failed"
            )
            browsing.last_error = round_error = task_error = public_error(error)
        browsing.store.execute(
            "UPDATE browsing_tasks SET status=?,error=? WHERE round_id=? "
            "AND status IN ('discovered','claimed','preparing','evidence_ready')",
            ("paused" if status == "paused" else "cancelled", task_error, identity),
        )
    finally:
        browsing.comment_windows.pop(identity, None)
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        heartbeats = [task for task in browsing.heartbeats if task.get_name() == identity]
        for heartbeat in heartbeats:
            heartbeat.cancel()
        await asyncio.gather(*heartbeats, return_exceptions=True)
        browsing.heartbeats.difference_update(heartbeats)
        metrics["elapsed_seconds"] = time.monotonic() - started
        metrics["cache_hits"] = browsing.evidence.hits - metrics.pop("cache_hits_before")
        metrics["merged_reads"] = browsing.evidence.merged - metrics.pop("merged_before")
        metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
        metrics = browsing.repo.round(identity)["metrics"] | metrics
        browsing.repo.finish_round(identity, status, metrics, round_error)
        if row["config"].get("batch_id"):
            browsing.interactions_changed.set()
            browsing.changed.set()
        operation_checkpoint.reset(checkpoint)
        operation_lane.reset(lane)
        read_round.reset(token)


async def sample_resources(browsing, metrics):
    while True:
        metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], psutil.Process().memory_info().rss)
        used = psutil.virtual_memory().used
        metrics["peak_system_ram_bytes"] = max(metrics.get("peak_system_ram_bytes", 0), used)
        await asyncio.sleep(0.1)


async def resume_batch_publications(browsing, row, bvid):
    for item in browsing.store.all(
        "SELECT id,status,kind,target FROM browsing_intents WHERE account_id=? AND character_id=? "
        "AND json_extract(target,'$.bvid')=? AND (status='publish_uncertain' OR "
        "(round_id=? AND character_version=? AND epoch=? AND status='pending_review' AND auto_authorized=1)) "
        "ORDER BY created_at",
        (row["account_id"], row["character_id"], bvid, row["id"], row["character_version"], row["epoch"]),
    ):
        if item["status"] == "publish_uncertain":
            try:
                await browsing.publication._reconcile(item["id"])
            except Exception as error:
                browsing.store.execute(
                    "UPDATE browsing_intents SET error=? WHERE id=? AND status='publish_uncertain'",
                    (public_error(error), item["id"]),
                )
        elif browsing.publication._action_allowed(item["kind"], browsing.effective()):
            browsing.resume_publication(row, item, json.loads(item["target"]))


async def process_discovery_video(browsing, row, candidate, metrics, semaphore):
    revisit = candidate.get("task_kind") == "comment_visit" or (
        "task_kind" not in candidate and bool(browsing.repo.content_progress(row, candidate["bvid"]))
    )
    window = (
        browsing.comment_windows.setdefault(row["id"], asyncio.Semaphore(row["config"]["workers"]))
        if revisit
        else semaphore
    )
    async with window:
        browsing.check_step(row)
        lane = operation_lane.set("background")
        try:
            await browsing._run_discovery_video(row, candidate, metrics)
        finally:
            operation_lane.reset(lane)


async def process_comment_visits(browsing, row, metrics):
    if not (row["config"]["reply_enabled"] or row["config"].get("comment_like_enabled")):
        return

    async def visit(candidate, key):
        try:
            await browsing._process_discovery_video(row, candidate, metrics, asyncio.Semaphore(1))
        finally:
            browsing.comment_active.pop(key, None)

    for candidate in browsing.repo.batches.pending_comment_visits(row):
        browsing.check_step(row)
        if not browsing.page_capacity():
            break
        key = (
            row["account_id"],
            row["character_id"],
            row["character_version"],
            row["config"].get("review_session_id", ""),
            candidate["bvid"],
        )
        if key not in browsing.comment_active:
            task = asyncio.create_task(visit(candidate, key))
            browsing.comment_active[key] = task
            await task
            break


async def run_discovery_video(browsing, row, candidate, metrics):
    """Execute one queue attempt; a later round may resume interrupted work."""
    bvid = candidate["bvid"]
    comment_batch_ids = candidate.get("comment_batch_ids")
    saved = None if comment_batch_ids else browsing.repo.video_retry(row, bvid)
    failures = saved["failures"] if saved else 0
    if failures >= 3:
        return
    browsing._check_round(row)
    identity = browsing.repo.task(row["id"], candidate)
    if comment_batch_ids:
        browsing.repo.batches.task(row, bvid, identity, batch_ids=comment_batch_ids)
    try:
        if failures or saved or row["config"].get("batch_id") or comment_batch_ids:
            await browsing._resume_batch_publications(row, bvid)
    except (DailyReplyLimitReached, SendLimitReached) as failure:
        payload = json.loads(
            browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
        ) | {
            "retry_kind": "send_limit" if isinstance(failure, SendLimitReached) else "daily_reply_limit",
            "next_at": failure.next_at,
        }
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='deferred_daily',payload=?,error=NULL,updated_at=? WHERE id=?",
            (dump(payload), time.time(), identity),
        )
        browsing.repo.batches.outcome(identity, "deferred_daily", payload=payload)
        return
    except BrowsingBudgetDeferred as failure:
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='checkpoint',error=? WHERE id=?",
            (public_error(failure), identity),
        )
        if comment_batch_ids:
            browsing.repo.batches.outcome(identity, "checkpoint")
        else:
            browsing.repo.defer_video(row, bvid, identity, failures)
        return
    except Exception as failure:
        metrics["failures"] += 1
        browsing.store.execute(
            "UPDATE browsing_tasks SET status='failed',error=? WHERE id=?",
            (public_error(failure), identity),
        )
    else:
        prepared = await browsing._prepare_task(identity, row, nullcontext(), metrics)
        if prepared:
            await browsing.decisions._evaluate_task(prepared, row, metrics)
    browsing._check_round(row)
    task = browsing.store.one("SELECT status,error,payload FROM browsing_tasks WHERE id=?", (identity,))
    payload = json.loads(task["payload"])
    reason = payload.get("reason")
    if comment_batch_ids:
        browsing.repo.batches.outcome(identity, task["status"], task["error"], payload)
        return
    if payload.get("retry_kind") == "round_budget":
        browsing.repo.defer_video(row, bvid, identity, failures)
        return
    if task["status"] in {"checkpoint", "invalid", "awaiting_review"}:
        browsing.repo.finish_video_attempt(row, candidate, identity, 0)
        return
    if task["status"] == "deferred_daily":
        return
    if payload.get("material_mode") == "unavailable":
        browsing.repo.finish_video_attempt(row, candidate, identity, failures + 1)
        return
    if task["status"] in {"selected", "viewed_without_comment", "comments_checked"} or (
        task["status"] == "skipped" and reason in {"no_subtitles", "empty", "filtered", "visual_unavailable"}
    ):
        browsing.repo.finish_video_attempt(row, candidate, identity, 0)
        return
    failures += 1
    error = task["error"]
    browsing.repo.finish_video_attempt(row, candidate, identity, failures)
    if failures == 3:
        browsing.store.execute(
            "UPDATE browsing_intents SET auto_authorized=0,error=? WHERE round_id=? "
            "AND json_extract(target,'$.bvid')=? AND status='pending_review'",
            (error, row["id"], bvid),
        )
