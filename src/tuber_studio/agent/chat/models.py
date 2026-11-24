"""Shared data models, constants and route contracts for operator chat."""

import asyncio
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, PrivateAttr, StringConstraints, model_validator

from ...core.models import StrictModel, content_hash
from ...persona.characters.front_persona import without_address
from ..memory_organizer import recall as recall_memory
from .lookup.operator_task_patch import TaskPatch
from .lookup.personal_agent import CAPABILITIES, Arrangement, ConversationObject
from .lookup.workshop_lookup import LOOKUP_TOOL, WorkshopLookup


class CallOrigin(StrictModel):
    ticket_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_signature: str = Field(pattern=r"^[a-f0-9]{64}$")


def verify_call(origin, key, scope):
    encoded = json.dumps(["studio.mic.v1", origin.ticket_hash, content_hash(scope)], ensure_ascii=False, separators=(",", ":")).encode()
    bound = hmac.digest(key.encode(), encoded, "sha256").hex()
    if not hmac.compare_digest(bound, origin.scope_signature):
        raise HTTPException(409, "通话授权已变化，请重新开始通话")


def chat_model_provider(runtime):
    """Resolve foreground text chat provider (9B /llm/v1 in runtime; test doubles preserved)."""
    fn = getattr(runtime, "foreground_chat_llm", None)
    return fn() if callable(fn) else (getattr(runtime, "chat_llm", None) or runtime.llm)


def text_generation_slot(runtime):
    """Enter strict-priority foreground chat slot, falling back cleanly for minimal test doubles."""
    fn = getattr(runtime, "chat_text_generation", None)
    return (fn or runtime.operator_generation)()

_BV = re.compile(r"(?:https://www\.bilibili\.com/video/)?(BV[0-9A-Za-z]{10})/?")
_MENTIONED_BV = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
_DYNAMIC_URL = re.compile(r"https://(?:www\.bilibili\.com/opus/|t\.bilibili\.com/)([1-9][0-9]{0,19})(?![0-9])")
_DYNAMIC_ID = re.compile(r"(?<![0-9])([1-9][0-9]{14,19})(?![0-9])")
_TIMECODE = re.compile(r"(?<![0-9:])(?:(\d{1,3}):)?(\d{1,4}):([0-5]\d)(?:\.(\d{1,3}))?(?![0-9:])")
_PART = re.compile(r"(?<![A-Za-z0-9])P\s*([1-9]\d{0,3})(?!\d)", re.I)
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_EXPERIENCE_SOURCE = (
    "(category='public_incident' OR source_ref LIKE 'operator-public:%' OR source_ref LIKE 'system:downtime:%')"
)
_EVENT_ACTIONS = {"event_details", "event_author", "event_reply", "remember_event", "full_video"}
_TASK_REFERENCE_ACTIONS = {"live_handoff", "stop", "resume_video", "video_details", "inspect_video_frame"}
_OWNER_UI_ONLY = set()
_ROUTE_READ_ACTIONS = (
    "clock", "activity", "event_details", "event_author", "event_reply",
    "video_activity", "video_details", "live_status", "game_status", "live_history",
    "public_experiences", "recall_experience", "owner_binding", "owner_profile",
    "operating_account", "operation_status", "web_lookup", "task_status",
    "workbench_capabilities", "outgoing_queue", "identity_read",
    LOOKUP_TOOL, "watch_totals", recall_memory.NAME, "memory_summary",
    "daily_settings", "pet_settings",
)
_ROUTE_CHANGE_ACTIONS = (
    "daily_settings", "pet_settings", "remember_event", "forget_memory",
    "save_memory", "update_memory", "delete_memory",
    "live_preference", "live_handoff", "game_handoff", "dynamic", "edit_dynamic",
    "browse", "full_video", "start_operations", "stop", "resume_video", "inspect_video_frame",
    "pause_task", "resume_task", "watch_goal", "start_live",
)
_ORDINAL_REFERENCE = re.compile(r"第\s*([一二三两123])\s*[件条个]")
_ORDINALS = {"一": 1, "二": 2, "两": 2, "三": 3, "1": 1, "2": 2, "3": 3}
_CONTINUED_REFERENCE = re.compile(r"那[条件个]|刚才|后来|然后")
_SINGULAR_DEICTIC = re.compile(r"(?:这|那|前面那|刚才那|上一|最后那)\s*[条件个位份篇支]")

