"""Shared business-day timezone window helpers."""

import datetime as dt
import time
from zoneinfo import ZoneInfo


def day_window(timezone: str = "Asia/Taipei", now: float | None = None) -> dict[str, object]:
    """Return ISO date and epoch [start_at, next_at) bounds for one timezone day."""
    current = time.time() if now is None else now
    local = dt.datetime.fromtimestamp(current, ZoneInfo(timezone))
    start = dt.datetime.combine(local.date(), dt.time(), tzinfo=local.tzinfo)
    following = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), tzinfo=local.tzinfo)
    return {"day": local.date().isoformat(), "start_at": start.timestamp(), "next_at": following.timestamp()}


def day_bounds(timezone: str = "Asia/Taipei", day: dt.date | None = None) -> dict[str, object]:
    """Return [start_at, next_at) bounds for an explicit date or today in timezone."""
    zone = ZoneInfo(timezone)
    target = day or dt.datetime.now(zone).date()
    return day_window(timezone, dt.datetime.combine(target, dt.time(), tzinfo=zone).timestamp())
