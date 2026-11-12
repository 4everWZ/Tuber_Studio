"""Evidence-bound browsing decisions and response generation."""

import asyncio
import json
import time
import unicodedata
from difflib import SequenceMatcher

from ...comments import category
from ...compaction import ContextWindowExceeded
from ...generation_meter import BrowsingBudgetDeferred, GenerationMeter, budgeted_json
from ...message_outreach import confirmed_invitation
from ...operation_continuations import content_hash
from ...operation_generation import GenerationInvalid, generate_with_recovery
from ...operation_identity import captured_identity
from ...operation_text import (
    operation_character,
    operation_output_diagnostics,
)
from ...operation_turns import operation_lane
from ...operations_control import canonical_intent
from ...providers import SYSTEM_RULES, public_error, reuse_turn_token_counts
from ...publication_errors import SendLimitReached
from ...store import dump
from .comment_reader import (
    CommentProgress,
    evaluate_comment_visit,
    handoff_comment,
    merge_continuous_comments,
)
from .inputs import (
    CLASSIFICATION_INPUT_VERSION,
    CLASSIFICATION_INSTRUCTION,
    RESPONSE_ACTOR_INSTRUCTION,
    ContentDecisionInput,
    ResponseDecisionInput,
    bind_content_decision,
    bind_response_actors,
    classification_registry,
    classification_source_groups,
    classification_view,
    compact_content_input,
    format_content_input,
    join_content_sources,
    restore_stored_content_decisions,
)
from .models import (
    CONTENT_DECISION_INTENT_GLOSS,
    RESPONSE_INSTRUCTION,
    RESPONSE_REFERENCE_INSTRUCTION,
    CandidateBatchPlan,
    ContentDecision,
    ResponseDecision,
    candidate_batch_plan,
    candidate_batch_schema,
    content_decision_schema,
    response_schema,
)
from .quotas import DailyReplyLimitReached, day_window
from .thread_context import comment_thread_context


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def decision_explanation(decision, material):
    """Keep legacy decisions readable without inventing a character thought."""
    if explanation := getattr(decision, "explanation", "").strip():
        return explanation
    title = material["metadata"].get("title") or material["metadata"]["bvid"]
    reasons = {
        "specific_detail": "选择就具体内容接话",
        "light_interaction": "选择参与这段交流",
        "no_interest": "本次选择不参与",
        "nothing_to_add": "看过后没有补充发言",
        "insufficient_evidence": "现有材料不足以完成这次回应",
        "visual_required": "需要补看画面后再决定",
        "unsafe_interaction": "本次未选择互动",
        "cooldown": "选择稍后继续这段交流",
        "spam": "跳过这条重复或灌水内容",
    }
    result = f"{title}：{reasons.get(decision.reason, '已保存本次行动选择')}"
    refs = set(decision.evidence_ids)
    selected = next(
        (
            segment["text"]
            for part in material["parts"]
            for segment in part["segments"]
            if segment["id"] in refs
        ),
        None,
    )
    return result + (f"；相关材料：{selected[:100]}" if selected else "")


