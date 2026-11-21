"""Browsing intent authorization, submission and read-only reconciliation."""

import asyncio
import json
import math
import time
from contextlib import nullcontext
from uuid import uuid4

from ...comment_materials import model_comment_materials, validate_comment_material_sources
from ...operation_attachments import (
    checked_image,
    comment_image,
    comment_payload,
    decision,
    image_scope,
    queue_image,
    record_upload,
    require_comment_attachment,
    row_ref,
    wait_decision,
)
from ...operation_continuations import content_hash
from ...operation_sends import current_send
from ...operation_feedback import (
    FeedbackPublication,
    PublicationContext,
    publication_binding,
)
from ...operation_generation import generate_with_recovery, generation_requires_review
from ...operation_identity import runtime_identity
from ...operation_text import (
    comment_body_issue,
    validate_comment_body,
    validate_operation_text,
)
from ...operations_control import canonical_intent
from ...platforms.bilibili import PlatformReadError, PublishUncertain
from ...providers import public_error
from ...publication_errors import PlatformRejected, UnknownPublication
from ...store import dump
from .comment_timeline import observe_thread, resource_key
from .inputs import ResponseDecisionInput
from .models import RESPONSE_INSTRUCTION, ResponseDecision
from .round_context import read_round
from .thread_context import comment_source, comment_thread_context, operator_reply, read_scoped_thread

_COMMENT_KINDS = {"top_level", "reply_public"}
_ENGAGEMENT_CHANNELS = {"video_like": "video_likes", "video_fav": "video_favorites",
                        "comment_like": "comment_likes", "dynamic_like": "dynamic_likes"}
_ENGAGEMENT_KINDS = _ENGAGEMENT_CHANNELS.keys()


