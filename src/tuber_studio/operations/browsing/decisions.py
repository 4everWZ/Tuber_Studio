"""Evidence-bound browsing decisions and response generation."""

import asyncio
import datetime
import json
import time
import unicodedata
from difflib import SequenceMatcher

from ...comments import category
from ...compaction import ContextWindowExceeded
from ...generation_meter import GenerationMeter, budgeted_json
from ...operation_continuations import content_hash
from ...operation_generation import generate_with_recovery
from ...operation_text import (
    operation_character,
    operation_output_diagnostics,
)
from ...operations_control import canonical_intent
from ...persona import built_in_anchor
from ...providers import SYSTEM_RULES, public_error, reuse_turn_token_counts
from ...store import dump
from .inputs import (
    CLASSIFICATION_INPUT_VERSION,
    CLASSIFICATION_INSTRUCTION,
    ContentDecisionInput,
    ResponseDecisionInput,
    bind_content_decision,
    classification_registry,
    classification_source_groups,
    classification_view,
    compact_content_input,
    format_content_input,
    join_content_sources,
)
from .models import (
    CONTENT_DECISION_INTENT_GLOSS,
    RESPONSE_INSTRUCTION,
    RESPONSE_REFERENCE_INSTRUCTION,
    ContentDecision,
    ResponseDecision,
    content_decision_schema,
    response_schema,
)
from .round_context import discovery_retry, generation_resume


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


