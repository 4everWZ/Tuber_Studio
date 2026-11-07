"""Bounded browsing decisions and the optional video-evidence provider contract."""

from copy import deepcopy
from typing import Literal, Protocol

from pydantic import Field, PrivateAttr, field_validator, model_validator

from ...models import StrictModel
from ...operation_text import validate_operation_text


def browsing_filter_reason(item, config, *, complete=False):
    """Apply cheap constraints to known fields; full metadata must fill restrictions."""
    text = " ".join(str(item.get(key) or "") for key in ("title", "description"))
    text += " " + " ".join(str(tag) for tag in item.get("tags") or [])
    if any(word.casefold() in text.casefold() for word in config["exclude_words"]):
        return "排除词命中"
    owner = str(item.get("owner_uid") or "")
    if owner and (
        owner in config["excluded_author_uids"]
        or config["allowed_author_uids"]
        and owner not in config["allowed_author_uids"]
    ):
        return "作者范围排除"
    if complete and config["allowed_author_uids"] and not owner:
        return "作者信息缺失，无法核验范围"
    category = item.get("category", item.get("category_id"))
    if config["allowed_categories"]:
        if category is not None and str(category) not in config["allowed_categories"]:
            return "分区范围排除"
        if complete and category is None:
            return "分区信息缺失，无法核验范围"
    duration = item.get("duration")
    if (
        duration is not None
        and not config["min_duration_seconds"] <= duration <= config["max_duration_seconds"]
    ):
        return "视频时长范围排除"
    if complete and duration is None:
        return "视频时长缺失，无法核验范围"
    timestamp = item.get("published_at", item.get("pubdate"))
    if timestamp is not None and (
        config["published_after"]
        and timestamp < config["published_after"]
        or config["published_before"]
        and timestamp > config["published_before"]
    ):
        return "发布时间范围排除"
    if complete and timestamp is None and (config["published_after"] or config["published_before"]):
        return "发布时间缺失，无法核验范围"
    return None


MAX_BATCH_CANDIDATES = 20


class BrowsingPolicy(StrictModel):
    sources: list[Literal["recommend", "search", "popular", "specified"]] = Field(
        default_factory=lambda: ["recommend"], max_length=4
    )
    keywords: list[str] = Field(default_factory=list, max_length=20)
    watch_videos: list[str] = Field(default_factory=list, max_length=20)
    autonomous_topics: bool = False
    discovery_enabled: bool = False
    top_level_enabled: bool = False
    top_level_auto_publish: bool = False
    reply_enabled: bool = False
    reply_auto_publish: bool = False
    video_like_enabled: bool = False
    video_like_auto_publish: bool = False
    video_fav_enabled: bool = False
    video_fav_auto_publish: bool = False
    max_replies_per_video: int = Field(default=2, ge=0, le=20)
    daily_watch_limit: int = Field(default=0, ge=0)
    business_timezone: str = "Asia/Taipei"
    auto_downgrade_on_error: bool = True
    mentions_enabled: bool = False
    mention_reply_enabled: bool = False
    replies_enabled: bool = False
    comment_handoff_enabled: bool = False
    visual_enabled: bool = False
    workers: int = Field(default=4, ge=1, le=8)
    candidate_limit: int = Field(default=MAX_BATCH_CANDIDATES, ge=1, le=MAX_BATCH_CANDIDATES)
    inspect_limit: int = Field(default=4, ge=1, le=20)
    max_search_terms: int = Field(default=2, ge=1, le=20)
    max_pages: int = Field(default=2, ge=1, le=2)
    max_reads: int = Field(default=64, ge=0, le=128)
    max_model_calls: int = Field(default=16, ge=0, le=1024)
    max_input_tokens: int = Field(default=262144, ge=0, le=1048576)
    max_output_tokens: int = Field(default=32768, ge=0, le=65536)
    decision_max_tokens: int = Field(default=8192, ge=512, le=65536)
    deadline_seconds: int = Field(default=1800, ge=10, le=1800)
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    notification_interval_seconds: int = Field(default=60, ge=60, le=86400)
    read_retries: int = Field(default=1, ge=0, le=1)
    max_takeovers: int = Field(default=1, ge=0, le=1)
    max_per_hour: int = Field(default=5, ge=0, le=30)
    max_per_day: int = Field(default=20, ge=0, le=200)
    recipient_cooldown_seconds: int = Field(default=120, ge=0, le=86400)
    top_level_limit: int = Field(default=1, ge=0, le=1)
    recent_sample_limit: int = Field(default=12, ge=0, le=30)
    exclude_words: list[str] = Field(default_factory=list, max_length=50)
    allowed_author_uids: list[str] = Field(default_factory=list, max_length=50)
    excluded_author_uids: list[str] = Field(default_factory=list, max_length=50)
    allowed_categories: list[str] = Field(default_factory=list, max_length=50)
    min_duration_seconds: int = Field(default=0, ge=0)
    max_duration_seconds: int = Field(default=3600, ge=1, le=86400)
    published_after: int = Field(default=0, ge=0)
    published_before: int = Field(default=0, ge=0)
    languages: list[str] = Field(
        default_factory=lambda: ["zh-CN", "zh-Hans", "zh"], min_length=1, max_length=8
    )
    report_enabled: bool = True

    @field_validator("business_timezone")
    @classmethod
    def known_business_timezone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("业务时区须为有效的 IANA 时区名称") from None
        return value

    @model_validator(mode="before")
    @classmethod
    def retire_public_length_preference(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "public_max_chars"}
        return value

    @model_validator(mode="after")
    def bounds(self):
        for values in (
            self.keywords,
            self.watch_videos,
            self.exclude_words,
            self.languages,
            self.allowed_author_uids,
            self.excluded_author_uids,
            self.allowed_categories,
        ):
            if any(not v.strip() or len(v) > 100 for v in values):
                raise ValueError("浏览列表项须为1到100字")
        if self.min_duration_seconds > self.max_duration_seconds:
            raise ValueError("最短时长不能大于最长时长")
        return self


