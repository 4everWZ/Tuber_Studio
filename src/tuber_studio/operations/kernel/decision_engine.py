"""Shared evidence-bound response and content decision engine."""

import json
import re
import time
import unicodedata
from difflib import SequenceMatcher

from ...core.errors import ContextWindowExceeded
from ...core.identity import BILIBILI_OWNER_INSTRUCTION, bilibili_owner_context, captured_identity
from ...core.meter import GenerationMeter, budgeted_json
from ...core.models import content_hash
from ...core.store import dump
from ...core.text import operation_character, operation_output_diagnostics
from ...persona.characters.front_persona import expressive_persona
from ...persona.context.message_builder import SYSTEM_RULES
from .generation import generate_with_recovery
from .inputs import (
    CLASSIFICATION_INPUT_VERSION,
    CLASSIFICATION_INSTRUCTION,
    RESPONSE_ACTOR_INSTRUCTION,
    ResponseDecisionInput,
    bind_response_actors,
    classification_view,
    compact_content_input,
    format_content_input,
)
from .models import (
    CONTENT_DECISION_INTENT_GLOSS,
    RESPONSE_INSTRUCTION,
    RESPONSE_REFERENCE_INSTRUCTION,
    CandidateBatchPlan,
    ContentDecision,
    ResponseDecision,
    VideoEngagementDecision,
    candidate_batch_plan,
    candidate_batch_schema,
    content_decision_schema,
    response_schema,
)


def normalized(text):
    """Normalize NFKC text down to lowercase alphanumeric characters for repetition checks."""
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


class CommentProgress:
    """Durable per-comment response decision cache keyed by canonical source hash."""

    def __init__(self, store):
        self.store = store
        store.db.execute("""CREATE TABLE IF NOT EXISTS browsing_comment_progress(
            account_id TEXT,character_id TEXT,bvid TEXT,comment_id TEXT,source_hash TEXT,
            decision TEXT,updated_at REAL,
            PRIMARY KEY(account_id,character_id,bvid,comment_id))""")

    @staticmethod
    def identity(row, bvid, source):
        return (row["account_id"], row["character_id"], bvid, str(source["comment_id"]))

    def get(self, row, bvid, source, *, legacy_source=None):
        value = self.store.one(
            "SELECT decision,source_hash FROM browsing_comment_progress "
            "WHERE account_id=? AND character_id=? AND bvid=? AND comment_id=?",
            self.identity(row, bvid, source),
        )
        if value and (
            value["source_hash"] == content_hash(source)
            or legacy_source is not None and value["source_hash"] == content_hash(legacy_source)
        ):
            saved = json.loads(value["decision"])
            generation = saved.pop("generation", {})
            decision = ResponseDecision.model_validate(saved)
            if decision.action in {"reply_public", "handoff_private"} and generation.get("source_id") != str(
                source["comment_id"]
            ):
                return None
            if (
                decision.action == "reply_public"
                and generation.get("input_format") != ResponseDecisionInput.COMMENT_FORMAT_VERSION
            ):
                return None
            decision._generation_id = generation.get("id")
            decision._generation_identity = generation.get("identity")
            decision._memory_audit = generation.get("memory_audit")
            decision._comment_materials = generation.get("comment_materials", [])
            return decision if decision.action != "wait" else None
        return None

    def save(self, row, bvid, source, decision):
        self.store.execute(
            "INSERT OR REPLACE INTO browsing_comment_progress VALUES(?,?,?,?,?,?,?)",
            (
                *self.identity(row, bvid, source),
                content_hash(source),
                dump(
                    decision.model_dump()
                    | {
                        "generation": {
                            "id": decision._generation_id,
                            "identity": decision._generation_identity,
                            "source_id": str(source["comment_id"]),
                            "input_format": ResponseDecisionInput.COMMENT_FORMAT_VERSION,
                            **({"memory_audit": decision._memory_audit} if decision._memory_audit else {}),
                            **(
                                {"comment_materials": decision._comment_materials}
                                if decision._comment_materials
                                else {}
                            ),
                        }
                    }
                ),
                time.time(),
            ),
        )


