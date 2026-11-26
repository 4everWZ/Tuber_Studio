"""Router tool schemas, compact arrangement JSON schema and direct action helpers."""

import json

from ...memory_organizer import recall as recall_memory
from ...tasks.delegation import function
from ..lookup import workbench_tools, workshop_lookup
from ..lookup.operator_task_patch import TaskPatch
from ..models import (
    _DIRECT_CHANGES,
    _DIRECT_READS,
    _EVENT_ACTIONS,
    _OWNER_UI_ONLY,
    _ROUTE_CHANGE_ACTIONS,
    _ROUTE_READ_ACTIONS,
    _TASK_CONTROLS,
    chat_model_provider,
)

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
    "meme_gallery_status",
)

_COMPACT_ARRANGEMENT_SCHEMA = {
    "type": "object",
    "description": "仅在对方明确预约未来日期时间（必须填 local_date 或 local_time）或周期/来信事件触发时填写；立即执行的普通任务不要填此项。",
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

_OPERATION_SCHEDULE_SCHEMA = {
    "type": "object",
    "description": "当 action=schedule_operations 时填写，用于设置按时长开启、单次时间段、每日固定时间段自主运营，或列出/取消已有定时计划。",
    "properties": {
        "operation": {"type": "string", "enum": ["create", "list", "cancel"]},
        "kind": {"type": "string", "enum": ["duration", "once_window", "daily_window"]},
        "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
        "local_date": {"type": "string", "maxLength": 10},
        "start_time": {"type": "string", "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$"},
        "end_time": {"type": "string", "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$"},
        "weekdays": {"type": "array", "maxItems": 7, "items": {"type": "integer", "minimum": 0, "maximum": 6}},
        "scope_discovery": {"type": "boolean"},
        "scope_inbox": {"type": "boolean"},
        "label": {"type": "string", "maxLength": 120},
        "schedule_id": {"type": "string", "maxLength": 64},
    },
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
    "判断当前这句来话是否明确交给后台处理一项工作。只作分流。"
    "原话里的「我」是说话人，「你」是助手。由对方明确交给你去做时才交办。"
    "对方说自己要去做、把结果发给听者、或者复述他人的吩咐，属于 conversation。"
    "普通接话、事实问答、讨论、转述、否定和假设归 conversation。"
    "要求接下来查找并交付资料、制作成果或稍后执行具体工作，可以交办。"
    "新交办选 delegate，目标和交付动作都须明确。改口选 revise，停止选 cancel，这两类都须指向本次通话里的唯一任务。"
    "quote 原样摘录能确定受托方和工作目标的连续原话。任务列表仅用于识别改口和取消。"
)


def routing_client(runtime):
    """Return the background Responses client when dual-brain routing is available."""
    provider = chat_model_provider(runtime)
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
            "meme_gallery_status",
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
                "description": "仅当对方明确提及或讨论某项已有任务或成果时填 task_id；普通闲聊不要填此项。",
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
                "只要对方要求留下持久产物（留份便笺/交接记录/笔记/总结/报告/提纲）、稍后交付（等我回来再看）、交给后台处理或定时预约，就必须选此项——即使句中同时提到核实直播/运营状态也选 delegate_task 而非 instant_read。否定措辞（先别/别接新活）禁止选此项。",
                with_clarification({
                    "goal": {"type": "string", "minLength": 1, "maxLength": 400},
                    "deliverable": {"type": "string", "minLength": 1, "maxLength": 400},
                    "profile": {
                        "type": "string",
                        "enum": ["meme_harvest", "video_production", "media_inspection", "general_research"],
                        "description": "后台窄工具集分组：meme_harvest=图库同步与二创筛选；video_production=制片/Remotion渲染/投稿；media_inspection=视频分P抽帧核查；general_research=公开检索与状态调研。",
                    },
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
                "owner_profile=去B站联网核验/去看绑定主人的最新B站主页资料；"
                "owner_binding=查本地记录的绑定主人B站主页链接和UID（不联网，用于问主人/他的主页、链接、UID）；"
                "operating_account=查当前登录的B站运营账号昵称和UID；"
                "identity_read=同时查绑定主人与运营账号（填 identity_subject=bilibili_owner|operating_account|both）；"
                "meme_gallery_status=当场查询图库待审箱新图数量与订阅源同步状态，只读不触发同步；"
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
            "memory_content": {"type": "string", "maxLength": 1000},
            "memory_tags": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 40}},
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
            "operation_schedule": _OPERATION_SCHEDULE_SCHEMA,
        }
        if cards:
            direct_props["arrangement"] = _COMPACT_ARRANGEMENT_SCHEMA
        menu.append(
            tool(
                "direct_action",
                "单轮立即修改设置、长期记忆或执行控制（仅当明确要求开启自主运营 start_operations、设置/查询/取消定时或时间段自主运营 schedule_operations、改变设置、切换宠物表情、保存/更新/忘记记忆 save_memory|update_memory|delete_memory|forget_memory、排队看完整视频或抽帧、暂停/恢复任务或停止 stop 时使用；纯查询或假设禁止选此项）。",
                with_clarification(direct_props),
                required=("action",),
            )
        )

        if cards:
            menu.append(
                tool(
                    "steer_task",
                    "对方对已有任务卡提出改口、换重点、补充要求或取消执行时使用；与已有任务无关的日常闲聊必须选 chat 而非 steer_task。"
                    "task_effect=revise/cancel 时必须填 change_quote，逐字原样摘录对方原话中表达新要求或停止的那一段，禁止改写。",
                    with_clarification({
                        "task_id": {"type": "string", "enum": task_enum},
                        "task_effect": {"type": "string", "enum": ["revise", "cancel", "keep_current", "discuss"]},
                        "change_quote": {
                            "type": "string",
                            "maxLength": 400,
                            "description": "逐字原样复制对方消息里表示具体改动或停止的子句（必须原样出现在 message 中，不得改写）。",
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
                "对方要求把已有待发动态、评论回复或私信整理送审/确认发布时必须选此项（操作待发草稿队列，不要误用前文视频或观看历史候选，也不要选 clarify）。",
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
            "仅当 candidates 中存在多个同类对象（如多条视频）且对方只用“那条/那个”等单数模糊指代未给序号/标题/BV，或缺少关键目标无法执行时使用。若对方换了新话题（闲聊/问当前日期时间/问别的事），严禁选 clarify 或重复上轮澄清。",
            with_clarification({
                "question": {"type": "string", "minLength": 1, "maxLength": 200},
            }),
            required=("question",),
        )
    )
    return menu
