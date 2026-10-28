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
from .providers import public_error

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
        self.credential_path = settings.runtime / "bilibili-credentials.json"
        credentials = (
            json.loads(self.credential_path.read_text(encoding="utf-8"))
            if self.credential_path.exists()
            else {}
        )
        self.credentials = credentials
        self.adapter = adapter or BilibiliAdapter(credentials)
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
        data = DEFAULT_POLICY | self.store.get("comment_policy", {})
        return data | {
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
        credential = dict(self.credentials)
        for name in ("sessdata", "csrf"):
            if name in patch and patch[name]:
                value = patch[name]
                if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\r\n;]", value):
                    raise ValueError("凭证格式无效")
                credential[name] = value
        if data["auto_reply"] and not (credential.get("sessdata") and credential.get("csrf")):
            raise ValueError("自动回复需要先配置账号 SESSDATA 与 bili_jct；当前仍为人工审核")
        if credential != self.credentials:
            # Write only runtime secrets; no credentials are ever returned by policy().
            self.credential_path.write_text(json.dumps(credential), encoding="utf-8")
            self.credential_path.chmod(0o600)
            self.credentials = credential
            await self.adapter.close()
            self.adapter = BilibiliAdapter(credential)
        self.store.set("comment_policy", data)
        self.policy_changed.set()
        return self.policy()

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
            row["draft"] = self.store.one("SELECT * FROM drafts WHERE comment_id=?", (row["id"],))
            if row["draft"] and row["draft"].get("answer_check"):
                row["draft"]["answer_check"] = json.loads(row["draft"]["answer_check"])
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
                self._task_progress(task_id, public_error(error))

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
        if row["category"] == "spam" or self.store.one("SELECT id FROM drafts WHERE comment_id=?", (cid,)):
            return
        event = self._comment_event(row)
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
                    reply, check = await self.runtime.generate_reply(
                        scope,
                        character,
                        event,
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
                finally:
                    if candidate:
                        candidate.store.close()
                break
        # Independent threads may prepare/publish concurrently; all model calls
        # above stay serialized, and publish() owns its one-shot write lock.
        if automatic and self.policy()["auto_reply"]:
            await self.publish(draft_id, automatic=True)

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
        if row["status"] in {"published", "publishing", "publish_uncertain"}:
            raise ValueError("已发布或结果不确定的回复不能覆盖；请核对平台")
        if action not in {"approve", "reject"}:
            raise ValueError("未知审核动作")
        text = text if text is not None else row["text"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 500
            or re.search(r"[\x00-\x08]", text)
        ):
            raise ValueError("回复需为1到500字")
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

    async def publish(self, draft_id, automatic=False):
        async with self.publish_lock:
            policy = self.policy()
            if automatic and not policy["auto_reply"]:
                raise ValueError("自动回复已停止，草稿保留待审核")
            if not policy["credentials_configured"]:
                raise ValueError("未配置B站账号写入凭证；草稿可人工审核复制")
            row = self.store.one(
                "SELECT d.*,c.video_id,c.thread_id,c.comment_id AS platform_comment_id FROM drafts d "
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
                receipt = await self.adapter.publish(
                    row["video_id"], row["platform_comment_id"], row["text"], **options
                )
            except (PublishUncertain, asyncio.CancelledError) as error:
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
                raise ValueError(message) from None
            except ValueError as error:
                message = public_error(error)
                self.store.execute(
                    "UPDATE drafts SET status=?,error=?,updated_at=? WHERE id=?",
                    (previous, message, time.time(), draft_id),
                )
                self.store.execute(
                    "UPDATE publish_attempts SET status='failed',error=? WHERE id=?", (message, attempt)
                )
                raise ValueError(message) from None
            except Exception:
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=? WHERE id=?",
                    ("发布异常，需核对平台；禁止自动重试", draft_id),
                )
                raise ValueError("发布异常，需核对平台；禁止自动重试") from None
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
            except Exception:
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=? WHERE id=?",
                    ("平台已返回成功，本地回执保存失败；请核对平台", draft_id),
                )
                raise ValueError("平台已返回成功，本地回执保存失败；禁止重发") from None
            return {"status": "published", "receipt": receipt}

    async def browse_once(self):
        policy = self.policy()
        videos = policy["watch_videos"]
        if not videos:
            videos = [item["bvid"] for item in (await self.adapter.popular())[:3]]
        for video in videos:
            if not self.policy()["auto_browse"]:
                break
            result = await self.fetch(video, 1)
            self.analyze(result["video_id"])
            candidates = self.store.all(
                "SELECT c.id FROM comments c LEFT JOIN drafts d ON d.comment_id=c.id "
                "WHERE c.video_id=? AND c.category IN ('question','interaction','suggestion') AND d.id IS NULL "
                "ORDER BY c.like_count DESC,c.id DESC LIMIT 3",
                (result["video_id"],),
            )
            if candidates:
                self.enqueue([r["id"] for r in candidates], automatic=True)
        self.last_browse_at = time.time()

    async def _browse_loop(self):
        while True:
            policy = self.policy()
            if policy["auto_browse"]:
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
