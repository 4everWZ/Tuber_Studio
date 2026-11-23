"""Scope checks, clarification restoration, memory mutations and DeliveryPacket assembly."""

import json
import time

from fastapi import HTTPException

from ...memory_organizer import MemorySuggestion
from ..context import ChatContext
from ..lookup import operator_facts
from ..lookup.handoff import LIVE_STYLE, LIVE_STYLE_GROUP
from ..models import (
    _CONTROL_ACTIONS,
    _DIRECT_CHANGES,
    _EVENT_ACTIONS,
    _MENTIONED_BV,
    _TASK_REFERENCE_ACTIONS,
    DeliveryPacket,
    LivePreferenceSelection,
    VideoSelection,
)


def prepare_dispatch_inputs(ctx: ChatContext, intent, char, request_id, data, events, tasks, focus, task_context):
    """Normalize references, clarification chains and video selection bounds before dispatch."""
    kind, topic, reference = intent.action, intent.topic, intent.reference
    if intent.position is not None and kind in _EVENT_ACTIONS:
        reference = next((key for key, (index, _) in events.items() if index == intent.position), "")
    elif kind in _EVENT_ACTIONS and reference is None and topic:
        matching = [
            key for key, (_, event) in events.items()
            if topic in event["title"] or topic in event["source"]["text"]
        ]
        if not matching:
            for index, event in enumerate(operator_facts.recent_events(ctx.browsing)["items"]):
                if topic in event["title"] or topic in event["source"]["text"]:
                    key = f"{request_id}/source-{index + 1}"
                    events[key] = (0, event | {"reference": key})
                    matching.append(key)
        if matching:
            reference = matching[0] if len(matching) == 1 else ""
    if kind in _TASK_REFERENCE_ACTIONS and kind != "stop" and reference is None:
        videos = set(_MENTIONED_BV.findall(data.message))
        matching = (
            [key for key, task in tasks.items() if task["bvid"] in videos]
            if videos
            else ([key for key, task in tasks.items() if task.get("focused")] or list(tasks))
        )
        if len(matching) == 1:
            reference = matching[0]
    selection = data.video_selection
    request = data.message
    if intent.clarification_id:
        prior = task_context.get("clarification", {})
        if prior.get("id") != str(intent.clarification_id):
            raise HTTPException(409, "引用的澄清已变化，请重新说明要求。")
        request = prior["request"] + "\n号主补充：" + request
        if selection is None and kind in {"agent_task", "steer_task", "clarify"}:
            saved = json.loads(ctx.turn(prior["id"])["result"])
            if original := saved.get("clarification_video_selection") or saved.get("submission", {}).get("video_selection"):
                selection = VideoSelection.model_validate(original)
    if selection and kind not in {"full_video", "agent_task", "steer_task", "clarify"}:
        raise HTTPException(400, "视频范围只能用于指定视频任务")
    clarification = {"clarification_request": request}
    if selection:
        clarification["clarification_video_selection"] = selection.model_dump(mode="json")
    return kind, topic, reference, selection, request, clarification


async def save_live_preference(dispatcher, char, request_id, request):
    ctx = dispatcher.ctx
    memory = ctx.runtime.daily_memory
    scope = ctx.runtime.memory_organizer.scope()
    if not memory.preference(scope)["enabled"]:
        return {"reply_text": "记忆已关闭，可在个性化设置启用。", "card": None}
    result = await dispatcher._model(
        "识别号主明确要求的长期直播表达规则。先讲结果选 result_first，先讲过程选 process_first，"
        "要求简洁选 concise，需要展开选 detailed。一次性要求或无法对应时填 null。",
        request,
        LivePreferenceSelection,
        max_tokens=100,
    )
    if not result.preference:
        return {"reply_text": "你希望直播先讲结果还是过程，或要调整详略？", "card": None}
    text = LIVE_STYLE[result.preference]
    ctx.check_scope(char, scope[0])
    if not memory.preference(scope)["enabled"]:
        return {"reply_text": "记忆已关闭。", "card": None}
    group = LIVE_STYLE_GROUP[result.preference]
    existing = next((item for item in memory.live_preferences(scope) if item["proof"].get("style_group") == group), None)
    source = {"key": "turn:" + request_id, "kind": "private", "text": request, "at": time.time(), "scene": "live", "style_group": group}
    decision = MemorySuggestion(action="add", existing_id=None, headline="直播偏好 · " + text[:30], note=text, tags=["直播"])
    with ctx.store.db:
        saved = memory.save_organized(scope, source, decision, existing)
        ctx.store.db.execute(
            "INSERT OR IGNORE INTO memory_organization VALUES(?,?,?,?,?,?,?)",
            (*scope, source["key"], request_id, "saved" if saved else "skipped", time.time()),
        )
    return {
        "reply_text": "已记下直播偏好。" if saved else "这条偏好保持原设置。",
        "card": {"type": "memory_change", "data": {"text": text, "target": "长期直播偏好"}} if saved else None,
    }


