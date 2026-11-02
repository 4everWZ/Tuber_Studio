"""Comment browsing, bounded drafts and an explicit review/auto-publish policy."""

import asyncio
import csv
import io
import json
import re
import time
from collections import deque
from uuid import uuid4

from .bilibili import BilibiliAdapter, PublishUncertain
from .config import atomic_json
from .media_cache import linked
from .operation_continuations import content_hash
from .operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    generation_view,
    publication_binding,
)
from .operation_text import validate_operation_text
from .operations_control import OperationsControl, canonical_intent
from .providers import public_error
from .publication_errors import PlatformRejected, UnknownPublication

DEFAULT_POLICY = {
    "auto_browse": False,
    "auto_reply": False,
    "watch_videos": [],
    "browse_interval_seconds": 300,
    "max_replies_per_hour": 5,
    "max_replies_per_day": 20,
}
CATEGORIES = {"question", "suggestion", "interaction", "spam", "general"}
THREAD_WORKERS = 4


def category(text):
    if re.search(r"加[微vV]|兼职|代刷|代充|https?://", text) or len(set(text.strip())) <= 1:
        return "spam"
    if re.search(r"建议|希望|应该|改进|投诉|问题是", text):
        return "suggestion"
    if re.search(r"[?？]|怎么|为什么|请问|能不能|如何|多少", text):
        return "question"
    if re.search(r"谢谢|喜欢|可爱|支持|鱼鱼|鲸鱼|大肥鱼", text):
        return "interaction"
    return "general"


