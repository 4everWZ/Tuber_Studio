"""Authenticated activity API for the operator's persistent Agent tasks."""

import time
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field, PositiveInt, field_validator

from ..models import StrictModel
from ..store import dump
from .goal_events import register_goal_events
from .goal_followups import register_goal_followups
from .operator_chat import VideoSelection
from .private_browser import register_private_browser
from .private_code import CodeFile, run_code
from .public_video import clip_path
from .task_runtime import Tool


class TextAttachment(StrictModel):
    name: str = Field(min_length=1, max_length=160, pattern=r"^[^/\\\x00-\x1f]+\.(?:md|txt|csv|json|srt|vtt)$")
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
    allow_code: bool = Field(default=False, strict=True)
    browser_sources: list[str] | None = Field(default=None, min_length=1, max_length=6)
    video_selection: VideoSelection | None = None

    @field_validator("goal")
    @classmethod
    def non_empty(cls, value):
        if not value.strip():
            raise ValueError("请填写任务目标")
        return value.strip()


class TaskControl(StrictModel):
    revision: int = Field(ge=1, strict=True)
    action: Literal["steer", "cancel", "approve", "pause", "resume"]
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
    register_private_browser(app, tasks, require_admin)
    tasks.register("private_code", Tool(
        "在获准的隔离 QuickJS 中变换私有附件。script 是同步函数体，files 为 {name,text} 数组，"
        "返回完整文件字符串并指定输出文件名。没有网络、磁盘或宿主命令。", CodeFile, run_code,
        responses_only=True,
    ))
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
        artifact = tasks.workspace.edition_view(task, tasks.workspace.latest(task["id"]))
        return tasks.view(task) | {"creative_goal": project, "artifact": artifact,
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
                                       video_selection=body.video_selection, allow_code=body.allow_code,
                                       browser_sources=body.browser_sources))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/agent/tasks/{identity}", dependencies=deps)
    async def get(identity: UUID):
        return detail(visible(identity))

    @app.get("/api/agent/tasks/{identity}/artifacts", dependencies=deps)
    async def artifact_history(identity: UUID):
        task = visible(identity)
        rows = tasks.store.all(
            "SELECT task_revision,edition,author,created FROM creative_editions WHERE task_id=? "
            "ORDER BY task_revision DESC,edition DESC", (task["id"],),
        )
        return [tasks.workspace.edition_view(task, row) for row in rows]

    @app.get("/api/agent/tasks/{identity}/clips/{step_id}", dependencies=deps)
    async def read_clip(identity: UUID, step_id: UUID):
        task = visible(identity)
        video_steps = {step["id"]: step for step in tasks.view(task)["steps"] if step["tool"] == "inspect_video"}
        step = video_steps.get(str(step_id), {})
        receipt = step.get("result") or {}
        # A resumed or reused read keeps the original request's media file.
        media_id = receipt.get("request_id", str(step_id))
        if not receipt.get("clip") or media_id not in video_steps:
            raise HTTPException(404, "此任务没有这段已核实的画面")
        path = clip_path(app.state.runtime.settings.runtime, media_id)
        if not path.is_file():
            raise HTTPException(404, "此工作区未保留这段短片文件")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    @app.get("/api/agent/tasks/{identity}/artifacts/{revision}/{edition}", dependencies=deps)
    async def read_artifact(identity: UUID, revision: PositiveInt, edition: PositiveInt):
        task = visible(identity)
        result = tasks.store.one("SELECT * FROM creative_editions WHERE task_id=? AND task_revision=? AND edition=?",
                                 (task["id"], revision, edition))
        if result is None:
            raise HTTPException(404, "找不到这个成果版本")
        return tasks.workspace.edition_view(task, result)

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
