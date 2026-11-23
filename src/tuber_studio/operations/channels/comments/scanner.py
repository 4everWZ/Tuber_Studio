"""Comment classification, import, fetching, and bounded thread worker scheduling."""

import asyncio
import csv
import io
import json
import re
import time
from collections import deque
from uuid import uuid4

from ....infra.inference.providers import public_error
from ....infra.platforms.bilibili.comment_materials import without_article_links
from ...kernel.feedback import generation_view

CATEGORIES = {"question", "suggestion", "interaction", "spam", "general"}
THREAD_WORKERS = 4
_LOW_CONTEXT_FILLER = re.compile(
    r"^(?:哈+|呵+|嘻+|嘿+|确实|不错|可以|好[的耶啊吧呀]?|行吧?|懂了|收到|打卡|沙发|前排|支持|厉害|牛[皮逼哇啊]?|强[啊呀]?|草+|绝了?|笑死|离谱|逆天|泪目|难绷|一般|无语|[a-zA-Z0-9]+)$",
    re.I,
)


def low_context_skip_reason(
    text,
    *,
    is_direct_to_self: bool = False,
    mentions=(),
    attachments=(),
    self_uid: str = "",
) -> str | None:
    """Return a concise skip reason for pure @other+emoji or contextless <=4-char fillers."""
    if is_direct_to_self or attachments:
        return None
    uid = str(self_uid or "")
    mention_uids = {str(m.get("uid") or "") for m in (mentions or []) if isinstance(m, dict) and m.get("uid")}
    if uid and uid in mention_uids:
        return None
    raw = str(text or "").strip()
    if not raw:
        return "低语境空评论"
    without_emoji = re.sub(r"\[[^\]\r\n]{1,24}\]", "", raw)
    without_at = re.sub(r"@[^\s@:：,，.。!！?？()（）\[\]]+", "", without_emoji)
    core = "".join(ch for ch in without_at if ch.isalnum())
    if not core:
        return "低语境纯@他人或表情"
    if "?" in raw or "？" in raw:
        return None
    if len(core) <= 4 and (_LOW_CONTEXT_FILLER.fullmatch(core) or len(set(core)) == 1):
        return "低语境短句"
    return None


def category(text, attachments=()):
    message = without_article_links(text, attachments)
    if re.search(r"加[微vV]|兼职|代刷|代充|https?://", message) or (not attachments and len(set(text.strip())) <= 1):
        return "spam"
    if re.search(r"建议|希望|应该|改进|投诉|问题是", text):
        return "suggestion"
    if re.search(r"[?？]|怎么|为什么|请问|能不能|如何|多少", text):
        return "question"
    if re.search(r"谢谢|喜欢|可爱|支持", text):
        return "interaction"
    return "general"


