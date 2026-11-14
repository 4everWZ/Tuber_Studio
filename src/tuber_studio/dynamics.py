"""Public-source-only character operations; scheduling is separate from live jobs."""

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime

import httpx

from .context_delivery import memory_source_allowed
from .daily_memory import DailyMemoryService
from .dynamic_comment_card import comment_card, render_comment_card
from .dynamic_grounding import review_caption
from .generation_meter import budgeted_json
from .meme_selection import select_meme
from .operation_continuations import content_hash
from .operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    continuation_view,
    generation_view,
    publication_binding,
)
from .operation_generation import generate_with_recovery
from .operation_identity import identity_messages, runtime_identity
from .operation_text import (
    MAX_OPERATION_TEXT_BYTES,
    operation_character,
    operation_output_diagnostics,
)
from .operations_control import OperationsControl, canonical_intent
from .operations_memory import OperationsMemory
from .platforms.bilibili import PublishUncertain
from .platforms.bilibili_dynamics import (
    PublishCancelled,
    PublishPreparationCancelled,
    PublishPreparationFailed,
    edit_dynamic_caption,
    publish_image,
    publish_text,
    read_dynamic_caption,
    validate_text,
)
from .platforms.bilibili_images import bfs_image_path
from .publication_errors import PlatformRejected, UnknownPublication

