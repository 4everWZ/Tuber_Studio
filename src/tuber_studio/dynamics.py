"""Public-source-only character operations; scheduling is separate from live jobs."""

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from contextlib import AsyncExitStack
from datetime import datetime

import httpx

from .agent import recall_memory
from .core.config import operation_profile
from .core.errors import PlatformRejected, SendLimitReached, UnknownPublication
from .core.identity import identity_messages, runtime_identity
from .core.meter import budgeted_json
from .core.models import content_hash
from .core.text import MAX_OPERATION_TEXT_BYTES, operation_character, operation_output_diagnostics
from .dynamic_comment_card import comment_card, render_comment_card
from .dynamic_materials import MaterialFilter, ensure_reflections, public_reads, reflections, source_links
from .infra.platforms.bilibili.client import PublishUncertain
from .infra.platforms.bilibili.dynamics import (
    PublishCancelled,
    PublishPreparationCancelled,
    PublishPreparationFailed,
    edit_dynamic_caption,
    publish_image,
    publish_text,
    read_dynamic_caption,
    validate_text,
)
from .infra.platforms.bilibili.images import bfs_image_path
from .operation_attachments import (
    checked_image,
    decision,
    image_choice,
    image_payload,
    image_scope,
    queue_image,
    record_upload,
    row_ref,
    uploads,
    wait_decision,
    workstation_image_decisions,
)
from .operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    continuation_view,
    generation_view,
    publication_binding,
)
from .operation_generation import generate_with_recovery
from .operations_control import OperationsControl, canonical_intent
from .operations_memory import OperationsMemory
from .persona.assets.illustration_policy import illustration_policy
from .persona.assets.meme_selection import select_meme
from .persona.characters.front_persona import expressive_persona
from .persona.context.delivery import memory_source_allowed
from .persona.memory.daily import DailyMemoryService
from .public_mentions import (
    MENTION_INSTRUCTION,
    MENTION_REQUESTS,
    bind_mentions,
    mention_requests,
    rebase_mentions,
)

MAX_SAFE_INTEGER = (1 << 53) - 1  # Exact integer range shared with browser JSON.

DEFAULT_POLICY = {
    "enabled": False,
    "auto_publish": True,
    "evaluate_interval_seconds": 1800,
    "min_post_interval_seconds": 21600,
    "max_posts_per_day": 2,
    "memory_summary_max_tokens": 2048,
}

# Older rows retain their diagnostics in storage. The workbench presents their
# current draft or publication state.
_RETIRED_CAPTION_DIAGNOSTICS = {
    "动态事实核对未完成，保留待审",
    "正文中的动作、次数或结果缺少来源支持，保留待审",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _has_legacy_chat_lineage(row):
    def items(field):
        value = row.get(field) or []
        return json.loads(value) if isinstance(value, str) else value

    def private(source):
        if isinstance(source, str):
            return source.startswith("daily-chat:")
        return (source.get("type") == "daily_chat" or str(source.get("id", "")).startswith("daily-chat:")
                or any(private(item) for item in source.get("memory_dependencies", [])))

    return any(private(source) for field in ("source_ids", "sources", "context_sources") for source in items(field))


def dynamic_decision_messages(character, recent_posts, public_sources, *, current_time):
    return [
        {
            "role": "system",
            "content": (
                "用自己的口吻和关注你的人分享。今天想聊什么、想聊多长，由你决定。"
                "public_incident是公开经历和原评论，published_reply是来评和你当时发出的回复，"
                "public_memory是角色故事摘录，runtime_event是运行记录，operator_note是号主确认的经历。"
                "随想、创作、兴趣、日常观察、角色小故事或公开互动都可以独立成稿，篇幅随话题。"
                "看过的视频、读到的公开评论和自己的心情都能单独分享，也可以自然地一起聊。"
                "几条素材在讲同一件事时可以合成一条。points 记录这次聊到的各个要点和对应的 source_ids。"
                "参考相关待发稿和已分享记录。后来只换说法重复同一个点就先等，有新发现或进展可以接着分享。"
                "source_ids 写实际用过的素材，随想可留空。想附具体链接才填 link_source_ids。"
                + MENTION_INSTRUCTION +
                "分享时选post，content写动态正文。暂缓时选wait，content留空。"
                "reason说明决定，next_evaluation_seconds是下次评估间隔。"
            ),
        },
        {
            "role": "user",
            "content": _json(
                {
                    "character": expressive_persona(operation_character(character)),
                    "current_time": current_time,
                    "recent_public_posts": recent_posts,
                    "public_sources": public_sources,
                }
            ),
        },
    ]


def _content_hash(text):
    # Whitespace, width/case and punctuation changes cannot evade duplicate checks.
    text = unicodedata.normalize("NFKC", text).casefold()
    return hashlib.sha256(
        "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P")).encode()
    ).hexdigest()


def _delivery_key(payload):
    text = payload["text"]
    pictures = payload.get("comment_card_page_sha256") or [
        payload.get("comment_card_sha256") or payload.get("asset_sha256")
    ]
    mentions = [(_content_hash(text[:item["start"]]), item["uid"]) for item in payload.get("mentions", [])]
    return content_hash([_content_hash(text), re.findall(r"https?://[^\s]+", text),
                         [item for item in pictures if item], mentions])


