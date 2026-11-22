"""Thin foreground Omni dialogue: streaming chat, delegation ack and DeliveryPacket paraphrasing (L2a)."""

import asyncio
import json
import re
import time

import httpx
from fastapi import HTTPException

from ..core.errors import ContextWindowExceeded
from ..core.identity import operating_account_facts, without_model_identity
from ..core.store import dump
from ..persona.context.message_builder import build_messages
from ..persona.memory.daily import contains_memory_credential
from . import operator_facts, workshop_lookup
from .assistant_output import AssistantTextStream, assistant_markdown, clock_readout, workbench_fields
from .chat_context import ChatContext
from .chat_models import ChatIntent, DeliveryPacket
from .owner_memory import history_preference
from .personal_agent import object_scope

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FRONT_JSON_SCHEMA = {
    "type": "object",
    "title": "FrontDialogueReply",
    "required": ["reply_markdown"],
    "additionalProperties": False,
    "properties": {
        "reply_markdown": {"type": "string", "minLength": 1, "maxLength": 4000},
    },
}


def _clean_front_reply(raw: str) -> str:
    text = _THINK_BLOCK.sub("", raw or "").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                for key in ("reply_markdown", "reply", "text", "content"):
                    if isinstance(payload.get(key), str) and payload[key].strip():
                        text = payload[key].strip()
                        break
        except ValueError:
            pass
    cleaned = assistant_markdown(text).strip()
    if not cleaned:
        raise ValueError("前台对话未返回有效可见文本")
    return cleaned


def build_shared_task_summary(runtime, chat_context, character, account, turn_id=None, *, include_ids=False, limit=4):
    """Build minimal task summary cards shared between Chats and Voice Calls."""
    if chat_context is None:
        return []
    shared = []
    seen = set()
    for subject in chat_context.conversation_objects(character, account, turn_id).values():
        if subject.kind in {"task", "artifact"} and not contains_memory_credential(subject.label):
            if task := runtime.tasks.current(subject.identity):
                seen.add(task["id"])
                entry = {
                    "kind": "交办",
                    "goal": subject.label,
                    "revision": task["revision"],
                    "status": runtime.tasks.view(task)["status"],
                }
                if include_ids:
                    entry["id"] = task["id"]
                shared.append(entry)
    if include_ids and len(shared) < limit:
        for task in runtime.tasks.activity()[:limit]:
            if task["id"] in seen or contains_memory_credential(task.get("goal", "")):
                continue
            shared.append({
                "kind": "交办",
                "id": task["id"],
                "goal": task["goal"][:120],
                "revision": task["revision"],
                "status": task["status"],
            })
            if len(shared) >= limit:
                break
    return shared


async def _generate_plain_text(provider, messages, *, max_tokens, on_content=None, on_stage=None,
                               char=None, request_id="", source_text=""):
    if callable(getattr(provider, "generate_text_stream", None)):
        raw = await provider.generate_text_stream(
            messages, max_tokens=max_tokens, on_content=on_content, on_stage=on_stage,
        )
        return _clean_front_reply(raw)
    if callable(getattr(provider, "generate_text", None)):
        raw = await provider.generate_text(messages, max_tokens=max_tokens)
        if on_content:
            on_content(raw)
        return _clean_front_reply(raw)
    if callable(getattr(provider, "generate_assistant_json", None)):
        payload = await provider.generate_assistant_json(
            messages, _FRONT_JSON_SCHEMA, max_tokens=max_tokens, on_content=on_content, on_stage=on_stage,
        )
        if isinstance(payload, dict) and isinstance(payload.get("reply_markdown"), str):
            return _clean_front_reply(payload["reply_markdown"])
        raise ValueError("前台对话未返回有效的 reply_markdown")
    generated = await provider.generate(messages, char, request_id, source_text=source_text)
    return generated.spoken_text.strip()


