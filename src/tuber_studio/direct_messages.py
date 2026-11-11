"""Source-triggered private replies, separate contexts and one-shot delivery."""

import asyncio
import json
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass

import httpx
from pydantic import Field, StrictInt

from .generation_meter import GenerationMeter, budgeted_json
from .meme_selection import select_meme
from .message_outreach import (
    assess_outreach,
    confirmed_invitation,
    contact_state,
    invitation_anchor,
    invitation_key,
    verified_agreement,
)
from .models import OperationReply, StrictModel
from .operation_continuations import GenerationFeedback, content_hash
from .operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    continuation_view,
    generation_view,
    private_generation_views,
    publication_binding,
)
from .operation_generation import generate_with_recovery, generation_requires_review, reply_generation_binding
from .operation_identity import runtime_identity
from .operation_text import operation_output_diagnostics, validate_operation_text
from .operations_control import canonical_intent
from .platforms.bilibili import PublishUncertain
from .platforms.bilibili_messages import (
    BilibiliMessagesAdapter,
    MessagePreparationCancelled,
    message_sequence,
)
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
    max_per_hour: int = Field(default=5, ge=0, le=30)
    max_per_day: int = Field(default=20, ge=0, le=200)
    recipient_cooldown_seconds: int = Field(default=120, ge=10, le=86400)


class IncomingMessage(StrictModel):
    platform: str = Field(default="bilibili", pattern=r"^[a-z0-9_-]{1,32}$")
    message_id: str = Field(min_length=1, max_length=100)
    platform_user_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    timestamp: float = Field(ge=0)
    account_id: str = Field(default="", max_length=100)


class MessageInputChanged(ValueError):
    """New incoming messages superseded a draft before submission."""


class MessageIdentityChanged(MessageInputChanged):
    """The account profile changed after the latest pre-send refresh."""


class IncomingSettlement(StrictModel):
    incoming: StrictInt
    outgoing: StrictInt
    quote: str = Field(min_length=1, max_length=300)


