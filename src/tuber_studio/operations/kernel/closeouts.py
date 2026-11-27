"""Close an actual operations period and run its independently timed video workflow."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from ...core.errors import SendLimitReached
from ...core.models import StrictModel
from ...core.store import dump
from ...infra.inference.providers import public_error
from .closeout_editorial import compose_closeout
from .closeout_material import public_period_brief

_CLOCK = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
_VIDEO_WAITING = ("scheduled", "preparing", "submitted", "publish_uncertain")


class OperationCloseoutConfig(StrictModel):
    dynamic_mode: Literal["off", "review", "automatic"] = "off"
    video_mode: Literal["off", "manual", "automatic_review", "automatic_publish"] = "off"
    video_start: str | None = Field(default=None, pattern=_CLOCK)
    video_end: str | None = Field(default=None, pattern=_CLOCK)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_window(self):
        if self.video_mode != "off" and (not self.video_start or not self.video_end or self.video_start == self.video_end):
            raise ValueError("视频制作时段需要不同的开始和结束时刻（HH:MM）")
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ValueError, ZoneInfoNotFoundError) as error:
                raise ValueError("视频制作时段需要有效的 IANA 时区") from error
        return self


class OperationCloseouts:
    def __init__(self, service, window_bounds):
        self.service, self.store, self.window_bounds = service, service.store, window_bounds
        self._worker = None
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS operation_schedule_closeouts(
                id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, account_id TEXT NOT NULL,
                character_id TEXT NOT NULL, character_version INTEGER NOT NULL,
                period_start_at REAL NOT NULL, period_end_at REAL NOT NULL, period_timezone TEXT NOT NULL,
                video_start_at REAL, video_end_at REAL, config TEXT NOT NULL, brief TEXT,
                dynamic_status TEXT NOT NULL, dynamic_id INTEGER, dynamic_error TEXT, dynamic_retry_at REAL NOT NULL DEFAULT 0,
                video_status TEXT NOT NULL, video_job_id TEXT, video_error TEXT, video_requested INTEGER DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
                UNIQUE(schedule_id, period_start_at));
            CREATE INDEX IF NOT EXISTS schedule_closeouts_recent
                ON operation_schedule_closeouts(schedule_id, period_end_at DESC);
        """)
        if "dynamic_retry_at" not in {row[1] for row in self.store.db.execute("PRAGMA table_info(operation_schedule_closeouts)")}:
            self.store.db.execute("ALTER TABLE operation_schedule_closeouts ADD COLUMN dynamic_retry_at REAL NOT NULL DEFAULT 0")

    @staticmethod
    def config(row):
        return OperationCloseoutConfig.model_validate_json(row.get("closeout") or "{}")

    def enqueue(self, schedule, start_at, end_at):
        config = self.config(schedule)
        if config.dynamic_mode == config.video_mode == "off":
            return
        start_video = end_video = None
        if config.video_mode != "off":
            # The video window starts no earlier than the close of the actual operations period.
            _, _, _, start_video, end_video = self.window_bounds(
                end_at - 0.001, config.video_start, config.video_end, config.timezone or schedule["timezone"],
            )
        self.store.db.execute(
            "INSERT OR IGNORE INTO operation_schedule_closeouts("
            "id,schedule_id,account_id,character_id,character_version,period_start_at,period_end_at,period_timezone,"
            "video_start_at,video_end_at,config,dynamic_status,video_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{schedule['id']}-{round(start_at * 1000)}", schedule["id"], schedule["account_id"],
             schedule["character_id"], schedule["character_version"], start_at, end_at, schedule["timezone"],
             start_video, end_video, config.model_dump_json(), "off" if config.dynamic_mode == "off" else "pending",
             "off" if config.video_mode == "off" else "manual" if config.video_mode == "manual" else "scheduled"),
        )

    def cancel(self, schedule_id):
        self.store.db.execute(
            "UPDATE operation_schedule_closeouts SET cancelled=1, "
            "dynamic_status=CASE WHEN dynamic_status='pending' THEN 'cancelled' ELSE dynamic_status END, "
            "video_status=CASE WHEN video_status IN ('scheduled','manual','preparing') THEN 'cancelled' ELSE video_status END "
            "WHERE schedule_id=?", (schedule_id,),
        )

    def _serialize(self, row):
        keys = ("id", "period_start_at", "period_end_at", "video_start_at", "video_end_at",
                "dynamic_status", "dynamic_id", "video_status", "video_job_id")
        result = {key: row[key] for key in keys}
        if row["dynamic_status"] == "review" and row["dynamic_id"]:
            draft = self.store.one("SELECT status FROM dynamic_posts WHERE id=?", (row["dynamic_id"],))
            if draft and draft["status"] in {"published", "publish_uncertain", "rejected", "publish_failed"}:
                result["dynamic_status"] = {"publish_uncertain": "uncertain", "publish_failed": "failed"}.get(
                    draft["status"], draft["status"],
                )
        if row["video_status"] in {"ready", "window_missed"} and row["video_job_id"]:
            skill = getattr(self.service.runtime, "remotion_skill", None)
            job = skill.get_job(row["video_job_id"]) if skill else None
            if job and job["status"] == "published":
                result["video_status"] = "published" if job.get("platform_state") == "public_visible" else "submitted"
            elif job and job["status"] == "publish_uncertain":
                result["video_status"] = "publish_uncertain"
        return result | {"last_error": "；".join(x for x in (row["dynamic_error"], row["video_error"]) if x) or None}

    def list(self, schedule_id):
        return [self._serialize(row) for row in self.store.all(
            "SELECT * FROM operation_schedule_closeouts WHERE schedule_id=? ORDER BY period_end_at DESC LIMIT 8",
            (schedule_id,),
        )]

    def _row(self, run_id):
        return self.store.one("SELECT * FROM operation_schedule_closeouts WHERE id=?", (run_id,))

    def _save(self, run_id, **fields):
        columns = ",".join(f"{key}=?" for key in fields)
        self.store.execute(f"UPDATE operation_schedule_closeouts SET {columns} WHERE id=?", (*fields.values(), run_id))

    def request_video(self, schedule_id, run_id):
        row = self._row(run_id)
        if not row or row["schedule_id"] != schedule_id or row["cancelled"]:
            raise ValueError("找不到可操作的运营收尾记录")
        if row["video_status"] not in {"manual", "render_failed", "preparing"} and not (
            row["video_status"] == "window_missed" and not row["video_job_id"]
        ):
            raise ValueError("当前复盘视频不能开始制作")
        self._save(run_id, video_status="preparing", video_requested=1, video_error=None)
        self.kick(time.time())
        return self._serialize(self._row(run_id))

    def _pending(self):
        return self.store.all(
            "SELECT * FROM operation_schedule_closeouts WHERE (cancelled=0 AND (dynamic_status='pending' "
            "OR video_status IN ('scheduled','preparing'))) OR video_status IN ('submitted','publish_uncertain') "
            "ORDER BY period_end_at",
        )

    @staticmethod
    def _video_ready_at(row):
        if row["video_status"] == "preparing":
            return 0
        if row["video_status"] == "scheduled":
            return row["video_start_at"]
        if row["video_status"] in {"submitted", "publish_uncertain"}:
            return row["retry_at"]
        return None

    @staticmethod
    def _dynamic_due(row, now):
        return row["dynamic_status"] == "pending" and now >= row["dynamic_retry_at"]

    def _due(self, row, now):
        video_at = self._video_ready_at(row)
        return self._dynamic_due(row, now) or (video_at is not None and now >= video_at)

    def next_delay(self, now):
        if self._worker and not self._worker.done() or self.service.operations.draining:
            return None
        if self.service.operations.policy()["emergency"]:
            return None
        times = []
        for row in self._pending():
            if row["dynamic_status"] == "pending":
                times.append(row["dynamic_retry_at"])
            if (video_at := self._video_ready_at(row)) is not None:
                times.append(video_at)
        return max(0.0, min(times) - now) if times else None

    def kick(self, now):
        if self._worker and not self._worker.done() or self.service.operations.draining:
            return
        if self.service.operations.policy()["emergency"]:
            return
        rows = [row for row in self._pending() if self._due(row, now)]
        if rows:
            self._worker = asyncio.create_task(self._run(rows, now))

    async def close(self):
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None

    def _guard(self, row, *, writing=False):
        fresh = self._row(row["id"])
        if fresh is None or fresh["cancelled"]:
            raise ValueError("收尾任务已取消")
        if self.service.operations.policy()["emergency"]:
            raise ValueError("紧急停止仍有效")
        char = self.service.runtime.character or self.store.character()
        account = self.service.bilibili_account.display_account_id() or self.service.account
        if not row["account_id"] or (str(account or ""), char["id"], char["version"]) != (
            row["account_id"], row["character_id"], row["character_version"],
        ):
            raise ValueError("收尾任务的账号或角色版本已变化")
        if writing:
            self.service.operations.assert_review_writable(account)
        return char

    async def _brief(self, row, character):
        if row["brief"]:
            brief = json.loads(row["brief"])
        else:
            brief = public_period_brief(self.store, row)
            self._save(row["id"], brief=dump(brief))
        if "editorial_items" in brief and "editorial" not in brief:
            brief["editorial"] = await compose_closeout(self.service.runtime, character, brief)
            self._save(row["id"], brief=dump(brief))
        return brief

    async def _run(self, rows, now):
        started = time.monotonic()

        def clock():
            return now + time.monotonic() - started

        for row in rows:
            config = OperationCloseoutConfig.model_validate_json(row["config"])
            if self._dynamic_due(row, clock()):
                await self._dynamic(row, config, clock)
            if row["video_status"] in _VIDEO_WAITING:
                await self._video(self._row(row["id"]), config, clock)

    async def _dynamic(self, row, config, clock):
        dynamics = self.service.dynamics
        try:
            character = self._guard(row)
            brief = await self._brief(row, character)
            self._guard(row)
            draft = dynamics.prepare_schedule_closeout(row, brief, character)
            self._save(row["id"], dynamic_id=draft["id"])
            if config.dynamic_mode == "automatic" and draft["status"] in {"pending_review", "approved"}:
                draft = await dynamics.publish_schedule_closeout(draft, lambda: self._guard(row, writing=True))
            state = {"published": "published", "publish_uncertain": "uncertain", "publish_failed": "failed",
                     "rejected": "rejected"}.get(draft["status"], "review")
            self._save(row["id"], dynamic_status=state, dynamic_error=draft.get("error"), dynamic_retry_at=0)
        except SendLimitReached as error:
            current = self._row(row["id"])
            if current and not current["cancelled"]:
                post = dynamics._post(current["dynamic_id"]) if current["dynamic_id"] else {}
                if post.get("status") in {"pending_review", "approved"} and not post.get("attempted_at"):
                    self._save(row["id"], dynamic_retry_at=max(error.next_at, clock()), dynamic_error=public_error(error))
                else:
                    self._save(row["id"], dynamic_status="uncertain", dynamic_error=public_error(error))
        except Exception as error:
            current = self._row(row["id"])
            if current and not current["cancelled"]:
                post = dynamics._post(current["dynamic_id"]) if current["dynamic_id"] else {}
                state = "uncertain" if post.get("status") in {"publishing", "publish_uncertain"} else "failed"
                self._save(row["id"], dynamic_status=state, dynamic_error=public_error(error))

    async def _video(self, row, config, clock):
        video_at = self._video_ready_at(row)
        if video_at is None or clock() < video_at:
            return
        skill = getattr(self.service.runtime, "remotion_skill", None)
        job_id = row["video_job_id"]
        if row["video_status"] in {"submitted", "publish_uncertain"}:
            try:
                state = await skill.refresh_video_publication(job_id)
            except Exception as error:
                self._save(row["id"], video_error=public_error(error), retry_at=clock() + 300)
            else:
                self._save_video_publication(row["id"], state, clock())
            return
        if row["cancelled"]:
            return

        manual = bool(row["video_requested"])
        if not manual and clock() >= row["video_end_at"]:
            self._save(row["id"], video_status="window_missed", video_error="制作与自动投稿时段已结束")
            return
        try:
            character = self._guard(row)
            if not skill:
                raise ValueError("Remotion 视频制作服务未启动")
            job = skill.get_job(job_id) if job_id else None
            if job and job["status"] in {"published", "publish_uncertain", "publishing"}:
                self._save_video_publication(row["id"], await skill.refresh_video_publication(job_id), clock())
                return
            if job is None:
                brief = await self._brief(row, character)
                self._guard(row)
                job_id = skill.plan_operations_recap(brief, job_key=row["id"])["job_id"]
                self._save(row["id"], video_job_id=job_id)
                job = skill.get_job(job_id)
            if job["status"] not in {"rendered", "ready_for_review"}:
                if job["status"] != "project_ready":
                    await skill.generate_remotion_project({"job_id": job_id})
                render = await skill.render_remotion_video({"job_id": job_id})
                if not render.get("ok"):
                    raise ValueError(render.get("error") or "视频渲染或质检失败")
            self._guard(row)
        except Exception as error:
            current = self._row(row["id"])
            if current and not current["cancelled"]:
                self._save(row["id"], video_status="render_failed", video_error=public_error(error))
            return

        def assert_window():
            self._guard(row, writing=True)
            if not row["video_start_at"] <= clock() < row["video_end_at"]:
                raise ValueError("已离开自动投稿时段，成片保留审核")

        if config.video_mode != "automatic_publish" or manual:
            self._save(row["id"], video_status="ready", video_error=None)
        elif clock() >= row["video_end_at"]:
            self._save(row["id"], video_status="window_missed", video_error="渲染完成时自动投稿时段已结束")
        else:
            try:
                job = skill.get_job(job_id) or {}
                args = {"job_id": job_id, "publish_mode": "auto_publish"}
                if job.get("content_hash"):
                    args["expected_content_hash"] = job["content_hash"]
                result = await skill.stage_or_publish_bilibili_video(
                    args, scheduled_guard=assert_window,
                )
            except Exception as error:
                job = skill.get_job(job_id) or {}
                state = "publish_uncertain" if job.get("status") in {"publishing", "publish_uncertain"} else "publish_failed"
                self._save(row["id"], video_status=state, video_error=public_error(error), retry_at=clock() + 300)
            else:
                self._save_video_publication(row["id"], result, clock())

    def _save_video_publication(self, run_id, result, now):
        status = result.get("status")
        if status == "published":
            status = "published" if result.get("platform_state") == "public_visible" else "submitted"
        elif status == "ready_for_review":
            status = "ready"
        elif status == "publishing":
            status = "publish_uncertain"
        elif status not in {"publish_uncertain", "submitted"}:
            status = "publish_failed"
        self._save(run_id, video_status=status, video_error=result.get("error") or result.get("reason"), retry_at=now + 300)
