"""SQLite schema migrations, dynamic decision/post state, and quota checks."""

import hashlib
import json
import re
import time
import unicodedata

from ....core.errors import SendLimitReached
from ....core.models import content_hash
from ....persona.memory import recall as recall_memory
from ...kernel.feedback import FeedbackPublication, continuation_view, generation_view
from ..attachments import decision, image_choice, row_ref, uploads
from .comment_card import comment_card
from .materials import ensure_reflections

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


class DynamicsRepositoryMixin:
    def _init_storage(self, store):
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
        if "link_source_ids" not in {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_edits)")}:
            store.execute("ALTER TABLE dynamic_edits ADD COLUMN link_source_ids TEXT")
        edit_cols = {row[1] for row in store.db.execute("PRAGMA table_info(dynamic_edits)")}
        for name, definition in (("include_comment_card", "INTEGER"), ("pictures", "TEXT")):
            if name not in edit_cols:
                store.execute(f"ALTER TABLE dynamic_edits ADD COLUMN {name} {definition}")
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

    def dynamic_edit(self, request_id, account, character_id):
        row = self.store.one(
            "SELECT * FROM dynamic_edits WHERE request_id=? AND account_id=? AND character_id=?",
            (request_id, account, character_id),
        )
        if row is None:
            raise ValueError("未找到当前账号和角色的动态修订")
        row["mentions"] = json.loads(row["mentions"]) if row["mentions"] is not None else None
        row["link_source_ids"] = (
            json.loads(row["link_source_ids"]) if row.get("link_source_ids") is not None else None
        )
        row["include_comment_card"] = bool(row.get("include_comment_card"))
        row["pictures"] = json.loads(row["pictures"]) if row.get("pictures") is not None else None
        if row["status"] == "draft":
            post = self._post(row["post_id"])
            caption = self._edit_caption(post, row["caption"], link_source_ids=row["link_source_ids"])
            row = row | {
                "caption": caption,
                "outgoing": self._outgoing_text(post, caption, link_source_ids=row["link_source_ids"]),
            }
        return row
