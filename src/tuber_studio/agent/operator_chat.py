"""Private operator conversation and typed shortcuts to existing studio tasks."""

import asyncio
import json
import re
import time
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import StringConstraints

from ..models import StrictModel
from ..store import dump

_POLITE = r"(?:(?:请|帮我|给我)[ ，]*){0,2}"
_DYNAMIC = re.compile(
    _POLITE + r"(?:发个动态|发条动态|发一条动态|发动态|写个动态|写条动态)"
    r"(?=$|[\s：:，,]|吐槽|分享|聊聊|说说|关于|庆祝|讲讲)(.*)", re.S
)
_BROWSE = re.compile(
    _POLITE + r"(?:去)?(?:开始刷视频|刷视频|看视频|刷[Bb]站|逛[Bb]站)"
    r"(?=$|[\s：:，,]|看看|关于|找找)(.*)", re.S
)
_LIVE = re.compile(_POLITE + r"(?:准备开播|查看直播状态|直播状态)[。！!]?")
_BV = re.compile(r"(?:https://www\.bilibili\.com/video/)?(BV[0-9A-Za-z]{10})/?")
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ChatTurn(StrictModel):
    request_id: UUID
    message: Text


class PublicExperience(StrictModel):
    fact: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


def action_for(message):
    for kind, pattern in (("dynamic", _DYNAMIC), ("browse", _BROWSE)):
        if match := pattern.fullmatch(message.rstrip("。！!")):
            topic = match[1].strip(" ：:，,\t\n")
            if topic.startswith(("是不是", "什么意思", "是什么意思", "这个词", "算了", "不用", "不要", "先别")) or (
                message.endswith(("？", "?", "吗"))
            ):
                break
            if kind == "browse" and topic.startswith(("看看", "找找")):
                topic = topic[2:].lstrip()
            return kind, topic
    if _LIVE.fullmatch(message):
        return "live", ""
    return "chat", ""


