"""Character, memory, and conversation context routes."""

import json
from typing import Annotated, Literal

from fastapi import Body, Depends, HTTPException, Path, Query

from ..models import Character
from ..relationships import UserProfile
from ..store import character_card


def register_agent_api(app, settings, runtime, store, comments, require_admin):
    def user_memory_scope(platform, user_id, account_id, character_id):
        messages = comments.direct_messages
        current_account = messages.display_account_id() if messages else comments.display_account_id()
        if (
            not current_account
            or str(current_account) != account_id
            or runtime.character["id"] != character_id
        ):
            raise HTTPException(409, "当前账号或角色已变化，请重新打开用户档案")
        return account_id, character_id, platform, user_id

    @app.get("/api/user-memory/{platform}/{user_id}", dependencies=[Depends(require_admin)])
    async def user_memory(
        platform: Literal["bilibili"],
        user_id: Annotated[str, Path(min_length=1, max_length=100)],
        account_id: str,
        character_id: str,
    ):
        return runtime.relationships.profile(*user_memory_scope(platform, user_id, account_id, character_id))

    @app.put("/api/user-memory/{platform}/{user_id}", dependencies=[Depends(require_admin)])
    async def save_user_memory(
        platform: Literal["bilibili"],
        user_id: Annotated[str, Path(min_length=1, max_length=100)],
        account_id: str,
        character_id: str,
        data: UserProfile,
    ):
        async with runtime.generation_lock:
            scope = user_memory_scope(platform, user_id, account_id, character_id)
            return runtime.relationships.set_profile(*scope, profile=data)

    @app.delete("/api/user-memory/{platform}/{user_id}", dependencies=[Depends(require_admin)])
    async def clear_user_memory(
        platform: Literal["bilibili"],
        user_id: Annotated[str, Path(min_length=1, max_length=100)],
        account_id: str,
        character_id: str,
    ):
        async with runtime.generation_lock:
            scope = user_memory_scope(platform, user_id, account_id, character_id)
            return runtime.relationships.set_profile(*scope)

    @app.get("/api/characters", dependencies=[Depends(require_admin)])
    async def characters():
        return [character_card(character) for character in store.characters()]

    @app.get("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def character(character_id: str):
        return character_card(store.character(character_id))

    @app.put("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def save_character(character_id: str, data: Character):
        if data.id != character_id:
            raise ValueError("角色ID不可修改，请复制新建OC")
        settings.voice(data.voice_profile)
        if not settings.assets(data.model_dump()):
            raise ValueError("素材清单无效或为空")
        active = runtime.character
        result = store.replace_character(data.model_dump())
        if active["id"] == character_id and character_card(active) != character_card(result):
            await runtime.activate(character_id)
        return character_card(result)

    @app.post("/api/characters/{character_id}/activate", dependencies=[Depends(require_admin)])
    async def activate(character_id: str):
        return character_card(await runtime.activate(character_id))

    @app.post("/api/characters/{character_id}/clone", dependencies=[Depends(require_admin)])
    async def clone(character_id: str, data: dict = Body(...)):
        if store.one("SELECT id FROM characters WHERE id=?", (data.get("id"),)):
            raise ValueError("角色ID已存在")
        seed = store.character(character_id)
        seed.update(id=data.get("id"), name=data.get("name"))
        return character_card(store.save_character(seed))

    @app.get("/api/assets", dependencies=[Depends(require_admin)])
    async def assets():
        return settings.assets(runtime.character)

    @app.get("/api/daily-memory", dependencies=[Depends(require_admin)])
    async def get_daily_memory(
        date: str | None = None,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        char = runtime.character
        highlights = runtime.daily_memory.list_highlights(
            char["id"], char["version"], date_str=date, status=status, limit=limit
        )
        active = runtime.daily_memory.get_active_anecdotes(
            char["id"], char["version"], limit=3, max_tokens=200
        )
        return {"highlights": highlights, "active_anecdotes": active}

    @app.post("/api/daily-memory/incidents", dependencies=[Depends(require_admin)])
    async def record_public_incident(data: dict = Body(...)):
        allowed = {"incident_key", "highlight_date", "headline", "anecdote", "public_sources", "tags"}
        if set(data) - allowed or not allowed - {"tags"} <= set(data):
            raise ValueError("请提供公开经历事实、稳定编号、日期及公开来源")
        char = runtime.character
        identifier = runtime.daily_memory.record_public_incident(char["id"], char["version"], **data)
        item = store.one("SELECT * FROM daily_highlights WHERE id=?", (identifier,))
        item["tags"] = json.loads(item["tags"] or "[]")
        item["public_sources"] = json.loads(item["public_sources"] or "[]")
        return item

    @app.post("/api/daily-memory/extract", dependencies=[Depends(require_admin)])
    async def extract_daily_memory(data: dict = Body(default={})):
        import datetime

        date_str = data.get("date") or datetime.date.today().isoformat()
        auto_approve = bool(data.get("auto_approve", False))
        async with runtime.generation_lock:
            extracted = await runtime.daily_memory.distill(
                runtime.character, date_str, runtime.llm, auto_approve=auto_approve
            )
        return {"extracted": extracted, "total": len(extracted), "date": date_str}

    @app.put("/api/daily-memory/{item_id}", dependencies=[Depends(require_admin)])
    async def update_daily_memory(item_id: int, data: dict = Body(...)):
        updated = runtime.daily_memory.update_highlight(item_id, data)
        if not updated:
            raise HTTPException(404, "未找到该记忆条目")
        return updated

    @app.delete("/api/daily-memory/{item_id}", dependencies=[Depends(require_admin)])
    async def delete_daily_memory(item_id: int):
        success = runtime.daily_memory.delete_highlight(item_id)
        if not success:
            raise HTTPException(404, "未找到该记忆条目")
        return {"ok": True, "id": item_id}

    @app.get("/api/context/status", dependencies=[Depends(require_admin)])
    async def context_status(video_id: str | None = None, thread_id: str | None = None):
        if video_id is not None or thread_id is not None:
            if not video_id or not thread_id:
                raise ValueError("请同时指定视频与对话楼ID")
            return runtime.context.status(comments.account_context_scope(video_id, thread_id), runtime.character)
        return runtime.context.status()

    @app.post("/api/context/rebuild", dependencies=[Depends(require_admin)])
    async def rebuild_context(data: dict = Body(...)):
        if set(data) != {"scope", "video_id", "thread_id"} or data.get("scope") != "comments":
            raise ValueError("请指定comments、video_id和thread_id，仅重建单楼")
        video_id, thread_id = data.get("video_id"), data.get("thread_id")
        scope = comments.account_context_scope(video_id, thread_id)
        if runtime.live.busy or runtime.generation_lock.locked():
            raise HTTPException(409, "正在生成或播放，请等待当前任务结束后重建")
        async with runtime.generation_lock:
            window = store.one(
                "SELECT history FROM context_windows WHERE key=?",
                (runtime.context.key(scope, runtime.character),),
            )
            if window and json.loads(window["history"]):
                raise HTTPException(409, "当前楼已有上下文，无需覆盖重建")
            if not store.one(
                "SELECT id FROM comments WHERE platform='bilibili' AND video_id=? AND thread_id=?",
                (video_id, thread_id),
            ):
                raise HTTPException(404, "本地没有该楼原文，请重新读取或导入评论")
            return await comments.rebuild_context(video_id, thread_id)

    @app.post("/api/context/compact", dependencies=[Depends(require_admin)])
    async def compact(data: dict = Body(...)):
        if runtime.live.busy:
            raise ValueError("直播正在生成或播放，请暂停并等待当前任务结束后压缩")
        if data.get("scope") == "live":
            scope, mode = "live:" + str(runtime.live.session_id), "live"
        elif data.get("scope") == "comments":
            video_id = data.get("video_id", "")
            if not isinstance(video_id, str) or not 1 <= len(video_id) <= 100:
                raise ValueError("请指定评论视频ID")
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 200:
                raise ValueError("请指定单个对话楼ID，不将整个视频的评论合并压缩")
            scope, mode = comments.account_context_scope(video_id, thread_id), "comment_draft"
        else:
            raise ValueError("scope应为live或comments")
        async with runtime.generation_lock:
            return await runtime.context.compact(scope, runtime.character, mode)
