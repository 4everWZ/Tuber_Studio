"""Source-triggered private replies, separate contexts and one-shot delivery."""

import asyncio
import json
import re
import time
from contextlib import nullcontext
from typing import Literal

from pydantic import Field, model_validator

from .bilibili import PublishUncertain
from .bilibili_messages import BilibiliMessagesAdapter, MessagePreparationCancelled
from .generation_meter import GenerationMeter, budgeted_json
from .live_media import StrictModel
from .meme_selection import select_meme
from .operation_continuations import GenerationFeedback, content_hash
from .operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    continuation_view,
    coordinator_for,
    generation_view,
    private_generation_views,
    publication_binding,
)
from .operation_generation import generate_with_recovery, reply_generation_binding
from .operation_text import validate_operation_text
from .operations_control import canonical_intent
from .providers import public_error
from .publication_errors import PlatformRejected, UnknownPublication


class MessagePolicy(StrictModel):
    enabled: bool = False
    auto_poll: bool = False
    auto_send: bool = False
    use_memes: bool = False
    allow_comment_handoff: bool = True
    channel_max_tokens: int = Field(default=2048, ge=256, le=65536)
    poll_interval_seconds: int = Field(default=60, ge=20, le=86400)
    max_per_hour: int = Field(default=5, ge=1, le=30)
    max_per_day: int = Field(default=20, ge=1, le=200)
    recipient_cooldown_seconds: int = Field(default=120, ge=10, le=86400)


class IncomingMessage(StrictModel):
    platform: str = Field(default="bilibili", pattern=r"^[a-z0-9_-]{1,32}$")
    message_id: str = Field(min_length=1, max_length=100)
    platform_user_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    timestamp: float = Field(ge=0)
    account_id: str = Field(default="", max_length=100)


class ChannelDecision(StrictModel):
    channel: Literal["comment", "direct_message"]
    reason: Literal["public_topic", "user_requested", "private_details", "long_followup"]

    @model_validator(mode="after")
    def consistent_reason(self):
        if (self.channel == "comment") != (self.reason == "public_topic"):
            raise ValueError("回复渠道与理由分类不一致")
        return self


CHANNEL_REASONS = {
    "public_topic": "普通公开讨论",
    "user_requested": "对方希望私下继续",
    "private_details": "涉及不宜公开的个人细节",
    "long_followup": "长篇跟进适合移入私信",
}