class ConversationReply(StrictModel):
    text: str = ""
    reason: str = Field(min_length=1, max_length=1024)
    reply_to: list[StrictInt] = Field(default_factory=list, max_length=8)
    handled: list[IncomingSettlement] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class ConversationWindow:
    timeline: list[dict]
    refs: dict[int, int]
    links: dict[tuple[int, int], str | None]
    fingerprint: str


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
        self._scheduled = set()
        self.operations_resumed = False
        self.active_account_id = ""
        self.active_credential = None
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS dm_inbox(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,message_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,content TEXT NOT NULL,sent_at REAL,character_id TEXT,received_at REAL,UNIQUE(platform,message_id));
            CREATE TABLE IF NOT EXISTS dm_drafts(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,source_kind TEXT,source_id INTEGER,character_id TEXT,character_version INTEGER,status TEXT,text TEXT,reason TEXT,answer_check TEXT,model TEXT,error TEXT,receipt TEXT,created_at REAL,updated_at REAL,UNIQUE(source_kind,source_id,character_id));
            CREATE TABLE IF NOT EXISTS dm_attempts(id INTEGER PRIMARY KEY,draft_id INTEGER,platform_user_id TEXT,started_at REAL,status TEXT,receipt TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS dm_operator_replies(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,account_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,message_id TEXT NOT NULL,content TEXT NOT NULL,sent_at REAL NOT NULL,character_id TEXT NOT NULL,UNIQUE(platform,account_id,message_id));
            CREATE INDEX IF NOT EXISTS dm_inbox_user ON dm_inbox(platform,platform_user_id,character_id,id);
            CREATE INDEX IF NOT EXISTS dm_operator_replies_lookup ON dm_operator_replies(platform,account_id,platform_user_id,character_id,sent_at);
        """)
        store.execute(
            "UPDATE dm_drafts SET status='queued',error='已保留生成进度，等待恢复' WHERE status IN ('queued','generating')"
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
            ("resume_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
            ("contact_type", "TEXT NOT NULL DEFAULT ''"),
            ("purpose", "TEXT NOT NULL DEFAULT ''"),
            ("trigger_source", "TEXT"),
            ("outreach_check", "TEXT"),
            ("contact_key", "TEXT NOT NULL DEFAULT ''"),
            ("proposed_text", "TEXT NOT NULL DEFAULT ''"),
            ("proposal_identity", "TEXT"),
        ):
            if name not in columns:
                store.execute(f"ALTER TABLE dm_drafts ADD COLUMN {name} {definition}")
        store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE status='queued'")
        store.execute("CREATE UNIQUE INDEX IF NOT EXISTS dm_outreach_invitation ON dm_drafts(account_id,character_id,platform,platform_user_id,contact_key) WHERE contact_key<>''")
        for table in ("dm_inbox", "dm_operator_replies"):
            if "sequence" not in {row[1] for row in store.db.execute(f"PRAGMA table_info({table})")}:
                store.execute(f"ALTER TABLE {table} ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")
        store.execute("CREATE INDEX IF NOT EXISTS dm_inbox_sequence ON dm_inbox(platform,account_id,character_id,platform_user_id,sent_at,sequence,id)")
        if "origin" not in {row[1] for row in store.db.execute("PRAGMA table_info(dm_operator_replies)")}:
            store.execute("ALTER TABLE dm_operator_replies ADD COLUMN origin TEXT NOT NULL DEFAULT 'operator'")
        store.execute("""CREATE TABLE IF NOT EXISTS dm_incoming_settlements(
            incoming_id INTEGER PRIMARY KEY, outgoing_id TEXT NOT NULL)""")
        store.execute("""CREATE TABLE IF NOT EXISTS dm_sync_cursors(
            account_id TEXT, character_id TEXT, platform_user_id TEXT, newest INTEGER NOT NULL,
            before_sequence INTEGER NOT NULL, pending_newest INTEGER NOT NULL,
            PRIMARY KEY(account_id,character_id,platform_user_id))""")

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

    def ingest(self, value, *, sequence=0):
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
            if sequence and not existing["sequence"]:
                self.store.execute("UPDATE dm_inbox SET sequence=? WHERE id=?", (sequence, existing["id"]))
            return existing["id"], False
        self.store.ensure_user(message["platform"], message["platform_user_id"], message["display_name"])
        identity = self.store.execute(
            "INSERT INTO dm_inbox(platform,message_id,platform_user_id,display_name,content,sent_at,character_id,received_at,account_id,sequence) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                sequence,
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
            if automatic:
                self._inbox_cutoff(str(own_uid))
            count, errors = 0, []
            for uid in users[:20]:
                require_current_account()
                if not self.policy()["enabled"] or (automatic and not self.policy()["auto_poll"]):
                    break
                try:
                    known = self.store.one(
                        "SELECT display_name FROM users WHERE platform='bilibili' AND platform_user_id=?",
                        (uid,),
                    )
                    def require_read_permission():
                        require_current_account()
                        if not self.policy()["enabled"] or (automatic and not self.policy()["auto_poll"]):
                            raise ValueError("私信读取已停止")

                    imported, _ = await self._sync_history(
                        adapter, uid, own_uid, known["display_name"] if known else f"UID {uid}",
                        require_read_permission, cutoff=self._inbox_cutoff(str(own_uid)) if automatic else None,
                    )
                    count += imported
                except (ValueError, KeyError, TypeError) as error:
                    errors.append(f"UID {uid}：{public_error(error)}")
            require_current_account()
            if automatic and self.policy()["auto_poll"]:
                self._queue_newest_messages(str(own_uid))
            self.last_sync = time.time()
            self.error = "；".join(errors)[:500] or None
            return {"imported": count, "errors": errors}

    async def _sync_history(self, adapter, uid, account, name, guard, *, cutoff=None, require_latest=False):
        if not hasattr(adapter, "history_page"):
            rows = await adapter.history(uid)
            guard()
            return self._ingest_history(rows, uid, account, name), True
        scope = (str(account), self.runtime.character["id"], str(uid))
        state = self.store.one(
            "SELECT newest,before_sequence,pending_newest FROM dm_sync_cursors WHERE account_id=? AND character_id=? AND platform_user_id=?",
            scope,
        ) or {"newest": 0, "before_sequence": 0, "pending_newest": 0}
        newest, before, pending = state["newest"], state["before_sequence"], state["pending_newest"]
        refreshed_head = not before
        count = 0
        for _ in range(3):
            rows, next_page, latest = await adapter.history_page(uid, before=before)
            guard()
            if self.runtime.character["id"] != scope[1]:
                raise ValueError("读取期间角色已变化")
            count += self._ingest_history(rows, uid, account, name)
            pending = max(pending, latest)
            before = next_page
            if before and (
                (newest and before <= newest)
                or (cutoff is not None and any(float(row.get("timestamp", 0)) < cutoff for row in rows))
            ):
                before = 0
            self.store.execute(
                "INSERT OR REPLACE INTO dm_sync_cursors VALUES(?,?,?,?,?,?)",
                (
                    *scope,
                    newest if before else pending,
                    before,
                    pending if before else 0,
                ),
            )
            if not before and (refreshed_head or not require_latest):
                return count, True
            if not before:
                newest, pending = pending, 0
                refreshed_head = True
        return count, False

    def _ingest_history(self, rows, uid, own_uid, display_name):
        count = 0
        for row in sorted(rows, key=lambda row: (message_sequence(row.get("msg_seqno")), int(row.get("timestamp", 0)))):
            if row.get("receiver_type", 1) != 1 or row.get("msg_type") != 1 or row.get("msg_status", 0) != 0:
                continue
            sender = str(row.get("sender_uid", ""))
            receiver = str(row.get("receiver_id", ""))
            msg_key = str(row.get("msg_key", ""))
            sequence = message_sequence(row.get("msg_seqno"))
            if not re.fullmatch(r"[1-9][0-9]{0,19}", msg_key):
                raise ValueError("平台私信缺少有效消息ID")
            content = row.get("content")
            content = json.loads(content) if isinstance(content, str) else content
            msg_text = content.get("content", "") if isinstance(content, dict) else str(content)
            if not isinstance(msg_text, str) or not msg_text.strip():
                continue
            ts = float(row.get("timestamp", 0))

            if sender == str(uid) and receiver == str(own_uid):
                _, fresh = self.ingest({
                    "message_id": msg_key,
                    "platform_user_id": str(uid),
                    "display_name": display_name,
                    "text": msg_text,
                    "timestamp": ts,
                    "account_id": str(own_uid),
                }, sequence=sequence)
                count += int(fresh)
            elif sender == str(own_uid) and receiver == str(uid):
                is_system = bool(
                    self.store.one("SELECT id FROM dm_drafts WHERE account_id=? AND platform_user_id=? "
                        "AND platform='bilibili' AND status='published' AND json_valid(receipt) "
                        "AND CAST(json_extract(receipt,'$.msg_key') AS TEXT)=?", (str(own_uid), str(uid), msg_key))
                )
                self.store.execute(
                        "INSERT OR IGNORE INTO dm_operator_replies"
                        "(platform, account_id, platform_user_id, message_id, content, sent_at, character_id, sequence,origin) "
                        "VALUES('bilibili', ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(platform,account_id,message_id) DO UPDATE SET sequence=excluded.sequence,origin=excluded.origin "
                        "WHERE dm_operator_replies.origin<>excluded.origin OR dm_operator_replies.sequence=0",
                        (
                            str(own_uid),
                            str(uid),
                            msg_key,
                            msg_text,
                            ts,
                            self.runtime.character["id"],
                            sequence,
                            "oc" if is_system else "operator",
                        ),
                    )

        return count

    async def _refresh_conversation_inbox(self, draft):
        if self.injected or not hasattr(await self._adapter(), "history"):
            return True
        if self.runtime.character["id"] != draft["character_id"]:
            raise ValueError("当前OC已变化，请切回原会话角色")
        async with self.sync_lock:
            credential = dict(self.comments.credentials)
            adapter = await self._adapter()
            account = str(await adapter.self_uid())
            self._require_unchanged_draft(draft)
            if account != draft["account_id"]:
                raise ValueError("当前账号与私信会话不一致")
            def guard():
                self._require_unchanged_draft(draft)
                if self.adapter is not adapter or credential != self.comments.credentials:
                    raise ValueError("读取期间账号已变化，未混入私密上下文")

            _, complete = await self._sync_history(adapter, draft["platform_user_id"], account, draft["display_name"],
                guard, cutoff=self._inbox_cutoff(account), require_latest=True)
            return complete

    def _require_unchanged_draft(self, draft):
        fresh = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft["id"],))
        fields = ("status", "text", "source_kind", "source_id", "account_id", "platform_user_id", "character_id", "character_version", "message_kind", "meme_id")
        if not fresh or any(fresh.get(key) != draft.get(key) for key in fields):
            raise ValueError("准备期间私信草稿已变化，保留最新编辑或审核状态")

    def _inbox_cutoff(self, account_id):
        key = f"message_inbox_cutoff:{account_id}:{self.runtime.character['id']}"
        cutoff = self.store.get(key)
        baseline = max(int(self.enabled_at), int(self.operations.policy()["enabled_at"] or 0))
        if cutoff is None or cutoff < baseline:
            cutoff = max(baseline, self.started_at if cutoff is None else 0)
            self.store.set(key, cutoff)
        return cutoff

    def _queue_newest_messages(self, account_id):
        cutoff = self._inbox_cutoff(account_id)
        if not self._processing_authorized():
            return
        slots = self.queue.maxsize - self.queue.qsize()
        if not slots:
            return
        candidates = self.store.all(
            """WITH newest AS (
                   SELECT id,platform,platform_user_id,sent_at,
                          ROW_NUMBER() OVER (PARTITION BY platform,platform_user_id ORDER BY sequence DESC,sent_at DESC,id DESC) AS rank
                   FROM dm_inbox i WHERE account_id=? AND character_id=? AND sent_at>=?
                   AND NOT EXISTS (SELECT 1 FROM dm_incoming_settlements s WHERE s.incoming_id=i.id)
                   AND NOT EXISTS (SELECT 1 FROM dm_sync_cursors c WHERE c.account_id=i.account_id
                       AND c.character_id=i.character_id AND c.platform_user_id=i.platform_user_id AND c.before_sequence<>0)
               ) SELECT id FROM newest n WHERE rank=1 AND NOT EXISTS (
                   SELECT 1 FROM dm_drafts d WHERE d.account_id=? AND d.character_id=?
                   AND d.platform=n.platform AND d.platform_user_id=n.platform_user_id
                   AND ((d.source_kind='message' AND d.source_id=n.id) OR
                        (d.status IN ('queued','generating','publishing','publish_uncertain') AND d.character_version=?))
               ) ORDER BY sent_at DESC,id DESC LIMIT ?""",
            (
                account_id,
                self.runtime.character["id"],
                cutoff,
                account_id,
                self.runtime.character["id"],
                self.runtime.character["version"],
                slots,
            ),
        )
        for candidate in candidates:
            self.enqueue("message", candidate["id"], automatic=True, continuous=True)

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
        purpose="",
        trigger_source=None,
        proposed_text="",
        proposal_identity=None,
        contact_trigger=None,
        continuous=False,
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
        trigger = trigger_source or (
            {"kind": "message", "ids": [identity], "message_id": source["message_id"]}
            if kind == "message" else
            {"kind": "comment", "comment_id": source["comment_id"], "bvid": source["video_id"], "root": source["thread_id"]}
        )
        if kind == "comment":
            trigger["contact_trigger"] = contact_trigger
        elif continuous:
            trigger["continuous"] = True
        if automatic and self.operations.policy()["enabled"]:
            trigger["automatic_epoch"] = self.operations.policy()["epoch"]
        draft_id = self.store.execute(
            "INSERT INTO dm_drafts(platform,platform_user_id,display_name,source_kind,source_id,character_id,character_version,status,reason,created_at,updated_at,account_id,video_evidence,contact_type,purpose,trigger_source,proposed_text,proposal_identity) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)",
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
                "reply" if kind == "message" else "outreach",
                str(purpose).strip(),
                json.dumps(trigger, ensure_ascii=False),
                validate_operation_text(proposed_text) if proposed_text else "",
                json.dumps(proposal_identity, ensure_ascii=False) if proposal_identity else None,
            ),
        ).lastrowid
        self.authorizations[draft_id] = (
            self.operations.policy()["epoch"] if automatic and self.operations.policy()["enabled"] else None
        )
        if schedule:
            self._schedule(draft_id, automatic)
        return draft_id

    async def route_comment(self, row, character, event, *, automatic=False):
        policy = self.policy()
        if not (policy["enabled"] and policy["allow_comment_handoff"] and row.get("platform_user_id")):
            return None
        evidence = dict(event.get("video_evidence") or {})
        thread = evidence.get("public_source_thread") or event.get("comment_context")
        if thread is None:
            thread = self.comments._comment_actors(row, character, event.get("account_id"))["comment_context"]
        invitation = confirmed_invitation(thread, event.get("account_id"), row["platform_user_id"])
        if not invitation:
            return None
        evidence["public_source_thread"] = thread
        return self.enqueue(
            "comment", row["id"], automatic=automatic, reason="user_requested", character=character,
            account_id=event.get("account_id", ""), video_evidence=evidence,
            purpose=invitation["quote"], contact_trigger=invitation["kind"],
        )

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
            "recipient": {"uid": draft["platform_user_id"], "name": draft["display_name"]},
            "self_actor": runtime_identity(self.runtime, character, draft.get("account_id")),
            "delivery_channel": "private_message",
            "account_id": draft.get("account_id", ""),
            "contact_type": "reply" if draft["source_kind"] == "message" else "outreach",
            "contact_purpose": draft.get("purpose") or "",
        }
        if draft.get("video_evidence"):
            event["video_evidence"] = json.loads(draft["video_evidence"])
        if draft["source_kind"] == "comment":
            event["agreement_id"] = draft["id"]
            trigger = json.loads(draft.get("trigger_source") or "{}")
            scope = self.comments.account_context_scope(
                source["video_id"], source["thread_id"], source["platform"], account_id=draft["account_id"]
            )
            event["public_source_thread"] = self.runtime.context.history_for_model(scope, character)
            if event.get("video_evidence", {}).get("public_source_thread"):
                event["public_source_thread"] = event["video_evidence"]["public_source_thread"]
            invitation = confirmed_invitation(
                event["public_source_thread"], draft.get("account_id"), draft["platform_user_id"]
            )
            if invitation:
                event["contact_invitation"] = invitation
            if trigger.get("contact_trigger") == "agreement" and verified_agreement(draft, event["public_source_thread"]):
                event["agreement_task"] = {"agreement_id": draft["id"], "purpose": draft.get("purpose") or "",
                    "completion_evidence": self._completion_evidence(draft),
                    "task": "完成原约定的查找后联系一次；尚未查到时保留约定等待，不把自称查到当完成证据"}
        else:
            conversation = self._reply_window(draft, source)
            event["conversation"] = conversation.timeline
            event["conversation_sha256"] = conversation.fingerprint
        if len(json.dumps(event, ensure_ascii=False)) > 1_000_000:
            raise ValueError("来源上下文过大，保留原文；请整理该楼后重新准备私信")
        return event

    @staticmethod
    def _ordered_messages(rows):
        by_time = sorted(rows, key=lambda row: (row["sent_at"], row["id"]))
        sequenced = iter(sorted((row for row in rows if row["sequence"]), key=lambda row: row["sequence"]))
        return [next(sequenced) if row["sequence"] else row for row in by_time]

    def _reply_window(self, draft, source):
        scope = tuple(draft[key] for key in ("platform", "platform_user_id", "character_id", "account_id"))
        common = "platform=? AND platform_user_id=? AND character_id=? AND account_id=?"
        order = " ORDER BY sequence DESC,sent_at DESC,id DESC"
        incoming = self.store.all(
            "SELECT i.id,message_id,content,sent_at,sequence,s.incoming_id AS settled,'viewer' AS origin "
            "FROM dm_inbox i LEFT JOIN dm_incoming_settlements s ON s.incoming_id=i.id WHERE "
            + common
            + " AND CASE WHEN sequence>0 AND ?>0 THEN sequence<=? ELSE (sent_at,i.id)<=(?,?) END"
            + order.replace("id DESC", "i.id DESC")
            + " LIMIT 30",
            (*scope, source["sequence"], source["sequence"], source["sent_at"], source["id"]),
        )
        outgoing = self.store.all(
            "SELECT id,message_id,content,sent_at,sequence,origin FROM dm_operator_replies WHERE "
            + common
            + order
            + " LIMIT 15",
            scope,
        )
        trigger = json.loads(draft.get("trigger_source") or "{}")
        cutoff = (
            self.store.get(f"message_inbox_cutoff:{draft['account_id']}:{draft['character_id']}", 0)
            if trigger.get("continuous")
            else 0
        )
        batch_start = max(source["sent_at"] - 600, cutoff)
        claimed = {
            item["source_id"]
            for item in self.store.all(
                "SELECT source_id FROM dm_drafts WHERE " + common + " AND id<>? AND source_kind='message' "
                "AND status IN ('queued','generating','published','publishing','publish_uncertain',"
                "'rejected','copied','pending_review','approved')",
                (*scope, draft["id"]),
            )
        }
        pending = [
            item
            for item in incoming
            if not item["settled"] and item["id"] not in claimed and item["sent_at"] >= batch_start
        ][:8]

        # An equal timestamp without two sequence values cannot prove which turn was first.
        def later(response, message):
            if response["sequence"] and message["sequence"]:
                return response["sequence"] > message["sequence"]
            return response["sent_at"] > message["sent_at"]

        timeline = self._ordered_messages([*incoming, *outgoing])
        refs, links, public = {}, {}, []
        pending_ids = {message["id"] for message in pending}
        for index, item in enumerate(timeline, 1):
            ref = {"message": index, "speaker": item["origin"], "text": item["content"]}
            if item["origin"] == "viewer" and item["id"] in pending_ids:
                ref["open"] = True
                refs[index] = item["id"]
            public.append(ref)
        for index, response in enumerate(timeline, 1):
            if response["origin"] != "operator":
                continue
            for i in refs:
                incoming = timeline[i - 1]
                if later(response, incoming):
                    links[(i, index)] = response["message_id"]
                elif response["sent_at"] == incoming["sent_at"] and not (
                    response["sequence"] and incoming["sequence"]
                ):
                    links[(i, index)] = None
        fingerprint = content_hash(
            [
                (message["message_id"], message["sequence"], view)
                for message, view in zip(timeline, public, strict=True)
            ]
        )
        return ConversationWindow(public, refs, links, fingerprint)

    def _settle_incoming(self, draft, incoming_ids, outgoing_id):
        self.store.db.executemany(
            "INSERT OR IGNORE INTO dm_incoming_settlements SELECT id,? FROM dm_inbox "
            "WHERE id=? AND account_id=? AND character_id=? AND platform=? AND platform_user_id=?",
            [
                (
                    str(outgoing_id),
                    value,
                    draft["account_id"],
                    draft["character_id"],
                    draft["platform"],
                    draft["platform_user_id"],
                )
                for value in incoming_ids
            ],
        )

    def _conversation_matches(self, draft, expected):
        if draft["source_kind"] != "message":
            return True
        source = self.store.one("SELECT * FROM dm_inbox WHERE id=?", (draft["source_id"],))
        conversation = self._reply_window(draft, source)
        return bool(conversation.refs) and (
            conversation.fingerprint == expected if expected else not conversation.links
        )

    def _requeue_changed_conversation(self, draft, automatic):
        self.store.execute(
            "UPDATE dm_drafts SET status='queued',resume_pending=1,next_attempt_at=0,error=NULL,updated_at=? "
            "WHERE id=? AND status NOT IN ('published','publish_uncertain','rejected')",
            (time.time(), draft["id"]),
        )
        self.authorizations[draft["id"]] = self.operations.policy()["epoch"] if automatic else None

    def _check_conversation(self, draft, expected, *, automatic):
        if not self._conversation_matches(draft, expected):
            self._requeue_changed_conversation(draft, automatic)
            raise MessageInputChanged("会话出现新发言，已保留原稿并准备更新回复")

    def _settle_delivered_reply(self, draft, receipt):
        if draft["source_kind"] == "message":
            check = json.loads(draft.get("answer_check") or "{}")
            self._settle_incoming(
                draft, check.get("reply_incoming_ids") or [draft["source_id"]], receipt["msg_key"]
            )

    def _delivered_event(self, draft, character):
        event = self._event(draft, character)
        if draft["source_kind"] != "message":
            return event
        check = json.loads(draft.get("answer_check") or "{}")
        if ids := check.get("reply_incoming_ids"):
            rows = self.store.all(
                "SELECT id,message_id,content,sent_at,sequence FROM dm_inbox WHERE platform=? AND platform_user_id=? "
                "AND character_id=? AND account_id=? AND id IN (SELECT value FROM json_each(?))",
                (
                    draft["platform"],
                    draft["platform_user_id"],
                    draft["character_id"],
                    draft["account_id"],
                    json.dumps(ids),
                ),
            )
            if rows:
                rows = self._ordered_messages(rows)
                event["text"] = "\n".join(row["content"] for row in rows)
                event["reply_sources"] = [row["message_id"] for row in rows]
        return event

    async def _reply_to_conversation(self, draft, character, event, relationship, refs, links):
        config = self.settings.data["llm"]
        maximum = config.get("operations_max_tokens", 8192)
        scope = self.scope(draft)
        binding = reply_generation_binding(scope, character, event, "direct_message") | {
            "conversation_sha256": event["conversation_sha256"],
        }
        snapshot = runtime_identity(self.runtime, character, draft["account_id"])
        binding["self_identity"] = snapshot
        messages = await self.runtime.context.prepare(
            scope,
            character,
            event,
            relationship,
            "direct_message",
            output_tokens=maximum,
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "这次只输出text、reason、reply_to、handled。conversation按真实先后列出用户、号主与已确认OC发言。"
                    "open为待处理来信。若号主已回答某条，handled填incoming与对应的operator发言号outgoing，"
                    "quote逐字引用真正解答该问题的号主原话。"
                    "reply_to列出本次text实际接话的open编号，text只回答这些未处理内容。意思相同的几条可一起处理。"
                    "全答过则text留空；不能判断的来信不填任何一栏，reason写明原因。发言号只放字段，勿写入正文。"
                ),
            }
        )
        schema = ConversationReply.model_json_schema()
        schema["properties"]["handled"]["items"] = schema.pop("$defs")["IncomingSettlement"]
        schema["properties"]["reply_to"]["items"]["enum"] = sorted(refs)
        schema["required"] = ["text", "reason", "reply_to", "handled"]

        def validate(value):
            result = ConversationReply.model_validate(
                value.model_dump() if hasattr(value, "model_dump") else value
            )
            replied = set(result.reply_to)
            handled = {item.incoming for item in result.handled}
            if (
                not result.reason.strip()
                or replied & handled
                or not replied <= refs.keys()
                or len(handled) != len(result.handled)
                or any(
                    not links.get((item.incoming, item.outgoing))
                    or item.quote.strip() not in event["conversation"][item.outgoing - 1]["text"]
                    or not item.quote.strip()
                    for item in result.handled
                )
                or bool(replied) != bool(result.text.strip())
            ):
                raise ValueError("私信接力决定与当前来信或实际号主回复不一致")
            if result.text:
                result.text = validate_operation_text(result.text)
            return result

        def guard():
            if (
                self.runtime.character["id"] != character["id"]
                or runtime_identity(
                    self.runtime,
                    character,
                    draft["account_id"],
                )
                != snapshot
            ):
                raise ValueError("私信生成期间账号或角色已变化")

        async def generate(packet, wire_schema):
            return await budgeted_json(self.runtime.llm, packet, wire_schema, max_tokens=maximum)

        async def invoke(packet, *, web_fetch=None):
            if web_fetch is not None:
                return await web_fetch.complete(packet, schema, generate)
            return await generate(packet, schema)

        decision, audit = await generate_with_recovery(
            self.runtime,
            channel="generation.direct_message",
            identity=event["event_id"] + ":" + content_hash(binding),
            binding=binding,
            character=character,
            messages=messages,
            generate=invoke,
            validate=validate,
            max_tokens=maximum,
            guard=guard,
            reuse_completed=True,
            allow_web_fetch=True,
            diagnose=lambda value: (
                "正文包含大量重复内容，保留原文待审"
                if value.text and operation_output_diagnostics(value.text)["needs_review"]
                else None
            ),
        )
        return decision, {
            "generation_identity": snapshot,
            "output": {
                "needs_review": any(value is None for value in links.values())
                or bool(audit and audit["state"] == "awaiting_review")
            },
        }

    async def _refresh_outreach_thread(self, draft):
        """Recover missing participant evidence without reading video material."""
        browsing = getattr(self.runtime, "browsing", None)
        adapter = getattr(browsing, "adapter", None)
        if draft["source_kind"] != "comment" or not hasattr(adapter, "read_video_thread"):
            return
        evidence = json.loads(draft.get("video_evidence") or "{}")
        thread = evidence.get("public_source_thread")
        if (
            isinstance(thread, dict) and "addressing" in thread
            and (thread.get("target") or {}).get("oid") and evidence.get("account_id") == draft["account_id"]
        ):
            return
        from .operations.browsing.thread_context import comment_thread_context

        character = self.store.character(draft["character_id"], draft["character_version"])
        source = self._source("comment", draft["source_id"], character)
        metadata = await browsing._read(adapter.video_metadata, source["video_id"])
        target = {"type": 1, "oid": str(metadata["aid"]), "bvid": metadata["bvid"],
                  "root": source["thread_id"] or source["comment_id"], "parent": source["comment_id"]}
        refreshed = await browsing._read(adapter.read_video_thread, target)
        self._require_unchanged_draft(draft)
        actual = refreshed["source"]
        if actual["text"] != source["content"] or str(actual["author_uid"]) != str(source["platform_user_id"]):
            raise ValueError("公开移交原文已变化，未沿旧来源联系")
        selected = next(item for item in refreshed["items"] if str(item["comment_id"]) == str(source["comment_id"]))
        thread = comment_thread_context(selected, refreshed["items"], target)
        thread["public_text"] = actual["text"]
        thread["resource_author_uid"] = str(metadata.get("owner_uid") or "")
        evidence.update(account_id=draft["account_id"], public_source_thread=thread, material_keys=evidence.get("material_keys", []),
                        source_versions=evidence.get("source_versions", []))
        draft["video_evidence"] = json.dumps(evidence, ensure_ascii=False)
        self.store.execute("UPDATE dm_drafts SET video_evidence=? WHERE id=?", (draft["video_evidence"], draft["id"]))

    @staticmethod
    def scope(draft):
        if draft.get("account_id"):
            return "dm:" + json.dumps(
                [draft["platform"], draft["account_id"], draft["platform_user_id"]], separators=(",", ":")
            )
        return f"dm:{draft['platform']}:{draft['platform_user_id']}"

    def _unanswered_outreach(self, draft):
        rows = self.store.all(
            "SELECT id,source_id,purpose,trigger_source,status,outreach_check,updated_at,contact_key FROM dm_drafts d "
            "WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? AND source_kind='comment' "
            "AND id<>? AND status IN ('published','publishing','publish_uncertain') AND NOT EXISTS ("
            "SELECT 1 FROM dm_inbox i WHERE i.account_id=d.account_id AND i.character_id=d.character_id "
            "AND i.platform=d.platform AND i.platform_user_id=d.platform_user_id AND i.sent_at>d.updated_at) "
            "ORDER BY updated_at DESC LIMIT 20",
            (draft["account_id"], draft["character_id"], draft["platform"], draft["platform_user_id"], draft["id"]),
        )
        result = []
        for row in rows:
            trigger = json.loads(row["trigger_source"] or "null")
            source = self.store.one("SELECT video_id,thread_id,comment_id,platform_user_id,display_name,content FROM comments WHERE id=?", (row["source_id"],))
            if not trigger:
                if source:
                    trigger = {"kind": "comment", "comment_id": source["comment_id"], "bvid": source["video_id"], "root": source["thread_id"]}
            original = None if not source else {"comment_id": str(source["comment_id"]),
                "author_uid": str(source["platform_user_id"]), "author_name": source["display_name"], "text": source["content"]}
            result.append(row | {"trigger_source": trigger, "public_source": original,
                                "outreach_check": json.loads(row["outreach_check"] or "null")})
        return result

    def _completion_evidence(self, draft):
        evidence = json.loads(draft.get("video_evidence") or "{}")
        result = []
        for proof in evidence.get("completion_evidence") or []:
            if not isinstance(proof, dict) or not proof.get("step_id"):
                continue
            saved = self.store.one("SELECT s.context,s.status,s.phase,c.binding FROM operation_continuation_steps s JOIN operation_continuations c ON c.id=s.continuation_id WHERE s.id=?", (proof["step_id"],))
            binding = json.loads(saved["binding"]) if saved else {}
            source = binding.get("source") or {}
            actor = binding.get("account_id") or source.get("account_id")
            if saved and saved["phase"] == "web_fetch" and saved["status"] == "ok" and source.get("agreement_id") == draft["id"] and str(actor) == draft["account_id"] and binding.get("character", {}).get("id") == draft["character_id"]:
                result.append({"step_id": proof["step_id"], **json.loads(saved["context"] or "{}")})
        for row in self.store.all(
            "SELECT s.id,s.context FROM operation_continuation_steps s JOIN operation_continuations c "
            "ON c.id=s.continuation_id WHERE c.channel='generation.direct_message' AND c.entity_id LIKE ? "
            "AND s.phase='web_fetch' AND s.status='ok' AND s.created_at>=?",
            (f"dm:{draft['id']}:%", draft["created_at"]),
        ):
            result.append({"step_id": row["id"], **json.loads(row["context"] or "{}")})
        return result

    async def fulfill_agreement(self, identity, step_id):
        """Wake the same stored agreement only after its bound real tool succeeds."""
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft or draft["source_kind"] != "comment" or draft["status"] != "waiting":
            return False
        trigger = json.loads(draft.get("trigger_source") or "{}")
        check = json.loads(draft.get("outreach_check") or "{}")
        if trigger.get("contact_trigger") != "agreement" or check.get("action") == "duplicate":
            return False
        character = self.store.character(draft["character_id"], draft["character_version"])
        if not verified_agreement(draft, self._event(draft, character).get("public_source_thread")):
            return False
        saved = self.store.one("SELECT s.status,s.phase,c.binding FROM operation_continuation_steps s JOIN operation_continuations c ON c.id=s.continuation_id WHERE s.id=?", (step_id,))
        binding = json.loads(saved["binding"]) if saved else {}
        source = binding.get("source") or {}
        if not saved or saved["phase"] != "web_fetch" or saved["status"] != "ok" or source.get("agreement_id") != identity or str(source.get("account_id")) != draft["account_id"] or binding.get("character", {}).get("id") != draft["character_id"]:
            return False
        evidence = json.loads(draft.get("video_evidence") or "{}")
        proofs = evidence.setdefault("completion_evidence", [])
        if not any(proof.get("step_id") == step_id for proof in proofs):
            proofs.append({"step_id": step_id})
        self.store.execute("UPDATE dm_drafts SET video_evidence=?,status='queued',proposed_text='',resume_pending=1,next_attempt_at=0,error=NULL WHERE id=?", (json.dumps(evidence, ensure_ascii=False), identity))
        if self._processing_authorized() and not self.queue.full():
            self._schedule(identity, self.policy()["auto_send"])
        return True

    async def _check_outreach(self, draft, event):
        if draft["source_kind"] != "comment":
            return True
        history = self._unanswered_outreach(draft)
        trigger = json.loads(draft.get("trigger_source") or "{}")
        # Changing the comment entry point in the same discussion is not a new contact.
        same_thread = next((row for row in history if row["trigger_source"] and
            row["trigger_source"].get("bvid") == trigger.get("bvid") and
            row["trigger_source"].get("root") == trigger.get("root")), None)
        thread = event.get("public_source_thread")
        current = (thread.get("source") or {}) if isinstance(thread, dict) else {}
        original_text = " ".join(str(current.get("text") or "").split())
        same_invitation = next((row for row in history if original_text and row["public_source"] and
            row["public_source"]["author_uid"] == str(current.get("author_uid")) and
            " ".join(row["public_source"]["text"].split()) == original_text), None)
        if same_thread or same_invitation or any(not row["trigger_source"] or not row["public_source"] for row in history):
            earlier = same_thread or same_invitation or history[0]
            self.store.execute("UPDATE dm_drafts SET status='waiting',error=NULL,outreach_check=?,updated_at=? WHERE id=?",
                (json.dumps({"action": "duplicate", "state": "awaiting_response", "previous_draft_id": earlier["id"],
                             "reason": "此前主动联系尚未收到回复，等待新的明确交流"}, ensure_ascii=False), time.time(), draft["id"]))
            return False
        completion = self._completion_evidence(draft)
        character = self.store.character(draft["character_id"], draft["character_version"])
        payload = self._outgoing(draft, character)
        checked_draft = dict(draft)
        if draft.get("message_kind") == "image":
            asset = self.memes.resolve(draft["meme_id"], character)
            checked_draft["outgoing_image"] = {key: asset.get(key) for key in ("asset_id", "title", "description", "emotions")}
        signature = content_hash({"outreach_contract": "program-bound-invitation-v3", "payload": payload, "thread": event.get("public_source_thread"),
                                  "account": draft["account_id"], "history": history, "completion": completion})
        previous = json.loads(draft.get("outreach_check") or "null")
        if previous and previous.get("signature") == signature:
            return previous.get("action") == "send"
        assessment, action = await assess_outreach(self.runtime, checked_draft, event, history, completion)
        self._require_unchanged_draft(draft)
        record = {"signature": signature, "action": action, "assessment": assessment.model_dump(),
                  "invitation_verified": invitation_anchor(assessment, draft, event.get("public_source_thread")) is not None}
        if action in {"wait", "duplicate"}:
            record["state"] = "awaiting_response" if history else "awaiting_completion" if assessment.invitation_kind == "agreement" and not completion else "awaiting_content" if not assessment.body_addresses_purpose else "awaiting_invitation"
        key = invitation_key(draft, assessment) if action == "send" else ""
        if key and self.store.one("SELECT id FROM dm_drafts WHERE contact_key=? AND id<>?", (key, draft["id"])):
            record["action"], action, key = "duplicate", "duplicate", ""
        self.store.execute(
            "UPDATE dm_drafts SET purpose=?,outreach_check=?,contact_key=?,status=?,error=NULL,updated_at=? WHERE id=?",
            (assessment.purpose.strip(), json.dumps(record, ensure_ascii=False), key,
             draft["status"] if action == "send" else "waiting" if action in {"wait", "duplicate"} else "not_initiated",
             time.time(), draft["id"]),
        )
        return action == "send"

    def _newer_incoming(self, draft, source):
        return self.store.one(
            "SELECT i.id,i.message_id FROM dm_inbox i WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? "
            "AND CASE WHEN sequence>0 AND ?>0 THEN sequence>? ELSE (sent_at,i.id)>(?,?) END "
            "AND NOT EXISTS (SELECT 1 FROM dm_incoming_settlements s WHERE s.incoming_id=i.id) "
            "ORDER BY sequence DESC,sent_at DESC,id DESC LIMIT 1",
            (draft["account_id"], draft["character_id"], draft["platform"], draft["platform_user_id"],
             source["sequence"], source["sequence"], source["sent_at"], source["id"]),
        )

    def _merge_incoming(self, identity, *, automatic=False):
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row or row["status"] in {"published", "publishing", "publish_uncertain"}:
            return False
        if row["source_kind"] == "comment":
            incoming = self.store.one(
                "SELECT id FROM dm_inbox WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? "
                "AND received_at>=? AND sent_at>=? ORDER BY sent_at DESC,id DESC LIMIT 1",
                (row["account_id"], row["character_id"], row["platform"], row["platform_user_id"], row["created_at"], int(row["created_at"])),
            )
            if incoming:
                reply = self.enqueue("message", incoming["id"], automatic=automatic,
                                     character=self.store.character(row["character_id"], row["character_version"]))
                self.store.execute("UPDATE dm_drafts SET status='waiting',resume_pending=0,error=NULL,outreach_check=? WHERE id=?",
                    (json.dumps({"action": "reply_received", "state": "reply_received", "reply_draft_id": reply}, ensure_ascii=False), identity))
                return True
            return False
        if row["source_kind"] != "message":
            return False
        source = self._source("message", row["source_id"], self.store.character(row["character_id"], row["character_version"]))
        latest = self._newer_incoming(row, source)
        if not latest:
            return False
        other = self.store.one("SELECT id FROM dm_drafts WHERE account_id=? AND character_id=? AND source_kind='message' AND source_id=? AND id<>?",
                               (row["account_id"], row["character_id"], latest["id"], identity))
        if other:
            self.store.execute("UPDATE dm_drafts SET status='cancelled',resume_pending=0,error=?,updated_at=? WHERE id=?",
                               ("新来信已合并到私信草稿 #" + str(other["id"]), time.time(), identity))
            return True
        trigger = json.loads(row.get("trigger_source") or "{}")
        trigger["ids"] = list(dict.fromkeys([*trigger.get("ids", [row["source_id"]]), latest["id"]]))
        trigger["message_id"] = latest["message_id"]
        self.store.execute("UPDATE dm_drafts SET source_id=?,trigger_source=?,status='queued',resume_pending=1,error=NULL,updated_at=? WHERE id=?",
                           (latest["id"], json.dumps(trigger, ensure_ascii=False), time.time(), identity))
        self.authorizations[identity] = self.operations.policy()["epoch"] if automatic else None
        return True

    async def _generate(self, identity, automatic):
        try:
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not draft:
                return
            if automatic and (not self.operations.policy()["enabled"] or self.operations.policy()["emergency"] or not self.policy()["enabled"]):
                self.store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE id=? AND status='queued'", (identity,))
                return
            scope = (draft["account_id"], draft["character_id"], "dm:" + draft["platform"], draft["platform_user_id"])
            async with self.runtime.conversation_turn(scope):
                return await self._generate_owned(identity, automatic)
        except MessageInputChanged:
            return
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE dm_drafts SET status='queued',resume_pending=1,next_attempt_at=0,error='生成进度已保存，等待恢复',updated_at=? WHERE id=? AND status IN ('queued','generating')",
                (time.time(), identity),
            )
            raise
        except Exception as error:
            transient = isinstance(
                error, (httpx.HTTPError, TimeoutError, ConnectionError, OSError)
            ) or getattr(error, "kind", None) in {"network", "timeout", "rate_limited", "anti_bot"}
            continuation = getattr(error, "continuation", None)
            updated = self.store.execute(
                "UPDATE dm_drafts SET status=?,resume_pending=?,next_attempt_at=?,error=?,generation_continuation_id=COALESCE(?,generation_continuation_id),updated_at=? WHERE id=? AND status IN ('queued','generating')",
                (
                    "queued" if transient else "failed",
                    int(transient),
                    time.time() + self.policy()["poll_interval_seconds"] if transient else 0,
                    public_error(error),
                    continuation["id"] if continuation else None,
                    time.time(),
                    identity,
                ),
            )
            if updated.rowcount and continuation:
                self.runtime.continuations.bind_source_view(
                    continuation["id"], "messages", identity, "私信回复生成"
                )
            raise

    async def _generate_owned(self, identity, automatic):
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft or draft["status"] != "queued":
            return
        if draft["source_kind"] == "message" and self.store.one(
            "SELECT 1 FROM dm_sync_cursors WHERE account_id=? AND character_id=? AND platform_user_id=? AND before_sequence<>0",
            (draft["account_id"], draft["character_id"], draft["platform_user_id"]),
        ):
            self.store.execute("UPDATE dm_drafts SET resume_pending=1,next_attempt_at=? WHERE id=?",
                               (time.time() + self.policy()["poll_interval_seconds"], identity))
            return
        if self.store.one(
            "SELECT 1 FROM character_archives WHERE character_id=? AND version=?",
            (draft["character_id"], draft["character_version"]),
        ):
            raise ValueError("角色版本已归档，保留原稿与来源，请使用当前角色重新准备回复")
        self._merge_incoming(identity, automatic=automatic)
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if draft["status"] != "queued":
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
        await self._refresh_outreach_thread(draft)
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
        if draft["source_kind"] == "comment":
            # Refresh before the one first-contact check, rather than approving
            # against stale actor data and then checking a replacement twice.
            observed = await self.runtime.refresh_identity(
                self.store.character(draft["character_id"], draft["character_version"]), draft["account_id"]
            )
            if observed.get("account_uid") != draft["account_id"]:
                raise ValueError("核对期间账号已变化，未读取旧私密上下文")
        if draft["source_kind"] == "comment":
            pending_event = self._event(draft, self.store.character(draft["character_id"], draft["character_version"]))
            invitation = confirmed_invitation(
                pending_event.get("public_source_thread"), draft.get("account_id"), draft["platform_user_id"]
            )
            if not invitation or invitation["kind"] == "agreement" and not self._completion_evidence(draft):
                await self._check_outreach(draft, pending_event)
                return
        async with self.runtime.offline_generation("dm"):
            if draft.get("resume_pending") and not self._processing_authorized():
                return
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
            handled, reply_ids = [], []
            if draft["source_kind"] == "message":
                window = self._reply_window(draft, source)
                refs, links = window.refs, window.links
                if not refs:
                    self.store.execute("UPDATE dm_drafts SET status='cancelled',resume_pending=0,error=? WHERE id=?",
                        ("来信已处理，无需重复发送", identity))
                    return
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
                snapshot = runtime_identity(self.runtime, character, draft["account_id"])
                proposal_identity = json.loads(draft.get("proposal_identity") or "null")
                researching = (event.get("agreement_task") or {}).get("completion_evidence") == []
                if draft.get("proposed_text") and proposal_identity == snapshot and not researching:
                    reply = OperationReply(spoken_text=draft["proposed_text"], reply_to=[event["event_id"]])
                    generation_id = (json.loads(draft.get("video_evidence") or "null") or {}).get(
                        "generation_continuation_id"
                    )
                    check = {"generation_identity": proposal_identity, "output": {
                        "needs_review": generation_requires_review(self.store, generation_id),
                    }, "reused_proposal": True}
                elif draft["source_kind"] == "message" and (links or len(refs) > 1):
                    decision, check = await self._reply_to_conversation(draft, character, event, relationship, refs, links)
                    handled = [(refs[item.incoming], links[(item.incoming, item.outgoing)]) for item in decision.handled]
                    reply_ids = [refs[ref] for ref in dict.fromkeys(decision.reply_to)]
                    reply = OperationReply(spoken_text=decision.text) if decision.text else None
                    check["operator_decision"] = decision.reason
                    if set(refs.values()) != set(reply_ids) | {incoming for incoming, _ in handled}:
                        check["output"]["needs_review"] = True
                else:
                    reply, check = await self.runtime.generate_reply(
                        self.scope(draft), character, event, relationship, mode="direct_message"
                    )
                    if draft["source_kind"] == "message":
                        reply_ids = list(refs.values())
                selected, selection_failed = None, False
                if reply and self.policy()["use_memes"] and self.memes:
                    try:
                        selected = await select_meme(
                            self.runtime, self.memes, character, event, reply.spoken_text
                        )
                        check["meme_selection"] = {"state": "selected" if selected else "text"}
                    except Exception:
                        check["meme_selection"] = {"state": "failed", "reason": "表情选择无效，保留待审文字"}
                        selection_failed = True
            if self._merge_incoming(identity, automatic=automatic):
                raise MessageInputChanged("新来信已合并，重新准备回复")
            if draft["source_kind"] == "message":
                self._check_conversation(draft, event["conversation_sha256"], automatic=automatic)
                with self.store.db:
                    for incoming, outgoing in handled:
                        self._settle_incoming(draft, [incoming], outgoing)
                    check["reply_incoming_ids"] = reply_ids
                    check["conversation_sha256"] = self._reply_window(draft, source).fingerprint
                    trigger = json.loads(draft.get("trigger_source") or "{}")
                    trigger["ids"] = sorted(set(trigger.get("ids", [])) | set(refs.values()))
                    self.store.db.execute("UPDATE dm_drafts SET trigger_source=? WHERE id=?",
                                          (json.dumps(trigger, ensure_ascii=False), identity))
                    if not reply:
                        complete = not check["output"].get("needs_review")
                        self.store.db.execute("UPDATE dm_drafts SET status=?,resume_pending=0,answer_check=?,error=?,updated_at=? WHERE id=?",
                            ("cancelled" if complete else "failed", json.dumps(check, ensure_ascii=False),
                            "号主已在平台手动回复，无需重复发送" if complete else decision.reason, time.time(), identity))
                if not reply:
                    return
            self.store.execute(
                "UPDATE dm_drafts SET status=?,resume_pending=0,next_attempt_at=0,text=?,answer_check=?,model=?,error=NULL,updated_at=? WHERE id=?",
                (
                    "generating" if draft["source_kind"] == "comment" else "pending_review",
                    reply.spoken_text,
                    json.dumps(check, ensure_ascii=False),
                    self.settings.data["llm"].get("model", "demo"),
                    time.time(),
                    identity,
                ),
            )
            if selected:
                # Model preparation is still in progress for outreach; it is not
                # a human review action and must not expose an approvable draft.
                owner = f"dm-draft:{identity}"
                self.memes.read(selected["asset_id"], character)
                self.memes.pin(selected["asset_id"], character, owner)
                try:
                    self.store.execute(
                        "UPDATE dm_drafts SET message_kind='image',meme_id=? WHERE id=?",
                        (selected["asset_id"], identity),
                    )
                except BaseException:
                    self.memes.release(owner, keep_asset_id=draft.get("meme_id"))
                    raise
                self.memes.release(owner, keep_asset_id=selected["asset_id"])
            elif selection_failed:
                self.store.execute(
                    "UPDATE dm_drafts SET error=? WHERE id=?",
                    ("表情选择失败，已保留待审文字，未自动发送", identity),
                )
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if draft["source_kind"] == "comment":
                qualified = await self._check_outreach(draft, event)
                if self._merge_incoming(identity, automatic=automatic):
                    raise MessageInputChanged("核对期间收到新来信，改为回应来信")
                if not qualified:
                    return
                self._require_unchanged_draft(draft)
                self.store.execute("UPDATE dm_drafts SET status='pending_review' WHERE id=? AND status='generating'", (identity,))
                draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if (
            automatic
            and self.policy()["auto_send"]
            and not selection_failed
            and not check.get("output", {}).get("needs_review")
        ):
            self._origin_round(draft, active=True)
            await self.send(identity, automatic=True, authorization_epoch=authorization_epoch, _conversation_owned=True)

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
            or parent["deadline"] is not None and parent["deadline"] <= time.time()
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
        check = json.loads(row.get("answer_check") or "{}")
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
                **({"reply_incoming_ids": check["reply_incoming_ids"]} if check.get("reply_incoming_ids") else {}),
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
            self.runtime.continuations.resume(continuation["id"], binding=continuation["binding"])
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
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            with self.store.db:
                self.store.db.execute("UPDATE dm_drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                    (encoded, time.time(), identity))
                self.store.db.execute("UPDATE dm_attempts SET status='published',receipt=? WHERE id=(SELECT MAX(id) FROM dm_attempts WHERE draft_id=?)",
                    (encoded, identity))
                self._settle_delivered_reply(draft, receipt)
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
                self._delivered_event(draft, character),
                spoken,
                "direct_message",
                memory_kind=None if payload.get("message_kind") == "image" else check.get("memory_kind"),
            )

        return await feedback.reconcile("messages", identity, read, commit)

    async def send(self, identity, *, automatic=False, authorization_epoch=None, _conversation_owned=False):
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft:
            raise ValueError("私信草稿不存在")
        if draft["status"] == "publish_uncertain":
            reconciled = await self._reconcile(identity)
            if reconciled is None:
                raise ValueError("私信仍待核对，不会重发")
            return reconciled
        self.operations.assert_review_writable(
            draft["account_id"] or self.display_account_id() or self.operations.policy()["account_id"]
        )
        if draft["status"] not in ({"pending_review"} if automatic else {"approved", "copied"}):
            raise ValueError("私信尚未审核、已发送或暂不满足联系条件，不能发送")
        if not _conversation_owned:
            scope = (draft["account_id"], draft["character_id"], "dm:" + draft["platform"], draft["platform_user_id"])
            async with self.runtime.conversation_turn(scope):
                return await self.send(identity, automatic=automatic, authorization_epoch=authorization_epoch, _conversation_owned=True)
        if not await self._refresh_conversation_inbox(draft):
            self._requeue_changed_conversation(draft, automatic)
            raise MessageInputChanged("私信历史仍在补齐，已保留进度，暂未发送")
        if self._merge_incoming(identity, automatic=automatic):
            if not self.queue.full():
                self._schedule(identity, automatic)
            raise MessageInputChanged("新来信已合并，重新准备回复后发送")
        try:
            self._check_conversation(draft, json.loads(draft.get("answer_check") or "{}").get("conversation_sha256"),
                                     automatic=automatic)
        except MessageInputChanged:
            if not self.queue.full():
                self._schedule(identity, automatic)
            raise
        character = self.store.character(draft["character_id"], draft["character_version"])
        observed = await self.runtime.refresh_identity(character, draft["account_id"])
        self._require_unchanged_draft(draft)
        previous = json.loads(draft.get("answer_check") or "{}").get("generation_identity")
        if observed.get("account_uid") != (draft["account_id"] or observed.get("account_uid")):
            raise ValueError("当前账号已变化，不能迁移或发送旧私密对话")
        if previous != observed:
            approved = draft["status"] in {"approved", "copied"}
            self.store.execute("UPDATE dm_drafts SET status='queued',proposed_text='',outreach_check=NULL,error=NULL WHERE id=?", (identity,))
            await self._generate_owned(identity, False)
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if draft["status"] != "pending_review":
                return self._draft_view(draft)
            if approved and not json.loads(draft.get("answer_check") or "{}").get("output", {}).get("needs_review"):
                self.review(identity, "approve")
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if draft["source_kind"] == "comment":
            await self._refresh_outreach_thread(draft)
            event = self._event(draft, character)
            async with self.runtime.offline_generation("dm"):
                if not await self._check_outreach(draft, event):
                    return self._draft_view(self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,)))
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
            draft = self.store.one("SELECT account_id FROM dm_drafts WHERE id=?", (identity,))
            if draft:
                self.operations.assert_review_writable(
                    draft["account_id"] or self.display_account_id() or self.operations.policy()["account_id"]
                )
            if self._merge_incoming(identity, automatic=automatic):
                if not self.queue.full():
                    self._schedule(identity, automatic)
                raise MessageInputChanged("发送前收到新来信，已合并重新准备")
            self._enabled()
            policy = self.policy()
            if automatic and not policy["auto_send"]:
                raise ValueError("自动发送私信已关闭，保留草稿")
            row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not row or row["status"] not in ({"pending_review"} if automatic else {"approved", "copied"}):
                raise ValueError("私信尚未审核、已发送或结果待核对，不能发送")
            if automatic and json.loads(row["answer_check"] or "{}").get("output", {}).get("needs_review"):
                raise ValueError("正文诊断尚待审核，未自动发送")
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
                < self._inbox_cutoff(row["account_id"])
            ):
                raise ValueError("历史来信不自动追发，保留草稿")
            for seconds, maximum in ((3600, policy["max_per_hour"]), (86400, policy["max_per_day"])):
                if (
                    maximum
                    and self.store.one(
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
            if row["source_kind"] == "comment":
                async with self.runtime.offline_generation("dm"):
                    if not await self._check_outreach(row, self._event(row, character)):
                        return self._draft_view(self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,)))
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
                    generated = json.loads(row.get("answer_check") or "{}").get("generation_identity")
                    if not self._conversation_matches(row, json.loads(row.get("answer_check") or "{}").get("conversation_sha256")):
                        raise MessageInputChanged("会话出现新发言，暂未提交私信")
                    if generated != runtime_identity(self.runtime, character, row["account_id"]):
                        raise MessageIdentityChanged("提交前账号资料已更新，按最新身份重新准备")
                    if row["source_kind"] == "message" and self._newer_incoming(row, source):
                        raise MessageInputChanged("提交前收到新来信，重新合并回复")
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
            except MessageInputChanged as error:
                if token:
                    self.operations.finish(token, "rejected")
                self.store.execute("UPDATE dm_drafts SET status=? WHERE id=?", (previous, identity))
                if isinstance(error, MessageIdentityChanged):
                    self.store.execute("UPDATE dm_drafts SET status='queued',proposed_text='',outreach_check=NULL,resume_pending=1,error=NULL WHERE id=?", (identity,))
                elif not self._merge_incoming(identity, automatic=automatic):
                    self._requeue_changed_conversation(row, automatic)
                if not self.queue.full():
                    self._schedule(identity, automatic)
                self.store.execute("UPDATE dm_attempts SET status='cancelled',error='新来信已合并，未提交' WHERE id=?", (attempt,))
                raise
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
                    self._settle_delivered_reply(row, receipt)
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
                self._delivered_event(row, character),
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
                "character_id": character["id"],
                "users": [],
                "drafts": [],
                "private_continuations": [],
            }
        users = self.store.all(
            "SELECT platform,platform_user_id,MAX(display_name) AS display_name,MAX(sent_at) AS recent FROM dm_inbox WHERE character_id=? AND account_id=? GROUP BY platform,platform_user_id ORDER BY recent DESC LIMIT 100",
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
            "character_id": character["id"],
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
        row = row | {"type": row.get("contact_type") or ("reply" if row["source_kind"] == "message" else "outreach"),
                     "contact_state": contact_state(row)}
        for field in ("trigger_source", "outreach_check"):
            row[field] = json.loads(row[field]) if isinstance(row.get(field), str) else row.get(field)
        result = continuation_view(self.runtime, "messages", row)
        if result["continuation"] is None:
            result["continuation"] = generation_view(
                self.runtime, row.get("generation_continuation_id"), "messages", row["id"], private=True
            )
        return result

    def conversation(self, platform=None, uid=None, *, inbox_id=None):
        character = self.runtime.character
        account_id = self.display_account_id()
        target = None
        if inbox_id is not None:
            target = account_id and self.store.one(
                "SELECT * FROM dm_inbox WHERE id=? AND account_id=? AND character_id=?",
                (inbox_id, account_id, character["id"]),
            )
            if not target:
                raise ValueError("当前账号和角色下找不到这条来信")
            platform, uid = target["platform"], target["platform_user_id"]
        if not account_id:
            return {"account_id": "", "character_id": character["id"], "messages": [], "drafts": [], "relationship": {}}
        incoming = self.store.all(
            "SELECT * FROM dm_inbox WHERE platform=? AND platform_user_id=? AND character_id=? AND account_id=? "
            "ORDER BY sequence DESC,sent_at DESC,id DESC LIMIT 100",
            (platform, uid, character["id"], account_id),
        )
        if target and not any(row["id"] == inbox_id for row in incoming):
            incoming.append(target)
        return {
            "account_id": account_id,
            "character_id": character["id"],
            "messages": self._ordered_messages(incoming),
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

    def _processing_authorized(self):
        return self.policy()["enabled"] and self.operations_resumed and self.operations.policy()["enabled"]

    def _schedule(self, identity, automatic):
        if identity in self._scheduled:
            return
        self.queue.put_nowait((identity, automatic))
        self._scheduled.add(identity)

    async def resume_pending(self):
        """Explicit activation of the operations inbox; never inferred at boot."""
        if self.operations.policy()["enabled"]:
            self.operations_resumed = True
        await self._resume_pending()

    async def _resume_pending(self):
        """Restore a few retained drafts after explicit activation, for review."""
        if self.closed or not self._processing_authorized() or self.queue.qsize() >= 2:
            return
        if not self.store.one(
            "SELECT id FROM dm_drafts WHERE status='queued' AND resume_pending=1 AND next_attempt_at<=? "
            "AND character_id=? AND character_version=? LIMIT 1",
            (time.time(), self.runtime.character["id"], self.runtime.character["version"]),
        ):
            return
        account_id = self.display_account_id()
        if not account_id:
            adapter = await self._adapter()
            credential = dict(self.comments.credentials)
            account_id, _ = await adapter.sessions()
            if self.adapter is not adapter or self.comments.credentials != credential:
                return
            self.active_account_id, self.active_credential = str(account_id), credential
        if self.closed or not self._processing_authorized():
            return
        scope = "account_id=?" if not self.injected else "account_id IN (?, '')"
        rows = self.store.all(
            "SELECT * FROM dm_drafts WHERE status='queued' AND resume_pending=1 AND next_attempt_at<=? "
            f"AND character_id=? AND character_version=? AND {scope} ORDER BY created_at,id LIMIT 3",
            (time.time(), self.runtime.character["id"], self.runtime.character["version"], str(account_id)),
        )
        for row in rows:
            if self.queue.qsize() >= 2 or self.queue.full():
                break
            if row["id"] in self._scheduled:
                continue
            if row.get("video_evidence"):
                evidence = json.loads(row["video_evidence"])
                if original := evidence.pop("origin_round_id", None):
                    evidence["retained_origin_round_id"] = original
                    self.store.execute(
                        "UPDATE dm_drafts SET video_evidence=? WHERE id=?",
                        (json.dumps(evidence, ensure_ascii=False), row["id"]),
                    )
            trigger = json.loads(row.get("trigger_source") or "{}")
            current = self.operations.policy()
            automatic = bool(self.policy()["auto_send"] and trigger.get("automatic_epoch") == current["epoch"]
                and row["account_id"] == current["account_id"] and row["character_id"] == current["character_id"])
            self.authorizations[row["id"]] = current["epoch"] if automatic else None
            self._schedule(row["id"], automatic)

    async def pause(self):
        """Retain unread messages and generation tasks, revoking queued send grants."""
        was_started = self.worker is not None
        self.operations_resumed = False
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            identity, _ = self.queue.get_nowait()
            self._scheduled.discard(identity)
            self.queue.task_done()
        self.authorizations.clear()
        self.store.execute(
            "UPDATE dm_drafts SET status='queued',resume_pending=1,next_attempt_at=0,error='进度已保存，等待恢复' "
            "WHERE status IN ('queued','generating')"
        )
        if was_started and not self.closed:
            self.worker = asyncio.create_task(self._work())

    def retry(self, identity):
        self._enabled()
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        check = json.loads(row.get("outreach_check") or "{}") if row else {}
        agreement = row and row["status"] == "waiting" and check.get("state") == "awaiting_completion" and check.get("invitation_verified") is True and not row.get("receipt") and not row.get("contact_key")
        if not row or row["status"] not in {"failed", "cancelled"} and not agreement:
            raise ValueError("只可重试未发送的生成失败任务；不重发结果不确定的私信")
        if self.store.one(
            "SELECT 1 FROM character_archives WHERE character_id=? AND version=?",
            (row["character_id"], row["character_version"]),
        ):
            raise ValueError("角色版本已归档，保留原稿与来源，请使用当前角色重新准备回复")
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
        automatic = bool(agreement and self._processing_authorized() and self.policy()["auto_send"])
        self.authorizations[identity] = self.operations.policy()["epoch"] if automatic else None
        self._schedule(identity, automatic)
        return {"draft_id": identity}

    async def close(self):
        self.closed = True
        await self.pause()
        for task in (self.worker, self.poller):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.worker, self.poller) if task), return_exceptions=True)
        if self.adapter:
            await self.adapter.close()

    async def _work(self):
        while True:
            identity, automatic = await self.queue.get()
            try:
                await self._generate(identity, automatic)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = public_error(error)
            finally:
                self._scheduled.discard(identity)
                self.queue.task_done()
                if not self.closed:
                    try:
                        retained = self.store.one("SELECT status,resume_pending,next_attempt_at FROM dm_drafts WHERE id=?", (identity,))
                        if retained and retained["status"] == "queued" and retained["resume_pending"] and retained["next_attempt_at"] <= time.time() and (not automatic or self._processing_authorized()):
                            self._schedule(identity, automatic)
                        await self._resume_pending()
                    except Exception as error:
                        self.error = public_error(error)

    async def _poll(self):
        while True:
            self.changed.clear()
            policy = self.policy()
            if policy["enabled"] and policy["auto_poll"]:
                try:
                    await self.sync(automatic=True)
                except Exception as error:
                    self.error = public_error(error)
            if policy["enabled"]:
                try:
                    await self._resume_pending()
                except Exception as error:
                    self.error = public_error(error)
            try:
                await asyncio.wait_for(self.changed.wait(), self.policy()["poll_interval_seconds"])
            except TimeoutError:
                pass
