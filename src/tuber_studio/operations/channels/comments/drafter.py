"""Comment reply generation, material preparation, and continuation revision handling."""

import asyncio
import json
import time

from ....core.identity import runtime_identity
from ....core.store import dump
from ....infra.platforms.bilibili.account import cached_account_actor
from ...kernel.control import canonical_intent
from ..mentions import bind_mentions, draft_mentions, mention_participants, rebase_mentions
from .materials import model_comment_materials, prepare_comment_materials
from .timeline import comment_source, comment_thread_context


class CommentsDrafterMixin:
    def _live_pending(self):
        return bool(self.is_live_busy())

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
            "mentions": json.loads(row.get("mentions") or "[]"),
        }

    def _comment_actors(self, row, character, account_id):
        rows = self._thread_sources(row["video_id"], row["thread_id"], row["platform"])
        items = [item | {"mentions": json.loads(item.get("mentions") or "[]")} for item in rows]
        source = row | {"mentions": json.loads(row.get("mentions") or "[]")}
        target = {"root": row["thread_id"] or row["comment_id"], "parent": row["comment_id"],
                  "recipient": row["platform_user_id"]}
        actor = cached_account_actor(self.store, self.account.credentials, account_id) | {
            "character_id": character["id"], "character_name": character["name"],
        }
        return {"self_actor": actor, "comment_context": comment_thread_context(source, items, target)}

    def _check_sources(self, rows, video_id, thread_id, platform):
        current = {row["id"]: row for row in self._thread_sources(video_id, thread_id, platform)}
        if any(row["id"] not in current or current[row["id"]]["content"] != row["content"] for row in rows):
            raise ValueError("楼原文在重建期间已清理或变化，未安装上下文")

    @staticmethod
    async def _attach_current_materials(browsing, row, event):
        attachments = json.loads(row.get("attachments") or "[]")
        if not attachments:
            return []
        materials = await prepare_comment_materials(
            browsing, {"parent": row["comment_id"]}, row | {"attachments": attachments},
        )
        if materials:
            event["comment_materials"] = model_comment_materials(materials)
        return materials

    async def rebuild_context(self, video_id, thread_id, character=None, *, platform="bilibili"):
        """Caller holds the operations inference lock. Rebuild from retained originals."""
        character = character or self.runtime.character
        scope = self.account_context_scope(video_id, thread_id, platform)
        context = self.runtime.operation_context
        previous = context.existing(scope, character)
        if previous and json.loads(previous["history"]):
            raise ValueError("当前楼已有非空上下文，请先清除后再重建")
        rows = self._thread_sources(video_id, thread_id, platform)
        if not rows:
            raise ValueError("这条讨论缺少已保存的原评论")
        candidate = context.source_candidate(scope, character, [self._comment_event(row) for row in rows])
        try:
            messages = await candidate.prepare(scope, character, None, mode="comment_draft")
            prefilled = await self.runtime.operation_llm.prefill(messages)
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
            self.operations.assert_authorized(
                automatic=True,
                channel="comments",
                account_id=control["account_id"],
                character=character,
                epoch=self.active_automation_epoch,
            )
        if row["category"] == "spam" or self.store.one("SELECT id FROM drafts WHERE comment_id=?", (cid,)):
            return
        event = self._comment_event(row)
        account_id = await self.account.account_id() if self.policy()["credentials_configured"] else self.account.context_account_id()
        event["account_id"] = account_id
        if self.direct_messages:
            handoff = await self.direct_messages.route_comment(row, character, event, automatic=automatic)
            if handoff:
                now = time.time()
                self.store.execute(
                    "INSERT OR IGNORE INTO drafts(comment_id,text,status,character_id,character_version,model,"
                    "input_summary,created_at,updated_at,account_id) VALUES(?,?,'private_handoff',?,?,?,?,?,?,?)",
                    (
                        cid, f"已转私信草稿 #{handoff}", character["id"], character["version"],
                        self.settings.data["omni"]["responses_model"], json.dumps(event, ensure_ascii=False),
                        now, now, account_id,
                    ),
                )
                return
        if automatic and browsing:
            event["video_evidence"] = await browsing.comment_evidence(row, character)
        materials = await self._attach_current_materials(browsing, row, event) if browsing else []
        if not event["text"].strip() and not event.get("video_evidence") and not any(
            item["status"] == "read" for item in materials
        ):
            raise ValueError("评论没有可读正文，附件也未能读取")
        scope = self.account_context_scope(row["video_id"], row["thread_id"], row["platform"], account_id=account_id)
        while True:
            while self._live_pending():
                await asyncio.sleep(0.2)
            async with self.runtime.offline_generation("background"):
                if self._live_pending():
                    continue
                # Recheck after waiting for the GPU: another request may already
                # have produced a draft. Reviewed/published drafts are immutable here.
                if self.store.one("SELECT id FROM drafts WHERE comment_id=?", (cid,)):
                    return
                if browsing and browsing.has_intent(row, character):
                    return
                context = self.runtime.operation_context
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
                event.update(self._comment_actors(row, character, account_id))
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
                    generation_identity = runtime_identity(self.runtime, character, account_id or None)
                    reply, check = await self.runtime.generate_reply(
                        scope,
                        character,
                        event,
                        relationship=relationship,
                        mode="comment_draft",
                        **({"context": candidate} if candidate else {}),
                    )
                    check.setdefault("generation_identity", generation_identity)
                    mentions = getattr(reply, "mentions", [])
                    if mentions:
                        bound, pending = await bind_mentions(self.account.adapter, reply.spoken_text,
                            [item.model_dump() for item in mentions],
                            mention_participants({"source": comment_source(row)}))
                        check.update(mentions=bound, pending_mentions=pending)
                    if materials:
                        check["comment_materials"] = materials
                    if candidate:
                        self._check_sources(sources, row["video_id"], row["thread_id"], row["platform"])
                        candidate.record_source(
                            scope,
                            character,
                            self._comment_event(row) | {"account_id": account_id},
                            "comment_draft",
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
                                self.settings.data["omni"]["responses_model"],
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
                            "UPDATE drafts SET account_id=?,generation_task_id=? WHERE id=?",
                            (account_id, self.active_task_id, draft_id),
                        )
                        if candidate:
                            context.install_candidate(scope, character, candidate)
                    if not candidate:
                        context.record_source(
                            scope,
                            character,
                            self._comment_event(row) | {"account_id": account_id},
                            "comment_draft",
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
        if automatic and self.policy()["auto_reply"] and not (
            check.get("output", {}).get("needs_review") or check.get("pending_mentions")
        ):
            await self.publish(draft_id, automatic=True, authorization_epoch=self.active_automation_epoch)

    def _apply_revision(self, draft_id, prepared, text):
        prepared.guard()
        audit = draft_mentions(self._draft_source(draft_id))
        if audit.get("mentions"):
            audit["mentions"] = rebase_mentions(text, audit["mentions"])
        cursor = self.store.execute(
            "UPDATE drafts SET text=?,answer_check=?,status='pending_review',error=NULL,updated_at=? "
            "WHERE id=? AND text=? AND status IN('approved','copied','pending_review')",
            (text, dump(audit), time.time(), draft_id, prepared.text),
        )
        if cursor.rowcount != 1:
            raise ValueError("草稿已被更新，修订结果保留在恢复记录")

    def _defer_revision(self, draft_id, prepared, state):
        prepared.guard()
        self.store.execute(
            "UPDATE drafts SET status=?,updated_at=? WHERE id=?",
            ("rejected" if state == "abandoned" else "pending_review", time.time(), draft_id),
        )