class OperatorChat:
    def __init__(self, runtime, store, browsing, dynamics):
        self.runtime, self.store, self.browsing, self.dynamics = runtime, store, browsing, dynamics
        self.lock = asyncio.Lock()
        store.execute(
            "CREATE TABLE IF NOT EXISTS operator_chat_turns ("
            "request_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, character_version INTEGER NOT NULL, "
            "message TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        store.execute("CREATE INDEX IF NOT EXISTS operator_chat_turn_age ON operator_chat_turns(created_at)")

    @staticmethod
    def _scope(character):
        return "operator:" + character["id"]

    def _visible(self, result):
        card = result.get("card")
        if card is None:
            return result
        if card["type"] == "dynamic_draft":
            identifier = card["data"]["id"]
            row = self.dynamics._post(identifier) if self.store.one(
                "SELECT 1 FROM dynamic_posts WHERE id=?", (identifier,)
            ) else None
            return result | {"card": card | {"data": self._draft_view(row)}}
        if card["type"] == "video_browsing":
            row = self.browsing.repo.round(card["data"]["id"])
            if row:
                return result | {"card": card | {"data": card["data"] | {"status": row["status"]}}}
        if card["type"] == "live_studio_hud":
            return result | {"card": card | {"data": self._live_view()}}
        return result

    def _live_view(self):
        live = self.runtime.live
        return {"state": live.state, "muted": live.muted, "busy": live.busy}

    @staticmethod
    def _draft_view(row):
        if row is None:
            return {"status": "unavailable"}
        return {key: row.get(key) for key in ("id", "content", "status", "receipt", "error")}

    def _turn(self, request_id):
        return self.store.one("SELECT * FROM operator_chat_turns WHERE request_id=?", (request_id,))

    def history(self):
        char = self.runtime.character
        rows = self.store.all(
            "SELECT * FROM operator_chat_turns WHERE character_id=? AND character_version=? "
            "ORDER BY created_at DESC LIMIT 100", (char["id"], char["version"]),
        )
        return {"character": char["name"], "turns": [
            {"request_id": row["request_id"], "message": row["message"], **self._visible(json.loads(row["result"]))}
            for row in reversed(rows)
        ]}

    async def send(self, data):
        request_id = str(data.request_id)
        async with self.lock:
            char = self.runtime.character
            if row := self._turn(request_id):
                if (
                    (row["character_id"], row["character_version"]) != (char["id"], char["version"])
                    or row["message"] != data.message
                ):
                    raise HTTPException(409, "该请求已绑定其他对话")
                return self._visible(json.loads(row["result"]))
            kind, topic = action_for(data.message)
            if kind == "chat":
                scope = self._scope(char)
                event = {"event_id": "operator:" + request_id, "text": data.message,
                         "sender_name": "号主", "is_operator": True}
                async with self.runtime.offline_generation("video"):
                    reply, _ = await self.runtime.generate_reply(scope, char, event, mode="operator_chat")
                    if self.runtime.character != char:
                        raise HTTPException(409, "当前角色已切换，请重新发送")
                    self.runtime.context.record(
                        scope, char, event, reply.spoken_text, "operator_chat", memory_kind="operator_private"
                    )
                result = {"reply_text": reply.spoken_text, "emotion": reply.emotion, "card": None, "private": True}
            else:
                result = await self._action(kind, char, request_id, topic)
            self.store.execute(
                "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                (request_id, char["id"], char["version"], data.message, dump(result), time.time()),
            )
            return result

    async def _action(self, kind, char, request_id, topic):
        if kind == "dynamic":
            row = await self.dynamics.create_operator_draft(
                char, request_id, topic or "想象中的海底小窝日常，保持为角色小故事"
            )
            return {"reply_text": "草稿已保存。", "card": {"type": "dynamic_draft", "data": self._draft_view(row)}}
        if kind == "browse":
            if len(topic) > 100:
                raise ValueError("浏览范围请控制在100字以内")
            video = _BV.fullmatch(topic)
            row = await self.browsing.run(
                preview=True, video_only=True, request_id=request_id,
                video=video[1] if video else None, keywords=[topic] if topic and not video else None,
            )
            return {"reply_text": "已创建只读浏览轮次。", "card": {"type": "video_browsing", "data": row}}
        return {"reply_text": "这是直播工作台的当前状态。", "card": {
            "type": "live_studio_hud", "data": self._live_view(),
        }}

    async def clear(self):
        async with self.lock, self.runtime.generation_lock:
            char = self.runtime.character
            scope = self._scope(char)
            with self.store.db:
                db = self.store.db
                db.execute("DELETE FROM context_history_audit WHERE window_key IN "
                           "(SELECT key FROM context_windows WHERE mode=? AND character_id=?)", (scope, char["id"]))
                db.execute("DELETE FROM context_windows WHERE mode=? AND character_id=?", (scope, char["id"]))
                db.execute("DELETE FROM grounded_memory WHERE scope=? AND character_id=?", (scope, char["id"]))
                db.execute("DELETE FROM operator_chat_turns WHERE character_id=?", (char["id"],))
        return {"ok": True}

    async def publish_experience(self, request_id, fact):
        async with self.lock:
            row, char = self._turn(request_id), self.runtime.character
            if row is None or (row["character_id"], row["character_version"]) != (char["id"], char["version"]):
                raise HTTPException(404, "找不到当前角色的这段对话")
            if not json.loads(row["result"]).get("private"):
                raise HTTPException(400, "请从私下对话选择要公开的经历")
            return {"id": self.runtime.daily_memory.confirm_operator_experience(char, request_id, fact)}


def register_operator_chat(app, runtime, store, browsing, dynamics, require_admin):
    service = OperatorChat(runtime, store, browsing, dynamics)
    deps = [Depends(require_admin)]

    @app.get("/api/chat/intelligent", dependencies=deps)
    async def history():
        return service.history()

    @app.post("/api/chat/intelligent", dependencies=deps)
    async def send(data: ChatTurn):
        return await service.send(data)

    @app.delete("/api/chat/intelligent", dependencies=deps)
    async def clear():
        return await service.clear()

    @app.post("/api/chat/intelligent/{request_id}/public-experience", dependencies=deps)
    async def publish_experience(request_id: UUID, data: PublicExperience):
        return await service.publish_experience(str(request_id), data.fact)
