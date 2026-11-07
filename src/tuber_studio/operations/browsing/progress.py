"""Prepare and resume viewing material under durable task leases."""

import asyncio
import json
from uuid import uuid4

from ...generation_meter import BrowsingBudgetDeferred
from ...providers import public_error
from .evidence import EvidenceUnavailable
from .round_context import agent_depth, read_round


async def prepare_evidence(
    browsing,
    video,
    config,
    *,
    force=False,
    task=None,
    deadline=None,
    require_visual=False,
    visual_source_ids=(),
):
    """Keep visual generation and token probes inside their parent round."""
    identity = read_round.get()
    if identity:
        task = dict(task or {})
        if task.get("round_id", identity) != identity:
            raise ValueError("视觉任务不能跨越父轮预算")
        task.setdefault("id", str(uuid4()))
        task["round_id"] = identity
        row = browsing.repo.round(identity)
        if not row:
            raise ValueError("视觉任务的父轮已不存在")
        if row["deadline"] is not None:
            deadline = min(deadline or row["deadline"], row["deadline"])
    return await browsing.evidence.prepare(
        video,
        config,
        force=force,
        task=task,
        deadline=deadline,
        require_visual=require_visual,
        visual_source_ids=visual_source_ids,
    )


async def prepare_task(browsing, identity, row, semaphore, metrics):
    current_depth = agent_depth.get()
    if current_depth >= 1:
        raise PermissionError("Subagent nesting prohibited; depth must not exceed 1")
    token = agent_depth.set(current_depth + 1)
    try:
        for attempt in range(row["config"]["max_takeovers"] + 1):
            owner = str(uuid4())
            try:
                return await browsing._prepare_attempt(identity, row, semaphore, metrics, owner)
            except Exception as error:
                task = browsing.store.one("SELECT * FROM browsing_tasks WHERE id=?", (identity,))
                if (
                    not task
                    or task["owner"] != owner
                    or browsing.repo.round(row["id"])["status"] not in {"queued", "running"}
                ):
                    metrics["late_results_rejected"] = metrics.get("late_results_rejected", 0) + 1
                    return None
                if attempt < row["config"]["max_takeovers"]:
                    browsing.store.execute(
                        "UPDATE browsing_tasks SET lease_until=0 WHERE id=? AND owner=? AND generation=?",
                        (identity, owner, task["generation"]),
                    )
                    metrics["takeovers"] = metrics.get("takeovers", 0) + 1
                    continue
                metrics["failures"] += 1
                browsing.repo.complete(
                    identity,
                    owner,
                    task["generation"],
                    "failed",
                    json.loads(task["payload"]) | {"reason": "worker_failed"},
                    error=public_error(error),
                )
                return None
    finally:
        agent_depth.reset(token)


async def prepare_attempt(browsing, identity, row, semaphore, metrics, owner):
    async with semaphore:
        generation = browsing.repo.claim(identity, owner)
        heartbeat = asyncio.create_task(browsing._heartbeat(identity, owner, generation), name=row["id"])
        browsing.heartbeats.add(heartbeat)
        heartbeat.add_done_callback(browsing.heartbeats.discard)
        ready = False
        try:
            item = json.loads(
                browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
            )
            progress = browsing.repo.content_progress(row, item["bvid"])
            if (
                not progress
                and not row["preview"]
                and not browsing.repo.quotas.start_watch(
                    identity,
                    row["account_id"],
                    row["character_id"],
                    item["bvid"],
                    row["config"].get("daily_watch_limit", 0),
                    row["config"].get("business_timezone", "Asia/Taipei"),
                )
            ):
                daily = browsing.repo.quotas.watch_status(
                    row["account_id"],
                    row["character_id"],
                    row["config"].get("daily_watch_limit", 0),
                    row["config"].get("business_timezone", "Asia/Taipei"),
                )
                browsing.repo.complete(
                    identity,
                    owner,
                    generation,
                    "deferred_daily",
                    item | {"reason": "daily_watch_limit", "next_at": daily["next_at"]},
                )
                return None
            if progress and not row["config"].get("force_read"):
                material_key, material = await browsing.evidence._store(progress["material"])
                browsing.evidence.hits += 1
            else:
                material_key, material = await browsing._prepare_evidence(
                    item["bvid"],
                    row["config"],
                    task={"id": identity},
                    deadline=row["deadline"],
                    force=row["config"].get("force_read", False),
                )
            # 原字幕或已绑定的真实帧观察均可作为材料。
            has_evidence = any(isinstance(p, dict) and p.get("segments") for p in material.get("parts", []))
            if not has_evidence:
                raise EvidenceUnavailable("no_subtitles", "无可用字幕或画面材料")
            reason = browsing.filter_reason(material["metadata"], row["config"])
            if reason:
                raise EvidenceUnavailable("filtered", reason)
            if not progress or row["config"].get("force_read"):
                browsing.repo.save_content_progress(row, material, [])
            browsing.repo.complete(
                identity,
                owner,
                generation,
                "evidence_ready",
                item
                | {
                    "metadata": browsing.decisions._metadata_summary(material["metadata"]),
                    "coverage": material["coverage"],
                    "missing_parts": material["missing_parts"],
                },
                material_key,
            )
            metrics["prepared"] += 1
            ready = True
            return {
                "id": identity,
                "owner": owner,
                "generation": generation,
                "material_key": material_key,
                "material": material,
                "heartbeat": heartbeat,
                "task_kind": item.get("task_kind", "comment_visit" if progress else "new_video"),
            }
        except ValueError as error:
            kind = getattr(error, "kind", "")
            deferred = isinstance(error, BrowsingBudgetDeferred)
            metrics["failures"] += int(
                kind not in {"no_subtitles", "empty", "filtered", "visual_unavailable"}
            )
            browsing.repo.complete(
                identity,
                owner,
                generation,
                "checkpoint" if deferred else "skipped" if kind else "failed",
                item
                | {"reason": kind or "preparation_failed"}
                | ({"retry_kind": "round_budget"} if deferred else {}),
                error=public_error(error),
            )
            return None
        finally:
            if not ready:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)


async def heartbeat(browsing, identity, owner, generation):
    while True:
        await asyncio.sleep(5)
        browsing.repo.heartbeat(identity, owner, generation)