class BrowsingDecisions:
    def __init__(self, browsing):
        self.browsing = browsing
        self.comment_progress = CommentProgress(browsing.store)

    def _response_actor_data(self, row, data):
        character = self.browsing.store.character(row["character_id"], row["character_version"])
        actor = self.browsing.runtime.identity_snapshot(character, row.get("account_id"))
        bound = bind_response_actors(data, actor)
        thread = bound.get("thread") or {}
        target, source = thread.get("target") or {}, thread.get("source") or {}
        video = bound.get("video_source") or {}
        if target.get("bvid") and target["bvid"] == video.get("bvid") and video.get("owner_uid"):
            thread["resource_author_uid"] = str(video["owner_uid"])
        root_id = (thread.get("addressing") or {}).get("source_root_comment_id") or target.get("root")
        if target.get("bvid") and (root_id or source.get("comment_id")):
            bound["own_public_experiences"] = self.browsing.runtime.daily_memory.context(
                character, video_id=target["bvid"], thread_id=root_id, comment_id=source.get("comment_id")
            )
        return bound

    def invited_private_response(self, row, thread):
        if not row["config"].get("comment_handoff_enabled"):
            return None
        policy = self.browsing.messages.policy()
        if not (policy["enabled"] and policy["allow_comment_handoff"]):
            return None
        source = thread.get("source") or {}
        recipient = (thread.get("target") or {}).get("recipient") or source.get("author_uid")
        invitation = confirmed_invitation(thread, row.get("account_id"), recipient)
        if invitation is None:
            return None
        return ResponseDecision(
            action="handoff_private", reason="user_requested",
            explanation="原评论请求私聊，" + invitation["quote"],
            contact_purpose=invitation["quote"], contact_trigger=invitation["kind"],
            evidence_ids=["public:" + str(source["comment_id"])],
        )

    def _model_input(self, row, schema, data, instruction, *, response_input=None):
        character = operation_character(
            self.browsing.store.character(row["character_id"], row["character_version"])
        )
        view = (
            classification_view(self.browsing.settings.root, character)
            if schema is ContentDecision
            else None
        )
        if schema is ResponseDecision:
            data = self._response_actor_data(row, data)
            response_input = response_input or ResponseDecisionInput(data)
            if data.get("kind") == "top_level" and data.get("coverage") == "sampled_visual":
                instruction += (
                    "按材料类型和时码区分字幕与画面。台词如果提到两者，就分别引用能支持它们的编号。"
                    "只看到了片段，可以就那个细节接话。"
                )
            instruction += RESPONSE_ACTOR_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION
        content = (
            response_input.format()
            if response_input is not None
            else format_content_input(data)
            if schema is ContentDecision
            else dump(data)
        )
        role = SYSTEM_RULES + dump({k: character[k] for k in ("core", "style", "rules")})
        if view is not None:
            content = compact_content_input(data)
            role = "角色：" + view["brief"]
            instruction = CLASSIFICATION_INSTRUCTION
        allow_visual = data.get("allow_visual", self.browsing.evidence.has_visual_provider())
        if schema is ContentDecision:
            instruction += (
                "需要补看时选visual，并引用最接近想补看时码的材料编号。"
                if allow_visual
                else "本次取样画面已到齐。请用现有材料判断，材料仍不足时可以暂不发言。"
            )
        messages = [
            {
                "role": "system",
                "content": role,
            },
            {
                "role": "system",
                "content": instruction,
            },
            {
                "role": "user",
                "content": "公开来源材料：" + content,
            },
        ]
        messages = self.browsing.runtime.identity_messages(messages, character, row.get("account_id"))
        model_schema = schema.model_json_schema()
        if schema is ContentDecision:
            model_schema = content_decision_schema(
                {segment["id"] for segment in data["segments"]}, allow_visual=allow_visual
            )
        elif schema is ResponseDecision:
            allowed = response_input.allowed_aliases
            model_schema = response_schema(
                allowed,
                video_actions=data.get("video_actions", False),
                allow_public=data.get("allow_public", True),
                allow_comment_like=data.get("allow_comment_like", False),
                allow_comments=data.get("allow_comments", True),
                allow_visual=allow_visual,
                grouped_comment_refs=response_input.grouped_comment_refs,
            )
        elif schema is CandidateBatchPlan:
            allowed_videos = [item["bvid"] for item in data["candidates"]]
            if len(allowed_videos) > row["config"]["candidate_limit"] or len(set(allowed_videos)) != len(
                allowed_videos
            ):
                raise ValueError("批次模型输入须按BV号去重并限制在设置的候选数量内")
            model_schema = candidate_batch_schema(data["candidates"], data["max_workers"])
        return character, messages, model_schema

    async def _model(
        self,
        row,
        schema,
        data,
        instruction,
        metrics=None,
        *,
        diagnose=None,
        source_binding=None,
        max_tokens=None,
    ):
        self.browsing._check_round(row)
        if schema is ResponseDecision:
            data = self._response_actor_data(row, data)
        response_input = ResponseDecisionInput(data) if schema is ResponseDecision else None
        video_response = schema is ResponseDecision and data.get("kind") == "top_level"
        character, messages, model_schema = self._model_input(
            row, schema, data, instruction, response_input=response_input
        )
        from .models import VideoEngagementDecision

        output = (
            max_tokens
            if max_tokens is not None
            else (
                min(row["config"]["decision_max_tokens"], 1024)
                if schema is VideoEngagementDecision
                else row["config"]["decision_max_tokens"]
            )
        )

        thread = data.get("thread") or {}
        profile_uid = (thread.get("source") or {}).get("author_uid")
        relations = self.browsing.runtime.relationships
        profile_revision = relations.profile_revision(
            row["account_id"], row["character_id"], "bilibili", profile_uid
        )

        def guard():
            self.browsing._check_round(row)
            if profile_revision != relations.profile_revision(
                row["account_id"], row["character_id"], "bilibili", profile_uid
            ):
                raise ValueError("等待模型期间用户档案已更新")
            source = data.get("source_identity")
            if source and self.browsing.publication.has_manual_draft(
                row, source["resource"], source["comment_id"],
            ):
                raise ValueError("等待模型期间同源评论已有候选，合并处理")

        async def invoke_model(packet, wire_schema):
            wait = time.monotonic()
            async with self.browsing.runtime.offline_generation():
                if metrics is not None:
                    metrics["lock_wait_seconds"] += time.monotonic() - wait
                guard()
                tokens = None
                with GenerationMeter(self.browsing.repo, row["id"]):
                    if video_response and getattr(self.browsing.runtime.llm, "generation_metered", False):
                        generate_video = getattr(self.browsing.runtime.llm, "generate_video_response", None)
                        if generate_video is not None:
                            return await generate_video(packet, wire_schema, max_tokens=output)
                    if not getattr(self.browsing.runtime.llm, "generation_metered", False):
                        counted = (
                            self.browsing.runtime.llm.prepare_json_messages(
                                packet, wire_schema, max_tokens=output
                            )
                            if hasattr(self.browsing.runtime.llm, "prepare_json_messages")
                            else packet
                        )
                        tokens = await self.browsing.runtime.llm.count_tokens(counted)
                        if tokens + output > self.browsing.settings.data["llm"]["context_tokens"] * 95 // 100:
                            raise ContextWindowExceeded("浏览输入超出真实窗口，未静默截断材料")
                    return await budgeted_json(
                        self.browsing.runtime.llm,
                        packet,
                        wire_schema,
                        max_tokens=output,
                        input_tokens=tokens,
                    )

        async def invoke(packet, *, web_fetch=None):
            if web_fetch is not None:
                return await web_fetch.complete(packet, model_schema, invoke_model)
            return await invoke_model(packet, model_schema)

        def validate(result):
            value = (
                response_input.validate_wire(result)
                if response_input is not None
                else candidate_batch_plan(result, data["candidates"])
                if schema is CandidateBatchPlan
                else schema.model_validate(result.model_dump() if hasattr(result, "model_dump") else result)
            )
            if schema is CandidateBatchPlan:
                allowed = {item["bvid"] for item in data["candidates"]}
                if (
                    not set(value.videos) <= allowed
                    or len(value.videos) > data["max_videos"]
                    or value.workers > data["max_workers"]
                ):
                    raise ValueError("批次计划须从本批候选选择视频，并遵守设置的并发上限")
            elif schema is ContentDecision:
                allowed = {segment["id"] for segment in data["segments"]}
                if (
                    not set(value.evidence_ids) <= allowed
                    or value.worth_commenting
                    and not value.evidence_ids
                ):
                    raise ValueError("内容判断引用不存在或为空")
            elif schema is ResponseDecision:
                if data.get("video_actions") and (
                    value.video_preference is None or not value.preference_reason.strip()
                ):
                    raise ValueError("视频行动需要具体的内容偏好与原因")
                if value.action == "reply_public" and not data.get("allow_public", True):
                    raise ValueError("本次未开放视频主评，可结束观看或补充材料")
                if value.action == "handoff_private":
                    raise ValueError("私信由原评论邀请创建，当前输出为公开回应")
                if value.action == "reply_public" and (
                    not value.evidence_ids or not value.text
                ):
                    raise ValueError("回应缺少有效正文或来源引用")
                if value.action == "reply_public" and value.reason not in {
                    "specific_detail",
                    "light_interaction",
                }:
                    raise ValueError("公开回复渠道与理由不一致")
            return value

        binding = {
            "task": "generation",
            "account_id": row["account_id"],
            "character": {"id": row["character_id"], "version": row["character_version"]},
            "schema": schema.__name__,
            "self_identity": captured_identity(messages),
            "source_sha256": content_hash(
                {
                    "backend_source": source_binding,
                    "data": {
                        key: (
                            {k: v for k, v in value.items() if k != "confirmed_relationship"}
                            if key == "thread" and isinstance(value, dict)
                            else value
                        )
                        for key, value in data.items()
                        if key != "recent_public_responses"
                    },
                }
            ),
        }
        if response_input is not None:
            binding["input_format"] = response_input.FORMAT_VERSION
        elif schema is CandidateBatchPlan:
            binding["input_format"] = "keyed-candidate-priorities-v4"
        if profile_revision is not None:
            binding["profile_revision"] = profile_revision
        if video_response:
            binding["video_response"] = "aligned-source-dry-v1"
        binding["model"] = self.browsing.model_signature()
        identity = content_hash(binding)
        value, _ = await generate_with_recovery(
            self.browsing.runtime,
            channel="generation.browsing",
            identity=identity,
            binding=binding,
            character=character,
            messages=messages,
            generate=invoke,
            validate=validate,
            max_tokens=output,
            guard=guard,
            parent_round_id=row["id"],
            deadline=row["deadline"],
            context_data={
                "instruction": messages[1]["content"],
                "source": response_input.format() if response_input is not None else data,
            },
            reuse_completed=True,
            diagnose=lambda value: (response_input.diagnose(value) if response_input is not None else None)
            or (diagnose(value) if diagnose else None),
            allow_web_fetch=response_input is not None,
        )
        result = response_input.resolve(value) if response_input is not None else value
        if schema is ResponseDecision:
            result._generation_identity = binding["self_identity"]
            saved_generation = self.browsing.runtime.continuations.find("generation.browsing", identity)
            result._generation_id = saved_generation["id"] if saved_generation else None
        return result

    @staticmethod
    def _video_response_segments(material, allowed):
        """Offer matching CC and observed frames without fetching again."""
        numbers = {str(part["cid"]): i for i, part in enumerate(material["metadata"]["parts"], 1)}
        tracks, frames, selected = {}, [], []
        for part in material["parts"]:
            cid = str(part["cid"])
            visual = part.get("visual", False)
            for segment in part["segments"]:
                source = dict(
                    segment, cid=cid, source_label=f"P{numbers[cid]} {'画面' if visual else '字幕'}"
                )
                if source["id"] in allowed:
                    selected.append(source)
                if visual:
                    frames.append(source)
                else:
                    tracks.setdefault(cid, []).append(source)
        added = set(allowed)
        for frame in frames:
            subtitles = [
                segment
                for segment in tracks.get(frame["cid"], ())
                if segment.get("start") is not None
                and segment.get("end") is not None
                and segment["start"] <= frame["start"] <= segment["end"]
            ]
            if frame["id"] in allowed and subtitles and not any(s["id"] in added for s in subtitles):
                subtitle = min(subtitles, key=lambda s: abs((s["start"] + s["end"]) / 2 - frame["start"]))
                selected.append(subtitle)
                added.add(subtitle["id"])
            if frame["id"] not in added and any(s["id"] in allowed for s in subtitles):
                selected.append(frame)
                added.add(frame["id"])
        return selected

    @staticmethod
    def _metadata_summary(metadata):
        return {
            key: metadata[key]
            for key in ("bvid", "aid", "title", "owner_uid", "owner", "duration")
            if key in metadata
        }

    @staticmethod
    def _content_instruction():
        return (
            "interest=like表示想参与这个话题，吐槽、共情和分享也属于参与；"
            "worth_commenting表示有具体想说的话。"
            "evidence_ids选择相关片段的id；值得发言时至少选一项。"
            "字幕/画面按分P和材料类型列出，[id|起始时码]标记可选择的内容块；evidence_ids选择这些短id。"
            "source_kinds和has_visual描述本次提供的材料，已有视觉观察和字幕都能作为依据。"
            "coverage_required表示还要补充的内容：当前材料够用选provided，"
            "需其它分P选unread_part；也可以不感兴趣或暂不发言。" + CONTENT_DECISION_INTENT_GLOSS
        )

    def _capacity_signature(self, config):
        llm = self.browsing.settings.data["llm"]
        return content_hash(
            {
                "context_tokens": llm["context_tokens"],
                "kind": llm.get("kind"),
                "model": llm.get("model"),
                "base_url": llm.get("base_url"),
                "output_tokens": config["decision_max_tokens"],
                "max_input_tokens": config["max_input_tokens"],
                "instruction": self._content_instruction(),
                "system": SYSTEM_RULES,
                "packing_protocol": "full-message-v1",
                "response_input_format": ResponseDecisionInput.FORMAT_VERSION,
                "classification_input_format": CLASSIFICATION_INPUT_VERSION,
                "classification_instruction": CLASSIFICATION_INSTRUCTION,
                "classification_views": classification_registry(self.browsing.settings.root),
            }
        )

    async def _chunks(self, material, row):
        # Count the same role, source and decoder schema that generation uses.
        segments = self.browsing._segments(material)
        presentation = self._content_presentation(material, row)
        output = row["config"]["decision_max_tokens"]
        context = self.browsing.settings.data["llm"]["context_tokens"]
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
            if hasattr(self.browsing.runtime.llm, "prepare_json_messages"):
                messages = self.browsing.runtime.llm.prepare_json_messages(
                    messages, schema, max_tokens=output
                )
            async with self.browsing.runtime.offline_generation():
                self.browsing._check_round(row)
                with GenerationMeter(self.browsing.repo, row["id"]):
                    return await self.browsing.runtime.llm.count_tokens(messages)

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

    @reuse_turn_token_counts
    async def _content_decisions(self, material, row, metrics):
        decisions = []
        presentation = self._content_presentation(material, row)
        version = material.get("source_version") or self.browsing.evidence.source_version(material)
        for chunk in await self._chunks(material, row):
            chunk_key = content_hash({"source_version": version, "chunk_ids": [s["id"] for s in chunk]})
            stored = self.browsing.repo.read_chunk(row, material["metadata"]["bvid"], chunk_key)
            if stored:
                decisions.extend(restore_stored_content_decisions(material, [stored]))
                continue
            data, aliases = presentation.present(chunk)
            data["allow_visual"] = self.browsing.evidence.has_visual_provider() and not material.get("visual_source_ids")
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
        source_ids = {ref for decision in requested for ref in decision.evidence_ids}
        if not source_ids:
            source_ids = {self.browsing._segments(material)[0]["id"]}
        followup.update(
            status="requested",
            decisions_before=[decision.model_dump() for decision in decisions],
            source_material_key=result["material_key"],
        )
        key, enriched = await self.browsing._prepare_evidence(
            material["metadata"]["bvid"], row["config"], visual_source_ids=source_ids,
            task={"id": identity}, deadline=row["deadline"],
        )
        result.update(material_key=key, material=enriched)
        if error := enriched.get("visual_error"):
            followup.update(status="failed", error=error)
            return enriched, decisions
        followup.update(status="prepared", material_key=key)
        payload = json.loads(self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))["payload"])
        self.browsing.repo.complete(
            identity, result["owner"], result["generation"], "evidence_ready",
            payload | {"coverage": enriched["coverage"], "missing_parts": enriched["missing_parts"],
                       "material_mode": self.browsing.evidence.material_mode(enriched), "visual_followup": followup},
            key,
        )
        decisions = await self._content_decisions(enriched, row, metrics)
        followup["status"] = "completed"
        return enriched, decisions

    async def _evaluate_task(self, result, row, metrics):
        if result.get("comment_only"):
            return await evaluate_comment_visit(self.browsing, result, row, metrics)
        identity, material = result["id"], result["material"]
        heartbeat = result["heartbeat"]
        decisions, followup = [], {}
        direct_response, context = None, None
        payload = {}
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
                        canonical_intent(
                            "bilibili", material["metadata"]["bvid"], "top-level", row["character_id"]
                        ),
                    ),
                )
                if references and not existing_top and not main_decision:
                    context = (
                        (await self.browsing.video_comment_context(row, material["metadata"]["bvid"]))
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
                        self.browsing.repo.save_main_decision(
                            row, material["metadata"]["bvid"], direct_response
                        )
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
                    )
                ]
                if coverage != "provided":
                    if coverage == "visual":
                        material, decisions = await self._followup_visual(
                            result, row, metrics, decisions, followup
                        )
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
                self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                    "payload"
                ]
            ) | {
                "metadata": self._metadata_summary(material["metadata"]),
                "coverage": material["coverage"],
                "material_mode": self.browsing.evidence.material_mode(material),
                "visual_error": material.get("visual_error"),
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

            # Pre-fetch comments to verify if account already left a top-level comment on Bilibili
            need_comments = actionable and (
                row["config"]["top_level_enabled"] or comment_actions
            )
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
                    or self.browsing.repo.round(row["id"])["budget"]["top_levels"]
                    < row["config"]["top_level_limit"]
                )
            ):
                self.browsing.repo.reserve_budget(row["id"], top_levels=1)
                if context is None:
                    context = await self.browsing.video_comment_context(row, bvid)
                response = direct_response or await self._response(
                    row, material, context, chosen, "top_level", metrics
                )
                payload["response_decision"] = response.model_dump()
                payload["reason"] = decision_explanation(response, material)
                self.browsing.repo.save_main_decision(row, bvid, response.model_dump())
                if response.action == "reply_public":
                    target = {
                        "type": 1,
                        "oid": aid,
                        "bvid": bvid,
                        "root": "0",
                        "parent": "0",
                    }
                    intent = self.browsing.publication._intent(
                        row, "top_level", target, response, [result["material_key"]], response.evidence_ids
                    )
                    payload["intent_id"] = intent["id"]
                    status = "selected"
                    if intent["auto_authorized"]:
                        await self.browsing.publish(intent["id"], automatic=True)

            like_enabled = row["config"].get("video_like_enabled", False)
            fav_enabled = row["config"].get("video_fav_enabled", False)
            if (like_enabled or fav_enabled) and actionable:
                engagement_decision = await self._engagement_decision(row, material, actionable, metrics)
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
                        like_target = {
                            "type": 1,
                            "oid": str(aid),
                            "bvid": bvid,
                            "title": title,
                            "action": "like",
                        }
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
                            await self.browsing.publish(like_intent["id"], automatic=True)

                if engagement_decision.should_favorite and fav_enabled:
                    fav_source_key = canonical_intent("bilibili", bvid, "favorite", row["character_id"])
                    existing_fav = self.browsing.store.one(
                        "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                        (row["account_id"], row["character_id"], fav_source_key),
                    )
                    if not existing_fav:
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
                            await self.browsing.publish(fav_intent["id"], automatic=True)

            may_discuss = (
                result.get("task_kind") == "comment_visit"
                or direct_response is None
                or direct_response.action in {"reply_public", "read_comments"}
            )
            defer_comments = (
                row["config"].get("batch_id")
                and payload.get("task_kind", "new_video") == "new_video"
                and comment_actions
                and chosen
                and may_discuss
            )
            comments_complete = True
            if row["config"].get("reply_enabled", False) and chosen and not defer_comments and may_discuss:
                if context is None:
                    context = await self.browsing.video_comment_context(row, material["metadata"]["bvid"])
                raw_items = context.get("items", []) if isinstance(context, dict) else []
                for item in merge_continuous_comments(raw_items):
                    if self.browsing.repo.video_reply_limit_reached(row, bvid):
                        comments_complete = False
                        status = "deferred_daily"
                        payload.update(
                            retry_kind="daily_reply_limit",
                            next_at=day_window(row["config"].get("business_timezone", "Asia/Taipei"))[
                                "next_at"
                            ],
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
            if defer_comments:
                await self.browsing.defer_comment_visit(row, bvid, metrics)
        except (DailyReplyLimitReached, SendLimitReached) as error:
            self.browsing.repo.complete(
                identity,
                result["owner"],
                result["generation"],
                "deferred_daily",
                json.loads(
                    self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                        "payload"
                    ]
                )
                | payload
                | {
                    "retry_kind": "send_limit"
                    if isinstance(error, SendLimitReached)
                    else "daily_reply_limit",
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
                "invalid" if isinstance(error, GenerationInvalid) else "checkpoint"
                if continuation or round_budget or isinstance(error, ContextWindowExceeded)
                else "failed",
                json.loads(
                    self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                        "payload"
                    ]
                )
                | payload
                | {
                    "reason": (payload.get("reason") or public_error(error)) if isinstance(error, GenerationInvalid)
                    else "生成进度已保存，可继续" if continuation else public_error(error),
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
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

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
        lane = operation_lane.set("reply")
        try:
            async with self.browsing.runtime.conversation_turn(scope) as waited:
                self.browsing._check_round(row)
                if self.browsing.repo.video_reply_limit_reached(row, bvid):
                    return None
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
                        candidates = [by_id[key] for record in candidates
                                      if (key := str(record["comment_id"])) in by_id]
                    elif hasattr(self.browsing.adapter, "read_video_reply"):
                        source = await self.browsing._read(self.browsing.adapter.read_video_reply, target)
                        candidates = [{
                            **{key: value for key, value in item.items() if key != "grouped_comments"},
                            "content": source["text"],
                            "text": source["text"],
                            "platform_user_id": source["author_uid"],
                            "user_id": source["author_uid"],
                        }]
                pending = []
                for candidate in candidates:
                    body = str(candidate.get("text") or candidate.get("content") or "")
                    uid = str(candidate.get("user_id") or candidate.get("platform_user_id") or "")
                    if uid != author_uid or len(body.strip()) < 2 or category(body) == "spam":
                        continue
                    candidate_id = candidate["comment_id"]
                    key = canonical_intent("bilibili", bvid, candidate_id, row["character_id"])
                    if not self.browsing.publication.source_intent(row, key) and not self.browsing.publication.has_manual_draft(
                        row, bvid, candidate_id,
                    ):
                        pending.append(candidate)
                candidates = pending
                if not candidates:
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
                    thread_response = self.comment_progress.get(row, bvid, source)
                    if thread_response and thread_response.action == "handoff_private":
                        thread_response = None
                if thread_response is None:
                    try:
                        thread_response = await self._response(
                            row, material, thread_context,
                            chosen if material else {"public:" + comment_id}, "reply_public", metrics
                        )
                    except GenerationInvalid as error:
                        if diagnostics is not None:
                            diagnostics.append({"comment_id": source["comment_id"],
                                "continuation_id": error.continuation["id"], "state": "invalid",
                                "feedback": error.continuation["feedback"],
                                "original_candidate": error.continuation.get("original_output"),
                                "current_candidate": error.continuation.get("current_output")})
                        return None
                    self.comment_progress.save(row, bvid, source, thread_response)
                if thread_response.action not in {"reply_public", "handoff_private"}:
                    return None
                if thread_response.action == "reply_public" and not thread_response.text:
                    return None
                target.update(
                    recipient=author_uid,
                    recipient_name=thread_context["source"]["author_name"],
                    source_text=item_text,
                )
                if thread_response.action == "handoff_private":
                    return await handoff_comment(
                        self.browsing, row, thread_response, target, thread_context, material,
                        [material_key] if material_key else [], created_at=item.get("created_at", ""),
                    )
                thread_intent = self.browsing.publication._intent(
                    row, "reply_public", target, thread_response,
                    [material_key] if material_key else [], thread_response.evidence_ids
                )
                if not thread_intent:
                    return None
                if thread_intent["auto_authorized"]:
                    await self.browsing.publish(thread_intent["id"], automatic=True)
                return thread_intent
        finally:
            operation_lane.reset(lane)

    @staticmethod
    def _video_comment_context(item, items, target):
        return comment_thread_context(item, items, target)

    async def _response(self, row, material, context, allowed, kind, metrics, *, video_actions=False):
        context = dict(context)
        context.pop("_scroll_progress", None)
        allow_public = context.pop("_allow_public", True)
        allow_comment_like = context.pop("_allow_comment_like", False)
        if kind != "top_level":
            metadata = material.get("metadata", {}) if material else {}
            if (context.get("target") or {}).get("bvid") == metadata.get("bvid") and metadata.get("owner_uid"):
                context["resource_author_uid"] = str(metadata["owner_uid"])
            if invitation := self.invited_private_response(row, context):
                return invitation
        if uid := (context.get("source") or {}).get("author_uid"):
            context = context | {
                "confirmed_relationship": self.browsing.runtime.relationships.view(
                    row["character_id"], "bilibili", str(uid), account_id=row["account_id"]
                )
            }
        if material and kind == "top_level" and material["coverage"] == "sampled_visual":
            segments = self._video_response_segments(material, allowed)
        else:
            segments = (
                [s for s in self.browsing._segments(material) if s["id"] in allowed] if material else []
            )
        recent = self.browsing.store.all(
            "SELECT text FROM browsing_intents WHERE status='published' AND account_id=? AND character_id=? "
            "AND kind IN ('top_level','reply_public') AND updated_at>=? ORDER BY updated_at DESC LIMIT ?",
            (
                row["account_id"], row["character_id"],
                self.browsing.store.get("browsing_history_visible_since", 0),
                row["config"]["recent_sample_limit"],
            ),
        )
        data = {
            "kind": kind,
            "evidence": segments,
            "thread": {} if kind == "top_level" else context,
            **({"video_source": self._metadata_summary(material["metadata"])} if material else {}),
            "coverage": material["coverage"] if material else "public_text",
            **({"allow_public": allow_public, "allow_comment_like": allow_comment_like}
               if allow_comment_like or not allow_public else {}),
            **({"video_actions": True, "allow_visual": self.browsing.evidence.has_visual_provider()
                and not material.get("visual_source_ids")} if video_actions else {}),
            **(
                {
                    "allow_public": row["config"]["top_level_enabled"],
                    "allow_comments": bool(row["config"].get("reply_enabled") or row["config"].get("comment_like_enabled")),
                }
                if video_actions
                else {}
            ),
        }
        if not material:
            data["allowed_evidence_ids"] = list(allowed)
        if kind != "top_level" and context.get("source") and context.get("target"):
            target = context["target"]
            # The current comment remains a real response source even when
            # video subtitles/frames are available as additional background.
            data["allowed_evidence_ids"] = sorted(
                set(data.get("allowed_evidence_ids", []))
                | {"public:" + str(context["source"]["comment_id"])}
                | {"public:" + str(item["comment_id"]) for item in context.get("grouped_comments", [])}
            )
            data["source_identity"] = {
                "resource": target.get("bvid") or "dynamic:" + str(target["oid"]),
                "comment_id": str(context["source"]["comment_id"]),
            }
        return await self._model(
            row,
            ResponseDecision,
            data,
            RESPONSE_INSTRUCTION
            + (" 想给当前评论点个赞时选like_comment，引用该评论，正文留空。"
               if allow_comment_like else "")
            + (" grouped_comments是同一用户对同一对象的连续来评。若一并公开回答，"
               "covered_comments列当前回复目标和正文一并回答的其他来评编号。"
               if context.get("grouped_comments") else "")
            + (
                "想发主评选reply_public并写正文。翻评论选read_comments，补看画面选watch_frames并引用附近时码，"
                "补读选read_more，结束观看选finish，略过选skip，稍后处理选wait。"
                "video_preference填like/not_interested/neutral，preference_reason说说视频里具体哪点影响了你的偏好。"
                if video_actions
                else ""
            ),
            metrics,
            diagnose=lambda value: (
                self.repetition_reason(value.text, recent, context)
                or (
                    "正文包含大量重复内容；结合表达意图选择保留、修订或暂缓"
                    if operation_output_diagnostics(value.text)["needs_review"]
                    else None
                )
                if value.action in {"reply_public", "handoff_private"}
                else None
            ),
        )

    @staticmethod
    def repetition_reason(text, recent, context):
        value = normalized(text)
        if len(value) < 12:
            return None
        samples = [r["text"] for r in recent]
        for sample in samples:
            other = normalized(sample)
            if other and (value == other or SequenceMatcher(None, value, other).ratio() > 0.85):
                return "正文与近期公开发言／热评重复"
        if len(value) >= 24:
            for item in context.get("items", []):
                other = normalized(item.get("content", item.get("text", "")))
                if other and SequenceMatcher(None, value, other).ratio() > 0.94:
                    return "正文与来源评论大段重复"
        return None

    async def _engagement_decision(self, row, material, actionable, metrics):
        from .models import VideoEngagementDecision

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
                row,
                VideoEngagementDecision,
                data,
                instruction,
                metrics,
                max_tokens=1024,
            )
        except Exception:
            return VideoEngagementDecision()
