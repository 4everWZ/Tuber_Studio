"""SQLite schema migrations, inbox ingestion, draft views, and conversation state queries."""

import asyncio
import json
import re
import time

from ....infra.platforms.bilibili.messages import message_sequence
from ...kernel.feedback import continuation_view, generation_view, private_generation_views
from ..attachments import decision as image_decision
from ..attachments import image_choice, row_ref, uploads
from .models import IncomingMessage
from .outreach import contact_state

_RECEIPT_CHARACTERS = """
    SELECT CAST(json_extract(CASE WHEN json_valid(receipt) THEN receipt END, '$.msg_key') AS TEXT) AS message_id,
           MIN(character_id) AS character_id
    FROM dm_drafts
    WHERE platform=? AND account_id=? AND platform_user_id=? AND status='published'
    GROUP BY message_id
    HAVING message_id IS NOT NULL AND COUNT(DISTINCT character_id)=1
"""


class DirectMessagesRepositoryMixin:
    def _init_storage(self, store):
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS dm_inbox(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,message_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,content TEXT NOT NULL,sent_at REAL,character_id TEXT,received_at REAL,UNIQUE(platform,message_id));
            CREATE TABLE IF NOT EXISTS dm_drafts(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,platform_user_id TEXT NOT NULL,display_name TEXT,source_kind TEXT,source_id INTEGER,character_id TEXT,character_version INTEGER,status TEXT,text TEXT,reason TEXT,answer_check TEXT,model TEXT,error TEXT,receipt TEXT,created_at REAL,updated_at REAL,UNIQUE(source_kind,source_id,character_id));
            CREATE TABLE IF NOT EXISTS dm_attempts(id INTEGER PRIMARY KEY,draft_id INTEGER,platform_user_id TEXT,started_at REAL,status TEXT,receipt TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS dm_delivery_steps(attempt_id INTEGER,kind TEXT,receipt TEXT NOT NULL,write_token TEXT,
                PRIMARY KEY(attempt_id,kind));
            CREATE TABLE IF NOT EXISTS dm_operator_replies(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,account_id TEXT NOT NULL,platform_user_id TEXT NOT NULL,message_id TEXT NOT NULL,content TEXT NOT NULL,sent_at REAL NOT NULL,character_id TEXT NOT NULL,UNIQUE(platform,account_id,message_id));
            CREATE INDEX IF NOT EXISTS dm_inbox_user ON dm_inbox(platform,platform_user_id,character_id,id);
            CREATE INDEX IF NOT EXISTS dm_operator_replies_lookup ON dm_operator_replies(platform,account_id,platform_user_id,character_id,sent_at);
        """)
        store.execute(
            "UPDATE dm_drafts SET status='queued',error='已保留生成进度，等待恢复' WHERE status IN ('queued','generating')"
        )
        store.execute(
            "UPDATE dm_drafts SET status='publish_uncertain',error='服务在发送时重启，私信结果待核对' WHERE status='publishing'"
        )
        store.execute("UPDATE dm_attempts SET status='uncertain',error='发送中重启' WHERE status='sending'")
        if "image_submission" not in {r[1] for r in store.db.execute("PRAGMA table_info(dm_attempts)")}:
            store.execute("ALTER TABLE dm_attempts ADD COLUMN image_submission TEXT")
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
            ("image_ref", "TEXT"),
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
        store.execute("CREATE TABLE IF NOT EXISTS dm_recovered_incoming(incoming_id INTEGER PRIMARY KEY)")
        if "origin" not in {row[1] for row in store.db.execute("PRAGMA table_info(dm_operator_replies)")}:
            store.execute("ALTER TABLE dm_operator_replies ADD COLUMN origin TEXT NOT NULL DEFAULT 'operator'")
        store.execute("""CREATE TABLE IF NOT EXISTS dm_incoming_settlements(
            incoming_id INTEGER PRIMARY KEY, outgoing_id TEXT NOT NULL)""")
        store.execute("""CREATE TABLE IF NOT EXISTS dm_sync_cursors(
            account_id TEXT, character_id TEXT, platform_user_id TEXT, newest INTEGER NOT NULL,
            before_sequence INTEGER NOT NULL, pending_newest INTEGER NOT NULL,
            PRIMARY KEY(account_id,character_id,platform_user_id))""")

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

    def _ingest_history(self, rows, uid, own_uid, display_name):
        count = 0
        receipt_characters = None
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
                if receipt_characters is None:
                    receipt_characters = {
                        receipt["message_id"]: receipt["character_id"]
                        for receipt in self.store.all(_RECEIPT_CHARACTERS, ("bilibili", str(own_uid), str(uid)))
                    }
                receipt_character = receipt_characters.get(msg_key)
                self.store.execute(
                    """INSERT INTO dm_operator_replies AS saved
                       (platform,account_id,platform_user_id,message_id,content,sent_at,character_id,sequence,origin)
                       VALUES('bilibili', ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(platform,account_id,message_id) DO UPDATE SET
                           sequence=CASE WHEN saved.sequence=0 THEN excluded.sequence ELSE saved.sequence END,
                           origin=CASE WHEN excluded.origin='oc' THEN 'oc' ELSE saved.origin END,
                           character_id=CASE WHEN excluded.origin='oc' THEN excluded.character_id ELSE saved.character_id END
                       WHERE saved.platform_user_id=excluded.platform_user_id
                         AND ((excluded.origin='oc' AND (saved.origin<>'oc' OR saved.character_id<>excluded.character_id))
                              OR (saved.sequence=0 AND excluded.sequence>0))""",
                    (str(own_uid), str(uid), msg_key, msg_text, ts, receipt_character or self.runtime.character["id"],
                     sequence, "oc" if receipt_character else "operator"),
                )

        return count

    def _outgoing_history(self, draft):
        account = tuple(draft[key] for key in ("platform", "account_id", "platform_user_id"))
        return self.store.all(
            f"""WITH receipts AS ({_RECEIPT_CHARACTERS})
                SELECT sent.id,sent.message_id,sent.content,sent.sent_at,sent.sequence,
                       CASE WHEN receipts.character_id IS NULL THEN 'operator' ELSE 'oc' END AS origin
                FROM dm_operator_replies sent LEFT JOIN receipts ON receipts.message_id=sent.message_id
                WHERE sent.platform=? AND sent.account_id=? AND sent.platform_user_id=?
                  AND COALESCE(receipts.character_id,sent.character_id)=?
                  AND (receipts.character_id IS NOT NULL OR sent.origin='operator')
                ORDER BY sent.sequence DESC,sent.sent_at DESC,sent.id DESC LIMIT 15""",
            (*account, *account, draft["character_id"]),
        )

    def _require_unchanged_draft(self, draft):
        fresh = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft["id"],))
        fields = ("status", "text", "source_kind", "source_id", "account_id", "platform_user_id", "character_id", "character_version", "message_kind", "meme_id", "image_ref")
        if not fresh or any(fresh.get(key) != draft.get(key) for key in fields):
            raise ValueError("准备期间私信草稿已变化，保留最新编辑或审核状态")

    def _inbox_cutoff(self, account_id):
        key = f"message_inbox_cutoff:{account_id}:{self.runtime.character['id']}"
        cutoff = self.store.get(key)
        baseline = max(int(self.enabled_at), int(self.operations.policy()["enabled_at"] or 0))
        if cutoff is None:
            cutoff = max(baseline, self.started_at)
            self.store.set(key, cutoff)
        return cutoff

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
        ref = row_ref(row)
        attachment = image_decision(self.runtime, "messages", row, ref)
        row = row | {"illustration": attachment,
                     "illustration_choice": image_choice(self.runtime, "messages", row, ref, attachment),
                     "image_ref": ref, "image_uploads": uploads(self.store, "messages", row["id"]),
                     "delivery_steps": [step | {"receipt": json.loads(step["receipt"])} for step in self.store.all(
                         "SELECT s.attempt_id,s.kind,s.receipt FROM dm_delivery_steps s JOIN dm_attempts a ON a.id=s.attempt_id "
                         "WHERE a.draft_id=? ORDER BY a.id,s.rowid", (row["id"],))]}
        row = row | {"type": row.get("contact_type") or ("reply" if row["source_kind"] == "message" else "outreach"),
                     "contact_state": contact_state(row)}
        for field in ("trigger_source", "outreach_check"):
            row[field] = json.loads(row[field]) if isinstance(row.get(field), str) else row.get(field)
        if row["source_kind"] == "message":
            try:
                audit = json.loads(row.get("answer_check") or "{}")
                row["reply_source_ids"] = audit.get("reply_incoming_ids") or [row["source_id"]]
            except (ValueError, AttributeError):
                row["reply_source_ids"] = []
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

    @staticmethod
    def scope(draft):
        if draft.get("account_id"):
            return "dm:" + json.dumps(
                [draft["platform"], draft["account_id"], draft["platform_user_id"]], separators=(",", ":")
            )
        return f"dm:{draft['platform']}:{draft['platform_user_id']}"

    async def pause(self):
        """Retain unread messages and generation tasks, revoking queued send grants."""
        was_started = self.worker is not None
        self.operations_resumed = False
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            _, _, identity, _ = self.queue.get_nowait()
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
