"""Native multi-turn + Responses API tool loop runner for operations channels."""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...core.text import operation_output_diagnostics
from ...infra.inference.api_providers import APIProvider
from ...infra.inference.providers import public_error
from ...infra.inference.responses_provider import FunctionCall, ResponsesClient, ResponseTurn
from .continuations import GenerationFeedback
from .l2_bridge import (
    ContextTree,
    append_assistant_tool_calls,
    append_branch_turn,
    append_tool_result,
    render_openai_messages,
    to_responses_input,
)
from .tools import OperationToolSession, operation_tool_definitions, validate_spoken_output


@dataclass
class OperationTurnResult:
    """Result returned by one completed multi-turn operations agent session."""

    action: str  # "speak" | "skip" | "handoff_private"
    text: str = ""
    skip_reason: str = ""
    skip_explanation: str = ""
    handoff: dict[str, Any] | None = None
    video_engagement: dict[str, Any] | None = None
    provenance: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tree: ContextTree | None = None
    branch_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    transport: str = "responses"

    def model_dump(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "text": self.text,
            "skip_reason": self.skip_reason,
            "skip_explanation": self.skip_explanation,
            "handoff": self.handoff,
            "video_engagement": self.video_engagement,
            "provenance": list(self.provenance),
            "tool_calls": list(self.tool_calls),
            "usage": dict(self.usage),
            "elapsed_ms": self.elapsed_ms,
            "transport": self.transport,
        }


def _normalize_calls(raw_calls: tuple | list) -> tuple[FunctionCall, ...]:
    normalized: list[FunctionCall] = []
    for call in raw_calls or ():
        if isinstance(call, FunctionCall):
            normalized.append(call)
        elif isinstance(call, dict):
            fn = call.get("function") or {}
            name = str(call.get("name") or fn.get("name") or "")
            raw_args = call.get("arguments") if "arguments" in call else fn.get("arguments", {})
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else dict(raw_args or {})
            normalized.append(FunctionCall(call_id=str(call.get("call_id") or call.get("id") or ""), name=name, arguments=args))
        else:
            args = getattr(call, "arguments", {})
            if isinstance(args, str):
                args = json.loads(args) if args.strip() else {}
            normalized.append(
                FunctionCall(
                    call_id=str(getattr(call, "call_id", "") or getattr(call, "id", "")),
                    name=str(getattr(call, "name", "")),
                    arguments=dict(args or {}),
                )
            )
    return tuple(normalized)


def _uses_mock_transport(llm: Any) -> bool:
    client = getattr(llm, "client", None)
    return isinstance(getattr(client, "_transport", None), httpx.MockTransport)


async def _call_single_turn(
    runtime: Any,
    tree: ContextTree,
    branch_id: str,
    tools: list[dict[str, Any]],
    *,
    tool_choice: str = "auto",
    max_output_tokens: int | None = None,
    thinking: bool = True,
    responses_client: Any = None,
) -> tuple[ResponseTurn, str]:
    """Dispatch one Responses-style model turn with clean test-stub compatibility."""
    messages = render_openai_messages(tree, branch_id)
    responses_input = to_responses_input(messages)
    client = (
        responses_client
        or getattr(runtime, "operation_responses", None)
        or getattr(runtime, "responses_client", None)
    )
    if client is not None:
        turn = await client.create(
            responses_input,
            tools=tools if tool_choice != "none" else (),
            tool_choice=tool_choice if tools and tool_choice != "none" else None,
            store=False,
            thinking=thinking,
            max_output_tokens=max_output_tokens,
        )
        return turn, "responses"

    llm = getattr(runtime, "operation_llm", None)
    if llm is not None and hasattr(llm, "create"):
        turn = await llm.create(
            responses_input,
            tools=tools if tool_choice != "none" else (),
            tool_choice=tool_choice if tools and tool_choice != "none" else None,
            store=False,
            thinking=thinking,
            max_output_tokens=max_output_tokens,
        )
        return turn, "responses"

    if llm is not None and isinstance(llm, APIProvider) and not _uses_mock_transport(llm) and getattr(runtime, "settings", None) is not None:
        async with ResponsesClient.from_settings(runtime.settings) as managed:
            turn = await managed.create(
                responses_input,
                tools=tools if tool_choice != "none" else (),
                tool_choice=tool_choice if tools and tool_choice != "none" else None,
                store=False,
                thinking=thinking,
                max_output_tokens=max_output_tokens,
            )
            return turn, "responses"

    if llm is not None:
        for method_name in ("create_response", "respond", "generate_turn"):
            fn = getattr(llm, method_name, None)
            if callable(fn):
                res = fn(
                    responses_input,
                    tools=tools if tool_choice != "none" else (),
                    tool_choice=tool_choice,
                    messages=messages,
                    max_output_tokens=max_output_tokens,
                )
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, ResponseTurn):
                    return res, "stub_responses"
                if isinstance(res, dict):
                    return (
                        ResponseTurn(
                            id=str(res.get("id") or "stub"),
                            calls=_normalize_calls(res.get("calls") or ()),
                            text=str(res.get("text") or res.get("content") or ""),
                            raw=res,
                        ),
                        "stub_responses",
                    )
        if callable(getattr(llm, "generate_text", None)):
            text = await llm.generate_text(messages, max_tokens=max_output_tokens or 1024)
            return ResponseTurn(id="text_compat", calls=(), text=str(text or ""), raw={}), "chat_text_compat"
        if callable(getattr(llm, "generate", None)):
            reply = await llm.generate(messages, getattr(runtime, "character", {}), f"{branch_id}", None)
            if hasattr(reply, "text"):
                text = str(reply.text or "")
            elif hasattr(reply, "spoken_text"):
                text = str(reply.spoken_text or "")
            elif isinstance(reply, dict):
                text = str(reply.get("text") or reply.get("spoken_text") or "")
            else:
                text = str(reply or "")
            return ResponseTurn(id="generate_compat", calls=(), text=text, raw={}), "generate_compat"

    raise ValueError("没有可用的模型响应客户端")


