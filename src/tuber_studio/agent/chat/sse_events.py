"""Structured SSE payload helpers and calm UX constraint mapping for chat."""

from __future__ import annotations

import re

_TOOL_NAME_MAP = {
    "recall_memory": "recall_history",
    "web_lookup": "web_search",
    "web_search": "web_search",
    "inspect_video_frame": "inspect_video",
    "full_video": "inspect_video",
    "video_details": "inspect_video",
    "video_activity": "inspect_video",
    "resume_video": "inspect_video",
    "remember_event": "save_memory",
    "live_preference": "save_memory",
    "save_memory": "save_memory",
    "update_memory": "update_memory",
    "forget_memory": "delete_memory",
    "delete_memory": "delete_memory",
    "workshop_lookup": "lookup_records",
    "watch_totals": "watch_totals",
    "recall_experience": "recall_history",
}

_TOOL_LABEL_MAP = {
    "web_search": "联网检索",
    "web_lookup": "联网检索",
    "recall_history": "回溯记忆与记录",
    "recall_memory": "回溯记忆与记录",
    "recall_experience": "回溯公开经历",
    "inspect_video": "查看视频与片段",
    "inspect_video_frame": "提取视频画面",
    "full_video": "读取视频字幕与片段",
    "video_details": "查看视频信息",
    "video_activity": "查看视频进度",
    "resume_video": "继续视频任务",
    "save_memory": "更新长期记忆",
    "remember_event": "保存事件记忆",
    "live_preference": "保存直播习惯",
    "update_memory": "更新长期记忆",
    "delete_memory": "遗忘记忆",
    "forget_memory": "遗忘记忆",
    "memory_summary": "整理记忆清单",
    "lookup_records": "查阅历史记录",
    "workshop_lookup": "查阅历史记录",
    "watch_totals": "统计观看记录",
    "owner_profile": "查看 B 站主人主页",
    "owner_binding": "核对 B 站绑定信息",
    "operating_account": "核对运营账号",
    "identity_read": "核对账号信息",
    "operation_status": "查看自主运营状态",
    "start_operations": "开启自主运营",
    "operation_control": "管理自主运营权限",
    "live_status": "查看直播状态",
    "start_live": "准备直播会话",
    "stop": "停止进行中的操作",
    "daily_settings": "调整日常设置",
    "pet_settings": "切换桌面形象",
    "workbench_capabilities": "查看可用能力",
    "meme_gallery_status": "查看图库待审箱与订阅源状态",
    "agent_task": "派发后台任务",
    "steer_task": "调整后台任务",
    "cancel_task": "取消后台任务",
    "pause_task": "暂停后台任务",
    "resume_task": "恢复后台任务",
    "watch_goal": "跟进预约任务",
    "outgoing_queue": "查看待发队列",
    "prepare_publication": "整理待发内容",
}

_INTERNAL_GUARD_PATTERNS = (
    (re.compile(r"并行委派只允许独立读取|并发委派只允许"), "这几步不能同时改动状态，已经按顺序排好处理，这一轮先暂缓，需要的话可以换个方式再试试。"),
    (re.compile(r"Responses 未返回|完整、唯一的函数调用"), "这轮操作指令没有完整解析出来，稍后再试一次或换个说法就好。"),
    (re.compile(r"同一批次存在重复委派"), "刚才重复发出了同一项操作请求，已拦截避免重复执行，可以再试一次。"),
    (re.compile(r"超出本轮委派范围|本轮委派范围"), "这一步试着调用了当前任务未开放的能力，先暂缓处理。"),
    (re.compile(r"没有按本轮委派提交实际材料|提交实际材料"), "整理阶段还没拿到可交付的有效结果，先停在这里，需要的话可以补充说明后再继续。"),
    (re.compile(r"补读短窗超出当前视频读取授权"), "这段补读超出了刚才指定的视频范围，需要继续的话请再指定具体片段。"),
    (re.compile(r"上下文容量|窗口预算|ContextWindowExceeded"), "这次要处理的内容偏多，超出了当前单轮上下文容量，可以拆成小段后再试。"),
    (re.compile(r"自主运营正在启动"), "自主运营正在检查启动条件，请稍等状态同步完成。"),
)