class BrowsePlan(StrictModel):
    action: Literal["recommend", "search", "popular", "wait"]
    keywords: list[str] = Field(default_factory=list, max_length=20)
    reason: Literal["character_interest", "user_keywords", "explore", "wait"]
    delay_seconds: int = Field(default=300, ge=60, le=86400)


class CandidateAction(StrictModel):
    action: Literal["watch", "comments", "skip", "later"]
    explanation: str = Field(min_length=1)

    @field_validator("explanation")
    @classmethod
    def concrete_explanation(cls, value):
        if not value.strip():
            raise ValueError("请说明这条候选行动的具体原因")
        return value.strip()


class BatchCandidateChoice(CandidateAction):
    bvid: str


class PrioritizedCandidateChoice(CandidateAction):
    priority: int = Field(strict=True)


class KeyedCandidateBatchPlan(StrictModel):
    workers: int = Field(ge=1, le=8)
    explanation: str = ""
    choices: dict[str, PrioritizedCandidateChoice] = Field(max_length=MAX_BATCH_CANDIDATES)


class CandidateBatchPlan(StrictModel):
    videos: list[str] = Field(default_factory=list, max_length=MAX_BATCH_CANDIDATES)
    workers: int = Field(ge=1, le=8)
    explanation: str = ""
    choices: list[BatchCandidateChoice] = Field(default_factory=list, max_length=MAX_BATCH_CANDIDATES)

    @field_validator("videos")
    @classmethod
    def distinct_videos(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("批次中同一个视频只选择一次")
        return value


def candidate_batch_schema(candidates, max_workers):
    schema = KeyedCandidateBatchPlan.model_json_schema()
    schema["title"] = "CandidateBatchPlan"
    choice_schema = schema.pop("$defs")["PrioritizedCandidateChoice"]
    properties = {}
    for candidate in candidates:
        choice = deepcopy(choice_schema)
        choice["properties"]["action"]["enum"] = candidate["available_actions"]
        properties[candidate["bvid"]] = choice
    schema["properties"]["choices"] = {
        "type": "object", "properties": properties, "required": list(properties),
        "additionalProperties": False,
    }
    schema["properties"]["workers"]["maximum"] = max_workers
    return schema


def candidate_batch_plan(value, candidates):
    """Resolve model priorities into the existing canonical ordered plan."""
    value = value.model_dump() if hasattr(value, "model_dump") else value
    allowed = {item["bvid"] for item in candidates}
    if isinstance(value, dict) and "videos" in value and isinstance(value.get("choices"), list):
        # Completed continuation outputs use the public canonical representation.
        plan = CandidateBatchPlan.model_validate(value)
        ids = [choice.bvid for choice in plan.choices]
        if len(ids) != len(set(ids)) or set(ids) != allowed:
            raise ValueError("保存的批次计划须保留每个候选且恰好一次")
        actions = {item["bvid"]: item["available_actions"] for item in candidates}
        if any(choice.action not in actions[choice.bvid] for choice in plan.choices):
            raise ValueError("保存的批次计划包含本轮未开放的行动")
        if plan.videos != [choice.bvid for choice in plan.choices if choice.action in {"watch", "comments"}]:
            raise ValueError("保存的批次执行索引与逐项行动不一致")
        return plan
    if not isinstance(value, dict) or not isinstance(value.get("choices"), dict):
        raise ValueError("choices须按候选BV号作为唯一键，每项给出action、explanation和整数priority")
    choices = value["choices"]
    missing = [item["bvid"] for item in candidates if item["bvid"] not in choices]
    unknown = [bvid for bvid in choices if bvid not in allowed]
    if missing or unknown:
        raise ValueError(f"每个候选须恰好一个去向；missing_bvids={missing}；unknown_bvids={unknown}")
    # videos is the program's derived index. Older providers may still echo it.
    wire = KeyedCandidateBatchPlan.model_validate({key: item for key, item in value.items() if key != "videos"})
    for candidate in candidates:
        if wire.choices[candidate["bvid"]].action not in candidate["available_actions"]:
            raise ValueError("候选行动未开放：" + candidate["bvid"])
    ordered = sorted(candidates, key=lambda item: -wire.choices[item["bvid"]].priority)
    canonical = [
        BatchCandidateChoice(bvid=item["bvid"], **wire.choices[item["bvid"]].model_dump(exclude={"priority"}))
        for item in ordered
    ]
    return CandidateBatchPlan(
        workers=wire.workers, explanation=wire.explanation, choices=canonical,
        videos=[item.bvid for item in canonical if item.action in {"watch", "comments"}],
    )


CONTENT_DECISION_INTENT_GLOSS = """
explanation结合本条标题、材料或评论说明这次行动的具体理由。
这一步决定是否准备回复，后续再选择公开评论或私信。reason说明本次参与交流的意图：
specific_detail：有可回应内容，共情、吐槽、轻互动、分享感受也算。
no_interest：此刻对这个话题没有参与兴趣。
nothing_to_add：已经读懂材料，但暂时没有想补充的话。
insufficient_evidence：想回应的具体内容还缺相关材料。
visual_required：想回应的具体内容需要补看相关画面。
unsafe_interaction：这次互动本身涉及骚扰、伤害他人或公开他人私密信息，选择略过。
各字段表达同一意图：准备回复时选interest=like、worth_commenting=true、reason=specific_detail；当前材料够用选coverage_required=provided。还想补读材料时选interest=need_more、worth_commenting=false，并用reason和coverage_required说明所需材料。没有想发言时worth_commenting=false。evidence_ids对应这次选择相关的内容块。"""


class ContentDecision(StrictModel):
    explanation: str = ""
    interest: Literal["like", "not_interested", "need_more", "skip"]
    worth_commenting: bool
    reason: Literal[
        "specific_detail",
        "no_interest",
        "nothing_to_add",
        "insufficient_evidence",
        "visual_required",
        "unsafe_interaction",
    ]
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    coverage_required: Literal["provided", "unread_part", "visual"] = "provided"

    @model_validator(mode="after")
    def consistent_prepared_intent(self):
        if (
            self.interest == "like"
            and self.worth_commenting
            and self.coverage_required == "provided"
            and self.reason != "specific_detail"
        ):
            raise ValueError(
                "字段组合不一致：interest=like、worth_commenting=true、"
                f"coverage_required=provided 表示准备回复，reason={self.reason} 与此不一致。"
                "请重新选择一致意图：有可回应内容时 reason=specific_detail；"
                "也可按实际意图调整兴趣、发言或补读字段。"
            )
        return self


class ResponseDecision(StrictModel):
    _generation_id: str | None = PrivateAttr(default=None)
    _generation_identity: dict | None = PrivateAttr(default=None)
    explanation: str = ""
    contact_purpose: str = ""
    contact_trigger: Literal["invitation", "agreement"] | None = None
    video_preference: Literal["like", "not_interested", "neutral"] | None = None
    preference_reason: str = ""
    action: Literal[
        "reply_public",
        "handoff_private",
        "skip",
        "wait",
        "read_comments",
        "watch_frames",
        "read_more",
        "finish",
    ]
    text: str = ""
    reason: Literal[
        "specific_detail",
        "light_interaction",
        "private_details",
        "user_requested",
        "long_followup",
        "no_interest",
        "insufficient_evidence",
        "cooldown",
        "spam",
    ]
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("text")
    @classmethod
    def bounded_body(cls, value):
        return validate_operation_text(value) if value else value


# Internal explanations are short task metadata; prose keeps its own configured
# request output budget. Bound open JSON strings before decoding can loop in one.
MAX_DECISION_REASON_CHARACTERS = 1024


class VideoEngagementDecision(StrictModel):
    should_like: bool = False
    like_reason: str = ""
    should_favorite: bool = False
    favorite_reason: str = ""


def content_decision_schema(evidence_ids, *, allow_visual=True):
    """Require source selection for a comment, with no requirement to comment."""
    properties = ContentDecision.model_json_schema()["properties"]
    properties["explanation"] = {"type": "string", "minLength": 1}
    if not allow_visual:
        for name, unavailable in (("coverage_required", "visual"), ("reason", "visual_required")):
            properties[name]["enum"].remove(unavailable)
    branches = []
    for worth in (False, True):
        if worth and not evidence_ids:
            continue
        fields = properties | {
            "worth_commenting": {"type": "boolean", "const": worth},
            "evidence_ids": {
                "type": "array",
                "minItems": 1 if worth else 0,
                "maxItems": 8 if evidence_ids else 0,
                "items": {"type": "string", "enum": sorted(evidence_ids)}
                if evidence_ids
                else {"type": "string"},
            },
        }
        alternatives = [{}]
        if worth:
            # These disjoint branches express exactly the existing model
            # validator. Native decoding must not offer a combination that
            # the same product immediately rejects after generation.
            alternatives = [
                {"interest": {"enum": [item for item in properties["interest"]["enum"] if item != "like"]}},
                {
                    "interest": {"enum": ["like"]},
                    "coverage_required": {
                        "enum": [
                            item for item in properties["coverage_required"]["enum"] if item != "provided"
                        ]
                    },
                },
                {
                    "interest": {"enum": ["like"]},
                    "coverage_required": {"enum": ["provided"]},
                    "reason": {"enum": ["specific_detail"]},
                },
            ]
        for constraints in alternatives:
            constrained = fields | {name: fields[name] | rule for name, rule in constraints.items()}
            branches.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": constrained,
                    "required": list(constrained),
                }
            )
    return {"title": "ContentDecision", "type": "object", "oneOf": branches}


