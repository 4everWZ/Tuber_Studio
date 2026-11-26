"""Discovery thread/comment reply evaluation with OperationClaims Inbox preemption."""

from contextlib import ExitStack

from ....core.turns import operation_lane
from ...channels.comments import category
from ...channels.comments.materials import prepare_comment_materials
from ...channels.direct_messages.outreach import handoff_comment
from ...kernel.control import canonical_intent
from ...kernel.generation import GenerationInvalid
from ...kernel.inputs import bind_response_actors
from ...kernel.quotas import DailyReplyLimitReached, day_window
from ..comments.admission import (
    release_discovery_claims,
    try_claim_discovery_target,
    verify_discovery_claims,
)
from ..comments.timeline import comment_thread_context, merge_continuous_comments, source_discussion


class CommentDecisionMixin:
    """Thread comment reply decisions with atomic claim yield to Inbox."""

    @staticmethod
    def _video_comment_context(item, items, target):
        return comment_thread_context(item, items, target)

    async def _reply_to_video_comment(
        self, row, material, material_key, chosen, item, items, metrics, *, diagnostics=None, metadata=None
    ):
        author_uid = str(item.get("user_id") or item.get("platform_user_id") or "")
        if not author_uid or author_uid == str(row["account_id"]):
            return None
        comment_id = str(item["comment_id"])
        root_id = str(item.get("thread_id") or comment_id)
        metadata = material["metadata"] if material else metadata
        bvid = metadata["bvid"]
        scope = (str(row["account_id"]), row["character_id"], bvid, root_id)
        lane = operation_lane.set("background")
        slots = ExitStack()
        claim_handle = None
        try:
            claim_handle = try_claim_discovery_target(
                self.browsing,
                row,
                resource_type="video",
                resource_id=bvid,
                root_id=root_id,
                comment_id=comment_id,
                oid=metadata.get("aid") if metadata else None,
            )
            if claim_handle is False:
                return None
            async with self.browsing.runtime.conversation_turn(scope) as waited:
                self.browsing._check_round(row)
                if not verify_discovery_claims(claim_handle):
                    return None
                if self.browsing.comment_admission.review_reply_limit_reached(row, bvid):
                    raise DailyReplyLimitReached(day_window(row["config"].get("business_timezone", "Asia/Taipei")))
                target = {
                    "type": 1,
                    "oid": str(metadata["aid"]),
                    "bvid": bvid,
                    "root": root_id,
                    "parent": comment_id,
                }
                candidates = item.get("grouped_comments") or [item]
                if waited:
                    read_thread = getattr(self.browsing.adapter, "read_video_thread", None)
                    if read_thread is not None:
                        fresh = await self.browsing._read(read_thread, target)
                        items = fresh["items"]
                        by_id = {str(record["comment_id"]): record for record in items}
                        candidates = [
                            by_id[key] for record in candidates if (key := str(record["comment_id"])) in by_id
                        ]
                    elif hasattr(self.browsing.adapter, "read_video_reply"):
                        source = await self.browsing._read(self.browsing.adapter.read_video_reply, target)
                        candidates = [
                            {
                                **{key: value for key, value in item.items() if key != "grouped_comments"},
                                "content": source["text"],
                                "text": source["text"],
                                "platform_user_id": source["author_uid"],
                                "user_id": source["author_uid"],
                            }
                        ]
                pending = []
                for candidate in candidates:
                    body = str(candidate.get("text") or candidate.get("content") or "")
                    uid = str(candidate.get("user_id") or candidate.get("platform_user_id") or "")
                    attachments = candidate.get("attachments") or []
                    if uid != author_uid or (len(body.strip()) < 2 and not attachments) or category(body, attachments) == "spam":
                        continue
                    candidate_id = candidate["comment_id"]
                    key = canonical_intent("bilibili", bvid, candidate_id, row["character_id"])
                    if not self.browsing.publication.source_intent(row, key) and not self.browsing.publication.has_manual_draft(
                        row, bvid, candidate_id
                    ):
                        pending.append(candidate)
                candidates = pending
                if not candidates or not verify_discovery_claims(claim_handle):
                    return None
                item = merge_continuous_comments(candidates)[-1]
                comment_id = str(item["comment_id"])
                target["parent"] = comment_id
                item_text = str(item.get("text") or item.get("content") or "")
                thread_context = self._video_comment_context(item, items, target)
                if metadata and metadata.get("owner_uid"):
                    thread_context["resource_author_uid"] = str(metadata["owner_uid"])
                source = thread_context["source"]
                if grouped := thread_context.get("grouped_comments"):
                    source = source | {"grouped_comments": grouped}
                thread_response = self.invited_private_response(row, thread_context)
                if thread_response is None:
                    bound_addressing = (
                        (
                            bind_response_actors(
                                {"video_source": metadata or {}, "thread": thread_context},
                                {"account_uid": str(row.get("account_id") or "")},
                            ).get("thread")
                            or {}
                        ).get("addressing")
                        or {}
                    )
                    if (
                        bound_addressing.get("conversation_nature") == "third_party_dialogue"
                        and not bound_addressing.get("is_explicitly_mentioned")
                        and not bound_addressing.get("is_reply_to_self")
                    ):
                        return None
                if thread_response is None:
                    admission = slots.enter_context(
                        self.browsing.comment_admission.slot(row, "reply_public", bvid, author_uid, direct=False)
                    )
                    if admission:
                        raise admission
                    thread_response = self.comment_progress.get(row, bvid, source)
                    if thread_response:
                        try:
                            self.browsing.runtime.validate_memory_delivery(thread_response._memory_audit)
                        except ValueError:
                            thread_response = None
                    if thread_response and thread_response.action == "handoff_private":
                        thread_response = None
                if thread_response is None:
                    if not verify_discovery_claims(claim_handle):
                        return None
                    try:
                        comment_materials = await prepare_comment_materials(self.browsing, target, item, row=row)
                        if comment_materials:
                            thread_context["_comment_materials"] = comment_materials
                        thread_response = await self._response(
                            row,
                            material,
                            thread_context,
                            chosen if material else {"public:" + comment_id},
                            "reply_public",
                            metrics,
                        )
                    except GenerationInvalid as error:
                        if diagnostics is not None:
                            diagnostics.append(
                                {
                                    "comment_id": source["comment_id"],
                                    "continuation_id": error.continuation["id"],
                                    "state": "invalid",
                                    "feedback": error.continuation["feedback"],
                                    "original_candidate": error.continuation.get("original_output"),
                                    "current_candidate": error.continuation.get("current_output"),
                                }
                            )
                        return None
                    if not verify_discovery_claims(claim_handle):
                        return None
                    self.comment_progress.save(row, bvid, source, thread_response)
                if thread_response.action not in {"reply_public", "handoff_private"}:
                    return None
                if thread_response.action == "reply_public" and not thread_response.text:
                    return None
                if not verify_discovery_claims(claim_handle):
                    return None
                target.update(
                    recipient=author_uid,
                    recipient_name=thread_context["source"]["author_name"],
                    source_text=item_text,
                )
                if thread_response.action == "handoff_private":
                    return await handoff_comment(
                        self.browsing,
                        row,
                        thread_response,
                        target,
                        thread_context,
                        material,
                        [material_key] if material_key else [],
                        created_at=item.get("created_at", ""),
                    )
                target["source_discussion"] = source_discussion(item, thread_context, items)
                if thread_response._comment_materials:
                    target["comment_materials"] = thread_response._comment_materials
                thread_intent = self.browsing.publication._intent(
                    row,
                    "reply_public",
                    target,
                    thread_response,
                    [material_key] if material_key else [],
                    thread_response.evidence_ids,
                )
                if not thread_intent:
                    return None
                if thread_intent["auto_authorized"]:
                    self.browsing.queue_publication(thread_intent["id"])
                return thread_intent
        finally:
            release_discovery_claims(claim_handle)
            slots.close()
            operation_lane.reset(lane)