def canonical_tool_name(action: str) -> str:
    """Map an internal chat action to a clean tool identifier for SSE capsules."""
    return _TOOL_NAME_MAP.get(action, action or "tool")


def tool_label(action: str) -> str:
    """Return a natural Chinese label for an action or canonical tool name."""
    if action in _TOOL_LABEL_MAP:
        return _TOOL_LABEL_MAP[action]
    canonical = canonical_tool_name(action)
    return _TOOL_LABEL_MAP.get(canonical, action or "执行工具")


def calm_constraint_message(error: Exception | str) -> str:
    """Translate internal delegation/tool guard strings into calm owner-facing text."""
    text = str(error or "").strip()
    if not text:
        return "这一步没能按预期完成，请稍后重试或补充说明。"
    if "号主" in text or "工坊" in text:
        text = (
            text.replace("工坊号主", "对方")
            .replace("绑定号主", "绑定主人")
            .replace("B 站号主", "B 站主人")
            .replace("号主给定的来源", "刚才指定的来源")
            .replace("号主", "对方")
            .replace("工坊", "当前环境")
        )
    for pattern, calm in _INTERNAL_GUARD_PATTERNS:
        if pattern.search(text):
            return calm
    if text.startswith("Traceback") or "ValidationError" in text or "pydantic" in text.lower():
        return "操作参数没能匹配当前要求，可以调整说法后再试。"
    return text


def tool_activity_payload(
    activity_id: str,
    action: str,
    *,
    status: str = "running",
    summary: str = "",
    detail: str = "",
    data: dict | None = None,
) -> dict:
    """Build a structured `tool_activity` SSE payload."""
    payload = {
        "id": str(activity_id),
        "tool": canonical_tool_name(action),
        "action": action,
        "label": tool_label(action),
        "status": status,
    }
    if summary:
        payload["summary"] = summary
    if detail:
        payload["detail"] = detail
    if data:
        payload["data"] = data
    return payload


def memory_updated_payload(receipt: dict | None) -> dict | None:
    """Format a normalized L2 memory receipt into a `memory_updated` SSE event."""
    if not isinstance(receipt, dict) or not receipt.get("ok"):
        return None
    action = str(receipt.get("action") or "").strip()
    if action not in {"created", "updated", "deleted", "deduplicated"}:
        return None
    item = receipt.get("item") if isinstance(receipt.get("item"), dict) else {}
    snippet = str(receipt.get("snippet") or item.get("content") or "").strip()
    if not snippet:
        return None
    return {
        "action": action,
        "scope": str(receipt.get("scope") or item.get("scope") or "owner"),
        "character_id": str(receipt.get("character_id") or item.get("character_id") or ""),
        "memory_id": receipt.get("memory_id") or item.get("id"),
        "snippet": snippet,
        "count": int(receipt.get("count") or 0),
        "limit": int(receipt.get("limit") or 100),
        "soft_limit_reached": bool(receipt.get("soft_limit_reached")),
    }


def emit_tool_activity(
    emit,
    activity_id: str,
    action: str,
    *,
    status: str = "running",
    summary: str = "",
    detail: str = "",
    data: dict | None = None,
    collector: list[dict] | None = None,
) -> dict:
    """Emit a `tool_activity` SSE event when streaming and optionally record it."""
    payload = tool_activity_payload(
        activity_id, action, status=status, summary=summary, detail=detail, data=data
    )
    if collector is not None:
        collector.append(payload)
    if callable(emit):
        emit("tool_activity", payload)
    return payload


def emit_memory_updated(emit, receipt: dict | None, *, collector: list[dict] | None = None) -> dict | None:
    """Emit a `memory_updated` SSE event for owner memory changes."""
    payload = memory_updated_payload(receipt)
    if payload is None:
        return None
    if collector is not None:
        collector.append(payload)
    if callable(emit):
        emit("memory_updated", payload)
    return payload
