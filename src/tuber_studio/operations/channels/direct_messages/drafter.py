"""Direct-message drafting, comment outreach handoff, and continuation recovery."""

import asyncio
import json
import time
from contextlib import nullcontext

import httpx

from ....core.identity import runtime_identity
from ....core.meter import GenerationMeter, budgeted_json
from ....core.models import OperationReply, content_hash
from ....core.text import operation_output_diagnostics, validate_operation_text
from ....core.turns import operation_ready_at
from ....infra.inference.providers import public_error
from ....persona.assets.illustration_policy import illustration_policy
from ....persona.assets.meme_selection import select_meme
from ...kernel.control import canonical_intent
from ...kernel.feedback import (
    FeedbackPublication,
    generation_view,
)
from ...kernel.generation import generate_with_recovery, generation_requires_review, reply_generation_binding
from ..attachments import (
    queue_image,
    row_ref,
    workstation_image_decisions,
)
from ..dynamics.materials import REFLECTION_INSTRUCTION, REFLECTIONS
from .models import ConversationReply, MessageInputChanged
from .outreach import (
    assess_outreach,
    confirmed_invitation,
    invitation_anchor,
    invitation_key,
    verified_agreement,
)
from .turns import generate_native_dm_reply, should_use_native_dm_turns


class DirectMessagesDrafterMixin:
    def enqueue(
        self,
        kind,
        identity,
        *,
        automatic=False,
        reason="对方主动来信",
        character=None,
        account_id=None,
        video_evidence=None,
        schedule=True,
        purpose="",
        trigger_source=None,
        proposed_text="",
        proposal_identity=None,
        contact_trigger=None,
        continuous=False,
        managed_handoff=False,
        waiting_since=None,
        transaction_owned=False,
    ):
        self._enabled()
        character = character or self.runtime.character
        source = self._source(kind, identity, character)
        account_id = str(source.get("account_id") or "") if kind == "message" else str(account_id or "")
        existing = self.store.one(
            "SELECT id FROM dm_drafts WHERE account_id=? AND source_kind=? AND source_id=? AND character_id=?",
            (account_id, kind, identity, character["id"]),
        )
        if existing:
            return existing["id"]
        if schedule and self.queue.full():
            raise ValueError("私信生成队列已满，请稍后重试")
        if kind == "comment" and not self.policy()["allow_comment_handoff"]:
            raise ValueError("评论转私信尚未启用")
        encoded = None if video_evidence is None else json.dumps(video_evidence, ensure_ascii=False)
        if encoded is not None and len(encoded.encode()) > 1_000_000:
            raise ValueError("移交的视频来源材料过大")
        now = time.time()
        automatic = automatic and self.policy()["auto_send"]
        trigger = trigger_source or (
            {"kind": "message", "ids": [identity], "message_id": source["message_id"]}
            if kind == "message" else
            {"kind": "comment", "comment_id": source["comment_id"], "bvid": source["video_id"], "root": source["thread_id"]}
        )
        if kind == "comment":
            trigger["contact_trigger"] = contact_trigger
            if managed_handoff:
                trigger["managed_handoff"] = True
                trigger["handoff_epoch"] = self.operations.policy()["epoch"]
        elif continuous:
            trigger["continuous"] = True
            trigger["waiting_since"] = waiting_since or now
        if automatic and self.operations.policy()["enabled"]:
            trigger["automatic_epoch"] = self.operations.policy()["epoch"]
        with nullcontext() if transaction_owned else self.store.db:
            draft_id = self.store.db.execute(
                "INSERT INTO dm_drafts(platform,platform_user_id,display_name,source_kind,source_id,character_id,character_version,status,reason,created_at,updated_at,account_id,video_evidence,contact_type,purpose,trigger_source,proposed_text,proposal_identity) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)",
                (
                    source["platform"],
                    source["platform_user_id"],
                    source["display_name"],
                    kind,
                    identity,
                    character["id"],
                    character["version"],
                    reason,
                    now,
                    now,
                    account_id,
                    encoded,
                    "reply" if kind == "message" else "outreach",
                    str(purpose).strip(),
                    json.dumps(trigger, ensure_ascii=False),
                    validate_operation_text(proposed_text) if proposed_text else "",
                    json.dumps(proposal_identity, ensure_ascii=False) if proposal_identity else None,
                ),
            ).lastrowid
            self.authorizations[draft_id] = (
                self.operations.policy()["epoch"] if automatic and self.operations.policy()["enabled"] else None
            )
            if managed_handoff:
                self.store.db.execute("UPDATE dm_drafts SET resume_pending=1 WHERE id=?", (draft_id,))
        if schedule:
            self._schedule(draft_id, automatic)
        return draft_id

    async def route_comment(self, row, character, event, *, automatic=False):
        policy = self.policy()
        if not (policy["enabled"] and policy["allow_comment_handoff"] and row.get("platform_user_id")):
            return None
        evidence = dict(event.get("video_evidence") or {})
        thread = evidence.get("public_source_thread") or event.get("comment_context")
        if thread is None:
            thread = self.comments._comment_actors(row, character, event.get("account_id"))["comment_context"]
        invitation = confirmed_invitation(thread, event.get("account_id"), row["platform_user_id"])
        if not invitation:
            return None
        evidence["public_source_thread"] = thread
        return self.enqueue(
            "comment", row["id"], automatic=automatic, reason="user_requested", character=character,
            account_id=event.get("account_id", ""), video_evidence=evidence,
            purpose=invitation["quote"], contact_trigger=invitation["kind"],
        )

    async def _refresh_outreach_thread(self, draft):
        """Recover missing participant evidence without reading video material."""
        browsing = getattr(self.runtime, "browsing", None)
        adapter = getattr(browsing, "adapter", None)
        if draft["source_kind"] != "comment" or not hasattr(adapter, "read_video_thread"):
            return
        evidence = json.loads(draft.get("video_evidence") or "{}")
        thread = evidence.get("public_source_thread")
        if (
            isinstance(thread, dict) and "addressing" in thread
            and (thread.get("target") or {}).get("oid") and evidence.get("account_id") == draft["account_id"]
        ):
            return
        from ..comments.timeline import comment_thread_context

        character = self.store.character(draft["character_id"], draft["character_version"])
        source = self._source("comment", draft["source_id"], character)
        metadata = await browsing._read(adapter.video_metadata, source["video_id"])
        target = {"type": 1, "oid": str(metadata["aid"]), "bvid": metadata["bvid"],
                  "root": source["thread_id"] or source["comment_id"], "parent": source["comment_id"]}
        refreshed = await browsing._read(adapter.read_video_thread, target)
        self._require_unchanged_draft(draft)
        actual = refreshed["source"]
        if actual["text"] != source["content"] or str(actual["author_uid"]) != str(source["platform_user_id"]):
            raise ValueError("公开移交原文已变化，未沿旧来源联系")
        selected = next(item for item in refreshed["items"] if str(item["comment_id"]) == str(source["comment_id"]))
        thread = comment_thread_context(selected, refreshed["items"], target)
        thread["public_text"] = actual["text"]
        thread["resource_author_uid"] = str(metadata.get("owner_uid") or "")
        evidence.update(account_id=draft["account_id"], public_source_thread=thread, material_keys=evidence.get("material_keys", []),
                        source_versions=evidence.get("source_versions", []))
        draft["video_evidence"] = json.dumps(evidence, ensure_ascii=False)
        self.store.execute("UPDATE dm_drafts SET video_evidence=? WHERE id=?", (draft["video_evidence"], draft["id"]))
        return thread

    def _unanswered_outreach(self, draft):
        rows = self.store.all(
            "SELECT id,source_id,purpose,trigger_source,status,outreach_check,updated_at,contact_key FROM dm_drafts d "
            "WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? AND source_kind='comment' "
            "AND id<>? AND status IN ('published','publishing','publish_uncertain') AND NOT EXISTS ("
            "SELECT 1 FROM dm_inbox i WHERE i.account_id=d.account_id AND i.character_id=d.character_id "
            "AND i.platform=d.platform AND i.platform_user_id=d.platform_user_id AND i.sent_at>d.updated_at) "
            "ORDER BY updated_at DESC LIMIT 20",
            (draft["account_id"], draft["character_id"], draft["platform"], draft["platform_user_id"], draft["id"]),
        )
        result = []
        for row in rows:
            trigger = json.loads(row["trigger_source"] or "null")
            source = self.store.one("SELECT video_id,thread_id,comment_id,platform_user_id,display_name,content FROM comments WHERE id=?", (row["source_id"],))
            if not trigger:
                if source:
                    trigger = {"kind": "comment", "comment_id": source["comment_id"], "bvid": source["video_id"], "root": source["thread_id"]}
            original = None if not source else {"comment_id": str(source["comment_id"]),
                "author_uid": str(source["platform_user_id"]), "author_name": source["display_name"], "text": source["content"]}
            result.append(row | {"trigger_source": trigger, "public_source": original,
                                "outreach_check": json.loads(row["outreach_check"] or "null")})
        return result

    def _completion_evidence(self, draft):
        evidence = json.loads(draft.get("video_evidence") or "{}")
        result = []
        for proof in evidence.get("completion_evidence") or []:
            if not isinstance(proof, dict) or not proof.get("step_id"):
                continue
            saved = self.store.one("SELECT s.context,s.status,s.phase,c.binding FROM operation_continuation_steps s JOIN operation_continuations c ON c.id=s.continuation_id WHERE s.id=?", (proof["step_id"],))
            binding = json.loads(saved["binding"]) if saved else {}
            source = binding.get("source") or {}
            actor = binding.get("account_id") or source.get("account_id")
            if saved and saved["phase"] == "web_fetch" and saved["status"] == "ok" and source.get("agreement_id") == draft["id"] and str(actor) == draft["account_id"] and binding.get("character", {}).get("id") == draft["character_id"]:
                result.append({"step_id": proof["step_id"], **json.loads(saved["context"] or "{}")})
        for row in self.store.all(
            "SELECT s.id,s.context FROM operation_continuation_steps s JOIN operation_continuations c "
            "ON c.id=s.continuation_id WHERE c.channel='generation.direct_message' AND c.entity_id LIKE ? "
            "AND s.phase='web_fetch' AND s.status='ok' AND s.created_at>=?",
            (f"dm:{draft['id']}:%", draft["created_at"]),
        ):
            result.append({"step_id": row["id"], **json.loads(row["context"] or "{}")})
        return result

    async def fulfill_agreement(self, identity, step_id):
        """Wake the same stored agreement only after its bound real tool succeeds."""
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft or draft["source_kind"] != "comment" or draft["status"] != "waiting":
            return False
        trigger = json.loads(draft.get("trigger_source") or "{}")
        check = json.loads(draft.get("outreach_check") or "{}")
        if trigger.get("contact_trigger") != "agreement" or check.get("action") == "duplicate":
            return False
        character = self.store.character(draft["character_id"], draft["character_version"])
        if not verified_agreement(draft, self._event(draft, character).get("public_source_thread")):
            return False
        saved = self.store.one("SELECT s.status,s.phase,c.binding FROM operation_continuation_steps s JOIN operation_continuations c ON c.id=s.continuation_id WHERE s.id=?", (step_id,))
        binding = json.loads(saved["binding"]) if saved else {}
        source = binding.get("source") or {}
        if not saved or saved["phase"] != "web_fetch" or saved["status"] != "ok" or source.get("agreement_id") != identity or str(source.get("account_id")) != draft["account_id"] or binding.get("character", {}).get("id") != draft["character_id"]:
            return False
        evidence = json.loads(draft.get("video_evidence") or "{}")
        proofs = evidence.setdefault("completion_evidence", [])
        if not any(proof.get("step_id") == step_id for proof in proofs):
            proofs.append({"step_id": step_id})
        self.store.execute("UPDATE dm_drafts SET video_evidence=?,status='queued',proposed_text='',resume_pending=1,next_attempt_at=0,error=NULL WHERE id=?", (json.dumps(evidence, ensure_ascii=False), identity))
        if self._processing_authorized() and not self.queue.full():
            self._schedule(identity, self.policy()["auto_send"])
        return True

    def _check_outreach(self, draft, event):
        if draft["source_kind"] != "comment":
            return True
        history = self._unanswered_outreach(draft)
        trigger = json.loads(draft.get("trigger_source") or "{}")
        # Changing the comment entry point in the same discussion is not a new contact.
        same_thread = next((row for row in history if row["trigger_source"] and
            row["trigger_source"].get("bvid") == trigger.get("bvid") and
            row["trigger_source"].get("root") == trigger.get("root")), None)
        thread = event.get("public_source_thread")
        current = (thread.get("source") or {}) if isinstance(thread, dict) else {}
        original_text = " ".join(str(current.get("text") or "").split())
        same_invitation = next((row for row in history if original_text and row["public_source"] and
            row["public_source"]["author_uid"] == str(current.get("author_uid")) and
            " ".join(row["public_source"]["text"].split()) == original_text), None)
        if same_thread or same_invitation or any(not row["trigger_source"] or not row["public_source"] for row in history):
            earlier = same_thread or same_invitation or history[0]
            self.store.execute("UPDATE dm_drafts SET status='waiting',error=NULL,outreach_check=?,updated_at=? WHERE id=?",
                (json.dumps({"action": "duplicate", "state": "awaiting_response", "previous_draft_id": earlier["id"],
                             "reason": "此前主动联系尚未收到回复，等待新的明确交流"}, ensure_ascii=False), time.time(), draft["id"]))
            return False
        completion = self._completion_evidence(draft)
        assessment, action = assess_outreach(draft, event, history, completion)
        self._require_unchanged_draft(draft)
        record = {"action": action, "assessment": assessment.model_dump(),
                  "invitation_verified": invitation_anchor(assessment, draft, event.get("public_source_thread")) is not None}
        if action in {"wait", "duplicate"}:
            record["state"] = "awaiting_response" if history else "awaiting_completion"
        key = invitation_key(draft, assessment.invitation_comment_id) if action == "send" else ""
        if key and (earlier := self.store.one("SELECT id FROM dm_drafts WHERE contact_key=? AND id<>?", (key, draft["id"]))):
            record["action"], action, key = "duplicate", "duplicate", ""
            record.update(state="awaiting_response", previous_draft_id=earlier["id"])
        status = draft["status"] if action == "send" else "waiting" if action in {"wait", "duplicate"} else "not_initiated"
        if status == draft["status"] and key == draft.get("contact_key") and json.loads(draft.get("outreach_check") or "null") == record:
            return action == "send"
        self.store.execute(
            "UPDATE dm_drafts SET purpose=?,outreach_check=?,contact_key=?,status=?,error=NULL,updated_at=? WHERE id=?",
            (assessment.purpose.strip(), json.dumps(record, ensure_ascii=False), key,
             status, time.time(), draft["id"]),
        )
        return action == "send"

    async def _generate(self, identity, automatic):
        try:
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not draft:
                return
            if automatic and (not self.operations.policy()["enabled"] or self.operations.policy()["emergency"] or not self.policy()["enabled"]):
                self.store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE id=? AND status='queued'", (identity,))
                return
            scope = (draft["account_id"], draft["character_id"], "dm:" + draft["platform"], draft["platform_user_id"])
            async with self.runtime.conversation_turn(scope):
                trigger = json.loads(draft["trigger_source"] or "{}")
                waited = time.time() - trigger.get("waiting_since", draft["created_at"])
                ready = operation_ready_at.set(time.monotonic() - max(0, waited))
                try:
                    policy = self.policy()
                    browsing = getattr(self.runtime, "browsing", None)
                    slot = nullcontext()
                    if automatic and policy["auto_send"] and browsing:
                        slot = browsing.sends.preparation_slot(
                            draft["account_id"] or self.operations.policy()["account_id"], "messages", policy,
                            retry_seconds=policy["poll_interval_seconds"], recipient=draft["platform_user_id"],
                        )
                    with slot as blocked:
                        if blocked:
                            self.store.execute(
                                "UPDATE dm_drafts SET resume_pending=1,next_attempt_at=?,error=? "
                                "WHERE id=? AND status='queued'", (blocked.next_at, str(blocked), identity),
                            )
                            return
                        return await self._generate_owned(identity, automatic)
                finally:
                    operation_ready_at.reset(ready)
        except MessageInputChanged:
            return
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE dm_drafts SET status='queued',resume_pending=1,next_attempt_at=0,error='生成进度已保存，等待恢复',updated_at=? WHERE id=? AND status IN ('queued','generating')",
                (time.time(), identity),
            )
            raise
        except Exception as error:
            transient = isinstance(
                error, (httpx.HTTPError, TimeoutError, ConnectionError, OSError)
            ) or getattr(error, "kind", None) in {"network", "timeout", "rate_limited", "anti_bot"}
            continuation = getattr(error, "continuation", None)
            updated = self.store.execute(
                "UPDATE dm_drafts SET status=?,resume_pending=?,next_attempt_at=?,error=?,generation_continuation_id=COALESCE(?,generation_continuation_id),updated_at=? WHERE id=? AND status IN ('queued','generating')",
                (
                    "queued" if transient else "failed",
                    int(transient),
                    time.time() + self.policy()["poll_interval_seconds"] if transient else 0,
                    public_error(error),
                    continuation["id"] if continuation else None,
                    time.time(),
                    identity,
                ),
            )
            if updated.rowcount and continuation:
                self.runtime.continuations.bind_source_view(
                    continuation["id"], "messages", identity, "私信回复生成"
                )
            raise

    def _direct_waiting(self, draft):
        if not getattr(self.runtime, "browsing", None):
            return False
        scope = (draft["account_id"], draft["character_id"], draft["character_version"], draft["platform_user_id"])
        return bool(self.store.one(
            """SELECT 1 FROM dm_drafts d WHERE account_id=? AND character_id=? AND character_version=?
               AND platform_user_id=? AND source_kind='message'
               AND (status IN ('queued','generating','publishing')
                 OR EXISTS(SELECT 1 FROM operation_send_queue q WHERE q.channel='messages'
                           AND q.source_id=CAST(d.id AS TEXT) AND q.wake_at<=?))
               LIMIT 1""", (*scope, time.time()),
        ))

    async def _generate_owned(self, identity, automatic, *, preserve_attachments=False):
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not draft or draft["status"] != "queued":
            return
        trigger = json.loads(draft["trigger_source"] or "{}")
        if trigger.get("managed_handoff") and self._direct_waiting(draft):
            self.store.execute(
                "UPDATE dm_drafts SET resume_pending=1,next_attempt_at=? WHERE id=?",
                (time.time() + 1, identity),
            )
            return
        if draft["source_kind"] == "message" and self.store.one(
            "SELECT 1 FROM dm_sync_cursors WHERE account_id=? AND character_id=? AND platform_user_id=? AND before_sequence<>0",
            (draft["account_id"], draft["character_id"], draft["platform_user_id"]),
        ):
            self.store.execute("UPDATE dm_drafts SET resume_pending=1,next_attempt_at=? WHERE id=?",
                               (time.time() + self.policy()["poll_interval_seconds"], identity))
            return
        if self.store.one(
            "SELECT 1 FROM character_archives WHERE character_id=? AND version=?",
            (draft["character_id"], draft["character_version"]),
        ):
            raise ValueError("角色版本已归档，保留原稿与来源，请使用当前角色重新准备回复")
        self._merge_incoming(identity, automatic=automatic)
        draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if draft["status"] != "queued":
            return
        authorization_epoch = self.authorizations.pop(identity, None)
        origin = self._origin_round(draft, active=True)
        character = self.store.character(draft["character_id"], draft["character_version"])
        credential = dict(self.account.credentials)
        identity_adapter = None
        actor_snapshot = (
            await self.runtime.refresh_identity(character, draft["account_id"])
            if draft["source_kind"] == "comment" else None
        )
        if not self.injected:
            identity_adapter = await self._adapter()
            actor = str(actor_snapshot.get("account_uid") or "") if actor_snapshot else str(await identity_adapter.self_uid())
            if (
                self.adapter is not identity_adapter
                or self.account.credentials != credential
                or self.credential_signature != (credential.get("sessdata"), credential.get("csrf"))
            ):
                raise ValueError("核验期间账号已变更，未读取旧账号私密上下文")
            if draft["account_id"] and draft["account_id"] != actor:
                raise ValueError("该私信草稿属于其他账号，不能读取其私密上下文")
            if not draft["account_id"]:
                # Only a newly queued generation can establish a fresh account
                # scope. Existing unbound private history is never migrated.
                draft["account_id"] = actor
                self.store.execute(
                    "UPDATE dm_drafts SET account_id=? WHERE id=? AND status='queued'", (actor, identity)
                )
            self.active_account_id, self.active_credential = actor, credential
        browsing = getattr(self.runtime, "browsing", None)
        await self._refresh_outreach_thread(draft)
        if draft["source_kind"] == "message" and browsing:
            linked = await browsing.linked_message_evidence(draft)
            if linked is not None:
                previous = json.loads(draft.get("video_evidence") or "{}")
                if previous.get("origin_round_id"):
                    linked = {**linked, "origin_round_id": previous["origin_round_id"]}
                draft["video_evidence"] = json.dumps(linked, ensure_ascii=False)
                self.store.execute(
                    "UPDATE dm_drafts SET video_evidence=? WHERE id=? AND status='queued'",
                    (draft["video_evidence"], identity),
                )
        if draft["source_kind"] == "comment":
            if actor_snapshot.get("account_uid") != draft["account_id"]:
                raise ValueError("核对期间账号已变化，未读取旧私密上下文")
            if not self._check_outreach(draft, self._event(draft, character)):
                return
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if (draft["source_kind"] == "comment" and not automatic
                and (browsing := getattr(self.runtime, "browsing", None))
                and not browsing.review_capacity("messages")):
            self.store.execute(
                "UPDATE dm_drafts SET resume_pending=1,next_attempt_at=?,error='私信审核积压' WHERE id=?",
                (time.time() + self.policy()["poll_interval_seconds"], identity),
            )
            return
        trigger = json.loads(draft["trigger_source"] or "{}")
        managed = automatic or draft["resume_pending"] or trigger.get("continuous")
        async with self.runtime.offline_generation(
            "direct" if draft["source_kind"] == "message" and managed else "background"
        ):
            if managed and self.operations.draining:
                self.store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE id=?", (identity,))
                return
            if draft.get("resume_pending") and not self._processing_authorized():
                return
            if not self.injected and (
                self.adapter is not identity_adapter or self.account.credentials != credential
            ):
                raise ValueError("等待模型期间账号已变更，未读取旧账号私密上下文")
            self.store.execute(
                "UPDATE dm_drafts SET status='generating',updated_at=? WHERE id=?", (time.time(), identity)
            )
            character = self.store.character(draft["character_id"], draft["character_version"])
            source = self._source(draft["source_kind"], draft["source_id"], character)
            event = self._event(draft, character)
            handled, reply_ids = [], []
            ambiguous_order = False
            if draft["source_kind"] == "message":
                window = self._reply_window(draft, source)
                refs = window.refs
                ambiguous_order = window.ambiguous
                if not refs:
                    self.store.execute(
                        "UPDATE dm_drafts SET status='cancelled',resume_pending=0,error=? WHERE id=?",
                        ("来信已处理，无需重复发送", identity),
                    )
                    return
                if window.latest_outgoing_id and not ambiguous_order:
                    self._settle_incoming(draft, list(refs.values()), window.latest_outgoing_id)
                    self.store.execute(
                        "UPDATE dm_drafts SET status='cancelled',resume_pending=0,error=?,updated_at=? WHERE id=?",
                        ("号主已在平台手动回复，无需重复发送", time.time(), identity),
                    )
                    return
                handled = list(window.prior_settled or ())
                reply_ids = list((window.unanswered_refs or refs).values())
            relationship = self.runtime.relationships.observe(
                character["id"],
                draft["platform"],
                draft["platform_user_id"],
                draft["display_name"],
                canonical_intent(
                    draft["platform"],
                    source.get("video_id", "dm"),
                    source.get("comment_id") or source.get("message_id"),
                    character["id"],
                ),
                event["text"],
                account_id=draft["account_id"],
            )
            meter = (
                GenerationMeter(self.runtime.browsing_repository, origin["id"])
                if origin and origin["status"] in {"queued", "running"} else nullcontext()
            )
            with meter:
                reflection = None
                snapshot = runtime_identity(self.runtime, character, draft["account_id"])
                proposal_identity = json.loads(draft.get("proposal_identity") or "null")
                if draft.get("proposed_text") and proposal_identity == snapshot:
                    reply = OperationReply(spoken_text=draft["proposed_text"], reply_to=[event["event_id"]])
                    generation_id = (json.loads(draft.get("video_evidence") or "null") or {}).get(
                        "generation_continuation_id"
                    )
                    check = {"generation_identity": proposal_identity, "output": {
                        "needs_review": generation_requires_review(self.store, generation_id),
                    }, "reused_proposal": True}
                else:
                    generation_event = event | {"share_reflection": True} if draft["source_kind"] == "message" else event
                    if draft["source_kind"] == "comment":
                        thread = event["public_source_thread"]
                        generation_event = event | {
                            "public_source_thread": {
                                key: thread.get(key) for key in ("source", "root", "parent", "timeline", "grouped_comments")
                            }
                        }
                        evidence = generation_event.pop("video_evidence", {})
                        if evidence.get("segments"):
                            generation_event["video_evidence"] = {"segments": evidence["segments"]}
                    reply, check = await self.runtime.generate_reply(
                        self.scope(draft), character, generation_event, relationship, mode="direct_message"
                    )
                    if draft["source_kind"] == "message":
                        if not reply_ids:
                            reply_ids = list(refs.values())
                        if ambiguous_order:
                            check.setdefault("output", {})["needs_review"] = True
                        reflection = getattr(reply, "public_reflection", None)
                selected, selection_failed = None, False
                if (reply and not preserve_attachments and illustration_policy(self.store).use_memes and self.memes
                        and not workstation_image_decisions(self.runtime)):
                    try:
                        selected = await select_meme(
                            self.runtime, self.memes, character, event, reply.spoken_text
                        )
                        if not illustration_policy(self.store).use_memes:
                            selected = None
                        check["meme_selection"] = {"state": "selected" if selected else "text"}
                    except Exception:
                        check["meme_selection"] = {"state": "failed", "reason": "表情选择无效，保留待审文字"}
                        selection_failed = True
            if self._merge_incoming(identity, automatic=automatic):
                raise MessageInputChanged("新来信已合并，重新准备回复")
            if draft["source_kind"] == "message":
                self._check_conversation(draft, event["conversation_sha256"], automatic=automatic)
                with self.store.db:
                    if reflection in REFLECTIONS and draft["account_id"]:
                        key = "reflection:" + content_hash([draft["account_id"], sorted(refs.values())])
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO dynamic_reflections VALUES(?,?,?,?,?,?)",
                            (draft["account_id"], character["id"], character["version"], key, reflection, time.time()),
                        )
                    for incoming, outgoing in handled:
                        self._settle_incoming(draft, [incoming], outgoing)
                    check["reply_incoming_ids"] = reply_ids
                    check["conversation_sha256"] = self._reply_window(draft, source).fingerprint
                    trigger = json.loads(draft.get("trigger_source") or "{}")
                    trigger["ids"] = sorted(set(trigger.get("ids", [])) | set(refs.values()))
                    self.store.db.execute("UPDATE dm_drafts SET trigger_source=? WHERE id=?",
                                          (json.dumps(trigger, ensure_ascii=False), identity))
                if not reply:
                    return
            ref = row_ref(draft) if preserve_attachments else None
            if not preserve_attachments and not selected and not selection_failed:
                ref = queue_image(self.runtime, "messages", draft,
                                  {("incoming" if draft["source_kind"] == "message" else "public_comment"): source["content"],
                                   "recipient_uid": draft["platform_user_id"],
                                   "reply_incoming_ids": reply_ids}, reply.spoken_text)
            self.store.execute(
                "UPDATE dm_drafts SET status=?,resume_pending=0,next_attempt_at=0,text=?,answer_check=?,model=?,"
                "image_ref=?,message_kind=?,meme_id=?,error=NULL,updated_at=? WHERE id=?",
                (
                    "pending_review",
                    reply.spoken_text,
                    json.dumps(check, ensure_ascii=False),
                    self.settings.data["omni"]["responses_model"],
                    json.dumps(ref) if ref else None,
                    draft["message_kind"] if preserve_attachments else "text",
                    draft["meme_id"] if preserve_attachments else None,
                    time.time(),
                    identity,
                ),
            )
            if selected:
                owner = f"dm-draft:{identity}"
                self.memes.read(selected["asset_id"], character)
                self.memes.pin(selected["asset_id"], character, owner)
                try:
                    self.store.execute(
                        "UPDATE dm_drafts SET message_kind='image',meme_id=? WHERE id=?",
                        (selected["asset_id"], identity),
                    )
                except BaseException:
                    self.memes.release(owner, keep_asset_id=draft.get("meme_id"))
                    raise
                self.memes.release(owner, keep_asset_id=selected["asset_id"])
            elif selection_failed:
                self.store.execute(
                    "UPDATE dm_drafts SET error=? WHERE id=?",
                    ("表情选择失败，正文已保存", identity),
                )
            draft = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if (
            automatic
            and self.policy()["auto_send"]
            and not selection_failed
            and not check.get("output", {}).get("needs_review")
        ):
            self._origin_round(draft, active=True)
            if browsing := getattr(self.runtime, "browsing", None):
                browsing.sends.enqueue("messages", identity, direct=draft["source_kind"] == "message")
            else:
                await self.send(identity, automatic=True, authorization_epoch=authorization_epoch, _conversation_owned=True)

    def _origin_round(self, draft, *, active):
        evidence = json.loads(draft.get("video_evidence") or "null")
        identity = evidence.get("origin_round_id") if isinstance(evidence, dict) else None
        if not identity:
            return None
        repository = self.runtime.browsing_repository
        parent = repository.round(identity)
        trigger = json.loads(draft.get("trigger_source") or "{}")
        if (
            not parent
            or active and parent["status"] in {"cancelled", "invalid", "archived"}
            or active and trigger.get("managed_handoff") and parent["preview"]
            or parent["account_id"] != draft["account_id"]
            or parent["character_id"] != draft["character_id"]
            or parent["character_version"] != draft["character_version"]
        ):
            raise ValueError("私信所属浏览轮次已停止或身份变化，请重新预览")
        return parent

    async def _reply_to_conversation(self, draft, character, event, relationship, refs, links):
        if should_use_native_dm_turns(self):
            return await generate_native_dm_reply(self, draft, character, event, relationship, refs, links)
        config = self.settings.data["omni"]
        maximum = config.get("operations_max_tokens", 8192)
        scope = self.scope(draft)
        binding = reply_generation_binding(scope, character, event, "direct_message") | {
            "conversation_sha256": event["conversation_sha256"],
        }
        snapshot = runtime_identity(self.runtime, character, draft["account_id"])
        binding["self_identity"] = snapshot
        messages = await self.runtime.operation_context.prepare(
            scope,
            character,
            event,
            relationship,
            "direct_message",
            output_tokens=maximum,
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "conversation依次列出用户、号主和你已发送的话。open是待处理来信。"
                    "把相近的来信一起自然接话，正文放text，实际回复的来信编号放reply_to。"
                    "号主答过的放handled，填来信incoming、号主发言outgoing和对应原话quote。"
                    "待判断的来信留在open，在reason说明原因。全答过时text留空。" + REFLECTION_INSTRUCTION
                ),
            }
        )
        schema = ConversationReply.model_json_schema()
        schema["properties"]["handled"]["items"] = schema.pop("$defs")["IncomingSettlement"]
        schema["properties"]["reply_to"]["items"]["enum"] = sorted(refs)
        schema["required"] = ["text", "reason", "reply_to", "handled"]

        def validate(value):
            result = ConversationReply.model_validate(
                value.model_dump() if hasattr(value, "model_dump") else value
            )
            replied = set(result.reply_to)
            handled = {item.incoming for item in result.handled}
            if (
                not result.reason.strip()
                or replied & handled
                or not replied <= refs.keys()
                or len(handled) != len(result.handled)
                or any(
                    not links.get((item.incoming, item.outgoing))
                    or item.quote.strip() not in event["conversation"][item.outgoing - 1]["text"]
                    or not item.quote.strip()
                    for item in result.handled
                )
                or bool(replied) != bool(result.text.strip())
            ):
                raise ValueError("私信接力决定与当前来信或实际号主回复不一致")
            if result.text:
                result.text = validate_operation_text(result.text)
            return result

        def guard():
            if (
                self.runtime.character["id"] != character["id"]
                or runtime_identity(
                    self.runtime,
                    character,
                    draft["account_id"],
                )
                != snapshot
            ):
                raise ValueError("私信生成期间账号或角色已变化")

        async def generate(packet, wire_schema):
            return await budgeted_json(self.runtime.operation_llm, packet, wire_schema, max_tokens=maximum)

        async def invoke(packet, *, web_fetch=None):
            if web_fetch is not None:
                return await web_fetch.complete(packet, schema, generate)
            return await generate(packet, schema)

        decision, audit = await generate_with_recovery(
            self.runtime,
            channel="generation.direct_message",
            identity=event["event_id"] + ":" + content_hash(binding),
            binding=binding,
            character=character,
            messages=messages,
            generate=invoke,
            validate=validate,
            max_tokens=maximum,
            guard=guard,
            reuse_completed=True,
            allow_web_fetch=True,
            diagnose=lambda value: (
                "正文包含大量重复内容，保留原文待审"
                if value.text and operation_output_diagnostics(value.text)["needs_review"]
                else None
            ),
        )
        return decision, {
            "generation_identity": snapshot,
            "output": {
                "needs_review": any(value is None for value in links.values())
                or bool(audit and audit["state"] == "awaiting_review")
            },
        }

    def _apply_revision(self, identity, prepared, value):
        prepared.guard()
        candidate = (
            {"text": value, "message_kind": "text", "meme_id": None} if isinstance(value, str) else value
        )
        text = validate_operation_text(candidate["text"])
        meme_id = candidate.get("meme_id")
        owner = f"dm-draft:{identity}"
        if meme_id:
            self.memes.pin(meme_id, prepared.character, owner)
        try:
            self.store.execute(
                "UPDATE dm_drafts SET text=?,message_kind=?,meme_id=?,status='pending_review',error=NULL,updated_at=? WHERE id=?",
                (text, "image" if meme_id else "text", meme_id, time.time(), identity),
            )
        except BaseException:
            if self.memes:
                self.memes.release(owner, keep_asset_id=prepared.payload.get("meme_id"))
            raise
        if self.memes:
            self.memes.release(owner, keep_asset_id=meme_id)

    async def resume_continuation(self, identity, continuation_id):
        if str(identity).startswith("generation:"):
            identity = str(identity).split(":", 1)[1]
            row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if not row or row.get("generation_continuation_id") != continuation_id:
                raise ValueError("生成恢复任务与私信不匹配")
            continuation = self.runtime.continuations.get(continuation_id)
            if (
                continuation["binding"]["character"]
                != {key: self.runtime.character[key] for key in ("id", "version")}
                or continuation["binding"]["source"].get("text")
                != self._source(row["source_kind"], row["source_id"], self.runtime.character)["content"]
            ):
                raise ValueError("原始私信或角色已变化，请重新准备")
            self.runtime.continuations.resume(continuation["id"], binding=continuation["binding"])
            return {
                "status": "queued",
                **self.retry(identity),
                "continuation": generation_view(self.runtime, continuation_id, "messages", identity),
            }
        return await FeedbackPublication(self.runtime, self.store, self.operations).resume(
            "messages",
            identity,
            continuation_id,
            prepare=lambda: self._publication_context(identity, False, None),
            apply_revision=lambda prepared, value: self._apply_revision(identity, prepared, value),
            reconcile=lambda token, prepared: self._reconcile(identity),
        )
