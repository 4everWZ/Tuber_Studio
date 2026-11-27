"""Pipeline A runner: independent autonomous Discovery loop with 412 cooldown isolation."""

import asyncio
import time
from contextlib import suppress

from ...infra.inference.providers import public_error


class DiscoveryRunner:
    """Independent background loop driving Pipeline A (active discovery & video rounds)."""

    def __init__(self, browsing):
        self.browsing = browsing
        self._task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _active_review_session(self) -> dict | None:
        review = self.browsing.store.get("browsing_review_session") or {}
        if review.get("state") in {"starting", "running"}:
            return review
        return None

    async def run_once(self) -> float:
        """Execute at most one Discovery step and return recommended wait seconds."""
        browsing = self.browsing
        delay = max(1, browsing.effective().get("interval_seconds", 60))
        if await browsing._start_full_video():
            return 0.2
        if browsing.operations.accepting_work and browsing.discovery.enabled and not browsing.discovery_tasks():
            if browsing.has_uncertain_writes():
                return min(float(delay), 10.0)
            review = self._active_review_session()
            account_id = browsing.account or browsing.discovery.history_scope()[0] or ""
            waiting = browsing.waiting_round()
            if waiting:
                delay = min(delay, 2.0)
            if not browsing.read_gate.allows("discovery", account_id=account_id, reconciliation=False):
                backoff = browsing.read_gate.discovery_backoff(account_id)
                remaining = max(0.5, float(backoff["until"]) - time.time()) if backoff else float(delay)
                browsing.discovery.attention = {
                    "type": "read_cooldown",
                    "summary": "发现流水线触发平台风控冷却，已暂停自动探索（消息响应不受影响）",
                    "action": "discovery",
                }
                return max(0.5, min(float(delay), remaining))
            try:
                await browsing.run_discovery_revisits_once()
                if not browsing.operations.accepting_work or not browsing.discovery.enabled:
                    return float(delay)
                result = await browsing.run(automatic=True, video_only=True)
            except Exception as error:
                if browsing.closed or not browsing.operations.accepting_work:
                    return float(delay)
                message = public_error(error)
                browsing.discovery.error = message
                browsing.discovery.attention = {
                    "type": "failure",
                    "summary": f"本轮自动探索失败：{message}",
                    "action": "discovery",
                }
                if review:
                    browsing.store.set("browsing_review_session", review | {"state": "stopped", "error": message})
                    await browsing.stop()
            else:
                if result.get("status") != "deferred":
                    browsing.discovery.error = None
                    browsing.discovery.attention = None
                elif result.get("next_at"):
                    delay = max(1, min(delay, result["next_at"] - time.time()))
                elif result.get("reason") != "OC有界延后计划尚未到期":
                    delay = min(delay, 30)
                else:
                    next_at = browsing.store.get(
                        f"browsing_next:{browsing.account}:{browsing.runtime.character['id']}",
                        0,
                    )
                    if next_at:
                        delay = max(1, min(delay, next_at - time.time()))
        return float(delay)

    async def loop(self) -> None:
        while not self.browsing.closed:
            delay = await self.run_once()
            if self.browsing.closed:
                return
            self.browsing.changed.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.browsing.changed.wait(), timeout=delay)