_EVENT_QUERIES = (
    (r"原话|原楼|原评论|[说讲]了什么", "event_details"),
    (r"作者|谁.{0,3}[说讲写发](?:过|出|的)", "event_author"),
    (r"(?:回复|回过|回了|有回).{0,4}[吗沒没谁]|回没回|送达|回执", "event_reply"),
    (r"记成.{0,4}经历|记下.{0,3}(?:这|那)", "remember_event"),
)

_TASK_ROUTING = (
    "不需执行操作即可直接回答时选 chat。需要持续推进、分头处理或稍后交付的新目标选 delegate_task。"
    "对已有交办改口、补充要求、暂停或停止选 steer_task，从候选任务卡选对应 task_id；其他动作不得捏造任务卡。"
    "明确准备发动态、评区回复或发私信前用 prepare_publication；准备发布动态即使前文提到过视频或对象，也仍用 prepare_publication。"
    "多候选对象下出现“那条/那个/这个/前面那个”等单数指代且未唯一指定序号、标题、BV 或 ID 时，必须用 clarify 请对方点名，禁止猜选或全选。"
    "若对方正在回答上轮澄清，clarification_id 填该轮 id，保留原要求；换话题时不填。"
)

_LIVE_TOOL_CONTEXT = (
    "可即时读取时钟、运营活动、视频进度、账号、记忆、直播与游戏状态、已送达和已播回执。"
    "普通闲聊用 chat 自然接话；当对方明确要求记住、修改或忘掉长期偏好/事实时，使用记忆工具直接保存或更新；请求查看已保存的记忆摘要才需查询摘要。"
    "回答当前状态时读取相应对象，以最新结果为准。"
)

_DIRECT_READS = {action for domain in CAPABILITIES.values() for action in domain.read_actions}
_PRESENTED_READS = _DIRECT_READS | {LOOKUP_TOOL, recall_memory.NAME}
_DIRECT_CHANGES = {action for domain in CAPABILITIES.values() for action in domain.change_actions}
_CURRENT_SCOPE_READS = {
    "clock", "game_status", "live_status", "operating_account", "operation_status", "workbench_capabilities",
    "outgoing_queue",
}
_SETTING_ACTIONS = {"daily_settings", "pet_settings"}
_OWNER_RELATION = re.compile(
    r"主人|号主|饲主|谁家|谁.{0,6}(?:养|照顾)|哪.{0,6}(?:养|照顾)|(?:养|照顾)(?:着)?你|属于谁"
)
_DIRECT_RELATION = re.compile(
    r"^(?:(?:请问|现在|告诉我|我想知道)\s*)*(?:你(?:的)?\s*)?(?:主人|饲主|号主)\s*"
    r"(?:是谁|是哪[位个]人?|叫(?:什么|啥)|是谁家的人)"
    r"|^(?:我说|我是问|我指的是)\s*你(?:的)?(?:主人|饲主|号主)[啊呀呢嘛？?。\s]*$"
    r"|^(?:(?:那|现在|请问)\s*)*你.{0,8}(?:谁|哪[位个]人?).{0,3}(?:养|照顾)"
    r"|^(?:是)?(?:谁|哪[位个]人?).{0,3}(?:养|照顾)你"
)
_OWNER_PLATFORM = re.compile(r"[Bb]\s*站|哔哩哔哩|[Bb]ilibili|主页|空间|账号|投稿")
_OWNER_FIELDS = re.compile(r"主页|空间|UID|链接|网址|地址|账号|(?<![A-Za-z])link(?![A-Za-z])", re.I)
_LIVE_PROFILE = re.compile(r"核验|核对|最新|近期|投稿|去看|打开|查.{0,10}主页")
_CURRENT_ACCOUNT = re.compile(r"登录|运营账号|登录账号")
_OBJECT_READS = {
    "owner": {"owner_binding", "owner_profile"},
    "account": {"operating_account"},
    "operations": {"operation_status", "activity", "video_activity"},
    "event": {"event_author", "event_details", "event_reply"},
    "video": {"video_details", "inspect_video_frame", "full_video"},
    "watched_video": {"workshop_lookup"},
    "public_comment": {"workshop_lookup"},
    "dynamic_post": {"workshop_lookup"},
    "live": {"live_status", "live_history"},
    "game": {"game_status"},
    "memory": {"recall_experience", "memory_summary"},
    "task": {"task_status"},
    "artifact": {"task_status"},
    "web": {"web_lookup"},
    "outgoing": {"outgoing_queue"},
}
_TASK_CONTROLS = {"pause_task", "resume_task", "watch_goal"}
_WORKSHOP_OBJECTS = {"watched_video": "video", "public_comment": "comment", "dynamic_post": "dynamic"}