class DirectMessages:
    def __init__(self, settings, store, runtime, comments, adapter=None, *, memes=None):
        self.settings, self.store, self.runtime, self.comments = settings, store, runtime, comments
        self.adapter, self.injected = adapter, adapter is not None
        self.memes = memes
        self.credential_signature = None
        self.queue = asyncio.Queue(maxsize=40)
        self.worker = self.poller = None
        self.closed = False
        self.changed = asyncio.Event()
        self.send_lock, self.sync_lock = asyncio.Lock(), asyncio.Lock()
        self.error, self.last_sync = None, None
        self.enabled_at = self.store.get("message_enabled_at", time.time())
        self.started_at = int(time.time())
        self.operations = comments.operations
        self.authorizations = {}
        self.active_account_id = ""
        self.active_credential = None
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS dm_inbox(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,message_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,content TEXT NOT NULL,sent_at REAL,character_id TEXT,received_at REAL,UNIQUE(platform,message_id));
            CREATE TABLE IF NOT EXISTS dm_drafts(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,source_kind TEXT,source_id INTEGER,character_id TEXT,character_version INTEGER,status TEXT,text TEXT,reason TEXT,answer_check TEXT,model TEXT,error TEXT,receipt TEXT,created_at REAL,updated_at REAL,UNIQUE(source_kind,source_id,character_id));
            CREATE TABLE IF NOT EXISTS dm_attempts(id INTEGER PRIMARY KEY,draft_id INTEGER,platform_user_id TEXT,started_at REAL,status TEXT,receipt TEXT,error TEXT);
            CREATE INDEX IF NOT EXISTS dm_inbox_user ON dm_inbox(platform,platform_user_id,character_id,id);
        """)
        store.execute(
            "UPDATE dm_drafts SET status='cancelled',error='服务重启，取消未完成生成' WHERE status IN ('queued','generating')"
        )
        store.execute(
            "UPDATE dm_drafts SET status='publish_uncertain',error='发送中重启，请核对平台，禁止重发' WHERE status='publishing'"
        )
        store.execute("UPDATE dm_attempts SET status='uncertain',error='发送中重启' WHERE status='sending'")
        if "account_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(dm_inbox)")}:
            store.execute("ALTER TABLE dm_inbox ADD COLUMN account_id TEXT DEFAULT ''")
        if "account_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(dm_drafts)")}:
            store.execute("ALTER TABLE dm_drafts ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
        if "video_evidence" not in {row[1] for row in store.db.execute("PRAGMA table_info(dm_drafts)")}:
            store.execute("ALTER TABLE dm_drafts ADD COLUMN video_evidence TEXT")
        definition = store.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='dm_inbox'")["sql"]
        if "UNIQUE(platform,message_id)" in "".join(definition.split()):
            # Preserve source IDs and leave unbound private originals unbound.
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                store.db.execute(
                    "CREATE TABLE dm_inbox_scoped(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,message_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,content TEXT NOT NULL,sent_at REAL,character_id TEXT,received_at REAL,account_id TEXT NOT NULL DEFAULT '',UNIQUE(platform,account_id,message_id))"
                )
                store.db.execute(
                    "INSERT INTO dm_inbox_scoped SELECT id,platform,message_id,platform_user_id,display_name,content,sent_at,character_id,received_at,COALESCE(account_id,'') FROM dm_inbox"
                )
                store.db.execute("DROP TABLE dm_inbox")
                store.db.execute("ALTER TABLE dm_inbox_scoped RENAME TO dm_inbox")
                store.db.execute(
                    "CREATE INDEX dm_inbox_user ON dm_inbox(platform,account_id,platform_user_id,character_id,id)"
                )
        definition = store.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='dm_drafts'")["sql"]
        if "UNIQUE(source_kind,source_id,character_id)" in "".join(definition.split()):
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                store.db.execute(
                    "CREATE TABLE dm_drafts_scoped(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,source_kind TEXT,source_id INTEGER,character_id TEXT,character_version INTEGER,status TEXT,text TEXT,reason TEXT,answer_check TEXT,model TEXT,error TEXT,receipt TEXT,created_at REAL,updated_at REAL,account_id TEXT NOT NULL DEFAULT '',video_evidence TEXT,UNIQUE(account_id,source_kind,source_id,character_id))"
                )
                store.db.execute(
                    "INSERT INTO dm_drafts_scoped SELECT id,platform,platform_user_id,display_name,source_kind,source_id,character_id,character_version,status,text,reason,answer_check,model,error,receipt,created_at,updated_at,COALESCE(account_id,''),video_evidence FROM dm_drafts"
                )
                store.db.execute("DROP TABLE dm_drafts")
                store.db.execute("ALTER TABLE dm_drafts_scoped RENAME TO dm_drafts")
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(dm_drafts)")}
        for name, definition in (
            ("message_kind", "TEXT NOT NULL DEFAULT 'text'"),
            ("meme_id", "TEXT"),
            ("generation_continuation_id", "TEXT"),
        ):
            if name not in columns:
                store.execute(f"ALTER TABLE dm_drafts ADD COLUMN {name} {definition}")

    def policy(self):
        base = MessagePolicy.model_validate(self.store.get("message_policy", {})).model_dump()
        return {
            **self.operations.effective("messages", base),
            "base": base,
            "automation_override": self.operations.policy()["enabled"],
            "emergency": self.operations.policy()["emergency"],
            "credentials_configured": bool(
                self.comments.credentials.get("sessdata") and self.comments.credentials.get("csrf")
            ),
            "last_error": self.error,
            "last_sync": self.last_sync,
        }

    async def configure(self, value):
        value = MessagePolicy.model_validate(value).model_dump()
        old = self.policy()
        if value["auto_poll"] or value["auto_send"]:
            if not value["enabled"]:
                raise ValueError("请先启用私信功能")
            if not self.injected and not old["credentials_configured"]:
                raise ValueError("自动收发私信需要先配置现有B站账号凭证")
        if value["enabled"] and not old["enabled"]:
            self.enabled_at = int(time.time())
            self.store.set("message_enabled_at", self.enabled_at)
        self.store.set("message_policy", value)
        self.changed.set()
        return self.policy()

    async def _adapter(self):
        if self.injected:
            return self.adapter
        credentials = self.comments.credentials
        signature = (credentials.get("sessdata"), credentials.get("csrf"))
        if signature != self.credential_signature or self.adapter is None:
            if self.adapter:
                await self.adapter.close()
            self.adapter = BilibiliMessagesAdapter(credentials)
            self.credential_signature = signature
        return self.adapter

    def _enabled(self):
        if self.closed:
            raise ValueError("私信服务正在停止")
        if not self.policy()["enabled"]:
            raise ValueError("私信功能未启用")

    def ingest(self, value):
        message = IncomingMessage.model_validate(value).model_dump()
        if message["timestamp"] > time.time() + 30:
            raise ValueError("私信时间在未来")
        existing = self.store.one(
            "SELECT * FROM dm_inbox WHERE platform=? AND account_id=? AND message_id=?",
            (message["platform"], message["account_id"], message["message_id"]),
        )
        if existing:
            if (
                existing["platform_user_id"] != message["platform_user_id"]
                or existing["content"] != message["text"]
                or (
                    existing.get("account_id")
                    and message["account_id"]
                    and existing["account_id"] != message["account_id"]
                )
            ):
                raise ValueError("同一私信消息ID的来源或原文不能改写")
            return existing["id"], False
        self.store.ensure_user(message["platform"], message["platform_user_id"], message["display_name"])
        identity = self.store.execute(
            "INSERT INTO dm_inbox(platform,message_id,platform_user_id,display_name,content,sent_at,character_id,received_at,account_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                message["platform"],
                message["message_id"],
                message["platform_user_id"],
                message["display_name"],
                message["text"],
                message["timestamp"],
                self.runtime.character["id"],
                time.time(),
                message["account_id"],
            ),
        ).lastrowid
        return identity, True

    async def sync(self, *, automatic=False):
        self._enabled()
        if automatic and not self.policy()["auto_poll"]:
            raise ValueError("自动读取私信已停止")
        async with self.sync_lock:
            credential = dict(self.comments.credentials)
            adapter = await self._adapter()

            def require_current_account():
                if not self.injected and (
                    self.adapter is not adapter
                    or self.comments.credentials != credential
                    or self.credential_signature != (credential.get("sessdata"), credential.get("csrf"))
                ):
                    raise ValueError("读取期间账号已变更，未导入旧账号私信")

            require_current_account()
            own_uid, users = await adapter.sessions()
            require_current_account()
            self.active_account_id, self.active_credential = str(own_uid), credential
            count, latest, errors = 0, {}, []
            for uid in users[:20]:
                require_current_account()
                if not self.policy()["enabled"] or (automatic and not self.policy()["auto_poll"]):
                    break
                try:
                    rows = await adapter.history(uid)
                    require_current_account()
                    if not self.policy()["enabled"] or (automatic and not self.policy()["auto_poll"]):
                        break
                    known = self.store.one(
                        "SELECT display_name FROM users WHERE platform='bilibili' AND platform_user_id=?",
                        (uid,),
                    )
                    for row in sorted(rows, key=lambda row: int(row.get("timestamp", 0))):
                        if (
                            str(row.get("sender_uid")) != str(uid)
                            or str(row.get("receiver_id")) != str(own_uid)
                            or row.get("receiver_type", 1) != 1
                            or row.get("msg_type") != 1
                            or row.get("msg_status", 0) != 0
                        ):
                            continue
                        content = row.get("content")
                        content = json.loads(content) if isinstance(content, str) else content
                        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(row.get("msg_key", ""))):
                            raise ValueError("平台私信缺少有效消息ID")
                        identity, fresh = self.ingest(
                            {
                                "message_id": str(row["msg_key"]),
                                "platform_user_id": str(uid),
                                "display_name": known["display_name"] if known else f"UID {uid}",
                                "text": content["content"],
                                "timestamp": row["timestamp"],
                                "account_id": str(own_uid),
                            }
                        )
                        count += int(fresh)
                        if (
                            fresh
                            and row["timestamp"]
                            >= max(
                                int(self.enabled_at),
                                self.started_at,
                                int(self.operations.policy()["enabled_at"] or 0),
                            )
                            and row["timestamp"] >= time.time() - 86400
                        ):
                            latest[uid] = identity
                except (ValueError, KeyError, TypeError) as error:
                    errors.append(f"UID {uid}：{public_error(error)}")
            require_current_account()
            if automatic and self.policy()["auto_poll"]:
                for identity in latest.values():
                    self.enqueue("message", identity, automatic=True)
            self.last_sync = time.time()
            self.error = "；".join(errors)[:500] or None
            return {"imported": count, "errors": errors}

    def _source(self, kind, identity, character):
        table = "dm_inbox" if kind == "message" else "comments" if kind == "comment" else None
        if table is None:
            raise ValueError("私信只能由对方来信或当前评论触发")
        row = self.store.one(f"SELECT * FROM {table} WHERE id=?", (identity,))
        if not row or not row.get("platform_user_id"):
            raise ValueError("私信缺少可确认的来源用户ID")
        if kind == "message" and row["character_id"] != character["id"]:
            raise ValueError("该来信属于其他OC，请切回对应角色")
        return row

    def enqueue(
        self,
        kind,
        identity,
        *,
        automatic=False,
        reason="对方主动来信",
        character=None,
        account_id=None,
        video_evidence=None,
        schedule=True,
    ):
        self._enabled()
        character = character or self.runtime.character
        source = self._source(kind, identity, character)
        account_id = str(source.get("account_id") or "") if kind == "message" else str(account_id or "")
        existing = self.store.one(
            "SELECT id FROM dm_drafts WHERE account_id=? AND source_kind=? AND source_id=? AND character_id=?",
            (account_id, kind, identity, character["id"]),
        )
        if existing:
            return existing["id"]
        if schedule and self.queue.full():
            raise ValueError("私信生成队列已满，请稍后重试")
        if kind == "comment" and not self.policy()["allow_comment_handoff"]:
            raise ValueError("评论转私信尚未启用")
        encoded = None if video_evidence is None else json.dumps(video_evidence, ensure_ascii=False)
        if encoded is not None and len(encoded.encode()) > 1_000_000:
            raise ValueError("移交的视频来源材料过大")
        now = time.time()
        automatic = automatic and self.policy()["auto_send"]
        draft_id = self.store.execute(
            "INSERT INTO dm_drafts(platform,platform_user_id,display_name,source_kind,source_id,character_id,character_version,status,reason,created_at,updated_at,account_id,video_evidence) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
            (
                source["platform"],
                source["platform_user_id"],
                source["display_name"],
                kind,
                identity,
                character["id"],
                character["version"],
                reason,
                now,
                now,
                account_id,
                encoded,
            ),
        ).lastrowid
        self.authorizations[draft_id] = (
            self.operations.policy()["epoch"] if automatic and self.operations.policy()["enabled"] else None
        )
        if schedule:
            self.queue.put_nowait((draft_id, automatic))
        return draft_id

    async def route_comment(self, row, character, relationship, context, scope, event, *, automatic=False):
        policy = self.policy()
        if not (policy["enabled"] and policy["allow_comment_handoff"] and row.get("platform_user_id")):
            return None
        binding = reply_generation_binding(scope, character, event, "comment_channel_decision")
        binding.update(
            account_id=str(event.get("account_id", "") or ""),
            schema="ChannelDecision",
            recipient_uid=str(row["platform_user_id"]),
        )
        identity = content_hash(binding)
        credential = dict(self.comments.credentials)
        source_fields = ("platform", "video_id", "thread_id", "comment_id", "platform_user_id", "content")

        def guard():
            current = self.runtime.character
            fresh = self.store.one("SELECT * FROM comments WHERE id=?", (row["id"],))
            actual = self.policy()
            if (
                {key: current[key] for key in ("id", "version")} != binding["character"]
                or self.comments.credentials != credential
                or not fresh
                or any(fresh.get(key) != row.get(key) for key in source_fields)
                or not (actual["enabled"] and actual["allow_comment_handoff"])
            ):
                raise ValueError("渠道分类期间账号、角色、原评论或私信授权已变化")

        messages = []
        model_context = {"task": "comment_channel_decision", "event": event, "relationship": relationship}

        async def invoke(packet):
            if not messages:
                prepared = await context.prepare(
                    scope,
                    character,
                    event,
                    relationship,
                    "comment_channel_decision",
                    output_tokens=policy["channel_max_tokens"],
                )
                prepared.append(
                    {
                        "role": "system",
                        "content": "根据当前对话选择接话渠道。公开话题继续在楼里聊：comment、public_topic；对方希望私下继续：direct_message、user_requested；个人细节适合单独聊：direct_message、private_details；上下文适合长篇跟进：direct_message、long_followup。填写channel与reason。",
                    }
                )
                messages.extend(prepared)
                model_context["public_context"] = [
                    message for message in messages if message["role"] != "system"
                ]
                if packet is not messages:
                    packet = [*messages, *packet]
            guard()
            return await budgeted_json(
                self.runtime.llm,
                packet,
                ChannelDecision.model_json_schema(),
                max_tokens=policy["channel_max_tokens"],
            )

        def validate(value):
            return ChannelDecision.model_validate(
                value.model_dump() if hasattr(value, "model_dump") else value
            )

        try:
            decision, audit = await generate_with_recovery(
                self.runtime,
                channel="generation.comment_channel",
                identity=identity,
                binding=binding,
                character=character,
                messages=messages,
                generate=invoke,
                validate=validate,
                max_tokens=policy["channel_max_tokens"],
                guard=guard,
                context_data=model_context,
                reuse_completed=True,
            )
        except Exception as error:
            if getattr(error, "continuation", None):
                self.runtime.continuations.bind_source_view(
                    error.continuation["id"], "comments", row["id"], "评论渠道生成记录"
                )
            raise
        if audit or decision.channel == "direct_message":
            record = coordinator_for(self.runtime, self.store, self.operations).find(
                "generation.comment_channel", identity
            )
            self.runtime.continuations.bind_source_view(
                record["id"], "comments", row["id"], "评论渠道生成记录"
            )
        if decision.channel == "comment":
            return None
        draft_id = self.enqueue(
            "comment",
            row["id"],
            automatic=automatic,
            reason=CHANNEL_REASONS[decision.reason],
            character=character,
            account_id=event.get("account_id", ""),
            video_evidence=event.get("video_evidence"),
        )
        return draft_id

    def _event(self, draft, character):
        source = self._source(draft["source_kind"], draft["source_id"], character)
        if draft["source_kind"] == "message" and str(source.get("account_id") or "") != draft.get(
            "account_id", ""
        ):
            raise ValueError("来信账号与私信上下文范围不一致，请重新同步来源")
        event = {
            "event_id": f"dm:{draft['id']}",
            "text": source["content"],
            "platform": draft["platform"],
            "platform_user_id": draft["platform_user_id"],
            "display_name": draft["display_name"],
            "delivery_channel": "private_message",
            "account_id": draft.get("account_id", ""),
        }
        if draft.get("video_evidence"):
            event["video_evidence"] = json.loads(draft["video_evidence"])
        if draft["source_kind"] == "comment":
            scope = self.comments.context_scope(source["video_id"], source["thread_id"], source["platform"])
            window = self.runtime.context.existing(scope, character)
            event["public_source_thread"] = json.loads(window["history"]) if window else []
            if event.get("video_evidence", {}).get("public_source_thread"):
                event["public_source_thread"] = event["video_evidence"]["public_source_thread"]
        else:
            event["recent_incoming"] = self.store.all(
                "SELECT message_id,content,sent_at FROM dm_inbox WHERE platform=? AND platform_user_id=? AND character_id=? AND account_id=? AND id<? ORDER BY id DESC LIMIT 30",
                (
                    draft["platform"],
                    draft["platform_user_id"],
                    character["id"],
                    draft.get("account_id", ""),
                    source["id"],
                ),
            )[::-1]
        if len(json.dumps(event, ensure_ascii=False)) > 1_000_000:
            raise ValueError("来源上下文过大，保留原文；请整理该楼后重新准备私信")
        return event

    @staticmethod
    def scope(draft):
        if draft.get("account_id"):
            return "dm:" + json.dumps(
                [draft["platform"], draft["account_id"], draft["platform_user_id"]], separators=(",", ":")
            )
        return f"dm:{draft['platform']}:{draft['platform_user_id']}"

    async def _generate(self, identity, automatic):
        try:
            return await self._generate_owned(identity, automatic)
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE dm_drafts SET status='cancelled',error='父任务停止，取消未提交私信生成',updated_at=? WHERE id=? AND status IN ('queued','generating')",
                (time.time(), identity),
            )
            raise
        except Exception as error:
            self.store.execute(
                "UPDATE dm_drafts SET status='failed',error=?,updated_at=? WHERE id=? AND status IN ('queued','generating')",
                (public_error(error), time.time(), identity),
            )
            raise

    async def _generate_owned(self, identity, automatic):
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft or draft["status"] != "queued":
            return
        authorization_epoch = self.authorizations.pop(identity, None)
        origin = self._origin_round(draft, active=True)
        credential = dict(self.comments.credentials)
        identity_adapter = None
        if not self.injected:
            identity_adapter = await self._adapter()
            actor = str(await identity_adapter.self_uid())
            if (
                self.adapter is not identity_adapter
                or self.comments.credentials != credential
                or self.credential_signature != (credential.get("sessdata"), credential.get("csrf"))
            ):
                raise ValueError("核验期间账号已变更，未读取旧账号私密上下文")
            if draft["account_id"] and draft["account_id"] != actor:
                raise ValueError("该私信草稿属于其他账号，不能读取其私密上下文")
            if not draft["account_id"]:
                # Only a newly queued generation can establish a fresh account
                # scope. Existing unbound private history is never migrated.
                draft["account_id"] = actor
                self.store.execute(
                    "UPDATE dm_drafts SET account_id=? WHERE id=? AND status='queued'", (actor, identity)
                )
            self.active_account_id, self.active_credential = actor, credential
        browsing = getattr(self.runtime, "browsing", None)
        if draft["source_kind"] == "message" and browsing:
            linked = await browsing.linked_message_evidence(draft)
            if linked is not None:
                previous = json.loads(draft.get("video_evidence") or "{}")
                if previous.get("origin_round_id"):
                    linked = {**linked, "origin_round_id": previous["origin_round_id"]}
                draft["video_evidence"] = json.dumps(linked, ensure_ascii=False)
                self.store.execute(
                    "UPDATE dm_drafts SET video_evidence=? WHERE id=? AND status='queued'",
                    (draft["video_evidence"], identity),
                )
        if draft.get("video_evidence") and browsing and hasattr(browsing, "validate_private_source"):
            await browsing.validate_private_source(draft)
        async with self.runtime.offline_generation():
            if not self.injected and (
                self.adapter is not identity_adapter or self.comments.credentials != credential
            ):
                raise ValueError("等待模型期间账号已变更，未读取旧账号私密上下文")
            self.store.execute(
                "UPDATE dm_drafts SET status='generating',updated_at=? WHERE id=?", (time.time(), identity)
            )
            character = self.store.character(draft["character_id"], draft["character_version"])
            source = self._source(draft["source_kind"], draft["source_id"], character)
            event = self._event(draft, character)
            relationship = self.runtime.relationships.observe(
                character["id"],
                draft["platform"],
                draft["platform_user_id"],
                draft["display_name"],
                canonical_intent(
                    draft["platform"],
                    source.get("video_id", "dm"),
                    source.get("comment_id") or source.get("message_id"),
                    character["id"],
                ),
                event["text"],
                account_id=draft["account_id"],
            )
            meter = (
                GenerationMeter(self.runtime.browsing_repository, origin["id"]) if origin else nullcontext()
            )
            with meter:
                reply, check = await self.runtime.generate_reply(
                    self.scope(draft), character, event, relationship, mode="direct_message"
                )
                selected, selection_failed = None, False
                if self.policy()["use_memes"] and self.memes:
                    try:
                        selected = await select_meme(
                            self.runtime, self.memes, character, event, reply.spoken_text
                        )
                        check["meme_selection"] = {"state": "selected" if selected else "text"}
                    except Exception:
                        check["meme_selection"] = {"state": "failed", "reason": "表情选择无效，保留待审文字"}
                        selection_failed = True
            self.store.execute(
                "UPDATE dm_drafts SET status='pending_review',text=?,answer_check=?,model=?,error=NULL,updated_at=? WHERE id=?",
                (
                    reply.spoken_text,
                    json.dumps(check, ensure_ascii=False),
                    self.settings.data["llm"].get("model", "demo"),
                    time.time(),
                    identity,
                ),
            )
            if selected:
                self.review(identity, "edit", meme_id=selected["asset_id"])
            elif selection_failed:
                self.store.execute(
                    "UPDATE dm_drafts SET error=? WHERE id=?",
                    ("表情选择失败，已保留待审文字，未自动发送", identity),
                )
        if (
            automatic
            and self.policy()["auto_send"]
            and not selection_failed
            and not check.get("output", {}).get("needs_review")
        ):
            self._origin_round(draft, active=True)
            await self.send(identity, automatic=True, authorization_epoch=authorization_epoch)

    def _origin_round(self, draft, *, active):
        evidence = json.loads(draft.get("video_evidence") or "null")
        identity = evidence.get("origin_round_id") if isinstance(evidence, dict) else None
        if not identity:
            return None
        repository = self.runtime.browsing_repository
        parent = repository.round(identity)
        if (
            not parent
            or (active and parent["status"] not in {"queued", "running"})
            or parent["deadline"] <= time.time()
            or parent["account_id"] != draft["account_id"]
            or parent["character_id"] != draft["character_id"]
            or parent["character_version"] != draft["character_version"]
        ):
            raise ValueError("私信所属浏览轮次已停止或身份变化，请重新预览")
        return parent

    def review(self, identity, action, text=None, **options):
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row or row["status"] not in {"pending_review", "approved", "rejected", "copied"}:
            raise ValueError("当前私信草稿不能编辑或审核")
        if action not in {"approve", "reject", "edit", "copy"}:
            raise ValueError("未知私信审核操作")
        if set(options) - {"meme_id"} or options and action != "edit":
            raise ValueError("更换表情需要重新编辑并审核")
        meme_id = options.get("meme_id", row.get("meme_id"))
        if action == "copy" and meme_id:
            raise ValueError("图片私信不能复制为文本，请使用发送图片")
        if meme_id and action != "reject":
            if not self.memes:
                raise ValueError("表情组件尚未启用")
            self.memes.read(meme_id, row["character_id"])
        if action == "copy" and row["status"] not in {"approved", "copied"}:
            raise ValueError("请先审核私信，再复制")
        text = text if text is not None else row["text"]
        text = validate_operation_text(text, label="私信内容")
        status = {"approve": "approved", "reject": "rejected", "edit": "pending_review", "copy": "copied"}[
            action
        ]
        owner = f"dm-draft:{identity}"
        if meme_id and action != "reject":
            self.memes.pin(meme_id, row["character_id"], owner)
        try:
            self.store.execute(
                "UPDATE dm_drafts SET text=?,status=?,message_kind=?,meme_id=?,error=NULL,updated_at=? WHERE id=?",
                (text.strip(), status, "image" if meme_id else "text", meme_id, time.time(), identity),
            )
        except BaseException:
            if self.memes and meme_id != row.get("meme_id"):
                self.memes.release(owner, keep_asset_id=row.get("meme_id"))
            raise
        if self.memes:
            self.memes.release(owner, keep_asset_id=meme_id)
            if status == "rejected":
                self.memes.release(owner)
        return {
            "status": status,
            "text": text.strip(),
            "message_kind": "image" if meme_id else "text",
            "meme_id": meme_id,
        }

    def _publication_binding(self, row, source, account, character):
        return publication_binding(
            "messages",
            account,
            character,
            canonical_intent(
                row["platform"],
                source["video_id"] if row["source_kind"] == "comment" else "dm",
                source["comment_id"] if row["source_kind"] == "comment" else source["message_id"],
                character["id"],
            ),
            {"platform": row["platform"], "recipient_uid": row["platform_user_id"]},
            {
                "kind": row["source_kind"],
                "local_id": row["source_id"],
                "platform_id": source["comment_id"]
                if row["source_kind"] == "comment"
                else source["message_id"],
                "content_sha256": content_hash(source["content"]),
            },
        )

    def _outgoing(self, row, character):
        if row.get("message_kind") == "image":
            if not self.memes or not row.get("meme_id"):
                raise ValueError("图片草稿缺少已登记表情")
            asset = self.memes.resolve(row["meme_id"], character)
            return {"message_kind": "image", "meme_id": row["meme_id"], "asset_sha256": asset["sha256"]}
        return {"text": row["text"]}

    async def _publication_context(self, identity, automatic, epoch):
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row:
            raise ValueError("私信草稿不存在")
        character = self.store.character(row["character_id"], row["character_version"])
        source = self._source(row["source_kind"], row["source_id"], character)
        adapter = await self._adapter()
        account = (
            str(source.get("account_id") or "injected-test-account")
            if self.injected
            else str(await adapter.self_uid())
        )
        binding = self._publication_binding(row, source, account, character)
        credential = dict(self.comments.credentials)

        def guard():
            if self.closed:
                raise asyncio.CancelledError
            fresh = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not fresh or any(
                fresh.get(key) != row.get(key)
                for key in (
                    "status",
                    "text",
                    "message_kind",
                    "meme_id",
                    "character_id",
                    "character_version",
                    "account_id",
                    "platform",
                    "platform_user_id",
                    "source_kind",
                    "source_id",
                )
            ):
                raise ValueError("恢复期间私信草稿或审核状态已变化")
            current = self._source(row["source_kind"], row["source_id"], character)
            if (
                self._publication_binding(fresh, current, account, character) != binding
                or self.adapter is not adapter
                or self.comments.credentials != credential
            ):
                raise ValueError("恢复期间私信账号、收件人或原始来源已变化")
            policy = self.policy()
            if (
                not policy["enabled"]
                or automatic
                and not policy["auto_send"]
                or row["source_kind"] == "comment"
                and not policy["allow_comment_handoff"]
            ):
                raise ValueError("私信授权已停止")
            self.operations.assert_allowed(
                automatic=automatic, channel="messages", account_id=account, character=character, epoch=epoch
            )

        def encode(value):
            if isinstance(value, str):
                return {"text": validate_operation_text(value)}
            if not isinstance(value, dict):
                raise ValueError("私信修订内容无效")
            candidate = row | value
            return self._outgoing(candidate, character)

        async def revise_image(model, current, decision):
            assets = [
                {key: asset[key] for key in ("asset_id", "title", "emotions")}
                for asset in (self.memes.list(character) if self.memes else [])
                if asset["enabled"] and not asset.get("missing")
            ]
            allowed = [asset["asset_id"] for asset in assets]
            schema = {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["text", "image"]},
                    "meme_id": {"enum": [None, *allowed]},
                },
                "required": ["kind", "meme_id"],
                "additionalProperties": False,
            }
            value = await model.generate_structured(
                current,
                schema,
                "为这次私信选择修订：选择图库中的另一张图片，或选text改为文字，文字正文在下一步生成。可用图库："
                + json.dumps(assets, ensure_ascii=False),
            )
            if not isinstance(value, dict) or set(value) != {"kind", "meme_id"}:
                raise GenerationFeedback("私信修订选择需要kind与meme_id", original_output=value)
            if value["kind"] == "text" and value["meme_id"] is None:
                return {
                    "text": await model.revise(current, decision),
                    "message_kind": "text",
                    "meme_id": None,
                }
            if value["kind"] != "image" or value["meme_id"] not in allowed:
                raise GenerationFeedback("修订图片须来自当前角色图库", original_output=value)
            return {"text": row["text"], "message_kind": "image", "meme_id": value["meme_id"]}

        def defer(prepared, state):
            prepared.guard()
            self.store.execute(
                "UPDATE dm_drafts SET status=?,updated_at=? WHERE id=?",
                ("rejected" if state == "abandoned" else "pending_review", time.time(), identity),
            )
            if state == "abandoned" and self.memes:
                self.memes.release(f"dm-draft:{identity}")

        return PublicationContext(
            binding,
            row["text"],
            character,
            {"source": self._event(row, character), "message_kind": row.get("message_kind", "text")},
            guard,
            payload=self._outgoing(row, character),
            encode_revision=encode,
            revision=revise_image if row.get("message_kind") == "image" else None,
            on_defer=defer,
            manual_approved=row["status"] == "approved",
        )

    def _apply_revision(self, identity, prepared, value):
        prepared.guard()
        candidate = (
            {"text": value, "message_kind": "text", "meme_id": None} if isinstance(value, str) else value
        )
        text = validate_operation_text(candidate["text"])
        meme_id = candidate.get("meme_id")
        owner = f"dm-draft:{identity}"
        if meme_id:
            self.memes.pin(meme_id, prepared.character, owner)
        try:
            self.store.execute(
                "UPDATE dm_drafts SET text=?,message_kind=?,meme_id=?,status='pending_review',error=NULL,updated_at=? WHERE id=?",
                (text, "image" if meme_id else "text", meme_id, time.time(), identity),
            )
        except BaseException:
            if self.memes:
                self.memes.release(owner, keep_asset_id=prepared.payload.get("meme_id"))
            raise
        if self.memes:
            self.memes.release(owner, keep_asset_id=meme_id)

    async def resume_continuation(self, identity, continuation_id):
        if str(identity).startswith("generation:"):
            identity = str(identity).split(":", 1)[1]
            row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not row or row.get("generation_continuation_id") != continuation_id:
                raise ValueError("生成恢复任务与私信不匹配")
            continuation = self.runtime.continuations.get(continuation_id)
            if (
                continuation["binding"]["character"]
                != {key: self.runtime.character[key] for key in ("id", "version")}
                or continuation["binding"]["source"].get("text")
                != self._source(row["source_kind"], row["source_id"], self.runtime.character)["content"]
            ):
                raise ValueError("原始私信或角色已变化，请重新准备")
            return {
                "status": "queued",
                **self.retry(identity),
                "continuation": generation_view(self.runtime, continuation_id, "messages", identity),
            }
        return await FeedbackPublication(self.runtime, self.store, self.operations).resume(
            "messages",
            identity,
            continuation_id,
            prepare=lambda: self._publication_context(identity, False, None),
            apply_revision=lambda prepared, value: self._apply_revision(identity, prepared, value),
            reconcile=lambda token, prepared: self._reconcile(identity),
        )

    async def _reconcile(self, identity):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        async def read(row, attempt):
            adapter = await self._adapter()
            if not hasattr(adapter, "reconcile_publication"):
                return {"status": "unsupported"}
            try:
                if str(await adapter.self_uid()) != str(row["binding"]["account_id"]):
                    return {"status": "check_failed", "reason": "account_changed"}
            except (ValueError, AttributeError):
                return {"status": "check_failed", "reason": "account_unavailable"}
            payload = row["feedback"]["sent_payload"]
            target = row["binding"]["target"] | {"message_kind": payload.get("message_kind", "text")}
            return await adapter.reconcile_publication(
                target,
                author_uid=row["binding"]["account_id"],
                text=payload.get("text", ""),
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )

        def commit(receipt, verification):
            encoded = json.dumps(receipt, ensure_ascii=False)
            self.store.execute(
                "UPDATE dm_drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                (encoded, time.time(), identity),
            )
            self.store.execute(
                "UPDATE dm_attempts SET status='published',receipt=? WHERE id=(SELECT MAX(id) FROM dm_attempts WHERE draft_id=?)",
                (encoded, identity),
            )
            if self.memes:
                self.memes.release(f"dm-draft:{identity}")
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            character = self.store.character(draft["character_id"], draft["character_version"])
            payload = feedback.coordinator.find("messages", identity)["feedback"]["sent_payload"]
            spoken = (
                f"[发送表情包：{payload['meme_id']}]"
                if payload.get("message_kind") == "image"
                else payload["text"]
            )
            check = json.loads(draft.get("answer_check") or "{}")
            self.runtime.context.record(
                self.scope(draft),
                character,
                self._event(draft, character),
                spoken,
                "direct_message",
                memory_kind=None if payload.get("message_kind") == "image" else check.get("memory_kind"),
            )

        return await feedback.reconcile("messages", identity, read, commit)

    async def send(self, identity, *, automatic=False, authorization_epoch=None):
        return await FeedbackPublication(self.runtime, self.store, self.operations).submit(
            "messages",
            identity,
            prepare=lambda: self._publication_context(identity, automatic, authorization_epoch),
            send=lambda ticket, binding, body_hash: self._send_once(
                identity,
                automatic=automatic,
                authorization_epoch=authorization_epoch,
                revision_ticket=ticket,
                binding=binding,
                body_hash=body_hash,
            ),
            apply_revision=lambda prepared, value: self._apply_revision(identity, prepared, value),
            automatic=automatic,
            epoch=authorization_epoch,
            reconcile=lambda token, prepared: self._reconcile(identity),
        )

    async def _send_once(
        self,
        identity,
        *,
        automatic=False,
        authorization_epoch=None,
        revision_ticket=None,
        binding=None,
        body_hash=None,
    ):
        async with self.send_lock:
            self._enabled()
            policy = self.policy()
            if automatic and not policy["auto_send"]:
                raise ValueError("自动发送私信已关闭，保留草稿")
            row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not row or row["status"] not in ({"pending_review"} if automatic else {"approved", "copied"}):
                raise ValueError("私信尚未审核、已发送或结果待核对，不能发送")
            character = self.store.character(row["character_id"], row["character_version"])
            source = self._source(row["source_kind"], row["source_id"], character)
            if source["platform_user_id"] != row["platform_user_id"] or source["platform"] != row["platform"]:
                raise ValueError("来源用户与私信收件人不一致")
            if row["platform"] != "bilibili" and not self.injected:
                raise ValueError("该平台尚未接入私信发送")
            if row["source_kind"] == "comment" and not policy["allow_comment_handoff"]:
                raise ValueError("评论转私信已关闭")
            if (
                automatic
                and row["source_kind"] == "message"
                and source["sent_at"]
                < max(int(self.enabled_at), self.started_at, int(self.operations.policy()["enabled_at"] or 0))
            ):
                raise ValueError("历史来信不自动追发，保留草稿")
            for seconds, maximum in ((3600, policy["max_per_hour"]), (86400, policy["max_per_day"])):
                if (
                    self.store.one(
                        "SELECT COUNT(*) AS n FROM dm_attempts WHERE started_at>?", (time.time() - seconds,)
                    )["n"]
                    >= maximum
                ):
                    raise ValueError("私信发送达到已配置频率上限")
            if self.store.one(
                "SELECT id FROM dm_attempts WHERE platform_user_id=? AND started_at>?"
                + (" AND draft_id<>?" if revision_ticket else ""),
                (
                    row["platform_user_id"],
                    time.time() - policy["recipient_cooldown_seconds"],
                    *((identity,) if revision_ticket else ()),
                ),
            ):
                raise ValueError("该用户私信发送仍在冷却中")
            adapter = await self._adapter()
            account_id = (
                str(source.get("account_id") or "injected-test-account")
                if self.injected
                else str(await adapter.self_uid())
            )
            if row["account_id"] and row["account_id"] != account_id:
                raise ValueError("该私信草稿属于其他账号，不能发送")
            if not self.injected and not row["account_id"]:
                self.store.execute(
                    "UPDATE dm_drafts SET status='failed',error='旧草稿未绑定账号，请重新生成并审核' WHERE id=?",
                    (identity,),
                )
                raise ValueError("旧草稿未绑定账号，请重新生成并审核；不会复用旧私密上下文")
            if row["source_kind"] == "message" and not self.injected:
                if not source["account_id"] or account_id != source["account_id"]:
                    raise ValueError("当前登录账号与收到该私信的账号不一致，不能回复")
            browsing = getattr(self.runtime, "browsing", None)
            if browsing:
                await browsing.validate_private_source(row)
            fresh = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            reviewed_fields = (
                "status",
                "text",
                "message_kind",
                "meme_id",
                "character_id",
                "character_version",
                "account_id",
                "platform",
                "platform_user_id",
                "source_kind",
                "source_id",
            )
            if not fresh or any(fresh.get(field) != row.get(field) for field in reviewed_fields):
                raise ValueError("准备期间私信草稿已变化，保留当前审核状态；请重新确认后发送")
            actual_binding = self._publication_binding(row, source, account_id, character)
            payload = self._outgoing(row, character)
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash(payload)
            ):
                raise ValueError("发送前私信原始绑定或内容已变化")
            image = None
            if row.get("message_kind") == "image":
                if not self.memes or not row.get("meme_id"):
                    raise ValueError("图片草稿缺少已登记表情")
                image = self.memes.read(row["meme_id"], character)
                if not hasattr(adapter, "send_image"):
                    raise ValueError("当前平台未提供图片私信接口，保留草稿")
            previous, now = row["status"], time.time()
            token = None
            credential = dict(self.comments.credentials)
            attempt = self.store.execute(
                "INSERT INTO dm_attempts(draft_id,platform_user_id,started_at,status) VALUES(?,?,?,'sending')",
                (identity, row["platform_user_id"], now),
            ).lastrowid
            self.store.execute(
                "UPDATE dm_drafts SET status='publishing',updated_at=? WHERE id=?", (now, identity)
            )
            try:

                def authorized(*, reserve=True):
                    nonlocal token
                    policy = self.policy()
                    if image:
                        asset = self.memes.resolve(row["meme_id"], character)
                        if asset["sha256"] != image["asset"]["sha256"]:
                            return False
                    latest = self.store.one(
                        "SELECT status,meme_id,text FROM dm_drafts WHERE id=?", (identity,)
                    )
                    if (
                        not latest
                        or latest["status"] != "publishing"
                        or latest["meme_id"] != row.get("meme_id")
                        or latest["text"] != row["text"]
                    ):
                        return False
                    if automatic:
                        self._origin_round(row, active=True)
                    browsing = getattr(self.runtime, "browsing", None)
                    if (
                        automatic
                        and self.operations.policy()["enabled"]
                        and browsing
                        and not browsing.semantic_ready()
                    ):
                        return False
                    if automatic and row["source_kind"] == "comment" and browsing:
                        browsing.assert_comment_evidence(self._event(row, character))
                    if not self.injected and self.credential_signature != (
                        self.comments.credentials.get("sessdata"),
                        self.comments.credentials.get("csrf"),
                    ):
                        return False
                    if (
                        not (
                            policy["enabled"]
                            and (not automatic or policy["auto_send"])
                            and (row["source_kind"] != "comment" or policy["allow_comment_handoff"])
                        )
                        or self.comments.credentials != credential
                    ):
                        return False
                    fresh = self._source(row["source_kind"], row["source_id"], character)
                    if (
                        fresh["content"] != source["content"]
                        or fresh["platform_user_id"] != row["platform_user_id"]
                    ):
                        return False
                    self.operations.assert_allowed(
                        automatic=automatic,
                        channel="messages",
                        account_id=account_id,
                        character=character,
                        epoch=authorization_epoch,
                    )
                    intent = canonical_intent(
                        row["platform"],
                        source["video_id"] if row["source_kind"] == "comment" else "dm",
                        source["comment_id"] if row["source_kind"] == "comment" else source["message_id"],
                        character["id"],
                    )
                    if reserve and token is None:
                        token = self.operations.reserve(
                            account_id,
                            character,
                            intent,
                            "messages",
                            recipient=row["platform_user_id"],
                            automatic=automatic,
                            limits=policy,
                            epoch=authorization_epoch,
                            binding=actual_binding,
                            content_hash=content_hash(payload),
                            revision_ticket=revision_ticket,
                        )
                    return True

                if image:
                    receipt = await adapter.send_image(
                        row["platform_user_id"],
                        image,
                        authorized=authorized,
                        prepare_authorized=lambda: authorized(reserve=False),
                    )
                else:
                    receipt = await adapter.send(row["platform_user_id"], row["text"], authorized=authorized)
                if not isinstance(receipt, dict) or not receipt.get("msg_key"):
                    raise PublishUncertain("平台没有返回有效消息ID")
                if image:
                    receipt = receipt | {
                        "message_kind": "image",
                        "meme_id": row["meme_id"],
                        "asset_sha256": image["asset"]["sha256"],
                    }
            except MessagePreparationCancelled:
                if token:
                    self.operations.finish(token, "rejected")
                self.store.execute(
                    "UPDATE dm_drafts SET status=?,error=?,updated_at=? WHERE id=?",
                    (previous, "图片或消息准备已取消，未提交私信", time.time(), identity),
                )
                self.store.execute(
                    "UPDATE dm_attempts SET status='failed',error='准备已取消，未提交私信' WHERE id=?",
                    (attempt,),
                )
                raise
            except (PublishUncertain, asyncio.CancelledError) as error:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "messages", identity, token, actual_binding, payload, "私信发送结果不确定"
                    )
                self._uncertain(identity, attempt)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise UnknownPublication("私信发送结果待核对，禁止重发", token) from None
            except ValueError as error:
                if token:
                    self.operations.finish(token, "rejected")
                self.store.execute(
                    "UPDATE dm_drafts SET status=?,error=?,updated_at=? WHERE id=?",
                    (previous, public_error(error), time.time(), identity),
                )
                self.store.execute(
                    "UPDATE dm_attempts SET status='failed',error=? WHERE id=?",
                    (public_error(error), attempt),
                )
                if isinstance(error, PlatformRejected):
                    error.write_token = token
                    error.body_hash = content_hash(payload)
                    error.sent_payload = payload
                raise
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "messages", identity, token, actual_binding, payload, "私信发送结果不确定"
                    )
                self._uncertain(identity, attempt)
                raise UnknownPublication("私信发送异常，结果待核对，禁止重发", token) from None
            try:
                encoded = json.dumps(receipt, ensure_ascii=False)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dm_drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                        (encoded, time.time(), identity),
                    )
                    self.store.db.execute(
                        "UPDATE dm_attempts SET status='published',receipt=? WHERE id=?", (encoded, attempt)
                    )
                if token:
                    self.operations.finish(token, "published", receipt=receipt)
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "messages", identity, token, actual_binding, payload, "平台成功后本地保存失败"
                    )
                self._uncertain(identity, attempt)
                raise UnknownPublication("平台已返回发送成功，本地记账失败，禁止重发", token) from None
            check = json.loads(row.get("answer_check") or "{}")
            if image:
                self.memes.release(f"dm-draft:{identity}")
            spoken = f"[发送表情包：{image['asset']['title']}]" if image else row["text"]
            self.runtime.context.record(
                self.scope(row),
                character,
                self._event(row, character),
                spoken,
                "direct_message",
                memory_kind=None if image else check.get("memory_kind"),
            )
            return {"status": "published", "receipt": receipt}

    def _uncertain(self, identity, attempt):
        self.store.execute(
            "UPDATE dm_drafts SET status='publish_uncertain',error='发送结果待核对，禁止重发',updated_at=? WHERE id=?",
            (time.time(), identity),
        )
        self.store.execute(
            "UPDATE dm_attempts SET status='uncertain',error='发送结果待核对' WHERE id=?", (attempt,)
        )

    def list(self):
        character = self.runtime.character
        account_id = self.display_account_id()
        if not account_id:
            return {
                "policy": self.policy(),
                "account_id": "",
                "users": [],
                "drafts": [],
                "private_continuations": [],
            }
        users = self.store.all(
            "SELECT platform,platform_user_id,MAX(display_name) AS display_name,MAX(id) AS recent FROM dm_inbox WHERE character_id=? AND account_id=? GROUP BY platform,platform_user_id ORDER BY recent DESC LIMIT 100",
            (character["id"], account_id),
        )
        drafts = self.store.all(
            "SELECT * FROM dm_drafts WHERE character_id=? AND account_id=? ORDER BY id DESC LIMIT 100",
            (character["id"], account_id),
        )
        known = {(user["platform"], user["platform_user_id"]) for user in users}
        for draft in drafts:
            draft.update(self._draft_view(draft))
            key = (draft["platform"], draft["platform_user_id"])
            if key not in known:
                users.append(
                    {"platform": key[0], "platform_user_id": key[1], "display_name": draft["display_name"]}
                )
                known.add(key)
        return {
            "policy": self.policy(),
            "account_id": account_id,
            "users": [
                {
                    **user,
                    "relationship": self.runtime.relationships.view(
                        character["id"],
                        user["platform"],
                        user["platform_user_id"],
                        user["display_name"],
                        account_id=account_id,
                    ),
                }
                for user in users
            ],
            "drafts": drafts,
            "private_continuations": private_generation_views(self.runtime, account_id, character),
        }

    def _draft_view(self, row):
        result = continuation_view(self.runtime, "messages", row)
        if result["continuation"] is None:
            result["continuation"] = generation_view(
                self.runtime, row.get("generation_continuation_id"), "messages", row["id"], private=True
            )
        return result

    def conversation(self, platform, uid):
        character = self.runtime.character
        account_id = self.display_account_id()
        if not account_id:
            return {"account_id": "", "messages": [], "drafts": [], "relationship": {}}
        return {
            "account_id": account_id,
            "messages": self.store.all(
                "SELECT * FROM dm_inbox WHERE platform=? AND platform_user_id=? AND character_id=? AND account_id=? ORDER BY id DESC LIMIT 100",
                (platform, uid, character["id"], account_id),
            )[::-1],
            "drafts": [
                self._draft_view(row)
                for row in self.store.all(
                    "SELECT * FROM dm_drafts WHERE platform=? AND platform_user_id=? AND character_id=? AND account_id=? ORDER BY id LIMIT 100",
                    (platform, uid, character["id"], account_id),
                )
            ],
            "relationship": self.runtime.relationships.view(
                character["id"], platform, uid, account_id=account_id
            ),
        }

    def display_account_id(self):
        if self.active_credential == self.comments.credentials:
            return self.active_account_id
        return self.comments.display_account_id()

    async def open(self):
        self.closed = False
        self.worker = asyncio.create_task(self._work())
        self.poller = asyncio.create_task(self._poll())

    def retry(self, identity):
        self._enabled()
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row or row["status"] not in {"failed", "cancelled"}:
            raise ValueError("只可重试未发送的生成失败任务；不重发结果不确定的私信")
        if self.queue.full():
            raise ValueError("私信生成队列已满")
        if row.get("video_evidence"):
            evidence = json.loads(row["video_evidence"])
            evidence.pop("origin_round_id", None)
            self.store.execute(
                "UPDATE dm_drafts SET video_evidence=? WHERE id=?",
                (json.dumps(evidence, ensure_ascii=False), identity),
            )
        self.store.execute(
            "UPDATE dm_drafts SET status='queued',error=NULL,updated_at=? WHERE id=?", (time.time(), identity)
        )
        self.queue.put_nowait((identity, False))
        return {"draft_id": identity}

    async def close(self):
        self.closed = True
        for task in (self.worker, self.poller):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.worker, self.poller) if task), return_exceptions=True)
        while not self.queue.empty():
            identity, _ = self.queue.get_nowait()
            self.store.execute(
                "UPDATE dm_drafts SET status='cancelled',error='服务停止' WHERE id=? AND status='queued'",
                (identity,),
            )
            self.queue.task_done()
        if self.adapter:
            await self.adapter.close()

    async def _work(self):
        while True:
            identity, automatic = await self.queue.get()
            try:
                await self._generate(identity, automatic)
            except asyncio.CancelledError:
                self.store.execute(
                    "UPDATE dm_drafts SET status='cancelled',error='停止私信生成' WHERE id=? AND status IN ('queued','generating')",
                    (identity,),
                )
                raise
            except Exception as error:
                self.error = public_error(error)
                continuation = getattr(error, "continuation", None)
                self.store.execute(
                    "UPDATE dm_drafts SET status='failed',error=?,generation_continuation_id=COALESCE(?,generation_continuation_id),updated_at=? WHERE id=? AND status IN ('queued','generating')",
                    (self.error, continuation["id"] if continuation else None, time.time(), identity),
                )
            finally:
                self.queue.task_done()

    async def _poll(self):
        while True:
            self.changed.clear()
            policy = self.policy()
            if policy["enabled"] and policy["auto_poll"]:
                try:
                    await self.sync(automatic=True)
                except Exception as error:
                    self.error = public_error(error)
            try:
                await asyncio.wait_for(self.changed.wait(), self.policy()["poll_interval_seconds"])
            except TimeoutError:
                pass
