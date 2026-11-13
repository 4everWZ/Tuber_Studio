"""Character, memory, and conversation context routes."""

import asyncio
import json
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Body, Depends, HTTPException, Path, Query
from pydantic import Field, StrictBool, StringConstraints, ValidationError

from ..daily_memory import HIGHLIGHT_ACCOUNT_FILTER
from ..models import Character, StrictModel
from ..operation_continuations import content_hash
from ..relationships import UserProfile
from ..store import character_card, editable_character
from .handoff import LIVE_STYLE, LIVE_STYLE_GROUP
from .operator_facts import recent_events


class OrganizedMemoryChange(StrictModel):
    channels: list[Literal["live", "comment", "dynamic"]] | None = None
    headline: str | None = Field(default=None, min_length=1, max_length=40)
    note: str | None = Field(default=None, min_length=1, max_length=150)


class NewCharacter(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class DeleteCharacter(StrictModel):
    replacement_id: str | None = None


class MemoryPreference(StrictModel):
    enabled: StrictBool
    scope: str


def organized_view(row):
    result = {key: row[key] for key in ("id", "memory_kind", "headline", "quote", "anecdote", "status",
                                       "channels", "source", "updated_at")}
    if row["status"] == "active":
        result["channels"] = ["operator", *result["channels"]]
    result["scene"] = row["proof"].get("scene")
    if result["scene"] == "live":
        result["choices"] = [text for key, text in LIVE_STYLE.items()
                             if LIVE_STYLE_GROUP[key] == row["proof"]["style_group"]]
    return result


def register_agent_api(app, settings, runtime, store, comments, require_admin, *, default_character):
    character_writes = asyncio.Lock()

    def memory_scope(expected=None):
        scope = runtime.memory_organizer.scope()
        token = content_hash(scope)
        if expected is not None and expected != token:
            raise HTTPException(409, "当前账号或角色已变化，请重新打开")
        return scope, token

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

    @app.post("/api/characters", dependencies=[Depends(require_admin)])
    async def create_character(data: NewCharacter):
        card = store.save_character({
            "id": f"oc-{uuid4().hex}", "name": data.name, "core": {}, "style": {},
            "voice_profile": default_character["voice_profile"],
            "avatar_manifest": default_character["avatar_manifest"],
        })
        return character_card(card)

    @app.get("/api/characters/deleted", dependencies=[Depends(require_admin)])
    async def deleted_characters():
        return [character_card(character) for character in store.deleted_characters()]

    @app.get("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def character(character_id: str):
        return character_card(store.managed_character(character_id))

    @app.put("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def save_character(character_id: str, data: dict = Body(...)):
        async with character_writes:
            store.managed_character(character_id)
            card = editable_character(data)
            card.pop("version", None)
            card["id"] = character_id
            if isinstance(card.get("name"), str):
                card["name"] = card["name"].strip()
            try:
                data = Character.model_validate(card).model_dump()
            except ValidationError as error:
                raise HTTPException(422, [
                    {"field": item["loc"][0], "message": item["msg"]} for item in error.errors()
                ]) from error
            try:
                settings.voice(data["voice_profile"])
            except ValueError as error:
                raise HTTPException(422, [{"field": "voice_profile", "message": str(error)}]) from error
            if not settings.assets(data):
                raise HTTPException(422, [{"field": "avatar_manifest", "message": "素材清单无效或为空"}])
            active = runtime.character
            result = store.replace_character(data)
            if active["id"] == character_id and character_card(active) != character_card(result):
                await runtime.activate(character_id)
            return character_card(result)

    @app.post("/api/characters/{character_id}/activate", dependencies=[Depends(require_admin)])
    async def activate(character_id: str):
        async with character_writes:
            return character_card(await runtime.activate(character_id))

    @app.delete("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def delete_character(character_id: str, data: DeleteCharacter | None = None):
        async with character_writes:
            if runtime.character["id"] == character_id:
                replacement = data.replacement_id if data else None
                if not replacement or replacement == character_id:
                    raise HTTPException(409, "请先选择要激活的替代角色")
                candidate = store.managed_character(replacement)
                if not settings.assets(candidate):
                    raise ValueError("替代角色的素材清单无效或为空")
                await runtime.activate(replacement)
            store.delete_character(character_id)
            return {"active_character_id": runtime.character["id"]}

    @app.post("/api/characters/{character_id}/restore", dependencies=[Depends(require_admin)])
    async def restore_character(character_id: str):
        async with character_writes:
            return character_card(store.restore_character(character_id))

    @app.post("/api/characters/{character_id}/clone", dependencies=[Depends(require_admin)])
    async def clone(character_id: str, data: dict = Body(...)):
        identity = data["id"] if "id" in data else f"oc-{uuid4().hex}"
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("请填写角色名称")
        if store.one("SELECT id FROM characters WHERE id=?", (identity,)):
            raise ValueError("角色ID已存在")
        seed = store.managed_character(character_id)
        seed.update(id=identity, name=name.strip())
        return character_card(store.save_character(seed))

    @app.get("/api/assets", dependencies=[Depends(require_admin)])
    async def assets():
        return settings.assets(runtime.character)

    @app.get("/api/daily-memory/preferences", dependencies=[Depends(require_admin)])
    async def get_memory_preference():
        scope, token = memory_scope()
        return runtime.daily_memory.preference(scope) | {"scope": token}

    @app.put("/api/daily-memory/preferences", dependencies=[Depends(require_admin)])
    async def set_memory_preference(data: MemoryPreference):
        scope, token = memory_scope(data.scope)
        result = runtime.daily_memory.set_preference(scope, data.enabled)
        runtime.memory_organizer.setting_changed()
        return result | {"scope": token}

    @app.get("/api/daily-memory", dependencies=[Depends(require_admin)])
    async def get_daily_memory(
        date: str | None = None,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        scope, token = memory_scope()
        account, character_id, version = scope
        highlights = runtime.daily_memory.list_highlights(
            character_id, version, date_str=date, status=status, limit=limit, account_id=account,
        )
        organized = runtime.daily_memory.organized(scope, limit=limit)
        return {"highlights": highlights,
                "organized": [organized_view(row) for row in organized], "organization": runtime.memory_organizer.state(),
                "scope": token, "enabled": runtime.daily_memory.preference(scope)["enabled"]}

    @app.get("/api/daily-memory/recent-events", dependencies=[Depends(require_admin)])
    async def get_recent_events():
        await runtime.browsing.discovery.restore_history_scope()
        return recent_events(runtime.browsing) | {"scope": memory_scope()[1]}

    @app.patch("/api/daily-memory/organized/{item_id}", dependencies=[Depends(require_admin)])
    async def change_organized_memory(item_id: int, data: OrganizedMemoryChange, scope: str | None = None):
        current, _ = memory_scope(scope)
        existing = runtime.daily_memory.organized(current, item_id=item_id)
        if existing and existing[0]["proof"].get("scene") == "live" and data.note is not None:
            group = existing[0]["proof"].get("style_group")
            if data.note not in {text for key, text in LIVE_STYLE.items() if LIVE_STYLE_GROUP[key] == group}:
                raise ValueError("请从直播表达规则中选择")
        row = runtime.daily_memory.change_organized(current, item_id, **data.model_dump())
        if not row:
            raise HTTPException(404, "未找到当前账号和角色的记忆")
        return organized_view(row)

    @app.delete("/api/daily-memory/organized/{item_id}", dependencies=[Depends(require_admin)])
    async def delete_organized_memory(item_id: int, scope: str | None = None):
        current, _ = memory_scope(scope)
        result = runtime.daily_memory.change_organized(current, item_id, delete=True)
        if not result:
            raise HTTPException(404, "未找到当前账号和角色的记忆")
        return result

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

    @app.put("/api/daily-memory/{item_id}", dependencies=[Depends(require_admin)])
    async def update_daily_memory(item_id: int, data: dict = Body(...), scope: str | None = None):
        async with runtime.generation_lock:
            memory_scope(scope)
            current_highlight(item_id)
            updated = runtime.daily_memory.update_highlight(item_id, data)
            if not updated:
                raise HTTPException(404, "未找到该记忆条目")
            return updated

    @app.delete("/api/daily-memory/{item_id}", dependencies=[Depends(require_admin)])
    async def delete_daily_memory(item_id: int, scope: str | None = None):
        async with runtime.generation_lock:
            memory_scope(scope)
            current_highlight(item_id)
            runtime.daily_memory.delete_highlight(item_id)
            return {"ok": True, "id": item_id}

    def current_highlight(item_id):
        char = runtime.character
        account = str(runtime.browsing.discovery.history_scope()[0] or "")
        if not store.one(
            "SELECT id FROM daily_highlights WHERE id=? AND character_id=? AND character_version=? "
            "AND CASE WHEN json_valid(source_evidence) THEN json_extract(source_evidence,'$.memory_kind') END IS NULL "
            "AND " + HIGHLIGHT_ACCOUNT_FILTER,
            (item_id, char["id"], char["version"], account, account),
        ):
            raise HTTPException(404, "未找到当前角色的记忆条目")

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