_WORKBENCH_TOOL_CONTEXT = (
    "即时工具可列出当前可用能力和可调项目、读设置、在已有桌面形象与表情中选择、"
    "按对方要求调整日常业务设置。询问能力或选项先读目录；明确要求改变才执行。"
    "后台任务承接持续运营、研究、取材和交付。权限策略可在设置页管理。"
)

_POLICY_FIELDS = {
    "browsing": {
        "discovery_enabled", "top_level_enabled", "top_level_auto_publish", "reply_enabled", "reply_auto_publish",
        "comment_like_enabled", "comment_like_auto_publish", "video_like_enabled", "video_like_auto_publish",
        "video_fav_enabled", "video_fav_auto_publish",
    },
    "messages": {"enabled", "auto_send", "auto_poll", "allow_comment_handoff"},
    "dynamics": {"enabled", "auto_publish"},
}
_CONTROL_ACTIONS = {"operation_control", "start_operations", "start_live"}
_ACTIVE_VIDEO_STATES = {"waiting", "queued", "running"}

_NONCANCELLABLE_ACTIONS = {
    "agent_task", "steer_task", "cancel_task", *_TASK_CONTROLS,
    "browse", "full_video", "inspect_video_frame", "stop", "resume_video", "game_handoff",
    "dynamic", "edit_dynamic", "remember_event", "forget_memory",
    "save_memory", "update_memory", "delete_memory", "live_preference", "live_handoff",
    "daily_settings", "pet_settings",
}


def event_query_actions(message):
    return {action for pattern, action in _EVENT_QUERIES if re.search(pattern, message)}


def _memory_query(message, character):
    """Drop an opening address before searching for subject matter."""
    return without_address(message, character)


def _read_target_text(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


memory_query = _memory_query
read_target_text = _read_target_text


class VideoRange(StrictModel):
    part: int = Field(ge=1, strict=True)
    start: float = Field(ge=0, strict=True)
    end: float = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("视频区间结束时间须晚于开始时间")
        return self


class VideoSelection(StrictModel):
    bvid: str = Field(pattern=r"^BV[0-9A-Za-z]{10}$")
    ranges: list[VideoRange] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def disjoint(self):
        self.ranges.sort(key=lambda item: (item.part, item.start))
        for left, right in zip(self.ranges, self.ranges[1:]):
            if left.part == right.part and left.end > right.start:
                raise ValueError("同一分P的取材区间不可重叠")
        return self


class DelegationBrief(StrictModel):
    goal: str = Field(min_length=1, max_length=400)
    constraints: list[Annotated[str, StringConstraints(max_length=200)]] = Field(default_factory=list, max_length=8)
    deliverable: str = Field(min_length=1, max_length=400)
    material_refs: list[str] = Field(default_factory=list, max_length=6)
    read_tools: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="明确只需读取指定对象时填完整读取范围，持续研究、写作或执行计划留空。",
    )


class IdentityRead(StrictModel):
    subject: Literal["bilibili_owner", "operating_account"]
    fields: list[Literal["name", "uid", "homepage"]] = Field(min_length=1, max_length=3)