class ResponseEngine:
    """Shared structured-output engine for comment/video response decisions across pipelines."""

    def __init__(self, browsing, *, channels=None):
        self.browsing = browsing
        self.channels = channels if channels is not None else getattr(browsing, "decision_channels", None) or {}
        self.comment_progress = CommentProgress(browsing.store)

    def _channel(self, name):
        fn = self.channels.get(name)
        if fn is None:
            raise RuntimeError(f"ResponseEngine missing channel collaborator: {name}")
        return fn

    def _response_actor_data(self, row, data):
        character = self.browsing.store.character(row["character_id"], row["character_version"])
        actor = self.browsing.runtime.identity_snapshot(character, row.get("account_id"))
        thread = data.get("thread") or {}
        if data.get("kind") == "reply_public" and (thread.get("target") or {}).get("bvid"):
            data = data | {"thread": {key: value for key, value in thread.items() if key != "public_text"}}
        bound = bind_response_actors(data, actor)
        thread = bound.get("thread") or {}
        target, source = thread.get("target") or {}, thread.get("source") or {}
        bound.update(bilibili_owner_context(character, "bilibili", source.get("author_uid")))
        video = bound.get("video_source") or {}
        if target.get("bvid") and target["bvid"] == video.get("bvid") and video.get("owner_uid"):
            thread["resource_author_uid"] = str(video["owner_uid"])
        root_id = (thread.get("addressing") or {}).get("source_root_comment_id") or target.get("root")
        memory = self.browsing.runtime.daily_memory
        scope = (str(row.get("account_id") or ""), character["id"], character["version"])
        preference = memory.preference(scope)
        if not preference["enabled"]:
            return bound
        if target.get("bvid") and (root_id or source.get("comment_id")):
            bound["own_public_experiences"] = memory.context(
                character, video_id=target["bvid"], thread_id=root_id, comment_id=source.get("comment_id")
            )
        memories = []
        if root_id and row.get("account_id"):
            resource_key = self._channel("resource_key")
            memories = memory.recall_organized(
                scope,
                "comment",
                source.get("text") or "",
                resource=resource_key(target),
                thread=root_id,
                participant=source.get("author_uid"),
            )
            if memories:
                bound.setdefault("own_public_experiences", []).extend(
                    {
                        "id": f"organized:{item['id']}",
                        "text": item["anecdote"],
                        "public_sources": item["source"]["references"],
                    }
                    for item in memories
                )
        if bound.get("own_public_experiences"):
            bound["_memory_audit"] = {
                "memory_scope": scope,
                "memory_channel": "comment",
                "memory_preference_revision": preference["revision"],
                "organized_memory": [memory.organized_reference(item) for item in memories],
            }
        return bound

    def invited_private_response(self, row, thread):
        if not row["config"].get("comment_handoff_enabled"):
            return None
        policy = self.browsing.messages.policy()
        if not (policy["enabled"] and policy["allow_comment_handoff"]):
            return None
        source = thread.get("source") or {}
        recipient = (thread.get("target") or {}).get("recipient") or source.get("author_uid")
        confirmed_invitation = self._channel("confirmed_invitation")
        invitation = confirmed_invitation(thread, row.get("account_id"), recipient)
        if invitation is None:
            return None
        return ResponseDecision(
            action="handoff_private",
            reason="user_requested",
            explanation="原评论请求私聊，" + invitation["quote"],
            contact_purpose=invitation["quote"],
            contact_trigger=invitation["kind"],
            evidence_ids=["public:" + str(source["comment_id"])],
        )

    def _model_input(self, row, schema, data, instruction, *, response_input=None):
        character = operation_character(
            self.browsing.store.persona_defaults(
                self.browsing.store.character(row["character_id"], row["character_version"])
            )
        )
        view = (
            classification_view(self.browsing.settings.root, character)
            if schema is ContentDecision
            else None
        )
        if schema is ResponseDecision:
            if response_input is None:
                data = self._response_actor_data(row, data)
                data.pop("_memory_audit", None)
            response_input = response_input or ResponseDecisionInput(data)
            if data.get("kind") == "top_level" and data.get("coverage") == "sampled_visual":
                instruction += (
                    "按材料类型和时码区分字幕与画面。台词如果提到两者，就分别引用能支持它们的编号。"
                    "只看到了片段，可以就那个细节接话。"
                )
            mention_instruction = self.channels.get("mention_instruction", "")
            instruction += RESPONSE_ACTOR_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION + mention_instruction
            if response_input.comment_reply:
                instruction += (
                    " 先接当前评论实际问的事，回复引用当前评论。"
                    "视频投稿者、画面里的人和来评者各按所给身份；信息不足可以问清楚或略过。"
                    "自己的作品、经历或收藏以记录为准，平台会显示回复对象，text直接写要说的话。"
                )
                if response_input.comment_video_available:
                    instruction += (
                        " 只有当前原话指向视频细节且需要核查时，选read_more，"
                        "在source_quote照抄需要核查的原句或短语。原话用本视频指代某个项目时，"
                        "这一步可先查项目和作者提供的链接；已有信息够用就直接回应。"
                    )
                if data.get("video_lookup_quote"):
                    instruction += " 补到的视频材料仅用于核查video_lookup_quote所指的细节，引用实际支持的字幕或画面。"
            if data.get("bilibili_owner"):
                instruction += "\n" + BILIBILI_OWNER_INSTRUCTION
        content = (
            response_input.format()
            if response_input is not None
            else format_content_input(data)
            if schema is ContentDecision
            else dump(data)
        )
        role = SYSTEM_RULES + dump(expressive_persona(character))
        if view is not None:
            content = compact_content_input(data)
            role = "角色：" + view["brief"]
            instruction = CLASSIFICATION_INSTRUCTION
        allow_visual = data.get("allow_visual", self.browsing.evidence.has_visual_provider())
        if schema is ContentDecision:
            instruction += (
                "需要补看时选visual。visual_targets引用字幕编号并选before（之前）、during（当时）、"
                "after（之后）或within（段内，fraction填0到1）；其它位置fraction为null。"
                "需要看瞬间动作或读屏幕小字时设precision=stream，其余用storyboard。"
                if allow_visual
                else "本次取样画面已到齐。请用现有材料判断，材料仍不足时可以暂不发言。"
            )
        messages = [
            {"role": "system", "content": role},
            {"role": "system", "content": instruction},
            {"role": "user", "content": "公开来源材料：" + content},
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
                dynamic_ref=response_input.dynamic_ref,
                allow_comments=data.get("allow_comments", True),
                allow_visual=allow_visual,
                grouped_comment_refs=response_input.grouped_comment_refs,
                comment_video_available=response_input.comment_video_available,
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
        web_search_links=(),
    ):
        self.browsing._check_round(row)
        if schema is ResponseDecision:
            data = self._response_actor_data(row, data)
        memory_audit = data.pop("_memory_audit", None)
        response_input = ResponseDecisionInput(data) if schema is ResponseDecision else None
        video_response = schema is ResponseDecision and data.get("kind") == "top_level"
        character, messages, model_schema = self._model_input(
            row, schema, data, instruction, response_input=response_input
        )
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
        target = thread.get("target") or {}

        def allow_web_url(url):
            host = url.host.rstrip(".")
            if host != "bilibili.com" and not host.endswith(".bilibili.com"):
                return True
            bvid = target["bvid"].casefold()
            aid = str(target.get("oid") or "")
            path = url.path.casefold().split("/")
            return bvid not in path and url.params.get("bvid", "").casefold() != bvid and (
                not aid or f"av{aid}" not in path and url.params.get("aid") != aid
            )

        profile_uid = (thread.get("source") or {}).get("author_uid")
        relations = self.browsing.runtime.relationships
        profile_revision = relations.profile_revision(
            row["account_id"], row["character_id"], "bilibili", profile_uid
        )

        def guard():
            self.browsing._check_round(row)
            self.browsing.runtime.validate_memory_delivery(memory_audit)
            if profile_revision != relations.profile_revision(
                row["account_id"], row["character_id"], "bilibili", profile_uid
            ):
                raise ValueError("等待模型期间用户档案已更新")
            source = data.get("source_identity")
            if source and self.browsing.publication.has_manual_draft(
                row, source["resource"], source["comment_id"]
            ):
                raise ValueError("等待模型期间同源评论已有候选，合并处理")

        async def invoke_model(packet, wire_schema):
            wait = time.monotonic()
            async with self.browsing.runtime.offline_generation():
                if metrics is not None:
                    metrics["lock_wait_seconds"] += time.monotonic() - wait
                self.browsing.check_step(row)
                guard()
                tokens = None
                with GenerationMeter(self.browsing.repo, row["id"]):
                    if not getattr(self.browsing.runtime.operation_llm, "generation_metered", False):
                        counted = (
                            self.browsing.runtime.operation_llm.prepare_json_messages(
                                packet, wire_schema, max_tokens=output
                            )
                            if hasattr(self.browsing.runtime.operation_llm, "prepare_json_messages")
                            else packet
                        )
                        tokens = await self.browsing.runtime.operation_llm.count_tokens(counted)
                        if tokens + output > self.browsing.settings.data["omni"]["context_tokens"] * 95 // 100:
                            raise ContextWindowExceeded("浏览输入超出真实窗口，未静默截断材料")
                    return await budgeted_json(
                        self.browsing.runtime.operation_llm,
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
                    or any(target.evidence_id not in allowed for target in value.visual_targets)
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
                if value.action == "reply_public" and (not value.evidence_ids or not value.text):
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
                    **({"web_search_links": web_search_links} if web_search_links else {}),
                }
            ),
        }
        if response_input is not None:
            binding["input_format"] = (
                response_input.COMMENT_FORMAT_VERSION
                if response_input.comment_reply
                else response_input.FORMAT_VERSION
            )
        elif schema is CandidateBatchPlan:
            binding["input_format"] = "keyed-candidate-priorities-v4"
        if profile_revision is not None:
            binding["profile_revision"] = profile_revision
        if memory_audit:
            binding["memory_audit"] = memory_audit
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
            diagnose=lambda item: (response_input.diagnose(item) if response_input is not None else None)
            or (diagnose(item) if diagnose else None),
            allow_web_fetch=response_input is not None
            and (not response_input.comment_reply or bool(response_input.source_text)),
            allow_web_url=allow_web_url if response_input and response_input.comment_reply and target.get("bvid") else None,
            web_search_source=response_input.source_text if response_input and response_input.comment_reply else None,
            web_search_links=web_search_links,
        )
        result = response_input.resolve(value) if response_input is not None else value
        if schema is ResponseDecision:
            self.browsing.runtime.validate_memory_delivery(memory_audit)
            if result.action == "reply_public" and result.mentions:
                mention_participants = self._channel("mention_participants")
                bind_mentions = self._channel("bind_mentions")
                participants = mention_participants(data.get("thread") or {}, data.get("video_source"))
                mentions, pending = await bind_mentions(
                    self.browsing.adapter,
                    result.text.strip(),
                    [item.model_dump() for item in result.mentions],
                    participants,
                )
                result._mention_selection = {"mentions": mentions, "pending_mentions": pending}
            result._memory_audit = memory_audit
            result._generation_identity = binding["self_identity"]
            saved_generation = self.browsing.runtime.continuations.find("generation.browsing", identity)
            result._generation_id = saved_generation["id"] if saved_generation else None
        return result

    @staticmethod
    def _video_response_segments(material, allowed):
        numbers = {str(part["cid"]): i for i, part in enumerate(material["metadata"]["parts"], 1)}
        tracks, frames, selected = {}, [], []
        for part in material["parts"]:
            cid = str(part["cid"])
            visual = part.get("visual", False)
            for segment in part["segments"]:
                source = dict(segment, cid=cid, source_label=f"P{numbers[cid]} {'画面' if visual else '字幕'}")
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
        llm = self.browsing.runtime.operation_context.effective_llm_config()
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
            }
        )

    async def _response(self, row, material, context, allowed, kind, metrics, *, video_actions=False):
        context = dict(context)
        context.pop("_scroll_progress", None)
        followup = context.pop("_visual_followup", {})
        comment_materials = context.pop("_comment_materials", [])
        allow_public = context.pop("_allow_public", True)
        allow_comment_like = context.pop("_allow_comment_like", False)
        dynamic_source = context.pop("_dynamic_like_source", None)
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
        comment_reply = kind == "reply_public"
        if comment_reply:
            segments = []
        elif material and material["coverage"] == "sampled_visual":
            segments = self._video_response_segments(material, allowed)
        else:
            segments = [s for s in self.browsing._segments(material) if s["id"] in allowed] if material else []
        recent = self.browsing.store.all(
            "SELECT text FROM browsing_intents WHERE status='published' AND account_id=? AND character_id=? "
            "AND json_extract(target,'$.memory_audit') IS NULL "
            "AND kind IN ('top_level','reply_public') AND updated_at>=? ORDER BY updated_at DESC LIMIT ?",
            (
                row["account_id"],
                row["character_id"],
                self.browsing.store.get("browsing_history_visible_since", 0),
                row["config"]["recent_sample_limit"],
            ),
        )
        model_comment_materials = self._channel("model_comment_materials")
        data = {
            "kind": kind,
            "evidence": segments,
            "thread": {} if kind == "top_level" else context,
            **({"comment_materials": model_comment_materials(comment_materials)} if comment_materials else {}),
            **({"video_source": self._metadata_summary(material["metadata"])} if material and not comment_reply else {}),
            "coverage": material["coverage"] if material and not comment_reply else "public_text",
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
        if dynamic_source:
            reference = "dynamic:" + dynamic_source["dynamic_id"]
            data["dynamic_evidence"] = reference
            data["evidence"].append({"id": reference, "text": dynamic_source["text"], "source_label": "原动态"})
        if video_actions:
            data["available_actions"] = {
                "read_comments": data["allow_comments"],
                "sample_frames": data["allow_visual"],
            }
        if followup.get("status") == "completed":
            data["completed_actions"] = [{"action": "sample_frames", "result": "已补看所引用时码附近的画面"}]
        if not material:
            data["allowed_evidence_ids"] = list(allowed)
        source_links = ()
        if (
            comment_reply
            and material
            and (context.get("source") or {}).get("text", "").strip()
            and (context.get("target") or {}).get("bvid") == material["metadata"].get("bvid")
        ):
            description = material["metadata"].get("description") or material["metadata"].get("desc") or ""
            source_links = tuple(re.findall(r"https://[^\s<>\"'，。！、；）]+", description)[:4])
            data["comment_video_available"] = any(
                segment["id"] in allowed for part in material["parts"] for segment in part["segments"]
            )
        if kind != "top_level" and context.get("source") and context.get("target"):
            target = context["target"]
            data["allowed_evidence_ids"] = sorted(
                set(data.get("allowed_evidence_ids", []))
                | {"public:" + str(context["source"]["comment_id"])}
                | {"public:" + str(item["comment_id"]) for item in context.get("grouped_comments", [])}
            )
            data["source_identity"] = {
                "resource": target.get("bvid") or "dynamic:" + str(target["oid"]),
                "comment_id": str(context["source"]["comment_id"]),
            }
        instruction = (
            RESPONSE_INSTRUCTION
            + (
                " comment_materials仅是当前原评的专栏摘录或图片抽样观察，"
                "用当前原评编号引用，依据实际读到的内容。"
                if comment_materials
                else ""
            )
            + (" 想给当前评论点个赞时选like_comment，引用该评论，正文留空。" if allow_comment_like else "")
            + (" 想给原动态点赞时选like_dynamic，引用原动态，正文留空。" if dynamic_source else "")
            + (
                " grouped_comments是同一用户对同一对象的连续来评。若一并公开回答，"
                "covered_comments列当前回复目标和正文一并回答的其他来评编号。"
                if context.get("grouped_comments")
                else ""
            )
            + (
                "想发主评选reply_public并写正文。翻评论选read_comments，补看画面选watch_frames；"
                "visual_targets引用字幕并选before、during、after或within（段内比例fraction为0到1，其它为null）。"
                "瞬间动作或屏幕小字用precision=stream，其余用storyboard。"
                "补读选read_more，结束观看选finish，略过选skip，稍后处理选wait。"
                "video_preference填like/not_interested/neutral，preference_reason说说视频里具体哪点影响了你的偏好。"
                if video_actions
                else ""
            )
        )

        def diagnose(value):
            if value.action not in {"reply_public", "handoff_private"}:
                return None
            return self.repetition_reason(value.text, recent, context) or (
                "正文包含大量重复内容；结合表达意图选择保留、修订或暂缓"
                if operation_output_diagnostics(value.text)["needs_review"]
                else None
            )

        response = await self._model(
            row, ResponseDecision, data, instruction, metrics, diagnose=diagnose, web_search_links=source_links
        )
        if comment_reply and response.action == "read_more":
            metadata = material["metadata"]
            video_source = self._metadata_summary(metadata)
            if description := metadata.get("description") or metadata.get("desc"):
                video_source["description"] = description[:1000]
            followup_data = data | {
                "comment_video_available": False,
                "video_lookup_quote": response.source_quote.strip(),
                "video_source": video_source,
                "coverage": material["coverage"],
                "evidence": self._selected_video_segments(material, allowed),
            }
            response = await self._model(
                row,
                ResponseDecision,
                followup_data,
                instruction,
                metrics,
                diagnose=diagnose,
                web_search_links=source_links,
            )
        response._comment_materials = comment_materials
        return response

    @staticmethod
    def _selected_video_segments(material, allowed):
        pages = {str(part["cid"]): page for page, part in enumerate(material["metadata"].get("parts", []), 1)}
        selected = []
        for part in material["parts"]:
            cid = str(part["cid"])
            kind = "画面观察" if part.get("visual") else "字幕"
            label = f"P{pages[cid]} {kind}" if cid in pages else kind
            selected.extend(
                dict(segment, cid=cid, source_label=label)
                for segment in part["segments"]
                if segment["id"] in allowed
            )
        return selected

    @staticmethod
    def repetition_reason(text, recent, context):
        value = normalized(text)
        if len(value) < 12:
            return None
        for sample in [r["text"] for r in recent]:
            other = normalized(sample)
            if other and (value == other or SequenceMatcher(None, value, other).ratio() > 0.85):
                return "正文与近期公开发言／热评重复"
        if len(value) >= 24:
            for item in context.get("items", []):
                other = normalized(item.get("content", item.get("text", "")))
                if other and SequenceMatcher(None, value, other).ratio() > 0.94:
                    return "正文与来源评论大段重复"
        return None


DecisionEngine = ResponseEngine