class Dynamics:
    def __init__(self, settings, store, runtime, comments, *, account=None, memes=None):
        self.settings, self.store, self.runtime, self.comments = settings, store, runtime, comments
        self.account = account or getattr(comments, "account", None) or comments
        self.memes = memes
        self.operations = getattr(runtime, "operations", None) or OperationsControl(store)
        runtime.operations = self.operations
        if not hasattr(runtime, "store"):
            runtime.store = store
        if not hasattr(runtime, "settings"):
            runtime.settings = settings
        self.evaluation_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.worker = None
        self.closed = False
        self._automatic_post_id = None
        self.memory_summary = OperationsMemory(store)
        self.daily_memory = getattr(runtime, "daily_memory", None) or DailyMemoryService(store)
        for sql in (
            "CREATE TABLE IF NOT EXISTS dynamic_memory_public(memory_id INTEGER PRIMARY KEY,"
            " fingerprint TEXT NOT NULL,marked_at REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS dynamic_decisions(id INTEGER PRIMARY KEY,"
            " character_id TEXT,character_version INTEGER,model TEXT,manual INTEGER,action TEXT,"
            " reason TEXT,content TEXT,source_ids TEXT,sources TEXT,next_evaluation_at REAL,"
            " created_at REAL,post_id INTEGER,status TEXT,error TEXT)",
            "CREATE TABLE IF NOT EXISTS dynamic_posts(id INTEGER PRIMARY KEY,decision_id INTEGER,"
            " character_id TEXT,character_version INTEGER,model TEXT,content TEXT,reason TEXT,"
            " source_ids TEXT,sources TEXT,status TEXT,receipt TEXT,error TEXT,created_at REAL,"
            " updated_at REAL,published_at REAL,attempted_at REAL,content_hash TEXT,manual INTEGER)",
            "CREATE INDEX IF NOT EXISTS dynamic_posts_attempt ON dynamic_posts(attempted_at)",
            "CREATE TABLE IF NOT EXISTS dynamic_edits(request_id TEXT PRIMARY KEY, post_id INTEGER NOT NULL,"
            " account_id TEXT NOT NULL, character_id TEXT NOT NULL, platform_id TEXT NOT NULL,"
            " original TEXT NOT NULL, caption TEXT NOT NULL, outgoing TEXT NOT NULL, status TEXT NOT NULL,"
            " error TEXT)",
            "CREATE UNIQUE INDEX IF NOT EXISTS dynamic_edits_inflight ON dynamic_edits(post_id)"
            " WHERE status IN ('submitting','pending_verification')",
        ):
            store.execute(sql)
        if "image_ref" not in {r[1] for r in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN image_ref TEXT")
        if "evaluation_account_id" not in {r[1] for r in store.db.execute("PRAGMA table_info(dynamic_decisions)")}:
            store.execute("ALTER TABLE dynamic_decisions ADD COLUMN evaluation_account_id TEXT")
        if "meme_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN meme_id TEXT")
        if "account_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
        if "generation_identity" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")
        }:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN generation_identity TEXT")
        if "comment_card_snapshot" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN comment_card_snapshot TEXT")
        if "context_sources" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN context_sources TEXT")
        if "send_payload" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN send_payload TEXT")
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}
        for name, definition in (("presentation", "TEXT"), ("delivery_key", "TEXT"), ("duplicate_of", "INTEGER")):
            if name not in columns:
                store.execute(f"ALTER TABLE dynamic_posts ADD COLUMN {name} {definition}")
        store.execute("CREATE INDEX IF NOT EXISTS dynamic_posts_delivery ON dynamic_posts(account_id,delivery_key)")
        if "mentions" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_edits)")}:
            store.execute("ALTER TABLE dynamic_edits ADD COLUMN mentions TEXT")
        ensure_reflections(store)
        if "generation_continuation_id" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(dynamic_decisions)")
        }:
            store.execute("ALTER TABLE dynamic_decisions ADD COLUMN generation_continuation_id TEXT")
        if "points" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_decisions)")}:
            store.execute("ALTER TABLE dynamic_decisions ADD COLUMN points TEXT")
        interrupted = store.all("SELECT id,send_payload FROM dynamic_posts WHERE status='publishing'")
        for post in interrupted:
            saved = json.loads(post["send_payload"] or "null")
            if saved:
                FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                    "dynamics", post["id"], saved["token"], saved["binding"], saved["payload"], "动态发送结果待核对",
                )
        store.execute(
            "UPDATE dynamic_posts SET status='publish_uncertain',"
            "error='写入中重启，请人工核对平台',updated_at=? WHERE status='publishing'",
            (time.time(),),
        )
        if interrupted:
            data = DEFAULT_POLICY | store.get("dynamic_policy", {})
            data["enabled"] = False
            store.set("dynamic_policy", data)
            self._schedule(86400, "存在不确定动态写入，请先人工核对平台再重新开启运营")
        store.execute(
            "UPDATE dynamic_edits SET status='pending_verification',error='请核对原动态的编辑结果'"
            " WHERE status='submitting'"
        )
        for row in store.db.execute(
            "SELECT id,source_ids,sources,context_sources FROM dynamic_posts WHERE attempted_at IS NULL "
            "AND status IN ('pending_review','approved','rejected') AND receipt IS NULL"
        ):
            try:
                private = _has_legacy_chat_lineage(dict(row))
            except (ValueError, TypeError, AttributeError):
                self._status(row["id"], "invalidated", "旧稿来源记录不完整，无法核验公开授权")
                continue
            if private:
                self._status(row["id"], "invalidated", "旧稿使用了未经披露授权的私聊素材，请从公开来源重新起草")

    def policy(self):
        base = DEFAULT_POLICY | self.store.get("dynamic_policy", {})
        policy = self.operations.effective("dynamics", base) | {
            "base": base,
            "credentials_configured": bool(self.account.credentials_configured),
            **self.store.get(
                "dynamic_schedule",
                {
                    "next_evaluation_at": None,
                    "backoff_until": None,
                    "last_error": None,
                },
            ),
        }
        if policy.get("last_error") in _RETIRED_CAPTION_DIAGNOSTICS:
            policy["last_error"] = None
        return policy

    async def set_policy(self, patch):
        if not isinstance(patch, dict) or not set(patch) <= set(DEFAULT_POLICY):
            raise ValueError("未知动态运营策略字段")
        data = DEFAULT_POLICY | self.store.get("dynamic_policy", {}) | patch
        for name in ("enabled", "auto_publish"):
            if type(data[name]) is not bool:
                raise ValueError("运营开关须为布尔值")
        for name, minimum in (
            ("evaluate_interval_seconds", 1),
            ("min_post_interval_seconds", 0),
            ("max_posts_per_day", 0),
            ("memory_summary_max_tokens", 1),
        ):
            if type(data[name]) is not int or not minimum <= data[name] <= MAX_SAFE_INTEGER:
                raise ValueError(f"{name} 须为不小于 {minimum} 的有效整数")
        if data["enabled"] and not self.policy()["credentials_configured"]:
            raise ValueError("开启动态运营需要先配置账号登录凭证")
        self.store.set("dynamic_policy", data)
        self.changed.set()
        return self.policy()

    def _character(self):
        return self.store.character()

    def _summary_model(self):
        fields = ("base_url", "model", "enable_thinking", "structured_output", "context_source", "prompt_token_endpoint")
        active = getattr(self.runtime.operation_llm, "config", {})
        active = active if isinstance(active, dict) else {}
        profile = operation_profile(self.settings.data["omni"])
        identity = {
            "settings": {k: profile.get(k) for k in fields},
            "provider": {k: active.get(k) for k in fields},
            "implementation": type(self.runtime.operation_llm).__module__ + "." + type(self.runtime.operation_llm).__qualname__,
        }
        return hashlib.sha256(_json(identity).encode()).hexdigest()

    def _summary_sources(self):
        sources, size = [], 0
        for source in self._sources():
            length = len(_json(source))
            if len(sources) < 8 and size + length <= 6000:
                sources.append(source)
                size += length
        return sorted(sources, key=lambda source: source["id"])

    def summary_status(self):
        return self.memory_summary.status(
            self._character(),
            [source for source in self._summary_sources() if source["type"] == "public_memory"],
            self.policy()["memory_summary_max_tokens"],
            self._summary_model(),
        )

    def _memory_rows(self, *, query=None, bound_ids=(), authorized_only=False):
        if not self.store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='grounded_memory'"):
            return []
        char = self._character()
        where, args = (query or MaterialFilter()).sql("g.text", "g.created_at", None)
        rows = self.store.all(
            "SELECT g.* FROM grounded_memory g " + (
                "JOIN dynamic_memory_public m ON m.memory_id=g.id " if authorized_only else ""
            ) + "WHERE character_id=? AND character_version=? "
            "AND role='assistant' AND kind='role_story' AND fictional=1 "
            "AND scope NOT LIKE 'dm:%' AND scope NOT LIKE 'operator:%' "
            "AND ('memory:'||g.id IN (SELECT value FROM json_each(?)) OR (1" + where + ")) "
            "ORDER BY ('memory:'||g.id IN (SELECT value FROM json_each(?))) DESC,g.id DESC LIMIT 200",
            (char["id"], char["version"], _json(bound_ids), *args, _json(bound_ids)),
        )
        account = runtime_identity(self.runtime, char).get("account_uid") or ""
        return [row for row in rows if memory_source_allowed(self.store, row, account_id=account)]

    @staticmethod
    def _memory_fingerprint(row):
        return hashlib.sha256(
            _json(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "scope",
                        "character_id",
                        "character_version",
                        "event_id",
                        "role",
                        "kind",
                        "fictional",
                        "text",
                        "created_at",
                    )
                }
            ).encode()
        ).hexdigest()

    def list_memories(self, limit=100):
        result = []
        for row in self._memory_rows()[: self._limit(limit, 200)]:
            mark = self.store.one(
                "SELECT fingerprint FROM dynamic_memory_public WHERE memory_id=?", (row["id"],)
            )
            result.append(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "scope",
                        "event_id",
                        "role",
                        "kind",
                        "text",
                        "created_at",
                        "character_id",
                        "character_version",
                    )
                }
                | {
                    "fictional": True,
                    "public": bool(mark and mark["fingerprint"] == self._memory_fingerprint(row)),
                }
            )
        return result

    def mark_memory(self, memory_id, public_bool):
        if type(public_bool) is not bool or type(memory_id) is not int:
            raise ValueError("记忆ID与公开开关无效")
        row = next((row for row in self._memory_rows() if row["id"] == memory_id), None)
        if row is None:
            raise ValueError("只能标记当前人物版本的虚构角色记忆")
        if public_bool:
            self.store.execute(
                "INSERT OR REPLACE INTO dynamic_memory_public VALUES(?,?,?)",
                (memory_id, self._memory_fingerprint(row), time.time()),
            )
        else:
            self.store.execute("DELETE FROM dynamic_memory_public WHERE memory_id=?", (memory_id,))
        return next(item for item in self.list_memories(200) if item["id"] == memory_id)

    def _memory_preference(self):
        organizer = getattr(self.runtime, "memory_organizer", None)
        scope = organizer.scope() if organizer and getattr(self.runtime, "browsing", None) else None
        return scope, self.daily_memory.preference(scope) if scope else {"enabled": True, "revision": 0}

    def _source_account(self):
        browsing = getattr(self.runtime, "browsing", None)
        discovery = getattr(browsing, "discovery", None)
        return str(getattr(self.account, "display_account_id", lambda: "")()
                   or (discovery.history_scope()[0] if discovery else getattr(browsing, "account", "")) or "")

    def _sources(self, *, bound_ids=(), query=None):
        char = self._character()
        browsing = getattr(self.runtime, "browsing", None)
        account_id = self._source_account()
        query = query or MaterialFilter()
        scope, preference = self._memory_preference()
        result = []
        if preference["enabled"]:
            result = self.daily_memory.public_incident_sources(char["id"], char["version"],
                                                                material_query=query, bound_ids=bound_ids)
            result.extend(self.daily_memory.public_experience_sources(char["id"], char["version"],
                                                                     material_query=query, bound_ids=bound_ids))
        if scope and preference["enabled"]:
            recall = ("dynamic", list(query.terms), query.resource, query.root) if query.terms or query.resource else None
            rows = self.daily_memory.organized(scope, recall=recall, limit=100)
            for identity in bound_ids:
                if identity.startswith("organized:") and identity[10:].isdecimal():
                    rows.extend(self.daily_memory.organized(scope, item_id=int(identity[10:])))
            for row in {row["id"]: row for row in rows}.values():
                if row["status"] != "active" or "dynamic" not in row["channels"] or not row["public_memory"]:
                    continue
                anchor = row["source"].get("thread") or {}
                public = row["public_memory"]
                references = public["references"]
                if not references:
                    continue
                result.append({"id": f"organized:{row['id']}", "type": "public_incident", "fictional": False,
                               "text": public["text"], "memory_version": row["updated_at"],
                               "observed_at": row["created_at"],
                               "source_url": references[0]["href"], "public_sources": [
                                   {"bvid": anchor["resource"], "thread_id": anchor["root_id"],
                                    "comment_id": ref["comment_id"], "quote": ref["text"]} for ref in references
                               ] if anchor.get("resource", "").startswith("BV") else []})
        if preference["revision"]:
            for source in result:
                source["memory_preference_revision"] = preference["revision"]
        has_account = any(row[1] == "account_id" for row in self.store.db.execute("PRAGMA table_info(drafts)"))
        scoped = bool(account_id and has_account)
        legacy_time = "d.updated_at"
        legacy_filter, legacy_args = query.sql("d.text||' '||c.content", legacy_time, "c.video_id")
        rows = self.store.all(
            "SELECT d.id,d.text,d.receipt,d.updated_at,c.video_id,c.comment_id,c.content FROM drafts d "
            "JOIN comments c ON c.id=d.comment_id WHERE d.status='published' AND c.platform='bilibili' "
            "AND json_extract(d.answer_check,'$.organized_memory') IS NULL "
            "AND d.character_id=? AND d.character_version=? " + (
                "AND (d.account_id=? OR d.account_id='' AND json_extract(d.receipt,'$.account_id')=?) " if scoped else ""
            ) + ("AND d.account_id='' " if browsing and has_account and not scoped else "")
            + "AND ('reply:'||d.id IN (SELECT value FROM json_each(?)) "
            "OR 'reply:'||json_extract(d.receipt,'$.rpid') IN (SELECT value FROM json_each(?)) OR (1" + legacy_filter
            + ")) ORDER BY ('reply:'||d.id IN (SELECT value FROM json_each(?)) "
            "OR 'reply:'||json_extract(d.receipt,'$.rpid') IN (SELECT value FROM json_each(?))) DESC,d.id DESC LIMIT 100",
            (char["id"], char["version"], *((account_id, account_id) if scoped else ()),
             _json(bound_ids), _json(bound_ids), *legacy_args, _json(bound_ids), _json(bound_ids)),
        )
        for row in rows:
            try:
                receipt = json.loads(row["receipt"] or "null")
                # A copied/imported row without an actual platform receipt is not public evidence.
                if not isinstance(receipt, dict) or not re.fullmatch(
                    r"[1-9][0-9]{0,19}", str(receipt.get("rpid", ""))
                ):
                    continue
            except (ValueError, TypeError):
                continue
            result.append(
                {
                    "id": f"reply:{row['id']}",
                    "type": "published_reply",
                    "text": row["text"],
                    "fictional": True,
                    "source_url": f"https://www.bilibili.com/video/{row['video_id']}",
                    "original_comment": {"id": row["comment_id"], "text": row["content"]},
                    "receipt_id": str(receipt["rpid"]),
                    "observed_at": row["updated_at"],
                }
            )
        material = list(reflections(self.store, account_id, char["id"], char["version"], bound_ids)) if account_id else []
        if browsing:
            if account_id:
                material.extend(public_reads(self.store, account_id, char["id"], bound_ids, query))
                reply_filter, reply_args = query.sql(
                    "text||' '||COALESCE(json_extract(target,'$.source_text'),'')", "updated_at",
                    "COALESCE(json_extract(target,'$.resource'),json_extract(target,'$.bvid'))",
                    "COALESCE(json_extract(target,'$.root'),json_extract(target,'$.root_id'))",
                )
                for row in self.store.all(
                    "SELECT id,text,target,receipt,updated_at FROM browsing_intents WHERE kind='reply_public' AND status='published' "
                    "AND json_extract(target,'$.memory_audit') IS NULL "
                    "AND account_id=? AND character_id=? AND character_version=? "
                    "AND ('reply:browsing:'||id IN (SELECT value FROM json_each(?)) "
                    "OR 'reply:'||json_extract(receipt,'$.rpid') IN (SELECT value FROM json_each(?)) OR (1" + reply_filter
                    + ")) ORDER BY ('reply:browsing:'||id IN (SELECT value FROM json_each(?)) "
                    "OR 'reply:'||json_extract(receipt,'$.rpid') IN (SELECT value FROM json_each(?))) DESC,updated_at DESC LIMIT 100",
                    (account_id, char["id"], char["version"], _json(bound_ids), _json(bound_ids), *reply_args,
                     _json(bound_ids), _json(bound_ids)),
                ):
                    target, receipt = json.loads(row["target"]), json.loads(row["receipt"] or "null")
                    if not isinstance(receipt, dict) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(receipt.get("rpid"))) or not (
                        re.fullmatch(r"BV[0-9A-Za-z]{10}", str(target.get("bvid"))) and target.get("source_text")
                    ):
                        continue
                    result.append({
                        "id": f"reply:browsing:{row['id']}", "type": "published_reply", "text": row["text"],
                        "fictional": False, "source_url": f"https://www.bilibili.com/video/{target['bvid']}",
                        "original_comment": {"id": target.get("parent"), "text": target["source_text"]},
                        "receipt_id": str(receipt["rpid"]),
                        "observed_at": row["updated_at"],
                        "reference": {"resource": target["bvid"], "thread_id": target.get("root") or target.get("root_id")},
                    })
        for row in self._memory_rows(query=query, bound_ids=bound_ids, authorized_only=True) if preference["enabled"] else ():
            mark = self.store.one(
                "SELECT fingerprint FROM dynamic_memory_public WHERE memory_id=?", (row["id"],)
            )
            if mark and mark["fingerprint"] == self._memory_fingerprint(row):
                result.append(
                    {
                        "id": f"memory:{row['id']}",
                        "type": "public_memory",
                        "text": row["text"],
                        "fictional": True,
                        "scope": row["scope"],
                        "event_id": row["event_id"],
                        "observed_at": row.get("created_at"),
                        **({"memory_preference_revision": preference["revision"]} if preference["revision"] else {}),
                    }
                )
        return [source for _, source in sorted(material, key=lambda item: -item[0])] + result

    @staticmethod
    def _limit(value, maximum=100):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("列表数量超出范围")
        return value

    @staticmethod
    def _decode(row):
        if row is None:
            raise ValueError("动态记录不存在")
        for name in ("source_ids", "sources", "receipt"):
            if name in row:
                row[name] = json.loads(row[name]) if row[name] else ([] if name != "receipt" else None)
        if row.get("presentation") is not None:
            row["presentation"] = json.loads(row["presentation"])
        if row.get("points") is not None:
            row["points"] = json.loads(row["points"])
        if "manual" in row:
            row["manual"] = bool(row["manual"])
        if row.get("error") in _RETIRED_CAPTION_DIAGNOSTICS:
            row["error"] = None
        return row

    def list_posts(self, status=None, limit=50):
        self._limit(limit)
        sql, args = "SELECT * FROM dynamic_posts", []
        if status is not None:
            sql += " WHERE status=?"
            args.append(status)
        return [
            self._post_view(r)
            for r in self.store.all(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        ]

    def list_decisions(self, limit=50):
        self._limit(limit)
        rows = self.store.all(
            "SELECT d.*,p.meme_id,COALESCE(p.status,d.status) AS current_status FROM dynamic_decisions d "
            "LEFT JOIN dynamic_posts p ON p.id=d.post_id ORDER BY d.id DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            row["status"] = row.pop("current_status")
            if row.get("post_id"):
                row.update(continuation_view(self.runtime, "dynamics", row, row["post_id"]))
            if not row.get("continuation"):
                row["continuation"] = generation_view(
                    self.runtime, row.get("generation_continuation_id"), "dynamics", row["id"]
                )
        return [self._decode(r) for r in rows]

    def latest_materials(self):
        char = self._character()
        row = self.store.one(
            "SELECT id,created_at,character_id,character_version,sources FROM dynamic_decisions "
            "WHERE evaluation_account_id=? AND character_id=? AND character_version=? ORDER BY id DESC LIMIT 1",
            (self._source_account(), char["id"], char["version"]),
        )
        if not row:
            return {"decision_id": None, "sources": []}
        sources = json.loads(row["sources"] or "[]")
        visible = sources if self._valid_sources(row | {"sources": sources}) else [
            source for source in sources if self._valid_sources(row | {"sources": [source]})
        ]
        quotes = recall_memory.originals(visible)
        return {"decision_id": row["id"], "created_at": row["created_at"],
                "sources": [recall_memory.model_material(source, comment_texts=quotes) for source in visible]}

    def _post(self, post_id):
        return self._post_view(self.store.one("SELECT * FROM dynamic_posts WHERE id=?", (post_id,)))

    def _post_view(self, row):
        post = self._decode(row)
        post.pop("send_payload", None)
        post["image_ref"] = row_ref(post)
        mode = (post.get("presentation") or {}).get("image_choice")
        snapshot = post.pop("comment_card_snapshot", None)
        candidate = json.loads(snapshot) if snapshot is not None else comment_card(post["sources"], legacy=True)
        if (mode == "shared" and not post["image_ref"] and post["status"] == "pending_review"
                and post["account_id"] == self._source_account()
                and self._valid_sources(post)):
            post["image_ref"] = self._queue_image(post, candidate)
            if post["image_ref"]:
                self.store.execute("UPDATE dynamic_posts SET image_ref=? WHERE id=? AND image_ref IS NULL",
                                   (_json(post["image_ref"]), post["id"]))
        attachment = decision(self.runtime, "dynamics", post, post["image_ref"])
        post["illustration_choice"] = image_choice(self.runtime, "dynamics", post, post["image_ref"], attachment)
        card_ref = (attachment or {}).get("comment_card_sha256")
        if card_ref:
            if candidate and content_hash(candidate) == card_ref:
                attachment = None
            else:
                attachment = {"job_id": (post["image_ref"] or {}).get("job_id", "unavailable"),
                              "status": "awaiting_choice", "error": "原评论图已变化，请重新选图。"}
                post["illustration_choice"] = "awaiting_choice"
        post["illustration"] = attachment
        if post["image_ref"] or post.get("meme_id") or snapshot:
            post["image_uploads"] = uploads(self.store, "dynamics", post["id"])
        show_card = mode == "comment_card" or mode == "shared" and bool(card_ref) and attachment is None
        post["comment_card"] = candidate if show_card or (mode is None and not attachment) else None
        if candidate and not post["comment_card"]:
            post["source_comment_card"] = candidate
        if mode == "text_only":
            post["illustration_choice"] = "text_only"
        elif mode == "shared" and not post["image_ref"]:
            post["illustration_choice"] = "awaiting_choice"
        post["review_key"] = content_hash([post.get(k) for k in (
            "id", "account_id", "character_id", "character_version", "updated_at", "content",
            "sources", "meme_id", "illustration", "illustration_choice", "comment_card", "presentation")])
        post["mentions"] = (post.get("presentation") or {}).get("mentions", [])
        post["pending_mentions"] = (post.get("presentation") or {}).get("pending_mentions", [])
        post["outgoing_links"] = [url for url in self._comment_links(post) if url.startswith("https://")]
        if post.get("duplicate_of"):
            original = self.store.one("SELECT status,receipt,error FROM dynamic_posts WHERE id=?", (post["duplicate_of"],))
            if original and original["status"] == "published":
                post.update(status="published", receipt=json.loads(original["receipt"]), error=None)
            else:
                post.update(status="publish_uncertain", error="相同内容的原提交待核对")
        return continuation_view(self.runtime, "dynamics", post)

    def _comment_card(self, sources, actor):
        browsing = getattr(self.runtime, "browsing", None)
        timezone = browsing.policy().get("business_timezone", "Asia/Taipei") if browsing else "Asia/Taipei"
        return comment_card(sources, store=self.store, actor=actor, timezone=timezone)

    def comment_card_image(self, post_id, page=0):
        card = self._post(post_id)["comment_card"]
        if not card:
            raise ValueError("这条动态没有原评论图")
        return render_comment_card(card, page)

    def _valid_sources(self, row):
        if _has_legacy_chat_lineage(row):
            return False
        char = self._character()
        sources = row["sources"]
        if row.get("manual") and len(sources) == 1 and sources[0].get("type") == "operator_topic":
            decision = self.store.one("SELECT sources FROM dynamic_decisions WHERE id=?", (row["decision_id"],))
            return (
                (row["character_id"], row["character_version"]) == (char["id"], char["version"])
                and decision is not None
                and json.loads(decision["sources"]) == sources
            )
        dependencies = [item for source in sources for item in source.get("memory_dependencies", [])]
        dependencies.extend(json.loads(row.get("context_sources") or "[]"))
        originals = [{key: value for key, value in source.items() if key != "memory_dependencies"} for source in sources]
        bound_ids = [s["id"] for s in originals + dependencies]
        current = {s["id"]: s for s in self._sources(bound_ids=bound_ids)}

        def unchanged(source):
            fresh = current.get(source["id"])
            if not fresh:
                return False
            if source["type"] == "watched_video" and source.get("material_version"):
                return fresh.get("material_version") == source["material_version"]
            return all(fresh.get(key) == value for key, value in source.items() if key != "observed_at")

        return (
            row["character_id"] == char["id"]
            and row["character_version"] == char["version"]
            and all(unchanged(source) for source in originals + dependencies)
        )

    def review(self, post_id, action, text=None, **options):
        row = self._post(post_id)
        if action not in {"approve", "reject", "edit", "copy", "reselect_image"} or row["status"] not in {
            "pending_review",
            "approved",
            "rejected",
        }:
            raise ValueError("当前动态不能执行该审核操作")
        if options.pop("review_key", row["review_key"]) != row["review_key"]:
            raise ValueError("草稿已变化，请刷新配图与正文")
        if action == "reselect_image":
            if text is not None or options or row["status"] == "rejected" or row.get("attempted_at"):
                raise ValueError("仅待发草稿可以重新选图")
            if row["account_id"] != self._source_account() or not self._valid_sources(row):
                raise ValueError("切换到草稿所属账号，或重新核对来源后再选图")
            candidate = row.get("source_comment_card") or row["comment_card"] or self._comment_card(
                row["sources"], json.loads(row.get("generation_identity") or "null"),
            )
            ref = self._queue_image(row, candidate)
            if not ref:
                raise ValueError("配图服务未连接，正文已保留")
            self.runtime.operation_images.reselect(image_scope("dynamics", row), ref["job_id"])
            presentation = (row.get("presentation") or {}) | {"image_choice": "shared"}
            self.store.execute("UPDATE dynamic_posts SET status='pending_review',meme_id=NULL,image_ref=?,"
                               "comment_card_snapshot=?,presentation=?,error=NULL,updated_at=? WHERE id=?",
                               (_json(ref), _json(candidate), _json(presentation), time.time(), post_id))
            if self.memes:
                self.memes.release(f"dynamic-draft:{post_id}")
            if browsing := getattr(self.runtime, "browsing", None):
                browsing.sends.withdraw("dynamics", post_id)
            return self._post(post_id)
        selection = options.pop("mention_selection", None)
        if set(options) - {"meme_id"} or (options or selection is not None) and action != "edit":
            raise ValueError("保存配图或提及后需重新审核")
        meme_id = options.get("meme_id", row.get("meme_id"))
        if not self._valid_sources(row):
            self._status(post_id, "invalidated", "人物版本或公开来源已变化")
            raise ValueError("人物版本或公开来源已变化，需重新评估")
        illustration = None if "meme_id" in options else row["illustration"]
        if action == "approve":
            if row["illustration_choice"] == "awaiting_choice" and not illustration:
                raise ValueError("配图待决定，请重新选图或使用纯文字")
            image_payload(illustration)
        if action == "copy":
            if illustration or meme_id or row["comment_card"]:
                raise ValueError("图文动态不能复制成纯文字，请审核后发布图文")
            if row["status"] != "approved":
                raise ValueError("请先审核动态再复制")
            return row | {"copy_text": row["content"]}
        content = validate_text(text) if text is not None else row["content"]
        presentation = row.get("presentation")
        if action == "approve" and content != row["content"] and (presentation or {}).get("image_choice") == "shared":
            raise ValueError("正文已修改，请先保存并查看新稿的配图决定")
        if selection is not None:
            presentation = (presentation if presentation is not None else {"link_source_ids": None}) | selection
        elif presentation and content != row["content"] and action != "reject":
            presentation = presentation | {"mentions": rebase_mentions(content, row["mentions"])}
        if presentation and _content_hash(content) != row["content_hash"]:
            presentation = presentation | {"points": []}
        if action == "approve" and (presentation or {}).get("pending_mentions"):
            raise ValueError("请先选择这条动态要 @ 的账号")
        owner = f"dynamic-draft:{post_id}"
        if meme_id and action != "reject":
            if not self.memes:
                raise ValueError("表情组件尚未启用")
            self.memes.pin(meme_id, row["character_id"], owner)
        status = {"approve": "approved", "reject": "rejected", "edit": "pending_review"}[action]
        bound_ref = illustration if action == "approve" else row["image_ref"]
        if "meme_id" in options:
            bound_ref = None
            if meme_id:
                asset = self.memes.resolve(meme_id, row["character_id"])
                bound_ref = {"asset_id": meme_id, "sha256": asset["sha256"]}
            presentation = (presentation or {}) | {"image_choice": "manual" if meme_id else "text_only"}
        elif (presentation or {}).get("image_choice") == "shared":
            if action == "approve":
                if (row["image_ref"] or {}).get("job_id"):
                    bound_ref = row["image_ref"]
                else:
                    mode = "comment_card" if row["comment_card"] else "manual" if illustration else "text_only"
                    presentation = presentation | {"image_choice": mode}
            elif action == "edit" and content != row["content"]:
                bound_ref = self._queue_image(row | {"content": content},
                                              row.get("source_comment_card") or row["comment_card"])
        try:
            self.store.execute(
                "UPDATE dynamic_posts SET status=?,content=?,content_hash=?,meme_id=?,image_ref=?,presentation=?,error=NULL,updated_at=? WHERE id=?",
                (status, content, _content_hash(content), meme_id, _json(bound_ref) if bound_ref else None,
                 _json(presentation) if presentation is not None else None, time.time(), post_id),
            )
        except BaseException:
            if self.memes and meme_id != row.get("meme_id"):
                self.memes.release(owner, keep_asset_id=row.get("meme_id"))
            raise
        if self.memes:
            self.memes.release(owner, keep_asset_id=meme_id)
            if status == "rejected":
                self.memes.release(owner)
        if browsing := getattr(self.runtime, "browsing", None):
            browsing.sends.withdraw("dynamics", post_id)
        return self._post(post_id)

    def _status(self, post_id, status, error=None, receipt=None):
        self.store.execute(
            "UPDATE dynamic_posts SET status=?,error=?,receipt=?,updated_at=?,published_at=? WHERE id=?",
            (
                status,
                error,
                _json(receipt) if receipt else None,
                time.time(),
                time.time() if status == "published" else None,
                post_id,
            ),
        )
        if self.memes and status in {"invalidated", "rejected"}:
            self.memes.release(f"dynamic-draft:{post_id}")

    def _schedule(self, seconds, error=None):
        now = time.time()
        self.store.set(
            "dynamic_schedule",
            {
                "next_evaluation_at": now + seconds,
                "backoff_until": now + seconds if error else None,
                "last_error": error,
            },
        )

    def _quota(self, exclude_post=None):
        now, policy = time.time(), self.policy()
        row = self.store.one(
            "SELECT MAX(attempted_at) AS last,"
            "SUM(CASE WHEN attempted_at>? THEN 1 ELSE 0 END) AS daily "
            "FROM dynamic_posts WHERE attempted_at IS NOT NULL"
            + (" AND id<>?" if exclude_post is not None else ""),
            (now - 86400, *((exclude_post,) if exclude_post is not None else ())),
        )
        blocks = []
        if row["last"] and (due := row["last"] + policy["min_post_interval_seconds"]) > now:
            blocks.append(SendLimitReached("尚未达到动态最小发布间隔", due))
        if (maximum := policy["max_posts_per_day"]) and (row["daily"] or 0) >= maximum:
            oldest = self.store.one(
                "SELECT attempted_at FROM dynamic_posts WHERE attempted_at>?"
                + (" AND id<>?" if exclude_post is not None else "")
                + " ORDER BY attempted_at DESC LIMIT 1 OFFSET ?",
                (now - 86400, *((exclude_post,) if exclude_post is not None else ()), maximum - 1),
            )
            blocks.append(SendLimitReached("已达到24小时动态发布上限", oldest["attempted_at"] + 86400))
        if blocks:
            raise max(blocks, key=lambda error: error.next_at)

    def _publication_binding(self, row, account, character):
        existing = getattr(self.runtime, "continuations", None)
        existing = existing.find("dynamics", row["id"]) if existing else None
        intent = (
            existing["binding"]["intent"]
            if existing
            else canonical_intent("bilibili", "dynamic", _delivery_key(self._outgoing(row, character)), character["id"])
        )
        decision = self.store.one(
            "SELECT sources,source_ids FROM dynamic_decisions WHERE id=?", (row["decision_id"],)
        )
        return publication_binding(
            "dynamics",
            account,
            character,
            intent,
            {"platform": "bilibili", "resource_type": "dynamic", "post_id": row["id"], "recipient_uid": ""},
            {
                "kind": "public_operations",
                "local_id": row["decision_id"],
                "platform_id": str(row["id"]),
                "content_sha256": content_hash(decision),
            },
        )

    @staticmethod
    def _comment_links(row):
        selected = (row.get("presentation") or {}).get("link_source_ids")
        if selected is not None:
            by_id = {source["id"]: source for source in row["sources"]}
            return list(dict.fromkeys(link for identity in selected if identity in by_id
                                      for link in source_links(by_id[identity])))
        if card := row.get("source_comment_card") or row["comment_card"]:
            links = card["comments"] if card.get("legacy") else card["links"]
            return list(dict.fromkeys(item["url"] for item in links))
        return []

    @classmethod
    def _outgoing_text(cls, row, text):
        if links := cls._comment_links(row):
            text += "\n\n" + "\n".join(("原评论：" if "#reply" in url else "") + url for url in links)
        return text

    @classmethod
    def _edit_caption(cls, row, text):
        text = validate_text(text)
        if links := cls._comment_links(row):
            known = "|".join(re.escape(url) for url in links)
            text = re.sub(rf"(?:\s*(?:原评论(?:链接)?\s*[：:]\s*)?(?:{known}))+\s*\Z", "", text)
        return validate_text(text)

    def _outgoing(self, row, character):
        value = {"text": self._outgoing_text(row, row["content"])}
        if row["mentions"]:
            value["mentions"] = row["mentions"]
        if card := row["comment_card"]:
            pages = card.get("page_count", 1)
            value["comment_card_sha256"] = hashlib.sha256(render_comment_card(card)).hexdigest()
            if pages > 1:
                value["comment_card_page_sha256"] = [
                    value["comment_card_sha256"],
                    *(hashlib.sha256(render_comment_card(card, page)).hexdigest() for page in range(1, pages)),
                ]
        elif row.get("illustration"):
            value.update(image_payload(row["illustration"]))
        elif row.get("illustration_choice") == "awaiting_choice":
            raise ValueError("配图待决定，请重新选图或使用纯文字")
        elif row.get("meme_id"):
            if not self.memes:
                raise ValueError("配图组件未挂载")
            asset = self.memes.resolve(row["meme_id"], character)
            value |= {"meme_id": row["meme_id"], "asset_sha256": asset["sha256"]}
        return value

    async def resolve_review_mentions(self, post_id, text, requests):
        row = self._post(post_id)
        participants = [person for source in row["sources"] for person in source.get("participants", [])]
        mentions, pending = await bind_mentions(self.account.adapter, validate_text(text), requests, participants)
        return {"mentions": mentions, "pending_mentions": pending}

    def _publication_images(self, row):
        if card := row["comment_card"]:
            return [{"mime_type": "image/png", "content": render_comment_card(card, page)}
                    for page in range(card.get("page_count", 1))]
        if row.get("illustration"):
            return [checked_image(self.memes, row["illustration"], row["character_id"],
                                  runtime=self.runtime, scope=image_scope("dynamics", row))]
        if row.get("meme_id"):
            if not self.memes:
                raise ValueError("配图服务未连接，草稿已保存")
            return [self.memes.read(row["meme_id"], row["character_id"])]
        return []

    async def _refresh_post_identity(self, post_id, automatic, epoch):
        refresh = getattr(self.runtime, "refresh_identity", None)
        if refresh is None:
            return
        row = self._post(post_id)
        if row["status"] != ("pending_review" if automatic else "approved") or row.get("attempted_at"):
            return
        policy = self.policy()
        if automatic and (
            not (policy["enabled"] and policy["auto_publish"])
            or row["manual"]
            or self._automatic_post_id != post_id
        ):
            raise ValueError("未授权自动发布此动态")
        if not policy["credentials_configured"]:
            raise ValueError("发布动态需要先配置账号登录凭证")
        character = self.store.character(row["character_id"], row["character_version"])
        account = row.get("account_id") or getattr(self.account, "display_account_id", lambda: "")() or None
        self.operations.assert_allowed(
            automatic=automatic,
            channel="dynamics",
            account_id=account,
            character=character,
            epoch=epoch,
        )
        if not self._valid_sources(row):
            raise ValueError("人物版本或公开来源已变化，需重新评估")
        identity = await refresh(character, account)
        previous = json.loads(row.get("generation_identity") or "null")
        if previous and previous.get("identity_version") == identity["identity_version"]:
            return
        original = {key: row.get(key) for key in ("status", "content", "sources", "meme_id", "account_id", "presentation")}

        def guard():
            fresh = self._post(post_id)
            if any(fresh.get(key) != value for key, value in original.items()) or not self._valid_sources(
                fresh
            ):
                raise ValueError("身份更新期间动态正文或来源已变化")
            if (
                runtime_identity(self.runtime, character, account)["identity_version"]
                != identity["identity_version"]
            ):
                raise ValueError("生成期间账号身份再次变化")
            self.operations.assert_allowed(
                automatic=automatic,
                channel="dynamics",
                account_id=account,
                character=character,
                epoch=epoch,
            )

        messages = identity_messages(
            dynamic_decision_messages(
                character,
                self._recent_public_posts(character),
                row["sources"],
                current_time=datetime.now().astimezone().isoformat(),
            ),
            identity,
        )
        maximum = self.settings.data["omni"].get("operations_max_tokens", 8192)

        async def invoke(packet):
            return await budgeted_json(
                self.runtime.operation_llm, packet, self.decision_schema(row["sources"]), max_tokens=maximum
            )

        async with self.runtime.offline_generation("background"):
            guard()
            data, audit = await generate_with_recovery(
                self.runtime,
                channel="generation.dynamics",
                identity=f"identity-refresh:{post_id}:{identity['identity_version']}",
                binding={
                    "task": "generation",
                    "post_id": post_id,
                    "character": {key: character[key] for key in ("id", "version")},
                    "source_sha256": content_hash(row["sources"]),
                    "self_identity": identity,
                },
                character=character,
                messages=messages,
                generate=invoke,
                validate=lambda value: self._parse(value, row["sources"]),
                max_tokens=maximum,
                guard=guard,
                diagnose=lambda value: "正文包含大量重复内容，保留原文待审"
                if value["action"] == "post" and operation_output_diagnostics(value["content"])["needs_review"]
                else None,
                context_data={"public_sources": row["sources"], "reason": "当前账号身份资料已更新"},
            )
            guard()
            if data["action"] != "post":
                self._status(post_id, "rejected", data["reason"])
                raise ValueError("身份更新后模型决定暂不发布：" + data["reason"])
            selected = [source for source in row["sources"] if source["id"] in data["source_ids"]]
            if row.get("presentation") is not None:
                try:
                    mentions = rebase_mentions(data["content"], row["mentions"])
                except ValueError:
                    self._status(post_id, "pending_review", "身份更新后的正文需要重新确认提及")
                    raise
                presentation = row["presentation"] | {"mentions": mentions}
                if data["points"]:
                    presentation["points"] = data["points"]
                if presentation.get("link_source_ids") is not None:
                    presentation["link_source_ids"] = [item for item in presentation["link_source_ids"]
                                                       if item in data["source_ids"]]
                # An approved target can only change through the draft editor.
                if data["mentions"] and data["mentions"] != [
                    {"label": "@" + item["name"], "account": item["uid"]} for item in mentions
                ]:
                    self._status(post_id, "pending_review", "身份更新后的正文需要重新确认提及")
                    raise ValueError("请重新确认动态的提及对象")
            else:
                presentation = None
            self.store.execute(
                "UPDATE dynamic_posts SET content=?,content_hash=?,reason=?,sources=?,source_ids=?,"
                "generation_identity=?,comment_card_snapshot=?,presentation=?,updated_at=?,error=NULL WHERE id=?",
                (
                    data["content"],
                    _content_hash(data["content"]),
                    data["reason"],
                    _json(selected),
                    _json(data["source_ids"]),
                    _json(identity),
                    _json(self._comment_card(selected, identity) if row["comment_card"] or row.get("source_comment_card") else None),
                    _json(presentation) if presentation is not None else None,
                    time.time(),
                    post_id,
                ),
            )
            self.store.execute(
                "UPDATE dynamic_decisions SET content=?,reason=?,sources=?,source_ids=? WHERE id=?",
                (
                    data["content"],
                    data["reason"],
                    _json(selected),
                    _json(data["source_ids"]),
                    row["decision_id"],
                ),
            )
            if audit:
                self.store.execute("UPDATE dynamic_decisions SET generation_continuation_id=? WHERE id=?",
                                   (audit["id"], row["decision_id"]))
                if audit["state"] == "awaiting_review":
                    self._status(post_id, "pending_review", "正文诊断尚待审核")

    async def _publication_context(self, post_id, automatic, epoch, *, submission=None):
        if submission is None:
            await self._refresh_post_identity(post_id, automatic, epoch)
        row = self._post(post_id)
        character = self.store.character(row["character_id"], row["character_version"])
        cached = getattr(self.account, "display_account_id", lambda: "")()
        account = cached or (
            await self.account.account_id()
            if hasattr(self.account, "account_id")
            else "injected-test-account"
        )
        binding = self._publication_binding(row, account, character)
        adapter = self.account.adapter
        credential = dict(getattr(self.account, "credentials", {}))

        def guard():
            fresh = self._post(post_id)
            if (
                not self._valid_sources(fresh)
                or any(
                    fresh.get(key) != row.get(key)
                    for key in ("status", "content", "meme_id", "character_id", "character_version", "presentation")
                )
                or self._publication_binding(fresh, account, character) != binding
            ):
                raise ValueError("恢复期间动态正文、角色或原始公开来源已变化")
            if (
                self.account.adapter is not adapter
                or getattr(self.account, "credentials", {}) != credential
                or automatic
                and not (self.policy()["enabled"] and self.policy()["auto_publish"])
            ):
                raise ValueError("恢复期间动态账号或授权已变化")
            self.operations.assert_allowed(
                automatic=automatic, channel="dynamics", account_id=account, character=character, epoch=epoch
            )

        def defer(prepared, state):
            prepared.guard()
            self._status(post_id, "rejected" if state == "abandoned" else "pending_review")

        def revised(value):
            text = validate_text(value)
            return self._outgoing(row | {"content": text, "mentions": rebase_mentions(text, row["mentions"])}, character)

        return PublicationContext(
            binding,
            row["content"],
            character,
            {"public_sources": row["sources"], "reason": row["reason"]},
            guard,
            payload=self._outgoing(row, character),
            encode_revision=revised,
            on_defer=defer,
            manual_approved=row["status"] == "approved",
        )

    def _apply_revision(self, post_id, prepared, text):
        prepared.guard()
        text = validate_text(text)
        row = self._post(post_id)
        presentation = row.get("presentation")
        if presentation is not None:
            presentation = presentation | {"mentions": rebase_mentions(text, row["mentions"]), "points": []}
        self.store.execute(
            "UPDATE dynamic_posts SET content=?,content_hash=?,presentation=?,status='pending_review',error=NULL,updated_at=? WHERE id=?",
            (text, _content_hash(text), _json(presentation) if presentation is not None else None, time.time(), post_id),
        )

    async def resume_continuation(self, post_id, continuation_id):
        if str(post_id).startswith("generation:"):
            identity = str(post_id).split(":", 1)[1]
            decision = self.store.one("SELECT * FROM dynamic_decisions WHERE id=?", (identity,))
            if not decision or decision.get("generation_continuation_id") != continuation_id:
                raise ValueError("生成恢复任务与运营决定不匹配")
            row = self.runtime.continuations.get(continuation_id)
            if row["binding"]["character"] != {key: self._character()[key] for key in ("id", "version")}:
                raise ValueError("生成恢复任务的角色已变化")
            self.runtime.continuations.resume(row["id"], binding=row["binding"])
            result = await self.evaluate(manual=True)
            return {
                "status": result["status"],
                "decision": result,
                "continuation": result.get("continuation"),
            }
        return await FeedbackPublication(self.runtime, self.store, self.operations).resume(
            "dynamics",
            post_id,
            continuation_id,
            prepare=lambda: self._publication_context(post_id, False, None),
            apply_revision=lambda prepared, text: self._apply_revision(post_id, prepared, text),
            reconcile=lambda token, prepared: self._reconcile(post_id),
        )

    async def _reconcile(self, post_id, *, guard=None):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        async def read(row, attempt):
            adapter = self.account.adapter
            if not guard and not hasattr(adapter, "reconcile_dynamic_publication"):
                return {"status": "unsupported"}
            try:
                account = await self.account.account_id()
            except ValueError:
                return {"status": "check_failed", "reason": "account_unavailable"}
            if account != row["binding"]["account_id"]:
                return {"status": "check_failed", "reason": "account_changed"}
            if guard:
                guard(row, attempt)
            if not hasattr(adapter, "reconcile_dynamic_publication"):
                return {"status": "unsupported"}
            payload = row["feedback"]["sent_payload"]
            target = row["binding"]["target"] | {
                "message_kind": "image" if payload.get("asset_sha256") or payload.get("comment_card_sha256") else "text"
            }
            if payload.get("prepared_images"):
                target["images"] = payload["prepared_images"]
            if "mentions" in payload:
                target["mentions"] = payload["mentions"]
            result = await adapter.reconcile_dynamic_publication(
                target,
                author_uid=account,
                text=payload["text"],
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )
            if self.account.adapter is not adapter:
                return {"status": "check_failed", "reason": "account_changed"}
            if guard:
                guard(row, attempt)
            return result

        def commit(receipt, verification):
            self._status(post_id, "published", receipt=receipt)
            if self.memes:
                self.memes.release(f"dynamic-draft:{post_id}")

        return await feedback.reconcile("dynamics", post_id, read, commit)

    async def publish(self, post_id, automatic=False, *, expected_text=None, submission=None):
        saved = self._post(post_id)
        if expected_text is not None and expected_text != saved["content"]:
            raise ValueError("草稿已变化，请重新检查当前正文")
        if saved["status"] == "published" and saved["receipt"] and submission is None:
            return saved
        if saved.get("duplicate_of"):
            if submission is not None:
                submission.check(saved)
            cached_account = getattr(self.account, "display_account_id", lambda: "")()
            if cached_account and cached_account != saved["account_id"]:
                raise ValueError("该动态草稿属于其他账号")
            if saved["status"] != "published":
                await self._reconcile(saved["duplicate_of"])
            return self._post(post_id)
        if submission is not None:
            if automatic or expected_text is None:
                raise ValueError("限定草稿提交必须绑定人工确认的正文")
            submission.check(self._post(post_id))
        if not hasattr(self.runtime, "settings"):
            self.runtime.settings = self.settings
        control = self.operations.policy()
        epoch = control["epoch"] if automatic and control["enabled"] else None
        if automatic:
            await wait_decision(self.runtime, "dynamics", self._post(post_id), lambda:
                                self.policy()["auto_publish"] and self.operations.policy()["epoch"] == epoch)
        confirmed_hash = None
        if expected_text is not None:
            confirmed = self._post(post_id)
            char = self.store.character(confirmed["character_id"], confirmed["character_version"])
            confirmed_hash = content_hash(self._outgoing(confirmed, char))
        return await FeedbackPublication(self.runtime, self.store, self.operations).submit(
            "dynamics",
            post_id,
            prepare=lambda: self._publication_context(post_id, automatic, epoch, submission=submission),
            send=lambda ticket, binding, body_hash: self._publish_once(
                post_id,
                automatic,
                revision_ticket=ticket,
                binding=binding,
                body_hash=confirmed_hash or body_hash,
                authorization_epoch=epoch,
                expected_text=expected_text,
                submission=submission,
            ),
            apply_revision=lambda prepared, text: self._apply_revision(post_id, prepared, text),
            automatic=automatic,
            epoch=epoch,
            reconcile=(lambda token, prepared: self._reconcile(post_id)) if submission is None else None,
            revise_on_rejection=submission is None,
        )

    async def _publish_once(
        self,
        post_id,
        automatic=False,
        *,
        revision_ticket=None,
        binding=None,
        body_hash=None,
        authorization_epoch=None,
        expected_text=None,
        submission=None,
    ):
        # Complete any identity refresh before taking the platform write lock.
        if submission is None:
            await self._refresh_post_identity(post_id, automatic, authorization_epoch)
        checked = self._post(post_id) if automatic else None
        async with self.account.credential_lock:
            row, policy = self._post(post_id), self.policy()
            if expected_text is not None and row["content"] != expected_text:
                raise ValueError("草稿已变化，请重新检查当前正文")
            if automatic:
                self._check_automatic_draft(row, policy)
                if row != checked:
                    raise ValueError("草稿已变化，请重新核对当前正文")
            elif submission is not None:
                submission.check(row)
            elif row["status"] != "approved":
                raise ValueError("请先审核通过动态")
            if not policy["credentials_configured"]:
                raise ValueError("发布动态需要先配置账号登录凭证")
            if not self._valid_sources(row):
                self._status(post_id, "invalidated", "人物版本或公开来源已变化")
                raise ValueError("人物版本或公开来源已变化，需重新评估")
            if row["pending_mentions"]:
                raise ValueError("请先选择这条动态要 @ 的账号")
            card = row["comment_card"]
            pages = self._publication_images(row)
            image = (pages if len(pages) > 1 else pages[0]) if pages else None
            adapter = self.account.adapter
            account_id = (
                await self.account.account_id()
                if hasattr(self.account, "account_id")
                else "injected-test-account"
            )
            fresh = self._post(post_id)
            if fresh["status"] != row["status"] or fresh["review_key"] != row["review_key"]:
                raise ValueError("账号核验期间草稿已变化，请重新确认后发布")
            if row.get("account_id") and row["account_id"] != str(account_id):
                raise ValueError("该动态草稿属于其他账号")
            if not row.get("account_id"):
                self.store.execute(
                    "UPDATE dynamic_posts SET account_id=? WHERE id=?", (str(account_id), post_id)
                )
                row["account_id"] = str(account_id)
            character = self.store.character(row["character_id"], row["character_version"])
            actual_binding = self._publication_binding(row, account_id, character)
            payload = self._outgoing(row, character)
            if (binding is not None and binding != actual_binding
                    or body_hash is not None and body_hash != content_hash(payload)):
                raise ValueError("发送前动态原始绑定或正文已变化")
            delivery = _delivery_key(payload)
            duplicate = self._duplicate_delivery(post_id, str(account_id), delivery, row["content_hash"])
            if duplicate:
                if submission is not None:
                    submission.check(row)
                self.store.execute("UPDATE dynamic_posts SET status='duplicate',duplicate_of=?,updated_at=? WHERE id=?",
                                   (duplicate, time.time(), post_id))
                if self.memes and row.get("meme_id"):
                    self.memes.release(f"dynamic-draft:{post_id}")
                return self._post(post_id)
            self._quota(post_id if revision_ticket else None)
            prepared_images = []

            def record_uploaded(index, picture):
                ref = row.get("illustration")
                if row.get("meme_id"):
                    ref = {"asset_id": row["meme_id"], "sha256": payload["asset_sha256"]}
                elif card:
                    hashes = payload.get("comment_card_page_sha256") or [payload["comment_card_sha256"]]
                    ref = {"asset_id": f"comment-card:{post_id}:{index + 1}", "sha256": hashes[index]}
                record_upload(self.store, "dynamics", post_id, ref, picture)

            def record_prepared_images(images):
                prepared_images.extend(
                    {"path": bfs_image_path(item["url"]), "width": item["width"], "height": item["height"]}
                    for item in images
                )

            def attempted_payload():
                return payload | {"prepared_images": prepared_images} if prepared_images else payload

            def save_attempt(attempt_token):
                self.store.db.execute(
                    "UPDATE dynamic_posts SET attempted_at=?,updated_at=?,send_payload=?,delivery_key=? WHERE id=?",
                    (time.time(), time.time(), _json({
                        "token": attempt_token, "binding": actual_binding, "payload": attempted_payload(),
                        **({"agent_step": submission.step_id} if submission is not None else {}),
                    }), delivery, post_id),
                )
            token = None

            def before_send(*, reserve=True):
                nonlocal token
                current_policy = self.policy()
                if (
                    self.account.adapter is not adapter
                    or not current_policy["credentials_configured"]
                    or (automatic and not (current_policy["enabled"] and current_policy["auto_publish"]))
                ):
                    raise PublishCancelled("运营已停止或账号已变化，取消发送")
                fresh = self._post(post_id)
                if submission is not None:
                    try:
                        submission.check(fresh, publishing=True)
                    except ValueError:
                        raise PublishCancelled("限定草稿的审批或来源已变化") from None
                if (
                    fresh["status"] != "publishing"
                    or not self._valid_sources(fresh)
                    or fresh["content"] != row["content"]
                    or fresh["sources"] != row["sources"]
                    or fresh.get("presentation") != row.get("presentation")
                    or fresh["comment_card"] != row["comment_card"]
                    or fresh.get("meme_id") != row.get("meme_id") or fresh.get("illustration") != row.get("illustration")
                ):
                    raise PublishCancelled("人物版本或公开来源已变化，取消发送")
                if image and row.get("meme_id") and not card:
                    try:
                        current_asset = self.memes.resolve(row["meme_id"], character)
                        if current_asset["sha256"] != image["asset"]["sha256"]:
                            raise ValueError("配图原文已变化")
                    except ValueError:
                        raise PublishCancelled("当前配图已禁用、解绑或变化，取消发送") from None
                try:
                    self._quota(post_id if revision_ticket else None)
                    if self._duplicate_delivery(post_id, str(account_id), delivery, row["content_hash"]):
                        raise ValueError("相同动态已有提交记录")
                except ValueError:
                    raise PublishCancelled("发布频率、正文或来源已不满足发送条件") from None
                try:
                    if not reserve:
                        self.operations.assert_allowed(
                            automatic=automatic,
                            channel="dynamics",
                            account_id=account_id,
                            character=character,
                            epoch=authorization_epoch,
                        )
                        return True
                    if automatic and (browsing := getattr(self.runtime, "browsing", None)):
                        browsing.sends.assert_ready("dynamics", post_id)
                    token = self.operations.reserve(
                        account_id,
                        character,
                        actual_binding["intent"],
                        "dynamics",
                        automatic=automatic,
                        limits={"max_per_day": current_policy["max_posts_per_day"]},
                        epoch=authorization_epoch,
                        binding=actual_binding,
                        content_hash=content_hash(payload),
                        revision_ticket=revision_ticket,
                        admission_hook=save_attempt,
                    )
                except ValueError:
                    raise PublishCancelled("账号全渠道额度、角色或紧急停止阻止了本次提交") from None

            # Account reads may fail or be cancelled before publication begins.
            # Keep the reviewed draft intact until all identity setup succeeds.
            fresh = self._post(post_id)
            if submission is not None:
                submission.check(fresh)
                if actual_binding != submission.binding or payload != submission.payload:
                    raise ValueError("限定草稿的目标或完整提交内容已变化")
            if (
                fresh["status"] != row["status"]
                or fresh["content"] != row["content"]
                or fresh["sources"] != row["sources"]
                or fresh.get("presentation") != row.get("presentation")
                or fresh["comment_card"] != row["comment_card"]
                or fresh.get("meme_id") != row.get("meme_id")
            ):
                raise ValueError("账号核验期间草稿已变化，保留当前审核状态；请重新确认后发布")
            self.store.execute(
                "UPDATE dynamic_posts SET status='publishing',updated_at=? WHERE id=?", (time.time(), post_id)
            )
            receipt = None
            try:
                if image:
                    receipt = await publish_image(
                        adapter,
                        payload["text"],
                        image,
                        before_send=before_send,
                        prepare_authorized=lambda: before_send(reserve=False),
                        on_prepared=record_prepared_images, on_uploaded=record_uploaded,
                        **({"mentions": payload["mentions"]} if "mentions" in payload else {}),
                    )
                else:
                    receipt = await publish_text(adapter, payload["text"], before_send=before_send,
                                                 **({"mentions": payload["mentions"]} if "mentions" in payload else {}))
                if "mentions" in payload:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    self._status(post_id, "publish_uncertain", "动态已受理，正在核对提及", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态已受理，提及待核对"
                    )
                    checked = await self._reconcile(post_id)
                    if checked is not None:
                        return self._post(post_id)
                    raise UnknownPublication("动态已受理，提及待核对", token)
            except UnknownPublication:
                raise
            except PublishPreparationCancelled:
                self._status(post_id, row["status"], "配图准备已取消，尚未提交动态")
                raise
            except PublishPreparationFailed:
                self._status(post_id, row["status"], "配图准备失败，草稿已保存")
                raise ValueError("配图准备失败，尚未提交动态；请审核后重试") from None
            except PublishCancelled:
                if token:
                    self.operations.finish(token, "cancelled")
                status = row["status"] if self._valid_sources(self._post(post_id)) else "invalidated"
                self._status(post_id, status, "发送前授权或来源已变化，取消发送；未写入平台")
                raise ValueError("发送前授权或来源已变化，取消发送；未写入平台") from None
            except asyncio.CancelledError:
                if token is None:
                    self._status(post_id, row["status"], "准备已取消，未提交动态")
                    raise
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态发送结果不确定"
                    )
                self._status(post_id, "publish_uncertain", "写入被取消，请人工核对平台")
                raise
            except PublishUncertain:
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态发送结果不确定"
                    )
                self._status(post_id, "publish_uncertain", "动态发布结果待核对")
                raise UnknownPublication("动态发布结果待核对", token) from None
            except ValueError as error:
                if token:
                    self.operations.finish(token, "rejected")
                self._status(post_id, "publish_failed", "平台拒绝动态写入，请检查账号和内容")
                self._schedule(3600, "平台拒绝动态写入")
                if isinstance(error, PlatformRejected):
                    error.write_token = token
                    error.body_hash = content_hash(payload)
                    error.sent_payload = payload
                    raise
                raise ValueError("平台拒绝动态写入，请检查账号和内容") from None
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态发送结果不确定"
                    )
                self._status(post_id, "publish_uncertain", "写入或回执保存失败，请人工核对平台")
                raise UnknownPublication("动态发布结果待核对", token) from None
            try:
                self._status(post_id, "published", receipt=receipt)
                if token:
                    self.operations.finish(token, "published", receipt=receipt)
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "平台成功后本地保存失败"
                    )
                # A local persistence failure after receipt is never a definite
                # remote rejection, even when the local exception is ValueError.
                self._status(post_id, "publish_uncertain", "平台写入后保存回执失败，请人工核对平台")
                raise UnknownPublication("B 站已受理动态，本地回执待补录", token) from None
            if self.memes and row.get("meme_id"):
                self.memes.release(f"dynamic-draft:{post_id}")
            return self._post(post_id)

    def _check_automatic_draft(self, row, policy):
        if not (policy["enabled"] and policy["auto_publish"]) or row["manual"] or self._automatic_post_id != row["id"]:
            raise ValueError("未授权自动发布此动态")
        if row["status"] != "pending_review":
            raise ValueError("动态不能重复发布")
        if operation_output_diagnostics(row["content"])["needs_review"]:
            raise ValueError("正文诊断尚待审核，未自动发布")

    def _duplicate_delivery(self, post_id, account, delivery, text_hash):
        for other in self.store.all(
            "SELECT id,delivery_key,send_payload,content FROM dynamic_posts WHERE account_id=? AND id<>? "
            "AND status IN ('publishing','publish_uncertain','published') AND attempted_at IS NOT NULL "
            "AND (delivery_key=? OR (delivery_key IS NULL AND (send_payload IS NOT NULL OR content_hash=?))) "
            "ORDER BY attempted_at,id", (account, post_id, delivery, text_hash),
        ):
            payload = json.loads(other["send_payload"] or "{}").get("payload") or {"text": other["content"]}
            if (other["delivery_key"] or _delivery_key(payload)) == delivery:
                return other["id"]
        return None

    def _live_busy(self):
        live = getattr(self.runtime, "live", None)
        return live is not None and live.state in {"live", "paused"} and live.busy

    def _recent_public_posts(self, character):
        _, preference = self._memory_preference()
        account = getattr(self.account, "display_account_id", lambda: "")()
        return [
            {"content": post["content"], "published_at": post["published_at"]}
            for post in self.list_posts("published", 5)
            if (post["character_id"], post["character_version"]) == (character["id"], character["version"])
            and (not account or post["account_id"] == account)
            and not _has_legacy_chat_lineage(post)
            and not json.loads(post.get("context_sources") or "[]")
            and not any(
                source.get("memory_version") or source.get("memory_dependencies")
                or (source["id"].startswith(("memory:", "experience:", "incident:")) and (
                    not preference["enabled"]
                    or source.get("memory_preference_revision", 0) != preference["revision"]
                ))
                for source in post["sources"]
            )
        ]

    def _related_posts(self, character, account, sources):
        if not sources or not account:
            return []
        ids = _json([source["id"] for source in sources])
        resources = _json(sorted({link.partition("#")[0] for source in sources for link in source_links(source)}))
        history = []
        for shared in (False, True):
            candidates = self.store.all(
                "SELECT * FROM dynamic_posts p WHERE account_id=? AND character_id=? "
                "AND status IN ('pending_review','approved','publishing','publish_uncertain','published') "
                "AND COALESCE(status='published',0)=? "
                "AND (status!='published' OR json_extract(receipt,'$.dynamic_id') IS NOT NULL) "
                "AND EXISTS (SELECT 1 FROM json_each(p.sources) s WHERE "
                "json_extract(s.value,'$.id') IN (SELECT value FROM json_each(?)) OR "
                "substr(json_extract(s.value,'$.source_url') || '#', 1, "
                "instr(json_extract(s.value,'$.source_url') || '#','#')-1) IN (SELECT value FROM json_each(?)) OR "
                "EXISTS (SELECT 1 FROM json_each(json_extract(s.value,'$.public_sources')) r "
                "WHERE 'https://www.bilibili.com/video/'||json_extract(r.value,'$.bvid') "
                "IN (SELECT value FROM json_each(?)))) ORDER BY updated_at DESC LIMIT 12",
                (account, character["id"], int(shared), ids, resources, resources),
            )
            found = 0
            for row in candidates:
                row = self._decode(row)
                if not self._valid_sources(row | {"character_version": character["version"]}):
                    continue
                history.append({
                    "status": "shared" if shared else "pending", "content": row["content"][:600],
                    "source_ids": row["source_ids"], "points": (row.get("presentation") or {}).get("points", [])[:8],
                })
                found += 1
                if found == 4:
                    break
        return history

    @staticmethod
    def decision_schema(sources):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "reason", "content", "source_ids", "points", "next_evaluation_seconds"],
            "properties": {
                "action": {"type": "string", "enum": ["post", "wait"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                "content": {"type": "string"},
                "source_ids": {
                    "type": "array",
                    "maxItems": len(sources),
                    "items": {"type": "string", **({"enum": [source["id"] for source in sources]} if sources else {})},
                },
                "link_source_ids": {"type": "array", "items": {"type": "string"}},
                "mentions": MENTION_REQUESTS,
                "points": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                           "required": ["text", "source_ids"], "properties": {
                               "text": {"type": "string", "minLength": 1, "maxLength": 300},
                               "source_ids": {"type": "array", "items": {"type": "string"}},
                           }}},
                "next_evaluation_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            },
        }

    @staticmethod
    def _parse(data, sources):
        if not isinstance(data, dict) or len(_json(data).encode("utf-8")) > MAX_OPERATION_TEXT_BYTES + 16384:
            raise ValueError("模型决定格式无效")
        required = {
            "action",
            "reason",
            "content",
            "source_ids",
            "next_evaluation_seconds",
        }
        if not required <= data.keys() or data.keys() - required - {"link_source_ids", "illustration", "mentions", "points"}:
            raise ValueError("模型决定字段无效")
        if data["action"] not in {"post", "wait"}:
            raise ValueError("模型决定动作无效")
        if not isinstance(data["reason"], str) or not 1 <= len(data["reason"]) <= 500:
            raise ValueError("模型决定理由无效")
        ids = data["source_ids"]
        allowed = {s["id"] for s in sources}
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("模型来源列表无效")
        if not set(ids) <= allowed:
            raise ValueError("模型引用了未授权来源")
        links = data.get("link_source_ids", [])
        linkable = {source["id"] for source in sources if source["id"] in ids and source_links(source)}
        if not isinstance(links, list) or any(not isinstance(item, str) or item not in linkable for item in links):
            raise ValueError("附加链接须选自用到的公开素材")
        illustration = data.get("illustration")
        if illustration not in (None, "none", "comment_card", "picture"):
            raise ValueError("模型配图选择无效")
        mentions = mention_requests(data.get("mentions", []))
        points = data.get("points", [])
        if not isinstance(points, list) or any(
            not isinstance(point, dict) or set(point) != {"text", "source_ids"}
            or not isinstance(point["text"], str) or not 1 <= len(point["text"].strip()) <= 300
            or not isinstance(point["source_ids"], list)
            or any(not isinstance(item, str) or item not in ids for item in point["source_ids"])
            for point in points
        ):
            raise ValueError("分享要点须关联本次用到的素材")
        seconds = data["next_evaluation_seconds"]
        if type(seconds) is not int or not 1 <= seconds <= MAX_SAFE_INTEGER:
            raise ValueError("模型下次评估间隔无效")
        if data["action"] == "post":
            data["content"] = validate_text(data["content"])
        elif data["content"] != "" or links or mentions or points or illustration not in (None, "none"):
            raise ValueError("等待决定不得包含待发布正文")
        return data | {"link_source_ids": links, "illustration": illustration, "mentions": mentions, "points": points}

    async def _presentation(self, data, sources):
        participants = [person for source in sources for person in source.get("participants", [])]
        mentions, pending = await bind_mentions(self.account.adapter, data["content"], data["mentions"], participants)
        return {"link_source_ids": data["link_source_ids"], "mentions": mentions, "pending_mentions": pending,
                "points": data["points"]}

    def _queue_image(self, post, candidate):
        public = [{key: source[key] for key in ("id", "type", "text", "source_url") if key in source}
                  for source in post["sources"]]
        return queue_image(self.runtime, "dynamics", post, public, post["content"],
                           comment_card=content_hash(candidate) if candidate else None, require_choice=True)

    def _save_decision(
        self, char, manual, data, sources, error=None, *, generation_identity=None, schedule=True, account_id=None,
        decide_image=True
    ):
        now = time.time()
        selected = [s for s in sources if s["id"] in data["source_ids"]]
        selected_ids = {s["id"] for s in selected}
        memory_dependencies = [
            s for s in sources if s["id"] not in selected_ids
            and (s.get("memory_version") or s["id"].startswith(("memory:", "experience:", "incident:")))
        ]
        if selected and memory_dependencies:
            selected[0] = selected[0] | {"memory_dependencies": memory_dependencies}
            memory_dependencies = []
        model = str(self.settings.data["omni"]["responses_model"])
        next_at = now + data["next_evaluation_seconds"]
        presentation = data.get("presentation")
        owner = (account_id if account_id is not None else
                 getattr(self.account, "display_account_id", lambda: "")() or "")
        shared_choice = bool(owner and decide_image and workstation_image_decisions(self.runtime))
        if shared_choice:
            presentation = (presentation or {}) | {"image_choice": "shared"}
        card_selected = shared_choice or presentation is None or data.get("illustration") == "comment_card"
        with self.store.db:
            decision_id = self.store.db.execute(
                "INSERT INTO dynamic_decisions(character_id,character_version,model,manual,action,reason,content,"
                "source_ids,sources,next_evaluation_at,created_at,status,error,points) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    char["id"],
                    char["version"],
                    model,
                    int(manual),
                    data["action"],
                    data["reason"],
                    data["content"],
                    _json(data["source_ids"]),
                    _json(selected),
                    next_at,
                    now,
                    data["action"],
                    error,
                    _json(presentation["points"]) if presentation is not None and "points" in presentation else None,
                ),
            ).lastrowid
            if "evaluation_account_id" in data:
                self.store.db.execute("UPDATE dynamic_decisions SET evaluation_account_id=? WHERE id=?",
                                      (data["evaluation_account_id"], decision_id))
            if data["action"] == "post":
                post_id = self.store.db.execute(
                    "INSERT INTO dynamic_posts(decision_id,character_id,character_version,model,content,reason,"
                    "source_ids,sources,status,created_at,updated_at,content_hash,manual,account_id,comment_card_snapshot,context_sources) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        char["id"],
                        char["version"],
                        model,
                        data["content"],
                        data["reason"],
                        _json(data["source_ids"]),
                        _json(selected),
                        "pending_review",
                        now,
                        now,
                        _content_hash(data["content"]),
                        int(manual),
                        owner,
                        _json(self._comment_card(selected, generation_identity) if card_selected else None),
                        _json(memory_dependencies),
                    ),
                ).lastrowid
                self.store.db.execute("UPDATE dynamic_decisions SET post_id=? WHERE id=?", (post_id, decision_id))
                self.store.db.execute(
                    "UPDATE dynamic_posts SET generation_identity=?,presentation=? WHERE id=?",
                    (_json(generation_identity or runtime_identity(self.runtime, char)),
                     _json(presentation) if presentation is not None else None, post_id),
                )
        if data["action"] == "post" and shared_choice:
            self._post(post_id)
        if schedule:
            self._schedule(data["next_evaluation_seconds"], error)
        return next(d for d in self.list_decisions(100) if d["id"] == decision_id)

    async def create_operator_draft(self, character, request_id, topic, *, account=""):
        source = {"id": f"operator-topic:{request_id}", "type": "operator_topic",
                  "text": topic or "选一个你自己想分享的话题"}
        sources = [source]
        async with self.evaluation_lock:
            existing = self.store.one(
                "SELECT id, character_id, character_version, sources, account_id FROM dynamic_posts "
                "WHERE manual=1 AND source_ids=?",
                (_json([source["id"]]),),
            )
            if existing:
                if (
                    (existing["character_id"], existing["character_version"])
                    != (character["id"], character["version"])
                    or json.loads(existing["sources"]) != sources
                    or existing["account_id"] != account
                ):
                    raise ValueError("请求已用于其他动态草稿")
                return self._post(existing["id"])
            scope = self.runtime.browsing.discovery.history_scope
            if str(scope()[0] or "") != account:
                raise ValueError("账号已切换，请重新交接动态素材")
            identity = runtime_identity(self.runtime, character, account or None)
            messages = dynamic_decision_messages(
                character, [], sources, current_time=datetime.now().astimezone().isoformat()
            )
            messages.append({
                "role": "system",
                "content": ("根据 operator_topic 写一条动态草稿，选 post 并引用这项来源。" if topic else
                            "从你自己感兴趣的话题写一条动态草稿，选 post。"),
            })
            async with self.runtime.offline_generation("background"):
                result = await budgeted_json(
                    self.runtime.operation_llm, identity_messages(messages, identity), self.decision_schema(sources),
                    max_tokens=512,
                )
            current = self._character()
            if (current["id"], current["version"]) != (character["id"], character["version"]) or (
                runtime_identity(self.runtime, current, account or None) != identity
            ) or str(scope()[0] or "") != account:
                raise ValueError("生成期间账号或角色已变化")
            data = self._parse(result, sources)
            if data["action"] != "post" or data["source_ids"] != [source["id"]]:
                raise ValueError("本次没有可保存的动态正文")
            data["presentation"] = await self._presentation(data, sources)
            if str(scope()[0] or "") != account or self._character() != current:
                raise ValueError("核对提及期间账号或角色已变化")
            saved = self._save_decision(character, True, data, sources, generation_identity=identity,
                                        schedule=False, account_id=account)
            return self._post(saved["post_id"])

    def dynamic_edit(self, request_id, account, character_id):
        row = self.store.one(
            "SELECT * FROM dynamic_edits WHERE request_id=? AND account_id=? AND character_id=?",
            (request_id, account, character_id),
        )
        if row is None:
            raise ValueError("未找到当前账号和角色的动态修订")
        row["mentions"] = json.loads(row["mentions"]) if row["mentions"] is not None else None
        if row["status"] == "draft":
            post = self._post(row["post_id"])
            caption = self._edit_caption(post, row["caption"])
            row = row | {"caption": caption, "outgoing": self._outgoing_text(post, caption)}
        return row

    async def propose_dynamic_edit(self, character, request_id, identifier, instruction):
        adapter = self.account.adapter
        account = str(await self.account.account_id())
        if self.store.one("SELECT 1 FROM dynamic_edits WHERE request_id=?", (request_id,)):
            previous = self.dynamic_edit(request_id, account, character["id"])
            if previous["platform_id"] != identifier:
                raise ValueError("这次修订请求已绑定另一条动态")
            return previous
        post = self.store.one(
            "SELECT id FROM dynamic_posts WHERE status='published' AND account_id=? AND character_id=?"
            " AND json_extract(receipt,'$.dynamic_id')=?",
            (account, character["id"], identifier),
        )
        if not post:
            raise ValueError("找不到当前账号和角色从工作台发布的这条动态")
        row = self._post(post["id"])
        original = await read_dynamic_caption(adapter, identifier, account)
        if not self._valid_sources(row):
            raise ValueError("原动态的公开来源已变化")
        async with self.runtime.offline_generation("background"):
            result = await budgeted_json(self.runtime.operation_llm, [
                {"role": "system", "content": (
                    "按号主的要求改写一条已经发布的动态。只输出可直接使用的正文。"
                    "原来的 @ 保留正文中的写法。只有号主要求改提及时才填 mentions，空列表表示全部移除。"
                    + MENTION_INSTRUCTION
                )},
                {"role": "user", "content": _json({
                    "instruction": instruction, "original": original["text"], "original_mentions": original.get("mentions", []),
                    "public_sources": [
                        {key: value for key, value in source.items() if key != "memory_dependencies"}
                        for source in row["sources"]
                    ],
                })},
            ], {
                "type": "object", "additionalProperties": False, "required": ["caption"],
                "properties": {"caption": {"type": "string", "minLength": 1}, "mentions": MENTION_REQUESTS},
            }, max_tokens=1024)
        if not isinstance(result, dict) or "caption" not in result or set(result) - {"caption", "mentions"}:
            raise ValueError("模型未返回完整的动态修订")
        if adapter is not self.account.adapter or self._post(row["id"]) != row or not self._valid_sources(row):
            raise ValueError("账号、动态或公开来源已变化")
        caption = self._edit_caption(row, result["caption"])
        if "mentions" in result:
            mentions, pending = await bind_mentions(adapter, caption, result["mentions"], original.get("mentions", []))
            if pending:
                raise ValueError("请在编辑要求中提供要 @ 的 B 站 UID 和当前昵称")
        else:
            mentions = rebase_mentions(caption, original.get("mentions", []))
        if adapter is not self.account.adapter or self._post(row["id"]) != row:
            raise ValueError("核对提及期间账号或动态已变化")
        self.store.execute(
            "INSERT INTO dynamic_edits(request_id,post_id,account_id,character_id,platform_id,original,caption,outgoing,status,mentions)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (request_id, row["id"], account, character["id"], identifier, _json(original),
             caption, self._outgoing_text(row, caption), "draft", _json(mentions)),
        )
        return self.dynamic_edit(request_id, account, character["id"])

    async def apply_dynamic_edit(self, request_id):
        async with self.account.credential_lock:
            adapter = self.account.adapter
            credentials = dict(self.account.credentials)
            account = str(await self.account.account_id())
            character = self._character()
            edit = self.dynamic_edit(request_id, account, character["id"])
            if edit["status"] == "applied":
                return edit
            row = self._post(edit["post_id"])
            identifier = edit["platform_id"]
            original = json.loads(edit["original"])

            def guard():
                if (
                    adapter is not self.account.adapter or self.account.credentials != credentials
                    or self._character() != character
                ):
                    raise ValueError("编辑期间账号或角色已变化")
                self.operations.assert_allowed(
                    automatic=False, channel="dynamics", account_id=account, character=character,
                )

            actual = await read_dynamic_caption(adapter, identifier, account)
            mentions = edit["mentions"] if edit["mentions"] is not None else rebase_mentions(edit["outgoing"], original.get("mentions", []))
            expected = {key: value for key, value in original.items() if key != "mentions"} | {"text": edit["outgoing"]}
            if mentions:
                expected["mentions"] = mentions
            if actual != expected and edit["status"] != "pending_verification":
                if actual != original or not self._valid_sources(row):
                    raise ValueError("原动态或公开来源已变化，请重新生成修订")
                if self.store.one(
                    "SELECT 1 FROM dynamic_edits WHERE post_id=? AND status IN ('submitting','pending_verification')",
                    (row["id"],),
                ):
                    raise ValueError("这条动态还有待核对的编辑")

                def reserve():
                    guard()
                    if self._post(row["id"]) != row or not self._valid_sources(row):
                        raise ValueError("原动态的公开来源已变化")
                    self.store.execute(
                        "UPDATE dynamic_edits SET status='submitting',caption=?,outgoing=?,error=NULL WHERE request_id=?",
                        (edit["caption"], edit["outgoing"], request_id),
                    )

                try:
                    await edit_dynamic_caption(adapter, identifier, edit["outgoing"], original["pictures"],
                                               before_send=reserve, **({"mentions": mentions} if mentions else {}))
                except PlatformRejected as error:
                    self.store.execute(
                        "UPDATE dynamic_edits SET status='draft',error=? WHERE request_id=?",
                        (str(error), request_id),
                    )
                    raise
                except (PublishUncertain, asyncio.CancelledError) as error:
                    self.store.execute(
                        "UPDATE dynamic_edits SET status='pending_verification',error='编辑结果待核对' "
                        "WHERE request_id=? AND status='submitting'", (request_id,),
                    )
                    if isinstance(error, asyncio.CancelledError):
                        raise
                else:
                    self.store.execute(
                        "UPDATE dynamic_edits SET status='pending_verification',error='编辑结果待核对' WHERE request_id=?",
                        (request_id,),
                    )
                try:
                    actual = await read_dynamic_caption(adapter, identifier, account)
                except (ValueError, httpx.HTTPError):
                    return self.dynamic_edit(request_id, account, character["id"])
            if actual == expected:
                guard()
                presentation = row.get("presentation")
                if presentation is not None or mentions:
                    presentation = (presentation or {"link_source_ids": None}) | {"mentions": mentions, "points": []}
                edited = self._outgoing(row | {"content": edit["caption"], "mentions": mentions}, character)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dynamic_posts SET content=?,content_hash=?,presentation=?,delivery_key=?,updated_at=? WHERE id=?",
                        (edit["caption"], _content_hash(edit["caption"]), _json(presentation) if presentation else None,
                         _delivery_key(edited), time.time(), row["id"]),
                    )
                    self.store.db.execute(
                        "UPDATE dynamic_edits SET status='applied',error=NULL WHERE request_id=?", (request_id,),
                    )
            return self.dynamic_edit(request_id, account, character["id"])

    async def evaluate(self, manual=False, *, topic=""):
        async with self.evaluation_lock, AsyncExitStack() as reservations:
            policy = self.policy()
            if not manual and (
                not policy["enabled"]
                or max(policy["next_evaluation_at"] or 0, policy["backoff_until"] or 0) > time.time()
            ):
                return {"status": "skipped", "reason": "运营已关闭或尚未到评估时间"}
            char = self._character()
            generation_identity = runtime_identity(self.runtime, char)
            evaluation_account = self._source_account()
            tasks = getattr(self.runtime, "tasks", None)
            evaluation_scope = tasks.scope() if tasks else None
            base = {
                "action": "wait",
                "reason": "暂缓动态评估",
                "content": "",
                "source_ids": [],
                "next_evaluation_seconds": policy["evaluate_interval_seconds"],
                "evaluation_account_id": evaluation_account,
            }
            if not manual and self._live_busy():
                base["reason"] = "直播队列繁忙，延后动态评估"
                return self._save_decision(char, manual, base, [])
            if not manual:
                blocked = None
                try:
                    self._quota()
                except SendLimitReached as error:
                    blocked = error
                browsing = getattr(self.runtime, "browsing", None)
                if policy["auto_publish"] and browsing:
                    shared = reservations.enter_context(browsing.sends.preparation_slot(
                        self.operations.policy()["account_id"], "dynamics",
                        {"max_per_day": policy["max_posts_per_day"]},
                        retry_seconds=policy["evaluate_interval_seconds"],
                        min_interval=policy["min_post_interval_seconds"],
                    ))
                    if shared and (not blocked or shared.next_at > blocked.next_at):
                        blocked = shared
                if blocked:
                    base.update(reason=str(blocked), next_evaluation_seconds=max(1, blocked.next_at - time.time()))
                    return self._save_decision(char, manual, base, [])
            current_time = datetime.now().astimezone().isoformat()
            memory_preference = self._memory_preference()
            messages = dynamic_decision_messages(
                char,
                self._recent_public_posts(char),
                [],
                current_time=current_time,
            )
            messages = identity_messages(messages, generation_identity)
            selected, selection_failed = None, False
            try:
                sources = recall_memory.search(self, recall_memory.RecallQuery())["items"]
                async with self.runtime.offline_generation("background"):
                    if not manual and self._live_busy():
                        base["reason"] = "直播队列繁忙，延后动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not manual and not self.policy()["enabled"]:
                        base["reason"] = "运营已停止，取消动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not self._valid_sources(
                        {"character_id": char["id"], "character_version": char["version"], "sources": sources}
                    ):
                        base["reason"] = "人物版本或公开来源已变化，取消动态评估"
                        return self._save_decision(char, manual, base, [])
                    model_identity = self._summary_model()

                    recall = None

                    def authorized(*, include_enabled=True):
                        return (
                            self._character()["id"] == char["id"]
                            and self._character()["version"] == char["version"]
                            and self._source_account() == evaluation_account
                            and (not tasks or tasks.scope() == evaluation_scope)
                            and self._valid_sources({"character_id": char["id"], "character_version": char["version"],
                                                     "sources": list(recall.sources.values()) if recall else sources})
                            and self._memory_preference() == memory_preference
                            and self._summary_model() == model_identity
                            and runtime_identity(self.runtime, self._character())["identity_version"]
                            == generation_identity["identity_version"]
                            and (not include_enabled or manual or self.policy()["enabled"])
                        )

                    def guard():
                        if not authorized(include_enabled=False):
                            raise ValueError("动态取材来源或授权已变化")

                    client = tasks.responses_client() if tasks else None
                    if client and sources:
                        browsing = self.runtime.browsing
                        recall = recall_memory.RecallSession(self, private=False)
                        recent = None if topic else recall_memory.recent_query(browsing)
                        objective = ({"目标": "按指定话题查找适合公开分享的相关素材，供本轮动态评估。", "本次话题": topic}
                                     if topic else {"目标": "从近期经历选话题，按话题回查相关旧记录。"
                                                          "选择可以交给本轮动态评估的记录，日常随想可以零引用。"})
                        sources = await recall.collect(client, objective, seed=recent, guard=guard)
                    elif topic:
                        sources = recall_memory.search(self, recall_memory.RecallQuery(topic=topic))["items"]
                    originals = recall_memory.originals(sources)
                    public_sources = [recall_memory.model_material(source, comment_texts=originals) for source in sources]
                    if not manual and self._live_busy():
                        base["reason"] = "直播队列繁忙，延后动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not authorized():
                        raise ValueError("运营记忆来源或授权已变化")
                    payload = json.loads(messages[1]["content"])
                    payload["public_sources"] = public_sources
                    account = getattr(self.account, "display_account_id", lambda: "")()
                    payload["related_posts"] = self._related_posts(char, account, sources)
                    for post in payload["related_posts"] + payload["recent_public_posts"]:
                        post["content"] = recall_memory.trim_repeated_quotes(post["content"], originals)
                        for point in post.get("points", []):
                            point["text"] = recall_memory.trim_repeated_quotes(point["text"], originals)
                    messages[1]["content"] = _json(payload)
                    maximum = self.settings.data["omni"].get("operations_max_tokens", 8192)
                    binding = {
                        "task": "generation",
                        "character": {"id": char["id"], "version": char["version"]},
                        "source_sha256": content_hash(public_sources),
                        "self_identity": generation_identity,
                        "evaluation_slot": current_time if manual else policy["next_evaluation_at"],
                        "epoch": self.operations.policy()["epoch"],
                    }

                    async def invoke_model(packet, schema):
                        return await budgeted_json(self.runtime.operation_llm, packet, schema, max_tokens=maximum)

                    async def invoke(packet, *, web_fetch=None):
                        schema = self.decision_schema(public_sources)
                        if web_fetch is not None:
                            return await web_fetch.complete(packet, schema, invoke_model)
                        return await invoke_model(packet, schema)

                    def diagnose(value):
                        return (
                            "正文包含大量重复内容，保留原文待审"
                            if value["action"] == "post"
                            and operation_output_diagnostics(value["content"])["needs_review"]
                            else None
                        )

                    data, generation_continuation = await generate_with_recovery(
                        self.runtime,
                        channel="generation.dynamics",
                        identity=content_hash(binding),
                        binding=binding,
                        character=char,
                        messages=messages,
                        generate=invoke,
                        validate=lambda value: self._parse(value, public_sources),
                        max_tokens=maximum,
                        guard=guard,
                        diagnose=diagnose,
                        context_data={"public_sources": sources},
                        allow_web_fetch=True,
                    )
                    data["presentation"] = await self._presentation(data, public_sources)
                    if not authorized(include_enabled=False):
                        raise ValueError("动态素材或账号已变化")
                    if (
                        data["action"] == "post" and data["illustration"] == "picture"
                        and illustration_policy(self.store).use_memes and self.memes and not workstation_image_decisions(self.runtime)
                    ):
                        try:
                            selected = await select_meme(
                                self.runtime,
                                self.memes,
                                char,
                                {"text": data["content"]},
                                data["content"],
                                mode="dynamic",
                            )
                        except Exception:
                            selection_failed = True
                        if not illustration_policy(self.store).use_memes:
                            selected = None
                        if not authorized(include_enabled=False):
                            raise ValueError("选图期间运营来源或授权已变化")
                data["next_evaluation_seconds"] = max(
                    data["next_evaluation_seconds"], policy["evaluate_interval_seconds"]
                )
                data["evaluation_account_id"] = evaluation_account
                if self._character()["id"] != char["id"] or self._character()["version"] != char["version"]:
                    raise ValueError("人物版本已变化")
                output_review = (
                    data["action"] == "post"
                    and operation_output_diagnostics(data["content"])["needs_review"]
                )
                decision = self._save_decision(
                    char,
                    manual,
                    data,
                    sources,
                    "长正文含大量重复段落，保留原文待审" if output_review else None,
                    generation_identity=generation_identity,
                    decide_image=not selected and not selection_failed,
                )
                if generation_continuation:
                    self.store.execute(
                        "UPDATE dynamic_decisions SET generation_continuation_id=? WHERE id=?",
                        (generation_continuation["id"], decision["id"]),
                    )
                if output_review and decision["post_id"]:
                    self.store.execute(
                        "UPDATE dynamic_posts SET error=? WHERE id=?",
                        ("长正文含大量重复段落，保留原文待审", decision["post_id"]),
                    )
                if selected and decision["post_id"]:
                    try:
                        self.review(decision["post_id"], "edit", meme_id=selected["asset_id"])
                    except ValueError:
                        selection_failed = True
                if selection_failed and decision["post_id"]:
                    error = "配图选择失败，正文已保存"
                    self.store.execute(
                        "UPDATE dynamic_posts SET error=? WHERE id=?", (error, decision["post_id"])
                    )
                    self.store.execute(
                        "UPDATE dynamic_decisions SET error=? WHERE id=?", (error, decision["id"])
                    )
                decision = next(d for d in self.list_decisions(100) if d["id"] == decision["id"])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                continuation = getattr(error, "continuation", None)
                if continuation:
                    base.update(
                        action="wait" if continuation["state"] in {"waiting", "abandoned"} else "error",
                        reason="本次运营生成已保留检查点",
                        next_evaluation_seconds=policy["evaluate_interval_seconds"],
                    )
                    saved = self._save_decision(char, manual, base, [], str(error))
                    self.store.execute(
                        "UPDATE dynamic_decisions SET status=?,generation_continuation_id=? WHERE id=?",
                        (error.continuation["state"], error.continuation["id"], saved["id"]),
                    )
                    return next(item for item in self.list_decisions(100) if item["id"] == saved["id"])
                base.update(action="error", reason="模型评估未返回合法完整决定", next_evaluation_seconds=3600)
                return self._save_decision(char, manual, base, [], "模型评估失败，请检查模型服务与输出格式")
            if (
                not manual
                and decision["post_id"]
                and self.policy()["enabled"]
                and self.policy()["auto_publish"]
                and not selection_failed
                and not output_review
                and not data["presentation"]["pending_mentions"]
            ):
                self._automatic_post_id = decision["post_id"]
                try:
                    if browsing := getattr(self.runtime, "browsing", None):
                        browsing.sends.enqueue("dynamics", decision["post_id"])
                    else:
                        await self.publish(decision["post_id"], automatic=True)
                except ValueError:
                    pass  # Durable post status/schedule is the actionable outcome.
                finally:
                    self._automatic_post_id = None
                decision = next(d for d in self.list_decisions(100) if d["id"] == decision["id"])
            return decision

    async def open(self):
        if self.worker is None or self.worker.done():
            self.closed = False
            self.worker = asyncio.create_task(self._loop())

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None

    async def pause(self):
        running = self.worker is not None and not self.worker.done()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None
        if running and not self.closed:
            await self.open()

    async def _loop(self):
        while not self.closed:
            self.changed.clear()
            policy = self.policy()
            if policy["enabled"]:
                try:
                    async with self.operations.automatic_work() as admitted:
                        if admitted:
                            browsing = getattr(self.runtime, "browsing", None)
                            if browsing and not policy["auto_publish"] and not browsing.review_capacity("dynamics"):
                                self._schedule(policy["evaluate_interval_seconds"], "动态审核积压")
                            else:
                                await self.evaluate()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._schedule(3600, "动态评估服务异常，已延后重试")
            policy = self.policy()
            due = max(policy["next_evaluation_at"] or 0, policy["backoff_until"] or 0)
            delay = max(1, due - time.time()) if policy["enabled"] else 86400
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=delay)
            except TimeoutError:
                pass