class ChatIntent(StrictModel):
    _task_patch: TaskPatch | None = PrivateAttr(default=None)
    _settings_read_only: bool = PrivateAttr(default=False)
    _setting_fields: tuple[str, ...] = PrivateAttr(default=())
    _subject: ConversationObject | None = PrivateAttr(default=None)
    _lookup: WorkshopLookup | None = PrivateAttr(default=None)
    _recall: recall_memory.RecallQuery | None = PrivateAttr(default=None)
    _fixed_metrics: bool = PrivateAttr(default=False)
    _continue_research: bool = PrivateAttr(default=False)
    _from_router: bool = PrivateAttr(default=False)
    _reply_markdown: str = PrivateAttr(default="")
    _identity_reply: bool = PrivateAttr(default=False)
    _clock_fields: tuple[str, ...] = PrivateAttr(default=())
    _read_original: bool = PrivateAttr(default=False)
    _recall_reason: bool = PrivateAttr(default=False)
    action: Literal[
        "chat", "clarify", "identity_read", "clock", "activity", "event_details", "event_author", "event_reply",
        "video_activity", "dynamic", "edit_dynamic", "browse", "full_video", "public_experiences", "remember_event",
        "operation_control", "start_operations", "live_status", "start_live", "stop", "resume_video", "video_details",
        "recall_experience", "inspect_video_frame", "live_handoff", "live_preference", "forget_memory",
        "save_memory", "update_memory", "delete_memory", "memory_summary",
        "game_status", "game_handoff", "live_history", "owner_profile", "owner_binding", "operating_account",
        "operation_status", "web_lookup", "task_status", "agent_task", "steer_task", "cancel_task",
        "pause_task", "resume_task", "watch_goal",
        "workbench_capabilities", "pet_settings", "daily_settings",
        "outgoing_queue", "prepare_publication", "workshop_lookup", "recall_memory",
    ]
    publication_channel: Literal["dynamics", "comments", "messages"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    topic: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    memory_content: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    memory_tags: list[str] = Field(default_factory=list, max_length=10, exclude_if=lambda value: not value)
    reference: Annotated[str, StringConstraints(max_length=90)] | None = None
    clarification_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    position: int | None = Field(default=None, ge=1, le=3, strict=True)
    part: int | None = Field(default=None, ge=1, strict=True)
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    source: Literal["video", "discussion", "game"] | None = None
    game: Literal["stardew_valley", "pvz"] | None = None
    game_action: Literal[
        "step", "water_crops", "chop_wood", "go_fishing", "eat_snack", "harvest", "sleep_day",
        "plant_sunflower", "plant_peashooter", "plant_wallnut", "use_cherry_bomb",
    ] | None = None
    settings_group: str | None = Field(default=None, max_length=40, exclude_if=lambda value: value is None)
    arrangement: Arrangement | None = Field(default=None, exclude_if=lambda value: value is None)
    brief: DelegationBrief | None = Field(default=None, exclude_if=lambda value: value is None)
    identities: list[IdentityRead] = Field(default_factory=list, max_length=2, exclude_if=lambda value: not value)


class TemporaryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class OpenedArtifact(StrictModel):
    task_id: UUID
    task_revision: int = Field(ge=1, strict=True)
    edition: int = Field(ge=1, strict=True)


class ChatTurn(StrictModel):
    opened_artifact: OpenedArtifact | None = Field(default=None, exclude_if=lambda value: value is None)
    request_id: UUID
    message: Text
    creative_goal_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    video_selection: VideoSelection | None = None
    intent: ChatIntent | None = None
    temporary: bool = False
    call_scope: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None)
    call_context: CallOrigin | None = Field(default=None, exclude=True)
    conversation_id: str | None = Field(default=None, max_length=100, exclude=True)
    window: list[TemporaryMessage] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def private_window(self):
        if self.intent and self.intent.action in {"pet_settings", "daily_settings"}:
            raise ValueError("请通过对话直接说明要修改的日常设置")
        if self.window and not self.temporary:
            raise ValueError("本轮窗口只用于临时对话")
        if self.call_context and not self.call_scope:
            raise ValueError("通话消息须提供当前授权")
        if self.temporary and (
            self.call_scope or self.opened_artifact or self.creative_goal_id or self.video_selection
            or self.intent and self.intent.action != "chat"
        ):
            raise ValueError("临时对话只接受聊天消息")
        return self


class LivePreferenceSelection(StrictModel):
    preference: Literal["result_first", "process_first", "concise", "detailed"] | None


class ControlConfirmation(StrictModel):
    browsing: dict[str, bool] = Field(default_factory=dict)
    messages: dict[str, bool] = Field(default_factory=dict)
    dynamics: dict[str, bool] = Field(default_factory=dict)


class EventSelection(StrictModel):
    items: list[int] = Field(max_length=3)
    introduction: Annotated[str, StringConstraints(max_length=120)] = ""
    summaries: list[Annotated[str, StringConstraints(max_length=300)]] = Field(default_factory=list, max_length=3)
    supporting_comments: list[Annotated[list[Literal[1, 2]], Field(max_length=2)]] = Field(default_factory=list, max_length=3)


@dataclass
class PendingChat:
    scope: tuple
    task: asyncio.Task
    conversation: str
    cancellable: bool = True
    stopped: bool = False


