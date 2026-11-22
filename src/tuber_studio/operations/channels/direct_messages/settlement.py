"""Review, platform send, receipt reconciliation, and DirectMessages channel facade."""

import asyncio
import json
import time

from ....core.errors import PlatformRejected, SendLimitReached, UnknownPublication
from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....core.text import validate_operation_text
from ....infra.inference.providers import public_error
from ....infra.platforms.bilibili.client import PublishUncertain
from ....infra.platforms.bilibili.messages import BilibiliMessagesAdapter, MessagePreparationCancelled
from ...kernel.continuations import GenerationFeedback
from ...kernel.control import canonical_intent
from ...kernel.feedback import FeedbackPublication, PublicationContext, publication_binding
from ..attachments import (
    TEXT_ONLY_IMAGE,
    checked_image,
    image_choice,
    image_payload,
    image_scope,
    rebind_image,
    record_upload,
    row_ref,
    wait_decision,
)
from ..attachments import decision as image_decision
from ..dynamics.materials import ensure_reflections
from .drafter import DirectMessagesDrafterMixin
from .models import MessageIdentityChanged, MessageInputChanged, MessagePolicy
from .repository import DirectMessagesRepositoryMixin
from .sync import DirectMessagesSyncMixin
from .windowing import DirectMessagesWindowingMixin


