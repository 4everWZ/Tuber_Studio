"""Private task delivery built only from persisted tool receipts."""

import json
import re
from urllib.parse import urlsplit

LABELS = {
    "web_search": "检索候选来源", "web_page": "读取网页", "bilibili_owner": "B 站号主",
    "operation_status": "自主运营", "today_activity": "当日运营事件", "inspect_video": "视频取材",
    "live_status": "直播状态", "live_handoff": "节目交接", "game_status": "模拟游戏", "game_action": "模拟游戏动作",
}
OPERATIONS = {
    "off": "未启动", "starting": "正在启动", "running": "运行中", "blocked": "有阻塞",
    "emergency": "紧急停止", "stopping": "正在停止", "draining": "正在收尾",
}
PLATFORM = {"stopped": "已停止", "live": "直播中", "paused": "已暂停"}
PROGRAM = {"idle": "空闲", "starting": "正在启动", "publishing": "正在发布", "stopping": "正在停止"}
VIEWING = {"watched": "取得字幕及画面", "subtitles_only": "仅取得字幕", "visual_only": "仅取得画面", "partial": "部分取材"}
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>#!|~])")


def _text(value):
    return _MARKDOWN.sub(r"\\\1", " ".join(value.split())) if isinstance(value, str) else ""


def _source(value):
    if not isinstance(value, str) or re.search(r"[\s\\<>\x00-\x1f]", value):
        return ""
    try:
        url = urlsplit(value)
    except ValueError:
        return ""
    return f"<{value}>" if url.scheme == "https" and url.hostname and url.username is None else ""


def _materials(items):
    materials = []
    for item in items or []:
        if not isinstance(item, dict) or not (text := _text(item.get("text") or item.get("title"))):
            continue
        label = _text(item.get("kind"))
        if type(item.get("part")) is int and type(item.get("seconds")) in {int, float}:
            label = f"分 P {item['part']} · {item['seconds']} 秒" + (f" · {label}" if label else "")
        materials.append(" · ".join(part for part in (label, text, _source(item.get("url"))) if part))
    return materials


def _describe(tool, result):
    if tool == "operation_status":
        value = OPERATIONS.get(result.get("state"))
        return [value or "运营状态暂不可查。"], bool(value)
    if tool == "live_status":
        platform = PLATFORM.get(result.get("state"))
        native = result.get("native_program")
        lines = [f"平台直播{platform or '状态暂不可查'}。"]
        program = PROGRAM.get(native.get("state")) if isinstance(native, dict) else None
        if native is not None:
            lines.append(f"Omni 节目{program or '状态暂不可查'}。")
        return lines, bool(platform) and (native is None or bool(program))
    if tool == "web_search":
        sources = [source for link in result.get("links", []) if (source := _source(link))]
        return (["候选链接，待读取正文核验。", *sources] if sources else ["未找到候选来源。"]), bool(sources)
    if tool == "web_page":
        origin, excerpt = _source(result.get("url")), _text(result.get("excerpt"))
        coverage = "来源摘录，内容有截断。" if result.get("truncated") else "来源摘录。"
        return [coverage, _text(result.get("title")), origin, excerpt], bool(origin and excerpt)
    if tool == "bilibili_owner":
        name, uid, origin = _text(result.get("name")), _text(result.get("uid")), _source(result.get("url"))
        return [f"{name}（UID {uid}）", origin] if name and uid and origin else ["缺少已核验的号主资料。"], bool(name and uid and origin)
    if tool == "today_activity":
        date = _text(result.get("date"))
        events = [item | {"url": (item.get("source") or {}).get("href")} for item in result.get("events") or []
                  if isinstance(item, dict)]
        return [date, *(_materials(events) or ["当前记录没有可列出的运营事件。"])], bool(date)
    if tool == "inspect_video":
        done = result.get("status") == "completed"
        details = _materials(result.get("findings", []))
        note = "已取得抽样片段。" if done and details else "没有足以交付的取材片段。"
        viewing = result.get("viewing")
        coverage = VIEWING.get(viewing, _text(viewing)) if isinstance(viewing, str) else ""
        return [note, coverage, _source(result.get("url")), *details], bool(done and details)
    if tool == "live_handoff":
        spoken = _text(result.get("spoken"))
        done = result.get("status") == "completed" and bool(spoken)
        return ["播放器确认播完。", f"台词为「{spoken}」"] if done else ["未收到播放器的播完确认。"], done
    if tool == "game_status":
        summary = _text((result.get("state") or {}).get("summary"))
        return [summary or "未取得模拟局面。"], result.get("simulated") is True and bool(summary)
    if tool == "game_action":
        done = result.get("success") is True
        return ["模拟游戏确认动作已执行。" if done else "模拟游戏未确认执行。", _text(result.get("state"))], done
    failed = result.get("status") in {"cancelled", "failed", "blocked", "partial"} or result.get("success") is False
    return ["工具未确认完成。" if failed else "已保存工具回执。"], bool(result) and not failed


def deliver_from_receipts(steps, revision, *, required_tools=(), reviewed_tools=()):
    results = {}
    for step in steps:
        if step["revision"] != revision or not isinstance(step.get("result"), dict):
            continue
        tool = step["tool"]
        key = step["id"] if tool in reviewed_tools else (tool, json.dumps(step.get("arguments", {}), sort_keys=True))
        results[key] = step

    blocks, gaps, observed = [], [], set()
    for step in results.values():
        tool, result = step["tool"], step["result"]
        label = LABELS.get(tool, _text(tool))
        error = _text(result.get("error"))
        lines, verified = ([error], False) if error else _describe(tool, result)
        blocks.append("\n".join([f"**{label}**", *(f"- {line}" for line in lines if line)]))
        if verified:
            observed.add(tool)
        elif tool != "web_search" or error:
            gaps.append(label)
    missing = set(required_tools) - observed
    gaps.extend(LABELS.get(tool, tool) for tool in sorted(missing))
    if not (observed - {"web_search"}):
        blocks.insert(0, "本轮未取得可交付的已核验工具结果。")
    if gaps:
        blocks.insert(0, "待核对的步骤为" + "、".join(dict.fromkeys(gaps)) + "。")
    return bool(observed - {"web_search"}) and not gaps, "\n\n".join(blocks)
