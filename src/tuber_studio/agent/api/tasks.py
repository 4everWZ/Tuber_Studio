"""Schemas and top-level workbench TaskRuntime composition for Agent tasks."""

import inspect
from typing import Literal
from uuid import UUID, uuid4

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


class SyncMemeSourcesArgs(StrictModel):
    source_id: str | None = Field(default=None, max_length=64)


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

    async def _maybe_await(value):
        return await value if inspect.isawaitable(value) else value

    def _project_source(row: dict) -> dict:
        return {
            "source_id": row.get("source_id"),
            "name": row.get("name"),
            "kind": row.get("kind"),
            "enabled": bool(row.get("enabled")),
            "last_status": row.get("last_status"),
            "last_synced_at": row.get("last_synced_at"),
            "last_error": str(row.get("last_error") or "")[:200],
        }

    async def run_meme_status(request, args):
        harvester = getattr(runtime, "meme_harvester", None)
        catalog = getattr(getattr(runtime, "pets", None), "catalog", None)
        if harvester is None and catalog is None:
            return {"error": "表情包图库服务尚未就绪。"}
        gallery_total = gallery_enabled = 0
        if catalog is not None and hasattr(catalog, "list"):
            char = getattr(runtime, "character", None)
            assets = catalog.list(char, include_disabled=True) if char else []
            gallery_total = len(assets)
            gallery_enabled = sum(1 for item in assets if item.get("enabled", True))
        inbox_pending = inbox_pending_vision = 0
        comment_harvest_enabled = True
        sources_list = []
        if harvester is not None:
            if hasattr(harvester, "list_inbox"):
                inbox = harvester.list_inbox(status="pending") or {}
                inbox_pending = int(inbox.get("pending_count", len(inbox.get("items", []))))
                inbox_pending_vision = int(inbox.get("pending_vision_count", 0))
            if hasattr(harvester, "get_config"):
                comment_harvest_enabled = bool((harvester.get_config() or {}).get("comment_harvest_enabled", True))
            if hasattr(harvester, "list_sources"):
                raw_sources = (harvester.list_sources() or {}).get("items", [])
                sources_list = [_project_source(s) for s in raw_sources if isinstance(s, dict)]
            elif hasattr(harvester, "status"):
                raw_status = await _maybe_await(harvester.status()) or {}
                inbox_pending = int(raw_status.get("inbox_pending", inbox_pending))
                inbox_pending_vision = int(raw_status.get("inbox_pending_vision", inbox_pending_vision))
                comment_harvest_enabled = bool(raw_status.get("comment_harvest_enabled", comment_harvest_enabled))
                sources_list = [_project_source(s) for s in (raw_status.get("sources") or []) if isinstance(s, dict)]
        return {
            "inbox_pending": inbox_pending,
            "inbox_pending_vision": inbox_pending_vision,
            "gallery_total": gallery_total,
            "gallery_enabled": gallery_enabled,
            "comment_harvest_enabled": comment_harvest_enabled,
            "sources_total": len(sources_list),
            "sources_enabled": sum(1 for s in sources_list if s["enabled"]),
            "sources": sources_list[:20],
        }

    async def run_meme_sync(request, args):
        harvester = getattr(runtime, "meme_harvester", None)
        if harvester is None:
            return {"error": "表情包采集器尚未就绪。"}
        source_id = (args.source_id or "").strip() or None
        queue_res: dict = {}
        if source_id:
            sync_res = await _maybe_await(harvester.sync_source(source_id)) or {}
            if hasattr(harvester, "process_queue_once"):
                queue_res = await _maybe_await(harvester.process_queue_once()) or {}
            elif hasattr(harvester, "drain_queue"):
                queue_res = await _maybe_await(harvester.drain_queue()) or {}
        elif hasattr(harvester, "sync_all_sources"):
            sync_res = await _maybe_await(harvester.sync_all_sources()) or {}
            if hasattr(harvester, "process_queue_once"):
                queue_res = await _maybe_await(harvester.process_queue_once()) or {}
            elif hasattr(harvester, "drain_queue"):
                queue_res = await _maybe_await(harvester.drain_queue()) or {}
        else:
            sync_res = await _maybe_await(harvester.sync_all()) or {}
            if isinstance(sync_res, dict) and isinstance(sync_res.get("queue"), dict):
                queue_res = sync_res["queue"]

        source_entries = sync_res.get("sources") if isinstance(sync_res, dict) and isinstance(sync_res.get("sources"), list) else (
            [sync_res] if isinstance(sync_res, dict) and "source_id" in sync_res else []
        )
        errors: list[str] = []
        for err in (sync_res.get("errors") or () if isinstance(sync_res, dict) else ()):
            errors.append(str(err)[:200])
        for err in (queue_res.get("errors") or () if isinstance(queue_res, dict) else ()):
            errors.append(str(err)[:200])
        for entry in source_entries:
            if isinstance(entry, dict) and (entry.get("error") or entry.get("status") == "error"):
                errors.append(f"{entry.get('source_id', '')}: {str(entry.get('error') or 'error')[:160]}")

        synced = int(
            sync_res["synced"]
            if isinstance(sync_res, dict) and "synced" in sync_res
            else sum(1 for e in source_entries if not (isinstance(e, dict) and e.get("status") == "error"))
        )
        added_inbox = int(
            sync_res["added_inbox"]
            if isinstance(sync_res, dict) and "added_inbox" in sync_res
            else queue_res.get("added_inbox", 0)
            or (sum(int(e.get("accepted", 0)) for e in source_entries if isinstance(e, dict)) + int(queue_res.get("accepted", 0)))
        )
        auto_approved = int(
            (sync_res.get("auto_approved", 0) if isinstance(sync_res, dict) else 0) + int(queue_res.get("auto_approved", 0))
        )
        discarded = int(
            sync_res["discarded"]
            if isinstance(sync_res, dict) and "discarded" in sync_res
            else int(queue_res.get("discarded", 0)) + sum(int(e.get("skipped", 0)) for e in source_entries if isinstance(e, dict))
        )
        pending_vision = int(
            queue_res["pending_vision"]
            if isinstance(queue_res, dict) and "pending_vision" in queue_res
            else queue_res.get("deferred", 0)
        )
        if not pending_vision and hasattr(harvester, "list_inbox"):
            pending_vision = int((harvester.list_inbox(status="pending") or {}).get("pending_vision_count", 0))
        return {
            "evidence_id": f"meme_sync:{uuid4().hex[:16]}",
            "synced": synced,
            "added_inbox": added_inbox,
            "auto_approved": auto_approved,
            "discarded": discarded,
            "pending_vision": pending_vision,
            "errors": errors[:20],
        }

    tasks.register(
        "meme_harvester_status",
        Tool(
            "只读查询表情包待审箱新图数量、主图库数量与订阅源同步状态，不触发同步。",
            StrictModel,
            run_meme_status,
            foreground_read=True,
            parallel_read=True,
            responses_only=True,
        ),
    )
    tasks.register(
        "sync_meme_sources",
        Tool(
            "在后台同步表情包订阅源并执行待审视觉筛选，返回精简统计与 evidence_id。",
            SyncMemeSourcesArgs,
            run_meme_sync,
            responses_only=True,
            own_deadline=True,
        ),
    )


def create_workbench_task_runtime(runtime, store, browsing):
    """Build TaskRuntime with core execution tools plus workshop/recall/dynamic tools."""
    return _create_base_task_runtime(runtime, store, browsing, registrars=(register_workbench_task_tools,))
