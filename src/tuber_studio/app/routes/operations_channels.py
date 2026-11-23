"""L4 operations channel routes: Bilibili account, comments workbench, dynamics, and direct messages."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import Body, Depends, Header, HTTPException, Path, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import Field

from ...core.models import StrictModel
from ...core.sse import encode_event
from ...infra.inference.providers import public_error
from ...operations.channels.comments.workbench import (
    generation_task,
    list_queue,
    prepare_queue_selection,
    publish_queue_selection,
    queue_detail,
    queue_selection,
    review_queue_selection,
)
from ...operations.channels.direct_messages import MessagePolicy
from ...operations.channels.mentions import bind_mentions
from .system import ERROR_RESPONSES


class ReviewMessage(StrictModel):
    action: Literal["approve", "reject", "edit", "copy", "reselect_image"]
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    meme_id: str | None = Field(default=None, pattern=r"^meme_[a-f0-9]{32}$")
    image_ref: dict | None = None
    updated_at: float | None = None
    expected_image: dict | None = None


def register_operations_channel_routes(app, store, account, comments, browsing, dynamics, messages, require_admin) -> None:
    """Register `/api/v1/operations/{account,comments,dynamics,messages}/*` channel routes."""
    deps = [Depends(require_admin)]
    prefix = "/api/v1/operations"
    comment_batch_lock = asyncio.Lock()

    def can_write_comments() -> bool:
        return account.credentials_configured

    def workbench_scope():
        actor = browsing.runtime.character
        account_id = account.display_account_id()
        return (account_id, actor["id"], actor["version"]) if account_id else None

    # --- Bilibili Account (8) ---
    @app.get(prefix + "/account/bilibili", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def account_status():
        return account.status()

    @app.put(prefix + "/account/bilibili", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def account_settings(data: dict = Body(...)):
        return await account.configure(data)

    @app.post(prefix + "/account/bilibili/refresh", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def account_refresh():
        return await account.refresh(maintain=True)

    @app.post(prefix + "/account/bilibili/login", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def start_bilibili_login():
        return await account.create_login()

    @app.get(
        prefix + "/account/bilibili/login/{identity}/image",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def bilibili_login_image(identity: str):
        try:
            image = account.login_image(identity)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post(
        prefix + "/account/bilibili/login/{identity}/poll",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def poll_bilibili_login(identity: str):
        return await account.poll_login(identity)

    @app.delete(
        prefix + "/account/bilibili/login/{identity}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def cancel_bilibili_login(identity: str):
        return await account.cancel_login(identity)

    @app.get(prefix + "/account/bilibili/avatar", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def account_avatar():
        try:
            image, mime = await account.avatar_image()
        except (ValueError, httpx.HTTPError):
            raise HTTPException(404, "账号头像暂时不可用") from None
        return Response(image, media_type=mime)

    # --- Comments & Workbench (17) ---
    @app.get(prefix + "/comments/videos", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def videos():
        try:
            return await account.adapter.popular()
        except Exception as error:
            raise ValueError(public_error(error)) from None

    @app.post(prefix + "/comments/fetch", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def fetch(data: dict = Body(...)):
        try:
            return await comments.fetch(data.get("video", data.get("video_id", "")), data.get("max_pages", 1))
        except Exception as error:
            raise ValueError(public_error(error)) from None

    @app.post(prefix + "/comments/import", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def import_comments(data: dict = Body(...)):
        return comments.import_items(
            data.get("video_id", ""), data.get("items"), data.get("csv_text"), data.get("field_mapping")
        )

    @app.post(prefix + "/comments/import/preview", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def preview_import(data: dict = Body(...)):
        prepared = comments.prepare_import(
            data.get("video_id", ""), data.get("items"), data.get("csv_text"), data.get("field_mapping")
        )
        return {"video_id": data.get("video_id"), "total": len(prepared), "items": prepared[:10]}

    @app.put(prefix + "/comments/{comment_id}/category", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def edit_category(comment_id: int, data: dict = Body(...)):
        return comments.set_category(comment_id, data.get("category"))

    @app.get(prefix + "/comments", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def list_comments(
        video_id: str | None = None,
        thread_id: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ):
        return comments.list_comments(video_id, category, status, limit, thread_id=thread_id)

    @app.post(prefix + "/comments/analyze", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def analyze(data: dict = Body(default={})):
        return comments.analyze(data.get("video_id"))

    @app.get(prefix + "/comments/workbench", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def comment_queue(
        view: Literal["review", "verify", "processing", "history", "authorized", "blocked"] = "review",
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        video_id: str | None = None,
        comment_ids: str | None = None,
        task_id: str | None = Query(None, min_length=1, max_length=100),
    ):
        return list_queue(
            store,
            view,
            limit,
            offset,
            video_id=video_id,
            comment_ids=comment_ids,
            task_id=task_id,
            can_write=can_write_comments(),
            scope=workbench_scope() if not (task_id or comment_ids) else None,
        )

    @app.get(prefix + "/comments/workbench/selection", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def comment_queue_selection(
        video_id: str | None = None,
        comment_ids: str | None = None,
        task_id: str | None = Query(None, min_length=1, max_length=100),
        include_unpublishable: bool = False,
        errored_only: bool = False,
    ):
        return queue_selection(
            store,
            can_write=can_write_comments(),
            include_unpublishable=include_unpublishable,
            errored_only=errored_only,
            video_id=video_id,
            comment_ids=comment_ids,
            task_id=task_id,
            scope=workbench_scope() if not (task_id or comment_ids) else None,
        )

    @app.post(
        prefix + "/comments/workbench/batch-review/{action}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def comment_queue_batch_review(action: Literal["preview", "save", "queue", "reject"], data: dict = Body(...)):
        selection = prepare_queue_selection(store, data)
        async with comment_batch_lock:
            return await review_queue_selection(browsing, selection, action)

    @app.post(
        prefix + "/comments/workbench/batch-publish",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
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

    @app.get(
        prefix + "/comments/workbench/{channel}/{identity}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def comment_queue_detail(channel: Literal["comments", "browsing", "jobs"], identity: str):
        row = queue_detail(store, comments, browsing, channel, identity)
        if not row:
            raise HTTPException(404, "记录不存在")
        return row

    @app.post(prefix + "/comments/drafts", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def drafts(data: dict = Body(...)):
        return comments.enqueue(data.get("comment_ids", []))

    @app.get(prefix + "/comments/tasks", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def tasks(task_id: str | None = Query(None, min_length=1, max_length=100)):
        if task_id:
            row = generation_task(store, task_id, can_write=can_write_comments())
            return [row] if row else []
        return [
            generation_task(store, row["id"], can_write=can_write_comments())
            for row in store.all("SELECT id FROM comment_tasks ORDER BY created_at DESC LIMIT 50")
        ]

    @app.post(
        prefix + "/comments/drafts/{draft_id}/review",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def review(draft_id: int, data: dict = Body(...)):
        options = {k: data[k] for k in ("image_ref", "updated_at", "expected_image") if k in data}
        if "mentions" in data:
            row = comments._draft_source(draft_id)
            if not row or data.get("updated_at") != row["updated_at"]:
                raise ValueError("草稿已变化，请刷新正文和提及")
            mentions, pending = await bind_mentions(
                account.adapter,
                data.get("text", row["text"]),
                data["mentions"],
                [{"uid": row["platform_user_id"], "name": row["display_name"]}],
            )
            options["mention_selection"] = {"mentions": mentions, "pending_mentions": pending}
        return comments.review(draft_id, data.get("action"), data.get("text"), **options)

    @app.post(
        prefix + "/comments/drafts/{draft_id}/copy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def copy(draft_id: int):
        return comments.copy(draft_id)

    @app.post(
        prefix + "/comments/drafts/{draft_id}/publish",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def publish(draft_id: int):
        return await comments.publish(draft_id)

    # --- Dynamics (13) ---
    @app.get(prefix + "/dynamics/policy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def dynamic_policy():
        return dynamics.policy()

    @app.put(prefix + "/dynamics/policy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def set_dynamic_policy(data: dict = Body(...)):
        return await dynamics.set_policy(data)

    @app.get(prefix + "/dynamics/posts", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def dynamic_posts(status: str | None = None, limit: int = Query(50, ge=1, le=100)):
        return dynamics.list_posts(status=status, limit=limit)

    @app.get(
        prefix + "/dynamics/posts/{post_id}/comment-card",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def dynamic_comment_card(post_id: int, page: int = Query(1, ge=1, le=9)):
        return Response(
            dynamics.comment_card_image(post_id, page - 1),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get(prefix + "/dynamics/decisions", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def dynamic_decisions(limit: int = Query(50, ge=1, le=100)):
        return dynamics.list_decisions(limit=limit)

    @app.get(prefix + "/dynamics/memories", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def dynamic_memories(limit: int = Query(100, ge=1, le=200)):
        return dynamics.list_memories(limit=limit)

    @app.get(prefix + "/dynamics/memory-summary", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def dynamic_memory_summary():
        return dynamics.summary_status()

    @app.get(prefix + "/dynamics/materials", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def dynamic_materials():
        return dynamics.latest_materials()

    @app.put(
        prefix + "/dynamics/memories/{memory_id}", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def dynamic_memory(memory_id: int, data: dict = Body(...)):
        if set(data) != {"public"} or type(data["public"]) is not bool:
            raise ValueError("请指定 public 布尔值")
        return dynamics.mark_memory(memory_id, data["public"])

    @app.post(prefix + "/dynamics/evaluate", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def evaluate_dynamic(topic: str = Body("", embed=True, max_length=200)):
        return await dynamics.evaluate(manual=True, topic=topic.strip())

    @app.post(
        prefix + "/dynamics/posts/batch-reject", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def batch_reject_dynamics(data: dict = Body(default={})):
        if set(data) - {"ids", "errored_only"}:
            raise ValueError("未知动态批量拒绝字段")
        ids = data.get("ids")
        errored_only = bool(data.get("errored_only"))
        if ids is None:
            if not errored_only:
                raise ValueError("请指定要拒绝的动态 ID 或勾选 errored_only")
            rows = store.all(
                "SELECT id FROM dynamic_posts WHERE status IN ('pending_review','approved') AND COALESCE(error,'')<>''"
            )
            ids = [row["id"] for row in rows]
        elif not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids):
            raise ValueError("动态 ID 须为正整数列表")
        results = []
        for post_id in dict.fromkeys(ids):
            try:
                dynamics.review(post_id, "reject")
                results.append({"id": post_id, "status": "rejected"})
            except Exception as exc:
                results.append({"id": post_id, "status": "failed", "error": public_error(exc)})
        return {"items": results}

    @app.post(
        prefix + "/dynamics/posts/{post_id}/review", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
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

    @app.post(
        prefix + "/dynamics/posts/{post_id}/publish", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def publish_dynamic(post_id: int, data: dict = Body(default={})):
        expected = None
        if data:
            if set(data) - {"text", "review_key"} or not isinstance(data.get("text"), str):
                raise ValueError("仅可提交当前动态正文")
            row = dynamics._post(post_id)
            if row["status"] in {"pending_review", "approved"} and not row.get("attempted_at"):
                if (
                    "review_key" in data
                    or row["illustration"]
                    or row["meme_id"]
                    or row["comment_card"]
                    or row["mentions"]
                    or row["pending_mentions"]
                ) and data.get("review_key") != row["review_key"]:
                    raise ValueError("正文、账号或配图已变化，请查看当前草稿后重新确认")
                expected = (
                    row["content"]
                    if row["status"] == "approved" and data["text"] == row["content"]
                    else dynamics.review(post_id, "approve", data["text"])["content"]
                )
            elif row["status"] == "published":
                return row
        return await dynamics.publish(post_id, expected_text=expected) if expected else await dynamics.publish(post_id)

    @app.post(
        prefix + "/dynamics/edits/{request_id}/apply", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def apply_dynamic_edit(request_id: UUID):
        edit = await dynamics.apply_dynamic_edit(str(request_id))
        return {key: edit[key] for key in ("request_id", "platform_id", "status", "error")}

    # --- Direct Messages (9) ---
    @app.get(prefix + "/messages", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def list_messages():
        return messages.list()

    @app.put(prefix + "/messages/policy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def message_policy(value: MessagePolicy):
        return await messages.configure(value.model_dump())

    @app.post(prefix + "/messages/sync", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def sync_messages():
        return await messages.sync()

    @app.get(prefix + "/messages/conversation", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def conversation(platform: str, user_id: str):
        if len(platform) > 32 or len(user_id) > 100:
            raise ValueError("用户身份无效")
        return messages.conversation(platform, user_id)

    @app.get(
        prefix + "/messages/inbox/{identity}/conversation",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def inbox_conversation(identity: Annotated[int, Path(ge=1, le=2**63 - 1)]):
        return messages.conversation(inbox_id=identity)

    @app.post(
        prefix + "/messages/inbox/{identity}/reply", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def prepare_message_reply(identity: int):
        return {"draft_id": messages.enqueue("message", identity)}

    @app.post(
        prefix + "/messages/drafts/batch-reject", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def batch_reject_messages(data: dict = Body(default={})):
        if set(data) - {"ids", "errored_only"}:
            raise ValueError("未知私信批量拒绝字段")
        ids = data.get("ids")
        errored_only = bool(data.get("errored_only"))
        if ids is None:
            if not errored_only:
                raise ValueError("请指定要拒绝的私信草稿 ID 或勾选 errored_only")
            rows = store.all(
                "SELECT id FROM dm_drafts WHERE status IN ('pending_review','approved','copied') AND COALESCE(error,'')<>''"
            )
            ids = [row["id"] for row in rows]
        elif not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids):
            raise ValueError("私信草稿 ID 须为正整数列表")
        results = []
        for draft_id in dict.fromkeys(ids):
            try:
                messages.review(draft_id, "reject")
                results.append({"id": draft_id, "status": "rejected"})
            except Exception as exc:
                results.append({"id": draft_id, "status": "failed", "error": public_error(exc)})
        return {"items": results}

    @app.post(
        prefix + "/messages/drafts/{identity}/review",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def review_message(identity: int, value: ReviewMessage):
        options = value.model_dump(exclude_unset=True, exclude={"action", "text"})
        return messages.review(identity, value.action, value.text, **options)

    @app.post(
        prefix + "/messages/drafts/{identity}/send", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def send_message(identity: int):
        return await messages.send(identity)

    @app.post(
        prefix + "/messages/drafts/{identity}/retry", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"]
    )
    async def retry_message(identity: int):
        return messages.retry(identity)
