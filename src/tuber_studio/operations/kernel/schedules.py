"""Durable timed and time-window autonomous operation schedules."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from ...core.models import StrictModel
from ...core.store import dump
from ...infra.inference.providers import public_error
from .closeouts import OperationCloseoutConfig, OperationCloseouts

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _parse_local_datetime(value: str, timezone: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError("请填写有效的时间")
    normalized = text.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("时间格式须为 YYYY-MM-DD HH:MM") from error
    if dt.tzinfo is not None:
        return dt.timestamp()
    zone = ZoneInfo(timezone)
    local = dt.replace(tzinfo=zone, fold=0)
    if datetime.fromtimestamp(local.timestamp(), zone).replace(tzinfo=None) != dt.replace(tzinfo=None):
        raise ValueError("该本地时间在时区切换中不存在")
    return local.timestamp()


def _format_local(timestamp: float | None, timezone: str) -> str | None:
    if timestamp is None:
        return None
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("Asia/Taipei")
    return datetime.fromtimestamp(float(timestamp), zone).strftime("%Y-%m-%d %H:%M")


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
    today = datetime.fromtimestamp(now, zone).date()
    allowed = set(weekdays)

    def _window_for_anchor(anchor: date) -> tuple[float, float] | None:
        if allowed and anchor.weekday() not in allowed:
            return None
        end_day = anchor + timedelta(days=1 if overnight else 0)
        start_wall = datetime(anchor.year, anchor.month, anchor.day, sh, sm)
        end_wall = datetime(end_day.year, end_day.month, end_day.day, eh, em)
        start_ts = start_wall.replace(tzinfo=zone, fold=0).timestamp()
        end_ts = end_wall.replace(tzinfo=zone, fold=0).timestamp()
        if end_ts <= start_ts:
            return None
        return start_ts, end_ts

    active_key = None
    active_start = None
    active_end = None
    for delta in (-1, 0):
        anchor = today + timedelta(days=delta)
        bounds = _window_for_anchor(anchor)
        if bounds and bounds[0] <= now < bounds[1]:
            active_key = anchor.isoformat()
            active_start, active_end = bounds
            break

    next_start = None
    next_end = None
    for delta in range(0, 15):
        anchor = today + timedelta(days=delta)
        bounds = _window_for_anchor(anchor)
        if bounds and bounds[0] > now:
            next_start, next_end = bounds
            break

    return active_key, active_start, active_end, next_start, next_end


class OperationScheduleCreate(StrictModel):
    """Validated payload for creating a timed or recurring autonomous operation schedule."""

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
    scope_discovery: bool = True
    scope_inbox: bool = True
    closeout: OperationCloseoutConfig = Field(default_factory=OperationCloseoutConfig)

    @model_validator(mode="after")
    def validate_schedule(self):
        self.label = " ".join(self.label.split())
        if not self.scope_discovery and not self.scope_inbox:
            raise ValueError("至少需要勾选主动探索（Discovery）或消息响应（Inbox）其中一项")
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
                raise ValueError("按时长开启须填写持续分钟数（1–1440 分钟）")
        elif self.kind == "once_window":
            if self.start_at is None and not self.start_local:
                raise ValueError("单次时间段须填写开始时间")
            if self.end_at is None and not self.end_local and self.duration_minutes is None:
                raise ValueError("单次时间段须填写结束时间或持续分钟数")
        elif self.kind == "daily_window":
            if not self.start_time or not self.end_time:
                raise ValueError("每日定时须填写开始与结束时刻（HH:MM）")
            if self.start_time == self.end_time:
                raise ValueError("每日定时的开始与结束时刻不能相同")
        return self


class OperationScheduleUpdate(StrictModel):
    """Validated payload for updating/enabling/cancelling an operation schedule."""

    enabled: bool | None = Field(default=None, strict=True)
    action: Literal["cancel"] | None = None
    label: str | None = Field(default=None, max_length=120)
    closeout: OperationCloseoutConfig | None = None


class OperationSchedules:
    """Persistent scheduler managing timed runs and recurring time windows for OperationsService."""

    def __init__(self, service):
        self.service = service
        self.store = service.store
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._stopping_from_schedule = False
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operation_schedules(
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                scope_discovery INTEGER NOT NULL DEFAULT 1,
                scope_inbox INTEGER NOT NULL DEFAULT 1,
                start_at REAL,
                end_at REAL,
                duration_minutes INTEGER,
                start_time TEXT,
                end_time TEXT,
                weekdays TEXT NOT NULL DEFAULT '[]',
                timezone TEXT NOT NULL DEFAULT 'Asia/Taipei',
                account_id TEXT NOT NULL DEFAULT '',
                character_id TEXT NOT NULL DEFAULT '',
                character_version INTEGER NOT NULL DEFAULT 0,
                last_run_key TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS operation_schedules_status_idx
                ON operation_schedules(enabled, status, start_at, end_at);
            """
        )
        if "closeout" not in {row[1] for row in self.store.db.execute("PRAGMA table_info(operation_schedules)")}:
            self.store.execute("ALTER TABLE operation_schedules ADD COLUMN closeout TEXT NOT NULL DEFAULT '{}'")
        self.closeouts = OperationCloseouts(service, daily_window_bounds)

    def _default_timezone(self) -> str:
        return str(self.service.policy().get("business_timezone") or "Asia/Taipei")

    def _scope_snapshot(self) -> tuple[str, str, int]:
        control = self.service.operations.policy()
        account_id = str(self.service.account or control.get("account_id") or "")
        character = self.service.runtime.character or {}
        return account_id, str(character.get("id") or ""), int(character.get("version") or 0)

    def start(self) -> None:
        """Start the background scheduler loop if not already running."""
        if self._task is not None and not self._task.done():
            self._wake.set()
            return
        self._wake.clear()
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        """Stop the background scheduler loop cleanly."""
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.closeouts.close()

    def _next_wake_seconds(self, *, now: float | None = None) -> float | None:
        now = time.time() if now is None else float(now)
        rows = self.store.all("SELECT * FROM operation_schedules WHERE enabled=1")
        nearest = self.closeouts.next_delay(now)
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
                if active_key and active_end is not None:
                    if row["status"] == "active" or row["last_run_key"] != active_key:
                        candidates.append(now if row["status"] != "active" else active_end)
                    elif next_start is not None:
                        candidates.append(next_start)
                elif next_start is not None:
                    candidates.append(next_start)
            for target in candidates:
                delay = max(0.0, target - now)
                if nearest is None or delay < nearest:
                    nearest = delay
        return nearest

    async def _loop(self) -> None:
        try:
            await self.tick()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        while not self.service.closed:
            try:
                self._wake.clear()
                delay = self._next_wake_seconds()
                wait_seconds = 15.0 if delay is None else min(delay, 15.0)
                if wait_seconds > 0 and not self._wake.is_set():
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait_seconds)
                    except TimeoutError:
                        pass
                if self.service.closed:
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
        next_start_at = start_at if row["status"] == "scheduled" and start_at is not None else None
        next_end_at = end_at if end_at is not None and row["status"] in {"scheduled", "active"} else None

        if row["kind"] == "daily_window" and row.get("start_time") and row.get("end_time"):
            active_key, act_s, act_e, nxt_s, nxt_e = daily_window_bounds(
                now, row["start_time"], row["end_time"], tz, weekdays
            )
            if row["status"] == "active" and act_e is not None:
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
            f"限时自主运营 {row['duration_minutes']} 分钟"
            if row["kind"] == "duration" and row.get("duration_minutes")
            else (
                f"每日 {row['start_time']}–{row['end_time']}"
                if row["kind"] == "daily_window"
                else "定时时间段运营"
            )
        )
        return {
            "id": row["id"],
            "kind": row["kind"],
            "label": row["label"] or default_label,
            "status": row["status"],
            "enabled": bool(row["enabled"]),
            "scope_discovery": bool(row["scope_discovery"]),
            "scope_inbox": bool(row["scope_inbox"]),
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
            "remaining_seconds": remaining,
            "last_error": row.get("last_error"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "closeout": self.closeouts.config(row).model_dump(),
            "closeout_runs": self.closeouts.list(row["id"]),
        }

    def list(self, *, now: float | None = None, include_finished: bool = True) -> list[dict]:
        """Return serialized operation schedules ordered active -> scheduled -> recent finished."""
        now = time.time() if now is None else float(now)
        active_rows = self.store.all(
            "SELECT * FROM operation_schedules WHERE status='active' ORDER BY end_at ASC, created_at ASC"
        )
        scheduled_rows = self.store.all(
            "SELECT * FROM operation_schedules WHERE status='scheduled' ORDER BY COALESCE(start_at, created_at) ASC"
        )
        if not include_finished:
            return [self._serialize(r, now=now) for r in (*active_rows, *scheduled_rows)]
        other_rows = self.store.all(
            "SELECT * FROM operation_schedules WHERE status NOT IN ('active','scheduled') "
            "ORDER BY updated_at DESC LIMIT 15"
        )
        return [self._serialize(r, now=now) for r in (*active_rows, *scheduled_rows, *other_rows)]

    def active(self, *, now: float | None = None) -> dict | None:
        """Return the active timed/window schedule with nearest end timestamp, if any."""
        now = time.time() if now is None else float(now)
        row = self.store.one(
            "SELECT * FROM operation_schedules WHERE enabled=1 AND status='active' "
            "ORDER BY COALESCE(end_at, 9999999999) ASC, updated_at DESC LIMIT 1"
        )
        return self._serialize(row, now=now) if row else None

    async def _activate_service(self, *, scope_discovery: bool, scope_inbox: bool) -> None:
        if self.service.operations.policy()["emergency"]:
            raise ValueError("紧急停止仍有效")
        if self.service.operations.draining:
            raise ValueError("当前动作正在收尾，请稍后再试")
        if self.service.operations.starting:
            raise ValueError("自主运营正在启动")
        previous_policy = self.service.policy()
        patch = {}
        if bool(previous_policy.get("discovery_enabled")) != bool(scope_discovery):
            patch["discovery_enabled"] = bool(scope_discovery)
        if bool(previous_policy.get("inbox_enabled")) != bool(scope_inbox):
            patch["inbox_enabled"] = bool(scope_inbox)
        if patch:
            await self.service.configure(patch)
        if not self.service.operations.policy()["enabled"]:
            try:
                await self.service.set_enabled(True)
            except Exception:
                if patch:
                    revert = {key: previous_policy[key] for key in patch}
                    await self.service.configure(revert)
                raise

    async def create(self, payload: dict | OperationScheduleCreate, *, now: float | None = None) -> dict:
        """Validate, persist, and immediately activate if the new schedule covers `now`."""
        model = (
            payload
            if isinstance(payload, OperationScheduleCreate)
            else OperationScheduleCreate.model_validate(payload)
        )
        now = time.time() if now is None else float(now)
        tz = model.timezone or self._default_timezone()
        account_id, character_id, character_version = self._scope_snapshot()
        schedule_id = f"sch-{uuid4().hex[:12]}"

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
                raise ValueError("单次时间段长度须在 1 分钟至 24 小时之间")
            if end_at <= now:
                raise ValueError("时间段结束时间已过，请选择未来的时间段")
            duration_minutes = max(1, round(span_seconds / 60))
            activate_now = start_at <= now < end_at
        elif model.kind == "daily_window":
            active_key, act_s, act_e, _, _ = daily_window_bounds(
                now, start_time or "", end_time or "", tz, model.weekdays
            )
            if active_key and act_s is not None and act_e is not None:
                activate_now = True
                start_at, end_at = act_s, act_e
                last_run_key = active_key

        if activate_now:
            await self._activate_service(
                scope_discovery=model.scope_discovery,
                scope_inbox=model.scope_inbox,
            )
            account_id, character_id, character_version = self._scope_snapshot()

        status = "active" if activate_now else "scheduled"
        with self.store.db:
            self.store.db.execute(
                """
                INSERT INTO operation_schedules(
                    id, kind, label, status, enabled, scope_discovery, scope_inbox,
                    start_at, end_at, duration_minutes, start_time, end_time, weekdays,
                    timezone, account_id, character_id, character_version, last_run_key,
                    last_error, created_at, updated_at, closeout
                ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
                """,
                (
                    schedule_id,
                    model.kind,
                    model.label,
                    status,
                    int(model.scope_discovery),
                    int(model.scope_inbox),
                    start_at,
                    end_at,
                    duration_minutes,
                    start_time,
                    end_time,
                    dump(model.weekdays),
                    tz,
                    account_id,
                    character_id,
                    character_version,
                    last_run_key,
                    now,
                    now,
                    model.closeout.model_dump_json(),
                ),
            )
        self._wake.set()
        row = self.store.one("SELECT * FROM operation_schedules WHERE id=?", (schedule_id,))
        return self._serialize(row, now=now)

    async def _stop_service_if_no_active(self) -> None:
        remaining = self.store.one("SELECT 1 FROM operation_schedules WHERE enabled=1 AND status='active' LIMIT 1")
        if remaining is None and (
            self.service.operations.policy()["enabled"] or self.service.operations.starting
        ):
            self._stopping_from_schedule = True
            try:
                await self.service.stop(emergency=False)
            finally:
                self._stopping_from_schedule = False

    async def update(
        self,
        schedule_id: str,
        payload: dict | OperationScheduleUpdate,
        *,
        now: float | None = None,
    ) -> dict:
        """Update label, pause/resume, or cancel a schedule, stopping runtime if it was the last active one."""
        row = self.store.one("SELECT * FROM operation_schedules WHERE id=?", (schedule_id,))
        if row is None:
            raise ValueError("找不到指定的定时运营计划")
        model = (
            payload
            if isinstance(payload, OperationScheduleUpdate)
            else OperationScheduleUpdate.model_validate(payload)
        )
        now = time.time() if now is None else float(now)
        was_active = row["status"] == "active"
        if model.closeout is not None and row["status"] not in {"scheduled", "paused"}:
            raise ValueError("只能修改待开始或暂停计划的收尾设置")

        next_label = row["label"] if model.label is None else " ".join(model.label.split())
        next_enabled = bool(row["enabled"])
        next_status = row["status"]
        next_error = row["last_error"]
        next_start = row["start_at"]
        next_end = row["end_at"]

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
            if row["kind"] != "daily_window":
                if row["end_at"] is None or float(row["end_at"]) <= now:
                    raise ValueError("该一次性定时任务已结束或已过期，请新建计划")
            next_enabled = True
            next_status = "scheduled"
            next_error = None

        with self.store.db:
            self.store.db.execute(
                "UPDATE operation_schedules SET label=?, enabled=?, status=?, start_at=?, end_at=?, "
                "last_error=?, updated_at=?, closeout=? WHERE id=?",
                (next_label, int(next_enabled), next_status, next_start, next_end, next_error, now,
                 model.closeout.model_dump_json() if model.closeout else row["closeout"], schedule_id),
            )
            if model.action == "cancel" or model.enabled is False:
                self.closeouts.cancel(schedule_id)
        if was_active and next_status != "active":
            await self._stop_service_if_no_active()
        if next_enabled and not was_active:
            await self.tick(now=now)
        self._wake.set()
        updated = self.store.one("SELECT * FROM operation_schedules WHERE id=?", (schedule_id,))
        return self._serialize(updated, now=now)

    async def delete(self, schedule_id: str, *, now: float | None = None) -> dict:
        """Delete a schedule and stop autonomous operations if it was the last active schedule."""
        now = time.time() if now is None else float(now)
        row = self.store.one("SELECT * FROM operation_schedules WHERE id=?", (schedule_id,))
        if row is None:
            raise ValueError("找不到指定的定时运营计划")
        was_active = row["status"] == "active"
        serialized = self._serialize(row, now=now)
        with self.store.db:
            self.closeouts.cancel(schedule_id)
            self.store.db.execute("DELETE FROM operation_schedules WHERE id=?", (schedule_id,))
        if was_active:
            await self._stop_service_if_no_active()
        self._wake.set()
        return serialized

    def on_manual_stop(self, *, emergency: bool = False, now: float | None = None) -> None:
        """Reconcile active schedules when the operator stops or emergency-halts automation manually."""
        if self._stopping_from_schedule or self.service.closed:
            return
        now = time.time() if now is None else float(now)
        rows = self.store.all("SELECT * FROM operation_schedules WHERE enabled=1 AND status='active'")
        if not rows:
            return
        reason = "紧急停止已中断" if emergency else "已由人工停止提前结束"
        with self.store.db:
            for row in rows:
                if row["kind"] == "daily_window":
                    self.store.db.execute(
                        "UPDATE operation_schedules SET status='scheduled', start_at=NULL, end_at=NULL, "
                        "last_error=?, updated_at=? WHERE id=?",
                        (reason, now, row["id"]),
                    )
                else:
                    self.store.db.execute(
                        "UPDATE operation_schedules SET status='cancelled', enabled=0, last_error=?, updated_at=? WHERE id=?",
                        (reason, now, row["id"]),
                    )
        self._wake.set()

    async def tick(self, *, now: float | None = None) -> dict:
        """Advance expired and due schedules against wall-clock `now`."""
        now = time.time() if now is None else float(now)
        expired_ids: list[str] = []
        due_rows: list[tuple[dict, str, float, float]] = []

        async with self._tick_lock:
            rows = self.store.all("SELECT * FROM operation_schedules WHERE enabled=1 ORDER BY created_at ASC")
            with self.store.db:
                for row in rows:
                    if row["kind"] in {"duration", "once_window"}:
                        start_at = float(row["start_at"] or 0.0)
                        end_at = float(row["end_at"] or 0.0)
                        if end_at <= now:
                            self.store.db.execute(
                                "UPDATE operation_schedules SET status='completed', enabled=0, "
                                "last_error=NULL, updated_at=? WHERE id=?",
                                (now, row["id"]),
                            )
                            if row["status"] == "active":
                                self.closeouts.enqueue(row, start_at, end_at)
                                expired_ids.append(row["id"])
                            continue
                        if start_at <= now < end_at:
                            due_rows.append((row, "", start_at, end_at))
                    elif row["kind"] == "daily_window" and row["start_time"] and row["end_time"]:
                        weekdays = json.loads(row["weekdays"] or "[]")
                        if row["status"] == "active" and row["end_at"] is not None and float(row["end_at"]) <= now:
                            self.closeouts.enqueue(row, float(row["start_at"]), float(row["end_at"]))
                            self.store.db.execute(
                                "UPDATE operation_schedules SET status='scheduled', start_at=NULL, end_at=NULL, "
                                "last_error=NULL, updated_at=? WHERE id=?",
                                (now, row["id"]),
                            )
                            expired_ids.append(row["id"])
                            row = dict(row, status="scheduled", start_at=None, end_at=None)
                        active_key, act_s, act_e, _, _ = daily_window_bounds(
                            now, row["start_time"], row["end_time"], row["timezone"], weekdays
                        )
                        if (
                            active_key
                            and act_s is not None
                            and act_e is not None
                            and (row["status"] == "active" or row["last_run_key"] != active_key)
                        ):
                            due_rows.append((row, active_key, act_s, act_e))

            if expired_ids:
                await self._stop_service_if_no_active()

            for row, run_key, win_start, win_end in due_rows:
                need_start = row["status"] != "active" or not self.service.operations.policy()["enabled"]
                if not need_start:
                    continue
                if self.service.operations.policy()["emergency"]:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE operation_schedules SET last_error=?, updated_at=? WHERE id=?",
                            ("紧急停止仍有效，未自动启动", now, row["id"]),
                        )
                    continue
                try:
                    await self._activate_service(
                        scope_discovery=bool(row["scope_discovery"]),
                        scope_inbox=bool(row["scope_inbox"]),
                    )
                except Exception as error:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE operation_schedules SET last_error=?, updated_at=? WHERE id=?",
                            (public_error(error), now, row["id"]),
                        )
                else:
                    account_id, character_id, character_version = self._scope_snapshot()
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE operation_schedules SET status='active', start_at=?, end_at=?, "
                            "last_run_key=?, last_error=NULL, updated_at=?, account_id=?, character_id=?, "
                            "character_version=? WHERE id=?",
                            (win_start, win_end, run_key or row["last_run_key"], now, account_id,
                             character_id, character_version, row["id"]),
                        )
                    self._wake.set()

        self.closeouts.kick(now)
        return {
            "expired": expired_ids,
            "active_schedule": self.active(now=now),
        }