def _thin_system_messages(char, request_id, user_text, *, notes=(), task_summaries=(), account_facts=None,
                          temporary=False, history_enabled=True, clock_hint=""):
    event = {"event_id": "operator:" + request_id, "text": user_text, "sender_name": "号主", "is_operator": True}
    role = build_messages(char, event, {}, "operator_chat", operating_account=account_facts)[:-2]
    instruction = (
        f"你是{char['name']}。只负责自然接话与角色语气陪伴，直接用第一人称纯文字回复（禁止输出 JSON 或内部标签）。"
        "只用中文自然口吻回复，不要附带英文说明或元注释。"
        "闲聊、感想、玩笑和假设直接接话。"
        "你没有直接操作工具的权限；需要查工坊、写文章或后台执行的事均由后台处理并异步交付，切勿口头虚构已完成发送、查库或操作回执。"
    )
    if not temporary and not history_enabled:
        instruction += "本轮只提供当前对话；若号主问及早先私聊记录而未附带上下文，请直接请号主点名或复述相关内容。"
    if clock_hint:
        instruction += clock_hint
    messages = [*role, {"role": "system", "content": instruction}]
    if notes:
        messages.append({
            "role": "system",
            "content": "与本轮相关的记忆资料，只作为自然语气背景：" + dump([
                {
                    "note": entry["anecdote"],
                    **(
                        {"original": entry["exact_source"]["quote"]}
                        if entry.get("exact_source") and entry["exact_source"]["quote"] != entry["anecdote"]
                        else {}
                    ),
                }
                for entry in notes
            ]),
        })
    if task_summaries:
        messages.append({
            "role": "system",
            "content": "当前活跃或近期交办摘要（仅供参考，禁止虚构未交付成果）：" + dump(task_summaries[:3]),
        })
    return messages


async def generate_chat_reply(context: ChatContext, char, account, request_id, data, *, notes=(), audit=None,
                              history=(), emit=None, clock_fields=(), local=None, discussed=None,
                              discussed_facts=None, validate_history=None, report=None,
                              generation_slot=None) -> tuple[ChatIntent, dict | None]:
    """Generate a thin-Omni conversational turn without tool/workflow planning schemas."""
    runtime = context.runtime
    prepared_provider = runtime.llm
    uses_model_card = getattr(prepared_provider, "uses_model_card", False)
    history_use = history_preference(runtime) if history and not data.temporary else None
    streamable = (
        emit is not None
        and not (audit and "organized_memory" in audit)
        and not history_use
        and not clock_fields
        and (not discussed or not discussed.get("artifact"))
    )
    local = local or operator_facts.now(context.browsing)
    service = getattr(runtime, "account", None)
    account_facts = operating_account_facts(service.status()) if service else None
    task_summaries = ([] if data.temporary else
                      build_shared_task_summary(runtime, context, char, account, limit=3))

    clock_hint = ""
    if clock_fields:
        rendered_clock = workbench_fields(" ".join("{{" + field + "}}" for field in clock_fields), local)
        clock_hint = f"本轮查询的工坊日历值：{rendered_clock}。用角色口吻自然简短接一句，精确数值会在末尾附带。"

    prefix = _thin_system_messages(
        char,
        request_id,
        data.message,
        notes=notes,
        task_summaries=task_summaries,
        account_facts=account_facts,
        temporary=data.temporary,
        history_enabled=history_preference(runtime)["history_enabled"],
        clock_hint=clock_hint,
    )
    if discussed_facts is not None:
        prefix.append({
            "role": "system",
            "content": (
                "这是只读交谈，本轮没有新交办或修改，请严格按下面附带版本回答。"
                "选材理由只引用记录中的真实理由，区分号主原话与编辑说明；记录没写时坦诚说明。"
            ),
        })
        current_content = dump({"task": discussed_facts, "message": data.message})
    else:
        current_content = data.message
    current = {"role": "user", "content": current_content}

    first_text = None
    sent = ""
    decoder = None

    def check_delivery_scope():
        context.check_scope(char, account)
        if service and account_facts != operating_account_facts(service.status()):
            raise HTTPException(409, "运营登录账号资料已变化，请重新发送")
        if discussed is not None and not context.task_history.read(discussed["task_id"], discussed["artifact"]):
            raise HTTPException(409, "任务权限已变化，请重新查看")

    def deliver(text):
        nonlocal first_text, sent
        check_delivery_scope()
        rendered = workbench_fields(decoder.emitted + text, local, partial=True)
        if not rendered.startswith(sent) or rendered == sent:
            return
        if first_text is None:
            first_text = time.monotonic()
        emit("delta", rendered[len(sent):])
        sent = rendered

    def read_chunk(text):
        if decoder:
            decoder.feed(text)

    if callable(getattr(prepared_provider, "generate_text_stream", None)) or callable(getattr(prepared_provider, "generate_text", None)):
        use_plain_decoder = True
    else:
        use_plain_decoder = False

    if report:
        report("preflight")
    if prepare := getattr(prepared_provider, "prepare_assistant_capacity", None):
        await prepare()
    if report:
        report("queued")

    slot = generation_slot() if generation_slot else runtime.operator_generation()
    try:
        async with slot:
            context.check_scope(char, account)
            runtime.validate_memory_delivery(audit)
            provider = runtime.llm
            if provider is not prepared_provider:
                raise HTTPException(409, "推理模型已切换，可重新发送")
            maximum = max(512, min(2048, getattr(provider, "config", {}).get("max_tokens", 1024)))
            if discussed_facts is not None:
                wire = [*prefix, current]
            elif uses_model_card:
                wire = await ChatContext.fit_assistant_history(provider, prefix, history, current, None, maximum)
            else:
                wire = [*prefix, *history, current]
            if streamable and (not discussed or not discussed.get("artifact")):
                decoder = AssistantTextStream(deliver, plain=use_plain_decoder, allow_reply_only=not use_plain_decoder)
            raw_text = await _generate_plain_text(
                provider,
                wire,
                max_tokens=maximum,
                on_content=read_chunk if (emit and streamable) else None,
                on_stage=report,
                char=char,
                request_id="operator:" + request_id,
                source_text=data.message,
            )
            check_delivery_scope()
            runtime.validate_memory_delivery(audit)
            if validate_history:
                validate_history()
            reply = workbench_fields(raw_text, local)
            if clock_fields:
                reply = clock_readout(reply, clock_fields, local)
            decision = ChatIntent(
                action="chat",
                reference=discussed["task_id"] if discussed else None,
            )
            decision._reply_markdown = reply
            decision._clock_fields = tuple(dict.fromkeys(clock_fields))
            return decision, audit
    except ContextWindowExceeded as error:
        raise HTTPException(422, str(error)) from error
    except (ValueError, TimeoutError, httpx.HTTPError) as error:
        raise HTTPException(503, "这轮回复未完成，可以重试。") from error


