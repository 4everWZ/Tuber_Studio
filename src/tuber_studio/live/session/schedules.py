"""Durable timed and time-window live broadcast schedules with operations staggering."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from ...core.models import StrictModel
from ...core.store import dump
from ...infra.inference.providers import public_error
from ...operations.kernel.schedules import (
    WEEKDAY_LABELS,
    _format_local,
    _parse_local_datetime,
    daily_window_bounds,
)


class LiveScheduleCreate(StrictModel):
    """Validated payload for creating a timed or recurring live stream schedule."""

    kind: Literal["duration", "once_window", "daily_window"]
    label: str = Field(default="", max_length=120)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    start_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    end_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    start_local: str | None = Field(default=None, max_length=32)
    end_local: str | None = Field(default=None, max_length=32)
    start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    base_duration_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    max_overtime_minutes: int = Field(default=120, ge=0, le=720, strict=True)
    extension_step_minutes: int = Field(default=15, ge=1, le=120, strict=True)
    auto_pause_operations: bool = True

    @model_validator(mode="after")
    def validate_schedule(self):
        self.label = " ".join(self.label.split())
        if self.timezone is not None:
            self.timezone = self.timezone.strip()
            try:
                ZoneInfo(self.timezone)
            except (ValueError, ZoneInfoNotFoundError) as error:
                raise ValueError("请选择有效的 IANA 时区") from error
        cleaned_days: list[int] = []
        for day in self.weekdays:
            if type(day) is not int or not 0 <= day <= 6:
                raise ValueError("星期范围须在 0（周一）到 6（周日）之间")
            if day not in cleaned_days:
                cleaned_days.append(day)
        self.weekdays = sorted(cleaned_days)

        if self.kind == "duration":
            if self.duration_minutes is None:
                raise ValueError("按时长开播须填写持续分钟数（1–1440 分钟）")
        elif self.kind == "once_window":
            if self.start_at is None and not self.start_local:
                raise ValueError("单次直播时间段须填写开始时间")
            if self.end_at is None and not self.end_local and self.duration_minutes is None:
                raise ValueError("单次直播时间段须填写结束时间或持续分钟数")
        elif self.kind == "daily_window":
            for src_attr, dst_attr in (("start_local", "start_time"), ("end_local", "end_time")):
                if not getattr(self, dst_attr) and getattr(self, src_attr):
                    raw_val = str(getattr(self, src_attr)).strip()
                    if len(raw_val) == 5 and ":" in raw_val:
                        setattr(self, dst_attr, raw_val)
                    elif " " in raw_val or "T" in raw_val:
                        tail = raw_val.replace("T", " ").split()[-1][:5]
                        setattr(self, dst_attr, tail)
            if not self.start_time or not self.end_time:
                raise ValueError("每日定时开播须填写开始与结束时刻（HH:MM）")
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.start_time) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", self.end_time
            ):
                raise ValueError("每日定时开播时刻须为 HH:MM 格式")
            if self.start_time == self.end_time:
                raise ValueError("每日定时开播的开始与结束时刻不能相同")
        return self


class LiveScheduleUpdate(StrictModel):
    """Validated payload for updating, pausing/enabling, or cancelling a live schedule."""

    enabled: bool | None = Field(default=None, strict=True)
    action: Literal["cancel"] | None = None
    label: str | None = Field(default=None, max_length=120)
    auto_pause_operations: bool | None = Field(default=None, strict=True)
    max_overtime_minutes: int | None = Field(default=None, ge=0, le=720, strict=True)
    extension_step_minutes: int | None = Field(default=None, ge=1, le=120, strict=True)


class LiveSchedules:
    """Persistent scheduler for timed/recurring live broadcasts with OvertimeTheater & ops stagger."""

    def __init__(self, live) -> None:
        self.live = live
        self.runtime = live.runtime
        self.store = live.store
        self.settings = live.settings
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._stopping_from_schedule = False
        self._closed = False
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_schedules(
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                start_at REAL,
                end_at REAL,
                duration_minutes INTEGER,
                start_time TEXT,
                end_time TEXT,
                weekdays TEXT NOT NULL DEFAULT '[]',
                timezone TEXT NOT NULL DEFAULT 'Asia/Taipei',
                base_duration_minutes INTEGER NOT NULL DEFAULT 60,
                max_overtime_minutes INTEGER NOT NULL DEFAULT 120,
                extension_step_minutes INTEGER NOT NULL DEFAULT 15,
                auto_pause_operations INTEGER NOT NULL DEFAULT 1,
                paused_operations INTEGER NOT NULL DEFAULT 0,
                paused_ops_snapshot TEXT NOT NULL DEFAULT '{}',
                last_run_key TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS live_schedules_status_idx
                ON live_schedules(enabled, status, start_at, end_at);
            """
        )

    def live_enabled(self) -> bool:
        return bool(self.settings.data.get("features", {}).get("live_enabled", False))

    def _default_timezone(self) -> str:
        return str(self.live.timezone() or "Asia/Taipei")

    def start(self) -> None:
        """Start the background live schedule loop if not already running."""
        self._closed = False
        if self._task is not None and not self._task.done():
            self._wake.set()
            return
        self._wake.clear()
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        """Stop the background live schedule loop cleanly without altering persisted schedule state."""
        self._closed = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _next_wake_seconds(self, *, now: float | None = None) -> float | None:
        now = time.time() if now is None else float(now)
        rows = self.store.all("SELECT * FROM live_schedules WHERE enabled=1")
        nearest: float | None = None
        for row in rows:
            candidates: list[float] = []
            if row["kind"] in {"duration", "once_window"}:
                if row["status"] == "scheduled" and row["start_at"] is not None:
                    candidates.append(float(row["start_at"]))
                if row["end_at"] is not None:
                    candidates.append(float(row["end_at"]))
            elif row["kind"] == "daily_window" and row["start_time"] and row["end_time"]:
                weekdays = json.loads(row["weekdays"] or "[]")
                active_key, _, active_end, next_start, _ = daily_window_bounds(
                    now, row["start_time"], row["end_time"], row["timezone"], weekdays
                )
                if row["status"] == "active" and row["end_at"] is not None:
                    candidates.append(float(row["end_at"]))
                elif active_key and active_end is not None:
                    if row["last_run_key"] != active_key:
                        candidates.append(now)
                    elif next_start is not None:
                        candidates.append(next_start)
                elif next_start is not None:
                    candidates.append(next_start)
            for target in candidates:
                delay = max(0.0, target - now)
                if nearest is None or delay < nearest:
                    nearest = delay
        if self.live.overtime_theater.active and self.live.overtime_theater.scheduled_end_at is not None:
            ot_delay = max(0.0, float(self.live.overtime_theater.scheduled_end_at) - now)
            if nearest is None or ot_delay < nearest:
                nearest = ot_delay
        return nearest

    async def _loop(self) -> None:
        try:
            await self.tick()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        while not self._closed:
            try:
                self._wake.clear()
                delay = self._next_wake_seconds()
                wait_seconds = 15.0 if delay is None else min(delay, 15.0)
                if wait_seconds > 0 and not self._wake.is_set():
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait_seconds)
                    except TimeoutError:
                        pass
                if self._closed:
                    break
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    def _serialize(self, row: dict, *, now: float) -> dict:
        weekdays = json.loads(row.get("weekdays") or "[]")
        tz = row.get("timezone") or "Asia/Taipei"
        start_at = float(row["start_at"]) if row.get("start_at") is not None else None
        end_at = float(row["end_at"]) if row.get("end_at") is not None else None
        if (
            row["status"] == "active"
            and self.live.overtime_theater.active
            and self.live.overtime_theater.scheduled_end_at is not None
        ):
            end_at = max(end_at or 0.0, float(self.live.overtime_theater.scheduled_end_at))

        next_start_at = start_at if row["status"] == "scheduled" and start_at is not None else None
        next_end_at = end_at if end_at is not None and row["status"] in {"scheduled", "active"} else None

        if row["kind"] == "daily_window" and row.get("start_time") and row.get("end_time"):
            active_key, act_s, act_e, nxt_s, nxt_e = daily_window_bounds(
                now, row["start_time"], row["end_time"], tz, weekdays
            )
            if row["status"] == "active":
                start_at = start_at or act_s
                end_at = end_at or act_e
                next_end_at = end_at
                next_start_at = nxt_s
            elif row["enabled"]:
                if active_key and row.get("last_run_key") != active_key and act_s is not None and act_e is not None:
                    next_start_at = act_s
                    next_end_at = act_e
                else:
                    next_start_at = nxt_s
                    next_end_at = nxt_e

        remaining = max(0, int(end_at - now)) if row["status"] == "active" and end_at is not None else None
        default_label = (
            f"限时直播 {row['duration_minutes']} 分钟"
            if row["kind"] == "duration" and row.get("duration_minutes")
            else (
                f"每日直播 {row['start_time']}–{row['end_time']}"
                if row["kind"] == "daily_window"
                else "定时直播时间段"
            )
        )
        return {
            "id": row["id"],
            "kind": row["kind"],
            "label": row["label"] or default_label,
            "status": row["status"],
            "enabled": bool(row["enabled"]),
            "start_at": start_at,
            "end_at": end_at,
            "start_local_display": _format_local(start_at, tz),
            "end_local_display": _format_local(end_at, tz),
            "next_start_at": next_start_at,
            "next_end_at": next_end_at,
            "next_start_local_display": _format_local(next_start_at, tz),
            "next_end_local_display": _format_local(next_end_at, tz),
            "duration_minutes": row.get("duration_minutes"),
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "weekdays": weekdays,
            "weekday_labels": [WEEKDAY_LABELS[d] for d in weekdays if 0 <= d <= 6],
            "timezone": tz,
            "base_duration_minutes": int(row.get("base_duration_minutes") or 60),
            "max_overtime_minutes": int(row.get("max_overtime_minutes") or 0),
            "extension_step_minutes": int(row.get("extension_step_minutes") or 15),
            "auto_pause_operations": bool(row.get("auto_pause_operations", 1)),
            "paused_operations": bool(row.get("paused_operations", 0)),
            "remaining_seconds": remaining,
            "last_error": row.get("last_error"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def list(self, *, now: float | None = None, include_finished: bool = True) -> list[dict]:
        """Return serialized live schedules ordered active -> scheduled -> recent finished."""
        now = time.time() if now is None else float(now)
        active_rows = self.store.all(
            "SELECT * FROM live_schedules WHERE status='active' ORDER BY end_at ASC, created_at ASC"
        )
        scheduled_rows = self.store.all(
            "SELECT * FROM live_schedules WHERE status='scheduled' ORDER BY COALESCE(start_at, created_at) ASC"
        )
        if not include_finished:
            return [self._serialize(r, now=now) for r in (*active_rows, *scheduled_rows)]
        other_rows = self.store.all(
            "SELECT * FROM live_schedules WHERE status NOT IN ('active','scheduled') "
            "ORDER BY updated_at DESC LIMIT 15"
        )
        return [self._serialize(r, now=now) for r in (*active_rows, *scheduled_rows, *other_rows)]

    def active(self, *, now: float | None = None) -> dict | None:
        """Return the active live schedule with nearest end timestamp, if any."""
        now = time.time() if now is None else float(now)
        row = self.store.one(
            "SELECT * FROM live_schedules WHERE enabled=1 AND status='active' "
            "ORDER BY COALESCE(end_at, 9999999999) ASC, updated_at DESC LIMIT 1"
        )
        return self._serialize(row, now=now) if row else None

    def status(self, *, now: float | None = None) -> dict:
        """Return combined live schedule status including feature flag & OvertimeTheater."""
        now = time.time() if now is None else float(now)
        items = self.list(now=now)
        active_item = self.active(now=now)
        paused_by_live = bool(
            self.store.one("SELECT 1 FROM live_schedules WHERE paused_operations=1 AND status='active' LIMIT 1")
        )
        return {
            "live_enabled": self.live_enabled(),
            "active_schedule": active_item,
            "items": items,
            "schedules": items,
            "overtime": self.live.overtime_theater.status(now=now),
            "operations_paused_by_live": paused_by_live,
        }

    async def _pause_operations_if_needed(self) -> tuple[bool, dict]:
        """Pause running autonomous operations when a scheduled live window starts."""
        ops = getattr(self.runtime, "operations", None)
        if ops is None:
            return False, {}
        policy = ops.policy()
        if not policy.get("enabled") or policy.get("emergency"):
            return False, {}

        snapshot = {
            "account_id": policy.get("account_id"),
            "character_id": policy.get("character_id"),
            "character_version": policy.get("character_version"),
        }
        browsing = getattr(self.runtime, "browsing", None)
        if browsing is not None and hasattr(browsing, "stop"):
            await browsing.stop(emergency=False)
            if ops.policy().get("enabled"):
                ops.configure({"enabled": False})
        else:
            ops.configure({"enabled": False})
        return True, snapshot

    async def _restore_operations_if_needed(self, rows: list[dict], *, now: float) -> str | None:
        """Restore autonomous operations once all live schedules that paused it have ended."""
        paused_rows = [row for row in rows if row.get("paused_operations")]
        if not paused_rows:
            return None
        still_pausing = self.store.one(
            "SELECT 1 FROM live_schedules WHERE enabled=1 AND status='active' AND paused_operations=1 LIMIT 1"
        )
        if still_pausing is not None:
            return None

        ops = getattr(self.runtime, "operations", None)
        if ops is None:
            return None
        policy = ops.policy()
        if policy.get("emergency") or policy.get("enabled"):
            return None

        snapshot: dict = {}
        for row in reversed(paused_rows):
            try:
                parsed = json.loads(row.get("paused_ops_snapshot") or "{}")
                if isinstance(parsed, dict) and parsed.get("account_id"):
                    snapshot = parsed
                    break
            except Exception:
                continue

        browsing = getattr(self.runtime, "browsing", None)
        if browsing is not None and hasattr(browsing, "set_enabled"):
            try:
                await browsing.set_enabled(True)
                return None
            except Exception as error:
                return public_error(error)

        account_id = snapshot.get("account_id") or policy.get("account_id") or "default"
        character = self.runtime.character
        try:
            ops.configure({"enabled": True}, account_id=account_id, character=character)
        except Exception as error:
            return public_error(error)
        return None

    async def _activate_live_window(
        self,
        *,
        win_start: float,
        win_end: float,
        base_duration_minutes: int,
        max_overtime_minutes: int,
        extension_step_minutes: int,
        auto_pause_operations: bool,
        now: float,
    ) -> tuple[bool, dict]:
        if not self.live_enabled():
            raise ValueError("直播功能未启用（features.live_enabled=false），无法自动开播")
        paused_ops = False
        snapshot: dict = {}
        if auto_pause_operations:
            paused_ops, snapshot = await self._pause_operations_if_needed()

        if self.live.state != "live":
            await self.live.control("start")

        if not self.live.overtime_theater.active:
            span_minutes = max(1, round((win_end - win_start) / 60))
            eff_base = base_duration_minutes or span_minutes
            self.live.overtime_theater.start_window(
                base_duration_minutes=eff_base,
                max_overtime_minutes=max_overtime_minutes,
                extension_step_minutes=extension_step_minutes,
                now=win_start,
            )
            # Align scheduled_end_at cleanly to the schedule window end when win_end differs
            self.live.overtime_theater.base_end_at = win_end
            self.live.overtime_theater.scheduled_end_at = win_end
            self.live.overtime_theater.tick(now=now)
        return paused_ops, snapshot

    async def _deactivate_live_if_no_active(self, ended_rows: list[dict], *, now: float) -> str | None:
        remaining = self.store.one(
            "SELECT 1 FROM live_schedules WHERE enabled=1 AND status='active' LIMIT 1"
        )
        if remaining is not None:
            return None

        if self.live.overtime_theater.active:
            self.live.overtime_theater.stop()
        if self.live.state != "stopped":
            self._stopping_from_schedule = True
            try:
                await self.live.control("stop")
            finally:
                self._stopping_from_schedule = False
        return await self._restore_operations_if_needed(ended_rows, now=now)

    async def create(self, payload: dict | LiveScheduleCreate, *, now: float | None = None) -> dict:
        """Validate, persist, and immediately activate if the live schedule covers `now`."""
        if not self.live_enabled():
            raise ValueError("直播功能未启用（features.live_enabled=false），请先开启直播功能后再创建直播计划")
        model = (
            payload
            if isinstance(payload, LiveScheduleCreate)
            else LiveScheduleCreate.model_validate(payload)
        )
        now = time.time() if now is None else float(now)
        tz = model.timezone or self._default_timezone()
        schedule_id = f"lsc-{uuid4().hex[:12]}"

        start_at: float | None = None
        end_at: float | None = None
        duration_minutes = model.duration_minutes
        start_time = model.start_time
        end_time = model.end_time
        last_run_key = ""
        activate_now = False

        if model.kind == "duration":
            start_at = now
            end_at = now + int(model.duration_minutes or 0) * 60
            activate_now = True
            base_duration = model.base_duration_minutes or int(model.duration_minutes or 60)
        elif model.kind == "once_window":
            start_at = (
                float(model.start_at)
                if model.start_at is not None
                else _parse_local_datetime(model.start_local or "", tz)
            )
            if model.end_at is not None:
                end_at = float(model.end_at)
            elif model.end_local:
                end_at = _parse_local_datetime(model.end_local, tz)
            else:
                end_at = start_at + int(model.duration_minutes or 0) * 60
            if end_at <= start_at:
                raise ValueError("结束时间必须晚于开始时间")
            span_seconds = end_at - start_at
            if span_seconds < 60 or span_seconds > 1440 * 60:
                raise ValueError("单次直播时间段长度须在 1 分钟至 24 小时之间")
            if end_at <= now:
                raise ValueError("直播时间段结束时间已过，请选择未来的时间段")
            duration_minutes = max(1, round(span_seconds / 60))
            base_duration = model.base_duration_minutes or duration_minutes
            activate_now = start_at <= now < end_at
        else:
            active_key, act_s, act_e, _, _ = daily_window_bounds(
                now, start_time or "", end_time or "", tz, model.weekdays
            )
            if active_key and act_s is not None and act_e is not None:
                activate_now = True
                start_at, end_at = act_s, act_e
                last_run_key = active_key
                base_duration = model.base_duration_minutes or max(1, round((act_e - act_s) / 60))
            else:
                _, _, _, nxt_s, nxt_e = daily_window_bounds(
                    now, start_time or "", end_time or "", tz, model.weekdays
                )
                span_min = round((nxt_e - nxt_s) / 60) if (nxt_s and nxt_e) else 60
                base_duration = model.base_duration_minutes or max(1, span_min)

        paused_ops = False
        snapshot: dict = {}
        if activate_now and start_at is not None and end_at is not None:
            paused_ops, snapshot = await self._activate_live_window(
                win_start=start_at,
                win_end=end_at,
                base_duration_minutes=base_duration,
                max_overtime_minutes=model.max_overtime_minutes,
                extension_step_minutes=model.extension_step_minutes,
                auto_pause_operations=model.auto_pause_operations,
                now=now,
            )

        status = "active" if activate_now else "scheduled"
        with self.store.db:
            self.store.db.execute(
                """
                INSERT INTO live_schedules(
                    id, kind, label, status, enabled, start_at, end_at, duration_minutes,
                    start_time, end_time, weekdays, timezone, base_duration_minutes,
                    max_overtime_minutes, extension_step_minutes, auto_pause_operations,
                    paused_operations, paused_ops_snapshot, last_run_key, last_error,
                    created_at, updated_at
                ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
                """,
                (
                    schedule_id,
                    model.kind,
                    model.label,
                    status,
                    start_at,
                    end_at,
                    duration_minutes,
                    start_time,
                    end_time,
                    dump(model.weekdays),
                    tz,
                    base_duration,
                    model.max_overtime_minutes,
                    model.extension_step_minutes,
                    int(model.auto_pause_operations),
                    int(paused_ops),
                    dump(snapshot),
                    last_run_key,
                    now,
                    now,
                ),
            )
        self._wake.set()
        row = self.store.one("SELECT * FROM live_schedules WHERE id=?", (schedule_id,))
        return self._serialize(row, now=now)

    async def update(
        self,
        schedule_id: str,
        payload: dict | LiveScheduleUpdate,
        *,
        now: float | None = None,
    ) -> dict:
        """Update label/overtime config, pause/resume, or cancel a live schedule."""
        row = self.store.one("SELECT * FROM live_schedules WHERE id=?", (schedule_id,))
        if row is None:
            raise ValueError("找不到指定的定时直播计划")
        model = (
            payload
            if isinstance(payload, LiveScheduleUpdate)
            else LiveScheduleUpdate.model_validate(payload)
        )
        now = time.time() if now is None else float(now)
        was_active = row["status"] == "active"

        next_label = row["label"] if model.label is None else " ".join(model.label.split())
        next_enabled = bool(row["enabled"])
        next_status = row["status"]
        next_error = row["last_error"]
        next_start = row["start_at"]
        next_end = row["end_at"]
        next_auto_pause = (
            bool(row["auto_pause_operations"])
            if model.auto_pause_operations is None
            else bool(model.auto_pause_operations)
        )
        next_max_ot = (
            int(row["max_overtime_minutes"])
            if model.max_overtime_minutes is None
            else int(model.max_overtime_minutes)
        )
        next_step = (
            int(row["extension_step_minutes"])
            if model.extension_step_minutes is None
            else int(model.extension_step_minutes)
        )

        if model.action == "cancel" or model.enabled is False:
            if row["kind"] == "daily_window" and model.action != "cancel":
                next_enabled = False
                next_status = "paused"
            else:
                next_enabled = False
                next_status = "cancelled"
            if row["kind"] == "daily_window":
                next_start = None
                next_end = None
            next_error = None
        elif model.enabled is True:
            if not self.live_enabled():
                raise ValueError("直播功能未启用（features.live_enabled=false），无法启用直播计划")
            if row["kind"] != "daily_window":
                if row["end_at"] is None or float(row["end_at"]) <= now:
                    raise ValueError("该一次性直播计划已结束或已过期，请新建计划")
            next_enabled = True
            next_status = "scheduled"
            next_error = None

        with self.store.db:
            self.store.db.execute(
                "UPDATE live_schedules SET label=?, enabled=?, status=?, start_at=?, end_at=?, "
                "auto_pause_operations=?, max_overtime_minutes=?, extension_step_minutes=?, "
                "last_error=?, updated_at=? WHERE id=?",
                (
                    next_label,
                    int(next_enabled),
                    next_status,
                    next_start,
                    next_end,
                    int(next_auto_pause),
                    next_max_ot,
                    next_step,
                    next_error,
                    now,
                    schedule_id,
                ),
            )
        if was_active and next_status != "active":
            await self._deactivate_live_if_no_active([row], now=now)
            with self.store.db:
                self.store.db.execute("UPDATE live_schedules SET paused_operations=0 WHERE id=?", (schedule_id,))
        if next_enabled and not was_active:
            await self.tick(now=now)
        self._wake.set()
        updated = self.store.one("SELECT * FROM live_schedules WHERE id=?", (schedule_id,))
        return self._serialize(updated, now=now)

    async def delete(self, schedule_id: str, *, now: float | None = None) -> dict:
        """Delete a live schedule and stop live / restore operations if it was active."""
        now = time.time() if now is None else float(now)
        row = self.store.one("SELECT * FROM live_schedules WHERE id=?", (schedule_id,))
        if row is None:
            raise ValueError("找不到指定的定时直播计划")
        was_active = row["status"] == "active"
        serialized = self._serialize(row, now=now)
        with self.store.db:
            self.store.db.execute("DELETE FROM live_schedules WHERE id=?", (schedule_id,))
        if was_active:
            await self._deactivate_live_if_no_active([row], now=now)
        self._wake.set()
        return serialized

    def on_manual_stop(self, *, now: float | None = None) -> None:
        """Reconcile active schedules when the host manually stops the live session."""
        if self._stopping_from_schedule or self._closed:
            return
        now = time.time() if now is None else float(now)
        rows = self.store.all("SELECT * FROM live_schedules WHERE enabled=1 AND status='active'")
        if not rows:
            return
        reason = "已由人工停止提前结束"
        with self.store.db:
            for row in rows:
                if row["kind"] == "daily_window":
                    self.store.db.execute(
                        "UPDATE live_schedules SET status='scheduled', start_at=NULL, end_at=NULL, "
                        "last_error=?, updated_at=? WHERE id=?",
                        (reason, now, row["id"]),
                    )
                else:
                    self.store.db.execute(
                        "UPDATE live_schedules SET status='cancelled', enabled=0, "
                        "last_error=?, updated_at=? WHERE id=?",
                        (reason, now, row["id"]),
                    )
        self._wake.set()

    async def tick(self, *, now: float | None = None) -> dict:
        """Advance due/expired live schedules and sync OvertimeTheater extensions."""
        now = time.time() if now is None else float(now)
        expired_ids: list[str] = []
        ended_rows: list[dict] = []
        due_rows: list[tuple[dict, str, float, float]] = []

        async with self._tick_lock:
            ot = self.live.overtime_theater
            ot_cue = ot.tick(now=now) if ot.active else None
            ot_end = float(ot.scheduled_end_at) if ot.scheduled_end_at is not None else None

            rows = self.store.all("SELECT * FROM live_schedules WHERE enabled=1 ORDER BY created_at ASC")
            with self.store.db:
                for row in rows:
                    if row["kind"] in {"duration", "once_window"}:
                        start_at = float(row["start_at"] or 0.0)
                        end_at = float(row["end_at"] or 0.0)
                        if row["status"] == "active" and ot_end is not None and ot_end > end_at:
                            end_at = ot_end
                            self.store.db.execute(
                                "UPDATE live_schedules SET end_at=?, updated_at=? WHERE id=?",
                                (end_at, now, row["id"]),
                            )
                            row = dict(row, end_at=end_at)
                        if end_at <= now:
                            self.store.db.execute(
                                "UPDATE live_schedules SET status='completed', enabled=0, "
                                "last_error=NULL, updated_at=? WHERE id=?",
                                (now, row["id"]),
                            )
                            if row["status"] == "active":
                                expired_ids.append(row["id"])
                                ended_rows.append(row)
                            continue
                        if start_at <= now < end_at:
                            due_rows.append((row, "", start_at, end_at))
                    elif row["kind"] == "daily_window" and row["start_time"] and row["end_time"]:
                        weekdays = json.loads(row["weekdays"] or "[]")
                        if row["status"] == "active":
                            row_end = float(row["end_at"] or 0.0)
                            if ot_end is not None and ot_end > row_end:
                                row_end = ot_end
                                self.store.db.execute(
                                    "UPDATE live_schedules SET end_at=?, updated_at=? WHERE id=?",
                                    (row_end, now, row["id"]),
                                )
                                row = dict(row, end_at=row_end)
                            if row["end_at"] is not None and row_end <= now:
                                self.store.db.execute(
                                    "UPDATE live_schedules SET status='scheduled', start_at=NULL, end_at=NULL, "
                                    "last_error=NULL, updated_at=? WHERE id=?",
                                    (now, row["id"]),
                                )
                                expired_ids.append(row["id"])
                                ended_rows.append(row)
                                row = dict(row, status="scheduled", start_at=None, end_at=None)
                        active_key, act_s, act_e, _, _ = daily_window_bounds(
                            now, row["start_time"], row["end_time"], row["timezone"], weekdays
                        )
                        if row["status"] == "active":
                            due_rows.append(
                                (row, row.get("last_run_key") or active_key or "", float(row["start_at"]), float(row["end_at"]))
                            )
                        elif active_key and act_s is not None and act_e is not None and row["last_run_key"] != active_key:
                            due_rows.append((row, active_key, act_s, act_e))

            if expired_ids:
                restore_error = await self._deactivate_live_if_no_active(ended_rows, now=now)
                with self.store.db:
                    for row in ended_rows:
                        self.store.db.execute(
                            "UPDATE live_schedules SET paused_operations=0, last_error=? WHERE id=?",
                            (restore_error, row["id"]),
                        )

            for row, run_key, win_start, win_end in due_rows:
                need_start = row["status"] != "active" or self.live.state != "live"
                if not need_start:
                    continue
                if not self.live_enabled():
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE live_schedules SET last_error=?, updated_at=? WHERE id=?",
                            ("直播功能未启用（features.live_enabled=false）", now, row["id"]),
                        )
                    continue
                try:
                    paused_ops, snapshot = await self._activate_live_window(
                        win_start=win_start,
                        win_end=win_end,
                        base_duration_minutes=int(row.get("base_duration_minutes") or 60),
                        max_overtime_minutes=int(row.get("max_overtime_minutes") or 120),
                        extension_step_minutes=int(row.get("extension_step_minutes") or 15),
                        auto_pause_operations=bool(row.get("auto_pause_operations", 1)),
                        now=now,
                    )
                except Exception as error:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE live_schedules SET last_error=?, updated_at=? WHERE id=?",
                            (public_error(error), now, row["id"]),
                        )
                else:
                    already_paused = bool(row.get("paused_operations"))
                    eff_paused = already_paused or paused_ops
                    eff_snapshot = row.get("paused_ops_snapshot") if already_paused else dump(snapshot)
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE live_schedules SET status='active', start_at=?, end_at=?, "
                            "last_run_key=?, paused_operations=?, paused_ops_snapshot=?, "
                            "last_error=NULL, updated_at=? WHERE id=?",
                            (
                                win_start,
                                win_end,
                                run_key or row["last_run_key"],
                                int(eff_paused),
                                eff_snapshot,
                                now,
                                row["id"],
                            ),
                        )
                    self._wake.set()

        return {
            "expired": expired_ids,
            "active_schedule": self.active(now=now),
            "overtime_cue": ot_cue,
        }
