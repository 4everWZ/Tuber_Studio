"""Shared business-day and schedule timezone helpers."""

import datetime as dt
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


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


def parse_local_datetime(value: str, timezone: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError("请填写有效的时间")
    normalized = text.replace(" ", "T")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("时间格式须为 YYYY-MM-DD HH:MM") from error
    if parsed.tzinfo is not None:
        return parsed.timestamp()
    zone = ZoneInfo(timezone)
    local = parsed.replace(tzinfo=zone, fold=0)
    if dt.datetime.fromtimestamp(local.timestamp(), zone).replace(tzinfo=None) != parsed.replace(tzinfo=None):
        raise ValueError("该本地时间在时区切换中不存在")
    return local.timestamp()


def format_local_datetime(timestamp: float | None, timezone: str) -> str | None:
    if timestamp is None:
        return None
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("Asia/Taipei")
    return dt.datetime.fromtimestamp(float(timestamp), zone).strftime("%Y-%m-%d %H:%M")


def daily_window_bounds(
    now: float,
    start_time: str,
    end_time: str,
    timezone: str,
    weekdays: list[int] | tuple[int, ...] = (),
) -> tuple[str | None, float | None, float | None, float | None, float | None]:
    """Return `(active_run_key, active_start, active_end, next_start, next_end)` for a daily window."""
    zone = ZoneInfo(timezone)
    sh, sm = map(int, start_time.split(":"))
    eh, em = map(int, end_time.split(":"))
    overnight = (eh, em) < (sh, sm)
    today = dt.datetime.fromtimestamp(now, zone).date()
    allowed = set(weekdays)

    def _window_for_anchor(anchor: dt.date) -> tuple[float, float] | None:
        if allowed and anchor.weekday() not in allowed:
            return None
        end_day = anchor + dt.timedelta(days=1 if overnight else 0)
        start_wall = dt.datetime(anchor.year, anchor.month, anchor.day, sh, sm)
        end_wall = dt.datetime(end_day.year, end_day.month, end_day.day, eh, em)
        start_ts = start_wall.replace(tzinfo=zone, fold=0).timestamp()
        end_ts = end_wall.replace(tzinfo=zone, fold=0).timestamp()
        if end_ts <= start_ts:
            return None
        return start_ts, end_ts

    active_key = None
    active_start = None
    active_end = None
    for delta in (-1, 0):
        anchor = today + dt.timedelta(days=delta)
        bounds = _window_for_anchor(anchor)
        if bounds and bounds[0] <= now < bounds[1]:
            active_key = anchor.isoformat()
            active_start, active_end = bounds
            break

    next_start = None
    next_end = None
    for delta in range(0, 15):
        anchor = today + dt.timedelta(days=delta)
        bounds = _window_for_anchor(anchor)
        if bounds and bounds[0] > now:
            next_start, next_end = bounds
            break

    return active_key, active_start, active_end, next_start, next_end