DEFAULT_POLICY = {
    "enabled": False,
    "auto_publish": True,
    "use_memes": False,
    "evaluate_interval_seconds": 1800,
    "min_post_interval_seconds": 21600,
    "max_posts_per_day": 2,
    "memory_summary_max_tokens": 2048,
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def dynamic_decision_messages(character, recent_posts, public_sources, *, current_time):
    return [
        {
            "role": "system",
            "content": (
                "挑一件值得分享的事，以角色的口吻讲给刚听说这件事的关注者。"
                "public_incident是公开经历和原评论，published_reply是来评和你当时发出的回复，"
                "public_memory是角色故事摘录，runtime_event是运行记录，operator_note是号主确认的经历。"
                "一句日常观察、感想或评论区趣事就可以单独分享，篇幅随话题。"
                "材料确实有经过时可按实际经过讲。动作、次数和结果以素材为准。"
                "分享时选post，content写动态正文，source_ids填一项来源。暂缓时选wait，content留空。"
                "reason说明决定，next_evaluation_seconds是下次评估间隔。"
            ),
        },
        {
            "role": "user",
            "content": _json(
                {
                    "character": {
                        key: value
                        for key, value in operation_character(character).items()
                        if key in ("name", "core", "style", "rules")
                    },
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


class Dynamics:
    def __init__(self, settings, store, runtime, comments, *, memes=None):
        self.settings, self.store, self.runtime, self.comments = settings, store, runtime, comments
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
        if "send_payload" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_posts)")}:
            store.execute("ALTER TABLE dynamic_posts ADD COLUMN send_payload TEXT")
        if "generation_continuation_id" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(dynamic_decisions)")
        }:
            store.execute("ALTER TABLE dynamic_decisions ADD COLUMN generation_continuation_id TEXT")
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

    def policy(self):
        base = DEFAULT_POLICY | self.store.get("dynamic_policy", {})
        return self.operations.effective("dynamics", base) | {
            "base": base,
            "credentials_configured": bool(self.comments.policy()["credentials_configured"]),
            **self.store.get(
                "dynamic_schedule",
                {
                    "next_evaluation_at": None,
                    "backoff_until": None,
                    "last_error": None,
                },
            ),
        }

    async def set_policy(self, patch):
        if not isinstance(patch, dict) or not set(patch) <= set(DEFAULT_POLICY):
            raise ValueError("未知动态运营策略字段")
        data = DEFAULT_POLICY | self.store.get("dynamic_policy", {}) | patch
        for name in ("enabled", "auto_publish", "use_memes"):
            if type(data[name]) is not bool:
                raise ValueError("运营开关须为布尔值")
        for name, minimum, maximum in (
            ("evaluate_interval_seconds", 300, 86400),
            ("min_post_interval_seconds", 21600, 604800),
            ("max_posts_per_day", 1, 2),
            ("memory_summary_max_tokens", 256, 4096),
        ):
            if type(data[name]) is not int or not minimum <= data[name] <= maximum:
                raise ValueError(f"{name} 须在 {minimum} 到 {maximum} 之间")
        if data["enabled"] and not self.policy()["credentials_configured"]:
            raise ValueError("开启动态运营需要先配置账号登录凭证")
        self.store.set("dynamic_policy", data)
        self.changed.set()
        return self.policy()

    def _character(self):
        return self.store.character()

    def _summary_model(self):
        fields = (
            "kind",
            "base_url",
            "model",
            "context_tokens",
            "native_context_tokens",
            "tokenizer",
            "tokenizer_path",
            "chat_template",
            "enable_thinking",
        )
        active = getattr(self.runtime.llm, "config", {})
        active = active if isinstance(active, dict) else {}
        identity = {
            "settings": {k: self.settings.data["llm"].get(k) for k in fields},
            "provider": {k: active.get(k) for k in fields},
            "implementation": type(self.runtime.llm).__module__ + "." + type(self.runtime.llm).__qualname__,
        }
        return hashlib.sha256(_json(identity).encode()).hexdigest()

    def _used_source_ids(self):
        result = set()
        for row in self.store.all("SELECT source_ids FROM dynamic_posts WHERE status != 'invalidated'"):
            result.update(json.loads(row["source_ids"]))
        return result

    def _summary_sources(self):
        used = self._used_source_ids()
        # Keep existing public material for the preview, while new material has
        # priority when the bounded source window fills. Canonical ordering keeps
        # the same source set reusable when a draft merely changes review state.
        candidates = sorted(self._sources(), key=lambda source: source["id"] in used)
        sources, size = [], 0
        for source in candidates:
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

    def _memory_rows(self):
        if not self.store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='grounded_memory'"):
            return []
        char = self._character()
        rows = self.store.all(
            "SELECT * FROM grounded_memory WHERE character_id=? AND character_version=? "
            "AND role='assistant' AND kind='role_story' AND fictional=1 "
            "AND scope NOT LIKE 'dm:%' AND scope NOT LIKE 'operator:%' ORDER BY id DESC LIMIT 200",
            (char["id"], char["version"]),
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

    def _sources(self):
        char = self._character()
        scope, preference = self._memory_preference()
        result = []
        if preference["enabled"]:
            result = self.daily_memory.public_incident_sources(char["id"], char["version"])
            result.extend(self.daily_memory.public_experience_sources(char["id"], char["version"]))
        if scope and preference["enabled"]:
            for row in self.daily_memory.organized(scope, limit=100):
                if row["status"] != "active" or "dynamic" not in row["channels"]:
                    continue
                anchor = row["source"].get("thread") or {}
                references = row["source"]["references"]
                result.append({"id": f"organized:{row['id']}", "type": "public_incident", "fictional": False,
                               "text": row["anecdote"], "memory_version": row["updated_at"],
                               "source_url": references[0]["href"], "public_sources": [
                                   {"bvid": anchor["resource"], "thread_id": anchor["root_id"],
                                    "comment_id": ref["comment_id"], "quote": ref["text"]} for ref in references
                               ] if anchor.get("resource", "").startswith("BV") else []})
        if preference["revision"]:
            for source in result:
                source["memory_preference_revision"] = preference["revision"]
        rows = self.store.all(
            "SELECT d.id,d.text,d.receipt,c.video_id,c.comment_id,c.content FROM drafts d "
            "JOIN comments c ON c.id=d.comment_id WHERE d.status='published' AND c.platform='bilibili' "
            "AND json_extract(d.answer_check,'$.organized_memory') IS NULL "
            "AND d.character_id=? AND d.character_version=? ORDER BY d.id DESC LIMIT 100",
            (char["id"], char["version"]),
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
                }
            )
        if browsing := getattr(self.runtime, "browsing", None):
            account_id = browsing.account or self.comments.display_account_id()
            if account_id:
                for row in self.store.all(
                    "SELECT id,text,target,receipt FROM browsing_intents WHERE kind='reply_public' AND status='published' "
                    "AND json_extract(target,'$.memory_audit') IS NULL "
                    "AND account_id=? AND character_id=? AND character_version=? ORDER BY updated_at DESC LIMIT 100",
                    (account_id, char["id"], char["version"]),
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
                    })
        for row in self._memory_rows() if preference["enabled"] else ():
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
                        **({"memory_preference_revision": preference["revision"]} if preference["revision"] else {}),
                    }
                )
        return result

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
        if "manual" in row:
            row["manual"] = bool(row["manual"])
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

    def _post(self, post_id):
        return self._post_view(self.store.one("SELECT * FROM dynamic_posts WHERE id=?", (post_id,)))

    def _post_view(self, row):
        post = self._decode(row)
        post.pop("send_payload", None)
        snapshot = post.pop("comment_card_snapshot", None)
        post["comment_card"] = json.loads(snapshot) if snapshot is not None else comment_card(post["sources"], legacy=True)
        return continuation_view(self.runtime, "dynamics", post)

    def comment_card_image(self, post_id, page=0):
        card = self._post(post_id)["comment_card"]
        if not card:
            raise ValueError("这条动态没有原评论图")
        return render_comment_card(card, page)

    def _valid_sources(self, row):
        char = self._character()
        sources = row["sources"]
        if row.get("manual") and len(sources) == 1 and sources[0].get("type") == "operator_topic":
            decision = self.store.one("SELECT sources FROM dynamic_decisions WHERE id=?", (row["decision_id"],))
            return (
                (row["character_id"], row["character_version"]) == (char["id"], char["version"])
                and decision is not None
                and json.loads(decision["sources"]) == sources
            )
        current = {s["id"]: s for s in self._sources()}
        dependencies = [item for source in sources for item in source.get("memory_dependencies", [])]
        originals = [{key: value for key, value in source.items() if key != "memory_dependencies"} for source in sources]
        return (
            row["character_id"] == char["id"]
            and row["character_version"] == char["version"]
            and bool(row["sources"])
            and all(current.get(s["id"]) == s for s in originals + dependencies)
        )

    def review(self, post_id, action, text=None, **options):
        row = self._post(post_id)
        if action not in {"approve", "reject", "edit", "copy"} or row["status"] not in {
            "pending_review",
            "approved",
            "rejected",
        }:
            raise ValueError("当前动态不能执行该审核操作")
        if set(options) - {"meme_id"} or options and action != "edit":
            raise ValueError("更换配图需要edit并重新审核")
        meme_id = options.get("meme_id", row.get("meme_id"))
        if not self._valid_sources(row):
            self._status(post_id, "invalidated", "人物版本或公开来源已变化")
            raise ValueError("人物版本或公开来源已变化，需重新评估")
        if action == "copy":
            if meme_id or row["comment_card"]:
                raise ValueError("图文动态不能复制成纯文字，请审核后发布图文")
            if row["status"] != "approved":
                raise ValueError("请先审核动态再复制")
            return row | {"copy_text": row["content"]}
        content = validate_text(text) if text is not None else row["content"]
        owner = f"dynamic-draft:{post_id}"
        if meme_id and row["comment_card"]:
            meme_id = None
        if meme_id and action != "reject":
            if not self.memes:
                raise ValueError("表情组件尚未启用")
            self.memes.pin(meme_id, row["character_id"], owner)
        status = {"approve": "approved", "reject": "rejected", "edit": "pending_review"}[action]
        try:
            self.store.execute(
                "UPDATE dynamic_posts SET status=?,content=?,content_hash=?,meme_id=?,error=NULL,updated_at=? WHERE id=?",
                (status, content, _content_hash(content), meme_id, time.time(), post_id),
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
        if row["last"] and now - row["last"] < policy["min_post_interval_seconds"]:
            raise ValueError("尚未达到动态最小发布间隔")
        if (row["daily"] or 0) >= policy["max_posts_per_day"]:
            raise ValueError("已达到24小时动态发布上限")

    def _publication_binding(self, row, account, character):
        existing = getattr(self.runtime, "continuations", None)
        existing = existing.find("dynamics", row["id"]) if existing else None
        intent = (
            existing["binding"]["intent"]
            if existing
            else canonical_intent("bilibili", "dynamic", row["content_hash"], character["id"])
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
        if card := row["comment_card"]:
            links = card["comments"] if card.get("legacy") else card["links"]
            return list(dict.fromkeys(item["url"] for item in links))
        return []

    @classmethod
    def _outgoing_text(cls, row, text):
        if links := cls._comment_links(row):
            text += "\n\n" + "\n".join(f"原评论：{url}" for url in links)
        return text

    @classmethod
    def _edit_caption(cls, row, text):
        text = validate_text(text)
        if links := cls._comment_links(row):
            known = "|".join(re.escape(url) for url in links)
            text = re.sub(rf"(?:\s*原评论(?:链接)?\s*[：:]\s*(?:{known}))+\s*\Z", "", text)
        return validate_text(text)

    def _outgoing(self, row, character):
        value = {"text": self._outgoing_text(row, row["content"])}
        if card := row["comment_card"]:
            pages = card.get("page_count", 1)
            value["comment_card_sha256"] = hashlib.sha256(render_comment_card(card)).hexdigest()
            if pages > 1:
                value["comment_card_page_sha256"] = [
                    value["comment_card_sha256"],
                    *(hashlib.sha256(render_comment_card(card, page)).hexdigest() for page in range(1, pages)),
                ]
        elif row.get("meme_id"):
            if not self.memes:
                raise ValueError("配图组件未挂载")
            asset = self.memes.resolve(row["meme_id"], character)
            value |= {"meme_id": row["meme_id"], "asset_sha256": asset["sha256"]}
        return value

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
        account = row.get("account_id") or getattr(self.comments, "display_account_id", lambda: "")() or None
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
        original = {key: row.get(key) for key in ("status", "content", "sources", "meme_id", "account_id")}

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
                [
                    {"content": post["content"], "published_at": post["published_at"]}
                    for post in self.list_posts("published", 5)
                ],
                row["sources"],
                current_time=datetime.now().astimezone().isoformat(),
            ),
            identity,
        )
        maximum = self.settings.data["llm"].get("operations_max_tokens", 8192)

        async def invoke(packet):
            return await budgeted_json(
                self.runtime.llm, packet, self.decision_schema(row["sources"]), max_tokens=maximum
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
            self.store.execute(
                "UPDATE dynamic_posts SET content=?,content_hash=?,reason=?,sources=?,source_ids=?,"
                "generation_identity=?,comment_card_snapshot=?,updated_at=?,error=NULL WHERE id=?",
                (
                    data["content"],
                    _content_hash(data["content"]),
                    data["reason"],
                    _json(selected),
                    _json(data["source_ids"]),
                    _json(identity),
                    _json(comment_card(selected, store=self.store, actor=identity)),
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

    async def _publication_context(self, post_id, automatic, epoch):
        await self._refresh_post_identity(post_id, automatic, epoch)
        row = self._post(post_id)
        character = self.store.character(row["character_id"], row["character_version"])
        cached = getattr(self.comments, "display_account_id", lambda: "")()
        account = cached or (
            await self.comments.account_id()
            if hasattr(self.comments, "account_id")
            else "injected-test-account"
        )
        binding = self._publication_binding(row, account, character)
        adapter = self.comments.adapter
        credential = dict(getattr(self.comments, "credentials", {}))

        def guard():
            fresh = self._post(post_id)
            if (
                not self._valid_sources(fresh)
                or any(
                    fresh.get(key) != row.get(key)
                    for key in ("status", "content", "meme_id", "character_id", "character_version")
                )
                or self._publication_binding(fresh, account, character) != binding
            ):
                raise ValueError("恢复期间动态正文、角色或原始公开来源已变化")
            if (
                self.comments.adapter is not adapter
                or getattr(self.comments, "credentials", {}) != credential
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

        return PublicationContext(
            binding,
            row["content"],
            character,
            {"public_sources": row["sources"], "reason": row["reason"]},
            guard,
            payload=self._outgoing(row, character),
            encode_revision=lambda value: self._outgoing(row | {"content": validate_text(value)}, character),
            on_defer=defer,
            manual_approved=row["status"] == "approved",
        )

    def _apply_revision(self, post_id, prepared, text):
        prepared.guard()
        text = validate_text(text)
        self.store.execute(
            "UPDATE dynamic_posts SET content=?,content_hash=?,status='pending_review',error=NULL,updated_at=? WHERE id=?",
            (text, _content_hash(text), time.time(), post_id),
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

    async def _reconcile(self, post_id):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        async def read(row, attempt):
            adapter = self.comments.adapter
            if not hasattr(adapter, "reconcile_dynamic_publication"):
                return {"status": "unsupported"}
            try:
                account = await self.comments.account_id()
            except ValueError:
                return {"status": "check_failed", "reason": "account_unavailable"}
            if account != row["binding"]["account_id"]:
                return {"status": "check_failed", "reason": "account_changed"}
            payload = row["feedback"]["sent_payload"]
            target = row["binding"]["target"] | {
                "message_kind": "image" if payload.get("meme_id") or payload.get("comment_card_sha256") else "text"
            }
            if payload.get("prepared_images"):
                target["images"] = payload["prepared_images"]
            return await adapter.reconcile_dynamic_publication(
                target,
                author_uid=account,
                text=payload["text"],
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )

        def commit(receipt, verification):
            self._status(post_id, "published", receipt=receipt)
            if self.memes:
                self.memes.release(f"dynamic-draft:{post_id}")

        return await feedback.reconcile("dynamics", post_id, read, commit)

    async def publish(self, post_id, automatic=False, *, expected_text=None):
        if not hasattr(self.runtime, "settings"):
            self.runtime.settings = self.settings
        control = self.operations.policy()
        epoch = control["epoch"] if automatic and control["enabled"] else None
        return await FeedbackPublication(self.runtime, self.store, self.operations).submit(
            "dynamics",
            post_id,
            prepare=lambda: self._publication_context(post_id, automatic, epoch),
            send=lambda ticket, binding, body_hash: self._publish_once(
                post_id,
                automatic,
                revision_ticket=ticket,
                binding=binding,
                body_hash=body_hash,
                authorization_epoch=epoch,
                expected_text=expected_text,
            ),
            apply_revision=lambda prepared, text: self._apply_revision(post_id, prepared, text),
            automatic=automatic,
            epoch=epoch,
            reconcile=lambda token, prepared: self._reconcile(post_id),
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
    ):
        # Model regeneration may wait for the inference lock. Finish it before
        # acquiring the shared platform write lock used by other generators.
        await self._refresh_post_identity(post_id, automatic, authorization_epoch)
        checked = None
        if automatic:
            checked = self._post(post_id)
            self._check_automatic_draft(checked, self.policy())
            if not self._valid_sources(checked):
                self._status(post_id, "invalidated", "人物版本或公开来源已变化")
                raise ValueError("人物版本或公开来源已变化，需重新评估")
            try:
                async with self.runtime.offline_generation("background"):
                    self._check_automatic_draft(checked, self.policy())
                    if self._post(post_id) != checked or not self._valid_sources(checked):
                        raise ValueError("草稿或公开来源已变化")
                    supported = await review_caption(self.runtime.llm, checked["content"], checked["sources"])
            except (ValueError, TimeoutError, httpx.HTTPError):
                error = "动态事实核对未完成，保留待审"
            else:
                error = None if supported else "正文中的动作、次数或结果缺少来源支持，保留待审"
            if error:
                if self._post(post_id) == checked:
                    with self.store.db:
                        self.store.db.execute("UPDATE dynamic_posts SET error=? WHERE id=?", (error, post_id))
                        self.store.db.execute(
                            "UPDATE dynamic_decisions SET error=? WHERE id=?", (error, checked["decision_id"]),
                        )
                raise ValueError(error)
        async with self.comments.publish_lock:
            row, policy = self._post(post_id), self.policy()
            if expected_text is not None and row["content"] != expected_text:
                raise ValueError("草稿已变化，请重新检查当前正文")
            if automatic:
                self._check_automatic_draft(row, policy)
                if row != checked:
                    raise ValueError("草稿已变化，请重新核对当前正文")
            elif row["status"] != "approved":
                raise ValueError("请先审核通过动态")
            if not policy["credentials_configured"]:
                raise ValueError("发布动态需要先配置账号登录凭证")
            if not self._valid_sources(row):
                self._status(post_id, "invalidated", "人物版本或公开来源已变化")
                raise ValueError("人物版本或公开来源已变化，需重新评估")
            self._quota(post_id if revision_ticket else None)
            if self.store.one(
                "SELECT id FROM dynamic_posts WHERE content_hash=? AND attempted_at IS NOT NULL"
                + (" AND id<>?" if revision_ticket else ""),
                (row["content_hash"], *((post_id,) if revision_ticket else ())),
            ):
                raise ValueError("相同正文已尝试发布，请勿重复发送")
            self._check_used_sources(row)
            image = None
            if card := row["comment_card"]:
                pages = [{"mime_type": "image/png", "content": render_comment_card(card, page)}
                         for page in range(card.get("page_count", 1))]
                image = pages if len(pages) > 1 else pages[0]
            elif row.get("meme_id"):
                if not self.memes:
                    raise ValueError("图文草稿缺少表情组件，保留待审")
                image = self.memes.read(row["meme_id"], row["character_id"])
            adapter = self.comments.adapter
            account_id = (
                await self.comments.account_id()
                if hasattr(self.comments, "account_id")
                else "injected-test-account"
            )
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
            prepared_images = []

            def record_prepared_images(images):
                prepared_images.extend(
                    {"path": bfs_image_path(item["url"]), "width": item["width"], "height": item["height"]}
                    for item in images
                )

            def attempted_payload():
                return payload | {"prepared_images": prepared_images} if prepared_images else payload

            def save_attempt(attempt_token):
                self.store.db.execute(
                    "UPDATE dynamic_posts SET attempted_at=?,updated_at=?,send_payload=? WHERE id=?",
                    (time.time(), time.time(), _json({
                        "token": attempt_token, "binding": actual_binding, "payload": attempted_payload(),
                    }), post_id),
                )
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash(payload)
            ):
                raise ValueError("发送前动态原始绑定或正文已变化")
            token = None

            def before_send(*, reserve=True):
                nonlocal token
                current_policy = self.policy()
                if (
                    self.comments.adapter is not adapter
                    or not current_policy["credentials_configured"]
                    or (automatic and not (current_policy["enabled"] and current_policy["auto_publish"]))
                ):
                    raise PublishCancelled("运营已停止或账号已变化，取消发送")
                fresh = self._post(post_id)
                if (
                    fresh["status"] != "publishing"
                    or not self._valid_sources(fresh)
                    or fresh["content"] != row["content"]
                    or fresh["sources"] != row["sources"]
                    or fresh["comment_card"] != row["comment_card"]
                    or fresh.get("meme_id") != row.get("meme_id")
                ):
                    raise PublishCancelled("人物版本或公开来源已变化，取消发送")
                if image and not card:
                    try:
                        current_asset = self.memes.resolve(row["meme_id"], character)
                        if current_asset["sha256"] != image["asset"]["sha256"]:
                            raise ValueError("配图原文已变化")
                    except ValueError:
                        raise PublishCancelled("当前配图已禁用、解绑或变化，取消发送") from None
                try:
                    self._quota(post_id if revision_ticket else None)
                    self._check_used_sources(fresh)
                    if self.store.one(
                        "SELECT id FROM dynamic_posts WHERE content_hash=? AND attempted_at IS NOT NULL"
                        + (" AND id<>?" if revision_ticket else ""),
                        (fresh["content_hash"], *((post_id,) if revision_ticket else ())),
                    ):
                        raise ValueError("相同正文已尝试发布")
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
            if (
                fresh["status"] != row["status"]
                or fresh["content"] != row["content"]
                or fresh["sources"] != row["sources"]
                or fresh["comment_card"] != row["comment_card"]
                or fresh.get("meme_id") != row.get("meme_id")
            ):
                raise ValueError("账号核验期间草稿已变化，保留当前审核状态；请重新确认后发布")
            self.store.execute(
                "UPDATE dynamic_posts SET status='publishing',updated_at=? WHERE id=?", (time.time(), post_id)
            )
            try:
                if image:
                    receipt = await publish_image(
                        adapter,
                        payload["text"],
                        image,
                        before_send=before_send,
                        prepare_authorized=lambda: before_send(reserve=False),
                        on_prepared=record_prepared_images,
                    )
                else:
                    receipt = await publish_text(adapter, payload["text"], before_send=before_send)
            except PublishPreparationCancelled:
                self._status(post_id, row["status"], "配图准备已取消，尚未提交动态")
                raise
            except PublishPreparationFailed:
                self._status(post_id, row["status"], "配图准备失败，尚未提交动态；保留待审")
                raise ValueError("配图准备失败，尚未提交动态；请审核后重试") from None
            except PublishCancelled:
                if token:
                    self.operations.finish(token, "cancelled")
                status = row["status"] if self._valid_sources(row) else "invalidated"
                self._status(post_id, status, "发送前授权或来源已变化，取消发送；未写入平台")
                raise ValueError("发送前授权或来源已变化，取消发送；未写入平台") from None
            except asyncio.CancelledError:
                if token is None:
                    self._status(post_id, row["status"], "准备已取消，未提交动态")
                    raise
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态发送结果不确定"
                    )
                self._status(post_id, "publish_uncertain", "写入被取消，请人工核对平台")
                raise
            except PublishUncertain:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态发送结果不确定"
                    )
                self._status(post_id, "publish_uncertain", "写入结果不确定，请人工核对平台，禁止重试")
                raise UnknownPublication("写入结果不确定，请人工核对平台，禁止重试", token) from None
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
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "dynamics", post_id, token, actual_binding, attempted_payload(), "动态发送结果不确定"
                    )
                self._status(post_id, "publish_uncertain", "写入或回执保存失败，请人工核对平台")
                raise UnknownPublication("写入或回执保存失败，请人工核对平台，禁止重试", token) from None
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
                raise UnknownPublication("平台写入后保存回执失败，请人工核对平台，禁止重试", token) from None
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

    def _check_used_sources(self, row):
        wanted = set(row["source_ids"])
        for other in self.store.all(
            "SELECT source_ids FROM dynamic_posts WHERE attempted_at IS NOT NULL AND id<>?", (row["id"],)
        ):
            if wanted.intersection(json.loads(other["source_ids"])):
                raise ValueError("此来源已尝试用于动态，请等待新的公开来源")

    def _live_busy(self):
        live = getattr(self.runtime, "live", None)
        return live is not None and live.state in {"live", "paused"} and live.busy

    @staticmethod
    def decision_schema(sources):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "reason", "content", "source_ids", "next_evaluation_seconds"],
            "properties": {
                "action": {"type": "string", "enum": ["post", "wait"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                "content": {"type": "string"},
                "source_ids": {
                    "type": "array",
                    "maxItems": min(len(sources), 1),
                    "items": {"type": "string", "enum": [source["id"] for source in sources]},
                },
                "next_evaluation_seconds": {"type": "integer", "minimum": 300, "maximum": 86400},
            },
        }

    @staticmethod
    def _parse(data, sources):
        if not isinstance(data, dict) or len(_json(data).encode("utf-8")) > MAX_OPERATION_TEXT_BYTES + 16384:
            raise ValueError("模型决定格式无效")
        if not isinstance(data, dict) or set(data) != {
            "action",
            "reason",
            "content",
            "source_ids",
            "next_evaluation_seconds",
        }:
            raise ValueError("模型决定字段无效")
        if data["action"] not in {"post", "wait"}:
            raise ValueError("模型决定动作无效")
        if not isinstance(data["reason"], str) or not 1 <= len(data["reason"]) <= 500:
            raise ValueError("模型决定理由无效")
        ids = data["source_ids"]
        allowed = {s["id"] for s in sources}
        if not isinstance(ids, list) or len(ids) > 1 or any(not isinstance(i, str) for i in ids):
            raise ValueError("模型来源列表无效")
        if not set(ids) <= allowed or (data["action"] == "post" and not ids):
            raise ValueError("模型引用了未授权来源")
        seconds = data["next_evaluation_seconds"]
        if type(seconds) is not int or not 300 <= seconds <= 86400:
            raise ValueError("模型下次评估间隔无效")
        if data["action"] == "post":
            data["content"] = validate_text(data["content"])
        elif data["content"] != "":
            raise ValueError("等待决定不得包含待发布正文")
        return data

    def _save_decision(
        self, char, manual, data, sources, error=None, *, generation_identity=None, schedule=True
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
        model = str(self.settings.data["llm"].get("model", self.settings.data["llm"]["kind"]))
        next_at = now + data["next_evaluation_seconds"]
        with self.store.db:
            decision_id = self.store.db.execute(
                "INSERT INTO dynamic_decisions(character_id,character_version,model,manual,action,reason,content,"
                "source_ids,sources,next_evaluation_at,created_at,status,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            ).lastrowid
            if data["action"] == "post":
                post_id = self.store.db.execute(
                    "INSERT INTO dynamic_posts(decision_id,character_id,character_version,model,content,reason,"
                    "source_ids,sources,status,created_at,updated_at,content_hash,manual,account_id,comment_card_snapshot) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        getattr(self.comments, "display_account_id", lambda: "")() or "",
                        _json(comment_card(selected, store=self.store, actor=generation_identity)),
                    ),
                ).lastrowid
                self.store.db.execute("UPDATE dynamic_decisions SET post_id=? WHERE id=?", (post_id, decision_id))
                self.store.db.execute(
                    "UPDATE dynamic_posts SET generation_identity=? WHERE id=?",
                    (_json(generation_identity or runtime_identity(self.runtime, char)), post_id),
                )
        if schedule:
            self._schedule(data["next_evaluation_seconds"], error)
        return next(d for d in self.list_decisions(100) if d["id"] == decision_id)

    async def create_operator_draft(self, character, request_id, topic):
        source = {"id": f"operator-topic:{request_id}", "type": "operator_topic", "text": topic}
        async with self.evaluation_lock:
            existing = self.store.one(
                "SELECT id, character_id, character_version, sources FROM dynamic_posts "
                "WHERE manual=1 AND source_ids=?",
                (_json([source["id"]]),),
            )
            if existing:
                if (
                    (existing["character_id"], existing["character_version"])
                    != (character["id"], character["version"])
                    or json.loads(existing["sources"]) != [source]
                ):
                    raise ValueError("请求已用于其他动态草稿")
                return self._post(existing["id"])
            identity = runtime_identity(self.runtime, character)
            messages = dynamic_decision_messages(
                character, [], [source], current_time=datetime.now().astimezone().isoformat()
            )
            messages.append({
                "role": "system",
                "content": "为号主在 operator_topic 提出的主题写一条动态草稿，选 post 并引用这项来源。",
            })
            async with self.runtime.offline_generation("background"):
                result = await budgeted_json(
                    self.runtime.llm, identity_messages(messages, identity), self.decision_schema([source]),
                    max_tokens=512,
                )
            current = self._character()
            if (current["id"], current["version"]) != (character["id"], character["version"]) or (
                runtime_identity(self.runtime, current) != identity
            ):
                raise ValueError("生成期间账号或角色已变化")
            data = self._parse(result, [source])
            if data["action"] != "post":
                raise ValueError("本次没有可保存的动态正文")
            saved = self._save_decision(character, True, data, [source], generation_identity=identity, schedule=False)
            return self._post(saved["post_id"])

    def dynamic_edit(self, request_id, account, character_id):
        row = self.store.one(
            "SELECT * FROM dynamic_edits WHERE request_id=? AND account_id=? AND character_id=?",
            (request_id, account, character_id),
        )
        if row is None:
            raise ValueError("未找到当前账号和角色的动态修订")
        if row["status"] == "draft":
            post = self._post(row["post_id"])
            caption = self._edit_caption(post, row["caption"])
            row = row | {"caption": caption, "outgoing": self._outgoing_text(post, caption)}
        return row

    async def propose_dynamic_edit(self, character, request_id, identifier, instruction):
        adapter = self.comments.adapter
        account = str(await self.comments.account_id())
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
            result = await budgeted_json(self.runtime.llm, [
                {"role": "system", "content": (
                    "按号主的要求改写一条已经发布的动态。只输出可直接使用的正文。"
                    "事实以公开来源为准，原动态可能有错。可以只写一句日常感想。"
                    "删除无依据的动作、次数和结果。原评论链接由程序添加。"
                )},
                {"role": "user", "content": _json({
                    "instruction": instruction, "original": original["text"],
                    "public_sources": [
                        {key: value for key, value in source.items() if key != "memory_dependencies"}
                        for source in row["sources"]
                    ],
                })},
            ], {
                "type": "object", "additionalProperties": False, "required": ["caption"],
                "properties": {"caption": {"type": "string", "minLength": 1}},
            }, max_tokens=1024)
        if not isinstance(result, dict) or set(result) != {"caption"}:
            raise ValueError("模型未返回完整的动态修订")
        if adapter is not self.comments.adapter or self._post(row["id"]) != row or not self._valid_sources(row):
            raise ValueError("账号、动态或公开来源已变化")
        caption = self._edit_caption(row, result["caption"])
        self.store.execute(
            "INSERT INTO dynamic_edits VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (request_id, row["id"], account, character["id"], identifier, _json(original),
             caption, self._outgoing_text(row, caption), "draft"),
        )
        return self.dynamic_edit(request_id, account, character["id"])

    async def apply_dynamic_edit(self, request_id):
        async with self.comments.publish_lock:
            adapter = self.comments.adapter
            credentials = dict(self.comments.credentials)
            account = str(await self.comments.account_id())
            character = self._character()
            edit = self.dynamic_edit(request_id, account, character["id"])
            if edit["status"] == "applied":
                return edit
            row = self._post(edit["post_id"])
            identifier = edit["platform_id"]
            original = json.loads(edit["original"])

            def guard():
                if (
                    adapter is not self.comments.adapter or self.comments.credentials != credentials
                    or self._character() != character
                ):
                    raise ValueError("编辑期间账号或角色已变化")
                self.operations.assert_allowed(
                    automatic=False, channel="dynamics", account_id=account, character=character,
                )

            actual = await read_dynamic_caption(adapter, identifier, account)
            expected = original | {"text": edit["outgoing"]}
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
                                               before_send=reserve)
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
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dynamic_posts SET content=?,content_hash=?,updated_at=? WHERE id=?",
                        (edit["caption"], _content_hash(edit["caption"]), time.time(), row["id"]),
                    )
                    self.store.db.execute(
                        "UPDATE dynamic_edits SET status='applied',error=NULL WHERE request_id=?", (request_id,),
                    )
            return self.dynamic_edit(request_id, account, character["id"])

    async def evaluate(self, manual=False):
        async with self.evaluation_lock:
            policy = self.policy()
            if not manual and (
                not policy["enabled"]
                or max(policy["next_evaluation_at"] or 0, policy["backoff_until"] or 0) > time.time()
            ):
                return {"status": "skipped", "reason": "运营已关闭或尚未到评估时间"}
            char = self._character()
            generation_identity = runtime_identity(self.runtime, char)
            base = {
                "action": "wait",
                "reason": "暂无获准公开的记忆或已发布互动",
                "content": "",
                "source_ids": [],
                "next_evaluation_seconds": policy["evaluate_interval_seconds"],
            }
            if not manual and self._live_busy():
                base["reason"] = "直播队列繁忙，延后动态评估"
                return self._save_decision(char, manual, base, [])
            sources = self._summary_sources()
            used_ids = self._used_source_ids()
            if not any(source["id"] not in used_ids for source in sources):
                return self._save_decision(char, manual, base, [])
            if not manual:
                try:
                    self._quota()
                except ValueError:
                    base["reason"] = "发布频率限制中，暂不生成动态"
                    return self._save_decision(char, manual, base, [])
            current_time = datetime.now().astimezone().isoformat()
            memory_preference = self._memory_preference()
            recent_posts = [
                {"content": p["content"], "published_at": p["published_at"]}
                for p in self.list_posts("published", 5)
                if p["character_id"] == char["id"] and p["character_version"] == char["version"]
                and not any(
                    s.get("memory_version") or s.get("memory_dependencies")
                    or (s["id"].startswith(("memory:", "experience:", "incident:")) and (
                        not memory_preference[1]["enabled"]
                        or s.get("memory_preference_revision", 0) != memory_preference[1]["revision"]
                    ))
                    for s in p["sources"]
                )
            ]
            messages = dynamic_decision_messages(
                char,
                recent_posts,
                [],
                current_time=current_time,
            )
            messages = identity_messages(messages, generation_identity)
            selected, selection_failed = None, False
            try:
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
                    budget = policy["memory_summary_max_tokens"]

                    def authorized(*, include_enabled=True):
                        return (
                            self._character()["id"] == char["id"]
                            and self._character()["version"] == char["version"]
                            and self._summary_sources() == sources
                            and self._memory_preference() == memory_preference
                            and self._summary_model() == model_identity
                            and runtime_identity(self.runtime, self._character())["identity_version"]
                            == generation_identity["identity_version"]
                            and self.policy()["memory_summary_max_tokens"] == budget
                            and (not include_enabled or manual or self.policy()["enabled"])
                        )

                    by_id = {source["id"]: source for source in sources}
                    public_sources = [source for source in sources if source["type"] != "public_memory"]
                    stories = [source for source in sources if source["type"] == "public_memory"]
                    if stories:
                        summary = await self.memory_summary.build(
                            self.runtime.llm, char, stories, budget, model_identity, authorized=authorized,
                        )
                        public_sources.extend(self.memory_summary.public_sources(summary))
                    used_ids = self._used_source_ids()
                    public_sources = [source for source in public_sources if source["id"] not in used_ids]
                    if not public_sources:
                        base["reason"] = "运营记忆摘要暂无新的可发布摘录"
                        return self._save_decision(char, manual, base, [])
                    if not manual and self._live_busy():
                        base["reason"] = "直播队列繁忙，延后动态评估"
                        return self._save_decision(char, manual, base, [])
                    if not authorized():
                        raise ValueError("运营记忆来源或授权已变化")
                    payload = json.loads(messages[1]["content"])
                    payload["public_sources"] = public_sources
                    messages[1]["content"] = _json(payload)
                    maximum = self.settings.data["llm"].get("operations_max_tokens", 8192)
                    binding = {
                        "task": "generation",
                        "character": {"id": char["id"], "version": char["version"]},
                        "source_sha256": content_hash(public_sources),
                        "self_identity": generation_identity,
                    }

                    async def invoke_model(packet, schema):
                        return await budgeted_json(self.runtime.llm, packet, schema, max_tokens=maximum)

                    async def invoke(packet, *, web_fetch=None):
                        schema = self.decision_schema(public_sources)
                        if web_fetch is not None:
                            return await web_fetch.complete(packet, schema, invoke_model)
                        return await invoke_model(packet, schema)

                    def guard():
                        if not authorized(include_enabled=False):
                            raise ValueError("运营记忆来源或授权已变化")

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
                                context_data={"public_sources": public_sources},
                        allow_web_fetch=True,
                    )
                    selected_sources = [by_id[source_id] for source_id in data["source_ids"]]
                    if (
                        data["action"] == "post" and not comment_card(selected_sources)
                        and self.policy()["use_memes"] and self.memes
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
                        if not self.policy()["use_memes"]:
                            selected = None
                        if not authorized(include_enabled=False):
                            raise ValueError("选图期间运营来源或授权已变化")
                data["next_evaluation_seconds"] = max(
                    data["next_evaluation_seconds"], policy["evaluate_interval_seconds"]
                )
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
                    error = "配图选择失败，保留待审正文；未自动发布"
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
