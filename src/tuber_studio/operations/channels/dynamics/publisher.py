"""Dynamic review, Bilibili publication/editing, reconciliation, and Dynamics channel facade."""

import asyncio
import hashlib
import json
import re
import time

import httpx

from ....core.errors import PlatformRejected, UnknownPublication
from ....core.models import content_hash
from ....core.text import operation_output_diagnostics
from ....infra.platforms.bilibili.client import PublishUncertain
from ....infra.platforms.bilibili.dynamics import (
    PublishCancelled,
    PublishPreparationCancelled,
    PublishPreparationFailed,
    edit_dynamic_caption,
    publish_image,
    publish_text,
    read_dynamic_caption,
    validate_text,
)
from ....infra.platforms.bilibili.images import bfs_image_path
from ....persona.memory.daily import DailyMemoryService
from ...kernel.control import OperationsControl, canonical_intent
from ...kernel.feedback import FeedbackPublication, PublicationContext, publication_binding
from ...kernel.memory import OperationsMemory
from ..attachments import (
    image_payload,
    image_scope,
    record_upload,
    wait_decision,
)
from ..mentions import bind_mentions, rebase_mentions
from .attachments import DynamicsAttachmentsMixin
from .comment_card import render_comment_card
from .drafter import DynamicsDrafterMixin
from .evaluator import DynamicsEvaluatorMixin
from .repository import DynamicsRepositoryMixin, _content_hash, _delivery_key, _json


class Dynamics(
    DynamicsRepositoryMixin,
    DynamicsAttachmentsMixin,
    DynamicsEvaluatorMixin,
    DynamicsDrafterMixin,
):
    """Public-source-only character operations; scheduling is separate from live jobs."""

    def __init__(self, settings, store, runtime, comments, *, account=None, memes=None, is_live_busy=None):
        self.settings, self.store, self.runtime, self.comments = settings, store, runtime, comments
        self.is_live_busy = is_live_busy or (lambda: False)
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
        self._init_storage(store)

    def review(self, post_id, action, text=None, **options):
        row = self._post(post_id)
        if action not in {"approve", "reject", "edit", "copy", "reselect_image"} or row["status"] not in {
            "pending_review",
            "approved",
            "rejected",
            "publish_failed",
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
            if isinstance(illustration, dict) and illustration.get("asset_id"):
                if not self.memes:
                    raise ValueError("表情组件尚未启用")
                asset = self.memes.resolve(illustration["asset_id"], row["character_id"])
                if asset.get("usage_scope", "all_channels") != "all_channels":
                    raise ValueError("该表情仅限互动渠道（评论/私信），不能用于公开动态")
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
            resolved = self.memes.resolve(meme_id, row["character_id"])
            if resolved.get("usage_scope", "all_channels") != "all_channels":
                raise ValueError("该表情仅限互动渠道（评论/私信），不能用于公开动态")
            self.memes.pin(meme_id, row["character_id"], owner)
        status = {"approve": "approved", "reject": "rejected", "edit": "pending_review"}[action]
        bound_ref = illustration if action == "approve" else row["image_ref"]
        if "meme_id" in options:
            bound_ref = None
            if meme_id:
                asset = self.memes.resolve(meme_id, row["character_id"])
                if asset.get("usage_scope", "all_channels") != "all_channels":
                    raise ValueError("该表情仅限互动渠道（评论/私信），不能用于公开动态")
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

    @classmethod
    def _outgoing_text(cls, row, text, *, link_source_ids=None):
        if links := cls._comment_links(row, link_source_ids=link_source_ids):
            text += "\n\n" + "\n".join(("原评论：" if "#reply" in url else "") + url for url in links)
        return text

    @classmethod
    def _edit_caption(cls, row, text, *, link_source_ids=None):
        text = validate_text(text)
        if links := cls._comment_links(row, link_source_ids=link_source_ids):
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
            illustration = row["illustration"]
            if isinstance(illustration, dict) and illustration.get("asset_id") and self.memes:
                asset = self.memes.resolve(illustration["asset_id"], character)
                if asset.get("usage_scope", "all_channels") != "all_channels":
                    raise ValueError("该表情仅限互动渠道（评论/私信），不能用于公开动态")
            value.update(image_payload(illustration))
        elif row.get("illustration_choice") == "awaiting_choice":
            raise ValueError("配图待决定，请重新选图或使用纯文字")
        elif row.get("meme_id"):
            if not self.memes:
                raise ValueError("配图组件未挂载")
            asset = self.memes.resolve(row["meme_id"], character)
            if asset.get("usage_scope", "all_channels") != "all_channels":
                raise ValueError("该表情仅限互动渠道（评论/私信），不能用于公开动态")
            value |= {"meme_id": row["meme_id"], "asset_sha256": asset["sha256"]}
        return value

    async def resolve_review_mentions(self, post_id, text, requests):
        row = self._post(post_id)
        participants = [person for source in row["sources"] for person in source.get("participants", [])]
        mentions, pending = await bind_mentions(self.account.adapter, validate_text(text), requests, participants)
        return {"mentions": mentions, "pending_mentions": pending}

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
                if image and not card:
                    asset_id = row.get("meme_id") or (
                        row["illustration"].get("asset_id")
                        if isinstance(row.get("illustration"), dict)
                        else None
                    )
                    if asset_id:
                        try:
                            current_asset = self.memes.resolve(asset_id, character)
                            if current_asset["sha256"] != image["asset"]["sha256"]:
                                raise ValueError("配图原文已变化")
                            if current_asset.get("usage_scope", "all_channels") != "all_channels":
                                raise ValueError("该表情已改为仅限互动渠道，不能用于公开动态")
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
                if edit.get("link_source_ids") is not None:
                    presentation = (presentation or {}) | {
                        "link_source_ids": edit["link_source_ids"],
                        "mentions": mentions,
                        "points": [],
                    }
                elif presentation is not None or mentions:
                    presentation = (presentation or {"link_source_ids": None}) | {"mentions": mentions, "points": []}
                edited = self._outgoing(
                    row | {"content": edit["caption"], "mentions": mentions, "presentation": presentation},
                    character,
                )
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