class CommentsScannerMixin:
    def prepare_import(self, video_id, items=None, csv_text=None, field_mapping=None, *, platform_fetch=False):
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
            "mentions",
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
            attachments = (item.get("attachments") or []) if platform_fetch else []
            item = {field: item.get(mapping.get(field, field)) for field in fields}
            cid, content = str(item.get("comment_id") or ""), item.get("content")
            if (not cid or len(cid) > 100 or not isinstance(content, str) or len(content) > 4000
                    or not content and not attachments):
                raise ValueError("每条评论需要comment_id与1到4000字content")
            thread_id = self._thread_identifier(item.get("thread_id"), cid)
            parent_id = self._thread_identifier(item.get("parent_comment_id"))
            uid = str(item["platform_user_id"])[:100] if item["platform_user_id"] is not None else None
            uid = uid or None
            name = str(item.get("display_name") or "匿名观众")[:80]
            likes = max(0, int(item.get("like_count", 0) or 0))
            created = str(item.get("created_at") or "")[:80]
            mentions = item.get("mentions") or []
            if isinstance(mentions, str):
                try:
                    mentions = json.loads(mentions)
                except ValueError:
                    raise ValueError("mentions须为UID与名称组成的JSON数组") from None
            if not isinstance(mentions, list) or any(
                not isinstance(person, dict) or type(person.get("uid")) not in {str, int}
                or not str(person["uid"]).strip() or len(str(person["uid"])) > 100
                or not isinstance(person.get("name", ""), str) or len(person.get("name", "")) > 100
                for person in mentions
            ):
                raise ValueError("mentions须为UID与名称组成的JSON数组")
            mentions = list({str(person["uid"]): {"uid": str(person["uid"]), "name": person.get("name", "")}
                             for person in mentions}.values())
            normalized.append(
                {
                    "video_id": video_id,
                    "comment_id": cid,
                    "thread_id": thread_id,
                    "parent_comment_id": parent_id,
                    "platform_user_id": uid,
                    "display_name": name,
                    "content": content,
                    "category": category(content, attachments),
                    "like_count": likes,
                    "created_at": created,
                    "mentions": mentions,
                    **({"attachments": attachments} if platform_fetch else {}),
                }
            )
        return normalized

    def import_items(self, video_id, items=None, csv_text=None, field_mapping=None, *, platform_fetch=False):
        prepared = self.prepare_import(video_id, items, csv_text, field_mapping, platform_fetch=platform_fetch)
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
                json.dumps(r["mentions"], ensure_ascii=False),
                json.dumps(r.get("attachments", []), ensure_ascii=False),
            )
            for r in prepared
        ]
        before = self.store.one("SELECT COUNT(*) AS n FROM comments")["n"]
        with self.store.db:
            self.store.db.executemany(
                "INSERT OR IGNORE INTO comments(video_id,comment_id,thread_id,parent_comment_id,platform_user_id,"
                "display_name,content,category,like_count,created_at,imported_at,mentions,attachments) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                normalized,
            )
            # Re-reading an existing comment enriches participant metadata;
            # it does not replace its body, drafts, or processing state.
            for item in prepared:
                if item["mentions"]:
                    self.store.db.execute(
                        "UPDATE comments SET mentions=? WHERE platform='bilibili' AND video_id=? AND comment_id=?",
                        (json.dumps(item["mentions"], ensure_ascii=False), video_id, item["comment_id"]),
                    )
                if platform_fetch:
                    self.store.db.execute(
                        "UPDATE comments SET attachments=?,category=CASE WHEN category_source='operator' THEN category ELSE ? END "
                        "WHERE platform='bilibili' AND video_id=? AND comment_id=? AND platform_user_id=? AND content=?",
                        (json.dumps(item["attachments"], ensure_ascii=False), item["category"], video_id,
                         item["comment_id"], item["platform_user_id"], item["content"]),
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
        async with self.fetch_lock:
            data = await self.account.adapter.fetch(video, max_pages)
        if not data["items"]:
            return {"imported": 0, "duplicates": 0, "video_id": data["video_id"], "detail": "未读到评论"}
        return self.import_items(data["video_id"], data["items"], platform_fetch=True) | {"title": data.get("title")}

    def analyze(self, video_id=None):
        where, args = (" WHERE video_id=?", (video_id,)) if video_id else ("", ())
        rows = self.store.all("SELECT id,content,category,category_source,attachments FROM comments" + where, args)
        counts = {c: 0 for c in CATEGORIES}
        with self.store.db:
            for row in rows:
                value = row["category"] if row["category_source"] == "operator" else category(
                    row["content"], json.loads(row.get("attachments") or "[]"),
                )
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
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 1000
            or any(type(i) is not int or i < 1 for i in ids)
        ):
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
            "INSERT INTO comment_tasks(id,status,total,character_id,created_at,updated_at,comment_ids) "
            "VALUES(?,'pending',?,?,?,?,?)",
            (task_id, len(ids), character["id"], time.time(), time.time(), json.dumps(ids)),
        )
        self.queue.put_nowait((task_id, ids, character, automatic))
        return {"task_id": task_id, "status": "pending", "total": len(ids), "comment_ids": ids}

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
                    scope = self.account_context_scope(row["video_id"], row["thread_id"], row["platform"])
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