def should_use_native_turns(
    runtime: Any,
    *,
    allow_runtime_hooks: tuple[str, ...] = (),
    llm_override_attrs: tuple[str, ...] = ("generate_json", "generate_operation", "generate"),
) -> bool:
    """Return True when Responses native multi-turn should run instead of legacy JSON/Reply hooks."""
    if getattr(runtime, "operation_responses", None) is not None or getattr(runtime, "responses_client", None) is not None:
        return True
    if any(hook in getattr(runtime, "__dict__", {}) for hook in allow_runtime_hooks):
        return False
    llm = getattr(runtime, "operation_llm", None)
    if llm is None:
        return False
    if any(name in getattr(llm, "__dict__", {}) for name in llm_override_attrs):
        return False
    if any(callable(getattr(llm, method, None)) for method in ("create", "create_response", "respond", "generate_turn")):
        return True
    if _uses_mock_transport(llm):
        return False
    return callable(getattr(llm, "generate_text", None)) or isinstance(llm, APIProvider) or type(llm).__name__ == "APIProvider"


async def run_tool_session(
    runtime: Any,
    tree: ContextTree,
    branch_id: str,
    tool_session: OperationToolSession,
    *,
    tool_names: tuple[str, ...] | list[str] | set[str] | None = None,
    spoken_kind: str = "reply_public",
    max_rounds: int = 6,
    max_output_tokens: int | None = None,
    max_text_length: int | None = None,
    thinking: bool = True,
    responses_client: Any = None,
) -> OperationTurnResult:
    """Run native multi-turn tool-calling session until plain-text speech or terminal tool."""
    started = time.perf_counter()
    tools = operation_tool_definitions(format="responses", include=tool_names)
    total_usage: dict[str, int] = {}
    transport_used = "responses"

    for round_index in range(max_rounds):
        allow_tools = round_index < max_rounds - 1
        turn, transport_used = await _call_single_turn(
            runtime,
            tree,
            branch_id,
            tools if allow_tools else [],
            tool_choice="auto" if allow_tools else "none",
            max_output_tokens=max_output_tokens,
            thinking=thinking,
            responses_client=responses_client,
        )
        raw_usage = (turn.raw or {}).get("usage") if isinstance(turn.raw, dict) else None
        if isinstance(raw_usage, dict):
            for k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
                if isinstance(raw_usage.get(k), int):
                    total_usage[k] = total_usage.get(k, 0) + int(raw_usage[k])

        calls = _normalize_calls(turn.calls)
        if calls:
            append_assistant_tool_calls(tree, branch_id, calls, content=turn.text)
            for call in calls:
                result = await tool_session.execute(call.name, call.arguments)
                append_tool_result(tree, branch_id, tool_call_id=call.call_id, name=call.name, result=result)
            if tool_session.handoff_queued is not None:
                return OperationTurnResult(
                    action="handoff_private",
                    handoff=dict(tool_session.handoff_queued),
                    video_engagement=tool_session.video_engagement,
                    provenance=list(tool_session.provenance),
                    tool_calls=list(tool_session.tool_events),
                    tree=tree,
                    branch_id=branch_id,
                    usage=total_usage,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    transport=transport_used,
                )
            if tool_session.skipped is not None:
                return OperationTurnResult(
                    action="skip",
                    skip_reason=tool_session.skipped["reason"],
                    skip_explanation=tool_session.skipped["explanation"],
                    video_engagement=tool_session.video_engagement,
                    provenance=list(tool_session.provenance),
                    tool_calls=list(tool_session.tool_events),
                    tree=tree,
                    branch_id=branch_id,
                    usage=total_usage,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    transport=transport_used,
                )
            continue

        raw_text = turn.text or ""
        try:
            clean_text = validate_spoken_output(raw_text, kind=spoken_kind, max_length=max_text_length)
        except ValueError as error:
            raise GenerationFeedback(
                public_error(error),
                original_output=raw_text,
                diagnostics=operation_output_diagnostics(raw_text),
            ) from None
        append_branch_turn(tree, branch_id, {"role": "assistant", "content": clean_text})
        return OperationTurnResult(
            action="speak",
            text=clean_text,
            video_engagement=tool_session.video_engagement,
            provenance=list(tool_session.provenance),
            tool_calls=list(tool_session.tool_events),
            tree=tree,
            branch_id=branch_id,
            usage=total_usage,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            transport=transport_used,
        )

    raise GenerationFeedback("本轮多次工具调用后仍未完成自然发言或明确跳过")
