"""Character, memory, and conversation context routes."""

import json
import re
import time
from typing import Annotated, Literal

from fastapi import Body, Depends, HTTPException, Path, Query

from ..models import Character
from ..relationships import UserProfile


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
        return store.characters()

    @app.get("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def character(character_id: str):
        return store.character(character_id)

    @app.get("/api/characters/{character_id}/versions", dependencies=[Depends(require_admin)])
    async def versions(character_id: str):
        return store.versions(character_id)

    @app.put("/api/characters/{character_id}", dependencies=[Depends(require_admin)])
    async def save_character(character_id: str, data: Character):
        if data.id != character_id:
            raise ValueError("角色ID不可修改，请复制新建OC")
        settings.voice(data.voice_profile)
        if not settings.assets(data.model_dump()):
            raise ValueError("素材清单无效或为空")
        active = runtime.character["id"] == character_id
        if active:
            runtime.live.clear_queue("角色配置更新")
            await runtime.live.cancel_current("角色配置更新")
        result = store.save_character(data.model_dump())
        if active:
            await runtime.activate(character_id, result["version"])
        return result

    @app.post("/api/characters/{character_id}/activate", dependencies=[Depends(require_admin)])
    async def activate(character_id: str, data: dict = Body(default={})):
        return await runtime.activate(character_id, data.get("version"))

    @app.post("/api/characters/{character_id}/clone", dependencies=[Depends(require_admin)])
    async def clone(character_id: str, data: dict = Body(...)):
        if store.one("SELECT id FROM characters WHERE id=?", (data.get("id"),)):
            raise ValueError("角色ID已存在")
        seed = store.character(character_id)
        seed.update(id=data.get("id"), name=data.get("name"))
        return store.save_character(seed)

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

    @app.post("/api/chat/intelligent", dependencies=[Depends(require_admin)])
    async def intelligent_chat(data: dict = Body(...)):
        user_message = (data.get("message") or "").strip()
        if not user_message:
            raise HTTPException(400, "消息不能为空")

        char = runtime.character
        char_id = char["id"]
        scope = f"operator:{char_id}"

        # A. Dynamic / Post intent
        if any(kw in user_message for kw in ("发动态", "发个动态", "发一条动态", "写个动态", "写条动态")):
            topic = re.sub(r"^(帮我|给我在b站|在b站)?(发个动态|发一条动态|发动态|写个动态|写条动态)[：:，,\s]*", "", user_message).strip() or "日常生活碎碎念"
            prompt_text = (
                f"你是一只叫{char.get('name', '大肥鱼')}的虚拟主播。饲养员让你发一条B站动态，主题是：{topic}。"
                "请以第一人称口吻写一段生动幽默、富有生活感的动态文案（40-100字），不写标题，直接写文案。"
            )
            messages = [
                {"role": "system", "content": "你是一只活泼傲娇的鲸鱼娘虚拟主播。直接输出动态正文。"},
                {"role": "user", "content": prompt_text}
            ]
            async with runtime.offline_generation("dynamic_card"):
                gen_reply = await runtime.llm.generate(messages, max_tokens=256)
            spoken = f"收到啦饲养员！本鱼已经准备好这篇关于【{topic}】的动态啦，你看看合不合心意~"
            card = {
                "type": "dynamic_draft",
                "title": "B站动态草稿",
                "data": {
                    "text": gen_reply.spoken_text if hasattr(gen_reply, "spoken_text") else str(gen_reply),
                    "topic": topic,
                    "character_id": char_id,
                },
                "actions": [
                    {"id": "publish", "label": "发布动态", "action": "publish_dynamic"},
                    {"id": "edit", "label": "编辑修改", "action": "edit_dynamic"}
                ]
            }
            return {
                "reply_text": spoken,
                "emotion": "cheerful",
                "card": card
            }

        # B. Video browsing intent
        elif any(kw in user_message for kw in ("刷视频", "看视频", "去看看视频", "刷b站", "逛b站")):
            keyword = re.sub(r"^(帮我|去)?(刷视频|看视频|去看看视频|刷b站|逛b站)[：:，,\s]*", "", user_message).strip() or "热门ACG/二次元"
            spoken = f"没问题！本鱼这就去潜水刷一刷【{keyword}】相关的视频，看看评论区大家都在聊什么好玩的~"
            card = {
                "type": "video_browsing",
                "title": "视频巡航与探索中控",
                "data": {
                    "keyword": keyword,
                    "frame_budget": "8-16 帧 (多模态高精度)",
                    "anti_hallucination": "字幕关键帧互证开启",
                    "status": "ready"
                },
                "actions": [
                    {"id": "start_browsing", "label": "开始巡航", "action": "start_video_browsing"},
                    {"id": "view_history", "label": "查看已读视频", "action": "view_browsing_history"}
                ]
            }
            return {
                "reply_text": spoken,
                "emotion": "excited",
                "card": card
            }

        # C. Livestream intent
        elif any(kw in user_message for kw in ("开播", "准备开播", "开始直播", "开启直播", "下播")):
            is_start = "下播" not in user_message
            spoken = (
                "好耶！直播间中控已经就绪，弹幕活动过滤与战团好感系统均已载入，随时可以开播迎接小鱼籽们啦！"
                if is_start else
                "呼~ 今天播得好尽兴，下播休息吃白饭去咯！"
            )
            card = {
                "type": "live_studio_hud",
                "title": "直播间极简中控 HUD",
                "data": {
                    "live_state": "running" if is_start else "stopped",
                    "poll_filtering": "活动过滤已激活",
                    "affinity_system": "Warband [-100, +100] 就绪",
                    "recent_barrage_count": len(getattr(runtime.live, "recent_barrage", []))
                },
                "actions": [
                    {"id": "toggle_live", "label": "切换直播状态", "action": "toggle_live"},
                    {"id": "draw_guess", "label": "发起你画我猜", "action": "start_draw_guess"}
                ]
            }
            return {
                "reply_text": spoken,
                "emotion": "cheerful" if is_start else "idle",
                "card": card
            }

        # D. Direct companion chat with operator (饲养员)
        event = {
            "event_id": f"op_{int(time.time() * 1000)}",
            "text": user_message,
            "sender_name": "饲养员",
            "is_operator": True,
        }
        operator_rel = {
            "stage": "特别熟悉",
            "affinity": 100,
            "relationship_summary": "工作站饲养员与主制作人，最信任的伙伴",
            "tags": ["饲养员", "制作人", "信任", "号主"],
        }
        reply, check = await runtime.generate_reply(
            scope,
            char,
            event,
            relationship=operator_rel,
            mode="live"
        )
        return {
            "reply_text": reply.spoken_text,
            "emotion": getattr(reply, "emotion", "cheerful"),
            "animation_id": getattr(reply, "animation_id", "talk_happy"),
            "card": {
                "type": "conversation",
                "title": "日常互动",
                "data": {
                    "role": "operator_companion",
                    "speaker": char.get("name", "大肥鱼"),
                }
            }
        }

