"""Pipeline B runner: independent Inbox Responder (`comment_reply`, `mention`, `own_content`, `dm`)."""

import asyncio
from contextlib import suppress
from contextvars import ContextVar

from ...core.turns import operation_checkpoint
from ...infra.platforms.bilibili.client import BilibiliAccountExpired
from . import comment_events as inbox_comment_events
from .dm_bridge import DMBridge

INBOX_RUNNER_ACTIVE: ContextVar[bool] = ContextVar("inbox_runner_active", default=False)


class InboxRunner:
    """Independent background coordinator driving Pipeline B (survives Discovery 412 & standalone 24h runs)."""

    def __init__(self, browsing, *, dm_bridge: DMBridge | None = None):
        self.browsing = browsing
        self.dm_bridge = dm_bridge if dm_bridge is not None else DMBridge(browsing)
        self._poller_task: asyncio.Task | None = None
        self._interaction_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return bool(
            (self._poller_task and not self._poller_task.done())
            or (self._interaction_task and not self._interaction_task.done())
        )

    def start(self) -> None:
        if not self._poller_task or self._poller_task.done():
            self._poller_task = asyncio.create_task(self.notifications_loop())
        if not self._interaction_task or self._interaction_task.done():
            self._interaction_task = asyncio.create_task(self.interaction_loop())

    async def stop(self) -> None:
        tasks = [task for task in (self._poller_task, self._interaction_task) if task is not None]
        self._poller_task = None
        self._interaction_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _has_publish_uncertain(self) -> bool:
        return bool(self.browsing.has_uncertain_writes())

    def _pending_events_exist(self) -> bool:
        account_id = str(self.browsing.account or "")
        if not account_id:
            return False
        return bool(
            self.browsing.store.one(
                "SELECT 1 FROM browsing_events WHERE account_id=? AND kind<>'comment_visit' "
                "AND status IN ('received','source_verified','context_ready','reply_deferred') LIMIT 1",
                (account_id,),
            )
        )

    async def poll_notifications_once(self) -> float:
        """Poll notifications, own content comments, and DMs under independent Inbox gate (tolerates Discovery 412)."""
        browsing = self.browsing
        delay = max(1, browsing.effective().get("notification_interval_seconds", 60))
        if not browsing.operations.accepting_work or not browsing.inbox_enabled():
            return float(delay)
        if not browsing.account:
            return float(delay)
        if self._has_publish_uncertain():
            return min(float(delay), 10.0)
        had_pending_before = self._pending_events_exist()
        dm_result = {"polled": 0}
        try:
            async with browsing.operations.automatic_work() as admitted:
                if admitted:
                    character = browsing.runtime.character
                    scope = {
                        "account_id": browsing.account,
                        "preview": False,
                        "character_id": character["id"],
                        "character_version": character["version"],
                        "epoch": browsing.operations.policy()["epoch"],
                        "config": browsing.effective(),
                    }

                    def verify_poll_scope():
                        browsing.check_step(scope)
                        browsing._check_round(scope)

                    checkpoint = operation_checkpoint.set(verify_poll_scope)
                    try:
                        await inbox_comment_events.run_interactions(browsing, scope, {"failures": 0})
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling() or not browsing.operations.draining:
                            raise
                    finally:
                        operation_checkpoint.reset(checkpoint)
            dm_result = await self.dm_bridge.sync_once()
        except BilibiliAccountExpired:
            await browsing.handle_auth_loss()
            return float(delay)
        except Exception:
            pass
        report = getattr(browsing.notifications, "last", {}) or {}
        if report.get("released") or dm_result.get("polled") or (not had_pending_before and self._pending_events_exist()):
            browsing.interactions_changed.set()
        if not report.get("complete", True):
            return min(float(delay), 1.0)
        return float(delay)

    async def process_interactions_once(self) -> float:
        """Run one local Inbox interaction pass without Discovery batches or `browsing_watch_starts`."""
        browsing = self.browsing
        delay = max(1, browsing.effective().get("notification_interval_seconds", 60))
        if (
            not browsing.operations.accepting_work
            or not browsing.inbox_enabled()
            or not browsing.account
            or browsing.operations.draining
        ):
            return float(delay)
        if self._has_publish_uncertain():
            return min(float(delay), 10.0)
        if browsing.has_interactions() or self._pending_events_exist():
            with suppress(Exception):
                token = INBOX_RUNNER_ACTIVE.set(True)
                try:
                    await browsing.run(automatic=True, interaction_only=True, local_interactions=True)
                finally:
                    INBOX_RUNNER_ACTIVE.reset(token)
            with suppress(Exception):
                await self.dm_bridge.sync_once()
        return float(delay)

    async def notifications_loop(self) -> None:
        while not self.browsing.closed:
            delay = await self.poll_notifications_once()
            if self.browsing.closed:
                return
            self.browsing.notification_schedule.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.browsing.notification_schedule.wait(), timeout=delay)

    async def interaction_loop(self) -> None:
        while not self.browsing.closed:
            delay = await self.process_interactions_once()
            if self.browsing.closed:
                return
            self.browsing.interactions_changed.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.browsing.interactions_changed.wait(), timeout=delay)
