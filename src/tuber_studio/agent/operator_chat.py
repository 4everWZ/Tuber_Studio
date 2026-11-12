"""Private operator conversation and typed shortcuts to existing studio tasks."""

import asyncio
import json
import re
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from pydantic import Field, StringConstraints, model_validator

from ..models import StrictModel
from ..operations.browsing.quotas import day_window
from ..store import dump

_POLITE = r"(?:(?:请|帮我|给我)[ ，]*){0,2}"
_DYNAMIC = re.compile(
    _POLITE + r"(?:发个动态|发条动态|发一条动态|发动态|写个动态|写条动态|写动态)"
    r"(?=$|[\s：:，,]|吐槽|分享|聊聊|说说|关于|庆祝|讲讲)(.*)", re.S
)
_BROWSE = re.compile(
    _POLITE + r"(?:去)?(?:开始刷视频|刷视频|看视频|刷[Bb]站|逛[Bb]站)"
    r"(?=$|[\s：:，,]|看看|关于|找找)(.*)", re.S
)
_LIVE = re.compile(_POLITE + r"(?:准备开播|查看直播状态|直播状态)[。！!]?")
_ACTIVITY = re.compile(_POLITE + r"(?:查看|看看)?今天(?:刷|看)了?(?:多少|几)(?:个|条)?视频[。！？!?]?")
_EXPERIENCES = re.compile(_POLITE + r"(?:查看|查询|看看)?公开经历[。！!]?")
_OPERATIONS = re.compile(_POLITE + r"(?:查看|开始|启动|停止)?自主运营[。！!]?")
_BV = re.compile(r"(?:https://www\.bilibili\.com/video/)?(BV[0-9A-Za-z]{10})/?")
_FULL_VIDEO = re.compile(_POLITE + r"(?:看完整视频|完整观看|从头到尾看)[ ：:，,]+(.+)")
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
_EXPERIENCE_SOURCE = (
    "(category='public_incident' OR source_ref LIKE 'operator-public:%' OR source_ref LIKE 'system:downtime:%')"
)
_EXPERIENCE_FIELDS = ("id", "category", "headline", "highlight_date", "quote", "anecdote", "status", "updated_at")


class VideoRange(StrictModel):
    part: int = Field(ge=1, strict=True)
    start: float = Field(ge=0, strict=True)
    end: float = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("视频区间结束时间须晚于开始时间")
        return self


class VideoSelection(StrictModel):
    bvid: str = Field(pattern=r"^BV[0-9A-Za-z]{10}$")
    ranges: list[VideoRange] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def disjoint(self):
        self.ranges.sort(key=lambda item: (item.part, item.start))
        for left, right in zip(self.ranges, self.ranges[1:]):
            if left.part == right.part and left.end > right.start:
                raise ValueError("同一分P的取材区间不可重叠")
        return self


class ChatTurn(StrictModel):
    request_id: UUID
    message: Text
    video_selection: VideoSelection | None = None


class PublicExperience(StrictModel):
    fact: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


class PublicExperienceState(StrictModel):
    status: Literal["active", "archived", "pending_review"]