RESPONSE_INSTRUCTION = (
    "explanation结合本条标题、材料或评论说明这次行动的具体理由。"
    "以当前角色接着这段交流，选择行动、理由、相关内容ID和台词。"
    "台词必须紧密贴合视频或评论的具体内容与看点，像活生生的ACG同好网友一样自然表达，"
    "用自己的话接当前话题，口癖随着交流自然使用。"
    "每一条回应围绕当前被回复的内容与楼内话题，语言生活化自然，自称或常规词汇（如本鱼、小鱼、白饭、摸鱼）直接使用。"
    "普通公开话题在楼里回复（reply_public）；对方明确邀请我方私聊，"
    "或有真实约定且已完成时，与当前作者私聊（handoff_private）。"
    "主动私聊须有指向我方账号的真实邀请或约定，contact_purpose说明具体交流事项；"
    "contact_trigger用invitation表示现在邀请私聊，agreement表示办完约定事项后联系。"
    "有兴趣、长篇跟进、历史聊过或@别人不构成主动私聊依据，普通话题继续在楼里聊。"
    "reply_public的正文接当前公开话题，不把普通交流引向私聊。"
    "根据当前问题、楼内上下文及提供的内容作答；需要更多信息时，可以自然追问。"
    "在楼中楼互动时，结合楼首话题与上下文，你可以自然@其他人或呼应楼内大家的讨论。"
    "想略过这次交流选skip，准备稍后接话选wait。"
    "公开回应理由用specific_detail/light_interaction；真实邀请或完成约定后的私聊理由用user_requested。"
)