class BrowsingDecisions:
    def __init__(self, browsing):
        self.browsing = browsing

    def _model_input(self, row, schema, data, instruction, *, response_input=None):
        character = self.browsing.store.character(row["character_id"], row["character_version"])
        anchor = built_in_anchor(
            character, self.browsing.settings.data, for_response=schema is ResponseDecision
        )
        if not anchor:
            character = operation_character(character)
        view = (
            classification_view(self.browsing.settings.root, character)
            if schema is ContentDecision and not anchor
            else None
        )
        if schema is ResponseDecision:
            response_input = response_input or ResponseDecisionInput(data)
            if data.get("kind") == "top_level" and data.get("coverage") == "sampled_visual":
                instruction += (
                    "按材料类型和时码区分字幕与画面。台词如果提到两者，就分别引用能支持它们的编号。"
                    "只看到了片段，可以就那个细节接话。"
                )
            instruction += RESPONSE_REFERENCE_INSTRUCTION
        content = (
            response_input.format()
            if response_input is not None
            else format_content_input(data)
            if schema is ContentDecision
            else dump(data)
        )
        role = anchor or SYSTEM_RULES + dump({k: character[k] for k in ("core", "style", "rules")})
        if view is not None:
            content = compact_content_input(data)
            role = "角色：" + view["brief"]
            instruction = CLASSIFICATION_INSTRUCTION
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
        model_schema = schema.model_json_schema()
        if schema is ContentDecision:
            model_schema = content_decision_schema({segment["id"] for segment in data["segments"]})
        elif schema is ResponseDecision:
            allowed = response_input.allowed_aliases
            model_schema = response_schema(allowed, allow_handoff=data.get("kind") != "top_level")
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

        def guard():
            self.browsing._check_round(row)
            source = data.get("source_identity")
            if source and self.browsing.store.one(
                "SELECT d.id FROM drafts d JOIN comments c ON c.id=d.comment_id WHERE c.platform='bilibili' AND c.video_id=? AND c.comment_id=? AND d.character_id=?",
                (source["resource"], source["comment_id"], row["character_id"]),
            ):
                raise ValueError("等待模型期间同源评论已有候选，合并处理")

        async def invoke(packet):
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
                            return await generate_video(packet, model_schema, max_tokens=output)
                    if not getattr(self.browsing.runtime.llm, "generation_metered", False):
                        counted = (
                            self.browsing.runtime.llm.prepare_json_messages(
                                packet, model_schema, max_tokens=output
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
                        model_schema,
                        max_tokens=output,
                        input_tokens=tokens,
                    )

        def validate(result):
            value = (
                response_input.validate_wire(result)
                if response_input is not None
                else schema.model_validate(result.model_dump() if hasattr(result, "model_dump") else result)
            )
            if schema is ContentDecision:
                allowed = {segment["id"] for segment in data["segments"]}
                if (
                    not set(value.evidence_ids) <= allowed
                    or value.worth_commenting
                    and not value.evidence_ids
                ):
                    raise ValueError("内容判断引用不存在或为空")
            elif schema is ResponseDecision:
                if value.action in {"reply_public", "handoff_private"} and (
                    not value.evidence_ids or not value.text
                ):
                    raise ValueError("回应缺少有效正文或来源引用")
                if value.action == "reply_public" and value.reason not in {
                    "specific_detail",
                    "light_interaction",
                }:
                    raise ValueError("公开回复渠道与理由不一致")
                if value.action == "handoff_private" and (
                    data.get("kind") == "top_level"
                    or value.reason not in {"private_details", "user_requested", "long_followup"}
                ):
                    raise ValueError("私信渠道与来源／理由不一致")
            return value

        binding = {
            "task": "generation",
            "account_id": row["account_id"],
            "character": {"id": row["character_id"], "version": row["character_version"]},
            "schema": schema.__name__,
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
        if video_response:
            binding["video_response"] = "aligned-source-dry-v1"
        independent = row["config"].get("discovery_publish")
        retry = discovery_retry.get()
        if independent:
            binding["discovery_model"] = self.browsing.model_signature()
            binding["discovery_video"] = retry["bvid"] if retry else None
            if retry and retry["error"]:
                messages[1] = messages[1] | {
                    "content": messages[1]["content"] + "\n上次运行反馈（数据）：" + dump(retry["error"])
                }
        value, _ = await generate_with_recovery(
            self.browsing.runtime,
            channel="generation.browsing",
            identity=content_hash(binding),
            binding=binding,
            character=character,
            messages=messages,
            generate=invoke,
            validate=validate,
            max_tokens=output,
            guard=guard,
            parent_round_id=row["id"],
            deadline=row["deadline"],
            generation_context=self.browsing.runtime.offline_generation,
            context_data={
                "instruction": messages[1]["content"],
                "source": response_input.format() if response_input is not None else data,
            },
            reuse_completed=generation_resume.get() or bool(independent and retry and retry["failures"]),
            max_recoveries=(1 if retry and retry["failures"] else 0) if independent else None,
            diagnose=diagnose,
        )
        return response_input.resolve(value) if response_input is not None else value

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
            "需新的画面选visual，需其它分P选unread_part；也可以不感兴趣或暂不发言。"
            + CONTENT_DECISION_INTENT_GLOSS
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
        available = min(context // 2, context * 95 // 100 - output, row["config"]["max_input_tokens"] // 2)
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
        view = (
            None
            if built_in_anchor(character, self.browsing.settings.data, for_response=False)
            else classification_view(self.browsing.settings.root, operation_character(character))
        )
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
            data, aliases = presentation.present(chunk)
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
            decisions.append(bind_content_decision(decision, aliases))
        return decisions

    async def _followup_visual(self, result, row, metrics, decisions, followup):
        identity, material = result["id"], result["material"]
        if any(decision.coverage_required == "visual" for decision in decisions) and not any(
            part.get("visual") for part in material["parts"]
        ):
            followup.update(
                status="requested" if row["config"]["visual_enabled"] else "disabled",
                decisions_before=[decision.model_dump() for decision in decisions],
                source_material_key=result["material_key"],
            )
            if row["config"]["visual_enabled"]:
                initial = json.loads(
                    self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                        "payload"
                    ]
                ) | {"decisions": followup["decisions_before"], "visual_followup": followup}
                self.browsing.repo.complete(
                    identity,
                    result["owner"],
                    result["generation"],
                    "evidence_ready",
                    initial,
                    result["material_key"],
                )
                try:
                    key, material = await self.browsing._prepare_evidence(
                        material["metadata"]["bvid"],
                        row["config"],
                        require_visual=True,
                        visual_source_ids={
                            ref
                            for decision in decisions
                            if decision.coverage_required == "visual"
                            for ref in decision.evidence_ids
                        },
                        task={"id": identity},
                        deadline=row["deadline"],
                    )
                    result.update(material_key=key, material=material)
                    followup.update(status="prepared", material_key=key)
                    self.browsing.repo.complete(
                        identity,
                        result["owner"],
                        result["generation"],
                        "evidence_ready",
                        initial
                        | {
                            "coverage": material["coverage"],
                            "missing_parts": material["missing_parts"],
                            "visual_followup": followup,
                        },
                        key,
                    )
                    decisions = await self._content_decisions(material, row, metrics)
                    followup.update(
                        status="completed",
                        needs_visual_again=any(
                            decision.coverage_required == "visual" for decision in decisions
                        ),
                    )
                except Exception as error:
                    followup.update(status="failed", error=public_error(error))
                    raise
        return material, decisions

    async def _evaluate_task(self, result, row, metrics):
        identity, material = result["id"], result["material"]
        heartbeat = result["heartbeat"]
        decisions, followup = [], {}
        payload = {}
        try:
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
                "decisions": [d.model_dump() for d in decisions],
                "subtitle_tracks": [
                    {k: part.get(k) for k in ("cid", "track_id", "language", "is_ai", "coverage")}
                    for part in material["parts"]
                ],
                "evidence_ids": sorted({ref for decision in decisions for ref in decision.evidence_ids}),
            }
            if followup:
                payload["visual_followup"] = followup
            bvid = material["metadata"]["bvid"]
            aid = str(material["metadata"]["aid"])
            status = "viewed_without_comment"
            source_key = canonical_intent("bilibili", bvid, "top-level", row["character_id"])
            existing = self.browsing.store.one(
                "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (row["account_id"], row["character_id"], source_key),
            )
            context = None
            chosen = {ref for d in actionable for ref in d.evidence_ids} if actionable else set()

            # Pre-fetch comments to verify if account already left a top-level comment on Bilibili
            need_comments = actionable and (
                row["config"]["top_level_enabled"] or row["config"].get("reply_enabled", False)
            )
            if need_comments and not existing:
                context = await self.browsing._read(
                    self.browsing.adapter.fetch,
                    bvid,
                    1,
                )

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
                and self.browsing.repo.round(row["id"])["budget"]["top_levels"]
                < row["config"]["top_level_limit"]
            ):
                self.browsing.repo.reserve_budget(row["id"], top_levels=1)
                if context is None:
                    context = await self.browsing._read(
                        self.browsing.adapter.fetch,
                        bvid,
                        1,
                    )
                response = await self._response(row, material, context, chosen, "top_level", metrics)
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
                        if row["config"].get("discovery_publish"):
                            await self.browsing._send_discovery_intent(intent["id"], row)
                        else:
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

            max_replies = row["config"].get("max_replies_per_video", 2)
            if row["config"].get("reply_enabled", False) and max_replies > 0 and chosen:
                if context is None:
                    context = await self.browsing._read(
                        self.browsing.adapter.fetch,
                        material["metadata"]["bvid"],
                        1,
                    )
                items = context.get("items", []) if isinstance(context, dict) else []
                comments_by_id = {str(it.get("comment_id")): it for it in items if it.get("comment_id")}
                threads_by_root = {}
                for it in items:
                    cid = str(it.get("comment_id"))
                    rid = str(it.get("thread_id") or cid)
                    threads_by_root.setdefault(rid, []).append(it)

                today_midnight = (
                    datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                )
                # Single video reply limit refreshed daily (since midnight today)
                count_row = self.browsing.store.one(
                    "SELECT COUNT(*) AS n FROM browsing_intents WHERE account_id=? AND character_id=? AND kind='reply_public' AND json_extract(target,'$.bvid')=? AND status NOT IN ('rejected','automatic_skipped') AND created_at >= ?",
                    (row["account_id"], row["character_id"], material["metadata"]["bvid"], today_midnight),
                )
                daily_replies_count = count_row["n"] if count_row else 0
                remaining_replies = max(0, max_replies - daily_replies_count)

                replies_created = 0
                for item in items:
                    if replies_created >= remaining_replies:
                        break
                    author_uid = str(item.get("user_id") or item.get("platform_user_id") or "")
                    if not author_uid or author_uid == str(row["account_id"]):
                        continue
                    item_text = str(item.get("text") or item.get("content") or "")
                    if category(item_text) == "spam" or len(item_text.strip()) < 2:
                        continue
                    comment_id = str(item["comment_id"])
                    canonical = canonical_intent(
                        "bilibili", material["metadata"]["bvid"], comment_id, row["character_id"]
                    )
                    if self.browsing.store.one(
                        "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                        (row["account_id"], row["character_id"], canonical),
                    ):
                        continue
                    root_id = str(item.get("thread_id") or comment_id)
                    author_name = str(item.get("username") or item.get("display_name") or "")
                    root_item = comments_by_id.get(root_id, item)
                    root_author_name = str(root_item.get("username") or root_item.get("display_name") or "")
                    root_text = str(root_item.get("text") or root_item.get("content") or "")

                    parent_id = str(item.get("parent_comment_id") or root_id)
                    parent_item = comments_by_id.get(parent_id) if parent_id != root_id else root_item
                    parent_author_name = (
                        str(parent_item.get("username") or parent_item.get("display_name") or "")
                        if parent_item
                        else ""
                    )
                    parent_text = (
                        str(parent_item.get("text") or parent_item.get("content") or "")
                        if parent_item
                        else ""
                    )

                    timeline_items = threads_by_root.get(root_id, [item])
                    thread_context = {
                        "source": {
                            "comment_id": comment_id,
                            "author_uid": author_uid,
                            "author_name": author_name,
                            "text": item_text,
                        },
                        "target": {
                            "type": 1,
                            "oid": str(material["metadata"]["aid"]),
                            "bvid": material["metadata"]["bvid"],
                            "root": root_id,
                            "parent": comment_id,
                        },
                        "root": {
                            "comment_id": root_id,
                            "author_name": root_author_name,
                            "text": root_text,
                        },
                        "parent": {
                            "comment_id": parent_id,
                            "author_name": parent_author_name,
                            "text": parent_text,
                        }
                        if comment_id != root_id
                        else None,
                        "timeline": [
                            {
                                "author_name": str(c.get("username") or c.get("display_name") or "观众"),
                                "text": str(c.get("text") or c.get("content") or ""),
                                "is_root": str(c.get("comment_id")) == root_id,
                                "is_target": str(c.get("comment_id")) == comment_id,
                            }
                            for c in timeline_items[:8]
                        ],
                        "items": timeline_items,
                    }
                    thread_response = await self._response(
                        row, material, thread_context, chosen, "reply_public", metrics
                    )
                    if thread_response.action == "reply_public" and thread_response.text:
                        target = {
                            "type": 1,
                            "oid": str(material["metadata"]["aid"]),
                            "bvid": material["metadata"]["bvid"],
                            "root": root_id,
                            "parent": comment_id,
                            "recipient": author_uid,
                            "recipient_name": author_name,
                            "source_text": item_text,
                        }
                        thread_intent = self.browsing.publication._intent(
                            row,
                            "reply_public",
                            target,
                            thread_response,
                            [result["material_key"]],
                            thread_response.evidence_ids,
                        )
                        if "reply_intent_ids" not in payload:
                            payload["reply_intent_ids"] = []
                        payload["reply_intent_ids"].append(thread_intent["id"])
                        status = "selected"
                        replies_created += 1
                        if thread_intent["auto_authorized"]:
                            if row["config"].get("discovery_publish"):
                                await self.browsing._send_discovery_intent(thread_intent["id"], row)
                            else:
                                await self.browsing.publish(thread_intent["id"], automatic=True)

            self.browsing.repo.complete(
                identity, result["owner"], result["generation"], status, payload, result["material_key"]
            )
            metrics["viewed"] += 1
        except Exception as error:
            metrics["failures"] += 1
            continuation = getattr(error, "continuation", None)
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
                "checkpoint" if continuation or isinstance(error, ContextWindowExceeded) else "failed",
                json.loads(
                    self.browsing.store.one("SELECT payload FROM browsing_tasks WHERE id=?", (identity,))[
                        "payload"
                    ]
                )
                | payload
                | {
                    "reason": "生成进度已保存，可继续" if continuation else public_error(error),
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

    async def _response(self, row, material, context, allowed, kind, metrics):
        if material and kind == "top_level" and material["coverage"] == "sampled_visual":
            segments = self._video_response_segments(material, allowed)
        else:
            segments = (
                [s for s in self.browsing._segments(material) if s["id"] in allowed] if material else []
            )
        recent = self.browsing.store.all(
            "SELECT text FROM browsing_intents WHERE status='published' AND character_id=? ORDER BY updated_at DESC LIMIT ?",
            (row["character_id"], row["config"]["recent_sample_limit"]),
        )
        data = {
            "kind": kind,
            "evidence": segments,
            "thread": context,
            "recent_public_responses": recent,
            "coverage": material["coverage"] if material else "public_text",
        }
        if not material:
            data["allowed_evidence_ids"] = list(allowed)
        if context.get("source") and context.get("target"):
            target = context["target"]
            data["source_identity"] = {
                "resource": target.get("bvid") or "dynamic:" + str(target["oid"]),
                "comment_id": str(context["source"]["comment_id"]),
            }
        return await self._model(
            row,
            ResponseDecision,
            data,
            RESPONSE_INSTRUCTION,
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
        samples = [r["text"] for r in recent] + [
            r.get("content", r.get("text", "")) for r in context.get("items", [])
        ]
        for sample in samples:
            other = normalized(sample)
            if other and (
                value == other
                or len(value) >= 12
                and SequenceMatcher(None, value, other).ratio() > 0.85
                or len(value) >= 12
                and value[:12] == other[:12]
            ):
                return "正文与近期公开发言／热评重复"
        return None

    async def _engagement_decision(self, row, material, actionable, metrics):
        from .models import VideoEngagementDecision

        metadata = material.get("metadata", {})
        title = metadata.get("title", "")
        desc = metadata.get("desc", "")
        owner = metadata.get("owner", {}).get("name", "")
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
