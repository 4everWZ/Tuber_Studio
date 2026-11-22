"""Shared Bilibili read gating & rate-limit backoff state machine.

HTTP 412/429 and API -352/-412/-509 responses widen Discovery exploration
cooldowns first while keeping Inbox polling and read-only reconciliation alive.
"""

import asyncio
import contextvars
import time

from ...core.errors import SendLimitReached
from ...core.turns import operation_checkpoint
from .context import agent_depth, proactive_read, read_round

RATE_LIMIT_HTTP_CODES = frozenset({412, 429})
RATE_LIMIT_API_CODES = frozenset({-352, -412, -509})
RATE_LIMIT_KINDS = frozenset({"rate_limited"})
PIPELINE_READ = contextvars.ContextVar("operations_read_pipeline", default=None)


def is_rate_limit_error(error: BaseException) -> bool:
    """Return True for HTTP 412/429 or Bilibili -352/-412/-509 rate/risk limits."""
    kind = getattr(error, "kind", None)
    status = getattr(error, "http_status", None)
    code = getattr(error, "code", None)
    wait_for = getattr(error, "wait_for", None)
    if kind in RATE_LIMIT_KINDS or status in RATE_LIMIT_HTTP_CODES or code in RATE_LIMIT_API_CODES:
        return True
    if wait_for == "rate_limit":
        return True
    message = str(error)
    return any(token in message for token in ("HTTP 412", "HTTP 429", "-352", "-412", "-509"))


class PlatformReadGate:
    """Coordinates read pacing, retries, and Discovery-first rate-limit backoff."""

    def __init__(
        self,
        *,
        read_interval: float = 0.25,
        discovery_base_cooldown: float = 60.0,
        discovery_max_cooldown: float = 900.0,
    ):
        self.read_interval = float(read_interval)
        self.read_lock = asyncio.Lock()
        self.last_read = 0.0
        self.discovery_base_cooldown = float(discovery_base_cooldown)
        self.discovery_max_cooldown = float(discovery_max_cooldown)
        self._discovery_backoff: dict[str, dict] = {}
        self._inbox_last_restricted: dict[str, dict] = {}

    def _scope_key(self, account_id="") -> str:
        return str(account_id or "__default__")

    def discovery_backoff(self, account_id="", *, now: float | None = None) -> dict | None:
        current = time.time() if now is None else now
        entry = self._discovery_backoff.get(self._scope_key(account_id))
        if entry and entry["until"] > current:
            return dict(entry)
        return None

    def allows(self, pipeline: str, *, account_id="", reconciliation: bool = False, now: float | None = None) -> bool:
        """Inbox polling and read-only reconciliation stay alive even when Discovery is backed off."""
        if reconciliation or pipeline == "inbox":
            return True
        if pipeline == "discovery":
            return self.discovery_backoff(account_id, now=now) is None
        return True

    def record_restriction(
        self, error: BaseException, *, pipeline: str = "discovery", account_id="", now: float | None = None
    ) -> dict:
        """Pause/widen Discovery cooldown first; keep Inbox & reconciliation unblocked."""
        current = time.time() if now is None else now
        key = self._scope_key(account_id)
        prev = self._discovery_backoff.get(key)
        strikes = (prev["strikes"] + 1) if prev else 1
        cooldown = min(self.discovery_max_cooldown, self.discovery_base_cooldown * (2 ** (strikes - 1)))
        until = current + cooldown
        state = {
            "until": until,
            "cooldown_seconds": cooldown,
            "strikes": strikes,
            "source_pipeline": pipeline,
            "reason": str(error),
            "http_status": getattr(error, "http_status", None),
            "code": getattr(error, "code", None),
        }
        self._discovery_backoff[key] = state
        if pipeline == "inbox":
            self._inbox_last_restricted[key] = {
                "at": current,
                "reason": str(error),
                "http_status": getattr(error, "http_status", None),
                "code": getattr(error, "code", None),
            }
        return dict(state)

    def record_success(self, *, pipeline: str = "discovery", account_id="", now: float | None = None) -> None:
        """Clear Discovery backoff strikes after a post-cooldown Discovery read succeeds."""
        current = time.time() if now is None else now
        key = self._scope_key(account_id)
        entry = self._discovery_backoff.get(key)
        if pipeline == "discovery" and entry and entry["until"] <= current:
            self._discovery_backoff.pop(key, None)

    def status(self, account_id="", *, now: float | None = None) -> dict:
        current = time.time() if now is None else now
        key = self._scope_key(account_id)
        backoff = self.discovery_backoff(account_id, now=current)
        return {
            "discovery_paused": backoff is not None,
            "discovery_backoff": backoff,
            "inbox_allowed": True,
            "reconciliation_allowed": True,
            "inbox_last_restricted": self._inbox_last_restricted.get(key),
        }

    async def gate(
        self,
        *,
        reserve_budget=None,
        quota_check=None,
        pipeline: str | None = None,
        account_id="",
        reconciliation: bool = False,
    ) -> None:
        identity = read_round.get()
        if checkpoint := operation_checkpoint.get():
            checkpoint()
        effective_pipeline = pipeline or PIPELINE_READ.get() or ("discovery" if proactive_read.get() else "general")
        if effective_pipeline == "discovery" and not reconciliation:
            if backoff := self.discovery_backoff(account_id):
                raise SendLimitReached("平台读频受限，暂停探索读取并优先保障收件箱", backoff["until"])
        if identity and reserve_budget is not None:
            reserve_budget(identity)
        async with self.read_lock:
            await asyncio.sleep(max(0, self.last_read + self.read_interval - time.monotonic()))
            if proactive_read.get() and quota_check is not None:
                if due := quota_check():
                    raise SendLimitReached("评论小时额度已用完，读取进度已保存", due)
            self.last_read = time.monotonic()

    async def read(
        self,
        method,
        *args,
        retries: int = 0,
        gate_fn=None,
        use_adapter_gate: bool = False,
        pipeline: str | None = None,
        account_id="",
        reconciliation: bool = False,
        **kwargs,
    ):
        effective_pipeline = pipeline or PIPELINE_READ.get() or ("discovery" if proactive_read.get() else "general")
        if effective_pipeline == "discovery" and not reconciliation:
            if backoff := self.discovery_backoff(account_id):
                raise SendLimitReached("平台读频受限，暂停探索读取并优先保障收件箱", backoff["until"])
        for attempt in range(retries + 1):
            try:
                if not use_adapter_gate and gate_fn is not None:
                    await gate_fn()
                result = await method(*args, **kwargs)
                self.record_success(pipeline=effective_pipeline, account_id=account_id)
                return result
            except ValueError as error:
                if is_rate_limit_error(error):
                    self.record_restriction(error, pipeline=effective_pipeline, account_id=account_id)
                    raise
                if getattr(error, "kind", None) != "network" or attempt >= retries:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))

    async def read_proactively(self, method, *args, **kwargs):
        token = proactive_read.set(True)
        pipeline_token = PIPELINE_READ.set("discovery")
        try:
            return await method(*args, **kwargs)
        finally:
            PIPELINE_READ.reset(pipeline_token)
            proactive_read.reset(token)


__all__ = [
    "PIPELINE_READ",
    "PlatformReadGate",
    "RATE_LIMIT_API_CODES",
    "RATE_LIMIT_HTTP_CODES",
    "RATE_LIMIT_KINDS",
    "agent_depth",
    "is_rate_limit_error",
    "proactive_read",
    "read_round",
]
