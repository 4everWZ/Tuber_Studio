"""Authenticated activity API for the operator's persistent Agent tasks."""

import time
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import Field, field_validator

from ..models import StrictModel


class TaskCreate(StrictModel):
    request_id: UUID
    goal: str = Field(min_length=1, max_length=2000)
    run_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("goal")
    @classmethod
    def non_empty(cls, value):
        if not value.strip():
            raise ValueError("请填写任务目标")
        return value.strip()


class TaskControl(StrictModel):
    revision: int = Field(ge=1, strict=True)
    action: Literal["steer", "cancel", "approve"]
    goal: str | None = Field(default=None, max_length=2000)
    step_id: UUID | None = None


def register_task_api(app, tasks, require_admin):
    deps = [Depends(require_admin)]

    @app.get("/api/agent/tasks", dependencies=deps)
    async def activity():
        return {"tasks": tasks.activity()}

    @app.post("/api/agent/tasks", dependencies=deps)
    async def create(body: TaskCreate):
        if body.run_at is not None and body.run_at > time.time() + 366 * 86400:
            raise HTTPException(400, "预约时间须在一年内")
        try:
            return tasks.view(tasks.create(str(body.request_id), body.goal, due=body.run_at))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/agent/tasks/{identity}", dependencies=deps)
    async def get(identity: UUID):
        task = tasks.current(str(identity))
        if not task:
            raise HTTPException(404, "找不到这项任务")
        return tasks.view(task)

    @app.patch("/api/agent/tasks/{identity}", dependencies=deps)
    async def control(identity: UUID, body: TaskControl):
        goal = (body.goal or "").strip()
        if body.action == "steer" and not goal:
            raise HTTPException(400, "请填写新的任务要求")
        try:
            task = await tasks.control(str(identity), body.revision, body.action, goal,
                                       str(body.step_id) if body.step_id else None)
        except LookupError as error:
            raise HTTPException(404, "找不到这项任务") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return tasks.view(task)