class Comments:
    def __init__(self, settings, store, runtime, adapter=None):
        self.settings, self.store, self.runtime = settings, store, runtime
        self.operations = getattr(runtime, "operations", None) or OperationsControl(store)
        runtime.operations = self.operations
        self.credential_path = settings.runtime / "bilibili-credentials.json"
        if linked(self.credential_path):
            raise ValueError("账号凭证文件不能使用链接")
        credentials = (
            json.loads(self.credential_path.read_text(encoding="utf-8"))
            if self.credential_path.exists()
            else {}
        )
        self.credentials = credentials
        if adapter is None:
            from .bilibili_browsing import BrowsingAdapter

            adapter = BrowsingAdapter(credentials)
            self.injected = False
        else:
            self.injected = True
        self.adapter = adapter
        self.queue = asyncio.Queue(maxsize=40)
        self.worker = None
        self.browser = None
        self.policy_changed = asyncio.Event()
        self.publish_lock = asyncio.Lock()
        self.browse_lock = asyncio.Lock()
        self.last_browse_error = None
        self.last_browse_at = None
        self.active_task_id = None
        self.thread_tasks = set()
        self.closed = False
        self.direct_messages = None
        self.task_authorizations = {}
        self.active_automation_epoch = None
        self.verified_account_id = ""
        self.verified_credential = None
        if "account_id" not in {row[1] for row in store.db.execute("PRAGMA table_info(drafts)")}:
            store.execute("ALTER TABLE drafts ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
        if "generation_continuation_id" not in {
            row[1] for row in store.db.execute("PRAGMA table_info(comments)")
        }:
            store.execute("ALTER TABLE comments ADD COLUMN generation_continuation_id TEXT")

    async def open(self):
        self.worker = asyncio.create_task(self._worker())
        self.browser = asyncio.create_task(self._browse_loop())

    async def close(self):
        self.closed = True
        for task in (self.worker, self.browser):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.worker, self.browser) if t), return_exceptions=True)
        while not self.queue.empty():
            task_id, *_ = self.queue.get_nowait()
            self.store.execute(
                "UPDATE comment_tasks SET status='cancelled',error='服务停止',updated_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self.queue.task_done()
        await self.adapter.close()

    def policy(self):
        base = DEFAULT_POLICY | self.store.get("comment_policy", {})
        data = self.operations.effective("comments", base)
        return data | {
            "base": base,
            "automation_override": self.operations.policy()["enabled"],
            "emergency": self.operations.policy()["emergency"],
            "credentials_configured": bool(self.credentials.get("sessdata") and self.credentials.get("csrf")),
            "last_browse_error": self.last_browse_error,
            "last_browse_at": self.last_browse_at,
        }

    async def set_policy(self, patch):
        allowed = set(DEFAULT_POLICY) | {"sessdata", "csrf"}
        if not set(patch) <= allowed:
            raise ValueError("未知评论策略字段")
        current = DEFAULT_POLICY | self.store.get("comment_policy", {})
        data = current | {k: v for k, v in patch.items() if k in DEFAULT_POLICY}
        if not isinstance(data["auto_browse"], bool) or not isinstance(data["auto_reply"], bool):
            raise ValueError("开关必须为布尔值")
        for field, minimum, maximum in (
            ("browse_interval_seconds", 60, 86400),
            ("max_replies_per_hour", 1, 30),
            ("max_replies_per_day", 1, 200),
        ):
            if not isinstance(data[field], int) or not minimum <= data[field] <= maximum:
                raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
        if not isinstance(data["watch_videos"], list) or len(data["watch_videos"]) > 20:
            raise ValueError("关注视频必须是最多20个BV号的列表")
        if any(
            not isinstance(v, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", v) for v in data["watch_videos"]
        ):
            raise ValueError("关注视频请使用完整BV号")
        credential_before = dict(self.credentials)
        credential = dict(credential_before)
        for name in ("sessdata", "csrf"):
            if name in patch and patch[name]:
                value = patch[name]
                if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\r\n;]", value):
                    raise ValueError("凭证格式无效")
                credential[name] = value
        if data["auto_reply"] and not (credential.get("sessdata") and credential.get("csrf")):
            raise ValueError("自动回复需要先配置账号 SESSDATA 与 bili_jct；当前仍为人工审核")
        if credential != self.credentials:
            await self.replace_credentials(credential, expected=credential_before)
        self.store.set("comment_policy", data)
        self.policy_changed.set()
        return self.policy()

    async def replace_credentials(self, credential, *, expected=None):
        """Settings and the legacy policy route share one private credential store."""
        if not isinstance(credential, dict) or set(credential) - {"sessdata", "csrf"}:
            raise ValueError("未知账号凭证字段")
        for value in credential.values():
            if not isinstance(value, str) or not 0 < len(value) <= 2000 or re.search(r"[\r\n;]", value):
                raise ValueError("凭证格式无效")
        from .bilibili_browsing import BrowsingAdapter

        async with self.publish_lock:
            if expected is not None and self.credentials != expected:
                raise ValueError("账号凭证已被另一项设置更新，请刷新后重新保存")
            if credential == self.credentials:
                return
            replacement = BrowsingAdapter(credential)
            try:
                atomic_json(self.credential_path, credential, private=True)
            except BaseException:
                await replacement.close()
                raise
            old = self.adapter
            self.credentials, self.adapter, self.injected = dict(credential), replacement, False
            self.verified_account_id, self.verified_credential = "", None
            self.policy_changed.set()
            await old.close()

    async def account_id(self):
        adapter = self.adapter
        credential = dict(self.credentials)
        if hasattr(adapter, "self_uid"):
            account_id = str(await adapter.self_uid())
            if self.adapter is not adapter or self.credentials != credential:
                raise ValueError("核验期间账号已变更，请重试")
            self.verified_account_id = account_id
            self.verified_credential = credential
            return account_id
        if self.injected:
            return "injected-test-account"
        raise ValueError("平台适配器不能核验当前账号")

    def display_account_id(self):
        return self.verified_account_id if self.verified_credential == self.credentials else ""

    def prepare_import(self, video_id, items=None, csv_text=None, field_mapping=None):
        if not isinstance(video_id, str) or not re.fullmatch(
            r"(?:BV[0-9A-Za-z]{10}|av[0-9]+|import-[\w-]{1,60})", video_id
        ):
            raise ValueError("视频ID应为BV号、av号或import-开头的导入批次名")
        if csv_text is not None:
            if not isinstance(csv_text, str) or len(csv_text) > 2_000_000:
                raise ValueError("CSV大小超限")
            reader = csv.DictReader(io.StringIO(csv_text))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV表头为空或字段重复")
            items = list(reader)
        if not isinstance(items, list) or not 1 <= len(items) <= 1000:
            raise ValueError("每批导入1到1000条评论")
        normalized = []
        fields = {
            "comment_id",
            "platform_user_id",
            "display_name",
            "content",
            "like_count",
            "created_at",
            "thread_id",
            "parent_comment_id",
        }
        mapping = {} if field_mapping is None else field_mapping
        if (
            not isinstance(mapping, dict)
            or not set(mapping) <= fields
            or any(not isinstance(v, str) or not 1 <= len(v) <= 100 for v in mapping.values())
        ):
            raise ValueError("字段映射必须为目标字段到源列名的对象")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("评论必须是对象")
            if None in item:
                raise ValueError("CSV行字段数与表头不一致")
            item = {field: item.get(mapping.get(field, field)) for field in fields}
            cid, content = str(item.get("comment_id") or ""), item.get("content")
            if not cid or len(cid) > 100 or not isinstance(content, str) or not 1 <= len(content) <= 4000:
                raise ValueError("每条评论需要comment_id与1到4000字content")
            thread_id = self._thread_identifier(item.get("thread_id"), cid)
            parent_id = self._thread_identifier(item.get("parent_comment_id"))
            uid = str(item["platform_user_id"])[:100] if item["platform_user_id"] is not None else None
            uid = uid or None
            name = str(item.get("display_name") or "匿名观众")[:80]
            likes = max(0, int(item.get("like_count", 0) or 0))
            created = str(item.get("created_at") or "")[:80]
            normalized.append(
                {
                    "video_id": video_id,
                    "comment_id": cid,
                    "thread_id": thread_id,
                    "parent_comment_id": parent_id,
                    "platform_user_id": uid,
                    "display_name": name,
                    "content": content,
                    "category": category(content),
                    "like_count": likes,
                    "created_at": created,
                }
            )
        return normalized

    def import_items(self, video_id, items=None, csv_text=None, field_mapping=None):
        prepared = self.prepare_import(video_id, items, csv_text, field_mapping)
        normalized = [
            (
                r["video_id"],
                r["comment_id"],
                r["thread_id"],
                r["parent_comment_id"],
                r["platform_user_id"],
                r["display_name"],
                r["content"],
                r["category"],
                r["like_count"],
                r["created_at"],
                time.time(),
            )
            for r in prepared
        ]
        before = self.store.one("SELECT COUNT(*) AS n FROM comments")["n"]
        with self.store.db:
            self.store.db.executemany(
                "INSERT OR IGNORE INTO comments(video_id,comment_id,thread_id,parent_comment_id,platform_user_id,"
                "display_name,content,category,like_count,created_at,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                normalized,
            )
        after = self.store.one("SELECT COUNT(*) AS n FROM comments")["n"]
        return {
            "imported": after - before,
            "duplicates": len(prepared) - (after - before),
            "video_id": video_id,
        }

    async def fetch(self, video, max_pages=1):
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 5:
            raise ValueError("每次读取1到5页")
        async with self.browse_lock:
            data = await self.adapter.fetch(video, max_pages)
        if not data["items"]:
            return {"imported": 0, "duplicates": 0, "video_id": data["video_id"], "detail": "未读到评论"}
        return self.import_items(data["video_id"], data["items"]) | {"title": data.get("title")}

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
    def context_scope(video_id, thread_id, platform="bilibili"):
        if (
            not isinstance(video_id, str)
            or not 1 <= len(video_id) <= 100
            or not isinstance(thread_id, str)
            or not 1 <= len(thread_id) <= 100
            or not isinstance(platform, str)
            or not re.fullmatch(r"[a-z0-9_-]{1,32}", platform)
        ):
            raise ValueError("评论上下文需要有效的平台、视频和楼ID")
        return "comments:" + json.dumps(
            [platform, video_id, thread_id], ensure_ascii=False, separators=(",", ":")
        )

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
            + " ORDER BY c.id DESC LIMIT ?",
            (*args, min(limit, 1000)),
        )
        for row in rows:
            row["continuation"] = generation_view(
                self.runtime, row.get("generation_continuation_id"), "comments", row["id"]
            )
            row["relationship"] = self.runtime.relationships.view(
                self.runtime.character["id"],
                row["platform"],
                row["platform_user_id"],
                account_id=self.display_account_id(),
            )
            row["draft"] = self.store.one("SELECT * FROM drafts WHERE comment_id=?", (row["id"],))
            if row["draft"] and row["draft"].get("answer_check"):
                row["draft"]["answer_check"] = json.loads(row["draft"]["answer_check"])
            if row["draft"] and getattr(self.runtime, "continuations", None):
                row["draft"]["continuation"] = self.runtime.continuations.dto("comments", row["draft"]["id"])
        return rows

    def analyze(self, video_id=None):
        where, args = (" WHERE video_id=?", (video_id,)) if video_id else ("", ())
        rows = self.store.all("SELECT id,content,category,category_source FROM comments" + where, args)
        counts = {c: 0 for c in CATEGORIES}
        with self.store.db:
            for row in rows:
                value = row["category"] if row["category_source"] == "operator" else category(row["content"])
                counts[value] += 1
                if row["category_source"] != "operator":
                    self.store.db.execute("UPDATE comments SET category=? WHERE id=?", (value, row["id"]))
        return counts

    def set_category(self, comment_id, value):
        if (
            not isinstance(value, str)
            or value not in CATEGORIES
            or not self.store.one("SELECT id FROM comments WHERE id=?", (comment_id,))
        ):
            raise ValueError("评论不存在或分类无效")
        self.store.execute(
            "UPDATE comments SET category=?,category_source='operator' WHERE id=?", (value, comment_id)
        )
        return self.store.one("SELECT * FROM comments WHERE id=?", (comment_id,))

    def enqueue(self, ids, automatic=False):
        if self.closed:
            raise ValueError("评论任务服务已停止")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(i, int) for i in ids):
            raise ValueError("选择1到1000条评论")
        ids = list(dict.fromkeys(ids))
        if self.queue.full():
            raise ValueError("评论任务队列已满，请等待当前批次")
        if any(self.store.one("SELECT id FROM comments WHERE id=?", (i,)) is None for i in ids):
            raise ValueError("所选评论不存在")
        task_id, character = str(uuid4()), self.runtime.character
        automatic = automatic and self.policy()["auto_reply"]
        self.task_authorizations[task_id] = (
            self.operations.policy()["epoch"] if automatic and self.operations.policy()["enabled"] else None
        )
        self.store.execute(
            "INSERT INTO comment_tasks(id,status,total,character_id,created_at,updated_at) "
            "VALUES(?,'pending',?,?,?,?)",
            (task_id, len(ids), character["id"], time.time(), time.time()),
        )
        self.queue.put_nowait((task_id, ids, character, automatic))
        return {"task_id": task_id, "status": "pending", "total": len(ids)}

    async def _worker(self):
        while True:
            task_id, ids, character, automatic = await self.queue.get()
            self.active_task_id = task_id
            self.active_automation_epoch = self.task_authorizations.pop(task_id, None)
            children = []
            self.store.execute(
                "UPDATE comment_tasks SET status='running',updated_at=? WHERE id=?", (time.time(), task_id)
            )
            try:
                threads = {}
                for cid in ids:
                    row = self.store.one("SELECT * FROM comments WHERE id=?", (cid,))
                    if row is None:
                        self._task_progress(task_id, "来源评论不存在")
                        continue
                    scope = self.context_scope(row["video_id"], row["thread_id"], row["platform"])
                    threads.setdefault(scope, []).append(row)
                pending = deque(threads.values())
                # Only four workflows exist at a time. A thread's comments remain
                # ordered, and batches themselves preserve queue order.
                async with asyncio.TaskGroup() as group:
                    for _ in range(min(THREAD_WORKERS, len(pending))):
                        child = group.create_task(self._thread_worker(task_id, pending, character, automatic))
                        children.append(child)
                        self.thread_tasks.add(child)
                self.store.execute(
                    "UPDATE comment_tasks SET status=CASE WHEN errors>0 THEN 'partial' ELSE "
                    "'completed' END,updated_at=? WHERE id=?",
                    (time.time(), task_id),
                )
            except asyncio.CancelledError:
                # TaskGroup has cancelled and collected every child, including
                # one-shot publish reconciliation, before reaching this branch.
                self.store.execute(
                    "UPDATE comment_tasks SET status='cancelled',error='服务停止',updated_at=? WHERE id=?",
                    (time.time(), task_id),
                )
                raise
            except Exception as error:
                self.store.execute(
                    "UPDATE comment_tasks SET status='failed',error=?,updated_at=? WHERE id=?",
                    (public_error(error), time.time(), task_id),
                )
            finally:
                self.thread_tasks.difference_update(children)
                self.active_task_id = None
                self.active_automation_epoch = None
                self.queue.task_done()

    async def _thread_worker(self, task_id, pending, character, automatic):
        while pending:
            rows = pending.popleft()
            await self._process_thread(task_id, rows, character, automatic)

    async def _process_thread(self, task_id, rows, character, automatic):
        for row in rows:
            try:
                await self._process_comment(row, character, automatic)
                self._task_progress(task_id)
            except Exception as error:
                continuation = getattr(error, "continuation", None)
                view = (
                    generation_view(self.runtime, continuation["id"], "comments", row["id"])
                    if continuation
                    else None
                )
                message = (
                    "渠道生成原文在私信工作区查看"
                    if view and view.get("output_visibility") in {"private", "unresolved"}
                    else public_error(error)
                )
                self._task_progress(task_id, message)

    def _live_pending(self):
        return self.runtime.state in {"live", "paused"} and (self.runtime.queue or self.runtime.current_id)

    @staticmethod
    def _comment_event(row):
        return {
            "event_id": f"comment:{row['id']}",
            "text": row["content"],
            "display_name": row["display_name"],
            "platform_user_id": row["platform_user_id"],
            "platform": row["platform"],
            "video_id": row["video_id"],
            "thread_id": row["thread_id"],
            "parent_comment_id": row["parent_comment_id"],
            "platform_comment_id": row["comment_id"],
            "source_created_at": row["created_at"],
            "source_imported_at": row["imported_at"],
        }

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

    def _check_sources(self, rows, video_id, thread_id, platform):
        current = {row["id"]: row for row in self._thread_sources(video_id, thread_id, platform)}
        if any(row["id"] not in current or current[row["id"]]["content"] != row["content"] for row in rows):
            raise ValueError("楼原文在重建期间已清理或变化，未安装上下文")

    async def rebuild_context(self, video_id, thread_id, character=None, *, platform="bilibili"):
        """Caller owns generation_lock; rebuild only retained originals, never old drafts."""
        character = character or self.runtime.character
        scope = self.context_scope(video_id, thread_id, platform)
        context = self.runtime.context
        previous = context.existing(scope, character)
        if previous and json.loads(previous["history"]):
            raise ValueError("当前楼已有非空上下文，请先清除后再重建")
        rows = self._thread_sources(video_id, thread_id, platform)
        if not rows:
            raise ValueError("当前楼没有保留的原始评论，不能重建；不会自动联网取回")
        candidate = context.source_candidate(scope, character, [self._comment_event(row) for row in rows])
        try:
            messages = await candidate.prepare(scope, character, None, mode="comment_draft")
            prefilled = await self.runtime.llm.prefill(messages)
            self._check_sources(rows, video_id, thread_id, platform)
            context.install_candidate(scope, character, candidate)
            status = context.status(scope, character)
            return {
                "rebuilt": True,
                "prefilled": prefilled,
                "source_count": len(rows),
                "scope": scope,
                "input_tokens": status["active_tokens"],
                "compacted": bool(status["compact_count"]),
                "message": "已从本楼原文重建并完成真实模型prefill"
                if prefilled
                else "已恢复本楼原文；演示模式没有真实模型prefill",
            }
        finally:
            candidate.store.close()

    async def _process_comment(self, row, character, automatic):
        cid = row["id"]
        row = self.store.one("SELECT * FROM comments WHERE id=?", (cid,))
        if row is None:
            raise ValueError("来源评论不存在")
        browsing = getattr(self.runtime, "browsing", None)
        if browsing and browsing.has_intent(row, character):
            return
        if automatic and self.active_automation_epoch is not None:
            control = self.operations.policy()
            self.operations.assert_allowed(
                automatic=True,
                channel="comments",
                account_id=control["account_id"],
                character=character,
                epoch=self.active_automation_epoch,
            )
        if row["category"] == "spam" or self.store.one("SELECT id FROM drafts WHERE comment_id=?", (cid,)):
            return
        event = self._comment_event(row)
        account_id = await self.account_id() if self.policy()["credentials_configured"] else ""
        event["account_id"] = account_id
        if automatic and browsing:
            event["video_evidence"] = await browsing.comment_evidence(row, character)
        scope = self.context_scope(row["video_id"], row["thread_id"], row["platform"])
        while True:
            while self._live_pending():
                await asyncio.sleep(0.2)
            async with self.runtime.generation_lock:
                if self._live_pending():
                    continue
                # Recheck after waiting for the GPU: another request may already
                # have produced a draft. Reviewed/published drafts are immutable here.
                if self.store.one("SELECT id FROM drafts WHERE comment_id=?", (cid,)):
                    return
                if browsing and browsing.has_intent(row, character):
                    return
                context = self.runtime.context
                previous = context.existing(scope, character)
                sources = (
                    self._thread_sources(row["video_id"], row["thread_id"], row["platform"], cid)
                    if (not previous or not json.loads(previous["history"]))
                    else []
                )
                candidate = (
                    context.source_candidate(
                        scope, character, [self._comment_event(source) for source in sources]
                    )
                    if sources
                    else None
                )
                try:
                    relationship = self.runtime.relationships.observe(
                        character["id"],
                        row["platform"],
                        row["platform_user_id"],
                        row["display_name"],
                        canonical_intent(
                            row["platform"], row["video_id"], row["comment_id"], character["id"]
                        ),
                        row["content"],
                        account_id=account_id,
                    )
                    handoff = (
                        await self.direct_messages.route_comment(
                            row,
                            character,
                            relationship,
                            candidate or context,
                            scope,
                            event,
                            automatic=automatic,
                        )
                        if self.direct_messages
                        else None
                    )
                    if handoff:
                        if candidate:
                            self._check_sources(sources, row["video_id"], row["thread_id"], row["platform"])
                            context.install_candidate(scope, character, candidate)
                        self.store.execute(
                            "INSERT OR IGNORE INTO drafts(comment_id,text,status,character_id,character_version,model,input_summary,created_at,updated_at) VALUES(?,?,'private_handoff',?,?,?,?,?,?)",
                            (
                                cid,
                                f"已转私信草稿 #{handoff}",
                                character["id"],
                                character["version"],
                                self.settings.data["llm"].get("model", "demo"),
                                json.dumps(event, ensure_ascii=False),
                                time.time(),
                                time.time(),
                            ),
                        )
                        self.store.execute(
                            "UPDATE drafts SET account_id=? WHERE comment_id=?", (account_id, cid)
                        )
                        return
                    reply, check = await self.runtime.generate_reply(
                        scope,
                        character,
                        event,
                        relationship=relationship,
                        mode="comment_draft",
                        **({"context": candidate} if candidate else {}),
                    )
                    if candidate:
                        self._check_sources(sources, row["video_id"], row["thread_id"], row["platform"])
                        candidate.record(
                            scope,
                            character,
                            event,
                            reply.spoken_text,
                            "comment_draft",
                            memory_kind=check.get("memory_kind"),
                        )
                    # The staged history, new draft and its new source index are
                    # installed together, with no await inside the SQL transaction.
                    with self.store.db:
                        cursor = self.store.db.execute(
                            "INSERT OR IGNORE INTO drafts(comment_id,text,status,character_id,"
                            "character_version,model,input_summary,answer_check,created_at,updated_at) "
                            "VALUES(?,?,'pending_review',?,?,?,?,?,?,?)",
                            (
                                cid,
                                reply.spoken_text,
                                character["id"],
                                character["version"],
                                self.settings.data["llm"].get("model", "demo"),
                                json.dumps(event, ensure_ascii=False),
                                json.dumps(check, ensure_ascii=False),
                                time.time(),
                                time.time(),
                            ),
                        )
                        if not cursor.rowcount:
                            return
                        draft_id = cursor.lastrowid
                        self.store.db.execute(
                            "UPDATE drafts SET account_id=? WHERE id=?", (account_id, draft_id)
                        )
                        if candidate:
                            context.install_candidate(scope, character, candidate)
                    if not candidate:
                        context.record(
                            scope,
                            character,
                            event,
                            reply.spoken_text,
                            "comment_draft",
                            memory_kind=check.get("memory_kind"),
                        )
                except Exception as error:
                    if getattr(error, "continuation", None):
                        self.store.execute(
                            "UPDATE comments SET generation_continuation_id=? WHERE id=?",
                            (error.continuation["id"], cid),
                        )
                    raise
                finally:
                    if candidate:
                        candidate.store.close()
                break
        # Independent threads may prepare/publish concurrently; all model calls
        # above stay serialized, and publish() owns its one-shot write lock.
        if automatic and self.policy()["auto_reply"] and not check.get("output", {}).get("needs_review"):
            await self.publish(draft_id, automatic=True, authorization_epoch=self.active_automation_epoch)

    def _task_progress(self, task_id, error=None):
        self.store.execute(
            "UPDATE comment_tasks SET done=done+1,errors=errors+?,error=COALESCE(?,error),updated_at=? "
            "WHERE id=?",
            (int(error is not None), error, time.time(), task_id),
        )

    def review(self, draft_id, action, text=None):
        row = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row:
            raise ValueError("草稿不存在")
        if row["status"] in {"published", "publishing", "publish_uncertain", "private_handoff"}:
            raise ValueError("已发布或结果不确定的回复不能覆盖；请核对平台")
        if action not in {"approve", "reject"}:
            raise ValueError("未知审核动作")
        text = text if text is not None else row["text"]
        text = validate_operation_text(text, label="回复")
        status = "approved" if action == "approve" else "rejected"
        self.store.execute(
            "UPDATE drafts SET text=?,status=?,error=NULL,updated_at=? WHERE id=?",
            (text.strip(), status, time.time(), draft_id),
        )
        return self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def copy(self, draft_id):
        row = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row or row["status"] not in {"approved", "copied"}:
            raise ValueError("先审核通过再复制")
        self.store.execute(
            "UPDATE drafts SET status='copied',updated_at=? WHERE id=?", (time.time(), draft_id)
        )
        return {"text": row["text"], "status": "copied"}

    def _publication_binding(self, row, account_id, character):
        return publication_binding(
            "comments",
            account_id,
            character,
            canonical_intent("bilibili", row["video_id"], row["platform_comment_id"], character["id"]),
            {
                "platform": "bilibili",
                "resource_type": "video",
                "resource_id": row["video_id"],
                "root": row["thread_id"],
                "parent": row["platform_comment_id"],
                "recipient_uid": row["platform_user_id"],
            },
            {
                "kind": "comment",
                "local_id": row["comment_id"],
                "platform_id": row["platform_comment_id"],
                "content_sha256": content_hash(row["content"]),
            },
        )

    def _draft_source(self, draft_id):
        return self.store.one(
            "SELECT d.*,c.video_id,c.thread_id,c.platform_user_id,c.content,c.comment_id AS platform_comment_id FROM drafts d "
            "JOIN comments c ON c.id=d.comment_id WHERE d.id=?",
            (draft_id,),
        )

    async def _publication_context(self, draft_id, automatic, epoch):
        row = self._draft_source(draft_id)
        if not row:
            raise ValueError("草稿不存在")
        character = self.store.character(row["character_id"], row["character_version"])
        account = self.display_account_id() or await self.account_id()
        binding = self._publication_binding(row, account, character)
        adapter, credential = self.adapter, dict(self.credentials)

        def guard():
            fresh = self._draft_source(draft_id)
            if self.closed:
                raise asyncio.CancelledError
            if (
                not fresh
                or any(
                    fresh[key] != row[key]
                    for key in ("status", "text", "account_id", "character_id", "character_version")
                )
                or self._publication_binding(fresh, account, character) != binding
            ):
                raise ValueError("恢复期间草稿、角色或原始来源已变化")
            if (
                self.adapter is not adapter
                or credential != self.credentials
                or (automatic and not self.policy()["auto_reply"])
            ):
                raise ValueError("恢复期间账号或授权已变化")
            self.operations.assert_allowed(
                automatic=automatic, channel="comments", account_id=account, character=character, epoch=epoch
            )

        summary = json.loads(row["input_summary"] or "{}")
        return PublicationContext(
            binding,
            row["text"],
            character,
            {
                "source": {"platform": "bilibili", "video_id": row["video_id"], "comment": row["content"]},
                "read_context": summary,
            },
            guard,
            on_defer=lambda prepared, state: self._defer_revision(draft_id, prepared, state),
            manual_approved=row["status"] in {"approved", "copied"},
        )

    def _apply_revision(self, draft_id, prepared, text):
        prepared.guard()
        cursor = self.store.execute(
            "UPDATE drafts SET text=?,status='pending_review',error=NULL,updated_at=? WHERE id=? AND text=? AND status IN('approved','copied','pending_review')",
            (text, time.time(), draft_id, prepared.text),
        )
        if cursor.rowcount != 1:
            raise ValueError("草稿已被更新，修订结果保留在恢复记录")

    def _defer_revision(self, draft_id, prepared, state):
        prepared.guard()
        self.store.execute(
            "UPDATE drafts SET status=?,updated_at=? WHERE id=?",
            ("rejected" if state == "abandoned" else "pending_review", time.time(), draft_id),
        )

    async def resume_continuation(self, draft_id, continuation_id):
        if str(draft_id).startswith("generation:"):
            cid = str(draft_id).split(":", 1)[1]
            source = self.store.one("SELECT * FROM comments WHERE id=?", (cid,))
            if not source or source.get("generation_continuation_id") != continuation_id:
                raise ValueError("生成恢复任务与评论不匹配")
            row = self.runtime.continuations.get(continuation_id)
            character = self.runtime.character
            event = self._comment_event(source)
            original = row["binding"]["source"]
            current_account = await self.account_id() if self.policy()["credentials_configured"] else ""
            if str(original.get("account_id", "") or "") != str(current_account):
                raise ValueError("生成检查点的原账号已变化")
            if row["binding"]["character"] != {"id": character["id"], "version": character["version"]} or any(
                event.get(key) != original.get(key)
                for key in (
                    "event_id",
                    "text",
                    "platform_user_id",
                    "video_id",
                    "thread_id",
                    "platform_comment_id",
                )
            ):
                raise ValueError("原始评论或角色已变化，请重新准备")
            value = self.enqueue([source["id"]], automatic=False)
            return {
                "status": "queued",
                **value,
                "continuation": generation_view(self.runtime, continuation_id, "comments", source["id"]),
            }
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)
        return await feedback.resume(
            "comments",
            draft_id,
            continuation_id,
            prepare=lambda: self._publication_context(draft_id, False, None),
            apply_revision=lambda prepared, text: self._apply_revision(draft_id, prepared, text),
            reconcile=lambda token, prepared: self._reconcile(draft_id),
        )

    async def _reconcile(self, draft_id):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        async def read(row, attempt):
            if not hasattr(self.adapter, "reconcile_publication"):
                return {"status": "unsupported"}
            try:
                account = await self.account_id()
            except ValueError:
                return {"status": "check_failed", "reason": "account_unavailable"}
            if account != row["binding"]["account_id"]:
                return {"status": "check_failed", "reason": "account_changed"}
            return await self.adapter.reconcile_publication(
                row["binding"]["target"],
                author_uid=account,
                text=row["feedback"]["sent_payload"].get("text", ""),
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )

        def commit(receipt, verification):
            self.store.execute(
                "UPDATE drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                (json.dumps(receipt, ensure_ascii=False), time.time(), draft_id),
            )
            self.store.execute(
                "UPDATE publish_attempts SET status='published',receipt=? WHERE id=(SELECT MAX(id) FROM publish_attempts WHERE draft_id=?)",
                (json.dumps(receipt, ensure_ascii=False), draft_id),
            )

        return await feedback.reconcile("comments", draft_id, read, commit)

    async def publish(self, draft_id, automatic=False, *, authorization_epoch=None):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        return await feedback.submit(
            "comments",
            draft_id,
            prepare=lambda: self._publication_context(draft_id, automatic, authorization_epoch),
            send=lambda ticket, binding, body_hash: self._publish_once(
                draft_id,
                automatic,
                authorization_epoch=authorization_epoch,
                revision_ticket=ticket,
                binding=binding,
                body_hash=body_hash,
            ),
            apply_revision=lambda prepared, text: self._apply_revision(draft_id, prepared, text),
            automatic=automatic,
            epoch=authorization_epoch,
            reconcile=lambda token, prepared: self._reconcile(draft_id),
        )

    async def _publish_once(
        self,
        draft_id,
        automatic=False,
        *,
        authorization_epoch=None,
        revision_ticket=None,
        binding=None,
        body_hash=None,
    ):
        async with self.publish_lock:
            policy = self.policy()
            if automatic and not policy["auto_reply"]:
                raise ValueError("自动回复已停止，草稿保留待审核")
            if not policy["credentials_configured"]:
                raise ValueError("未配置B站账号写入凭证；草稿可人工审核复制")
            row = self.store.one(
                "SELECT d.*,c.video_id,c.thread_id,c.platform_user_id,c.content,c.comment_id AS platform_comment_id FROM drafts d "
                "JOIN comments c ON c.id=d.comment_id WHERE d.id=?",
                (draft_id,),
            )
            allowed = {"pending_review"} if automatic else {"approved", "copied"}
            if not row or row["status"] not in allowed:
                raise ValueError("当前草稿状态不能发布，或已发布／结果未确认")
            for seconds, cap in (
                (3600, policy["max_replies_per_hour"]),
                (86400, policy["max_replies_per_day"]),
            ):
                count = self.store.one(
                    "SELECT COUNT(*) AS n FROM publish_attempts WHERE started_at>?", (time.time() - seconds,)
                )["n"]
                if count >= cap:
                    raise ValueError("达到已配置回复频率上限，保留草稿")
            previous = row["status"]
            account_id = await self.account_id()
            if row["account_id"] and row["account_id"] != account_id:
                raise ValueError("该草稿属于其他账号，不能由当前账号发送")
            character = self.store.character(row["character_id"], row["character_version"])
            fresh = self._draft_source(draft_id)
            if not fresh or any(
                fresh[key] != row[key]
                for key in (
                    "status",
                    "text",
                    "account_id",
                    "character_id",
                    "character_version",
                    "content",
                    "platform_comment_id",
                    "platform_user_id",
                    "thread_id",
                    "video_id",
                )
            ):
                raise ValueError("发送准备期间草稿或来源已变化")
            actual_binding = self._publication_binding(row, account_id, character)
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash({"text": row["text"]})
            ):
                raise ValueError("发送前原始绑定或正文已变化")
            adapter, credential = self.adapter, dict(self.credentials)
            token = None

            def before_send():
                nonlocal token
                fresh = self.store.one("SELECT * FROM comments WHERE id=?", (row["comment_id"],))
                if (
                    self.adapter is not adapter
                    or credential != self.credentials
                    or not fresh
                    or fresh["content"] != row["content"]
                    or fresh["comment_id"] != row["platform_comment_id"]
                    or fresh["platform_user_id"] != row["platform_user_id"]
                    or (automatic and not self.policy()["auto_reply"])
                ):
                    raise ValueError("发送前账号、来源或授权已变化，保留草稿")
                browsing = getattr(self.runtime, "browsing", None)
                if automatic and browsing:
                    current_draft = self.store.one("SELECT input_summary FROM drafts WHERE id=?", (draft_id,))
                    browsing.assert_comment_evidence(json.loads(current_draft["input_summary"] or "{}"))
                    if self.operations.policy()["enabled"] and not browsing.semantic_ready():
                        raise ValueError("真实模型语义验收未通过，保留草稿待审核")
                self.operations.assert_allowed(
                    automatic=automatic,
                    channel="comments",
                    account_id=account_id,
                    character=character,
                    epoch=authorization_epoch,
                )
                token = self.operations.reserve(
                    account_id,
                    character,
                    canonical_intent(
                        "bilibili", row["video_id"], row["platform_comment_id"], character["id"]
                    ),
                    "comments",
                    recipient=row["platform_user_id"] or "",
                    automatic=automatic,
                    limits={
                        "max_per_hour": policy["max_replies_per_hour"],
                        "max_per_day": policy["max_replies_per_day"],
                    },
                    epoch=authorization_epoch,
                    binding=actual_binding,
                    content_hash=content_hash({"text": row["text"]}),
                    revision_ticket=revision_ticket,
                )

            attempt = self.store.execute(
                "INSERT INTO publish_attempts(draft_id,started_at,status) VALUES(?,?,'sending')",
                (draft_id, time.time()),
            ).lastrowid
            self.store.execute(
                "UPDATE drafts SET status='publishing',updated_at=? WHERE id=?", (time.time(), draft_id)
            )
            try:
                options = (
                    {"thread_id": row["thread_id"]} if row["thread_id"] != row["platform_comment_id"] else {}
                )
                if isinstance(adapter, BilibiliAdapter):
                    options["before_send"] = before_send
                else:
                    before_send()
                receipt = await self.adapter.publish(
                    row["video_id"], row["platform_comment_id"], row["text"], **options
                )
                if not isinstance(receipt, dict) or not re.fullmatch(
                    r"[1-9][0-9]{0,19}", str(receipt.get("rpid", ""))
                ):
                    raise PublishUncertain("平台没有返回有效评论ID")
            except (PublishUncertain, asyncio.CancelledError) as error:
                if token is None and isinstance(error, asyncio.CancelledError):
                    self.store.execute(
                        "UPDATE drafts SET status=?,updated_at=? WHERE id=?",
                        (previous, time.time(), draft_id),
                    )
                    self.store.execute(
                        "UPDATE publish_attempts SET status='failed',error='准备已取消，未提交评论' WHERE id=?",
                        (attempt,),
                    )
                    raise
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "comments", draft_id, token, actual_binding, {"text": row["text"]}, "发布结果不确定"
                    )
                message = "发布结果不确定，请先核对平台，禁止自动重试"
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=?,updated_at=? WHERE id=?",
                    (message, time.time(), draft_id),
                )
                self.store.execute(
                    "UPDATE publish_attempts SET status='uncertain',error=? WHERE id=?", (message, attempt)
                )
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise UnknownPublication(message, token) from None
            except ValueError as error:
                if token:
                    self.operations.finish(token, "rejected")
                message = public_error(error)
                self.store.execute(
                    "UPDATE drafts SET status=?,error=?,updated_at=? WHERE id=?",
                    (previous, message, time.time(), draft_id),
                )
                self.store.execute(
                    "UPDATE publish_attempts SET status='failed',error=? WHERE id=?", (message, attempt)
                )
                if isinstance(error, PlatformRejected):
                    error.write_token = token
                    error.body_hash = content_hash({"text": row["text"]})
                    error.sent_payload = {"text": row["text"]}
                    raise
                raise ValueError(message) from None
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "comments", draft_id, token, actual_binding, {"text": row["text"]}, "发布结果不确定"
                    )
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=? WHERE id=?",
                    ("发布异常，需核对平台；禁止自动重试", draft_id),
                )
                raise UnknownPublication("发布异常，需核对平台；禁止自动重试", token) from None
            # Platform success and local bookkeeping are separate boundaries. A database
            # failure must never restore an approved draft and allow another HTTP write.
            try:
                encoded = json.dumps(receipt, ensure_ascii=False)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                        (encoded, time.time(), draft_id),
                    )
                    self.store.db.execute(
                        "UPDATE publish_attempts SET status='published',receipt=? WHERE id=?",
                        (encoded, attempt),
                    )
                if token:
                    self.operations.finish(token, "published", receipt=receipt)
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "comments",
                        draft_id,
                        token,
                        actual_binding,
                        {"text": row["text"]},
                        "平台成功后本地保存失败",
                    )
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=? WHERE id=?",
                    ("平台已返回成功，本地回执保存失败；请核对平台", draft_id),
                )
                raise UnknownPublication("平台已返回成功，本地回执保存失败；禁止重发", token) from None
            return {"status": "published", "receipt": receipt}

    async def browse_once(self):
        browsing = getattr(self.runtime, "browsing", None)
        if browsing is None:
            raise ValueError("字幕证据浏览服务未初始化，不能自动回复视频")
        result = await browsing.run(automatic=True)
        self.last_browse_at = time.time()
        return result

    async def _browse_loop(self):
        while True:
            policy = self.policy()
            if self.store.get("comment_policy", {}).get("auto_browse", False) and policy["auto_browse"]:
                try:
                    await self.browse_once()
                    self.last_browse_error = None
                except Exception as error:
                    self.last_browse_error = public_error(error)
                    # Failures pause the cycle until the configured interval; no aggressive retries.
            self.policy_changed.clear()
            try:
                await asyncio.wait_for(self.policy_changed.wait(), timeout=policy["browse_interval_seconds"])
            except TimeoutError:
                pass