RESPONSE_REFERENCE_INSTRUCTION = (
    "所选材料以[ref:1|起止秒]逐段呈现；ref:是内部引用命名空间，仅用于evidence_ids，text写给对方的自然正文。"
    "每个编号对应一条原始引用，最多选择8条；当前公开来源的编号对应thread中的原文。"
)


def response_schema(
    evidence_ids,
    *,
    maximum=None,
    allow_handoff=True,
    video_actions=False,
    allow_public=True,
    allow_comments=True,
):
    """Constrain source IDs and channel consistency before the model emits text."""
    reasons = {
        "reply_public": ["specific_detail", "light_interaction"],
        "handoff_private": ["user_requested"],
        "skip": ["no_interest", "insufficient_evidence", "spam"],
        "wait": ["cooldown", "insufficient_evidence"],
    }
    if video_actions:
        reasons.update(
            {
                "read_comments": ["specific_detail", "light_interaction"],
                "watch_frames": ["insufficient_evidence"],
                "read_more": ["insufficient_evidence"],
                "finish": ["specific_detail", "light_interaction", "no_interest"],
            }
        )
    branches = []
    for action, allowed_reasons in reasons.items():
        if action == "reply_public" and not allow_public or action == "read_comments" and not allow_comments:
            continue
        sending = action in {"reply_public", "handoff_private"}
        if sending and not evidence_ids or action == "handoff_private" and not allow_handoff:
            continue
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **(
                        {
                            "video_preference": {
                                "type": "string",
                                "enum": ["like", "not_interested", "neutral"],
                            },
                            "preference_reason": {"type": "string", "minLength": 1, "maxLength": MAX_DECISION_REASON_CHARACTERS},
                        }
                        if video_actions
                        else {}
                    ),
                    "reason": {"type": "string", "enum": allowed_reasons},
                    "explanation": {"type": "string", "minLength": 1, "maxLength": MAX_DECISION_REASON_CHARACTERS},
                    "action": {"type": "string", "enum": [action]},
                    **({"contact_purpose": {"type": "string", "minLength": 1, "maxLength": MAX_DECISION_REASON_CHARACTERS},
                        "contact_trigger": {"type": "string", "enum": ["invitation", "agreement"]}}
                       if action == "handoff_private" else {}),
                    "evidence_ids": {
                        "type": "array",
                        "minItems": 1 if sending else 0,
                        "maxItems": 8,
                        "items": {"$ref": "#/$defs/evidence_id"}
                        if evidence_ids
                        else {"type": "string"},
                    },
                    "text": {
                        "type": "string",
                        "minLength": 1 if sending else 0,
                        **({"maxLength": maximum} if sending and maximum is not None else {}),
                        **({"maxLength": 0} if not sending else {}),
                    },
                },
                "required": ["reason", "explanation", "action", "evidence_ids", "text"]
                + (["contact_purpose", "contact_trigger"] if action == "handoff_private" else [])
                + (["video_preference", "preference_reason"] if video_actions else []),
            }
        )
    schema = {"title": "ResponseDecision", "type": "object", "oneOf": branches}
    if evidence_ids:
        # Every branch uses the same invocation-local source set. Share it so
        # a long real transcript does not duplicate hundreds of IDs per action.
        schema["$defs"] = {"evidence_id": {"type": "string", "enum": sorted(evidence_ids)}}
    return schema


class VideoEvidenceProvider(Protocol):
    def capabilities(self) -> dict: ...
    async def analyze_video_evidence(
        self, task: dict, metadata: dict, frames: list[dict], question: str, *, deadline: float
    ) -> dict: ...
    async def cancel(self, task_id: str) -> None: ...
    async def close(self) -> None: ...


class DisabledVideoEvidence:
    def capabilities(self):
        return {
            "provider_id": "disabled",
            "version": "1",
            "available": False,
            "images": False,
            "multiple_images": False,
            "max_frames": 0,
            "max_pixels": 0,
            "max_bytes": 0,
            "context_tokens": None,
            "admitted": False,
            "reason": "未配置视频视觉后端；首期只使用CC字幕",
        }

    async def analyze_video_evidence(self, *args, **kwargs):
        raise ValueError("视频视觉后端not_configured")

    async def cancel(self, task_id):
        pass

    async def close(self):
        pass