class BrowsingPublication:
    def __init__(self, browsing):
        self.browsing = browsing
        browsing.store.execute("""CREATE TABLE IF NOT EXISTS browsing_reply_coverage(
            account_id TEXT,character_id TEXT,source_key TEXT,intent_id TEXT NOT NULL REFERENCES browsing_intents(id) ON DELETE CASCADE,
            PRIMARY KEY(account_id,character_id,source_key))""")

    def source_intent(self, row, key):
        scope = (row["account_id"], row["character_id"], key)
        store = self.browsing.store
        return store.one(
            "SELECT id,status FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=? "
            "UNION ALL SELECT i.id,i.status FROM browsing_reply_coverage c JOIN browsing_intents i ON i.id=c.intent_id "
            "WHERE c.account_id=? AND c.character_id=? AND c.source_key=? "
            "AND i.status IN ('pending_review','approved','publishing','publish_uncertain','published') LIMIT 1",
            (*scope, *scope),
        )

    @staticmethod
    def _comment_like_key(target):
        resource_type = target.get("resource_type") or ("video" if target.get("bvid") else "dynamic")
        return canonical_intent("bilibili", f"{resource_type}:{target['oid']}", target["parent"], "comment_like")

    def comment_like_intent(self, row, target):
        return self.browsing.repo.decode(self.browsing.store.one(
            "SELECT * FROM browsing_intents WHERE account_id=? AND source_key=? AND kind='comment_like' "
            "AND (status NOT IN ('pending_review','approved','rejected') OR character_id=?) "
            "ORDER BY created_at LIMIT 1",
            (row["account_id"], self._comment_like_key(target), row["character_id"]),
        ))

    @staticmethod
    def _dynamic_like_key(identifier):
        return canonical_intent("bilibili", "dynamic:" + identifier, "like", "dynamic_like")

    def dynamic_like_intent(self, row, identifier):
        return self.browsing.store.one(
            "SELECT id FROM browsing_intents WHERE account_id=? AND source_key=? AND status<>'archived' LIMIT 1",
            (row["account_id"], self._dynamic_like_key(identifier)),
        )

    def _reserve_daily_reply(self, token, row):
        bvid = row["target"].get("bvid")
        if row["kind"] not in {"top_level", "reply_public"} or not bvid:
            return
        policy = self.browsing.policy()
        self.browsing.runtime.browsing_quotas.reserve_reply(
            token,
            row["account_id"],
            row["character_id"],
            bvid,
            row["source_key"],
            policy["max_replies_per_video"],
            policy["business_timezone"],
        )

    def _intent(self, row, kind, target, response, material_keys, evidence_ids, *, transaction_owned=False):
        character = self.browsing.store.character(row["character_id"], row["character_version"])
        target = target | {"generation_identity": getattr(response, "_generation_identity", None)
                           or runtime_identity(self.browsing.runtime, character, row["account_id"])}
        if response._generation_id:
            target["generation_continuation_id"] = response._generation_id
        if response._memory_audit:
            self.browsing.runtime.validate_memory_delivery(response._memory_audit)
            target["memory_audit"] = response._memory_audit
        resource = target.get("bvid") or "dynamic:" + str(target["oid"])
        if kind == "top_level":
            source = "top-level"
        elif kind == "video_like":
            source = "like"
        elif kind == "video_fav":
            source = "favorite"
        else:
            source = str(target.get("parent", "0"))
        source_key = (self._comment_like_key(target) if kind == "comment_like"
                      else self._dynamic_like_key(target["dynamic_id"]) if kind == "dynamic_like"
                      else canonical_intent("bilibili", resource, source, row["character_id"]))
        if kind == "reply_public" and (
            target.get("bvid")
            and self.browsing.repo.video_reply_limit_reached(row, target["bvid"])
            or self.source_intent(row, source_key)
        ):
            return None
        if kind == "top_level":
            def public_material():
                segments = []
                for key in material_keys:
                    evidence = self.browsing.evidence.load(key)
                    if evidence["metadata"]["bvid"] == target["bvid"]:
                        segments.extend({k: s[k] for k in ("id", "cid", "start", "end", "text") if k in s}
                                        for s in self.browsing._segments(evidence) if s["id"] in evidence_ids)
                return {"bvid": target["bvid"], "public_comment": target.get("source_text", ""), "evidence": segments}

            image_ref = queue_image(self.browsing.runtime, "browsing", row | {"kind": kind, "target": target}, public_material, response.text)
            if image_ref:
                target = target | {"image_ref": image_ref}
        identity, now = str(uuid4()), time.time()
        body_issue = (
            comment_body_issue(response.text, recipient_name=target.get("recipient_name") or "")
            if kind in _COMMENT_KINDS else None
        )
        auto = (
            not row["preview"]
            and not body_issue
            and self.browsing.operations.policy()["enabled"]
            and self.browsing.operations.policy()["epoch"] == row["epoch"]
            and self._action_allowed(kind, self.browsing.effective())
            and not generation_requires_review(
                self.browsing.store, getattr(response, "_generation_id", None)
            )
        )
        store = self.browsing.store
        covered_ids = response.covered_comments if kind == "reply_public" else []
        coverage = [canonical_intent("bilibili", resource, value, row["character_id"])
                    for value in covered_ids if value != source]
        if covered_ids:
            target["covered_comment_ids"] = covered_ids
        with nullcontext() if transaction_owned else store.db:
            if coverage:
                if not transaction_owned:
                    store.db.execute("BEGIN IMMEDIATE")
                if any(self.source_intent(row, key) for key in [source_key, *coverage]):
                    return None
            inserted = store.db.execute(
                "INSERT OR IGNORE INTO browsing_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    row["account_id"],
                    row["character_id"],
                    row["character_version"],
                    source_key,
                    kind,
                    row["id"],
                    row["epoch"],
                    int(auto),
                    "private_handoff" if kind == "private_handoff" else "pending_review",
                    "" if kind == "private_handoff" else response.text.strip(),
                    response.explanation.strip() or response.reason,
                    dump(target),
                    dump(sorted(evidence_ids)),
                    dump(material_keys),
                    dump([self.browsing.evidence.load(k)["source_version"] for k in material_keys]),
                    None,
                    "not_checked",
                    body_issue,
                    now,
                    now,
                ),
            )
            if inserted.rowcount:
                store.db.executemany(
                    "INSERT INTO browsing_reply_coverage VALUES(?,?,?,?) "
                    "ON CONFLICT(account_id,character_id,source_key) DO UPDATE SET intent_id=excluded.intent_id",
                    [(row["account_id"], row["character_id"], key, identity) for key in coverage],
                )
        return self.browsing.repo.decode(
            self.browsing.store.one(
                "SELECT * FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (row["account_id"], row["character_id"], source_key),
            )
        )

    def has_intent(self, comment, character):
        key = canonical_intent(
            comment["platform"], comment["video_id"], comment["comment_id"], character["id"]
        )
        account = self.browsing.account or self.browsing.operations.policy()["account_id"]
        return bool(self.source_intent({"account_id": account, "character_id": character["id"]}, key))

    def has_manual_draft(self, row, resource, comment_id):
        return bool(self.browsing.store.one(
            "SELECT d.id FROM drafts d JOIN comments c ON c.id=d.comment_id "
            "WHERE c.platform='bilibili' AND c.video_id=? AND c.comment_id=? "
            "AND d.account_id=? AND d.character_id=?",
            (resource, str(comment_id), row["account_id"], row["character_id"]),
        ))

    def review(self, identity, action, text=None, **options):
        row = self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        if (
            not row
            or row["status"] not in {"pending_review", "approved"}
            or not isinstance(action, str)
            or action not in {"approve", "reject", "edit", "save"}
        ):
            raise ValueError("当前候选不能审核")
        if text is not None and not isinstance(text, str):
            raise ValueError("正文须为字符串")
        text = row["text"] if text is None else text
        text = "" if row["kind"] in {"comment_like", "dynamic_like"} else validate_operation_text(text)
        issue = None
        if row["kind"] in _COMMENT_KINDS:
            recipient_name = json.loads(row["target"]).get("recipient_name") or ""
            issue = comment_body_issue(text, recipient_name=recipient_name)
            if issue and action == "approve":
                raise ValueError(issue)
        target = json.loads(row["target"])
        expected = options.pop("updated_at", None)
        if "expected_image" in options and options.pop("expected_image") != decision(
            self.browsing.runtime, "browsing", row | {"target": target}, target.get("image_ref")
        ):
            raise ValueError("配图已变化，请核对当前候选")
        if expected is not None and expected != row["updated_at"]:
            raise ValueError("候选版本已变化，请刷新")
        if action == "approve" and target.get("image_ref"):
            ref = decision(self.browsing.runtime, "browsing", row | {"target": target}, target["image_ref"])
            if ref:
                require_comment_attachment("browsing", row | {"target": target}, ref)
                checked_image(self.browsing.comments.memes, ref, row["character_id"], runtime=self.browsing.runtime,
                              scope=image_scope("browsing", row | {"target": target}))
                target["image_ref"] = ref
            else:
                target.pop("image_ref")
        if options:
            if action != "edit" or set(options) != {"image_ref"} or row["kind"] not in _COMMENT_KINDS:
                raise ValueError("配图变更须保存为待审的评论")
            if options["image_ref"] is None:
                target.pop("image_ref", None)
            else:
                require_comment_attachment("browsing", row | {"target": target}, options["image_ref"])
                checked_image(self.browsing.comments.memes, options["image_ref"], row["character_id"],
                              runtime=self.browsing.runtime, scope=image_scope("browsing", row | {"target": target}))
                target["image_ref"] = options["image_ref"]
        new_status = (
            ("pending_review" if issue or options or text != row["text"] else row["status"])
            if action in {"edit", "save"}
            else ("approved" if action == "approve" else "rejected")
        )
        self.browsing.store.execute(
            "UPDATE browsing_intents SET status=?,text=?,target=?,auto_authorized=0,error=?,updated_at=? WHERE id=?",
            (new_status, text, dump(target), issue if row["kind"] in _COMMENT_KINDS else row["error"],
             max(time.time(), math.nextafter(row["updated_at"], math.inf)), identity),
        )
        self.browsing.sends.withdraw("browsing", identity)
        return self.browsing.repo.decode(
            self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        )

    def _publication_binding(self, row, character, metadata):
        target = row["target"]
        source_text = target.get("source_text")
        source = (
            source_text
            if source_text is not None
            else [{key: item.get(key) for key in ("bvid", "aid")} for item in metadata]
        )
        return publication_binding(
            _ENGAGEMENT_CHANNELS.get(row["kind"], "comments"),
            row["account_id"],
            character,
            row["source_key"],
            {
                "platform": "bilibili",
                "resource_type": target.get("resource_type", "video"),
                **({"type": target["type"]} if target.get("type") == 11 else {}),
                "oid": str(target["oid"]),
                "root": str(target.get("root") or "0"),
                "parent": str(target.get("parent") or "0"),
                "recipient_uid": str(target.get("recipient") or ""),
            },
            {
                "kind": row["kind"],
                "local_id": row["id"],
                "platform_id": str(target.get("dynamic_id") if row["kind"] == "dynamic_like" else target.get("parent") or target["oid"]),
                "content_sha256": content_hash(source),
                **({"covered_comment_ids": target["covered_comment_ids"]} if target.get("covered_comment_ids") else {}),
                **({"discovery": True} if self._has_legacy_binding(row) else {}),
            },
        )

    def _has_legacy_binding(self, intent):
        parent = self.browsing.repo.round(intent["round_id"])
        return (
            parent["config"]["discovery_id"] if parent and parent["config"].get("discovery_publish") else None
        )

    @staticmethod
    def _action_allowed(kind, config):
        if kind == "private_handoff":
            return config.get("comment_handoff_enabled", False)
        keys = {
            "top_level": ("top_level_enabled", "top_level_auto_publish"),
            "reply_public": ("reply_enabled", "reply_auto_publish"),
            "comment_like": ("comment_like_enabled", "comment_like_auto_publish"),
            "dynamic_like": ("dynamic_like_enabled", "dynamic_like_auto_publish"),
            "video_like": ("video_like_enabled", "video_like_auto_publish"),
            "video_fav": ("video_fav_enabled", "video_fav_auto_publish"),
        }.get(kind)
        return bool(keys and all(config.get(key, False) for key in keys))

    def _intent_materials(self, row):
        return [self.browsing.evidence.load(key, allow_expired=True) for key in row["material_keys"]]

    async def _publication_context(self, identity, automatic):
        await self._refresh_intent_identity(identity, automatic)
        row = self.browsing.repo.decode(
            self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        )
        if not row:
            raise ValueError("候选不存在")
        account = await self.browsing._current_account(row["account_id"])
        character = self.browsing.store.character(row["character_id"], row["character_version"])
        materials = self._intent_materials(row)
        metadata = [item["metadata"] for item in materials]
        binding = self._publication_binding(row, character, metadata)
        signature = self.browsing.credential_signature()

        def guard():
            fresh = self.browsing.repo.decode(
                self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
            )
            if self.browsing.closed:
                raise asyncio.CancelledError
            if (
                not fresh
                or any(
                    fresh.get(key) != row.get(key)
                    for key in (
                        "status",
                        "text",
                        "source_key",
                        "account_id",
                        "character_id",
                        "character_version",
                        "target",
                        "input_version",
                    )
                )
                or signature != self.browsing.credential_signature()
            ):
                raise ValueError("恢复期间候选、账号或原始互动已变化")
            current = self._intent_materials(fresh)
            if self._publication_binding(fresh, character, [item["metadata"] for item in current]) != binding:
                raise ValueError("恢复期间原始视频或互动已变化")
            policy = self.browsing.effective()
            if automatic and not (self._action_allowed(row["kind"], policy) and row["auto_authorized"]):
                raise ValueError("自动回应授权已停止")
            self.browsing.operations.assert_allowed(
                automatic=automatic,
                channel="comments",
                account_id=account,
                character=character,
                epoch=row["epoch"] if automatic else None,
            )

        segments = [segment for material in materials for segment in self.browsing._segments(material)]
        selected = set(row["evidence"])
        used = [segment for segment in segments if segment["id"] in selected]
        context = {
            "target_source": row["target"].get("source_text"),
            "metadata": metadata,
            "evidence": used,
            "coverage": [item["coverage"] for item in materials],
        }
        if comment_materials := row["target"].get("comment_materials"):
            context["comment_materials"] = model_comment_materials(comment_materials)
        parent = self.browsing.repo.round(read_round.get() or row["round_id"])
        active = parent and parent["status"] in {"queued", "running"}
        return PublicationContext(
            binding,
            row["text"],
            character,
            context,
            guard,
            parent_round_id=parent["id"] if active else None,
            deadline=parent["deadline"] if active else None,
            max_tokens=parent["config"]["decision_max_tokens"]
            if active
            else self.browsing.policy()["decision_max_tokens"],
            payload=comment_payload(row, self.browsing.runtime, "browsing"),
            encode_revision=lambda value: comment_payload(row, self.browsing.runtime, "browsing") | {"text": value},
            on_defer=lambda prepared, state: self._defer_revision(identity, prepared, state),
            manual_approved=row["status"] == "approved",
        )

    async def _refresh_intent_identity(self, identity, automatic):
        if not automatic:
            return
        browsing = self.browsing
        refresh = getattr(browsing.runtime, "refresh_identity", None)
        if refresh is None or not hasattr(browsing.adapter, "account_profile"):
            return
        row = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        if not row or row["kind"] not in _COMMENT_KINDS or row["status"] != "pending_review":
            return
        validate_comment_body(row["text"], recipient_name=row["target"].get("recipient_name") or "")
        character = browsing.store.character(row["character_id"], row["character_version"])
        account = await browsing._current_account(row["account_id"])
        current = await refresh(character, account)
        previous = row["target"].get("generation_identity")
        if previous and previous.get("identity_version") == current["identity_version"]:
            return
        materials = self._intent_materials(row)
        target = row["target"]
        model_target = {key: value for key, value in target.items() if key not in {
            "generation_identity", "source_event", "source_discussion", "comment_materials",
        }}
        if row["kind"] == "reply_public":
            # Refresh the wording with the originally cited material only.
            cited = set(row["evidence"])
            segments = [segment for material in materials
                        for segment in browsing.decisions._selected_video_segments(material, cited)]
            if target.get("source_event"):
                thread = await browsing._read(browsing.adapter.read_public_thread, target["source_event"])
            elif hasattr(browsing.adapter, "read_video_thread"):
                thread = await browsing._read(browsing.adapter.read_video_thread, target)
            else:
                thread = {"source": await browsing._read(browsing.adapter.read_video_reply, target)}
            source = thread["source"]
            if source["text"] != target["source_text"] or str(source["author_uid"]) != str(target["recipient"]):
                raise ValueError("原始互动已删除或变化，保留草稿")
            recipient_name = source.get("author_name") or target.get("recipient_name", "")
            target = target | {"recipient_name": recipient_name}
            model_target["recipient_name"] = recipient_name
            items = thread.get("items", [])
            source_row = next((item for item in items if str(item["comment_id"]) == str(target["parent"])), None)
            if source_row:
                thread |= comment_thread_context(source_row, items, model_target)
            else:
                thread = thread | {"target": model_target}
        else:
            segments = [segment for material in materials for segment in browsing._segments(material)]
            thread = {"target": model_target,
                      "video": [material["metadata"] for material in materials]}
        allowed = [segment["id"] for segment in segments]
        if row["kind"] == "reply_public":
            allowed.append("public:" + str(thread["source"]["comment_id"]))
        request = row | {"config": browsing.policy() | {"comment_handoff_enabled": False}}
        comment_materials = model_comment_materials(target.get("comment_materials", []))
        data = browsing.decisions._response_actor_data(request, {
            "kind": row["kind"], "thread": thread, "evidence": segments,
            "coverage": "public_text" if not materials else materials[0]["coverage"],
            "allowed_evidence_ids": allowed,
            **({"comment_materials": comment_materials} if comment_materials else {}),
        })
        response_input = ResponseDecisionInput(data)
        _, messages, schema = browsing.decisions._model_input(
            request, ResponseDecision, data, RESPONSE_INSTRUCTION, response_input=response_input,
        )

        def guard():
            fresh = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
            if not fresh or any(fresh[key] != row[key] for key in ("text", "status", "target")):
                raise ValueError("身份更新期间候选已变化，未覆盖")
            if runtime_identity(browsing.runtime, character, account)["identity_version"] != current["identity_version"]:
                raise ValueError("生成期间账号身份再次变化")
            browsing.operations.assert_allowed(automatic=automatic, channel="comments", account_id=account,
                                               character=character, epoch=row["epoch"] if automatic else None)

        async def invoke(packet):
            return await browsing.runtime.operation_llm.generate_json(packet, schema, max_tokens=request["config"]["decision_max_tokens"])

        async with browsing.runtime.offline_generation("background"):
            wire, audit = await generate_with_recovery(
                browsing.runtime, channel="generation.browsing_identity", identity=identity + ":" + current["identity_version"],
                binding={"task": "generation", "account_id": account,
                         "character": {key: character[key] for key in ("id", "version")},
                         "source_sha256": content_hash([thread, comment_materials]) if comment_materials
                         else content_hash(thread), "self_identity": current},
                character=character, messages=messages, generate=invoke, validate=response_input.validate_wire,
                max_tokens=request["config"]["decision_max_tokens"], guard=guard,
                diagnose=response_input.diagnose,
            )
            response = response_input.resolve(wire)
            guard()
            if response.action != "reply_public":
                raise ValueError("身份更新后选择暂不发送：" + (response.explanation or response.reason))
            target = target | {"generation_identity": current}
            if audit:
                target["generation_continuation_id"] = audit["id"]
            browsing.store.execute(
                "UPDATE browsing_intents SET text=?,target=?,evidence=?,reason=?,error=NULL,updated_at=? WHERE id=?",
                (response.text, dump(target), dump(response.evidence_ids), response.explanation or response.reason,
                 time.time(), identity),
            )
            if audit and audit["state"] == "awaiting_review":
                browsing.store.execute(
                    "UPDATE browsing_intents SET status='pending_review',auto_authorized=0 WHERE id=?", (identity,)
                )

    def _apply_revision(self, identity, prepared, text):
        prepared.guard()
        text = validate_operation_text(text)
        self.browsing.store.execute(
            "UPDATE browsing_intents SET status=?,text=?,error=NULL,updated_at=? WHERE id=?",
            (
                "pending_review",
                text,
                time.time(),
                identity,
            ),
        )

    def _defer_revision(self, identity, prepared, state):
        prepared.guard()
        status = "rejected" if state == "abandoned" else "pending_review"
        self.browsing.store.execute(
            "UPDATE browsing_intents SET status=?,updated_at=? WHERE id=?",
            (status, time.time(), identity),
        )

    async def resume_continuation(self, identity, continuation_id):
        if identity.startswith("generation:"):
            if self.browsing.active:
                raise ValueError("浏览轮正在执行，结束后可继续生成检查点")
            local_id = identity.removeprefix("generation:")
            event = local_id.startswith("event:")
            if event:
                source = self.browsing.store.one(
                    "SELECT * FROM browsing_events WHERE id=?", (local_id.removeprefix("event:"),)
                )
                saved = json.loads(source["error"] or "{}") if source else {}
            else:
                source = self.browsing.store.one(
                    "SELECT t.*,r.account_id FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE t.id=?",
                    (local_id,),
                )
                saved = json.loads(source["payload"]) if source else {}
            continuation = self.browsing.runtime.continuations.get(continuation_id)
            await self.browsing._current_account()
            character = self.browsing.runtime.character
            if (
                not source
                or saved.get("generation_continuation_id") != continuation_id
                or str(source["account_id"]) != str(self.browsing.account)
                or continuation["binding"]["character"]
                != {"id": character["id"], "version": character["version"]}
                or not continuation["can_resume"]
            ):
                raise ValueError("生成检查点的账号、角色、原始任务或状态已变化")
            if event:
                policy = self.browsing.configured()
                if (
                    not (policy["reply_enabled"] or policy["comment_like_enabled"])
                    or source["kind"] == "mention"
                    and not policy["mention_reply_enabled"]
                ):
                    raise ValueError("通知回应尚未启用")
                # A provider change starts a new generation for this source.
                # Leave the previous engine's failed attempt readable.
                if continuation["binding"].get("model") in (None, self.browsing.model_signature()):
                    self.browsing.runtime.continuations.resume(
                        continuation_id, binding=continuation["binding"]
                    )
                self.browsing.store.execute(
                    "UPDATE browsing_events SET status='received' WHERE id=? AND status IN ('checkpoint','invalid')",
                    (source["id"],),
                )
                return await self.browsing.run(preview=True, notifications_only=True)
            self.browsing.runtime.continuations.resume(
                continuation_id, binding=continuation["binding"]
            )
            return await self.browsing.reread(local_id)
        return await FeedbackPublication(
            self.browsing.runtime, self.browsing.store, self.browsing.operations
        ).resume(
            "browsing",
            identity,
            continuation_id,
            prepare=lambda: self._publication_context(identity, False),
            apply_revision=lambda prepared, text: self._apply_revision(identity, prepared, text),
            reconcile=lambda token, prepared: self._reconcile(identity),
        )

    async def _reconcile(self, identity):
        feedback = FeedbackPublication(self.browsing.runtime, self.browsing.store, self.browsing.operations)
        if not feedback.coordinator.find("browsing", identity):
            intent = self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
            if intent and intent["status"] == "publish_uncertain" and self._has_legacy_binding(intent):
                saved = self.browsing.store.one(
                    """SELECT token FROM operation_write_attempts WHERE account_id=? AND character_id=?
                       AND intent=? AND resolved_status='uncertain' ORDER BY started_at DESC LIMIT 1""",
                    (intent["account_id"], intent["character_id"], intent["source_key"]),
                )
                if saved:
                    attempt = self.browsing.operations.get_attempt(saved["token"])
                    source = ((attempt or {}).get("binding") or {}).get("source")
                    if isinstance(source, dict) and source.get("local_id") == identity:
                        feedback.record_unknown(
                            "browsing",
                            identity,
                            attempt["token"],
                            attempt["binding"],
                            comment_payload(self.browsing.repo.decode(intent), self.browsing.runtime, "browsing"),
                            "恢复发送回执核对",
                        )

        async def read(row, attempt):
            intent_row = self.browsing.repo.decode(
                self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
            )
            if intent_row and intent_row.get("kind") in ("video_like", "video_fav"):
                return {"status": "unsupported"}
            try:
                await self.browsing._current_account(row["binding"]["account_id"])
            except ValueError:
                return {"status": "check_failed", "reason": "account_unavailable"}
            if intent_row and intent_row["kind"] == "comment_like":
                target = intent_row["target"]
                try:
                    source = await self._verify_reply_source(target, intent_row["account_id"])
                except ValueError:
                    return {"status": "check_failed"}
                if self._liked_target(source, target):
                    return {"status": "visible", "receipt": self._like_observation(target)}
                return {"status": "not_found"}
            if intent_row and intent_row["kind"] == "dynamic_like":
                try:
                    observed = await self._verify_dynamic_like(intent_row["target"])
                except ValueError:
                    return {"status": "check_failed"}
                return {"status": "visible", "receipt": observed} if observed else {"status": "not_found"}
            if intent_row and comment_payload(intent_row, self.browsing.runtime, "browsing").get("asset_sha256") and not (attempt["receipt"] or {}).get("rpid"):
                return {"status": "unsupported", "reason": "image_receipt_required"}
            if not hasattr(self.browsing.adapter, "reconcile_publication"):
                return {"status": "unsupported"}
            return await self.browsing.adapter.reconcile_publication(
                row["binding"]["target"],
                author_uid=row["binding"]["account_id"],
                text=row["feedback"]["sent_payload"]["text"],
                started_at=attempt["started_at"],
                receipt=attempt["receipt"],
            )

        def commit(receipt, verification):
            self.browsing.store.execute(
                "UPDATE browsing_intents SET status='published',receipt=?,visibility=?,error=NULL,updated_at=? WHERE id=?",
                (dump(receipt), verification["status"], time.time(), identity),
            )

        return await feedback.reconcile("browsing", identity, read, commit)

    async def publish(self, identity, *, automatic=False):
        row = self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        row = self.browsing.repo.decode(row)
        if row and row["status"] in {"pending_review", "approved"}:
            require_comment_attachment("browsing", row, row_ref(row))
        if automatic and row:
            await wait_decision(self.browsing.runtime, "browsing", row, lambda:
                                self.browsing.operations.policy()["enabled"] and self.browsing.operations.policy()["epoch"] == row["epoch"])
        manual_comment = row and row["kind"] in _COMMENT_KINDS and not automatic
        try:
            if row and row["kind"] == "comment_like":
                return await self._publish_once(identity, automatic=automatic)
            return await FeedbackPublication(
                self.browsing.runtime, self.browsing.store, self.browsing.operations
            ).submit(
                "browsing",
                identity,
                prepare=lambda: self._publication_context(identity, automatic),
                send=lambda ticket, binding, body_hash: self._publish_once(
                    identity, automatic=automatic, revision_ticket=ticket, binding=binding,
                    body_hash=content_hash(comment_payload(row, self.browsing.runtime, "browsing")) if manual_comment else body_hash,
                ),
                apply_revision=lambda prepared, text: self._apply_revision(identity, prepared, text),
                automatic=automatic,
                epoch=row["epoch"] if row and automatic else None,
                reconcile=lambda token, prepared: self._reconcile(identity),
                revise_on_rejection=not manual_comment,
            )
        except UnknownPublication:
            if not automatic:
                raise
            # A submitted intent with an unknown receipt is a durable outcome,
            # not a failure to read this video or its other discussion threads.
            return {"status": "publish_uncertain", "id": identity}

    async def _verify_reply_source(self, target, account):
        if target.get("source_event"):
            verified = await read_scoped_thread(
                self.browsing.store, self.browsing.adapter.read_public_thread, target["source_event"], target["source_event"],
                invoke=self.browsing._read,
            )
        elif hasattr(self.browsing.adapter, "read_video_thread"):
            verified = await read_scoped_thread(
                self.browsing.store, self.browsing.adapter.read_video_thread, target,
                target | {"account_id": account, "resource_type": "video"}, invoke=self.browsing._read,
            )
        else:
            verified = {"source": await self.browsing._read(self.browsing.adapter.read_video_reply, target)}
        if (verified["source"]["text"] != target["source_text"]
                or str(verified["source"]["author_uid"]) != str(target["recipient"])):
            raise PlatformReadError("invalid", "原始互动已删除或变化")
        observed_type = (verified.get("target") or {}).get("type")
        if target.get("resource_type") == "dynamic" and observed_type is not None and (
            observed_type != target.get("type", 17)
        ):
            raise ValueError("原始动态评论类型已变化")
        validate_comment_material_sources(target.get("comment_materials", []), verified.get("items", []), target["parent"])
        return verified

    async def _verify_dynamic_like(self, target):
        current = await self.browsing._read(self.browsing.adapter.read_dynamic_like, target["dynamic_id"])
        if (not current or current["dynamic_id"] != target["dynamic_id"] or current["author_uid"] != target["recipient"]
                or current["text"] != target["source_text"]):
            raise ValueError("原始动态或作者已变化")
        if current["liked"]:
            return {"platform": "bilibili", "action": "dynamic_like", "dynamic_id": current["dynamic_id"], "observed": True}

    @staticmethod
    def _liked_target(thread, target):
        return any(str(item["comment_id"]) == str(target["parent"]) and item.get("liked_by_self") is True
                   for item in thread.get("items", []))

    @staticmethod
    def _like_observation(target):
        return {"platform": "bilibili", "action": "comment_like", "oid": str(target["oid"]),
                "resource_type": target.get("resource_type", "video"), "rpid": str(target["parent"]),
                "observed": True}

    async def _publish_once(
        self, identity, *, automatic=False, revision_ticket=None, binding=None, body_hash=None
    ):
        await self._refresh_intent_identity(identity, automatic)
        async with self.browsing.comments.publish_lock:
            row = self.browsing.repo.decode(
                self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
            )
            required = "pending_review" if automatic else "approved"
            if not row or row["status"] != required:
                raise ValueError("候选未获授权或已提交")
            if automatic and not row["auto_authorized"]:
                raise ValueError("该候选不能自动发送")
            is_like = row["kind"] == "comment_like"
            is_dynamic_like = row["kind"] == "dynamic_like"
            channel = _ENGAGEMENT_CHANNELS.get(row["kind"], "comments")
            if is_like and not self.browsing.effective().get("comment_like_enabled"):
                raise ValueError("评论点赞授权已关闭")
            if is_dynamic_like and not self.browsing.effective().get("dynamic_like_enabled"):
                raise ValueError("动态点赞授权已关闭")
            if row["kind"] in _COMMENT_KINDS:
                validate_comment_body(row["text"], recipient_name=row["target"].get("recipient_name") or "")
            character = self.browsing.store.character(row["character_id"], row["character_version"])
            account = await self.browsing._current_account(row["account_id"])
            signature = self.browsing.credential_signature()
            if account != row["account_id"]:
                raise ValueError("候选来源账号已变化")
            keys = await self.browsing._refresh_material_keys(
                row["material_keys"], json.loads(row["input_version"]),
                allow_expired=not automatic and row["kind"] in _COMMENT_KINDS,
            )
            if keys != row["material_keys"]:
                row["material_keys"] = keys
                self.browsing.store.execute(
                    "UPDATE browsing_intents SET material_keys=? WHERE id=?", (dump(keys), identity)
                )
            if is_dynamic_like:
                if observed := await self._verify_dynamic_like(row["target"]):
                    self.browsing.store.execute(
                        "UPDATE browsing_intents SET status='published',receipt=?,visibility='visible',error=NULL,updated_at=? WHERE id=?",
                        (dump(observed), time.time(), identity),
                    )
                    return self.browsing.repo.decode(self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
            elif row["kind"] == "top_level" and (job := current_send.get()) and job["approval"]:
                target = row["target"]
                video = await self.browsing._read(self.browsing.adapter.video_metadata, target["bvid"])
                if video["bvid"] != target["bvid"] or str(video["aid"]) != str(target["oid"]):
                    raise ValueError("源视频已变化，请重新审核草稿")
            elif row["kind"] not in ("top_level", "video_like", "video_fav"):
                target = row["target"]
                verified = await self._verify_reply_source(target, account)
                with self.browsing.store.db:
                    observe_thread(
                        self.browsing.store, row, resource_key(target), verified.get("items"),
                        via="submission_check", resource_author_uid=verified.get("resource_author_uid"),
                    )
                if is_like:
                    if self._liked_target(verified, target):
                        receipt = self._like_observation(target)
                        self.browsing.store.execute(
                            "UPDATE browsing_intents SET status='published',receipt=?,visibility='visible',"
                            "error=NULL,updated_at=? WHERE id=?", (dump(receipt), time.time(), identity),
                        )
                        return self.browsing.repo.decode(
                            self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
                        )
                else:
                    if handled := operator_reply(verified.get("items", []), target, account):
                        return self._record_account_reply(row, handled)
                    name = verified["source"].get("author_name")
                    if name and name != target.get("recipient_name"):
                        validate_comment_body(row["text"], recipient_name=name)
            materials = self._intent_materials(row)
            actual_binding = self._publication_binding(
                row, character, [item["metadata"] for item in materials]
            )
            require_comment_attachment("browsing", row, row_ref(row))
            payload = comment_payload(row, self.browsing.runtime, "browsing")
            image = (comment_image(self.browsing.comments.memes, payload, character, runtime=self.browsing.runtime,
                                   scope=image_scope("browsing", row)) if payload.get("asset_sha256") else None)
            if image and not any(str(m["metadata"].get("aid")) == str(row["target"]["oid"])
                                 and m["metadata"].get("bvid") == row["target"].get("bvid") for m in materials):
                meta = await self.browsing.adapter.video_metadata(row["target"]["bvid"])
                if (str(meta.get("aid")) != str(row["target"]["oid"])
                        or meta.get("bvid") != row["target"]["bvid"]):
                    raise ValueError("配图评论的视频与原材料不符")
            if (
                binding is not None
                and binding != actual_binding
                or body_hash is not None
                and body_hash != content_hash(payload)
            ):
                raise ValueError("发送前原始绑定或正文已变化")
            token = None

            def authorize():
                self.browsing.runtime.validate_memory_delivery(row["target"].get("memory_audit"))
                if token is not None:
                    raise ValueError("同一意图不能二次进入提交边界")
                if signature != self.browsing.credential_signature() or self.browsing.closed:
                    raise ValueError("账号／服务已变化，取消提交")
                captured = row["target"].get("generation_identity")
                if automatic and captured and captured.get("identity_version") != runtime_identity(
                    self.browsing.runtime, character, account
                )["identity_version"]:
                    raise ValueError("发送前身份资料已更新，保留草稿重新生成")
                fresh = self.browsing.repo.decode(
                    self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
                )
                if not fresh or any(
                    fresh.get(key) != row.get(key)
                    for key in (
                        "status",
                        "text",
                        "source_key",
                        "target",
                        "account_id",
                        "character_id",
                        "character_version",
                        "input_version",
                    )
                ):
                    raise ValueError("准备期间候选或审核状态已变化，保留最新状态")
                if automatic:
                    self.browsing.sends.assert_ready("browsing", identity)
                self.browsing.sends.assert_approval("browsing", identity, fresh, payload)
                if automatic and not self._action_allowed(row["kind"], self.browsing.effective()):
                    raise ValueError("自动回应授权已停止")
                if is_like and not self.browsing.effective().get("comment_like_enabled"):
                    raise ValueError("评论点赞授权已关闭")
                if is_dynamic_like and not self.browsing.effective().get("dynamic_like_enabled"):
                    raise ValueError("动态点赞授权已关闭")
                self.browsing.operations.assert_allowed(
                    automatic=automatic,
                    channel=channel,
                    account_id=account,
                    character=character,
                    epoch=row["epoch"] if automatic else None,
                )
                for key in row["material_keys"]:
                    self.browsing.evidence.load(key, allow_expired=True)
                if image:
                    comment_image(self.browsing.comments.memes, payload, character, runtime=self.browsing.runtime,
                                  scope=image_scope("browsing", row))
                return True

            def before_send():
                nonlocal token
                authorize()
                token = self.browsing.operations.reserve(
                    account,
                    character,
                    row["source_key"],
                    channel,
                    recipient=row["target"].get("recipient", "") if channel == "comments" else "",
                    automatic=automatic,
                    limits=self.browsing.comments.publication_limits() if channel == "comments" else None,
                    epoch=row["epoch"] if automatic else None,
                    binding=actual_binding,
                    content_hash=content_hash(payload),
                    revision_ticket=revision_ticket,
                    admission_hook=lambda ticket: self._reserve_daily_reply(ticket, row),
                )
                self.browsing.store.execute(
                    "UPDATE browsing_intents SET status='publishing',updated_at=? WHERE id=?",
                    (time.time(), identity),
                )

            try:
                options = {}
                if image:
                    ref = decision(self.browsing.runtime, "browsing", row, row_ref(row))
                    options.update(image=image, prepare_authorized=authorize,
                                   on_uploaded=lambda pic: record_upload(self.browsing.store, "browsing", identity, ref, pic))
                if is_like:
                    receipt = await self.browsing.adapter.like_comment(row["target"], before_send=before_send)
                elif is_dynamic_like:
                    receipt = await self.browsing.adapter.like_dynamic(row["target"], before_send=before_send)
                elif row["kind"] == "video_like":
                    receipt = await self.browsing.adapter.like_video(
                        row["target"], like=True, before_send=before_send
                    )
                elif row["kind"] == "video_fav":
                    receipt = await self.browsing.adapter.favorite_video(
                        row["target"], before_send=before_send
                    )
                elif row["kind"] == "top_level":
                    receipt = await self.browsing.adapter.publish_top_level(
                        row["target"], row["text"], before_send=before_send, **options
                    )
                else:
                    receipt = await self.browsing.adapter.publish_resource_reply(
                        row["target"], row["text"], before_send=before_send
                    )
                if image and isinstance(receipt, dict):
                    receipt.update(image_ref=ref)
                if (
                    not token
                    or not isinstance(receipt, dict)
                    or (
                        row["kind"] not in ("video_like", "video_fav", "dynamic_like")
                        and (not str(receipt.get("rpid", "")).isdigit() or int(receipt["rpid"]) <= 0)
                    )
                ):
                    raise PublishUncertain("没有有效一次提交回执")
                if is_dynamic_like and not await self._verify_dynamic_like(row["target"]):
                    raise PublishUncertain("动态点赞已受理，等状态读回")
            except BaseException as error:
                if token:
                    uncertain = isinstance(
                        error, (PublishUncertain, asyncio.CancelledError)
                    ) or not isinstance(error, ValueError)
                    self.browsing.operations.finish(
                        token, "uncertain" if uncertain else "rejected", error=public_error(error)
                    )
                    if uncertain:
                        FeedbackPublication(
                            self.browsing.runtime, self.browsing.store, self.browsing.operations
                        ).record_unknown(
                            "browsing",
                            identity,
                            token,
                            actual_binding,
                            payload,
                            public_error(error),
                        )
                    self.browsing.store.execute(
                        "UPDATE browsing_intents SET status=?,error=?,updated_at=? WHERE id=?",
                        (
                            "publish_uncertain"
                            if uncertain
                            else row["status"]
                            if isinstance(error, PlatformRejected) and not is_like
                            else "rejected_by_platform",
                            public_error(error),
                            time.time(),
                            identity,
                        ),
                    )
                    if isinstance(error, PlatformRejected):
                        error.write_token = token
                        error.body_hash = content_hash(payload)
                        error.sent_payload = payload
                    elif uncertain and not isinstance(error, asyncio.CancelledError):
                        raise UnknownPublication(public_error(error), token) from None
                raise
            try:
                self.browsing.store.execute(
                    "UPDATE browsing_intents SET status='published',receipt=?,updated_at=? WHERE id=?",
                    (dump(receipt), time.time(), identity),
                )
                self.browsing.operations.finish(token, "published", receipt=receipt)
            except Exception:
                self.browsing.operations.finish(
                    token, "uncertain", receipt=receipt, error="平台成功后本地保存失败"
                )
                FeedbackPublication(
                    self.browsing.runtime, self.browsing.store, self.browsing.operations
                ).record_unknown(
                    "browsing",
                    identity,
                    token,
                    actual_binding,
                    payload,
                    "平台成功后本地保存失败",
                )
                self.browsing.store.execute(
                    "UPDATE browsing_intents SET status='publish_uncertain',error='本地回执待补录' WHERE id=?",
                    (identity,),
                )
                raise UnknownPublication("平台提交结果待核对", token) from None
            if row["kind"] in _ENGAGEMENT_KINDS:
                self.browsing.store.execute(
                    "UPDATE browsing_intents SET visibility='visible' WHERE id=?", (identity,)
                )
            else:
                try:
                    visibility = await self.browsing.adapter.verify_publication(row["target"], receipt)
                    self.browsing.store.execute(
                        "UPDATE browsing_intents SET visibility=? WHERE id=?",
                        (visibility["status"], identity),
                    )
                except Exception:
                    self.browsing.store.execute(
                        "UPDATE browsing_intents SET visibility='check_failed' WHERE id=?", (identity,)
                    )
            return self.browsing.repo.decode(
                self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
            )

    def _record_account_reply(self, row, handled):
        target = row["target"] | {"account_reply": comment_source(handled) | {"created_at": handled.get("created_at")}}
        self.browsing.store.execute(
            "UPDATE browsing_intents SET status='operator_replied',target=?,error=NULL,auto_authorized=0,updated_at=? "
            "WHERE id=? AND status=? AND text=?",
            (dump(target), time.time(), row["id"], row["status"], row["text"]),
        )
        return self.browsing.repo.decode(
            self.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (row["id"],))
        )
