"""Schemas and top-level workbench TaskRuntime composition for Agent tasks."""

from typing import Literal
from uuid import UUID

from pydantic import Field, PositiveInt, field_validator

from ...core.models import StrictModel
from ..chat.lookup import operator_facts, workshop_lookup
from ..chat.lookup.dynamic_publication import register_dynamic_publication
from ..chat.models import VideoSelection
from ..creative.private_browser import attach_private_browser
from ..creative.private_code import CodeFile, run_code
from ..goals.events import attach_goal_events
from ..goals.followups import attach_goal_followups
from ..memory_organizer import recall as recall_memory
from ..tasks.runtime import Tool
from ..tasks.tools import create_task_runtime as _create_base_task_runtime


class TextAttachment(StrictModel):
    name: str = Field(min_length=1, max_length=160, pattern=r"^[^/\\\x00-\x1f]+\.(?:md|txt|csv|json|srt|vtt)$")
    text: str = Field(min_length=1, max_length=8000)

    @field_validator("text")
    @classmethod
    def readable(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("附件必须是非空文本")
        return value


class TaskCreate(StrictModel):
    request_id: UUID
    goal: str = Field(min_length=1, max_length=2000)
    run_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    creative_goal_id: UUID | None = None
    attachments: list[TextAttachment] = Field(default_factory=list, max_length=3)
    allow_code: bool = Field(default=False, strict=True)
    browser_sources: list[str] | None = Field(default=None, min_length=1, max_length=6)
    video_selection: VideoSelection | None = None

    @field_validator("goal")
    @classmethod
    def non_empty(cls, value):
        if not value.strip():
            raise ValueError("请填写任务目标")
        return value.strip()


class TaskControl(StrictModel):
    revision: int = Field(ge=1, strict=True)
    action: Literal["steer", "cancel", "approve", "pause", "resume"]
    goal: str | None = Field(default=None, max_length=2000)
    step_id: UUID | None = None
    video_selection: VideoSelection | None = None
    selected_ids: list[PositiveInt] | None = Field(default=None, min_length=1)


class EditionEdit(StrictModel):
    task_revision: int = Field(ge=1)
    edition: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=64000)


class GoalStop(StrictModel):
    confirmed_runs: dict[UUID, PositiveInt]


def attach_workbench_task_extensions(tasks):
    """Attach private browser, isolated QuickJS code tool, goal followups, and goal event watchers."""
    if getattr(tasks, "private_browser", None) is None:
        attach_private_browser(tasks, tool_cls=Tool)
    if "private_code" not in tasks.tools:
        tasks.register(
            "private_code",
            Tool(
                "在获准的隔离 QuickJS 中变换私有附件。script 是同步函数体，files 为 {name,text} 数组，"
                "返回完整文件字符串并指定输出文件名。没有网络、磁盘或宿主命令。",
                CodeFile,
                run_code,
                responses_only=True,
            ),
        )
    if getattr(tasks, "followups", None) is None:
        attach_goal_followups(tasks)
    if getattr(tasks, "goal_events", None) is None:
        attach_goal_events(tasks)
    return tasks


def register_workbench_task_tools(tasks, runtime, store, browsing):
    """Attach chat lookup, recall_memory and dynamic publication tools to TaskRuntime without L3 cycles."""
    from ...core.models import StrictModel

    async def today(request, args):
        date = operator_facts.now(browsing).date().isoformat()
        events = operator_facts.daily_events(browsing, date)
        return {"date": date, "events": [{"title": event["title"], "source": event["source"]}
                                        for event in events[:6]]}

    async def lookup_workshop(request, args):
        return workshop_lookup.lookup(browsing, request.scope, args)

    tasks.register("today_activity", Tool("读取当前业务日的公开运营事件和来源。", StrictModel, today, foreground_read=True))
    tasks.register(
        workshop_lookup.LOOKUP_TOOL,
        Tool(
            workshop_lookup.LOOKUP_DESCRIPTION,
            workshop_lookup.WorkshopLookup,
            lookup_workshop,
            foreground_read=True,
            reusable=True,
        ),
    )
    if (dynamics := getattr(browsing, "dynamics", None)) is not None:
        tasks.recall_dynamics = dynamics
        tasks.validate_recall_read = lambda receipt: recall_memory.validate_read(dynamics, receipt)
        tasks.tool_continuations["recall_memory"] = recall_memory.continue_task

        async def recall_workshop(request, args):
            result = recall_memory.search(dynamics, args, private=True)
            seen = {recall_memory.canonical_id(item) for step in request.receipts if step["tool"] == recall_memory.NAME
                    for item in step.get("result", {}).get("items", [])}
            result["items"] = [item for item in result["items"] if recall_memory.canonical_id(item) not in seen]
            return result | recall_memory.read_authority(runtime, request.scope)

        tasks.register(
            recall_memory.NAME,
            Tool(
                recall_memory.DESCRIPTION,
                recall_memory.RecallQuery,
                recall_workshop,
                foreground_read=True,
                reusable=True,
                responses_only=True,
            ),
        )
        register_dynamic_publication(tasks, dynamics)


def create_workbench_task_runtime(runtime, store, browsing):
    """Build TaskRuntime with core execution tools plus workshop/recall/dynamic tools."""
    return _create_base_task_runtime(runtime, store, browsing, registrars=(register_workbench_task_tools,))
