"""Turn routing classifiers for text chat turns and signed voice utterances."""

import asyncio
import time
from contextlib import nullcontext

import httpx
from fastapi import HTTPException

from ....core.identity import without_model_identity
from ....core.store import dump
from ...tasks.delegation import function
from ..context import ChatContext
from ..lookup import workbench_tools
from ..models import (
    _DIRECT_RELATION,
    _LIVE_TOOL_CONTEXT,
    _OWNER_PLATFORM,
    _OWNER_UI_ONLY,
    _TASK_ROUTING,
    _WORKBENCH_TOOL_CONTEXT,
    ChatIntent,
    TurnRoute,
    VoiceRoute,
    chat_model_provider,
    memory_query,
)
from .intent_mapper import _route_from_tool_call, route_to_chat_intent
from .prompt_builder import _build_router_messages, logger
from .schemas import _VOICE_ROUTING_PROMPT, route_tools, routing_client


async def route_chat_turn(context: ChatContext, char, account, request_id, data, task_context, *,
                          history=(), report=None, generation_slot=None, model_caller=None) -> ChatIntent:
    """Determine what the user turn means using the background router or offline fallback."""
    runtime = context.runtime
    cards = list({c["id"]: c for c in [*task_context.get("recent_task_cards", []), *task_context.get("history_task_cards", [])]}.values())
    chat_with_cards = (
        data.intent is not None
        and data.intent.action == "chat"
        and not data.temporary
        and bool(cards)
        and (routing_client(runtime) is not None or callable(getattr(chat_model_provider(runtime), "generate_assistant_json", None)))
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

    provider = chat_model_provider(runtime)
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
