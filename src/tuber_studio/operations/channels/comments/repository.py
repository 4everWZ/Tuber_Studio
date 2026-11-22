"""SQLite schema migrations, context scope lookup, and comment repository queries."""

import json
import re
import time

from ...kernel.feedback import generation_view


class CommentsRepositoryMixin:
    def _init_storage(self, store):
        if "image_ref" not in {r[1] for r in store.db.execute("PRAGMA table_info(drafts)")}:
            store.execute("ALTER TABLE drafts ADD COLUMN image_ref TEXT")
        if "account_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(drafts)")}:
            store.execute("ALTER TABLE drafts ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
        if "generation_continuation_id" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(comments)")
        }:
            store.execute("ALTER TABLE comments ADD COLUMN generation_continuation_id TEXT")
        if "mentions" not in {row[1] for row in store.db.execute("PRAGMA table_info(comments)")}:
            store.execute("ALTER TABLE comments ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]'")
        if "attachments" not in {row[1] for row in store.db.execute("PRAGMA table_info(comments)")}:
            store.execute("ALTER TABLE comments ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        if "generation_task_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(drafts)")}:
            store.execute("ALTER TABLE drafts ADD COLUMN generation_task_id TEXT")
        if "comment_ids" not in {row[1] for row in store.db.execute("PRAGMA table_info(comment_tasks)")}:
            store.execute("ALTER TABLE comment_tasks ADD COLUMN comment_ids TEXT NOT NULL DEFAULT '[]'")
        store.execute(
            "CREATE INDEX IF NOT EXISTS drafts_generation_task ON drafts(generation_task_id,updated_at)"
        )

    @staticmethod
    def _thread_identifier(value, fallback=None):
        if value is None or value == "" or value == "0" or (type(value) is int and value == 0):
            return fallback
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("楼ID和父评论ID须为1到100字的字符串或整数")
        value = str(value).strip()
        if not value or len(value) > 100:
            raise ValueError("楼ID和父评论ID须为1到100字")
        return value

    @staticmethod
    def context_scope(video_id, thread_id, platform="bilibili", *, account_id=""):
        if (
            not isinstance(video_id, str)
            or not 1 <= len(video_id) <= 100
            or not isinstance(thread_id, str)
            or not 1 <= len(thread_id) <= 100
            or not isinstance(platform, str)
            or not re.fullmatch(r"[a-z0-9_-]{1,32}", platform)
            or not isinstance(account_id, str)
            or len(account_id) > 100
        ):
            raise ValueError("评论上下文需要有效的平台、视频和楼ID")
        return "comments:" + json.dumps(
            [account_id, platform, video_id, thread_id], ensure_ascii=False, separators=(",", ":")
        )

    def account_context_scope(self, video_id, thread_id, platform="bilibili", *, account_id=None):
        return self.context_scope(video_id, thread_id, platform,
                                  account_id=self.account.context_account_id() if account_id is None else str(account_id))

    def list_comments(self, video_id=None, category_filter=None, status=None, limit=200, *, thread_id=None):
        conditions, args = [], []
        if video_id:
            conditions.append("c.video_id=?")
            args.append(video_id)
        if category_filter:
            conditions.append("c.category=?")
            args.append(category_filter)
        if status:
            conditions.append("d.status=?")
            args.append(status)
        if thread_id:
            conditions.append("c.thread_id=?")
            args.append(thread_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.store.all(
            "SELECT c.* FROM comments c LEFT JOIN drafts d ON d.comment_id=c.id"
            + where
            + " ORDER BY (d.id IS NOT NULL) DESC,d.updated_at DESC,c.imported_at DESC,c.id DESC LIMIT ?",
            (*args, min(limit, 1000)),
        )
        account_id = self.account.display_account_id()
        for row in rows:
            row["continuation"] = generation_view(
                self.runtime, row.get("generation_continuation_id"), "comments", row["id"]
            )
            row["relationship"] = self.runtime.relationships.view(
                self.runtime.character["id"],
                row["platform"],
                row["platform_user_id"],
                account_id=account_id,
            )
            row["relationship_account_id"] = account_id
            row["draft"] = self.store.one("SELECT * FROM drafts WHERE comment_id=?", (row["id"],))
            if row["draft"] and row["draft"].get("answer_check"):
                row["draft"]["answer_check"] = json.loads(row["draft"]["answer_check"])
            if row["draft"] and getattr(self.runtime, "continuations", None):
                row["draft"]["continuation"] = self.runtime.continuations.dto("comments", row["draft"]["id"])
        return rows

    def _thread_sources(self, video_id, thread_id, platform, exclude_id=None):
        self.context_scope(video_id, thread_id, platform)
        return [
            row
            for row in self.store.all(
                "SELECT * FROM comments WHERE platform=? AND video_id=? AND thread_id=? ORDER BY id",
                (platform, video_id, thread_id),
            )
            if row["id"] != exclude_id and row["content"].strip()
        ]

    def _draft_source(self, draft_id):
        return self.store.one(
            "SELECT d.*,c.platform,c.video_id,c.thread_id,c.platform_user_id,c.display_name,c.content,c.comment_id AS platform_comment_id FROM drafts d "
            "JOIN comments c ON c.id=d.comment_id WHERE d.id=?",
            (draft_id,),
        )

    def _task_progress(self, task_id, error=None):
        self.store.execute(
            "UPDATE comment_tasks SET done=done+1,errors=errors+?,error=COALESCE(?,error),updated_at=? "
            "WHERE id=?",
            (int(error is not None), error, time.time(), task_id),
        )

    def copy(self, draft_id):
        row = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if row and row.get("image_ref"):
            raise ValueError("配图草稿不能仅以复制文字完成")
        if not row or row["status"] not in {"approved", "copied"}:
            raise ValueError("先审核通过再复制")
        self.store.execute(
            "UPDATE drafts SET status='copied',updated_at=? WHERE id=?", (time.time(), draft_id)
        )
        return {"text": row["text"], "status": "copied"}