def save_event_memory(dispatcher, event, request_id):
    ctx = dispatcher.ctx
    memory = ctx.runtime.daily_memory
    scope = ctx.runtime.memory_organizer.scope()
    if not memory.preference(scope)["enabled"]:
        return {"reply_text": "记忆已关闭，可在个性化设置启用。", "card": None}
    ref = event["source"]
    source_kind = ref.get("kind")
    if (source_kind not in {"comment", "subtitle"} or not ref.get("text") or not ref.get("href")
            or source_kind == "comment" and str(ref.get("uid") or "") == scope[0]):
        return {"reply_text": "这件事缺少可核对的原始字幕或评论。", "card": None}
    anchor = event.get("thread")
    key = f"thread:{anchor['resource']}:{anchor['root_id']}" if anchor else "event:" + ref["href"]
    note = ref["text"][:150].strip()
    source = {
        "key": key,
        "kind": "experience",
        "source_kind": source_kind,
        "text": ref["text"],
        "at": event.get("at") or time.time(),
        "anchor": anchor,
        "references": [{"href": ref["href"], "text": ref["text"], "kind": source_kind, "author_uid": ref.get("uid"), "author_name": ref.get("author")}],
    }
    decision = MemorySuggestion(action="add", existing_id=None, headline=event["title"][:40], note=note, tags=[note[:24]])
    with ctx.store.db:
        saved = memory.save_organized(scope, source, decision, None)
        ctx.runtime.unified_memory.add_memory(note, scope="owner", character_id=ctx.runtime.character["id"], source="operator_chat", tags=[note[:24]])
        ctx.store.db.execute(
            "INSERT OR IGNORE INTO memory_organization VALUES(?,?,?,?,?,?,?)",
            (*scope, "turn:" + request_id, request_id, "skipped", time.time()),
        )
    return {
        "reply_text": "已记下这件事。" if saved else "这条记忆保持原设置。",
        "card": {"type": "memory_change", "data": {"text": note, "target": "已保存记忆"}} if saved else None,
    }


def forget_memory(dispatcher, topic):
    unified = dispatcher.ctx.runtime.unified_memory
    char_id = dispatcher.ctx.runtime.character["id"]
    memory = dispatcher.ctx.runtime.daily_memory
    scope = dispatcher.ctx.runtime.memory_organizer.scope()
    rows = {row["id"]: row for row in memory.organized(scope)}
    rows.update((row["id"], row) for row in memory.live_preferences(scope))
    rows = rows.values()
    exact = [row for row in rows if topic in (row["headline"], row["anecdote"])]
    matches = exact or [
        row
        for row in rows
        if topic
        and (
            memory.memory_match(row, topic) >= 3
            or (
                row["proof"].get("style_group") == "order"
                and "先讲" in topic
                and (
                    ("结果" in topic and row["anecdote"].startswith("先讲结果"))
                    or ("过程" in topic and row["anecdote"].startswith("先讲过程"))
                )
            )
        )
        and ("直播" not in topic or row["proof"].get("scene") == "live")
    ]
    removed_unified = unified.delete_matching(topic, scope="owner", character_id=char_id)
    if len(matches) != 1:
        if removed_unified:
            return {"reply_text": "已删除这条记忆。", "card": None}
        return {"reply_text": "请说一下要忘掉的那条记忆的具体内容。", "card": None}
    memory.change_organized(scope, matches[0]["id"], delete=True)
    return {"reply_text": "已删除这条记忆。", "card": None}


def build_delivery_packet(intent, result) -> DeliveryPacket:
    """Wrap a verified execution result into a standardized DeliveryPacket."""
    card = result.get("card") or {}
    card_type = card.get("type")
    action = intent.action if intent else ""
    if card_type == "agent_task" and action == "prepare_publication":
        kind = "prepare_publication"
    elif action in {"steer_task", "cancel_task"}:
        kind = "task_steered"
    elif card_type == "agent_task":
        kind = "task_accepted"
    elif result.get("clarification_request") or action == "clarify":
        kind = "clarify"
    elif action in (
        _DIRECT_CHANGES
        | _CONTROL_ACTIONS
        | {
            "dynamic", "edit_dynamic", "browse", "full_video", "inspect_video_frame", "stop", "resume_video",
            "live_handoff", "live_preference", "forget_memory", "remember_event", "game_handoff",
            "pause_task", "resume_task", "watch_goal",
        }
    ):
        kind = "direct_action"
    else:
        kind = "instant_read"
    return DeliveryPacket(
        kind=kind,
        action=action,
        summary=result.get("reply_text", ""),
        facts=result.get("tool_receipt") or {},
        sources=result.get("sources", []),
        card=result.get("card"),
        tool_receipt=result.get("tool_receipt"),
        memory_dependent=bool(result.get("organized_memory") or result.get("memory") or result.get("task_reference")),
        clarification={"request": result["clarification_request"]} if "clarification_request" in result else None,
        question=result.get("clarification_request", ""),
    )