async def acknowledge_delegation(context: ChatContext, char, request_id, user_message, packet: DeliveryPacket, *,
                                 generation_slot=None) -> str:
    """Voice a brief natural foreground ack after background task creation; deterministic fallback."""
    if packet.kind != "task_accepted" or "已预约" in packet.summary:
        return packet.summary
    provider = context.runtime.llm
    if not getattr(provider, "uses_model_card", False) or not (
        callable(getattr(provider, "generate_text_stream", None)) or callable(getattr(provider, "generate_text", None))
    ):
        return packet.summary
    messages = _thin_system_messages(char, request_id, user_message)
    messages.extend([
        {
            "role": "system",
            "content": (
                f"后台已正式立项并开始处理号主的这项交办（基础受理回执：{packet.summary}）。"
                "请你用角色口吻说一两句简短、自然的受理承接语，明确告诉号主你已经接下交给后台去做、做好后会在当前对话回交，不要编造进度或提前给结论。"
            ),
        },
        {"role": "user", "content": user_message},
    ])
    slot = generation_slot() if generation_slot else context.runtime.operator_generation()
    try:
        async with asyncio.timeout(10), slot:
            context.check_scope(char, context.account())
            text = await _generate_plain_text(provider, messages, max_tokens=160, char=char, request_id=request_id)
            return text or packet.summary
    except (ValueError, TimeoutError, httpx.HTTPError, HTTPException):
        return packet.summary


