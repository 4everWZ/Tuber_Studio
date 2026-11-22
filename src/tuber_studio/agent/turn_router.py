"""Background llm-default turn router and voice utterance classifier (L2b)."""

import asyncio
import json
import logging
import re
import time
from contextlib import nullcontext
from datetime import timedelta

import httpx
from fastapi import HTTPException

from ..front_persona import character_names
from ..operation_identity import without_model_identity
from ..store import dump
from . import operator_facts, outgoing_queue, recall_memory, workbench_tools, workshop_lookup
from .assistant_output import calendar_fields
from .chat_context import ChatContext
from .chat_models import (
    _CURRENT_ACCOUNT,
    _DIRECT_CHANGES,
    _DIRECT_READS,
    _DIRECT_RELATION,
    _EVENT_ACTIONS,
    _MENTIONED_BV,
    _DYNAMIC_ID,
    _DYNAMIC_URL,
    _ORDINAL_REFERENCE,
    _OWNER_FIELDS,
    _OWNER_PLATFORM,
    _OWNER_RELATION,
    _OWNER_UI_ONLY,
    _ROUTE_CHANGE_ACTIONS,
    _ROUTE_READ_ACTIONS,
    _SETTING_ACTIONS,
    _SINGULAR_DEICTIC,
    _TASK_CONTROLS,
    _TASK_ROUTING,
    _WORKBENCH_TOOL_CONTEXT,
    _WORKSHOP_OBJECTS,
    _LIVE_PROFILE,
    _LIVE_TOOL_CONTEXT,
    ChatIntent,
    DelegationBrief,
    IdentityRead,
    TurnRoute,
    VoiceRoute,
    memory_query,
)
from .operator_task_patch import TaskPatch, grounded_patch, patch_vocabulary, review_change
from .personal_agent import Arrangement
from .task_delegation import function
from .workshop_lookup import WorkshopLookup

logger = logging.getLogger("uvicorn.error").getChild("turn_router")

_PRIMARY_READ_ACTIONS = (
    workshop_lookup.LOOKUP_TOOL,
    "watch_totals",
    recall_memory.NAME,
    "daily_settings",
    "pet_settings",
    "workbench_capabilities",
    "operation_status",
    "live_status",
    "owner_profile",
    "owner_binding",
    "operating_account",
    "identity_read",
    "web_lookup",
    "outgoing_queue",
    "task_status",
    "activity",
    "game_status",
)

_COMPACT_ARRANGEMENT_SCHEMA = {
    "type": "object",
    "description": "仅在号主明确预约未来日期时间（必须填 local_date 或 local_time）或周期/来信事件触发时填写；立即执行的普通任务不要填此项。",
    "properties": {
        "kind": {"type": "string", "enum": ["once", "daily", "weekly", "inbox", "comment"]},
        "quote": {"type": "string", "minLength": 2, "maxLength": 300},
        "local_date": {"type": "string", "maxLength": 10},
        "local_time": {"type": "string", "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$"},
        "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
        "states": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "enum": ["live_status", "game_status", "operation_status", "today_activity"]},
        },
        "sender_uid": {"type": "string", "pattern": r"^[1-9]\d{0,19}$"},
        "resource": {"type": "string", "maxLength": 32},
        "root_id": {"type": "string", "pattern": r"^[1-9]\d{0,19}$"},
        "after_reply": {"type": "string", "minLength": 1, "maxLength": 400},
    },
    "required": ["kind", "quote"],
    "additionalProperties": False,
}


def _as_str_list(value) -> list[str]:
    """Normalize array or JSON-encoded array strings from tool arguments into stripped strings."""
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except ValueError:
                return []
        else:
            return [raw] if raw else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []

_VOICE_ROUTING_PROMPT = (
    "判断当前这句号主来话是否明确交给工坊处理一项工作。只作分流。"
    "原话里的「我」是号主，「你」是工坊。由号主指定工坊去做时才交办。"
    "号主说自己要去做、把结果发给听者、或者复述他人的吩咐，属于 conversation。"
    "普通接话、事实问答、讨论、转述、否定和假设归 conversation。"
    "要求接下来查找并交付资料、制作成果或稍后执行具体工作，可以交办。"
    "新交办选 delegate，目标和交付动作都须明确。改口选 revise，停止选 cancel，这两类都须指向本次通话里的唯一任务。"
    "quote 原样摘录能确定受托方和工作目标的连续原话。任务列表仅用于识别改口和取消。"
)


def routing_client(runtime):
    """Return the background Responses client when dual-brain routing is available."""
    provider = getattr(runtime, "llm", None)
    if not getattr(provider, "uses_model_card", False):
        return None
    tasks = getattr(runtime, "tasks", None)
    get_client = getattr(tasks, "responses_client", None)
    if not callable(get_client):
        return None
    client = get_client()
    if client is None:
        return None
    if getattr(client, "_requires_omni_key", False):
        settings = getattr(runtime, "settings", None)
        if not settings or not settings.provider_key("omni"):
            return None
    return client


def direct_tool_actions() -> tuple[str, ...]:
    """Return the allowed tool/intent actions exposed to routing models."""
    actions = (
        _DIRECT_READS
        | _DIRECT_CHANGES
        | _EVENT_ACTIONS
        | _TASK_CONTROLS
        | {
            "task_status", "agent_task", "steer_task", "cancel_task",
            "prepare_publication", "outgoing_queue", "identity_read",
            workshop_lookup.LOOKUP_TOOL, recall_memory.NAME,
            "owner_profile", "owner_binding", "operating_account", "operation_status",
            "web_lookup", "workbench_capabilities", "pet_settings", "daily_settings",
        }
    ) - _OWNER_UI_ONLY
    return tuple(sorted(actions))


def _delegation_reads(runtime):
    return {
        name: tool.description
        for name, tool in runtime.tasks.tools.items()
        if tool.foreground_read or name == "inspect_video"
    }


