"""Authenticated activity API for the operator's persistent Agent tasks."""

import time
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import Field, PositiveInt, field_validator

from ..models import StrictModel
from ..store import dump
from .goal_events import register_goal_events
from .goal_followups import register_goal_followups
from .operator_chat import VideoSelection


class TextAttachment(StrictModel):
    name: str = Field(min_length=1, max_length=160, pattern=r"^[^/\\\x00-\x1f]+\.(?:md|txt)$")
    text: str = Field(min_length=1, max_length=8000)

    @field_validator("text")
    @classmethod
    def readable(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("附件必须是非空文本")
        return value


class TaskCreate(StrictModel):
    request_id: UUID
    goal: str = Field(min_length=1, max_length=2000)
    run_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    creative_goal_id: UUID | None = None
    attachments: list[TextAttachment] = Field(default_factory=list, max_length=3)
    video_selection: VideoSelection | None = None

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
    video_selection: VideoSelection | None = None


class EditionEdit(StrictModel):
    task_revision: int = Field(ge=1)
    edition: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=64000)


class GoalStop(StrictModel):
    confirmed_runs: dict[UUID, PositiveInt]


def register_task_api(app, tasks, require_admin):
    register_goal_followups(app, tasks, require_admin)
    register_goal_events(app, tasks, require_admin)
    deps = [Depends(require_admin)]

    def visible(identity):
        task = tasks.current(str(identity))
        if not task:
            raise HTTPException(404, "找不到这项任务")
        return task

    def detail(task):
        root = task.get("creative_goal_id")
        project = tasks.workspace.goal(root) if root and tasks.current(root) else None
        if project:
            scope = dump(tasks.scope())
            scopes, params = tasks.visible_scopes()
            runs = tasks.store.all(
                "SELECT id,goal,status,revision,scope FROM agent_tasks "
                f"WHERE creative_goal_id=? AND scope IN {scopes} ORDER BY created,id", (root, *params),
            )
            project["stop_scope"] = {
                run["id"]: run["revision"] for run in runs
                if not project["closed"] and run["status"] not in {"completed", "failed", "cancelled"}
                and run["scope"] == scope
            }
            project["runs"] = [{key: run[key] for key in ("id", "goal", "status", "revision")} for run in runs]
        notice = tasks.store.one("SELECT updated FROM creative_attention WHERE task_id=?", (task["id"],))
        return tasks.view(task) | {"creative_goal": project, "artifact": tasks.workspace.latest(task["id"]),
                                   "can_control": tasks.can_control(task) and not bool(project and project["closed"]),
                                   "attention": notice["updated"] if notice else None}

    def notices():
        scopes, params = tasks.visible_scopes()
        return tasks.store.all(
            "SELECT t.id,t.goal,t.status FROM creative_attention a JOIN agent_tasks t ON t.id=a.task_id "
            f"WHERE t.scope IN {scopes} ORDER BY a.updated DESC", params,
        )

    @app.get("/api/agent/attention", dependencies=deps)
    async def attention():
        scopes, params = tasks.visible_scopes()
        return tasks.store.one("SELECT count(*) AS count FROM creative_attention a JOIN agent_tasks t ON t.id=a.task_id "
                               f"WHERE t.scope IN {scopes}", params)

    @app.delete("/api/agent/tasks/{identity}/attention", dependencies=deps)
    async def acknowledge(identity: UUID, updated: float):
        visible(identity)
        tasks.store.execute("DELETE FROM creative_attention WHERE task_id=? AND updated=?", (str(identity), updated))
        return {"ok": True}

    @app.get("/api/agent/tasks", dependencies=deps)
    async def activity():
        scopes, params = tasks.visible_scopes()
        goals = tasks.store.all(
            "SELECT g.id,g.title,g.closed FROM creative_goals g JOIN agent_tasks t ON t.id=g.id "
            f"WHERE t.scope IN {scopes} ORDER BY g.rowid DESC", params,
        )
        return {"tasks": tasks.activity(), "goals": goals, "attention": notices()}

    @app.post("/api/agent/tasks", dependencies=deps)
    async def create(body: TaskCreate):
        if body.run_at is not None and body.run_at > time.time() + 366 * 86400:
            raise HTTPException(400, "预约时间须在一年内")
        try:
            root = str(body.creative_goal_id) if body.creative_goal_id else None
            if root:
                visible(root)
                project = tasks.workspace.goal(root)
                if not project:
                    raise HTTPException(404, "找不到这个长期目标")
                if project["closed"]:
                    raise ValueError("目标已经结束")
            return detail(tasks.create(str(body.request_id), body.goal, due=body.run_at,
                                       creative_goal_id=root, source_key=["goal", root] if root else None,
                                       attachments=[item.model_dump() for item in body.attachments],
                                       video_selection=body.video_selection))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/agent/tasks/{identity}", dependencies=deps)
    async def get(identity: UUID):
        return detail(visible(identity))

    @app.put("/api/agent/tasks/{identity}/goal", dependencies=deps)
    async def promote(identity: UUID):
        task = visible(identity)
        try:
            tasks.workspace.promote(task)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(visible(identity))

    @app.post("/api/agent/tasks/{identity}/goal/close", dependencies=deps)
    async def close_goal(identity: UUID, body: GoalStop | None = None):
        visible(identity)
        if not tasks.workspace.goal(str(identity)):
            raise HTTPException(404, "找不到这个长期目标")
        try:
            await tasks.close_goal(str(identity), {str(key): rev for key, rev in body.confirmed_runs.items()} if body else {})
        except LookupError as error:
            raise HTTPException(404, "找不到这个长期目标") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(visible(identity))

    @app.patch("/api/agent/tasks/{identity}/artifact", dependencies=deps)
    async def edit_artifact(identity: UUID, body: EditionEdit):
        task = visible(identity)
        if not body.body.strip():
            raise HTTPException(400, "成果内容不能为空")
        try:
            tasks.workspace.edit(task, body.task_revision, body.edition, body.body.strip())
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(task)

    @app.patch("/api/agent/tasks/{identity}", dependencies=deps)
    async def control(identity: UUID, body: TaskControl):
        goal = (body.goal or "").strip()
        if body.action == "steer" and not goal:
            raise HTTPException(400, "请填写新的任务要求")
        if body.action != "steer" and body.video_selection is not None:
            raise HTTPException(400, "只有改口可以更换视频范围")
        try:
            task = await tasks.control(str(identity), body.revision, body.action, goal,
                                       str(body.step_id) if body.step_id else None, video_selection=body.video_selection)
        except LookupError as error:
            raise HTTPException(404, "找不到这项任务") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(task)
