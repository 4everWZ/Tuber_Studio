"""Bilibili account, comments, and dynamics routes."""

import asyncio
from contextlib import aclosing
from typing import Literal
from uuid import UUID

import httpx
from fastapi import Body, Depends, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from ..core.sse import encode_event
from ..infra.inference.providers import public_error
from ..operations.channels.comments.workbench import (
    generation_task,
    list_queue,
    prepare_queue_selection,
    publish_queue_selection,
    queue_detail,
    queue_selection,
    review_queue_selection,
)


def register_operations_api(app, store, account, comments, browsing, dynamics, require_admin):
    comment_batch_lock = asyncio.Lock()

    @app.get("/api/operations/illustrations/{channel}/{identity}/{artifact_id}", dependencies=[Depends(require_admin)])
    async def illustration(channel: Literal["comments", "browsing", "dynamics", "messages"], identity: str, artifact_id: str):
        from ..operations.channels.attachments.attachments import (
            checked_image,
            decision,
            image_scope,
            row_ref,
        )

        if channel == "comments":
            row = comments._draft_source(identity)
        elif channel == "browsing":
            row = browsing.repo.decode(store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        elif channel == "messages":
            row = store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        else:
            row = store.one("SELECT * FROM dynamic_posts WHERE id=?", (identity,))
        if not row or row["account_id"] != account.display_account_id():
            raise HTTPException(404)
        ref = decision(comments.runtime, channel, row, row_ref(row))
        if not ref or ref.get("artifact_id") != artifact_id:
            raise HTTPException(404)
        image = checked_image(comments.memes, ref, row["character_id"], runtime=comments.runtime,
                              scope=image_scope(channel, row))
        return Response(image["content"], media_type=image["mime_type"], headers={"Cache-Control":"private, no-store"})

    def can_write_comments():
        return account.credentials_configured

    def workbench_scope():
        actor = browsing.runtime.character
        account_id = account.display_account_id()
        return (account_id, actor["id"], actor["version"]) if account_id else None

    @app.get("/api/account/bilibili", dependencies=[Depends(require_admin)])
    async def account_status():
        return account.status()

    @app.put("/api/account/bilibili", dependencies=[Depends(require_admin)])
    async def account_settings(data: dict = Body(...)):
        return await account.configure(data)

    @app.post("/api/account/bilibili/refresh", dependencies=[Depends(require_admin)])
    async def account_refresh():
        return await account.refresh(maintain=True)

    @app.post("/api/account/bilibili/login", dependencies=[Depends(require_admin)])
    async def start_bilibili_login():
        return await account.create_login()

    @app.get("/api/account/bilibili/login/{identity}/image", dependencies=[Depends(require_admin)])
    async def bilibili_login_image(identity: str):
        try:
            image = account.login_image(identity)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/account/bilibili/login/{identity}/poll", dependencies=[Depends(require_admin)])
    async def poll_bilibili_login(identity: str):
        return await account.poll_login(identity)

    @app.delete("/api/account/bilibili/login/{identity}", dependencies=[Depends(require_admin)])
    async def cancel_bilibili_login(identity: str):
        return await account.cancel_login(identity)

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
            return await account.adapter.popular()
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
        view: Literal["review", "verify", "processing", "history", "authorized", "blocked"] = "review",
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        video_id: str | None = None,
        comment_ids: str | None = None,
        task_id: str | None = Query(None, min_length=1, max_length=100),
    ):
        return list_queue(
            store, view, limit, offset, video_id=video_id, comment_ids=comment_ids, task_id=task_id,
            can_write=can_write_comments(), scope=workbench_scope() if not (task_id or comment_ids) else None,
        )

    @app.get("/api/comments/workbench/selection", dependencies=[Depends(require_admin)])
    async def comment_queue_selection(
        video_id: str | None = None,
        comment_ids: str | None = None,
        task_id: str | None = Query(None, min_length=1, max_length=100),
    ):
        return queue_selection(
            store, can_write=can_write_comments(), video_id=video_id, comment_ids=comment_ids, task_id=task_id,
            scope=workbench_scope() if not (task_id or comment_ids) else None,
        )

    @app.post("/api/comments/workbench/batch-review/{action}", dependencies=[Depends(require_admin)])
    async def comment_queue_batch_review(action: Literal["preview", "save", "queue"], data: dict = Body(...)):
        selection = prepare_queue_selection(store, data)
        async with comment_batch_lock:
            return await review_queue_selection(browsing, selection, action)

    @app.post("/api/comments/workbench/batch-publish", dependencies=[Depends(require_admin)])
    async def comment_queue_batch_publish(data: dict = Body(...), accept: str = Header("")):
        selection = prepare_queue_selection(store, data)
        results = [None] * len(selection)

        async def publish():
            async with comment_batch_lock:
                async for index, result in publish_queue_selection(store, comments, browsing, selection):
                    results[index] = result
                    yield result

        if "text/event-stream" in accept:
            async def stream():
                async with aclosing(publish()) as updates:
                    async for result in updates:
                        yield encode_event("progress", result)
                yield encode_event("done", {"items": results})

            return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

        async for _ in publish():
            pass
        return {"items": results}

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
        options = {k: data[k] for k in ("image_ref", "updated_at", "expected_image") if k in data}
        if "mentions" in data:
            from ..operations.channels.mentions import bind_mentions

            row = comments._draft_source(draft_id)
            if not row or data.get("updated_at") != row["updated_at"]:
                raise ValueError("草稿已变化，请刷新正文和提及")
            mentions, pending = await bind_mentions(account.adapter, data.get("text", row["text"]), data["mentions"],
                [{"uid": row["platform_user_id"], "name": row["display_name"]}])
            options["mention_selection"] = {"mentions": mentions, "pending_mentions": pending}
        return comments.review(draft_id, data.get("action"), data.get("text"), **options)

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
    async def dynamic_comment_card(post_id: int, page: int = Query(1, ge=1, le=9)):
        return Response(
            dynamics.comment_card_image(post_id, page - 1), media_type="image/png", headers={"Cache-Control": "no-store"}
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

    @app.get("/api/dynamics/materials", dependencies=[Depends(require_admin)])
    async def dynamic_materials():
        return dynamics.latest_materials()

    @app.put("/api/dynamics/memories/{memory_id}", dependencies=[Depends(require_admin)])
    async def dynamic_memory(memory_id: int, data: dict = Body(...)):
        if set(data) != {"public"} or type(data["public"]) is not bool:
            raise ValueError("请指定 public 布尔值")
        return dynamics.mark_memory(memory_id, data["public"])

    @app.post("/api/dynamics/evaluate", dependencies=[Depends(require_admin)])
    async def evaluate_dynamic(topic: str = Body("", embed=True, max_length=200)):
        return await dynamics.evaluate(manual=True, topic=topic.strip())

    @app.post("/api/dynamics/posts/{post_id}/review", dependencies=[Depends(require_admin)])
    async def review_dynamic(post_id: int, data: dict = Body(...)):
        if set(data) - {"action", "text", "meme_id", "review_key", "mentions"}:
            raise ValueError("未知动态审核字段")
        options = {key: data[key] for key in ("meme_id", "review_key") if key in data}
        if "mentions" in data:
            row = dynamics._post(post_id)
            if data.get("review_key") != row["review_key"]:
                raise ValueError("草稿已变化，请刷新正文和提及")
            options["mention_selection"] = await dynamics.resolve_review_mentions(
                post_id, data.get("text", row["content"]), data["mentions"]
            )
        return dynamics.review(post_id, data.get("action"), data.get("text"), **options)

    @app.post("/api/dynamics/posts/{post_id}/publish", dependencies=[Depends(require_admin)])
    async def publish_dynamic(post_id: int, data: dict = Body(default={})):
        expected = None
        if data:
            if set(data) - {"text", "review_key"} or not isinstance(data.get("text"), str):
                raise ValueError("仅可提交当前动态正文")
            row = dynamics._post(post_id)
            if row["status"] in {"pending_review", "approved"} and not row.get("attempted_at"):
                if ("review_key" in data or row["illustration"] or row["meme_id"] or row["comment_card"]
                        or row["mentions"] or row["pending_mentions"]) and data.get("review_key") != row["review_key"]:
                    raise ValueError("正文、账号或配图已变化，请查看当前草稿后重新确认")
                expected = row["content"] if row["status"] == "approved" and data["text"] == row["content"] else (
                    dynamics.review(post_id, "approve", data["text"])["content"]
                )
            elif row["status"] == "published":
                return row
        return await dynamics.publish(post_id, expected_text=expected) if expected else await dynamics.publish(post_id)

    @app.post("/api/dynamics/edits/{request_id}/apply", dependencies=[Depends(require_admin)])
    async def apply_dynamic_edit(request_id: UUID):
        edit = await dynamics.apply_dynamic_edit(str(request_id))
        return {key: edit[key] for key in ("request_id", "platform_id", "status", "error")}