def route_tools(runtime, *, cards=(), objects=(), candidates=(), read_only=False, allow_identity_read=True,
                has_clarification=False):
    """Build the 7 compact flat Responses tools without object_ref-based pruning."""
    groups = workbench_tools.settings_menu()
    object_enum = [item["ref"] for item in candidates] if candidates else []
    object_schema = (
        {"type": "string", "enum": object_enum} if object_enum else {"type": "string", "maxLength": 40}
    )
    task_enum = [card["id"] for card in cards]
    delegation_reads = _delegation_reads(runtime)
    read_tool_schema = (
        {"type": "string", "enum": list(delegation_reads)}
        if delegation_reads
        else {"type": "string", "maxLength": 60}
    )

    def tool(name, description, properties, *, required=()):
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        }
        return function(name, description, schema, require_all=False)

    def with_clarification(properties: dict) -> dict:
        if has_clarification:
            return properties | {"clarification_id": {"type": "string", "maxLength": 64}}
        return properties

    chat_props = {
        "clock_fields": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "enum": ["local_date", "local_time", "local_weekday"]},
        },
    }
    if task_enum:
        chat_props |= {
            "reference": {
                "type": "string",
                "enum": task_enum,
                "description": "仅当号主明确提及或讨论某项已有任务或成果时填 task_id；普通闲聊不要填此项。",
            },
            "read_original": {"type": "boolean"},
            "recall_reason": {"type": "boolean"},
        }
    menu = [
        tool(
            "chat",
            "纯闲聊、打招呼、玩笑、设想（假如/要是/如果）、否定动作（先别/别动/不用/先别接新活），或只问当前钟点/日期/星期（填 clock_fields；非问当前时间留空数组）。",
            chat_props,
        )
    ]

    if not read_only:
        menu.append(
            tool(
                "delegate_task",
                "只要号主要求留下持久产物（留份便笺/交接记录/笔记/总结/报告/提纲）、稍后交付（等我回来再看）、交给后台处理或定时预约，就必须选此项——即使句中同时提到核实直播/运营状态也选 delegate_task 而非 instant_read。否定措辞（先别/别接新活）禁止选此项。",
                with_clarification({
                    "goal": {"type": "string", "minLength": 1, "maxLength": 400},
                    "deliverable": {"type": "string", "minLength": 1, "maxLength": 400},
                    "constraints": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 200}},
                    "material_refs": {"type": "array", "maxItems": 6, "items": object_schema},
                    "read_tools": {"type": "array", "maxItems": 8, "items": read_tool_schema},
                    "arrangement": _COMPACT_ARRANGEMENT_SCHEMA,
                }),
                required=("goal", "deliverable"),
            )
        )

        object_items = objects.values() if isinstance(objects, dict) else objects
        has_active_video = any(
            getattr(obj, "kind", None) == "video" or (isinstance(obj, dict) and obj.get("kind") == "video")
            for obj in object_items
        )
        allowed_reads = [
            action
            for action in _ROUTE_READ_ACTIONS
            if action not in _OWNER_UI_ONLY
            and action not in {"clock", "video_activity"}
            and (allow_identity_read or action != "identity_read")
            and (has_active_video or action != "video_details")
        ]
        read_actions = [a for a in _PRIMARY_READ_ACTIONS if a in allowed_reads] + [
            a for a in allowed_reads if a not in _PRIMARY_READ_ACTIONS
        ]
        group_hints = "、".join(f"{name}({label})" for name, label in groups.items())
        menu.append(
            tool(
                "instant_read",
                "仅当场口头查一次、不要求留便笺/笔记/总结时使用："
                "watch_totals=仅问今天看了几条数量（问具体看了哪些或想聊哪条视频用 workshop_lookup, collection=video）；"
                "workshop_lookup=查今天/昨天/具体日期（YYYY-MM-DD）或已知BV的已看视频、原视频链接、已发动态及编号（必填 collection=video|comment|dynamic，day仅填today/yesterday/YYYY-MM-DD，严禁填上周或模糊时段）；"
                "recall_memory=按话题回查过往/上周/较早的原评、评论内容、经历、记忆或私聊（回查上周/历史原评必选此项，填 topic；追问 candidates 中的 recalled_source 时填 object_ref）；"
                "operation_status=当场问自主运营状态；live_status=当场问直播间开播状态；"
                f"daily_settings=查当前设置（可选 {group_hints}，必填 settings_group 与 settings_read_only=true，具体字段填 setting_fields）；"
                "pet_settings=查当前宠物设置；workbench_capabilities=问能调哪些设置或工具；"
                "owner_profile=去B站联网核验/去看绑定号主的最新B站主页资料；"
                "owner_binding=查本地记录的绑定号主B站主页链接和UID（不联网，用于问号主/他的主页、链接、UID）；"
                "operating_account=查工坊当前登录的B站运营账号昵称和UID；"
                "identity_read=同时查号主与运营账号（填 identity_subject=bilibili_owner|operating_account|both）；"
                "web_lookup=联网搜新闻或公开网页；outgoing_queue=查待发队列；task_status=查已有交办进度；activity=今日趣事；game_status=游戏状态。",
                {
                    "action": {"type": "string", "enum": read_actions},
                    "collection": {"type": "string", "enum": ["video", "comment", "dynamic"]},
                    "day": {"type": "string", "maxLength": 20},
                    "terms": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 80}},
                    "topic": {"type": "string", "maxLength": 200},
                    "object_ref": object_schema,
                    "position": {"type": "integer", "minimum": 1, "maximum": 3},
                    "part": {"type": "integer", "minimum": 1},
                    "seconds": {"type": "number", "minimum": 0},
                    "watch_day": {"type": "string", "maxLength": 20},
                    "settings_group": {"type": "string", "enum": list(groups)} if groups else {"type": "string", "maxLength": 40},
                    "setting_fields": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 60}},
                    "settings_read_only": {"type": "boolean"},
                    "publication_channel": {"type": "string", "enum": ["dynamics", "comments", "messages"]},
                    "identity_subject": {"type": "string", "enum": ["bilibili_owner", "operating_account", "both"]},
                    "continue_research": {"type": "boolean"},
                },
                required=("action",),
            )
        )

        change_actions = [
            action
            for action in _ROUTE_CHANGE_ACTIONS
            if action not in _OWNER_UI_ONLY
        ]
        direct_props = {
            "action": {"type": "string", "enum": change_actions},
            "topic": {"type": "string", "maxLength": 200},
            "object_ref": object_schema,
            "reference": {"type": "string", "maxLength": 90},
            "settings_group": {"type": "string", "enum": list(groups)} if groups else {"type": "string", "maxLength": 40},
            "game": {"type": "string", "enum": ["stardew_valley", "pvz"]},
            "game_action": {
                "type": "string",
                "enum": [
                    "step", "water_crops", "chop_wood", "go_fishing", "eat_snack", "harvest", "sleep_day",
                    "plant_sunflower", "plant_peashooter", "plant_wallnut", "use_cherry_bomb",
                ],
            },
            "source": {"type": "string", "enum": ["video", "discussion", "game"]},
            "part": {"type": "integer", "minimum": 1},
            "seconds": {"type": "number", "minimum": 0},
            "end_seconds": {"type": "number", "exclusiveMinimum": 0},
        }
        if cards:
            direct_props["arrangement"] = _COMPACT_ARRANGEMENT_SCHEMA
        menu.append(
            tool(
                "direct_action",
                "单轮立即修改工坊设置或执行控制（仅当明确要求改变设置、切换宠物表情、保存/忘记记忆、排队看完整视频或抽帧、暂停/恢复任务或停止时使用；纯查询或假设禁止选此项）。",
                with_clarification(direct_props),
                required=("action",),
            )
        )

        if cards:
            menu.append(
                tool(
                    "steer_task",
                    "号主对已有任务卡提出改口、换重点、补充要求或取消执行时使用；与已有任务无关的日常闲聊必须选 chat 而非 steer_task。"
                    "task_effect=revise/cancel 时必须填 change_quote，逐字原样摘录号主原话中表达新要求或停止的那一段，禁止改写。",
                    with_clarification({
                        "task_id": {"type": "string", "enum": task_enum},
                        "task_effect": {"type": "string", "enum": ["revise", "cancel", "keep_current", "discuss"]},
                        "change_quote": {
                            "type": "string",
                            "maxLength": 400,
                            "description": "逐字原样复制号主消息里表示具体改动或停止的子句（必须原样出现在 message 中，不得改写）。",
                        },
                        "replacement_goal": {
                            "type": "string",
                            "maxLength": 400,
                            "description": TaskPatch.model_fields["replacement_goal"].description,
                        },
                        "goal": {"type": "string", "maxLength": 400},
                        "deliverable": {"type": "string", "maxLength": 400},
                        "constraints": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 200}},
                        "material_refs": {"type": "array", "maxItems": 6, "items": object_schema},
                        "read_tools": {"type": "array", "maxItems": 8, "items": read_tool_schema},
                        "read_original": {"type": "boolean"},
                        "recall_reason": {"type": "boolean"},
                    }),
                    required=("task_id", "task_effect"),
                )
            )

        menu.append(
            tool(
                "prepare_publication",
                "号主要求把已有待发动态、评论回复或私信整理送审/确认发布时必须选此项（操作待发草稿队列，不要误用前文视频或观看历史候选，也不要选 clarify）。",
                with_clarification({
                    "publication_channel": {"type": "string", "enum": ["dynamics", "comments", "messages"]},
                    "topic": {"type": "string", "maxLength": 200},
                }),
                required=("publication_channel",),
            )
        )

    menu.append(
        tool(
            "clarify",
            "仅当 candidates 中存在多个同类对象（如多条视频）且号主只用“那条/那个”等单数模糊指代未给序号/标题/BV，或缺少关键目标无法执行时使用。若号主换了新话题（闲聊/问当前日期时间/问别的事），严禁选 clarify 或重复上轮澄清。",
            with_clarification({
                "question": {"type": "string", "minLength": 1, "maxLength": 200},
            }),
            required=("question",),
        )
    )
    return menu


