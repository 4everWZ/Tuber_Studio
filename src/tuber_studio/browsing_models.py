"""Bounded browsing decisions and the optional video-evidence provider contract."""

from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .live_media import StrictModel
from .operation_text import validate_operation_text


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


class BrowsingPolicy(StrictModel):
    sources: list[Literal["recommend", "search", "popular", "specified"]] = Field(
        default_factory=lambda: ["recommend"], max_length=4
    )
    keywords: list[str] = Field(default_factory=list, max_length=2)
    watch_videos: list[str] = Field(default_factory=list, max_length=20)
    autonomous_topics: bool = False
    discovery_enabled: bool = False
    top_level_enabled: bool = False
    reply_enabled: bool = False
    mentions_enabled: bool = False
    mention_reply_enabled: bool = False
    replies_enabled: bool = False
    visual_enabled: bool = False
    workers: int = Field(default=4, ge=1, le=8)
    candidate_limit: int = Field(default=20, ge=1, le=20)
    inspect_limit: int = Field(default=4, ge=1, le=20)
    max_search_terms: int = Field(default=2, ge=1, le=2)
    max_pages: int = Field(default=2, ge=1, le=2)
    max_reads: int = Field(default=64, ge=1, le=128)
    max_model_calls: int = Field(default=16, ge=1, le=32)
    max_input_tokens: int = Field(default=262144, ge=1024, le=1048576)
    max_output_tokens: int = Field(default=16384, ge=2048, le=65536)
    decision_max_tokens: int = Field(default=2048, ge=512, le=65536)
    deadline_seconds: int = Field(default=1800, ge=10, le=1800)
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    notification_interval_seconds: int = Field(default=60, ge=60, le=86400)
    read_retries: int = Field(default=1, ge=0, le=1)
    max_takeovers: int = Field(default=1, ge=0, le=1)
    max_per_hour: int = Field(default=5, ge=1, le=30)
    max_per_day: int = Field(default=20, ge=1, le=200)
    recipient_cooldown_seconds: int = Field(default=120, ge=10, le=86400)
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
    keywords: list[str] = Field(default_factory=list, max_length=2)
    reason: Literal["character_interest", "user_keywords", "explore", "wait"]
    delay_seconds: int = Field(default=300, ge=60, le=86400)


CONTENT_DECISION_INTENT_GLOSS = """
这一步决定是否准备回复，后续再选择公开评论或私信。reason说明本次参与交流的意图：
specific_detail：有可回应内容，共情、吐槽、轻互动、分享感受也算。
no_interest：此刻对这个话题没有参与兴趣。
nothing_to_add：已经读懂材料，但暂时没有想补充的话。
insufficient_evidence：想回应的具体内容还缺相关材料。
visual_required：想回应的具体内容需要补看相关画面。
unsafe_interaction：这次互动本身涉及骚扰、伤害他人或公开他人私密信息，选择略过。
各字段表达同一意图：准备回复时选interest=like、worth_commenting=true、reason=specific_detail；当前材料够用选coverage_required=provided。还想补读材料时选interest=need_more、worth_commenting=false，并用reason和coverage_required说明所需材料。没有想发言时worth_commenting=false。evidence_ids对应这次选择相关的内容块。"""


class ContentDecision(StrictModel):
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
    action: Literal["reply_public", "handoff_private", "skip", "wait"]
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


def content_decision_schema(evidence_ids):
    """Require source selection for a comment, with no requirement to comment."""
    properties = ContentDecision.model_json_schema()["properties"]
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
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": fields,
                "required": list(fields),
            }
        )
    return {"title": "ContentDecision", "type": "object", "oneOf": branches}


RESPONSE_INSTRUCTION = (
    "以当前角色接着这段交流，选择行动、理由、相关内容ID和台词。"
    "普通公开话题在楼里回复（reply_public）；对方希望私下继续、涉及个人细节，"
    "或上下文更适合单独聊时，与当前作者私聊（handoff_private）。"
    "根据当前问题和提供的内容作答；需要更多信息时，可以自然追问。"
    "想略过这次交流选skip，准备稍后接话选wait。"
    "公开回应理由用specific_detail/light_interaction；私聊理由分别为private_details、"
    "user_requested（对方希望私聊）、long_followup（适合单独展开的持续讨论）。"
)

RESPONSE_REFERENCE_INSTRUCTION = (
    "所选材料以[r1|起止秒]这类短编号逐段呈现；evidence_ids选择本次短编号。"
    "每个编号对应一条原始引用，最多选择8条；当前公开来源的编号对应thread中的原文。"
)


def response_schema(evidence_ids, *, maximum=None, allow_handoff=True):
    """Constrain source IDs and channel consistency before the model emits text."""
    reasons = {
        "reply_public": ["specific_detail", "light_interaction"],
        "handoff_private": ["private_details", "user_requested", "long_followup"],
        "skip": ["no_interest", "insufficient_evidence", "spam"],
        "wait": ["cooldown", "insufficient_evidence"],
    }
    branches = []
    for action, allowed_reasons in reasons.items():
        sending = action in {"reply_public", "handoff_private"}
        if sending and not evidence_ids or action == "handoff_private" and not allow_handoff:
            continue
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reason": {"type": "string", "enum": allowed_reasons},
                    "action": {"type": "string", "enum": [action]},
                    "evidence_ids": {
                        "type": "array",
                        "minItems": 1 if sending else 0,
                        "maxItems": 8,
                        "items": {"type": "string", "enum": sorted(evidence_ids)}
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
                "required": ["reason", "action", "evidence_ids", "text"],
            }
        )
    return {"title": "ResponseDecision", "type": "object", "oneOf": branches}


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