class DirectMessages(
    DirectMessagesRepositoryMixin,
    DirectMessagesSyncMixin,
    DirectMessagesWindowingMixin,
    DirectMessagesDrafterMixin,
):
    """Source-triggered private replies, separate contexts and one-shot delivery."""

    def __init__(self, settings, store, runtime, comments, adapter=None, *, account=None, memes=None):
        self.settings, self.store, self.runtime, self.comments = settings, store, runtime, comments
        self.account = account or comments.account
        ensure_reflections(store)
        self.adapter, self.injected = adapter, adapter is not None
        self.memes = memes
        self.credential_signature = None
        self.queue = asyncio.PriorityQueue(maxsize=40)
        self.worker = self.poller = None
        self.closed = False
        self.changed = asyncio.Event()
        self.sync_lock = asyncio.Lock()
        self.error, self.last_sync = None, None
        self.enabled_at = self.store.get("message_enabled_at", time.time())
        self.started_at = int(time.time())
        self.operations = comments.operations
        self.authorizations = {}
        self._scheduled = set()
        self.operations_resumed = False
        self.active_account_id = ""
        self.active_credential = None
        self._init_storage(store)

    def policy(self):
        base = MessagePolicy.model_validate(self.store.get("message_policy", {})).model_dump()
        return {
            **self.operations.effective("messages", base),
            "base": base,
            "automation_override": self.operations.policy()["enabled"],
            "emergency": self.operations.policy()["emergency"],
            "credentials_configured": bool(
                self.account.credentials.get("sessdata") and self.account.credentials.get("csrf")
            ),
            "last_error": self.error,
            "last_sync": self.last_sync,
        }

    async def configure(self, value):
        value = MessagePolicy.model_validate(value).model_dump()
        old = self.policy()
        if value["auto_poll"] or value["auto_send"]:
            if not value["enabled"]:
                raise ValueError("请先启用私信功能")
            if not self.injected and not old["credentials_configured"]:
                raise ValueError("自动收发私信需要先配置现有B站账号凭证")
        if value["enabled"] and not old["enabled"]:
            self.enabled_at = int(time.time())
            self.store.set("message_enabled_at", self.enabled_at)
        self.store.set("message_policy", value)
        self.changed.set()
        return self.policy()

    def _enabled(self):
        if self.closed:
            raise ValueError("私信服务正在停止")
        if not self.policy()["enabled"]:
            raise ValueError("私信功能未启用")

    def display_account_id(self):
        if self.active_credential == self.account.credentials:
            return self.active_account_id
        return self.account.display_account_id()

    def _processing_authorized(self):
        return self.policy()["enabled"] and self.operations_resumed and self.operations.policy()["enabled"]

    def _schedule(self, identity, automatic):
        if identity in self._scheduled:
            return
        draft = self.store.one("SELECT source_kind,created_at,trigger_source FROM dm_drafts WHERE id=?", (identity,))
        if not draft:
            return
        waiting_since = json.loads(draft["trigger_source"] or "{}").get("waiting_since", draft["created_at"])
        self.queue.put_nowait((draft["source_kind"] != "message", waiting_since, identity, automatic))
        self._scheduled.add(identity)

    async def resume_pending(self):
        """Explicit activation of the operations inbox; never inferred at boot."""
        if self.operations.policy()["enabled"]:
            self.operations_resumed = True
        await self._resume_pending()

    async def _resume_pending(self):
        """Restore a few retained drafts after explicit activation, for review."""
        if self.closed or not self._processing_authorized() or not self.operations.accepting_work or self.queue.qsize() >= 2:
            return
        if not self.store.one(
            "SELECT id FROM dm_drafts WHERE status='queued' AND resume_pending=1 AND next_attempt_at<=? "
            "AND character_id=? AND character_version=? LIMIT 1",
            (time.time(), self.runtime.character["id"], self.runtime.character["version"]),
        ):
            return
        account_id = self.display_account_id()
        if not account_id:
            adapter = await self._adapter()
            credential = dict(self.account.credentials)
            account_id, _ = await adapter.sessions()
            if self.adapter is not adapter or self.account.credentials != credential:
                return
            self.active_account_id, self.active_credential = str(account_id), credential
        if self.closed or not self._processing_authorized() or not self.operations.accepting_work:
            return
        scope = "account_id=?" if not self.injected else "account_id IN (?, '')"
        rows = self.store.all(
            "SELECT * FROM dm_drafts WHERE status='queued' AND resume_pending=1 AND next_attempt_at<=? "
            f"AND character_id=? AND character_version=? AND {scope} ORDER BY (source_kind<>'message'),created_at,id LIMIT 3",
            (time.time(), self.runtime.character["id"], self.runtime.character["version"], str(account_id)),
        )
        for row in rows:
            if self.queue.qsize() >= 2 or self.queue.full():
                break
            if row["id"] in self._scheduled:
                continue
            trigger = json.loads(row.get("trigger_source") or "{}")
            current = self.operations.policy()
            automatic = bool(self.policy()["auto_send"] and trigger.get("automatic_epoch") == current["epoch"]
                and row["account_id"] == current["account_id"] and row["character_id"] == current["character_id"])
            self.authorizations[row["id"]] = current["epoch"] if automatic else None
            self._schedule(row["id"], automatic)

    def review(self, identity, action, text=None, **options):
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row or row["status"] not in {"pending_review", "approved", "rejected", "copied"}:
            raise ValueError("当前私信草稿不能编辑或审核")
        reselect = action == "reselect_image"
        if action not in {"approve", "reject", "edit", "copy", "reselect_image"}:
            raise ValueError("未知私信审核操作")
        expected = options.pop("updated_at", None)
        if "expected_image" in options and options.pop("expected_image") != image_decision(self.runtime, "messages", row, row_ref(row)):
            raise ValueError("配图已变化，请核对当前私信")
        if expected is not None and expected != row["updated_at"]:
            raise ValueError("草稿版本已变化，请刷新")
        raw = row_ref(row)
        if reselect:
            if text is not None or options or row["status"] not in {"pending_review", "approved"}:
                raise ValueError("仅待发私信可以重新选图")
            if self.display_account_id() != row["account_id"]:
                raise ValueError("请切换到草稿所属账号")
            options["image_ref"] = rebind_image(self.runtime, "messages", row, row["text"], retry=True)
            action = "edit"
        if set(options) - {"meme_id", "image_ref"} or len(options) > 1 or options and action != "edit":
            raise ValueError("更换表情需要重新编辑并审核")
        ref = (options.get("image_ref") or TEXT_ONLY_IMAGE) if "image_ref" in options else raw
        if "meme_id" in options:
            ref = None if options["meme_id"] else TEXT_ONLY_IMAGE
        if raw and "job_id" in raw and text is not None and text.strip() != row["text"] and not options:
            if action == "approve":
                raise ValueError("正文已修改，请先保存并查看新稿的配图决定")
            if action == "edit":
                ref = rebind_image(self.runtime, "messages", row, validate_operation_text(text, label="私信内容"))
        if action == "approve":
            ref = image_decision(self.runtime, "messages", row, ref)
            if ref and "job_id" in ref:
                raise ValueError("配图待决定，请重新选图或使用纯文字")
            if ref is None and image_choice(self.runtime, "messages", row, raw, ref) == "text_only":
                ref = TEXT_ONLY_IMAGE
        if ref and ref != TEXT_ONLY_IMAGE and not reselect and ("image_ref" in options or action == "approve"):
            if not row["account_id"] or not row["platform_user_id"]:
                raise ValueError("图文私信需要绑定账号和收件人")
            checked_image(self.memes, ref, row["character_id"], runtime=self.runtime, scope=image_scope("messages", row))
        meme_id = None if "image_ref" in options else options.get("meme_id", row.get("meme_id"))
        image = ref and ref != TEXT_ONLY_IMAGE
        kind = "text_image" if image else ("image" if meme_id else "text")
        if action == "copy" and (meme_id or image):
            raise ValueError("图片私信不能复制为文本，请使用发送图片")
        if meme_id and action != "reject":
            if not self.memes:
                raise ValueError("表情组件尚未启用")
            self.memes.read(meme_id, row["character_id"])
        if action == "copy" and row["status"] not in {"approved", "copied"}:
            raise ValueError("请先审核私信，再复制")
        text = text if text is not None else row["text"]
        text = validate_operation_text(text, label="私信内容")
        status = {"approve": "approved", "reject": "rejected", "edit": "pending_review", "copy": "copied"}[
            action
        ]
        owner = f"dm-draft:{identity}"
        if meme_id and action != "reject":
            self.memes.pin(meme_id, row["character_id"], owner)
        try:
            self.store.execute(
                "UPDATE dm_drafts SET text=?,status=?,message_kind=?,meme_id=?,image_ref=?,error=NULL,updated_at=? WHERE id=?",
                (text.strip(), status, kind, meme_id,
                 json.dumps(raw if action == "approve" and raw and "job_id" in raw else ref) if ref else None,
                 time.time(), identity),
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
            browsing.sends.withdraw("messages", identity)
        return {
            "status": status,
            "text": text.strip(),
            "message_kind": kind,
            "meme_id": meme_id,
        }

    def _publication_binding(self, row, source, account, character):
        check = json.loads(row.get("answer_check") or "{}")
        return publication_binding(
            "messages",
            account,
            character,
            canonical_intent(
                row["platform"],
                source["video_id"] if row["source_kind"] == "comment" else "dm",
                source["comment_id"] if row["source_kind"] == "comment" else source["message_id"],
                character["id"],
            ),
            {"platform": row["platform"], "recipient_uid": row["platform_user_id"]},
            {
                "kind": row["source_kind"],
                "local_id": row["source_id"],
                "platform_id": source["comment_id"]
                if row["source_kind"] == "comment"
                else source["message_id"],
                "content_sha256": content_hash(source["content"]),
                **({"reply_incoming_ids": check["reply_incoming_ids"]} if check.get("reply_incoming_ids") else {}),
            },
        )

    def _outgoing(self, row, character):
        ref = image_decision(self.runtime, "messages", row, row_ref(row))
        if ref:
            return {"message_kind": "text_image", "text": row["text"]} | image_payload(ref)
        if row.get("message_kind") == "image":
            if not self.memes or not row.get("meme_id"):
                raise ValueError("图片草稿缺少已登记表情")
            asset = self.memes.resolve(row["meme_id"], character)
            return {"message_kind": "image", "meme_id": row["meme_id"], "asset_sha256": asset["sha256"]}
        return {"text": row["text"]}

    async def _publication_context(self, identity, automatic, epoch):
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row:
            raise ValueError("私信草稿不存在")
        character = self.store.character(row["character_id"], row["character_version"])
        source = self._source(row["source_kind"], row["source_id"], character)
        adapter = await self._adapter()
        account = (
            str(source.get("account_id") or "injected-test-account")
            if self.injected
            else str(await adapter.self_uid())
        )
        binding = self._publication_binding(row, source, account, character)
        credential = dict(self.account.credentials)

        def guard():
            if self.closed:
                raise asyncio.CancelledError
            fresh = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not fresh or any(
                fresh.get(key) != row.get(key)
                for key in (
                    "status",
                    "text",
                    "message_kind",
                    "meme_id",
                    "image_ref",
                    "character_id",
                    "character_version",
                    "account_id",
                    "platform",
                    "platform_user_id",
                    "source_kind",
                    "source_id",
                )
            ):
                raise ValueError("恢复期间私信草稿或审核状态已变化")
            current = self._source(row["source_kind"], row["source_id"], character)
            if (
                self._publication_binding(fresh, current, account, character) != binding
                or self.adapter is not adapter
                or self.account.credentials != credential
            ):
                raise ValueError("恢复期间私信账号、收件人或原始来源已变化")
            policy = self.policy()
            if (
                not policy["enabled"]
                or automatic
                and not policy["auto_send"]
                or row["source_kind"] == "comment"
                and not policy["allow_comment_handoff"]
            ):
                raise ValueError("私信授权已停止")
            self.operations.assert_allowed(
                automatic=automatic, channel="messages", account_id=account, character=character, epoch=epoch
            )

        def encode(value):
            if isinstance(value, str):
                return self._outgoing(row | {"text": validate_operation_text(value)}, character) if row_ref(row) else {"text": validate_operation_text(value)}
            if not isinstance(value, dict):
                raise ValueError("私信修订内容无效")
            candidate = row | value
            return self._outgoing(candidate, character)

        async def revise_image(model, current, decision):
            assets = [
                {key: asset[key] for key in ("asset_id", "title", "emotions")}
                for asset in (self.memes.list(character) if self.memes else [])
                if asset["enabled"] and not asset.get("missing")
            ]
            allowed = [asset["asset_id"] for asset in assets]
            schema = {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["text", "image"]},
                    "meme_id": {"enum": [None, *allowed]},
                },
                "required": ["kind", "meme_id"],
                "additionalProperties": False,
            }
            value = await model.generate_structured(
                current,
                schema,
                "为这次私信选择修订：选择图库中的另一张图片，或选text改为文字，文字正文在下一步生成。可用图库："
                + json.dumps(assets, ensure_ascii=False),
            )
            if not isinstance(value, dict) or set(value) != {"kind", "meme_id"}:
                raise GenerationFeedback("私信修订选择需要kind与meme_id", original_output=value)
            if value["kind"] == "text" and value["meme_id"] is None:
                return {
                    "text": await model.revise(current, decision),
                    "message_kind": "text",
                    "meme_id": None,
                }
            if value["kind"] != "image" or value["meme_id"] not in allowed:
                raise GenerationFeedback("修订图片须来自当前角色图库", original_output=value)
            return {"text": row["text"], "message_kind": "image", "meme_id": value["meme_id"]}

        def defer(prepared, state):
            prepared.guard()
            self.store.execute(
                "UPDATE dm_drafts SET status=?,updated_at=? WHERE id=?",
                ("rejected" if state == "abandoned" else "pending_review", time.time(), identity),
            )
            if state == "abandoned" and self.memes:
                self.memes.release(f"dm-draft:{identity}")

        return PublicationContext(
            binding,
            row["text"],
            character,
            {"source": self._event(row, character), "message_kind": row.get("message_kind", "text")},
            guard,
            payload=self._outgoing(row, character),
            encode_revision=encode,
            revision=revise_image if row.get("message_kind") == "image" else None,
            on_defer=defer,
            manual_approved=row["status"] == "approved",
        )

    def _settle_delivered_reply(self, draft, receipt):
        if draft["source_kind"] == "message":
            check = json.loads(draft.get("answer_check") or "{}")
            self._settle_incoming(
                draft, check.get("reply_incoming_ids") or [draft["source_id"]], receipt["msg_key"]
            )

    def _delivered_event(self, draft, character):
        event = self._event(draft, character)
        if draft["source_kind"] != "message":
            return event
        check = json.loads(draft.get("answer_check") or "{}")
        if ids := check.get("reply_incoming_ids"):
            rows = self.store.all(
                "SELECT id,message_id,content,sent_at,sequence FROM dm_inbox WHERE platform=? AND platform_user_id=? "
                "AND character_id=? AND account_id=? AND id IN (SELECT value FROM json_each(?))",
                (
                    draft["platform"],
                    draft["platform_user_id"],
                    draft["character_id"],
                    draft["account_id"],
                    json.dumps(ids),
                ),
            )
            if rows:
                rows = self._ordered_messages(rows)
                event["text"] = "\n".join(row["content"] for row in rows)
                event["reply_sources"] = [row["message_id"] for row in rows]
        return event

    async def _reconcile(self, identity, *, guard=None):
        feedback = FeedbackPublication(self.runtime, self.store, self.operations)
        if not feedback.coordinator.find("messages", identity):
            saved = self.store.one("SELECT image_submission FROM dm_attempts WHERE draft_id=? AND status='uncertain' "
                                   "ORDER BY id DESC LIMIT 1", (identity,))
            if saved and saved["image_submission"]:
                checkpoint = json.loads(saved["image_submission"])
                original = self.operations.get_attempt(checkpoint["token"])
                if original and original["binding"] == checkpoint["binding"]:
                    feedback.record_unknown("messages", identity, original["token"], original["binding"],
                                            checkpoint["payload"], "按原图文私信提交核对")

        async def read(row, attempt):
            adapter = await self._adapter()
            if not hasattr(adapter, "reconcile_publication"):
                return {"status": "unsupported"}
            try:
                if str(await adapter.self_uid()) != str(row["binding"]["account_id"]):
                    return {"status": "check_failed", "reason": "account_changed"}
            except (ValueError, AttributeError):
                return {"status": "check_failed", "reason": "account_unavailable"}
            payload = row["feedback"]["sent_payload"]
            target = row["binding"]["target"] | {"message_kind": payload.get("message_kind", "text")}
            if guard:
                guard(row, attempt)
            credential = dict(self.account.credentials)
            async def verify(kind, receipt):
                return await adapter.reconcile_publication(
                    target | {"message_kind": kind}, author_uid=row["binding"]["account_id"],
                    text=payload.get("text", ""), started_at=attempt["started_at"], receipt=receipt)

            if target["message_kind"] == "text_image":
                steps = {item["kind"]: json.loads(item["receipt"]) for item in self.store.all(
                    "SELECT s.kind,s.receipt FROM dm_delivery_steps s JOIN dm_attempts a ON a.id=s.attempt_id "
                    "WHERE a.draft_id=? AND s.write_token=?", (identity, attempt["token"]))}
                if not all(steps.get(kind, {}).get("msg_key") for kind in ("image", "text")):
                    return {"status": "unsupported", "reason": "图文私信尚未取得两条独立消息 ID"}
                checks = []
                for kind in ("image", "text"):
                    check = await verify(kind, steps[kind])
                    if check["status"] not in {"visible", "not_visible"}:
                        return check
                    if check["receipt"]["msg_key"] != steps[kind]["msg_key"]:
                        return {"status": "check_failed", "reason": "消息 ID 与原回执不符"}
                    checks.append(check["status"])
                ref = ({"asset_id": payload["meme_id"]} if "meme_id" in payload else {"artifact_id": payload["artifact_id"]})
                receipt = steps["text"] | {"message_kind": "text_image", "image_msg_key": steps["image"]["msg_key"],
                                          "image_ref": ref | {"sha256": payload["asset_sha256"]}}
                result = {"status": "not_visible" if "not_visible" in checks else "visible", "receipt": receipt}
            else:
                result = await verify(target["message_kind"], attempt["receipt"])
            if guard:
                if self.adapter is not adapter or self.account.credentials != credential:
                    raise ValueError("核对期间私信账号连接已变化")
                guard(row, attempt)
            return result

        def commit(receipt, verification):
            encoded = json.dumps(receipt, ensure_ascii=False)
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            with self.store.db:
                self.store.db.execute("UPDATE dm_drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                    (encoded, time.time(), identity))
                self.store.db.execute("UPDATE dm_attempts SET status='published',receipt=? WHERE id=(SELECT MAX(id) FROM dm_attempts WHERE draft_id=?)",
                    (encoded, identity))
                self._settle_delivered_reply(draft, receipt)
            if self.memes:
                self.memes.release(f"dm-draft:{identity}")
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            character = self.store.character(draft["character_id"], draft["character_version"])
            payload = feedback.coordinator.find("messages", identity)["feedback"]["sent_payload"]
            spoken = (
                f"[发送表情包：{payload['meme_id']}]"
                if payload.get("message_kind") == "image"
                else payload["text"]
            )
            check = json.loads(draft.get("answer_check") or "{}")
            self.runtime.operation_context.record(
                self.scope(draft),
                character,
                self._delivered_event(draft, character),
                None if "memory_preference_revision" in check else spoken,
                "direct_message",
                memory_kind=None if payload.get("message_kind") == "image" else check.get("memory_kind"),
            )

        return await feedback.reconcile("messages", identity, read, commit)

    async def send(self, identity, *, automatic=False, authorization_epoch=None, _conversation_owned=False, submission=None):
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft:
            raise ValueError("私信草稿不存在")
        if submission:
            submission.check()
        if draft["status"] == "publish_uncertain":
            reconciled = await self._reconcile(identity)
            if reconciled is None:
                raise ValueError("私信仍待核对，不会重发")
            return reconciled
        self.operations.assert_review_writable(
            draft["account_id"] or self.display_account_id() or self.operations.policy()["account_id"]
        )
        allowed = {"pending_review"} if automatic else {"approved", "copied"}
        if submission:
            allowed.add("pending_review")
        if draft["status"] not in allowed:
            raise ValueError("私信尚未审核、已发送或暂不满足联系条件，不能发送")
        if not _conversation_owned:
            scope = (draft["account_id"], draft["character_id"], "dm:" + draft["platform"], draft["platform_user_id"])
            async with self.runtime.conversation_turn(scope):
                return await self.send(identity, automatic=automatic, authorization_epoch=authorization_epoch,
                                       _conversation_owned=True, submission=submission)
        if automatic and row_ref(draft):
            await wait_decision(self.runtime, "messages", draft, lambda:
                                self.policy()["auto_send"] and self.operations.policy()["epoch"] == authorization_epoch)
        if not await self._refresh_conversation_inbox(draft):
            if submission:
                raise ValueError("原会话尚未同步完整，须重新核对")
            self._requeue_changed_conversation(draft, automatic)
            raise MessageInputChanged("私信历史仍在补齐，已保留进度，暂未发送")
        if submission:
            character = self.store.character(draft["character_id"], draft["character_version"])
            identity_now = await self.runtime.refresh_identity(character, draft["account_id"])
            if json.loads(draft.get("answer_check") or "{}").get("generation_identity") != identity_now:
                raise ValueError("账号身份已变化，请重新准备并审核私信")
            submission.check()
            return await self._send_once(identity, submission=submission)
        if self._merge_incoming(identity, automatic=automatic):
            if not self.queue.full():
                self._schedule(identity, automatic)
            raise MessageInputChanged("新来信已合并，重新准备回复后发送")
        try:
            self._check_conversation(draft, json.loads(draft.get("answer_check") or "{}").get("conversation_sha256"),
                                     automatic=automatic)
        except MessageInputChanged:
            if not self.queue.full():
                self._schedule(identity, automatic)
            raise
        character = self.store.character(draft["character_id"], draft["character_version"])
        observed = await self.runtime.refresh_identity(character, draft["account_id"])
        self._require_unchanged_draft(draft)
        previous = json.loads(draft.get("answer_check") or "{}").get("generation_identity")
        if observed.get("account_uid") != (draft["account_id"] or observed.get("account_uid")):
            raise ValueError("当前账号已变化，不能迁移或发送旧私密对话")
        if previous != observed:
            approved = draft["status"] in {"approved", "copied"}
            self.store.execute("UPDATE dm_drafts SET status='queued',proposed_text='',outreach_check=NULL,error=NULL WHERE id=?", (identity,))
            await self._generate_owned(identity, False, preserve_attachments=True)
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if draft["status"] != "pending_review":
                return self._draft_view(draft)
            if approved and not json.loads(draft.get("answer_check") or "{}").get("output", {}).get("needs_review"):
                self.review(identity, "approve")
        return await FeedbackPublication(self.runtime, self.store, self.operations).submit(
            "messages",
            identity,
            prepare=lambda: self._publication_context(identity, automatic, authorization_epoch),
            send=lambda ticket, binding, body_hash: self._send_once(
                identity,
                automatic=automatic,
                authorization_epoch=authorization_epoch,
                revision_ticket=ticket,
                binding=binding,
                body_hash=body_hash,
            ),
            apply_revision=lambda prepared, value: self._apply_revision(identity, prepared, value),
            automatic=automatic,
            epoch=authorization_epoch,
            reconcile=lambda token, prepared: self._reconcile(identity),
        )

    async def _send_once(
        self,
        identity,
        *,
        automatic=False,
        authorization_epoch=None,
        revision_ticket=None,
        binding=None,
        body_hash=None,
        submission=None,
    ):
        destination = self.store.one("SELECT account_id,platform,platform_user_id FROM dm_drafts WHERE id=?", (identity,))
        if not destination:
            raise ValueError("私信草稿不存在")
        async with self.runtime.conversation_turn(("dm_send", destination["account_id"],
                                                   destination["platform"], destination["platform_user_id"])):
            draft = self.store.one("SELECT account_id FROM dm_drafts WHERE id=?", (identity,))
            if draft:
                self.operations.assert_review_writable(
                    draft["account_id"] or self.display_account_id() or self.operations.policy()["account_id"]
                )
            if not submission and self._merge_incoming(identity, automatic=automatic):
                if not self.queue.full():
                    self._schedule(identity, automatic)
                raise MessageInputChanged("发送前收到新来信，已合并重新准备")
            self._enabled()
            policy = self.policy()
            if automatic and not policy["auto_send"]:
                raise ValueError("自动发送私信已关闭，保留草稿")
            row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            allowed = {"pending_review"} if automatic else {"approved", "copied"}
            if submission:
                submission.check()
                allowed.add("pending_review")
            if not row or row["status"] not in allowed:
                raise ValueError("私信尚未审核、已发送或结果待核对，不能发送")
            if automatic and json.loads(row["answer_check"] or "{}").get("output", {}).get("needs_review"):
                raise ValueError("正文诊断尚待审核，未自动发送")
            if automatic and row["source_kind"] == "comment" and self._direct_waiting(row):
                raise SendLimitReached("等待同一收件人的直接来信处理", time.time() + 1)
            character = self.store.character(row["character_id"], row["character_version"])
            source = self._source(row["source_kind"], row["source_id"], character)
            if source["platform_user_id"] != row["platform_user_id"] or source["platform"] != row["platform"]:
                raise ValueError("来源用户与私信收件人不一致")
            if row["platform"] != "bilibili" and not self.injected:
                raise ValueError("该平台尚未接入私信发送")
            if row["source_kind"] == "comment" and not policy["allow_comment_handoff"]:
                raise ValueError("评论转私信已关闭")
            if (
                automatic
                and row["source_kind"] == "message"
                and source["sent_at"]
                < self._inbox_cutoff(row["account_id"])
                and not self._recovered_incoming(source["id"])
            ):
                raise ValueError("历史来信不自动追发，保留草稿")
            # The account ledger applies the channel quota and cooldown atomically.
            adapter = await self._adapter()
            account_id = (
                str(source.get("account_id") or "injected-test-account")
                if self.injected
                else str(await adapter.self_uid())
            )
            if row["account_id"] and row["account_id"] != account_id:
                raise ValueError("该私信草稿属于其他账号，不能发送")
            if not self.injected and not row["account_id"]:
                self.store.execute(
                    "UPDATE dm_drafts SET status='failed',error='旧草稿未绑定账号，请重新生成并审核' WHERE id=?",
                    (identity,),
                )
                raise ValueError("旧草稿未绑定账号，请重新生成并审核；不会复用旧私密上下文")
            if row["source_kind"] == "message" and not self.injected:
                if not source["account_id"] or account_id != source["account_id"]:
                    raise ValueError("当前登录账号与收到该私信的账号不一致，不能回复")
            browsing = getattr(self.runtime, "browsing", None)
            fresh_thread = None
            if browsing:
                fresh_thread = await self._refresh_outreach_thread(row)
                if fresh_thread is None:
                    fresh_thread = await browsing.validate_private_source(row)
            if row["source_kind"] == "comment":
                event = self._event(row, character)
                if fresh_thread is not None:
                    event["public_source_thread"] = fresh_thread
                if not self._check_outreach(row, event):
                    return self._draft_view(self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,)))
            fresh = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            reviewed_fields = (
                "status",
                "text",
                "message_kind",
                "meme_id",
                "image_ref",
                "character_id",
                "character_version",
                "account_id",
                "platform",
                "platform_user_id",
                "source_kind",
                "source_id",
            )
            if not fresh or any(fresh.get(field) != row.get(field) for field in reviewed_fields):
                raise ValueError("准备期间私信草稿已变化，保留当前审核状态；请重新确认后发送")
            actual_binding = self._publication_binding(row, source, account_id, character)
            payload = self._outgoing(row, character)
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash(payload)
            ):
                raise ValueError("发送前私信原始绑定或内容已变化")
            image = None
            composite = payload.get("message_kind") == "text_image"
            if composite:
                ref = image_decision(self.runtime, "messages", row, row_ref(row))
                image = checked_image(self.memes, ref, character, runtime=self.runtime, scope=image_scope("messages", row))
                if not isinstance(adapter, BilibiliMessagesAdapter):
                    raise ValueError("当前平台未提供图文私信接口")
            elif row.get("message_kind") == "image":
                if not self.memes or not row.get("meme_id"):
                    raise ValueError("图片草稿缺少已登记表情")
                image = self.memes.read(row["meme_id"], character)
                ref = {"asset_id": row["meme_id"], "sha256": image["asset"]["sha256"]}
                if not hasattr(adapter, "send_image"):
                    raise ValueError("当前平台未提供图片私信接口，保留草稿")
            previous, now = row["status"], time.time()
            token = None
            credential = dict(self.account.credentials)
            if submission:
                submission.check()
            attempt = self.store.execute(
                "INSERT INTO dm_attempts(draft_id,platform_user_id,started_at,status) VALUES(?,?,?,'sending')",
                (identity, row["platform_user_id"], now),
            ).lastrowid
            self.store.execute(
                "UPDATE dm_drafts SET status='publishing',updated_at=? WHERE id=?", (now, identity)
            )
            try:

                def admit(ticket):
                    if submission:
                        submission.admit(ticket, actual_binding, payload, attempt)
                    if composite:
                        self.store.db.execute("UPDATE dm_attempts SET image_submission=? WHERE id=?",
                                              (json.dumps({"token": ticket, "binding": actual_binding,
                                                           "payload": payload}), attempt))

                def authorized(*, reserve=True):
                    nonlocal token
                    if submission:
                        submission.check(publishing=True)
                    audit = json.loads(row.get("answer_check") or "{}")
                    if not image or composite:
                        self.runtime.validate_memory_delivery(audit)
                    generated = audit.get("generation_identity")
                    if not self._conversation_matches(row, audit.get("conversation_sha256")):
                        raise MessageInputChanged("会话出现新发言，暂未提交私信")
                    if generated != runtime_identity(self.runtime, character, row["account_id"]):
                        raise MessageIdentityChanged("提交前账号资料已更新，按最新身份重新准备")
                    if not submission and row["source_kind"] == "message" and self._newer_incoming(row, source):
                        raise MessageInputChanged("提交前收到新来信，重新合并回复")
                    policy = self.policy()
                    if image and not composite:
                        asset = self.memes.resolve(row["meme_id"], character)
                        if asset["sha256"] != image["asset"]["sha256"]:
                            return False
                    if composite:
                        checked_image(self.memes, ref, character, runtime=self.runtime, scope=image_scope("messages", row))
                    latest = self.store.one(
                        "SELECT * FROM dm_drafts WHERE id=?", (identity,)
                    )
                    if (
                        not latest
                        or latest["status"] != "publishing"
                        or any(latest[k] != row[k] for k in reviewed_fields if k != "status")
                    ):
                        return False
                    if automatic:
                        self._origin_round(row, active=True)
                    browsing = getattr(self.runtime, "browsing", None)
                    if automatic and row["source_kind"] == "comment" and browsing:
                        browsing.assert_comment_evidence(self._event(row, character))
                    if not self.injected and self.credential_signature != (
                        self.account.credentials.get("sessdata"),
                        self.account.credentials.get("csrf"),
                    ):
                        return False
                    if (
                        not (
                            policy["enabled"]
                            and (not automatic or policy["auto_send"])
                            and (row["source_kind"] != "comment" or policy["allow_comment_handoff"])
                        )
                        or self.account.credentials != credential
                    ):
                        return False
                    fresh = self._source(row["source_kind"], row["source_id"], character)
                    if (
                        fresh["content"] != source["content"]
                        or fresh["platform_user_id"] != row["platform_user_id"]
                    ):
                        return False
                    self.operations.assert_allowed(
                        automatic=automatic,
                        channel="messages",
                        account_id=account_id,
                        character=character,
                        epoch=authorization_epoch,
                    )
                    intent = canonical_intent(
                        row["platform"],
                        source["video_id"] if row["source_kind"] == "comment" else "dm",
                        source["comment_id"] if row["source_kind"] == "comment" else source["message_id"],
                        character["id"],
                    )
                    if automatic and browsing:
                        browsing.sends.assert_ready("messages", identity)
                    if reserve and token is None:
                        token = self.operations.reserve(
                            account_id,
                            character,
                            intent,
                            "messages",
                            recipient=row["platform_user_id"],
                            automatic=automatic,
                            limits=policy,
                            epoch=authorization_epoch,
                            binding=actual_binding,
                            content_hash=content_hash(payload),
                            revision_ticket=revision_ticket,
                            admission_hook=admit,
                        )
                    return True

                if image:
                    options = {}
                    if isinstance(adapter, BilibiliMessagesAdapter):
                        options["on_uploaded"] = lambda pic: record_upload(self.store, "messages", identity, ref, pic)
                        if composite:
                            options["expected_sender"] = account_id
                    receipt = await adapter.send_image(
                        row["platform_user_id"],
                        image,
                        authorized=authorized,
                        prepare_authorized=lambda: authorized(reserve=False),
                        **options,
                    )
                else:
                    receipt = await adapter.send(row["platform_user_id"], row["text"], authorized=authorized)
                if not isinstance(receipt, dict) or not receipt.get("msg_key"):
                    raise PublishUncertain("平台没有返回有效消息ID")
                if composite:
                    image_receipt = receipt
                    text_submitted = False

                    def step(kind, value):
                        self.store.execute(
                            "INSERT INTO dm_delivery_steps VALUES(?,?,?,?) ON CONFLICT(attempt_id,kind) "
                            "DO UPDATE SET receipt=excluded.receipt", (attempt, kind, json.dumps(value), token))

                    def authorize_text():
                        nonlocal text_submitted
                        if not authorized():
                            return False
                        step("text", {"status": "submitting"})
                        text_submitted = True
                        return True

                    try:
                        step("image", image_receipt)
                        receipt = await adapter.send(row["platform_user_id"], row["text"], authorized=authorize_text,
                                                     expected_sender=account_id)
                        if not receipt.get("msg_key") or receipt["msg_key"] == image_receipt["msg_key"]:
                            raise ValueError("图文回执须有两个独立消息 ID")
                        step("text", receipt)
                    except (Exception, asyncio.CancelledError) as error:
                        state = "not_submitted" if not text_submitted else (
                            "rejected" if isinstance(error, PlatformRejected) else "uncertain")
                        step("text", {"status": state})
                        raise PublishUncertain("图片已送达，正文发送结果待核对") from error
                    receipt = receipt | {
                        "message_kind": "text_image", "image_ref": ref, "image_msg_key": image_receipt["msg_key"],
                    }
                elif image:
                    receipt = receipt | {
                        "message_kind": "image", "meme_id": row["meme_id"], "asset_sha256": image["asset"]["sha256"],
                    }
            except MessageInputChanged as error:
                if token:
                    self.operations.finish(token, "rejected")
                self.store.execute("UPDATE dm_drafts SET status=? WHERE id=?", (previous, identity))
                if submission:
                    self.store.execute("UPDATE dm_attempts SET status='cancelled',error='会话已变化，未提交' WHERE id=?", (attempt,))
                    raise
                if isinstance(error, MessageIdentityChanged):
                    self.store.execute("UPDATE dm_drafts SET status='queued',proposed_text='',outreach_check=NULL,resume_pending=1,error=NULL WHERE id=?", (identity,))
                elif not self._merge_incoming(identity, automatic=automatic):
                    self._requeue_changed_conversation(row, automatic)
                if not self.queue.full():
                    self._schedule(identity, automatic)
                self.store.execute("UPDATE dm_attempts SET status='cancelled',error='新来信已合并，未提交' WHERE id=?", (attempt,))
                raise
            except MessagePreparationCancelled:
                if token:
                    self.operations.finish(token, "rejected")
                self.store.execute(
                    "UPDATE dm_drafts SET status=?,error=?,updated_at=? WHERE id=?",
                    (previous, "图片或消息准备已取消，未提交私信", time.time(), identity),
                )
                self.store.execute("UPDATE dm_attempts SET status='failed',error='准备已取消，未提交私信' WHERE id=?", (attempt,))
                raise
            except (PublishUncertain, asyncio.CancelledError) as error:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "messages", identity, token, actual_binding, payload, "私信发送结果不确定"
                    )
                self._uncertain(identity, attempt)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise UnknownPublication("私信发送结果待核对", token) from None
            except ValueError as error:
                if token:
                    self.operations.finish(token, "rejected")
                self.store.execute(
                    "UPDATE dm_drafts SET status=?,error=?,updated_at=? WHERE id=?",
                    (previous, public_error(error), time.time(), identity),
                )
                self.store.execute("UPDATE dm_attempts SET status='failed',error=? WHERE id=?", (public_error(error), attempt))
                if isinstance(error, PlatformRejected):
                    error.write_token = token
                    error.body_hash = content_hash(payload)
                    error.sent_payload = payload
                raise
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain")
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "messages", identity, token, actual_binding, payload, "私信发送结果不确定"
                    )
                self._uncertain(identity, attempt)
                raise UnknownPublication("私信发送结果待核对", token) from None
            try:
                encoded = json.dumps(receipt, ensure_ascii=False)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dm_drafts SET status='published',receipt=?,error=NULL,updated_at=? WHERE id=?",
                        (encoded, time.time(), identity),
                    )
                    if composite:
                        self.store.db.execute("UPDATE dm_drafts SET message_kind='text_image',image_ref=? WHERE id=?",
                                              (json.dumps(ref), identity))
                    self.store.db.execute(
                        "UPDATE dm_attempts SET status='published',receipt=? WHERE id=?", (encoded, attempt)
                    )
                    self._settle_delivered_reply(row, receipt)
                if token:
                    self.operations.finish(token, "published", receipt=receipt)
            except Exception:
                if token:
                    self.operations.finish(token, "uncertain", receipt=receipt)
                    FeedbackPublication(self.runtime, self.store, self.operations).record_unknown(
                        "messages", identity, token, actual_binding, payload, "平台成功后本地保存失败"
                    )
                self._uncertain(identity, attempt)
                raise UnknownPublication("B 站已受理私信，本地回执待补录", token) from None
            check = json.loads(row.get("answer_check") or "{}")
            if image and self.memes:
                self.memes.release(f"dm-draft:{identity}")
            spoken = f"[发送表情包：{image['asset']['title']}]" if image and not composite else row["text"]
            self.runtime.operation_context.record(
                self.scope(row),
                character,
                self._delivered_event(row, character),
                None if "memory_preference_revision" in check else spoken,
                "direct_message",
                memory_kind=None if image and not composite else check.get("memory_kind"),
            )
            return {"status": "published", "receipt": receipt}

    def _uncertain(self, identity, attempt):
        self.store.execute(
            "UPDATE dm_drafts SET status='publish_uncertain',error='私信发送结果待核对',updated_at=? WHERE id=?",
            (time.time(), identity),
        )
        self.store.execute(
            "UPDATE dm_attempts SET status='uncertain',error='发送结果待核对' WHERE id=?", (attempt,)
        )