def action_for(message):
    if _ACTIVITY.fullmatch(message):
        return "video_activity", ""
    if _EXPERIENCES.fullmatch(message):
        return "public_experiences", ""
    if _OPERATIONS.fullmatch(message):
        return "operation_control", ""
    if match := _FULL_VIDEO.fullmatch(message.rstrip("。！!")):
        if video := _BV.fullmatch(match[1].strip()):
            return "full_video", video[1]
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
        store.execute(
            "CREATE INDEX IF NOT EXISTS operator_chat_history "
            "ON operator_chat_turns(character_id,character_version,created_at DESC,request_id DESC)"
        )

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
                account, _, _ = self.browsing.discovery.history_scope()
                if not account or row["account_id"] != str(account):
                    return result | {"card": card | {"data": {"id": row["id"], "status": "unavailable"}}}
                if card["data"].get("full_video"):
                    return result | {"card": card | {"data": self.browsing.full_video_status(row)}}
                return result | {"card": card | {"data": card["data"] | {"status": row["status"]}}}
        if card["type"] == "live_studio_hud":
            return result | {"card": card | {"data": self._live_view()}}
        if card["type"] == "operation_control":
            return result | {"card": card | {"data": self.browsing.status()}}
        if card["type"] == "public_experiences":
            return result | {"card": card | {"data": self.experiences(active_only=True)}}
        if card["type"] == "video_activity":
            return result | {"card": card | {"data": self.video_activity(card["data"])}}
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

    def current_turn(self, request_id):
        row = self._turn(request_id)
        char = self.runtime.character
        if row is None or (row["character_id"], row["character_version"]) != (char["id"], char["version"]):
            raise HTTPException(404, "找不到当前角色的这段对话")
        return row

    def visible_turn(self, row):
        return {
            "request_id": row["request_id"], "message": row["message"],
            **self._visible(json.loads(row["result"])),
        }

    def history(self, limit, before):
        char = self.runtime.character
        scope = (char["id"], char["version"])
        boundary, cursor = "", ()
        if before:
            anchor = self.current_turn(before)
            boundary = " AND (created_at,request_id)<(?,?)"
            cursor = (anchor["created_at"], anchor["request_id"])
        rows = self.store.all(
            "SELECT * FROM operator_chat_turns WHERE character_id=? AND character_version=? "
            + boundary + " ORDER BY created_at DESC,request_id DESC LIMIT ?", (*scope, *cursor, limit + 1),
        )
        return {
            "character": char["name"],
            "character_id": char["id"], "character_version": char["version"],
            "turns": [self.visible_turn(row) for row in reversed(rows[:limit])],
            "next_cursor": rows[limit - 1]["request_id"] if len(rows) > limit else None,
        }

    async def send(self, data):
        request_id = str(data.request_id)
        async with self.lock:
            char = self.runtime.character
            kind, topic = action_for(data.message)
            selection = data.video_selection
            if selection is None and kind == "full_video":
                selection = VideoSelection(bvid=topic)
            elif selection is not None and kind == "full_video" and selection.bvid != topic:
                raise HTTPException(400, "取材设置与消息中的指定视频不同")
            if selection is not None:
                kind = "full_video"
            selected = selection.model_dump() if selection is not None else None
            if row := self._turn(request_id):
                result = json.loads(row["result"])
                if (
                    (row["character_id"], row["character_version"]) != (char["id"], char["version"])
                    or row["message"] != data.message
                    or ((result.get("card") or {}).get("data") or {}).get("selection") != selected
                ):
                    raise HTTPException(409, "该请求已绑定其他对话")
                return self._visible(result)
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
                result = await self._action(kind, char, request_id, topic, selected)
            self.store.execute(
                "INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                (request_id, char["id"], char["version"], data.message, dump(result), time.time()),
            )
            return self._visible(result)

    async def _action(self, kind, char, request_id, topic, selection):
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
        if kind == "full_video":
            row = await self.browsing.queue_full_video(request_id, char, selection)
            return {"reply_text": "已安排指定视频的只读取材。", "card": {
                "type": "video_browsing", "data": row | {"full_video": True, "selection": selection},
            }}
        if kind == "operation_control":
            return {"reply_text": "可以在这里确认权限、启动或停止自主运营。", "card": {
                "type": kind, "data": {},
            }}
        if kind == "public_experiences":
            return {"reply_text": "这是当前角色可公开回忆的经历。", "card": {"type": kind, "data": {}}}
        if kind == "video_activity":
            account, _, _ = self.browsing.discovery.history_scope()
            timezone = self.browsing.policy()["business_timezone"]
            return {"reply_text": "这是该业务日的观看记录。", "card": {
                "type": kind, "data": {"account_id": account, "timezone": timezone, **day_window(timezone)},
            }}
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
        async with self.lock, self.runtime.generation_lock:
            row, char = self.current_turn(request_id), self.runtime.character
            if not json.loads(row["result"]).get("private"):
                raise HTTPException(400, "请从私下对话选择要公开的经历")
            identity = self.runtime.daily_memory.confirm_operator_experience(char, request_id, fact)
            return self.experience(identity)

    def _experience_rows(self, *, item_id=None, active_only=False, limit=50):
        char = self.runtime.character
        sql = "SELECT * FROM daily_highlights WHERE character_id=? AND character_version=? AND " + _EXPERIENCE_SOURCE
        params = [char["id"], char["version"]]
        if item_id is not None:
            sql += " AND id=?"
            params.append(item_id)
        if active_only:
            sql += " AND status='active'"
        return self.store.all(sql + " ORDER BY highlight_date DESC,id DESC LIMIT ?", (*params, limit))

    @staticmethod
    def _experience_view(row):
        if row["category"] == "public_incident":
            source = {"kind": "public_thread", "public_sources": json.loads(row["public_sources"] or "[]")}
        elif row["source_ref"].startswith("operator-public:"):
            source = {"kind": "confirmed_turn", "request_id": row["source_ref"].split(":", 1)[1]}
        else:
            source = {"kind": "runtime_event"}
        return {key: row[key] for key in _EXPERIENCE_FIELDS} | {"source": source}

    def experiences(self, *, active_only=False, limit=50):
        return {"items": [
            self._experience_view(row) for row in self._experience_rows(active_only=active_only, limit=limit)
            if self.runtime.daily_memory._source_evidence(row)
        ]}

    def experience(self, item_id):
        rows = self._experience_rows(item_id=item_id)
        if not rows or not self.runtime.daily_memory._source_evidence(rows[0]):
            raise HTTPException(404, "找不到当前角色的公开经历")
        return self._experience_view(rows[0])

    async def change_experience(self, item_id, status=None):
        async with self.lock, self.runtime.generation_lock:
            self.experience(item_id)
            if status is None:
                self.runtime.daily_memory.delete_highlight(item_id)
                return {"ok": True, "id": item_id}
            self.runtime.daily_memory.update_highlight(item_id, {"status": status})
            return self.experience(item_id)

    def video_activity(self, period):
        account, character, _ = self.browsing.discovery.history_scope()
        base = {key: period[key] for key in ("day", "timezone", "start_at", "next_at")}
        if not account or account != period["account_id"]:
            return base | {"available": False}
        entries = self.store.all(
            "SELECT w.bvid,w.started_at,t.round_id,t.status,t.material_key,"
            "json_extract(t.payload,'$.coverage') AS coverage,"
            "COALESCE(json_array_length(t.payload,'$.missing_parts'),0) AS missing_parts "
            "FROM browsing_watch_starts w "
            "LEFT JOIN (browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id) "
            "ON t.id=w.task_id AND r.account_id=w.account_id AND r.character_id=w.character_id "
            "WHERE w.account_id=? AND w.character_id=? AND w.started_at>=? AND w.started_at<? "
            "ORDER BY w.started_at DESC,w.task_id DESC",
            (account, character, base["start_at"], base["next_at"]),
        )
        completed = [
            item for item in entries if item["status"] in {"selected", "viewed_without_comment"} and item["material_key"]
        ]
        fully_read = sum(item["coverage"] == "all_available_cc" and not item["missing_parts"] for item in completed)
        fields = ("bvid", "started_at", "round_id", "status", "coverage")
        return base | {
            "available": True, "started": len(entries), "completed": len(completed), "fully_read": fully_read,
            "records": [{key: item[key] for key in fields} for item in entries[:20]],
        }

    async def update_video(self, request_id, *, resume):
        async with self.lock:
            row = self.current_turn(request_id)
            card = json.loads(row["result"]).get("card") or {}
            if card.get("type") != "video_browsing" or not card["data"].get("full_video"):
                raise HTTPException(404, "这段对话没有指定视频取材任务")
            await self.browsing.update_full_video(request_id, resume=resume)
            return self.visible_turn(row)


