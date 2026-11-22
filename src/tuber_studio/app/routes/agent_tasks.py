"""L4 agent task routes (`/api/v1/agent/attention`, `/api/v1/agent/tasks/*`, browser, and goals)."""

from __future__ import annotations

import time
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from ...agent.api.tasks import (
    EditionEdit,
    GoalStop,
    TaskControl,
    TaskCreate,
    attach_workbench_task_extensions,
)
from ...agent.chat.lookup.dynamic_publication import approval_image
from ...agent.creative.private_browser import BrowserGrant, Command, Control
from ...agent.goals.comment_events import CommentWatch
from ...agent.goals.events import EventGrant
from ...agent.goals.followups import FollowupGrant
from ...agent.tasks.public_video import clip_path
from ...core.store import dump
from .system import ERROR_RESPONSES


def register_task_routes(app, tasks, require_admin) -> None:
    """Register `/api/v1/agent/attention` and `/api/v1/agent/tasks/*` routes onto FastAPI."""
    attach_workbench_task_extensions(tasks)
    deps = [Depends(require_admin)]
    prefix = "/api/v1/agent"

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
                f"WHERE creative_goal_id=? AND scope IN {scopes} ORDER BY created,id",
                (root, *params),
            )
            project["stop_scope"] = {
                run["id"]: run["revision"]
                for run in runs
                if not project["closed"]
                and run["status"] not in {"completed", "failed", "cancelled"}
                and run["scope"] == scope
            }
            project["runs"] = [{key: run[key] for key in ("id", "goal", "status", "revision")} for run in runs]
        notice = tasks.store.one("SELECT updated FROM creative_attention WHERE task_id=?", (task["id"],))
        artifact = tasks.workspace.edition_view(task, tasks.workspace.latest(task["id"]))
        if artifact and not tasks.recall_edition_available(task["id"], artifact["task_revision"]):
            artifact = None
        return tasks.view(task) | {
            "creative_goal": project,
            "artifact": artifact,
            "can_control": tasks.can_control(task) and not bool(project and project["closed"]),
            "attention": notice["updated"] if notice else None,
        }

    def notices():
        scopes, params = tasks.visible_scopes()
        return tasks.store.all(
            "SELECT t.id,t.goal,t.status FROM creative_attention a JOIN agent_tasks t ON t.id=a.task_id "
            f"WHERE t.scope IN {scopes} ORDER BY a.updated DESC",
            params,
        )

    def browser_call(fn, *args):
        try:
            return fn(*args)
        except LookupError:
            raise HTTPException(404, "找不到当前授权下的浏览任务") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get(prefix + "/attention", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def attention():
        scopes, params = tasks.visible_scopes()
        return tasks.store.one(
            f"SELECT count(*) AS count FROM creative_attention a JOIN agent_tasks t ON t.id=a.task_id WHERE t.scope IN {scopes}",
            params,
        )

    @app.delete(prefix + "/tasks/{identity}/attention", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def acknowledge(identity: UUID, updated: float):
        visible(identity)
        tasks.store.execute("DELETE FROM creative_attention WHERE task_id=? AND updated=?", (str(identity), updated))
        return {"ok": True}

    @app.get(prefix + "/tasks", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def activity():
        scopes, params = tasks.visible_scopes()
        goals = tasks.store.all(
            "SELECT g.id,g.title,g.closed FROM creative_goals g JOIN agent_tasks t ON t.id=g.id "
            f"WHERE t.scope IN {scopes} ORDER BY g.rowid DESC",
            params,
        )
        return {"tasks": tasks.activity(), "goals": goals, "attention": notices()}

    @app.post(prefix + "/tasks", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
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
            return detail(
                tasks.create(
                    str(body.request_id),
                    body.goal,
                    due=body.run_at,
                    creative_goal_id=root,
                    source_key=["goal", root] if root else None,
                    attachments=[item.model_dump() for item in body.attachments],
                    video_selection=body.video_selection,
                    allow_code=body.allow_code,
                    browser_sources=body.browser_sources,
                )
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get(prefix + "/tasks/{identity}", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def get(identity: UUID):
        return detail(visible(identity))

    @app.get(
        prefix + "/tasks/{identity}/approval/{step_id}/dynamics/{post_id}/images/{index}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def get_approval_image(identity: UUID, step_id: UUID, post_id: int, index: int):
        if post_id < 1:
            raise HTTPException(422, "post_id 须为正整数")
        task = visible(identity)
        try:
            if not tasks.can_control(task):
                raise LookupError(post_id)
            image = approval_image(app.state.dynamics, task, str(step_id), post_id, index)
        except (ValueError, LookupError):
            raise HTTPException(404, "这张待确认配图已变化") from None
        return Response(image["content"], media_type=image["mime_type"], headers={"Cache-Control": "private, no-store"})

    @app.get(prefix + "/tasks/{identity}/artifacts", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def artifact_history(identity: UUID):
        task = visible(identity)
        rows = tasks.store.all(
            "SELECT task_revision,edition,author,created FROM creative_editions WHERE task_id=? "
            "ORDER BY task_revision DESC,edition DESC",
            (task["id"],),
        )
        return [tasks.workspace.edition_view(task, row) for row in rows]

    @app.get(prefix + "/tasks/{identity}/clips/{step_id}", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
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

    @app.get(
        prefix + "/tasks/{identity}/artifacts/{revision}/{edition}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def read_artifact(identity: UUID, revision: int, edition: int, download: bool = False):
        if revision < 1 or edition < 1:
            raise HTTPException(422, "版本与版次须为正整数")
        task = visible(identity)
        result = tasks.store.one(
            "SELECT * FROM creative_editions WHERE task_id=? AND task_revision=? AND edition=?",
            (task["id"], revision, edition),
        )
        if result is None or not tasks.recall_edition_available(task["id"], revision):
            raise HTTPException(404, "找不到这个成果版本")
        if download:
            if not tasks.edition_recallable(task["id"], revision):
                raise HTTPException(404, "这个成果版本当前不可下载")
            return Response(
                result["body"],
                media_type="text/markdown",
                headers={
                    "Content-Disposition": f'attachment; filename="studio-{identity}-r{revision}-v{edition}.md"',
                    "Cache-Control": "private, no-store",
                },
            )
        return tasks.workspace.edition_view(task, result)

    @app.put(prefix + "/tasks/{identity}/goal", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def promote(identity: UUID):
        task = visible(identity)
        try:
            tasks.workspace.promote(task)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(visible(identity))

    @app.post(prefix + "/tasks/{identity}/goal/close", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def close_goal(identity: UUID, body: GoalStop | None = None):
        visible(identity)
        if not tasks.workspace.goal(str(identity)):
            raise HTTPException(404, "找不到这个长期目标")
        try:
            await tasks.close_goal(
                str(identity),
                {str(key): rev for key, rev in body.confirmed_runs.items()} if body else {},
            )
        except LookupError as error:
            raise HTTPException(404, "找不到这个长期目标") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(visible(identity))

    @app.patch(prefix + "/tasks/{identity}/artifact", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def edit_artifact(identity: UUID, body: EditionEdit):
        task = visible(identity)
        if not body.body.strip():
            raise HTTPException(400, "成果内容不能为空")
        try:
            tasks.workspace.edit(task, body.task_revision, body.edition, body.body.strip())
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(task)

    @app.patch(prefix + "/tasks/{identity}", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def control(identity: UUID, body: TaskControl):
        goal = (body.goal or "").strip()
        if body.action == "steer" and not goal:
            raise HTTPException(400, "请填写新的任务要求")
        if body.action != "steer" and body.video_selection is not None:
            raise HTTPException(400, "只有改口可以更换视频范围")
        if body.action != "approve" and body.selected_ids is not None:
            raise HTTPException(400, "只有确认可以提交选择清单")
        try:
            task = await tasks.control(
                str(identity),
                body.revision,
                body.action,
                goal,
                str(body.step_id) if body.step_id else None,
                video_selection=body.video_selection,
                selected_ids=body.selected_ids,
            )
        except LookupError as error:
            raise HTTPException(404, "找不到这项任务") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return detail(task)

    # --- Private Browser (4) ---
    browser_path = prefix + "/tasks/{identity}/browser"

    @app.get(browser_path, dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def browser_state(identity: UUID):
        return browser_call(tasks.private_browser.state, str(identity))

    @app.put(browser_path, dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def grant_browser(identity: UUID, body: BrowserGrant):
        return browser_call(tasks.private_browser.grant, str(identity), body)

    @app.patch(browser_path, dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def control_browser(identity: UUID, body: Control):
        return browser_call(tasks.private_browser.control, str(identity), body)

    @app.post(browser_path + "/actions", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def interact_browser(identity: UUID, body: Command):
        try:
            await tasks.private_browser.turn(str(identity), body, "owner")
            return tasks.private_browser.state(str(identity))
        except LookupError:
            raise HTTPException(404, "找不到当前授权下的浏览任务") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    # --- Goal Events, Comment Events, and Followups (9) ---
    @app.get(prefix + "/tasks/{identity}/goal/events", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def event_watch(identity: UUID):
        return tasks.goal_events.get(str(identity))

    @app.put(prefix + "/tasks/{identity}/goal/events", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def configure_events(identity: UUID, body: EventGrant):
        return tasks.goal_events.configure(str(identity), body)

    @app.delete(prefix + "/tasks/{identity}/goal/events", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def disable_events(identity: UUID, revision: int = Query(ge=0)):
        return tasks.goal_events.disable(str(identity), revision)

    @app.get(
        prefix + "/tasks/{identity}/goal/comment-events",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def comment_watch(identity: UUID):
        return tasks.goal_events.comments.get(str(identity))

    @app.put(
        prefix + "/tasks/{identity}/goal/comment-events",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def configure_comment_watch(identity: UUID, body: CommentWatch):
        return tasks.goal_events.comments.configure(str(identity), body)

    @app.delete(
        prefix + "/tasks/{identity}/goal/comment-events",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["agent"],
    )
    async def disable_comment_watch(identity: UUID, revision: int = Query(ge=0)):
        return tasks.goal_events.comments.disable(str(identity), revision)

    @app.get(prefix + "/tasks/{identity}/goal/followup", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def get_followup(identity: UUID):
        return tasks.followups.get(str(identity))

    @app.put(prefix + "/tasks/{identity}/goal/followup", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def set_followup(identity: UUID, body: FollowupGrant):
        return tasks.followups.configure(str(identity), body, time.time())

    @app.delete(prefix + "/tasks/{identity}/goal/followup", dependencies=deps, responses=ERROR_RESPONSES, tags=["agent"])
    async def disable_followup(identity: UUID):
        return tasks.followups.disable(str(identity))