class DeliveryPacket(StrictModel):
    """Standardized background/action delivery payload for foreground role voicing."""

    kind: Literal[
        "instant_read",
        "direct_action",
        "task_accepted",
        "task_steered",
        "task_completed",
        "task_waiting_approval",
        "task_failed",
        "clarify",
        "prepare_publication",
    ]
    action: str = ""
    summary: str = ""
    facts: dict = Field(default_factory=dict)
    sources: list[dict] = Field(default_factory=list)
    card: dict | None = None
    tool_receipt: dict | None = None
    task_delivery: dict | None = None
    memory_dependent: bool = False
    clarification: dict | None = None
    question: str = ""


class TurnRoute(StrictModel):
    """Low-ambiguity 7-way top-level route chosen by background llm-default."""

    route: Literal[
        "chat",
        "instant_read",
        "direct_action",
        "delegate_task",
        "steer_task",
        "prepare_publication",
        "clarify",
    ]
    action: str = "chat"
    topic: str = ""
    memory_content: str = ""
    memory_tags: list[str] = Field(default_factory=list)
    question: str = ""
    object_ref: str | None = None
    reference: str | None = None
    clarification_id: UUID | None = None
    position: int | None = Field(default=None, ge=1, le=3, strict=True)
    part: int | None = Field(default=None, ge=1, strict=True)
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    source: Literal["video", "discussion", "game"] | None = None
    game: Literal["stardew_valley", "pvz"] | None = None
    game_action: Literal[
        "step", "water_crops", "chop_wood", "go_fishing", "eat_snack", "harvest", "sleep_day",
        "plant_sunflower", "plant_peashooter", "plant_wallnut", "use_cherry_bomb",
    ] | None = None
    settings_group: str | None = Field(default=None, max_length=40)
    publication_channel: Literal["dynamics", "comments", "messages"] | None = None
    arrangement: Arrangement | None = None
    brief: DelegationBrief | None = None
    identities: list[IdentityRead] = Field(default_factory=list, max_length=2)
    task_id: str | None = None
    task_effect: Literal["revise", "cancel", "keep_current", "discuss"] | None = None
    change_quote: str = ""
    replacement_goal: str = ""
    lookup: WorkshopLookup | None = None
    recall: recall_memory.RecallQuery | None = None
    settings_read_only: bool = False
    setting_fields: list[str] = Field(default_factory=list)
    clock_fields: list[Literal["local_date", "local_time", "local_weekday"]] = Field(default_factory=list, max_length=3)
    read_original: bool = False
    recall_reason: bool = False
    continue_research: bool = False
    watch_day: str | None = None
    route_reason: str = ""

    @model_validator(mode="after")
    def _validate_route_action(self):
        if self.route == "chat":
            if self.action != "chat":
                raise ValueError(f"route=chat 不允许动作 {self.action}")
        elif self.route == "delegate_task":
            if self.action == "chat":
                self.action = "agent_task"
            elif self.action != "agent_task":
                raise ValueError(f"route=delegate_task 不允许动作 {self.action}")
        elif self.route == "prepare_publication":
            if self.action == "chat":
                self.action = "prepare_publication"
            elif self.action != "prepare_publication":
                raise ValueError(f"route=prepare_publication 不允许动作 {self.action}")
        elif self.route == "clarify":
            if self.action == "chat":
                self.action = "clarify"
            elif self.action != "clarify":
                raise ValueError(f"route=clarify 不允许动作 {self.action}")
        elif self.route == "steer_task":
            if self.task_effect == "cancel" and self.action == "chat":
                self.action = "cancel_task"
            elif self.task_effect == "revise" and self.action == "chat":
                self.action = "steer_task"
            if self.action not in {"steer_task", "cancel_task", "chat"}:
                raise ValueError(f"route=steer_task 不允许动作 {self.action}")
        elif self.route == "instant_read":
            if self.action in _OWNER_UI_ONLY or self.action not in _ROUTE_READ_ACTIONS:
                raise ValueError(f"route=instant_read 不允许动作 {self.action}")
        elif self.route == "direct_action":
            if self.action in _OWNER_UI_ONLY or self.action not in _ROUTE_CHANGE_ACTIONS:
                raise ValueError(f"route=direct_action 不允许动作 {self.action}")
        return self


class VoiceRoute(StrictModel):
    """Unified background route decision for a signed voice call utterance."""

    action: Literal["conversation", "delegate", "revise", "cancel"]
    quote: str = ""
    task_id: str | None = None
