"""Deduplicated, cooldown-governed event bus unifying background ticks and research hooks."""

from __future__ import annotations

import inspect
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from ...core.models import StrictModel, content_hash

HeartbeatEventKind = Literal[
    "tick",
    "discovery_health",
    "inbox_health",
    "dm_health",
    "dynamics_health",
    "hot_topic",
    "curiosity_research",
    "video_candidate",
    "custom",
]
HeartbeatSeverity = Literal["info", "attention", "urgent"]


class HeartbeatEvent(StrictModel):
    """Normalized signal published onto the always-on AgentEventBus."""

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=120)
    kind: HeartbeatEventKind
    source: str = Field(min_length=1, max_length=80)
    dedup_key: str = Field(min_length=1, max_length=240)
    severity: HeartbeatSeverity = "info"
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=800)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


class BusEmissionResult(StrictModel):
    """Outcome of publishing an event onto AgentEventBus."""

    accepted: bool
    reason: Literal["emitted", "deduplicated", "cooldown"]
    event: HeartbeatEvent
    handler_errors: list[str] = Field(default_factory=list)


class AgentEventBus:
    """In-process deduplicated and cooldown-governed event bus for always-on agent signals."""

    def __init__(
        self,
        *,
        default_cooldown_seconds: float = 900.0,
        dedup_window_seconds: float = 3600.0,
        max_history: int = 200,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.default_cooldown_seconds = max(0.0, float(default_cooldown_seconds))
        self.dedup_window_seconds = max(0.0, float(dedup_window_seconds))
        self._clock = clock or time.time
        self._history: deque[HeartbeatEvent] = deque(maxlen=max(10, int(max_history)))
        self._last_by_key: dict[str, tuple[float, str]] = {}
        self._subscribers: dict[str, list[Callable[[HeartbeatEvent], Any]]] = {}

    def subscribe(self, kind: str, handler: Callable[[HeartbeatEvent], Any]) -> Callable[[], None]:
        """Subscribe a sync or async handler to a specific event kind or `*`."""
        bucket = self._subscribers.setdefault(kind, [])
        if handler not in bucket:
            bucket.append(handler)
        return lambda: self.unsubscribe(kind, handler)

    def unsubscribe(self, kind: str, handler: Callable[[HeartbeatEvent], Any]) -> None:
        """Remove a previously registered handler."""
        bucket = self._subscribers.get(kind)
        if not bucket:
            return
        while handler in bucket:
            bucket.remove(handler)
        if not bucket:
            self._subscribers.pop(kind, None)

    @staticmethod
    def _fingerprint(event: HeartbeatEvent) -> str:
        return content_hash(
            {
                "kind": event.kind,
                "severity": event.severity,
                "title": event.title,
                "summary": event.summary,
                "payload": event.payload,
            }
        )

    async def publish(
        self,
        event: HeartbeatEvent,
        *,
        cooldown_seconds: float | None = None,
        bypass_cooldown: bool = False,
    ) -> BusEmissionResult:
        """Publish an event if it passes deduplication and cooldown checks."""
        now = float(self._clock())
        digest = self._fingerprint(event)
        previous = self._last_by_key.get(event.dedup_key)
        if previous is not None and not bypass_cooldown:
            last_at, last_digest = previous
            elapsed = now - last_at
            if last_digest == digest and elapsed < self.dedup_window_seconds:
                return BusEmissionResult(accepted=False, reason="deduplicated", event=event)
            effective_cooldown = (
                self.default_cooldown_seconds if cooldown_seconds is None else max(0.0, float(cooldown_seconds))
            )
            if elapsed < effective_cooldown:
                return BusEmissionResult(accepted=False, reason="cooldown", event=event)

        self._last_by_key[event.dedup_key] = (now, digest)
        self._history.append(event)

        errors: list[str] = []
        handlers = [*self._subscribers.get(event.kind, ()), *self._subscribers.get("*", ())]
        for handler in handlers:
            try:
                outcome = handler(event)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                errors.append(type(exc).__name__)

        return BusEmissionResult(accepted=True, reason="emitted", event=event, handler_errors=errors)

    def recent_events(self, *, kind: str | None = None, limit: int = 50) -> list[HeartbeatEvent]:
        """Return most recent emitted events, optionally filtered by kind."""
        items = list(self._history)
        if kind is not None:
            items = [item for item in items if item.kind == kind]
        return list(reversed(items[-max(1, int(limit)) :]))

    def clear_cooldown(self, dedup_key: str | None = None) -> None:
        """Clear cooldown/dedup state for one key or all keys."""
        if dedup_key is None:
            self._last_by_key.clear()
        else:
            self._last_by_key.pop(dedup_key, None)

    def stats(self) -> dict[str, Any]:
        """Return compact bus metrics for diagnostics."""
        return {
            "tracked_keys": len(self._last_by_key),
            "history_count": len(self._history),
            "subscriber_kinds": sorted(self._subscribers),
        }
