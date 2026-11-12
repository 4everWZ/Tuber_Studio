"""Bilibili account, comments, and dynamics routes."""

import asyncio
from typing import Literal

import httpx
from fastapi import Body, Depends, HTTPException, Query
from fastapi.responses import Response

from ..comment_workbench import (
    generation_task,
    list_queue,
    publish_queue_selection,
    queue_detail,
    queue_selection,
)
from ..providers import public_error


def register_operations_api(app, store, account, comments, browsing, dynamics, require_admin):
    comment_batch_lock = asyncio.Lock()

    def can_write_comments():
        return comments.policy()["credentials_configured"]

    @app.get("/api/account/bilibili", dependencies=[Depends(require_admin)])
    async def account_status():
        return account.status()

    @app.put("/api/account/bilibili", dependencies=[Depends(require_admin)])
    async def account_settings(data: dict = Body(...)):
        return await account.configure(data)

    @app.post("/api/account/bilibili/refresh", dependencies=[Depends(require_admin)])
    async def account_refresh():
        return await account.refresh()

    @app.get("/api/account/bilibili/avatar", dependencies=[Depends(require_admin)])
    async def account_avatar():
        try:
            image, mime = await account.avatar_image()
        except (ValueError, httpx.HTTPError):
            raise HTTPException(404, "账号头像暂时不可用") from None
        return Response(image, media_type=mime)

    @app.get("/api/comments/videos", dependencies=[Depends(require_admin)])
    async def videos():
        try:
            return await comments.adapter.popular()
        except Exception as error:
            raise ValueError(public_error(error)) from None

    @app.post("/api/comments/fetch", dependencies=[Depends(require_admin)])
    async def fetch(data: dict = Body(...)):
        try:
            return await comments.fetch(data.get("video", data.get("video_id", "")), data.get("max_pages", 1))
        except Exception as error:
            raise ValueError(public_error(error)) from None

    @app.post("/api/comments/import", dependencies=[Depends(require_admin)])
    async def import_comments(data: dict = Body(...)):
        return comments.import_items(
            data.get("video_id", ""), data.get("items"), data.get("csv_text"), data.get("field_mapping")
        )

    @app.post("/api/comments/import/preview", dependencies=[Depends(require_admin)])
    async def preview_import(data: dict = Body(...)):
        prepared = comments.prepare_import(
            data.get("video_id", ""), data.get("items"), data.get("csv_text"), data.get("field_mapping")
        )
        return {"video_id": data.get("video_id"), "total": len(prepared), "items": prepared[:10]}

    @app.put("/api/comments/{comment_id}/category", dependencies=[Depends(require_admin)])
    async def edit_category(comment_id: int, data: dict = Body(...)):
        return comments.set_category(comment_id, data.get("category"))

    @app.get("/api/comments", dependencies=[Depends(require_admin)])
    async def list_comments(
        video_id: str | None = None,
        thread_id: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ):
        return comments.list_comments(video_id, category, status, limit, thread_id=thread_id)

    @app.post("/api/comments/analyze", dependencies=[Depends(require_admin)])
    async def analyze(data: dict = Body(default={})):
        return comments.analyze(data.get("video_id"))

    @app.get("/api/comments/workbench", dependencies=[Depends(require_admin)])
    async def comment_queue(
        view: Literal["review", "verify", "processing", "history"] = "review",
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        video_id: str | None = None,
        comment_ids: str | None = None,
        task_id: str | None = Query(None, min_length=1, max_length=100),
    ):
        return list_queue(
            store, view, limit, offset, video_id=video_id, comment_ids=comment_ids, task_id=task_id,
            can_write=can_write_comments(),
        )

    @app.get("/api/comments/workbench/selection", dependencies=[Depends(require_admin)])
    async def comment_queue_selection(
        video_id: str | None = None,
        comment_ids: str | None = None,
        task_id: str | None = Query(None, min_length=1, max_length=100),
    ):
        return queue_selection(
            store, can_write=can_write_comments(), video_id=video_id, comment_ids=comment_ids, task_id=task_id,
        )

    @app.post("/api/comments/workbench/batch-publish", dependencies=[Depends(require_admin)])
    async def comment_queue_batch_publish(data: dict = Body(...)):
        async with comment_batch_lock:
            return await publish_queue_selection(store, comments, browsing, data)

    @app.get("/api/comments/workbench/{channel}/{identity}", dependencies=[Depends(require_admin)])
    async def comment_queue_detail(channel: Literal["comments", "browsing", "jobs"], identity: str):
        row = queue_detail(store, comments, browsing, channel, identity)
        if not row:
            raise HTTPException(404, "记录不存在")
        return row

    @app.post("/api/comments/drafts", dependencies=[Depends(require_admin)])
    async def drafts(data: dict = Body(...)):
        return comments.enqueue(data.get("comment_ids", []))

    @app.get("/api/comment-tasks", dependencies=[Depends(require_admin)])
    async def tasks(task_id: str | None = Query(None, min_length=1, max_length=100)):
        if task_id:
            row = generation_task(store, task_id, can_write=can_write_comments())
            return [row] if row else []
        return [
            generation_task(store, row["id"], can_write=can_write_comments())
            for row in store.all("SELECT id FROM comment_tasks ORDER BY created_at DESC LIMIT 50")
        ]

    @app.post("/api/comments/drafts/{draft_id}/review", dependencies=[Depends(require_admin)])
    async def review(draft_id: int, data: dict = Body(...)):
        return comments.review(draft_id, data.get("action"), data.get("text"))

    @app.post("/api/comments/drafts/{draft_id}/copy", dependencies=[Depends(require_admin)])
    async def copy(draft_id: int):
        return comments.copy(draft_id)

    @app.post("/api/comments/drafts/{draft_id}/publish", dependencies=[Depends(require_admin)])
    async def publish(draft_id: int):
        return await comments.publish(draft_id)

    @app.get("/api/dynamics/policy", dependencies=[Depends(require_admin)])
    async def dynamic_policy():
        return dynamics.policy()

    @app.put("/api/dynamics/policy", dependencies=[Depends(require_admin)])
    async def set_dynamic_policy(data: dict = Body(...)):
        return await dynamics.set_policy(data)

    @app.get("/api/dynamics/posts", dependencies=[Depends(require_admin)])
    async def dynamic_posts(status: str | None = None, limit: int = Query(50, ge=1, le=100)):
        return dynamics.list_posts(status=status, limit=limit)

    @app.get("/api/dynamics/posts/{post_id}/comment-card", dependencies=[Depends(require_admin)])
    async def dynamic_comment_card(post_id: int):
        return Response(
            dynamics.comment_card_image(post_id), media_type="image/png", headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/dynamics/decisions", dependencies=[Depends(require_admin)])
    async def dynamic_decisions(limit: int = Query(50, ge=1, le=100)):
        return dynamics.list_decisions(limit=limit)

    @app.get("/api/dynamics/memories", dependencies=[Depends(require_admin)])
    async def dynamic_memories(limit: int = Query(100, ge=1, le=200)):
        return dynamics.list_memories(limit=limit)

    @app.get("/api/dynamics/memory-summary", dependencies=[Depends(require_admin)])
    async def dynamic_memory_summary():
        return dynamics.summary_status()

    @app.put("/api/dynamics/memories/{memory_id}", dependencies=[Depends(require_admin)])
    async def dynamic_memory(memory_id: int, data: dict = Body(...)):
        if set(data) != {"public"} or type(data["public"]) is not bool:
            raise ValueError("请指定 public 布尔值")
        return dynamics.mark_memory(memory_id, data["public"])

    @app.post("/api/dynamics/evaluate", dependencies=[Depends(require_admin)])
    async def evaluate_dynamic():
        return await dynamics.evaluate(manual=True)

    @app.post("/api/dynamics/posts/{post_id}/review", dependencies=[Depends(require_admin)])
    async def review_dynamic(post_id: int, data: dict = Body(...)):
        if set(data) - {"action", "text", "meme_id"}:
            raise ValueError("未知动态审核字段")
        options = {"meme_id": data["meme_id"]} if "meme_id" in data else {}
        return dynamics.review(post_id, data.get("action"), data.get("text"), **options)

    @app.post("/api/dynamics/posts/{post_id}/publish", dependencies=[Depends(require_admin)])
    async def publish_dynamic(post_id: int, data: dict = Body(default={})):
        expected = None
        if data:
            if set(data) != {"text"} or not isinstance(data["text"], str):
                raise ValueError("仅可提交当前动态正文")
            row = dynamics._post(post_id)
            if row["status"] == "published":
                return row
            if row["status"] in {"pending_review", "approved"} and not row.get("attempted_at"):
                expected = dynamics.review(post_id, "approve", data["text"])["content"]
        return await dynamics.publish(post_id, expected_text=expected) if expected else await dynamics.publish(post_id)