def _parse_arrangement(raw) -> Arrangement | None:
    if not raw:
        return None
    if isinstance(raw, dict) and isinstance(raw.get("states"), str):
        raw = {**raw, "states": _as_str_list(raw["states"])}
    return Arrangement.model_validate(raw)


def _route_from_tool_call(call_name: str, args: dict) -> TurnRoute:
    if call_name == "chat":
        return TurnRoute(
            route="chat",
            action="chat",
            topic=args.get("topic", ""),
            reference=args.get("reference"),
            read_original=bool(args.get("read_original")),
            recall_reason=bool(args.get("recall_reason")),
            clock_fields=_as_str_list(args.get("clock_fields")),
        )
    if call_name == "instant_read":
        action = args["action"]
        lookup = None
        if args.get("lookup"):
            lookup = WorkshopLookup.model_validate(args["lookup"])
        elif action == workshop_lookup.LOOKUP_TOOL and args.get("collection"):
            day = args.get("day") or args.get("watch_day")
            if isinstance(day, str):
                day = {"今天": "today", "昨天": "yesterday"}.get(day.strip(), day.strip()) or None
                if day and day not in {"today", "yesterday"} and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    day = None
            terms = _as_str_list(args.get("terms"))
            lookup = WorkshopLookup(
                collection=args["collection"],
                day=day,
                terms=terms,
                part=args.get("part"),
                seconds=args.get("seconds"),
            )
        recall = None
        if args.get("recall"):
            recall = recall_memory.RecallQuery.model_validate(args["recall"])
        elif action == recall_memory.NAME and (args.get("topic") or args.get("terms")):
            topic_text = str(args.get("topic") or " ".join(_as_str_list(args.get("terms")))).strip()
            recall = recall_memory.RecallQuery(topic=topic_text)
        identities = [IdentityRead.model_validate(item) for item in args.get("identities", [])]
        if not identities and action == "identity_read" and args.get("identity_subject"):
            raw_subject = {"owner": "bilibili_owner"}.get(args["identity_subject"], args["identity_subject"])
            subjects = ("bilibili_owner", "operating_account") if raw_subject == "both" else (raw_subject,)
            identities = [IdentityRead(subject=subj, fields=["name", "uid", "homepage"]) for subj in subjects]
        return TurnRoute(
            route="instant_read",
            action=action,
            topic=args.get("topic", ""),
            object_ref=args.get("object_ref"),
            reference=args.get("reference"),
            position=args.get("position"),
            part=args.get("part"),
            seconds=args.get("seconds"),
            watch_day=args.get("watch_day") or args.get("day"),
            settings_group=args.get("settings_group"),
            setting_fields=_as_str_list(args.get("setting_fields")),
            settings_read_only=bool(args.get("settings_read_only", True)),
            publication_channel=args.get("publication_channel"),
            identities=identities,
            lookup=lookup,
            recall=recall,
            continue_research=bool(args.get("continue_research")),
        )
    if call_name == "direct_action":
        arrangement = _parse_arrangement(args.get("arrangement"))
        return TurnRoute(
            route="direct_action",
            action=args["action"],
            topic=args.get("topic", ""),
            object_ref=args.get("object_ref"),
            reference=args.get("reference"),
            settings_group=args.get("settings_group"),
            game=args.get("game"),
            game_action=args.get("game_action"),
            source=args.get("source"),
            part=args.get("part"),
            seconds=args.get("seconds"),
            end_seconds=args.get("end_seconds"),
            arrangement=arrangement,
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "delegate_task":
        arrangement = _parse_arrangement(args.get("arrangement"))
        brief = DelegationBrief(
            goal=args["goal"],
            constraints=_as_str_list(args.get("constraints")),
            deliverable=args["deliverable"],
            material_refs=_as_str_list(args.get("material_refs")),
            read_tools=_as_str_list(args.get("read_tools")),
        )
        return TurnRoute(
            route="delegate_task",
            action="agent_task",
            topic=args.get("topic", ""),
            brief=brief,
            arrangement=arrangement,
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "steer_task":
        brief = None
        if args.get("brief"):
            brief = DelegationBrief.model_validate(args["brief"])
        elif args.get("goal") and args.get("deliverable"):
            brief = DelegationBrief(
                goal=args["goal"],
                constraints=_as_str_list(args.get("constraints")),
                deliverable=args["deliverable"],
                material_refs=_as_str_list(args.get("material_refs")),
                read_tools=_as_str_list(args.get("read_tools")),
            )
        effect = args.get("task_effect", "discuss")
        action = {
            "revise": "steer_task",
            "cancel": "cancel_task",
            "keep_current": "chat",
            "discuss": "chat",
        }[effect]
        return TurnRoute(
            route="steer_task",
            action=action,
            task_id=args.get("task_id"),
            reference=args.get("task_id"),
            task_effect=effect,
            change_quote=args.get("change_quote", ""),
            replacement_goal=args.get("replacement_goal", ""),
            brief=brief,
            read_original=bool(args.get("read_original")),
            recall_reason=bool(args.get("recall_reason")),
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "prepare_publication":
        return TurnRoute(
            route="prepare_publication",
            action="prepare_publication",
            publication_channel=args.get("publication_channel"),
            topic=args.get("topic", ""),
            object_ref=args.get("object_ref"),
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "clarify":
        return TurnRoute(
            route="clarify",
            action="clarify",
            question=args["question"],
            topic=args["question"],
            clarification_id=args.get("clarification_id") or None,
        )
    raise ValueError(f"未知的路由工具：{call_name}")


def _unique_candidate_match(message: str, group: list[dict]) -> dict | None:
    """Return the unique candidate in a same-kind group explicitly identified by text/identity."""
    if _ORDINAL_REFERENCE.search(message):
        return None
    mentioned_bv = set(_MENTIONED_BV.findall(message))
    mentioned_dyn = set(_DYNAMIC_URL.findall(message) or _DYNAMIC_ID.findall(message))
    matched = []
    for item in group:
        identity = str(item.get("identity") or "")
        label = str(item.get("label") or "")
        if identity and (identity in mentioned_bv or identity in mentioned_dyn or identity in message):
            matched.append(item)
            continue
        title = label.split("｜", 1)[0].split("（", 1)[0].strip()
        if len(title) >= 3 and title in message:
            matched.append(item)
    return matched[0] if len(matched) == 1 else None


def _candidate_group_key(item: dict) -> str:
    """Group recalled sources by sub-type prefix (e.g. comment vs video) before disambiguation."""
    kind = str(item.get("kind") or "")
    if kind == "recalled_source":
        prefix = str(item.get("identity") or "").split(":", 1)[0]
        if prefix:
            return f"recalled_source:{prefix}"
    return kind


def _disambiguate_route(route: TurnRoute, message: str, candidates: list[dict]) -> TurnRoute:
    """Block guessing or bulk-selecting when a singular deictic spans >= 2 same-kind candidates."""
    if route.route not in {"instant_read", "direct_action", "prepare_publication"}:
        return route
    if route.action in {"clock", "operating_account", "operation_status", "workbench_capabilities", "activity"}:
        return route
    if not candidates or route.position is not None or _ORDINAL_REFERENCE.search(message):
        return route
    if not _SINGULAR_DEICTIC.search(message):
        return route
    if re.search(r"哪些|几[条件个位]|全部|所有|列表|都[要看看查发]|分别|各自", message):
        return route
    by_kind = {}
    for item in candidates:
        by_kind.setdefault(_candidate_group_key(item), []).append(item)
    multi_groups = {kind: group for kind, group in by_kind.items() if len(group) >= 2}
    if not multi_groups:
        return route
    if route.object_ref:
        selected = next((item for item in candidates if item["ref"] == route.object_ref), None)
        selected_kind = _candidate_group_key(selected) if selected is not None else None
        if selected is None or selected_kind not in multi_groups:
            return route
        group = multi_groups[selected_kind]
        if _unique_candidate_match(message, group) is not None:
            return route
        options = "\n".join(f"{idx}. {item['label']}" for idx, item in enumerate(group[:4], 1))
        return TurnRoute(route="clarify", action="clarify", question="你想指哪一个？\n" + options, clarification_id=route.clarification_id)
    # Multiple same-kind candidates and no object_ref / unique pin
    first_group = next(iter(multi_groups.values()))
    if _unique_candidate_match(message, first_group) is not None:
        return route
    options = "\n".join(f"{idx}. {item['label']}" for idx, item in enumerate(first_group[:4], 1))
    return TurnRoute(route="clarify", action="clarify", question="你想指哪一个？\n" + options, clarification_id=route.clarification_id)


def _workshop_decision(route: TurnRoute, context: dict, message: str = "") -> ChatIntent:
    args = route.lookup
    objects = context.get("objects", {})
    ref = route.object_ref or (route.reference if route.reference in objects else None)
    if args is None:
        if ref and (subject := objects.get(ref)):
            collection = "video" if subject.kind == "watch_history" else _WORKSHOP_OBJECTS.get(subject.kind, "video")
            args = WorkshopLookup(collection=collection, day=subject.identity if subject.kind == "watch_history" else None)
        elif len(mentioned := _MENTIONED_BV.findall(message)) == 1:
            args = WorkshopLookup(collection="video", identity=mentioned[0])
        else:
            return ChatIntent(action="clarify", topic="你想查哪条工坊记录？")
    if args.collection == "video" and not args.identity and not ref:
        if len(mentioned := _MENTIONED_BV.findall(message)) == 1:
            args = args.model_copy(update={"identity": mentioned[0], "terms": []})
    decision = ChatIntent(action=workshop_lookup.LOOKUP_TOOL, position=route.position)
    if ref:
        subject = objects.get(ref)
        if subject and subject.kind == "watch_history" and args.collection in {"watch_count", "video"}:
            decision._lookup = args.model_copy(
                update={"day": args.day or WorkshopLookup(collection=args.collection, day=subject.identity).day}
            )
            return decision
        if subject is None or _WORKSHOP_OBJECTS.get(subject.kind) != args.collection:
            return ChatIntent(action="clarify", topic="你指的是哪条记录？说一下标题或编号就行。")
        decision._subject = subject
        args = args.model_copy(
            update={
                "identity": subject.identity,
                "terms": [],
                "part": args.part or route.part or (subject.part if (args.seconds is None and route.seconds is None) else None),
                "seconds": (
                    args.seconds
                    if args.seconds is not None
                    else (route.seconds if route.seconds is not None else (subject.seconds if args.part is None else None))
                ),
            }
        )
    decision._lookup = args
    return decision


def _build_router_messages(char, data, packet, *, allow_identity_read=True):
    fields = workbench_tools.settings_catalog()
    setting_keys = {group: list(fields[group]["fields"]) for group in workbench_tools.settings_menu()}
    system_prompt = (
        f"你是{char['name']}工坊的后台交互调度器。结合最近对话与候选对象清单，每轮仅调用一个最准确的路由工具。\n"
        f"可调设置字段键：{dump(setting_keys)}\n"
        "核心边界（严格按优先级判断）：\n"
        "1. 闲聊、问候、玩笑、设想（假如/要是/如果）、否定/阻止操作（先别/别动/先别接新活），或只问当前几点/几号/星期几 -> chat（仅问当前时间/日期/星期时填 clock_fields）。\n"
        "2. 凡是要求留下便笺/笔记/交接记录/总结/报告、「等我回来再看」、交给后台处理或定时预约 -> 必须选 delegate_task（即使同时提到核实演播间/直播/运营状态，也不能选 instant_read）。\n"
        "3. 对已有 task_id 改口、换重点、补充要求或取消 -> steer_task，且 change_quote 必须逐字复制号主原话里的新要求子句；与已有任务无关的日常闲聊必须选 chat。\n"
        "4. 准备发动态/评论回复/私信送审 -> 必须选 prepare_publication（操作待发队列，不受前文视频/观看记录影响）。\n"
        "5. 仅当场口头只读查询、不留便笺/报告 -> instant_read："
        "查今天/昨天/具体日期或已知BV的视频/原链接/已发动态用 workshop_lookup（必填 collection，day仅限today/yesterday/YYYY-MM-DD）；查数量用 watch_totals；"
        "按话题回查上周/过往原评、旧事或经历必须用 recall_memory（填 topic；追问 recalled_source 填 object_ref）；"
        "查当前设置必须填 action=daily_settings + settings_group + settings_read_only=true；问能调什么用 workbench_capabilities"
        + (
            "；去看/联网核验号主 B 站主页用 owner_profile，查本地记录的号主主页链接/UID 用 owner_binding，查运营登录账号用 operating_account，同时查用 identity_read。"
            if allow_identity_read
            else "。"
        )
        + "\n6. 仅明确要立即修改设置、换宠物表情（pet_settings）、排完整看视频或停止时才选 direct_action。\n"
        "7. 当 candidates 中存在多个同类型对象（如多条视频）且只说“那条/那个”未给出序号/标题/BV 时 -> clarify；若候选分别对应评论与回复且号主明确说“那条评论”，直接按对应 object_ref 调 recall_memory。\n"
        "8. 只针对最后一条 user JSON 中的 message 字段决策；历史对话与 clarification 仅为可选背景。若号主换了新话题（如问当前时间/日期/星期、闲聊、或问别的事），必须完全忽略 clarification，严禁填 clarification_id，也严禁重复上轮澄清问题；仅当 message 明确在回答 clarification.question 时才填 clarification_id。"
    )
    recent = [
        {"role": item["role"], "content": item["content"]}
        for item in (packet.get("recent_dialogue") or ())
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    rest = {key: value for key, value in packet.items() if key != "recent_dialogue"}
    user_payload = {
        **rest,
        "message": data.message,
    }
    return [
        {"role": "system", "content": system_prompt},
        *recent,
        {"role": "user", "content": dump(user_payload)},
    ]


async def route_to_chat_intent(context: ChatContext, route: TurnRoute, data, char, task_context, *,
                               client=None) -> ChatIntent:
    """Validate and lower a TurnRoute into a scoped ChatIntent for dispatcher/dialogue."""
    packet = task_context
    objects = packet.get("objects", {})
    candidates = [
        {
            "ref": ref,
            "index": idx,
            "kind": obj.kind,
            "label": obj.label,
            "identity": obj.identity,
        }
        for idx, (ref, obj) in enumerate(objects.items(), 1)
    ]
    route = _disambiguate_route(route, data.message, candidates)

    if route.route == "clarify" or route.action == "clarify":
        return ChatIntent(
            action="clarify",
            topic=route.question or route.topic or "你指的是哪一件事？",
            clarification_id=route.clarification_id,
        )

    if route.route == "chat" or (route.action == "chat" and route.route != "steer_task"):
        intent = ChatIntent(
            action="chat",
            topic=route.topic,
            reference=route.reference or route.task_id,
        )
        intent._clock_fields = tuple(calendar_fields(memory_query(data.message, char), route.clock_fields))
        intent._read_original = route.read_original
        intent._recall_reason = route.recall_reason
        return intent

    if route.route == "prepare_publication" or route.action == "prepare_publication":
        channel = route.publication_channel or outgoing_queue.named_channel(data.message)
        intent = ChatIntent(
            action="prepare_publication",
            publication_channel=channel,
            topic=route.topic,
            reference=route.reference,
            clarification_id=route.clarification_id,
        )
        if route.object_ref and route.object_ref in objects:
            intent._subject = objects[route.object_ref]
        return intent

    if route.route == "delegate_task" or route.action == "agent_task":
        arrangement = route.arrangement
        if (
            arrangement is not None
            and arrangement.kind == "once"
            and arrangement.local_date is None
            and arrangement.local_time is None
            and not re.search(r"预约|定时|明[天日]|后天|下周|周[一二三四五六日天]|星期|\d{1,2}\s*[:点时]", data.message)
        ):
            arrangement = None
        return ChatIntent(
            action="agent_task",
            topic=route.topic,
            brief=route.brief,
            arrangement=arrangement,
            clarification_id=route.clarification_id,
        )

    if route.route == "steer_task":
        cards = list({c["id"]: c for c in [*task_context.get("recent_task_cards", []), *task_context.get("history_task_cards", [])]}.values())
        target_id = route.task_id or route.reference
        if target_id is None and len(task_context.get("recent_task_cards", [])) == 1:
            target_id = task_context["recent_task_cards"][0]["id"]
        if route.task_effect in {"discuss", "keep_current"} or route.recall_reason:
            if target_id and not any(c["id"] == target_id for c in cards):
                return ChatIntent(action="clarify", topic="你指的是哪项交办？")
            if route.read_original and not any(c["id"] == target_id and c.get("original_edition") for c in cards):
                return ChatIntent(action="clarify", topic="能确认一下你指的是哪一版原稿吗？")
            if route.recall_reason and not target_id:
                return ChatIntent(action="clarify", topic="你想核对哪一份成果的理由？")
            intent = ChatIntent(action="chat", reference=target_id)
            intent._read_original = route.read_original
            intent._recall_reason = route.recall_reason
            return intent

        # Revise or cancel: preserve patch_vocabulary + grounded_patch + review_change guarantees
        card = next((c for c in task_context.get("recent_task_cards", []) if c["id"] == target_id), None)
        if card is None:
            return ChatIntent(action="clarify", topic="你想调整哪项交办？请说一下原任务的目标。")
        current_task = context.runtime.tasks.executable(card["id"])
        if not current_task or current_task["revision"] != card["revision"]:
            raise HTTPException(409, "任务或授权已变化，请重新查看任务卡。")
        clarification = task_context.get("clarification") or {}
        clarified = bool(route.clarification_id and str(route.clarification_id) == clarification.get("id"))
        evidence = (clarification["request"] + "\n" + data.message) if clarified else data.message
        patch_choices, bindings = patch_vocabulary([card], evidence)
        if not patch_choices and not clarification:
            return ChatIntent(action="chat", reference=None)
        requested_action = "cancel" if route.task_effect == "cancel" else "revise"
        if requested_action == "cancel" and "execution" not in card.get("editable", {}):
            return ChatIntent(action="clarify", topic="这次没有修改任务。请确认要调整哪项要求，以及要改成什么。")

        mentions_item = any(key.startswith("item:") for key in bindings)
        selected_patch = None
        if client is not None and mentions_item and patch_choices:
            action, selected_patch = await review_change(client, evidence, card, requested_action, patch_choices, bindings)
            if action == "new_work":
                return ChatIntent(action="agent_task", brief=route.brief, clarification_id=route.clarification_id)
            if action == "clarify":
                return ChatIntent(action="clarify", topic="请确认要改原交办的哪项要求，还是另做独立的一份。")
            if action == "discuss":
                return ChatIntent(action="chat", reference=target_id)
            if action == "recall_reason":
                intent = ChatIntent(action="chat", reference=target_id)
                intent._recall_reason = True
                return intent
            requested_action = action
        elif patch_choices and (quote := route.change_quote.strip()):
            allowed_fields = (lambda f: f in {"execution", "goal"}) if requested_action == "cancel" else (lambda f: f != "execution")
            for (field, subject), requirements in patch_choices.items():
                if not allowed_fields(field) or quote not in requirements:
                    continue
                candidate = TaskPatch(
                    field=field,
                    subject=subject,
                    requirement=quote,
                    replacement_goal=route.replacement_goal if field == "goal" else "",
                )
                if grounded_patch(candidate, card, evidence):
                    selected_patch = candidate
                    break
            if selected_patch is None:
                for (field, subject) in patch_choices:
                    if not allowed_fields(field):
                        continue
                    candidate = TaskPatch(
                        field=field,
                        subject=subject,
                        requirement=quote,
                        replacement_goal=route.replacement_goal if field == "goal" else "",
                    )
                    if grounded_patch(candidate, card, evidence):
                        selected_patch = candidate
                        break

        if (selected_patch is None or not grounded_patch(selected_patch, card, evidence)
                or (requested_action == "cancel" and (
                    selected_patch.field not in {"execution", "goal"} or "execution" not in card.get("editable", {})
                ))
                or (requested_action == "revise" and selected_patch.field == "execution")):
            return ChatIntent(action="clarify", topic="这次没有修改任务。请确认要调整哪项要求，以及要改成什么。")

        intent_action = "cancel_task" if requested_action == "cancel" else "steer_task"
        intent = ChatIntent(
            action=intent_action,
            reference=target_id,
            brief=route.brief,
            clarification_id=route.clarification_id,
        )
        intent._task_patch = selected_patch
        return intent

    # instant_read or direct_action
    action = route.action
    if action in _OWNER_UI_ONLY:
        raise HTTPException(422, "这项控制仅供号主在设置界面操作")
    if (
        action == "owner_binding"
        or (
            action == "identity_read"
            and len(route.identities) == 1
            and route.identities[0].subject == "bilibili_owner"
        )
    ) and _LIVE_PROFILE.search(data.message):
        action = "owner_profile"
    if action == "identity_read" and not (
        _OWNER_RELATION.search(data.message)
        or _OWNER_PLATFORM.search(data.message)
        or _CURRENT_ACCOUNT.search(data.message)
        or (task_context.get("identity_reference") and _OWNER_FIELDS.search(data.message))
    ):
        return ChatIntent(action="clarify", topic="你想问哪一个账号或绑定关系？")
    if action == "watch_totals":
        decision = ChatIntent(action=workshop_lookup.LOOKUP_TOOL)
        decision._lookup = WorkshopLookup(collection="watch_count", day=route.watch_day or "today")
        decision._fixed_metrics = True
        return decision
    if action == workshop_lookup.LOOKUP_TOOL:
        return _workshop_decision(route, task_context, data.message)
    if action == recall_memory.NAME:
        decision = ChatIntent(action=recall_memory.NAME, topic=route.topic)
        decision._recall = route.recall or (recall_memory.RecallQuery(topic=route.topic) if route.topic else None)
        if route.object_ref and (subject := objects.get(route.object_ref)):
            if subject.kind == "recalled_source":
                base = decision._recall or recall_memory.RecallQuery(topic=route.topic)
                decision._recall = base.model_copy(update={"source_id": subject.identity})
        elif decision._recall and not decision._recall.source_id:
            recalled = [obj for obj in objects.values() if obj.kind == "recalled_source"]
            if len(recalled) == 1 and re.search(r"刚才|那[条个次]|来源|哪里读到|哪读的", data.message):
                decision._recall = decision._recall.model_copy(update={"source_id": recalled[0].identity})
        return decision

    subject = objects.get(route.object_ref) if route.object_ref else None
    decision = ChatIntent(
        action=action,
        topic=route.topic,
        reference=route.reference,
        clarification_id=route.clarification_id,
        position=route.position,
        part=route.part,
        seconds=route.seconds,
        end_seconds=route.end_seconds,
        source=route.source,
        game=route.game,
        game_action=route.game_action,
        settings_group=route.settings_group,
        publication_channel=route.publication_channel,
        arrangement=route.arrangement,
        brief=route.brief,
        identities=route.identities,
    )
    decision._subject = subject
    decision._continue_research = route.continue_research
    if subject and subject.kind in {"task", "artifact", "video", "event"} and not decision.reference:
        decision.reference = subject.identity
        if subject.part and decision.part is None:
            decision.part = subject.part
    if subject and subject.kind == "outgoing" and subject.identity in outgoing_queue.CHANNELS and not decision.publication_channel:
        decision.publication_channel = subject.identity
    if action in _SETTING_ACTIONS:
        if subject and subject.kind == "setting" and action == "daily_settings" and decision.settings_group is None:
            decision.settings_group = subject.identity
        if decision.settings_group is None and action == "daily_settings" and (prev := task_context.get("latest_tool")):
            decision.settings_group = prev.get("group")
        if action == "pet_settings" and decision.settings_group is not None:
            raise HTTPException(400, "宠物操作不能附带其他设置分组")
        group = "pets" if action == "pet_settings" else decision.settings_group
        read_only = bool(route.settings_read_only or route.route == "instant_read")
        if group:
            setting_hint = workbench_tools.requested_setting(data.message, char, task_context.get("latest_tool"))
            if setting_hint and setting_hint[1] == group:
                read_only = setting_hint[2]
                decision._setting_fields = setting_hint[3]
            elif read_only and action == "daily_settings":
                named = workbench_tools.named_setting_fields(group, data.message)
                decision._setting_fields = named or tuple(dict.fromkeys(route.setting_fields))
            if not subject and not workbench_tools.setting_target(group, data.message, character=char, read_only=read_only):
                if not setting_hint:
                    return ChatIntent(action="chat")
        decision._settings_read_only = read_only
    return decision


async def route_chat_turn(context: ChatContext, char, account, request_id, data, task_context, *,
                          history=(), report=None, generation_slot=None, model_caller=None) -> ChatIntent:
    """Determine what the user turn means using the background router or offline fallback."""
    runtime = context.runtime
    if workbench_tools.requests_owner_control(data.message):
        return ChatIntent(action="clarify", topic=workbench_tools.OWNER_CONTROL_REPLY)
    cards = list({c["id"]: c for c in [*task_context.get("recent_task_cards", []), *task_context.get("history_task_cards", [])]}.values())
    chat_with_cards = (
        data.intent is not None
        and data.intent.action == "chat"
        and not data.temporary
        and bool(cards)
        and (routing_client(runtime) is not None or callable(getattr(runtime.llm, "generate_assistant_json", None)))
    )
    if data.intent is not None and not chat_with_cards:
        return data.intent
    if (not data.video_selection and _DIRECT_RELATION.search(memory_query(data.message, char))
            and not _OWNER_PLATFORM.search(data.message) and not workbench_tools.is_non_read_request(data.message)):
        return ChatContext.identity_reply(char)

    client = routing_client(runtime)
    packet = context.router_context_packet(char, account, data, task_context, history=history)
    objects = task_context.get("objects", {})
    candidates = packet.get("candidates", [])
    read_only = data.temporary or chat_with_cards

    if client is not None:
        tools = route_tools(
            runtime,
            cards=cards,
            objects=objects,
            candidates=candidates,
            read_only=read_only,
            has_clarification=bool(packet.get("clarification")),
        )
        messages = _build_router_messages(char, data, packet)
        started = time.monotonic()
        if report:
            report("routing")
        slot = getattr(runtime, "chat_routing_generation", nullcontext)
        try:
            async with slot(), asyncio.timeout(45):
                with without_model_identity():
                    reply = await client(
                        input=messages,
                        tools=tools,
                        tool_choice="required",
                        parallel_tool_calls=False,
                        max_output_tokens=640,
                        store=False,
                        thinking=True,
                        thinking_budget_tokens=160,
                        temperature=0.0,
                    )
        except TimeoutError as error:
            raise HTTPException(503, "后台路由超时，可以重试。") from error
        except Exception as error:
            logger.warning("turn_router error=%s request_id=%s", type(error).__name__, request_id)
            raise HTTPException(503, "后台路由暂时不可用，可以重试。") from error
        route_ms = (time.monotonic() - started) * 1000
        if not getattr(reply, "calls", None) or len(reply.calls) != 1:
            raise HTTPException(503, "后台路由未返回单个明确工具决策。")
        call, = reply.calls
        if call.name not in {item["name"] for item in tools}:
            raise HTTPException(503, "后台路由选择了未知工具。")
        route = _route_from_tool_call(call.name, call.arguments)
        usage = (getattr(reply, "raw", None) or {}).get("usage") or {}
        details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
        log_fn = logger.warning if route_ms > 3000 else logger.info
        log_fn(
            "turn_router route=%s action=%s route_ms=%.0f input_tokens=%s output_tokens=%s reasoning_tokens=%s request_id=%s",
            route.route,
            route.action,
            route_ms,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            details.get("reasoning_tokens"),
            request_id,
        )
        return await route_to_chat_intent(context, route, data, char, task_context, client=client)

    provider = runtime.llm
    if callable(getattr(provider, "generate_assistant_json", None)):
        schema = TurnRoute.model_json_schema()
        messages = _build_router_messages(char, data, packet)
        slot = generation_slot() if generation_slot else runtime.operator_generation()
        try:
            async with slot:
                context.check_scope(char, account)
                value = await provider.generate_assistant_json(
                    messages, schema, max_tokens=1024, on_content=None, on_stage=report,
                )
            route = TurnRoute.model_validate(value)
        except (ValueError, TimeoutError, httpx.HTTPError) as error:
            raise HTTPException(503, "这轮回复未完成，可以重试。") from error
        if chat_with_cards and route.route == "steer_task" and route.task_effect not in {"discuss", "keep_current"} and not route.recall_reason:
            raise HTTPException(503, "明确的私聊只可引用任务")
        intent = await route_to_chat_intent(context, route, data, char, task_context)
        if chat_with_cards and intent.action != "chat":
            raise HTTPException(503, "明确的私聊只可引用任务")
        return intent

    # Legacy OfflineChat path (generate_json + generate only)
    if not data.video_selection:
        if followup := context.source_followup(data.message, char, account):
            return followup
    if bound := context.bound_intent(data, char, task_context):
        return bound
    if model_caller is None:
        return ChatIntent(action="chat")
    schema = ChatIntent.model_json_schema()
    schema["properties"]["action"]["enum"] = [a for a in schema["properties"]["action"]["enum"] if a not in _OWNER_UI_ONLY]
    intent = await model_caller(
        "识别当前消息的明确查询或任务。普通对话选 chat。"
        "查询事件原话、作者或回执时，用 position 填已显示序号，或在 topic 填明确标题和原话。"
        "有明确分 P 或时码时填 part、seconds、end_seconds。引用用本轮原话和已找到的任务 ID。"
        + _WORKBENCH_TOOL_CONTEXT + _LIVE_TOOL_CONTEXT + _TASK_ROUTING,
        {"message": data.message, **{key: value for key, value in task_context.items() if key != "objects"}},
        ChatIntent,
        schema=schema,
    )
    if intent.action in _OWNER_UI_ONLY:
        raise HTTPException(422, "这项控制仅供号主在设置界面操作")
    return intent


async def route_voice_utterance(chat, words: str, tasks: list[dict]) -> VoiceRoute:
    """Classify a signed voice utterance via unified background router."""
    packet = {"utterance": words, "call_tasks": tasks}
    runtime = chat.runtime
    client = routing_client(runtime)
    if client is None:
        return await chat._model(_VOICE_ROUTING_PROMPT, packet, VoiceRoute, max_tokens=192)
    quote = {"type": "string", "minLength": 2, "maxLength": 2000}

    def voice_tool(name, description, properties):
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        return function(name, description, schema)

    menu = [
        voice_tool("conversation", "继续当前语音对话。", {}),
        voice_tool("delegate", "号主明确交办新工作并说明目标。", {"quote": quote}),
    ]
    if tasks:
        target = {"quote": quote, "task_id": {"type": "string", "enum": [item["id"] for item in tasks]}}
        menu.extend([
            voice_tool("revise", "号主明确修改指定交办的要求。", target),
            voice_tool("cancel", "号主明确停止指定交办。", target),
        ])
    slot = getattr(runtime, "chat_routing_generation", None)
    slot_ctx = slot() if callable(slot) else chat._generation_slot()
    async with slot_ctx:
        with without_model_identity():
            reply = await client(
                input=[
                    {"role": "system", "content": _VOICE_ROUTING_PROMPT},
                    {"role": "user", "content": dump(packet)},
                ],
                tools=menu,
                tool_choice="required",
                parallel_tool_calls=False,
                max_output_tokens=256,
                store=False,
                thinking=False,
                temperature=0.0,
            )
    if len(reply.calls) != 1:
        raise ValueError("expected one voice route")
    call, = reply.calls
    if call.name not in {item["name"] for item in menu}:
        raise ValueError("unknown voice route")
    return VoiceRoute.model_validate(call.arguments | {"action": call.name})