async def paraphrase_delivery(context: ChatContext, char, account, request_id, data, result, packet: DeliveryPacket,
                              audit, *, generation_slot=None):
    """Voice a DeliveryPacket / tool read result with thin Omni plain text while preserving receipts."""
    if not context.recall_visible(result):
        raise HTTPException(409, "记忆来源或授权已变化，请重新读取")
    receipt = result.get("tool_receipt") or {}
    if receipt.get("state") in {"empty", "unavailable", "forbidden"}:
        return result
    if result.get("clarification_request") or not result.get("reply_text"):
        return result
    runtime = context.runtime
    provider = runtime.llm
    slot = generation_slot() if generation_slot else runtime.operator_generation()

    if "lookup_chosen" in result:
        chosen = result["lookup_chosen"]
        scope = (object_scope(runtime), history_preference(runtime))
        opening = ""
        if getattr(provider, "uses_model_card", False) and (
            callable(getattr(provider, "generate_text_stream", None)) or callable(getattr(provider, "generate_text", None))
        ):
            summary_items = [
                {"kind": item["kind"], "title": workshop_lookup.label(item), "state": item["state"]}
                for item in chosen[:3]
            ]
            messages = _thin_system_messages(char, request_id, data.message)
            messages.extend([
                {
                    "role": "system",
                    "content": (
                        "这次工坊读取已经完成。请用角色口吻写一句简短自然的开场感想或接话（纯文字 1 句话，不超过 60 字）；"
                        "具体视频标题、评论原话、编号、时码与链接由工坊在下方直接渲染，禁止你自己拼链接或复读完整条目：\n"
                        + dump(summary_items)
                    ),
                },
                {"role": "user", "content": data.message},
            ])
            try:
                async with asyncio.timeout(12), slot:
                    opening = await _generate_plain_text(provider, messages, max_tokens=160, char=char, request_id=request_id)
            except (ValueError, TimeoutError, httpx.HTTPError):
                opening = ""
        context.check_scope(char, account)
        runtime.validate_memory_delivery(audit)
        if scope != (object_scope(runtime), history_preference(runtime)):
            raise HTTPException(409, "读取权限已变化，请重新查询")
        sections, sources = workshop_lookup.present(chosen, opening)
        return result | {
            "lookup_items": chosen,
            "reply_sections": sections,
            "sources": sources,
            "reply_text": "\n\n".join(sections),
            "reply_format": "plain",
            "card": None,
        }

    if result.get("reply_sections"):
        return result
    messages = _thin_system_messages(char, request_id, data.message)
    messages.extend([
        {
            "role": "system",
            "content": "这次读取已经结束。只用中文自然口吻把查到的答案告诉号主，严格以回执为准，不要附带英文注释或额外解释。以下是本轮已经取得的读取回执："
            + dump({"summary": packet.summary, "receipt": packet.tool_receipt, "sources": packet.sources[:4]}),
        },
        {"role": "user", "content": data.message},
    ])
    try:
        async with asyncio.timeout(24), slot:
            reply = await _generate_plain_text(provider, messages, max_tokens=1024, char=char, request_id=request_id)
    except (ValueError, TimeoutError, httpx.HTTPError):
        return result
    context.check_scope(char, account)
    runtime.validate_memory_delivery(audit)
    if not context.recall_visible(result):
        raise HTTPException(409, "记忆来源或授权已变化，请重新读取")
    return result | {"reply_text": reply, "reply_format": "markdown"}


async def paraphrase_task_delivery(runtime, char, packet: DeliveryPacket) -> str:
    """Role-voice an asynchronous background task delivery packet for Push Back."""
    if not packet.summary:
        return ""
    provider = runtime.llm
    if not getattr(provider, "uses_model_card", False) or not (
        callable(getattr(provider, "generate_text_stream", None)) or callable(getattr(provider, "generate_text", None))
    ):
        return packet.summary
    status_label = {
        "task_completed": "已完成并交回成果",
        "task_waiting_approval": "待号主确认下一步发布或操作",
        "task_failed": "未能顺利完成",
    }.get(packet.kind, "已有进展更新")
    messages = _thin_system_messages(char, "delivery", "同步后台任务交付")
    messages.extend([
        {
            "role": "system",
            "content": (
                f"后台任务当前{status_label}。请用角色口吻简短转述下面这份核验回执给号主，忠于原结论、状态与限制，不编造链接、文件或额外细节："
                + dump({"status": status_label, "summary": packet.summary, "facts": packet.facts})
            ),
        },
        {"role": "user", "content": "向我同步刚才任务的结果。"},
    ])
    try:
        async with asyncio.timeout(10), runtime.operator_generation():
            with without_model_identity():
                return await _generate_plain_text(provider, messages, max_tokens=220, char=char, request_id="delivery")
    except (ValueError, TimeoutError, httpx.HTTPError):
        return packet.summary
