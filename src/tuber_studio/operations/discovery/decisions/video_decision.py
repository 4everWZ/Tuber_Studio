"""Video content classification, visual follow-up, and top-level/engagement decisions."""

import asyncio
import json
from contextlib import ExitStack

from ....core.errors import ContextWindowExceeded, SendLimitReached
from ....core.meter import BrowsingBudgetDeferred, GenerationMeter
from ....core.models import content_hash
from ....core.text import operation_character
from ....infra.inference.providers import public_error
from ...kernel.context import agent_depth
from ...kernel.control import canonical_intent
from ...kernel.decision_engine import decision_explanation
from ...kernel.generation import GenerationInvalid
from ...kernel.inputs import (
    ContentDecisionInput,
    bind_content_decision,
    classification_registry,
    classification_source_groups,
    classification_view,
    join_content_sources,
    restore_stored_content_decisions,
)
from ...kernel.models import ContentDecision, ResponseDecision, VideoEngagementDecision
from ...kernel.quotas import DailyReplyLimitReached, day_window
from ..comments.reader import admit_comment_page, evaluate_comment_visit
from ..comments.timeline import merge_continuous_comments


class VideoDecisionMixin:
    """Chunked content evaluation, visual follow-up, and engagement/top-level decisions."""

    async def _chunks(self, material, row):
        segments = self.browsing._segments(material)
        presentation = self._content_presentation(material, row)
        output = row["config"]["decision_max_tokens"]
        provider = self.browsing.runtime.operation_llm
        if getattr(provider, "uses_model_card", False):
            await provider.ensure_capacity()
            available = provider.capacity_window - output
        else:
            context = self.browsing.settings.data["omni"]["context_tokens"]
            available = min(context // 2, context * 95 // 100 - output)
        if row["config"]["max_input_tokens"]:
            available = min(available, row["config"]["max_input_tokens"] // 2)
        if available <= 0:
            raise ContextWindowExceeded(
                "显式输出预留已占满可用窗口；调整窗口或输出设置后可重读，原文保持完整"
            )

        async def count(source):
            data, _ = presentation.present(source)
            _, messages, schema = self._model_input(row, ContentDecision, data, self._content_instruction())
            if hasattr(self.browsing.runtime.operation_llm, "prepare_json_messages"):
                messages = self.browsing.runtime.operation_llm.prepare_json_messages(
                    messages, schema, max_tokens=output
                )
            async with self.browsing.runtime.offline_generation():
                self.browsing._check_round(row)
                with GenerationMeter(self.browsing.repo, row["id"]):
                    return await self.browsing.runtime.operation_llm.count_tokens(messages)

        chunks, start = [], 0
        while start < len(segments):
            lower, upper, best = start + 1, len(segments), start
            if await count(segments[start:]) <= available:
                chunks.append(segments[start:])
                break
            while lower <= upper:
                middle = (lower + upper) // 2
                if await count(segments[start:middle]) <= available:
                    best, lower = middle, middle + 1
                else:
                    upper = middle - 1
            if best == start:
                raise ContextWindowExceeded(
                    "角色、Schema和至少一个原文片段连同显式输出预留超出当前容量；调整容量设置后可重读，未截断原文"
                )
            chunks.append(segments[start:best])
            start = best
        return chunks

    def _content_presentation(self, material, row):
        character = self.browsing.store.character(row["character_id"], row["character_version"])
        view = classification_view(self.browsing.settings.root, operation_character(character))
        return ContentDecisionInput(
            material,
            text_renderer=join_content_sources if view else None,
            source_grouper=classification_source_groups if view else None,
        )

    def _capacity_signature(self, config):
        base_hash = super()._capacity_signature(config)
        return content_hash(
            {
                "base": base_hash,
                "classification_views": classification_registry(self.browsing.settings.root),
            }
        )

    async def _content_decisions(self, material, row, metrics, *, allow_visual=None):
        decisions = []
        presentation = self._content_presentation(material, row)
        version = material.get("source_version") or self.browsing.evidence.source_version(material)
        for chunk in await self._chunks(material, row):
            can_watch = (
                self.browsing.evidence.has_visual_provider() and not material.get("visual_source_ids")
                if allow_visual is None
                else allow_visual
            )
            chunk_key = content_hash(
                {
                    "source_version": version,
                    "chunk_ids": [s["id"] for s in chunk],
                    "allow_visual": can_watch,
                    "visual_targets": 1,
                }
            )
            stored = self.browsing.repo.read_chunk(row, material["metadata"]["bvid"], chunk_key)
            if stored:
                decisions.extend(restore_stored_content_decisions(material, [stored]))
                continue
            data, aliases = presentation.present(chunk)
            data["allow_visual"] = can_watch and "subtitle" in data["source_kinds"]
            decision = await self._model(
                row,
                ContentDecision,
                data,
                self._content_instruction(),
                metrics,
                source_binding={
                    "source_version": version,
                    "chunk_ids": [segment["id"] for segment in chunk],
                },
            )
            resolved = bind_content_decision(decision, aliases)
            self.browsing.repo.save_read_chunk(row, material["metadata"]["bvid"], chunk_key, resolved)
            decisions.append(resolved)
        return decisions

    async def _followup_visual(self, result, row, metrics, decisions, followup):
        identity, material = result["id"], result["material"]
        requested = [decision for decision in decisions if decision.coverage_required == "visual"]
        if not requested or not self.browsing.evidence.has_visual_provider() or material.get("visual_source_ids"):
            return material, decisions
        targets = {
            (target.evidence_id, target.position, target.fraction, target.precision)
            for decision in requested
            for target in decision.visual_targets
        }
        source_ids = {ref for decision in requested for ref in decision.evidence_ids}
        if not source_ids:
            source_ids = {self.browsing._segments(material)[0]["id"]}
        requests = targets or source_ids
        task = {
            "id": "visual:"
            + content_hash(
                [
                    row["account_id"],
                    row["character_id"],
                    row["character_version"],
                    material["metadata"]["bvid"],
                    material["source_version"],
                    sorted(requests, key=repr),
                ]
            ),
            "round_id": row["id"],
            "parent_id": identity,
        }
        followup.update(
            id=task["id"],
            action="sample_frames",
            status="requested",
            decisions_before=[decision.model_dump() for decision in decisions],
            source_material_key=result["material_key"],
            evidence_ids=sorted(source_ids),
            targets=[
                {"evidence_id": source, "position": position, "fraction": fraction, "precision": precision}
                for source, position, fraction, precision in sorted(targets, key=repr)
            ],
        )
        payload = json.loads(
            self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
        )
        self.browsing.repo.complete(
            identity,
            result["owner"],
            result["generation"],
            "evidence_ready",
            payload | {"visual_followup": followup},
            result["material_key"],
        )

        async def read_frames():
            depth = agent_depth.set(agent_depth.get() + 1)
            try:
                return await self.browsing._prepare_evidence(
                    material["metadata"]["bvid"],
                    row["config"],
                    visual_source_ids=requests,
                    task=task,
                    deadline=row["deadline"],
                )
            finally:
                agent_depth.reset(depth)

        child = asyncio.create_task(read_frames(), name=task["id"])
        key, enriched = await child
        result.update(material_key=key, material=enriched)
        if error := enriched.get("visual_error"):
            followup.update(status="failed", error=error)
            return enriched, decisions
        followup.update(status="prepared", material_key=key)
        self.browsing.repo.complete(
            identity,
            result["owner"],
            result["generation"],
            "evidence_ready",
            payload
            | {
                "coverage": enriched["coverage"],
                "missing_parts": enriched["missing_parts"],
                "material_mode": self.browsing.evidence.material_mode(enriched),
                "visual_followup": followup,
            },
            key,
        )
        decisions = await self._content_decisions(enriched, row, metrics)
        followup["status"] = "completed"
        return enriched, decisions

    async def priority_top_level(self, active, row, material, context, metrics):
        """Use the ordinary source key, owner check and publication boundary."""
        metadata = material["metadata"]
        bvid = metadata["bvid"]
        scope = canonical_intent("bilibili", bvid, "top-level", row["character_id"])
        if self.browsing.publication.source_intent(row, scope) or any(
            str(item.get("platform_user_id") or item.get("user_id")) == row["account_id"]
            and str(item.get("parent_comment_id") or "0") in {"0", str(item.get("comment_id"))}
            for item in context["items"]
        ):
            return
        progress = self.browsing.repo.content_progress(row, bvid) or {}
        saved = progress.get("main_decision") or {}
        if saved.get("action") in {"finish", "skip", "read_comments"}:
            return
        references = {segment["id"] for segment in self.browsing._segments(material)}
        if not references or active["config"]["top_level_limit"] <= 0:
            return
        with self.browsing.comment_admission.slot(active, "top_level", bvid, direct=False) as admission:
            if admission:
                return
            return await self._priority_comment(active, row, material, context, references, metrics)

    async def _priority_comment(self, active, row, material, context, references, metrics):
        metadata = material["metadata"]
        bvid = metadata["bvid"]
        response = await self._response(row, material, context, references, "top_level", metrics)
        self.browsing.repo.save_main_decision(row, bvid, response)
        if response.action != "reply_public" or not self.browsing.operations.accepting_work:
            return
        try:
            self.browsing.operations.assert_authorized(
                automatic=True,
                channel="comments",
                account_id=row["account_id"],
                character=self.browsing.runtime.character,
                epoch=row["epoch"],
            )
        except ValueError:
            return
        if not self.browsing.effective()["top_level_enabled"]:
            return
        key, _ = await self.browsing.evidence._store(material)
        self.browsing._check_round(row)
        if (
            not self.browsing.operations.accepting_work
            or self.browsing.operations.policy()["epoch"] != row["epoch"]
            or not self.browsing.effective()["top_level_enabled"]
        ):
            return
        self.browsing.repo.reserve_budget(row["id"], top_levels=1)
        intent = self.browsing.publication._intent(
            active,
            "top_level",
            {"type": 1, "oid": str(metadata["aid"]), "bvid": bvid, "root": "0", "parent": "0"},
            response,
            [key],
            response.evidence_ids,
        )
        if intent and intent["auto_authorized"]:
            self.browsing.queue_publication(intent["id"])

    async def _evaluate_task(self, result, row, metrics):
        if result.get("comment_only"):
            return await evaluate_comment_visit(self.browsing, result, row, metrics)
        identity, material = result["id"], result["material"]
        heartbeat = result["heartbeat"]
        decisions, followup = [], {}
        direct_response, context = None, None
        payload = {}
        slots = ExitStack()
        try:
            progress = self.browsing.repo.content_progress(row, material["metadata"]["bvid"])
            main_decision = (progress or {}).get("main_decision")
            if main_decision and main_decision.get("action") == "wait":
                main_decision = None
            if main_decision:
                direct_response = ResponseDecision.model_validate(main_decision)
            previous = (
                self.browsing.store.one(
                    "SELECT t.payload FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id "
                    "WHERE r.account_id=? AND r.character_id=? AND t.material_key=? "
                    "AND t.status IN ('selected','viewed_without_comment') ORDER BY t.updated_at DESC LIMIT 1",
                    (row["account_id"], row["character_id"], result["material_key"]),
                )
                if not row["config"].get("force_read")
                else None
            )
            saved_decisions = json.loads(previous["payload"]).get("decisions") if previous else None
            if progress and not row["config"].get("force_read"):
                saved_decisions = progress["decisions"]
            if saved_decisions and not (
                main_decision and main_decision.get("action") in {"watch_frames", "read_more"}
            ):
                decisions = restore_stored_content_decisions(material, saved_decisions)
            elif row["config"].get("autonomous_batch"):
                segments = self.browsing._segments(material)
                references = {segment["id"] for segment in segments}
                existing_top = self.browsing.store.one(
                    "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                    (
                        row["account_id"],
                        row["character_id"],
                        canonical_intent("bilibili", material["metadata"]["bvid"], "top-level", row["character_id"]),
                    ),
                )
                if references and not existing_top and not main_decision:
                    admission = slots.enter_context(
                        self.browsing.comment_admission.slot(row, "top_level", material["metadata"]["bvid"], direct=False)
                    )
                    if admission:
                        raise admission
                    context = (
                        await self.browsing.video_comment_context(row, material["metadata"]["bvid"])
                        if row["config"]["top_level_enabled"]
                        else {"items": []}
                    )
                    own_top = any(
                        str(item.get("platform_user_id") or item.get("user_id")) == str(row["account_id"])
                        and str(item.get("parent_comment_id") or "0") in {"0", str(item.get("comment_id"))}
                        for item in context.get("items", [])
                    )
                    if not own_top:
                        direct_response = await self._response(
                            row, material, context, references, "top_level", metrics, video_actions=True
                        )
                        self.browsing.repo.save_main_decision(row, material["metadata"]["bvid"], direct_response)
                    if not direct_response or direct_response.action != "reply_public":
                        slots.close()
                action = direct_response.action if direct_response else "read_comments"
                if result.get("task_kind") == "comment_visit" and action in {"skip", "wait", "finish"}:
                    action = "read_comments"
                coverage = {"watch_frames": "visual", "read_more": "unread_part"}.get(action, "provided")
                can_continue = action in {"reply_public", "read_comments", "watch_frames", "read_more"}
                decisions = [
                    ContentDecision(
                        interest="like"
                        if can_continue and coverage == "provided"
                        else "need_more"
                        if can_continue
                        else "not_interested",
                        worth_commenting=can_continue and coverage == "provided",
                        reason="visual_required"
                        if coverage == "visual"
                        else "insufficient_evidence"
                        if coverage == "unread_part"
                        else "specific_detail"
                        if can_continue or action == "finish"
                        else "no_interest",
                        explanation=decision_explanation(direct_response, material)
                        if direct_response
                        else "本次继续浏览评论区，未准备新的主回复",
                        evidence_ids=(
                            direct_response.evidence_ids
                            if direct_response and direct_response.evidence_ids
                            else list(references)[:8]
                        ),
                        coverage_required=coverage,
                        visual_targets=direct_response.visual_targets if direct_response else [],
                    )
                ]
                if coverage != "provided":
                    if coverage == "visual":
                        material, decisions = await self._followup_visual(result, row, metrics, decisions, followup)
                    else:
                        decisions = await self._content_decisions(material, row, metrics)
                    direct_response = None
                    main_decision = None
            else:
                decisions = await self._content_decisions(material, row, metrics)
                material, decisions = await self._followup_visual(result, row, metrics, decisions, followup)
            actionable = [
                d
                for d in decisions
                if d.interest == "like"
                and d.worth_commenting
                and d.coverage_required == "provided"
                and d.reason == "specific_detail"
            ]
            payload = json.loads(
                self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
            ) | {
                "metadata": self._metadata_summary(material["metadata"]),
                "coverage": material["coverage"],
                "material_mode": self.browsing.evidence.material_mode(material),
                "visual_error": material.get("visual_error"),
                "visual_scan": material.get("visual_scan", []),
                "decisions": [d.model_dump() for d in decisions],
                "reason": "；".join(dict.fromkeys(decision_explanation(d, material) for d in decisions)),
                "subtitle_tracks": [
                    {k: part.get(k) for k in ("cid", "track_id", "language", "is_ai", "coverage")}
                    for part in material["parts"]
                ],
                "evidence_ids": sorted({ref for decision in decisions for ref in decision.evidence_ids}),
            }
            if followup:
                payload["visual_followup"] = followup
            if direct_response:
                payload["response_decision"] = direct_response.model_dump()
                payload["reason"] = decision_explanation(direct_response, material)
                self.browsing.repo.save_main_decision(
                    row, material["metadata"]["bvid"], direct_response.model_dump()
                )
            elif decisions and not actionable and not main_decision:
                self.browsing.repo.save_main_decision(
                    row,
                    material["metadata"]["bvid"],
                    {
                        "action": "skip",
                        "reason": "no_interest",
                        "text": "",
                        "evidence_ids": [],
                        "explanation": payload["reason"],
                    },
                )
            self.browsing.repo.save_content_progress(row, material, decisions)
            if trigger := getattr(self, "dynamic_trigger", None):
                trigger.notify_material_saved(row, material["metadata"]["bvid"])
            bvid = material["metadata"]["bvid"]
            aid = str(material["metadata"]["aid"])
            status = "comments_checked" if saved_decisions else "viewed_without_comment"
            source_key = canonical_intent("bilibili", bvid, "top-level", row["character_id"])
            existing = self.browsing.store.one(
                "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (row["account_id"], row["character_id"], source_key),
            )
            chosen = {ref for d in actionable for ref in d.evidence_ids} if actionable else set()
            comment_actions = row["config"].get("reply_enabled") or row["config"].get("comment_like_enabled")
            if not chosen and comment_actions:
                chosen = {segment["id"] for segment in self.browsing._segments(material)}

            need_comments = actionable and (row["config"]["top_level_enabled"] or comment_actions)
            if need_comments and not existing and context is None:
                context = await self.browsing.video_comment_context(row, bvid)

            items_list = context.get("items", []) if isinstance(context, dict) else []
            has_own_top_level = any(
                str(c.get("platform_user_id") or c.get("user_id")) == str(row["account_id"])
                and (
                    not c.get("parent_comment_id")
                    or str(c.get("parent_comment_id")) in ("0", "", "None", str(c.get("comment_id")))
                )
                for c in items_list
            )
            already_commented = bool(existing or has_own_top_level)

            if (
                actionable
                and row["config"]["top_level_enabled"]
                and not already_commented
                and (direct_response is None or direct_response.action == "reply_public")
                and (
                    row["config"].get("autonomous_batch")
                    or self.browsing.repo.round(row["id"])["budget"]["top_levels"] < row["config"]["top_level_limit"]
                )
            ):
                if not direct_response or main_decision:
                    admission = slots.enter_context(
                        self.browsing.comment_admission.slot(row, "top_level", bvid, direct=False)
                    )
                    if admission:
                        raise admission
                self.browsing.repo.reserve_budget(row["id"], top_levels=1)
                if context is None:
                    context = await self.browsing.video_comment_context(row, bvid)
                response = direct_response or await self._response(
                    row, material, context | {"_visual_followup": followup}, chosen, "top_level", metrics
                )
                payload["response_decision"] = response.model_dump()
                payload["reason"] = decision_explanation(response, material)
                self.browsing.repo.save_main_decision(row, bvid, response.model_dump())
                if response.action == "reply_public":
                    target = {"type": 1, "oid": aid, "bvid": bvid, "root": "0", "parent": "0"}
                    intent = self.browsing.publication._intent(
                        row, "top_level", target, response, [result["material_key"]], response.evidence_ids
                    )
                    payload["intent_id"] = intent["id"]
                    status = "selected"
                    if intent["auto_authorized"]:
                        self.browsing.queue_publication(intent["id"])

            slots.close()
            like_enabled = row["config"].get("video_like_enabled", False)
            fav_enabled = row["config"].get("video_fav_enabled", False)
            if (like_enabled or fav_enabled) and actionable:
                enabled_actions = [
                    kind for kind, enabled in (("video_like", like_enabled), ("video_fav", fav_enabled)) if enabled
                ]
                if all(self.browsing.comment_admission.requires_slot(row, kind) for kind in enabled_actions):
                    admission = slots.enter_context(
                        self.browsing.comment_admission.slot(row, enabled_actions[0], direct=False)
                    )
                    if admission:
                        raise admission
                engagement_decision = await self._engagement_decision(row, material, actionable, metrics)
                slots.close()
                bvid = material["metadata"]["bvid"]
                aid = material["metadata"]["aid"]
                title = material["metadata"].get("title", "")

                if engagement_decision.should_like and like_enabled:
                    like_source_key = canonical_intent("bilibili", bvid, "like", row["character_id"])
                    existing_like = self.browsing.store.one(
                        "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                        (row["account_id"], row["character_id"], like_source_key),
                    )
                    if not existing_like:
                        admission = slots.enter_context(
                            self.browsing.comment_admission.slot(row, "video_like", direct=False)
                        )
                        if admission:
                            raise admission
                        like_target = {"type": 1, "oid": str(aid), "bvid": bvid, "title": title, "action": "like"}
                        like_response = ResponseDecision(
                            action="reply_public",
                            text=engagement_decision.like_reason or "认可视频内容，给UP主点赞",
                            reason="light_interaction",
                            evidence_ids=list(chosen)[:4] if chosen else [],
                        )
                        like_intent = self.browsing.publication._intent(
                            row, "video_like", like_target, like_response, [result["material_key"]], chosen
                        )
                        payload["like_intent_id"] = like_intent["id"]
                        status = "selected"
                        if like_intent["auto_authorized"]:
                            self.browsing.queue_publication(like_intent["id"])
                        slots.close()

                if engagement_decision.should_favorite and fav_enabled:
                    fav_source_key = canonical_intent("bilibili", bvid, "favorite", row["character_id"])
                    existing_fav = self.browsing.store.one(
                        "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                        (row["account_id"], row["character_id"], fav_source_key),
                    )
                    if not existing_fav:
                        admission = slots.enter_context(
                            self.browsing.comment_admission.slot(row, "video_fav", direct=False)
                        )
                        if admission:
                            raise admission
                        fav_target = {
                            "type": 1,
                            "oid": str(aid),
                            "bvid": bvid,
                            "title": title,
                            "action": "favorite",
                        }
                        fav_response = ResponseDecision(
                            action="reply_public",
                            text=engagement_decision.favorite_reason or "视频质量极高，收藏备用",
                            reason="light_interaction",
                            evidence_ids=list(chosen)[:4] if chosen else [],
                        )
                        fav_intent = self.browsing.publication._intent(
                            row, "video_fav", fav_target, fav_response, [result["material_key"]], chosen
                        )
                        payload["fav_intent_id"] = fav_intent["id"]
                        status = "selected"
                        if fav_intent["auto_authorized"]:
                            self.browsing.queue_publication(fav_intent["id"])
                        slots.close()

            may_discuss = (
                result.get("task_kind") == "comment_visit"
                or direct_response is None
                or direct_response.action in {"reply_public", "read_comments"}
            )
            defer_comments = bool(row["config"].get("autonomous_batch") and comment_actions)
            if defer_comments:
                context = await self.browsing.video_comment_context(row, bvid)
                with self.browsing.store.db:
                    admit_comment_page(self.browsing, row, identity, material["metadata"], context.get("items", []))
            comments_complete = True
            if row["config"].get("reply_enabled", False) and chosen and not defer_comments and may_discuss:
                if context is None:
                    context = await self.browsing.video_comment_context(row, material["metadata"]["bvid"])
                raw_items = context.get("items", []) if isinstance(context, dict) else []
                for item in merge_continuous_comments(raw_items):
                    if self.browsing.comment_admission.review_reply_limit_reached(row, bvid):
                        comments_complete = False
                        status = "deferred_daily"
                        payload.update(
                            retry_kind="daily_reply_limit",
                            next_at=day_window(row["config"].get("business_timezone", "Asia/Taipei"))["next_at"],
                        )
                        break
                    thread_intent = await self._reply_to_video_comment(
                        row,
                        material,
                        result["material_key"],
                        chosen,
                        item,
                        raw_items,
                        metrics,
                        diagnostics=payload.setdefault("repetition_decisions", []),
                    )
                    if thread_intent:
                        payload.setdefault("reply_intent_ids", []).append(thread_intent["id"])
                        status = "selected"

            self.browsing.repo.complete(
                identity, result["owner"], result["generation"], status, payload, result["material_key"]
            )
            if not defer_comments and comments_complete:
                self.browsing.commit_comment_context(context)
            self.browsing.repo.mark_watch_result(
                row,
                bvid,
                fully_read=material["coverage"] == "all_available_cc" and not material.get("missing_parts"),
            )
            metrics["viewed"] += 1
        except (DailyReplyLimitReached, SendLimitReached) as error:
            self.browsing.repo.complete(
                identity,
                result["owner"],
                result["generation"],
                "deferred_daily",
                json.loads(
                    self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
                )
                | payload
                | {
                    "retry_kind": "send_limit" if isinstance(error, SendLimitReached) else "daily_reply_limit",
                    "next_at": error.next_at,
                },
                result["material_key"],
            )
        except Exception as error:
            if isinstance(error, GenerationInvalid):
                self.browsing.repo.mark_watch_result(row, material["metadata"]["bvid"], fully_read=False)
                metrics["viewed"] += 1
            metrics["failures"] += 1
            continuation = getattr(error, "continuation", None)
            round_budget = isinstance(error, BrowsingBudgetDeferred) or (
                continuation and continuation["feedback"].get("kind") == "round_budget"
            )
            if continuation:
                self.browsing.runtime.continuations.bind_source_view(
                    error.continuation["id"],
                    "browsing",
                    identity,
                    "浏览回应生成记录 · " + material["metadata"]["bvid"],
                )
            self.browsing.repo.complete(
                identity,
                result["owner"],
                result["generation"],
                "invalid"
                if isinstance(error, GenerationInvalid)
                else "checkpoint"
                if continuation or round_budget or isinstance(error, ContextWindowExceeded)
                else "failed",
                json.loads(
                    self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"]
                )
                | payload
                | {
                    "reason": (payload.get("reason") or public_error(error))
                    if isinstance(error, GenerationInvalid)
                    else "生成进度已保存，可继续"
                    if continuation
                    else public_error(error),
                    **({"retry_kind": "round_budget"} if round_budget else {}),
                    **({"generation_continuation_id": error.continuation["id"]} if continuation else {}),
                    **(
                        {"capacity_signature": self._capacity_signature(row["config"])}
                        if isinstance(error, ContextWindowExceeded)
                        else {}
                    ),
                    "decisions": [decision.model_dump() for decision in decisions],
                    **({"visual_followup": followup} if followup else {}),
                },
                result["material_key"],
                public_error(error),
            )
        finally:
            slots.close()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _engagement_decision(self, row, material, actionable, metrics):
        metadata = material.get("metadata", {})
        title = metadata.get("title", "")
        desc = metadata.get("description") or metadata.get("desc", "")
        owner = metadata.get("owner") or ""
        if isinstance(owner, dict):
            owner = owner.get("name", "")
        summary_evidence = [d.model_dump() for d in actionable] if actionable else []
        data = {
            "title": title,
            "owner": owner,
            "desc": desc[:200] if desc else "",
            "summary_evidence": summary_evidence[:4],
        }
        instruction = (
            "你正在像真实用户一样在 B 站“刷视频”。\n"
            "结合你的角色人设、兴趣偏好与视频内容，自主决策是否对该视频进行【点赞】和【收藏】。\n"
            "原则与要求：\n"
            "1. 真诚互动，不敷衍、不机械刷赞刷藏。\n"
            "2. 点赞（should_like）：当你觉得视频有趣、有启发、有共鸣、认可创作者的用心或契合你的角色兴趣时为 true，并用简短一句话说明理由；否则为 false。\n"
            "3. 收藏（should_favorite）：收藏的标准高于单纯点赞。仅当你觉得该视频质量极高、值得反复回看、当作重要素材或触动极深时为 true，并说明理由；一般视频仅点赞不收藏，为 false。\n"
            "4. 投币功能目前暂不启用（TBD）。"
        )
        try:
            return await self._model(
                row, VideoEngagementDecision, data, instruction, metrics, max_tokens=1024
            )
        except Exception:
            return VideoEngagementDecision()
