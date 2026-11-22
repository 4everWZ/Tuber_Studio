"""Comment review, quota-checked publication, and Comments channel facade."""

import asyncio
import json
import math
import re
import time

from ....core.errors import PlatformRejected, SendLimitReached, UnknownPublication
from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....core.store import dump
from ....core.text import validate_comment_body, validate_operation_text
from ....infra.inference.providers import public_error
from ....infra.platforms.bilibili.client import BilibiliAdapter, PublishUncertain
from ...kernel.control import OperationsControl, canonical_intent
from ...kernel.feedback import FeedbackPublication, PublicationContext, generation_view, publication_binding
from ...kernel.models import BrowsingPolicy
from ...kernel.quotas import DailyReplyLimitReached
from ..attachments import comment_payload, decision, require_comment_attachment, row_ref
from ..mentions import draft_mentions, rebase_mentions, revision_payload
from .drafter import CommentsDrafterMixin
from .materials import validate_comment_material_sources
from .repository import CommentsRepositoryMixin
from .scanner import CommentsScannerMixin
from .timeline import comment_source, operator_reply, read_scoped_thread


class Comments(CommentsRepositoryMixin, CommentsScannerMixin, CommentsDrafterMixin):
    """Comment browsing, bounded drafts and an explicit review/auto-publish policy."""

    def __init__(self, settings, store, runtime, account, *, is_live_busy=None):
        self.settings, self.store, self.runtime, self.account = settings, store, runtime, account
        self.is_live_busy = is_live_busy or (lambda: False)
        self.memes = None
        self._init_storage(store)
        self.operations = getattr(runtime, "operations", None) or OperationsControl(store)
        runtime.operations = self.operations
        self.queue = asyncio.Queue(maxsize=40)
        self.worker = None
        self.policy_changed = asyncio.Event()
        self.fetch_lock = asyncio.Lock()
        self.active_task_id = None
        self.thread_tasks = set()
        self.closed = False
        self.direct_messages = None
        self.task_authorizations = {}
        self.active_automation_epoch = None

    async def open(self):
        self.worker = asyncio.create_task(self._worker())

    async def close(self):
        self.closed = True
        for task in (self.worker,):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.worker,) if t), return_exceptions=True)
        while not self.queue.empty():
            task_id, *_ = self.queue.get_nowait()
            self.store.execute(
                "UPDATE comment_tasks SET status='cancelled',error='服务停止',updated_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self.queue.task_done()

    def policy(self):
        configured = BrowsingPolicy.model_validate(self.store.get("browsing_policy", {}))
        credentials = self.account.credentials_configured
        base = {
            "auto_reply": configured.reply_enabled and configured.reply_auto_publish,
            "max_replies_per_hour": configured.max_per_hour,
            "max_replies_per_day": configured.max_per_day,
        }
        data = self.operations.effective("comments", base)
        data["auto_reply"] = data["auto_reply"] and credentials
        return data | {
            "base": base,
            "automation_override": self.operations.policy()["enabled"],
            "emergency": self.operations.policy()["emergency"],
            "credentials_configured": credentials,
        }

    def review(self, draft_id, action, text=None, **options):
        row = self._draft_source(draft_id)
        if not row:
            raise ValueError("草稿不存在")
        if row["status"] in {"published", "publishing", "publish_uncertain", "private_handoff", "operator_replied"}:
            raise ValueError("这条回复已提交，可查看平台回执")
        expected = options.pop("updated_at", None)
        selection = options.pop("mention_selection", None)
        if selection is not None and action != "edit":
            raise ValueError("保存提及后需重新审核")
        if "expected_image" in options and options.pop("expected_image") != decision(self.runtime, "comments", row, row_ref(row)):
            raise ValueError("配图已变化，请核对当前草稿")
        if expected is not None and expected != row["updated_at"]:
            raise ValueError("草稿版本已变化，请刷新")
        if options and (set(options) != {"image_ref"} or action != "edit"):
            raise ValueError("图片变更须保存为待审草稿")
        if action not in {"approve", "reject", "edit"}:
            raise ValueError("未知审核动作")
        text = text if text is not None else row["text"]
        if action == "approve":
            text = validate_comment_body(text, recipient_name=row["display_name"] or "")
        else:
            text = validate_operation_text(text, label="回复")
        audit = draft_mentions(row)
        if selection is not None:
            audit.update(selection)
        elif text != row["text"] and audit.get("mentions") and action != "reject":
            audit["mentions"] = rebase_mentions(text, audit["mentions"])
        if action == "approve" and audit.get("pending_mentions"):
            raise ValueError("请先选择这条评论要 @ 的账号")
        ref = options.get("image_ref", row_ref(row))
        if "image_ref" not in options and action == "approve":
            ref = decision(self.runtime, "comments", row, ref)
        if ref is not None and (action == "approve" or "image_ref" in options):
            require_comment_attachment("comments", row, ref)
        status = {"approve": "approved", "reject": "rejected", "edit": "pending_review"}[action]
        updated_at = max(time.time(), math.nextafter(row["updated_at"], math.inf))
        self.store.execute(
            "UPDATE drafts SET text=?,status=?,image_ref=?,answer_check=?,error=NULL,updated_at=? WHERE id=?",
            (text, status, dump(ref) if ref else None, dump(audit), updated_at, draft_id),
        )
        if browsing := getattr(self.runtime, "browsing", None):
            browsing.sends.withdraw("comments", draft_id)
        if action == "reject":
            source = self.store.one("SELECT * FROM comments WHERE id=?", (row["comment_id"],))
            character = self.store.character(row["character_id"], row["character_version"])
            scope = self.account_context_scope(source["video_id"], source["thread_id"], source["platform"],
                                               account_id=row.get("account_id") or "")
            self.runtime.operation_context.remove_unsent_reply(scope, character, self._comment_event(source)["event_id"])
        return self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

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

    async def _publication_context(self, draft_id, automatic, epoch):
        await self._refresh_draft_identity(draft_id, automatic, epoch)
        row = self._draft_source(draft_id)
        if not row:
            raise ValueError("草稿不存在")
        character = self.store.character(row["character_id"], row["character_version"])
        account = self.account.display_account_id() or await self.account.account_id()
        binding = self._publication_binding(row, account, character)
        adapter, credential = self.account.adapter, dict(self.account.credentials)

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
                or comment_payload(fresh, self.runtime) != comment_payload(row, self.runtime)
                or self._publication_binding(fresh, account, character) != binding
            ):
                raise ValueError("恢复期间草稿、角色或原始来源已变化")
            if (
                self.account.adapter is not adapter
                or credential != self.account.credentials
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
            payload=comment_payload(row, self.runtime),
            encode_revision=lambda value: revision_payload(comment_payload(row, self.runtime), value),
            on_defer=lambda prepared, state: self._defer_revision(draft_id, prepared, state),
            manual_approved=row["status"] in {"approved", "copied"},
        )

    async def _refresh_draft_identity(self, draft_id, automatic, epoch):
        """Refresh an automatic draft under the current account facts."""
        if not automatic:
            return
        refresh = getattr(self.runtime, "refresh_identity", None)
        if refresh is None or not hasattr(self.account.adapter, "account_profile"):
            return
        row = self._draft_source(draft_id)
        if not row or row["status"] != "pending_review":
            return
        validate_comment_body(row["text"], recipient_name=row["display_name"] or "")
        character = self.store.character(row["character_id"], row["character_version"])
        account = self.account.display_account_id() or await self.account.account_id()
        if row["account_id"] and row["account_id"] != account:
            raise ValueError("该草稿属于其他账号，不能由当前账号发送")
        current = await refresh(character, account)
        previous = json.loads(row["answer_check"] or "{}").get("generation_identity")
        if previous and previous.get("identity_version") == current["identity_version"]:
            return
        source = self.store.one("SELECT * FROM comments WHERE id=?", (row["comment_id"],))
        event = self._comment_event(source) | {"account_id": account}
        event.update(self._comment_actors(source, character, account))
        browsing = getattr(self.runtime, "browsing", None)
        materials = await self._attach_current_materials(browsing, source, event) if browsing else []
        scope = self.account_context_scope(row["video_id"], row["thread_id"], "bilibili", account_id=account)
        async with self.runtime.offline_generation("background"):
            fresh = self._draft_source(draft_id)
            if not fresh or any(fresh[key] != row[key] for key in ("text", "status", "content", "account_id")):
                raise ValueError("更新身份期间草稿或来源已变化，保留当前内容")
            self.operations.assert_allowed(automatic=automatic, channel="comments", account_id=account,
                                           character=character, epoch=epoch)
            sources = self._thread_sources(row["video_id"], row["thread_id"], "bilibili", source["id"])
            candidate = self.runtime.operation_context.source_candidate(scope, character, [self._comment_event(item) for item in sources])
            try:
                reply, check = await self.runtime.generate_reply(
                    scope, character, event, mode="comment_draft", context=candidate,
                )
                self._check_sources(sources, row["video_id"], row["thread_id"], "bilibili")
                if runtime_identity(self.runtime, character, account)["identity_version"] != current["identity_version"]:
                    raise ValueError("生成期间账号身份再次变化，保留草稿")
                check["generation_identity"] = current
                old_mentions = draft_mentions(row)
                if old_mentions.get("mentions") or old_mentions.get("pending_mentions"):
                    check["mentions"] = rebase_mentions(reply.spoken_text, old_mentions.get("mentions", []))
                    check["pending_mentions"] = old_mentions.get("pending_mentions", [])
                if materials:
                    check["comment_materials"] = materials
                changed = self.store.execute(
                    "UPDATE drafts SET text=?,input_summary=?,answer_check=?,error=NULL,updated_at=? "
                    "WHERE id=? AND status=? AND text=?",
                    (reply.spoken_text, json.dumps(event, ensure_ascii=False), json.dumps(check, ensure_ascii=False),
                     time.time(), draft_id, row["status"], row["text"]),
                )
                if changed.rowcount != 1:
                    raise ValueError("生成期间草稿已更新，未覆盖")
                if check.get("output", {}).get("needs_review"):
                    self.store.execute("UPDATE drafts SET status='pending_review' WHERE id=?", (draft_id,))
            finally:
                candidate.store.close()

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
            current_account = await self.account.account_id() if self.policy()["credentials_configured"] else ""
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
            self.runtime.continuations.resume(row["id"], binding=row["binding"])
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

    async def _reconcile(self, draft_id, *, guard=None):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)

        async def read(row, attempt):
            if row["feedback"]["sent_payload"].get("asset_sha256") and not (attempt["receipt"] or {}).get("rpid"):
                return {"status": "unsupported", "reason": "image_receipt_required"}
            if not hasattr(self.account.adapter, "reconcile_publication"):
                return {"status": "unsupported"}
            try:
                account = await self.account.account_id()
            except ValueError:
                return {"status": "check_failed", "reason": "account_unavailable"}
            if account != row["binding"]["account_id"]:
                return {"status": "check_failed", "reason": "account_changed"}
            if guard:
                guard(row, attempt)
            adapter, credential = self.account.adapter, dict(self.account.credentials)
            result = await adapter.reconcile_publication(
                row["binding"]["target"] | {"mentions": row["feedback"]["sent_payload"].get("mentions", [])},
                author_uid=account,
                text=row["feedback"]["sent_payload"].get("text", ""),
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )
            if guard:
                if self.account.adapter is not adapter or self.account.credentials != credential:
                    raise ValueError("核对期间账号连接已变化")
                guard(row, attempt)
            return result

        def commit(receipt, verification):
            self.store.execute(
                "UPDATE drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                (json.dumps(receipt, ensure_ascii=False), time.time(), draft_id),
            )
            self.store.execute(
                "UPDATE publish_attempts SET status='published',receipt=? WHERE id=(SELECT MAX(id) FROM publish_attempts WHERE draft_id=?)",
                (json.dumps(receipt, ensure_ascii=False), draft_id),
            )
            self._record_published_context(draft_id, feedback.coordinator.find("comments", draft_id)["binding"]["account_id"])

        return await feedback.reconcile("comments", draft_id, read, commit)

    def _record_published_context(self, draft_id, account_id):
        draft = self._draft_source(draft_id)
        receipt = json.loads(draft["receipt"] or "null") if draft else None
        if not draft or draft["status"] != "published" or not receipt or not re.fullmatch(
            r"[1-9][0-9]{0,19}", str(receipt.get("rpid", ""))
        ):
            return
        source = self.store.one("SELECT * FROM comments WHERE id=?", (draft["comment_id"],))
        character = self.store.character(draft["character_id"], draft["character_version"])
        event = self._comment_event(source) | {"account_id": account_id}
        scope = self.account_context_scope(source["video_id"], source["thread_id"], source["platform"], account_id=account_id)
        check = json.loads(draft["answer_check"] or "{}")
        used_memory = check.get("organized_memory") or "memory_preference_revision" in check
        self.runtime.operation_context.record(scope, character, event, None if used_memory else draft["text"], "comment_draft",
                                    memory_kind=check.get("memory_kind"))

    def publication_limits(self):
        policy = BrowsingPolicy.model_validate(self.store.get("browsing_policy", {}))
        return {"max_per_hour": policy.max_per_hour, "max_per_day": policy.max_per_day}

    def _reserve_daily_video_reply(self, token, row, account_id):
        if not (row.get("video_id") or "").startswith("BV"):
            return
        policy = self.store.get("browsing_policy", {})
        self.runtime.browsing_quotas.reserve_reply(
            token,
            account_id,
            row["character_id"],
            row["video_id"],
            canonical_intent("bilibili", row["video_id"], row["platform_comment_id"], row["character_id"]),
            policy.get("max_replies_per_video", 2),
            policy.get("business_timezone", "Asia/Taipei"),
        )

    async def publish(self, draft_id, automatic=False, *, authorization_epoch=None, submission=None):
        row = self._draft_source(draft_id)
        if row and row["status"] in {"pending_review", "approved"}:
            require_comment_attachment("comments", row, row_ref(row))
        if submission is not None:
            submission.check()
            return await self._publish_once(draft_id, submission=submission)
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)
        confirmed = None if automatic else row
        confirmed_hash = content_hash(comment_payload(confirmed, self.runtime)) if confirmed else None
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
                body_hash=confirmed_hash or body_hash,
            ),
            apply_revision=lambda prepared, text: self._apply_revision(draft_id, prepared, text),
            automatic=automatic,
            epoch=authorization_epoch,
            reconcile=lambda token, prepared: self._reconcile(draft_id),
            revise_on_rejection=automatic,
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
        submission=None,
    ):
        await self._refresh_draft_identity(draft_id, automatic, authorization_epoch)
        async with self.account.credential_lock:
            policy = self.policy()
            if automatic and not policy["auto_reply"]:
                raise ValueError("自动回复已停止，草稿保留待审核")
            if not policy["credentials_configured"]:
                raise ValueError("未配置B站账号写入凭证；草稿可人工审核复制")
            row = self._draft_source(draft_id)
            allowed = {"pending_review"} if automatic else {"approved", "copied"}
            if submission:
                allowed = allowed | {"pending_review"}
                submission.check()
            if not row or row["status"] not in allowed:
                raise ValueError("当前草稿状态不能发布，或已发布／结果未确认")
            validate_comment_body(row["text"], recipient_name=row["display_name"] or "")
            if automatic and json.loads(row["answer_check"] or "{}").get("output", {}).get("needs_review"):
                raise ValueError("正文诊断尚待审核，未自动发布")
            previous = row["status"]
            account_id = await self.account.account_id()
            if row["account_id"] and row["account_id"] != account_id:
                raise ValueError("该草稿属于其他账号，不能由当前账号发送")
            adapter, credential = self.account.adapter, dict(self.account.credentials)
            if submission and not hasattr(adapter, "read_video_thread"):
                raise ValueError("当前适配器无法核验原评论和号主回复")
            if hasattr(adapter, "read_video_thread"):
                target = {"bvid": row["video_id"], "root": row["thread_id"],
                          "parent": row["platform_comment_id"]}
                thread = await read_scoped_thread(
                    self.store, adapter.read_video_thread, target, target | {"account_id": account_id, "resource_type": "video"},
                )
                if adapter is not self.account.adapter or credential != self.account.credentials:
                    raise ValueError("核验期间账号已变化，保留草稿")
                source = thread["source"]
                if source["text"] != row["content"] or (
                    row["platform_user_id"] and str(source["author_uid"]) != str(row["platform_user_id"])
                ):
                    raise ValueError("原始评论或作者已变化，保留草稿")
                if matched := operator_reply(thread["items"], target, account_id):
                    if submission:
                        submission.check()
                    changed = self.store.execute(
                        "UPDATE drafts SET status='operator_replied',answer_check=json_set(COALESCE(answer_check,'{}'),"
                        "'$.account_reply',json(?)),error=NULL,updated_at=? WHERE id=? AND text=? AND status=?",
                        (dump(comment_source(matched) | {"created_at": matched.get("created_at")}),
                         time.time(), draft_id, row["text"], row["status"]),
                    )
                    if changed.rowcount != 1:
                        raise ValueError("核验期间草稿已更新")
                    return self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
                validate_comment_material_sources(json.loads(row["answer_check"] or "{}").get("comment_materials", []),
                                                  thread["items"], row["platform_comment_id"])
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
                    "display_name",
                    "image_ref",
                    "thread_id",
                    "video_id",
                )
            ):
                raise ValueError("发送准备期间草稿或来源已变化")
            require_comment_attachment("comments", row, row_ref(row))
            payload = comment_payload(row, self.runtime)
            if draft_mentions(row).get("pending_mentions"):
                raise ValueError("请先选择这条评论要 @ 的账号")
            actual_binding = self._publication_binding(row, account_id, character)
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash(payload)
            ):
                raise ValueError("发送前原始绑定或正文已变化")
            token = None

            def admit(ticket):
                self._reserve_daily_video_reply(ticket, row, account_id)
                if submission:
                    submission.admit(ticket, actual_binding, payload, attempt)

            def authorize():
                audit = json.loads(row["answer_check"] or "{}")
                self.runtime.validate_memory_delivery(audit)
                captured = audit.get("generation_identity")
                if (
                    automatic and hasattr(self.account.adapter, "account_profile") and captured
                    and captured.get("identity_version") != runtime_identity(self.runtime, character, account_id)["identity_version"]
                ):
                    raise ValueError("发送前身份资料已更新，保留草稿重新生成")
                fresh = self._draft_source(draft_id)
                if (
                    self.account.adapter is not adapter
                    or credential != self.account.credentials
                    or not fresh
                    or fresh["content"] != row["content"]
                    or fresh["platform_comment_id"] != row["platform_comment_id"]
                    or fresh["platform_user_id"] != row["platform_user_id"]
                    or fresh["display_name"] != row["display_name"]
                    or fresh["status"] != "publishing"
                    or comment_payload(fresh, self.runtime) != payload
                    or self._publication_binding(fresh, account_id, character) != actual_binding
                    or (automatic and not self.policy()["auto_reply"])
                ):
                    raise ValueError("发送前账号、来源或授权已变化，保留草稿")
                browsing = getattr(self.runtime, "browsing", None)
                if browsing:
                    browsing.sends.assert_approval("comments", draft_id, fresh, payload)
                if automatic and browsing:
                    current_draft = self.store.one("SELECT input_summary FROM drafts WHERE id=?", (draft_id,))
                    browsing.assert_comment_evidence(json.loads(current_draft["input_summary"] or "{}"))
                self.operations.assert_allowed(
                    automatic=automatic,
                    channel="comments",
                    account_id=account_id,
                    character=character,
                    epoch=authorization_epoch,
                )
                if submission:
                    submission.check(publishing=True)
                return True

            def before_send():
                nonlocal token
                authorize()
                token = self.operations.reserve(
                    account_id,
                    character,
                    canonical_intent(
                        "bilibili", row["video_id"], row["platform_comment_id"], character["id"]
                    ),
                    "comments",
                    recipient=row["platform_user_id"] or "",
                    automatic=automatic,
                    limits=self.publication_limits(),
                    epoch=authorization_epoch,
                    binding=actual_binding,
                    content_hash=content_hash(payload),
                    revision_ticket=revision_ticket,
                    admission_hook=admit,
                )

            if submission:
                submission.check()
            attempt = self.store.execute(
                "INSERT INTO publish_attempts(draft_id,started_at,status) VALUES(?,?,'sending')",
                (draft_id, time.time()),
            ).lastrowid
            self.store.execute(
                "UPDATE drafts SET status='publishing',updated_at=? WHERE id=?", (time.time(), draft_id)
            )
            receipt = None
            try:
                options = (
                    {"thread_id": row["thread_id"]} if row["thread_id"] != row["platform_comment_id"] else {}
                )
                if isinstance(adapter, BilibiliAdapter):
                    options["before_send"] = before_send
                else:
                    before_send()
                if payload.get("mentions"):
                    options["mentions"] = payload["mentions"]
                receipt = await self.account.adapter.publish(
                    row["video_id"], row["platform_comment_id"], row["text"], **options
                )
                if not isinstance(receipt, dict) or not re.fullmatch(
                    r"[1-9][0-9]{0,19}", str(receipt.get("rpid", ""))
                ):
                    raise PublishUncertain("平台没有返回有效评论ID")
                if payload.get("mentions"):
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    self.store.execute("UPDATE drafts SET status='publish_uncertain',receipt=?,error=? WHERE id=?",
                                       (dump(receipt), "评论已受理，提及待核对", draft_id))
                    self.store.execute("UPDATE publish_attempts SET status='uncertain',receipt=? WHERE id=?",
                                       (dump(receipt), attempt))
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "comments", draft_id, token, actual_binding, payload, "评论已受理，提及待核对"
                    )
                    if await self._reconcile(draft_id) is not None:
                        return self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
                    raise UnknownPublication("评论已受理，提及待核对", token)
            except UnknownPublication:
                raise
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
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "comments", draft_id, token, actual_binding, payload, "发布结果不确定"
                    )
                message = "评论发布结果待核对"
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
                if isinstance(error, (SendLimitReached, DailyReplyLimitReached)):
                    raise
                if isinstance(error, PlatformRejected):
                    error.write_token = token
                    error.body_hash = content_hash(payload)
                    error.sent_payload = payload
                    raise
                raise ValueError(message) from None
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "comments", draft_id, token, actual_binding, payload, "发布结果不确定"
                    )
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=? WHERE id=?",
                    ("评论发布结果待核对", draft_id),
                )
                raise UnknownPublication("评论发布结果待核对", token) from None
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
                        payload,
                        "平台成功后本地保存失败",
                    )
                self.store.execute(
                    "UPDATE drafts SET status='publish_uncertain',error=? WHERE id=?",
                    ("平台已返回成功，本地回执保存失败；请核对平台", draft_id),
                )
                raise UnknownPublication("B 站已受理评论，本地回执待补录", token) from None
            self._record_published_context(draft_id, account_id)
            return {"status": "published", "receipt": receipt}