def register_operator_chat(app, runtime, store, browsing, dynamics, require_admin):
    service = OperatorChat(runtime, store, browsing, dynamics)
    deps = [Depends(require_admin)]

    @app.get("/api/chat/intelligent", dependencies=deps)
    async def history(limit: int = Query(20, ge=1, le=100), before: UUID | None = None):
        return service.history(limit, str(before) if before else None)

    @app.get("/api/chat/intelligent/turns/{request_id}", dependencies=deps)
    async def get_turn(request_id: UUID):
        return service.visible_turn(service.current_turn(str(request_id)))

    @app.post("/api/chat/intelligent/turns/{request_id}/cancel-video", dependencies=deps)
    async def cancel_video(request_id: UUID):
        return await service.update_video(str(request_id), resume=False)

    @app.post("/api/chat/intelligent/turns/{request_id}/resume-video", dependencies=deps)
    async def resume_video(request_id: UUID):
        return await service.update_video(str(request_id), resume=True)

    @app.post("/api/chat/intelligent", dependencies=deps)
    async def send(data: ChatTurn):
        return await service.send(data)

    @app.delete("/api/chat/intelligent", dependencies=deps)
    async def clear():
        return await service.clear()

    @app.post("/api/chat/intelligent/{request_id}/public-experience", dependencies=deps)
    async def publish_experience(request_id: UUID, data: PublicExperience):
        return await service.publish_experience(str(request_id), data.fact)

    @app.get("/api/chat/intelligent/public-experiences", dependencies=deps)
    async def experiences(active_only: bool = False, limit: int = Query(50, ge=1, le=100)):
        return service.experiences(active_only=active_only, limit=limit)

    @app.get("/api/chat/intelligent/public-experiences/{item_id}", dependencies=deps)
    async def experience(item_id: int):
        return service.experience(item_id)

    @app.patch("/api/chat/intelligent/public-experiences/{item_id}", dependencies=deps)
    async def change_experience(item_id: int, data: PublicExperienceState):
        return await service.change_experience(item_id, data.status)

    @app.delete("/api/chat/intelligent/public-experiences/{item_id}", dependencies=deps)
    async def forget_experience(item_id: int):
        return await service.change_experience(item_id)
