"""Read-only staged ResponseDecision experiment; never installs or sends results.

The small decision and the plain body share one output allowance, including a
single directed retry. Backend source rejection is reported separately from
model competence; no gold strings, expected actions or case IDs enter prompts.
"""

import asyncio
import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Literal

from pydantic import Field, create_model

from tuber_studio.browsing_models import RESPONSE_INSTRUCTION, ResponseDecision, response_schema
from tuber_studio.generation_meter import GenerationMeter, reserve_generation, settle_generation
from tuber_studio.http_api import response_json
from tuber_studio.live_media import StrictModel
from tuber_studio.providers import SYSTEM_RULES, LlamaCppProvider, parse_json_object, single_system_messages

DecisionOnly = create_model(
    "StagedDecision",
    __base__=StrictModel,
    **{
        key: (ResponseDecision.model_fields[key].annotation, deepcopy(ResponseDecision.model_fields[key]))
        for key in ("action", "reason", "evidence_ids")
    },
)
SENDING = {"reply_public", "handoff_private"}


class StageFailure(ValueError):
    def __init__(self, kind, message, *, repairable=False, retry_kind=None):
        self.kind, self.repairable, self.retry_kind = kind, repairable, retry_kind
        super().__init__(message)


class EvidenceQuote(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=2000)


class EvidencePacket(StrictModel):
    answerability: Literal["answerable", "insufficient", "visual_required", "unread_part"]
    missing: list[str] = Field(max_length=8)
    quotes: list[EvidenceQuote] = Field(max_length=8)


def evidence_packet_schema(data):
    schema = EvidencePacket.model_json_schema()
    schema["$defs"]["EvidenceQuote"]["properties"]["evidence_id"]["enum"] = sorted(
        {item["id"] for item in data.get("evidence", [])}
    ) or ["no_valid_source_id"]
    return schema


def _packet(value, data):
    try:
        packet = EvidencePacket.model_validate(value)
    except ValueError as error:
        raise StageFailure(
            "invalid_evidence_packet", str(error), repairable=True, retry_kind="evidence_format_repair"
        ) from None
    originals = {}
    for item in data.get("evidence", []):
        if item["id"] in originals:
            raise StageFailure("ambiguous_source_id", "原始材料存在重复ID，不能定位逐字引文")
        originals[item["id"]] = item
    selected_rows, mapping, seen = {}, [], set()
    for item in packet.quotes:
        original = originals.get(item.evidence_id)
        if (
            not original
            or not isinstance(original.get("text"), str)
            or item.quote not in original["text"]
            or (item.evidence_id, item.quote) in seen
        ):
            raise StageFailure(
                "unverified_quote",
                "quote必须是对应真实来源ID的逐字子串，且不能重复",
                repairable=True,
                retry_kind="evidence_reselection",
            )
        seen.add((item.evidence_id, item.quote))
        offset = original["text"].index(item.quote)
        location = {
            "evidence_id": item.evidence_id,
            "quote": item.quote,
            "source_char_start": offset,
            "source_char_end": offset + len(item.quote),
            "source_text_sha256": hashlib.sha256(original["text"].encode()).hexdigest(),
            "quote_sha256": hashlib.sha256(item.quote.encode()).hexdigest(),
        }
        mapping.append(location)
        if item.evidence_id not in selected_rows:
            selected_rows[item.evidence_id] = deepcopy(original) | {"verified_quotes": []}
        selected_rows[item.evidence_id]["verified_quotes"].append(deepcopy(location))
    # Selection may omit whole segments, but cannot remove a selected segment's
    # negation or context. Preserve its original order and annotate the quotes.
    rows = [selected_rows[item["id"]] for item in data.get("evidence", []) if item["id"] in selected_rows]
    if packet.answerability == "answerable" and not rows:
        raise StageFailure(
            "missing_quote",
            "声称可回答但没有提供真实引文",
            repairable=True,
            retry_kind="evidence_reselection",
        )
    retained_keys = {
        "kind",
        "thread",
        "coverage",
        "missing_parts",
        "visual_available",
        "source_identity",
        "max_chars",
        "recent_public_responses",
    }
    routed = {key: deepcopy(data[key]) for key in retained_keys if key in data}
    author = {
        key: deepcopy(data.get("metadata", {})[key])
        for key in ("owner_uid", "owner")
        if key in data.get("metadata", {})
    }
    if author:
        routed["metadata"] = author
    routed["evidence"] = rows
    routed["packet_policy"] = "selected_full_segments_with_verified_quotes"
    routed["extraction_assessment"] = {
        "answerability": packet.answerability,
        "missing": packet.missing,
        "status": "unverified_model_assessment_not_a_fact_verdict",
    }
    return routed, {
        "packet_policy": "selected_full_segments_with_verified_quotes",
        "model_extraction": packet.model_dump(),
        "quote_mapping": mapping,
        "packet": routed,
        "packet_sha256": hashlib.sha256(_dump(routed).encode()).hexdigest(),
        "dropped_top_level_fields": sorted(set(data) - retained_keys - {"evidence", "metadata"}),
        "dropped_metadata_fields": sorted(set(data.get("metadata", {})) - set(author)),
        "unselected_evidence_ids": sorted(set(originals) - {item["id"] for item in rows}),
        "evidence_rewritten": any(item["text"] != originals[item["id"]]["text"] for item in rows),
        "evidence_text_preserved": True,
        "substring_validation_only": True,
        "semantic_quality_accepted": None,
    }


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def decision_schema(data):
    allowed = {item["id"] for item in data.get("evidence", [])} | set(data.get("allowed_evidence_ids", []))
    schema = response_schema(
        allowed, maximum=data.get("max_chars", 100), allow_handoff=data.get("kind") != "top_level"
    )
    for branch in schema["oneOf"]:
        branch["properties"].pop("text")
        branch["required"].remove("text")
    schema["title"] = "StagedDecision"
    return schema, allowed


def source_rejection(data, decision):
    """Conservative experiment gate, not a claim that the model understood it.

    Partial material without a required-part mapping is unresolved here. A
    caller with a verified relevance mapping may supply source_acceptance.
    """
    if decision.action not in SENDING:
        return None
    if data.get("visual_available") is False:
        return "来源显式缺少所需画面，不能据字幕猜视觉事实"
    if data.get("missing_parts") and data.get("coverage") == "partial":
        return "存在未读取分P且未核验问题所需分P，不能生成事实回复"
    return None


def _decision(value, schema, allowed, data):
    if set(value) != {"action", "reason", "evidence_ids"}:
        raise StageFailure(
            "invalid_decision_fields",
            "决策必须且只能包含action/reason/evidence_ids",
            repairable=True,
            retry_kind="decision_format_repair",
        )
    try:
        selected = DecisionOnly.model_validate(value)
    except ValueError as error:
        raise StageFailure(
            "invalid_decision_fields", str(error), repairable=True, retry_kind="decision_format_repair"
        ) from None
    if not set(selected.evidence_ids) <= allowed or len(selected.evidence_ids) != len(
        set(selected.evidence_ids)
    ):
        raise StageFailure(
            "invalid_references",
            "引用须为提供的唯一证据ID",
            repairable=True,
            retry_kind="decision_reselection",
        )
    branch = next(
        (branch for branch in schema["oneOf"] if selected.action in branch["properties"]["action"]["enum"]),
        None,
    )
    if not branch or selected.reason not in branch["properties"]["reason"]["enum"]:
        raise StageFailure(
            "channel_reason_mismatch",
            "行动／理由与既有ResponseDecision来源契约不符",
            repairable=True,
            retry_kind="decision_reselection",
        )
    if selected.action in SENDING and not selected.evidence_ids:
        raise StageFailure(
            "missing_references",
            "需要回复时必须提供当前证据引用",
            repairable=True,
            retry_kind="decision_reselection",
        )
    if selected.action == "handoff_private":
        source = data.get("thread", {}).get("source", {})
        identity = source.get("author_uid")
        if (
            not isinstance(identity, (str, int))
            or isinstance(identity, bool)
            or not re.fullmatch(r"[1-9]\d{0,19}", str(identity))
        ):
            raise StageFailure(
                "unverified_recipient",
                "未核验当前评论作者UID，不能由模型补充收件人",
                repairable=True,
                retry_kind="decision_reselection",
            )
    return selected


def _body(content, data, service):
    if not isinstance(content, str) or not content.strip():
        raise StageFailure("empty_body", "正文为空", repairable=True, retry_kind="body_format_repair")
    candidate = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.S)
    candidate = fence[1] if fence else candidate
    try:
        container = json.loads(candidate)
    except ValueError:
        container = None
    if isinstance(container, dict) and isinstance(container.get("text"), str):
        content = container["text"]
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", content):
        raise StageFailure(
            "body_control_characters",
            "正文含无效文本控制字符",
            repairable=True,
            retry_kind="body_format_repair",
        )
    if not content.strip():
        raise StageFailure("empty_body", "正文为空", repairable=True, retry_kind="body_format_repair")
    if len(content) > 500:
        raise StageFailure(
            "body_too_long", "正文超过ResponseDecision交付长度", repairable=True, retry_kind="body_revision"
        )
    return content


def _messages(character, data, selected=None, previous=None, *, phase="decision", decision_character="full"):
    instruction = RESPONSE_INSTRUCTION
    if phase == "evidence":
        instruction = (
            "挑选与当前问题相关的原文，按来源evidence_id填写quotes。"
            "quote摘录对应evidence.text中的原句；answerability填写这些内容的可回答性，"
            "missing填写还需要补充的信息。"
        )
        payload = data
    elif selected is None:
        instruction += " 此阶段填写action、reason、evidence_ids。"
        payload = data
    else:
        instruction = (
            "以角色口吻接着和当前用户聊，结合对方的话、相关材料与当前互动决定回应。"
            "这一阶段输出台词正文。"
            f"台词控制在{min(500, data.get('max_chars', 100))}字以内。"
        )
        payload = {"unchanged_source_material": data, "fixed_decision": selected.model_dump()}
    fields = (
        ("core", "style", "rules")
        if phase == "body" or (phase == "decision" and decision_character == "full")
        else ("core", "rules")
    )
    messages = [
        {
            "role": "system",
            "content": SYSTEM_RULES + _dump({key: character[key] for key in fields}),
        },
        {"role": "system", "content": instruction},
        {"role": "user", "content": "当前交流与相关材料：" + _dump(payload)},
    ]
    if previous is not None:
        messages.append({"role": "user", "content": "请根据这条反馈调整本阶段的输出：" + _dump(previous)})
    return single_system_messages(messages)


def _visible(content):
    if not isinstance(content, str):
        return None, False
    redacted, number = re.subn(
        r"<think\b[^>]*>.*?(?:</think>|$)", "[reasoning omitted]", content, flags=re.I | re.S
    )
    if "</think>" in redacted.lower():
        # Cannot distinguish an unmarked reasoning prefix safely.
        return "[content omitted: reasoning boundary invalid]", True
    return redacted, bool(number)


async def _native(service, row, messages, maximum, record, schema=None):
    """Same native transport/tokenizer/sampling, with failure usage retained.

    Unlike generate_json's success-only result, this experiment must record
    actual stop/usage even when parsing fails. No summarize/compaction path.
    """
    runtime, provider = service.runtime, service.runtime.llm
    started = time.perf_counter()
    record.update(
        max_tokens=maximum, model_calls=0, stop=False, usage={}, output_tokens_charged=0, usage_verified=False
    )
    try:
        async with asyncio.timeout(max(0, row["deadline"] - time.time())):
            async with runtime.offline_generation():
                service._check_round(row)
                if time.time() >= row["deadline"]:
                    raise TimeoutError("实验轮次已达到期限")
                await provider.ensure_capacity()
                tokens = await provider.count_tokens(messages)
                record["input_tokens_counted"] = tokens
                if tokens + maximum > provider.config["context_tokens"] * 95 // 100:
                    raise StageFailure("context_window", "输入超窗，未静默截断")
                payload = {
                    "model": provider.config.get("model", "local"),
                    "messages": messages,
                    **provider.sampling(),
                    "max_tokens": maximum,
                    "stream": False,
                    "chat_template_kwargs": {
                        "enable_thinking": provider.config.get("enable_thinking", False)
                    },
                    **(
                        {"reasoning_format": "deepseek"}
                        if provider.config.get("enable_thinking", False)
                        else {}
                    ),
                    **({"seed": provider.config["seed"]} if "seed" in provider.config else {}),
                }
                if schema is not None:
                    payload["response_format"] = {"type": "json_object", "schema": schema}
                with GenerationMeter(service.repo, row["id"]):
                    reservation = reserve_generation(tokens, maximum)
                    record.update(model_calls=1, output_tokens_charged=maximum)
                    response = await response_json(
                        provider.client,
                        "POST",
                        "/v1/chat/completions",
                        payload=payload,
                        timeout=min(
                            provider.config.get("prefill_timeout_seconds", 600),
                            max(0.001, row["deadline"] - time.time()),
                        ),
                    )
                    observed = response.get("choices")
                    if isinstance(observed, list) and len(observed) == 1 and isinstance(observed[0], dict):
                        record["finish_reason"] = observed[0].get("finish_reason")
                        record["stop"] = record["finish_reason"] == "stop"
                        observed_message = observed[0].get("message")
                        if isinstance(observed_message, dict):
                            record["raw_output"], _ = _visible(observed_message.get("content"))
                            record["reasoning_characters_observed_not_retained"] = (
                                len(observed_message["reasoning_content"])
                                if isinstance(observed_message.get("reasoning_content"), str)
                                else 0
                            )
                    usage = response.get("usage", {})
                    usage = usage if isinstance(usage, dict) else {}
                    record["usage"] = {
                        key: usage[key]
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if type(usage.get(key)) is int
                    }
                    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
                    if (
                        type(prompt) is not int
                        or prompt != tokens
                        or type(completion) is not int
                        or not 0 <= completion <= maximum
                    ):
                        raise StageFailure("invalid_usage", "服务未提供与实际tokenizer一致的有界计量")
                    settle_generation(reservation, usage)
                    record["usage_verified"] = True
                    provider.last_usage = record["usage"].copy()
                    record["output_tokens_charged"] = completion
                choices = response.get("choices")
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    raise StageFailure("invalid_envelope", "服务未返回唯一回答")
                choice = choices[0]
                record["finish_reason"] = choice.get("finish_reason")
                record["stop"] = choice.get("finish_reason") == "stop"
                message = choice.get("message")
                if not isinstance(message, dict):
                    raise StageFailure("invalid_envelope", "回答消息无效")
                content, embedded_reasoning = _visible(message.get("content"))
                record["raw_output"] = content
                record["reasoning_characters_observed_not_retained"] = (
                    len(message["reasoning_content"])
                    if isinstance(message.get("reasoning_content"), str)
                    else 0
                )
                if embedded_reasoning:
                    raise StageFailure("reasoning_in_content", "服务把思考混入正文，已移除且不采用")
                if (
                    message.get("tool_calls")
                    or message.get("function_call")
                    or record["finish_reason"] == "tool_calls"
                ):
                    raise StageFailure("tools_forbidden", "本实验不允许工具调用")
                if not record["stop"]:
                    raise StageFailure(
                        "not_stop",
                        "输出未完整stop，未采用部分结果",
                        repairable=record["finish_reason"] == "length",
                        retry_kind="new_" + record["stage"] + "_attempt",
                    )
                if not isinstance(content, str):
                    raise StageFailure("invalid_content", "服务未返回正文文本")
                return content
    finally:
        record["elapsed_ms"] = (time.perf_counter() - started) * 1000


async def evaluate_staged_case(
    service,
    row,
    case,
    *,
    total_output_tokens=4096,
    decision_tokens=1024,
    max_retries=1,
    source_acceptance=None,
    strategy="staged",
    evidence_tokens=1024,
    source_guard_mode="necessary",
    decision_character="full",
):
    """Callable adapter for the isolated native CLI. Does not finish its round.

    actual remains a complete ResponseDecision dict. A body retry cannot change
    the decision. A decision retry is explicitly recorded as reselection or
    format repair. All original failures remain in stages; no expected field is
    sent to the model or used as a repair target.
    """
    if not isinstance(service.runtime.llm, LlamaCppProvider):
        raise ValueError("staged实验仅支持可核验真实tokenizer的原生LlamaCppProvider")
    if (
        type(total_output_tokens) is not int
        or not 2 <= total_output_tokens <= 65536
        or type(decision_tokens) is not int
        or not 0 < decision_tokens < total_output_tokens
        or type(max_retries) is not int
        or not 0 <= max_retries <= 1
    ):
        raise ValueError("两阶段必须共享有界输出额度，最多定向重试一次")
    if (
        strategy not in {"staged", "evidence_first"}
        or source_guard_mode not in {"necessary", "conservative"}
        or decision_character not in {"full", "core_rules"}
        or type(evidence_tokens) is not int
        or evidence_tokens < 1
        or strategy == "evidence_first"
        and evidence_tokens + decision_tokens >= total_output_tokens
    ):
        raise ValueError("未知实验条件或证据／决策预算未留下正文额度")
    data = deepcopy(case["data"])
    schema, allowed = decision_schema(data)
    character = service.store.character(row["character_id"], row["character_version"])
    source = data.get("thread", {}).get("source", {})
    report = {
        "id": case.get("id"),
        "strategy": strategy,
        "packet_policy": "selected_full_segments_with_verified_quotes"
        if strategy == "evidence_first"
        else None,
        "actual": None,
        "structural_complete": False,
        "stages": [],
        "total_output_tokens": total_output_tokens,
        "decision_tokens": decision_tokens,
        "evidence_tokens": evidence_tokens if strategy == "evidence_first" else 0,
        "body_tokens": total_output_tokens
        - decision_tokens
        - (evidence_tokens if strategy == "evidence_first" else 0),
        "max_retries": max_retries,
        "retries_used": 0,
        "output_tokens_charged": 0,
        "materials_sha256": hashlib.sha256(_dump(data).encode()).hexdigest(),
        "source_gate_policy": "caller_supplied" if source_acceptance else source_guard_mode,
        "decision_character": decision_character,
        "original_materials": deepcopy(data),
        "raw_model_action_matches": None,
        "final_model_action_matches": None,
        "pipeline_acceptance": False,
        "backend_binding": {
            "account_id": row["account_id"],
            "character_id": row["character_id"],
            "character_version": row["character_version"],
            "source_author_uid": source.get("author_uid"),
            "resource": data.get("source_identity", {}).get("resource")
            or data.get("metadata", {}).get("bvid"),
            "provenance": "trusted_evaluation_case_source_only; no platform target created",
        },
        "requires_human_fact_review": True,
        "semantic_quality_accepted": None,
        "platform_messages_sent": 0,
        "production_configuration_written": False,
        "generation_parameters": {
            "provider_class": type(service.runtime.llm).__name__,
            "model": service.runtime.llm.config.get("model"),
            "context_tokens": service.runtime.llm.config["context_tokens"],
            "native_context_tokens": service.runtime.llm.config.get("native_context_tokens"),
            "enable_thinking": service.runtime.llm.config.get("enable_thinking", False),
            "sampling": service.runtime.llm.sampling(),
            "seed": service.runtime.llm.config.get("seed"),
        },
        "stage_timing_scope": "lock wait + native profile/tokenizer/transport/generation; validation included in case wall time",
    }
    budget_before = service.repo.round(row["id"])["budget"].copy()
    started, selected, phase, previous = (
        time.perf_counter(),
        None,
        "evidence" if strategy == "evidence_first" else "decision",
        None,
    )
    guard = source_acceptance or (
        source_rejection if source_guard_mode == "conservative" else lambda data, decision: None
    )
    working = deepcopy(data)
    retry_directives = 0
    while True:
        maximum = min(
            report["evidence_tokens"]
            if phase == "evidence"
            else decision_tokens
            if phase == "decision"
            else report["body_tokens"],
            total_output_tokens - report["output_tokens_charged"],
        )
        if maximum <= 0:
            report["error"] = {
                "kind": "output_allowance_exhausted",
                "message": "共享输出额度耗尽，不扩额重试",
            }
            break
        stage = {
            "stage": phase,
            "attempt": 1 + sum(item["stage"] == phase for item in report["stages"]),
            "retry": deepcopy(previous),
            "materials_sha256": report["materials_sha256"],
            "packet_sha256": report.get("evidence_packet", {}).get("packet_sha256"),
            "character_fields": ["core", "style", "rules"]
            if phase == "body" or phase == "decision" and decision_character == "full"
            else ["core", "rules"],
        }
        report["stages"].append(stage)
        if phase == "decision" and stage["attempt"] == 1:
            report["first_model_decision"] = None
        try:
            raw = await _native(
                service,
                row,
                _messages(
                    character,
                    working,
                    selected if phase == "body" else None,
                    previous,
                    phase=phase,
                    decision_character=decision_character,
                ),
                maximum,
                stage,
                evidence_packet_schema(data)
                if phase == "evidence"
                else schema
                if phase == "decision"
                else None,
            )
            if phase in {"evidence", "decision"}:
                try:
                    value = parse_json_object(raw)
                except ValueError as error:
                    stage["json_parseable"] = False
                    raise StageFailure(
                        "invalid_" + phase + "_json",
                        str(error),
                        repairable=True,
                        retry_kind=phase + "_format_repair",
                    ) from None
                stage["json_parseable"] = True
                if phase == "evidence":
                    working, report["evidence_packet"] = _packet(value, data)
                    schema, allowed = decision_schema(working)
                    phase, previous = "decision", None
                    continue
                if stage["attempt"] == 1:
                    report["first_model_decision"] = {
                        key: value.get(key) for key in ("action", "reason", "evidence_ids")
                    }
                    report["raw_model_action_matches"] = value.get("action") in case.get(
                        "expected_actions", []
                    )
                selected = _decision(value, schema, allowed, working)
                stage["decision"] = selected.model_dump()
                report["final_model_action_matches"] = selected.action in case.get("expected_actions", [])
                rejection = guard(working, selected)
                if rejection:
                    raise StageFailure(
                        "source_rejected",
                        rejection,
                        repairable=True,
                        retry_kind="decision_reselection_after_source_rejection",
                    )
                if selected.action in SENDING:
                    phase, previous = "body", None
                    continue
                text = ""
            else:
                text = _body(raw, working, service)
            assembled = json.dumps(
                selected.model_dump() | {"text": text}, ensure_ascii=False, allow_nan=False
            )
            report["actual"] = ResponseDecision.model_validate_json(assembled).model_dump()
            report["assembled_json"] = assembled
            report["structural_complete"] = True
            report["pipeline_acceptance"] = True
            report["action_matches"] = selected.action in case.get("expected_actions", [])
            report["references_valid"] = set(selected.evidence_ids) <= allowed
            break
        except asyncio.CancelledError:
            stage["error"] = {"kind": "cancelled", "message": "任务取消，未采用结果"}
            raise
        except Exception as error:
            failure = {"kind": getattr(error, "kind", type(error).__name__), "message": str(error)}
            stage["error"] = failure
            if not getattr(error, "repairable", False) or retry_directives >= max_retries:
                report["error"] = failure
                break
            if report["output_tokens_charged"] + stage.get("output_tokens_charged", 0) >= total_output_tokens:
                report["error"] = {
                    "kind": "output_allowance_exhausted",
                    "message": "共享输出额度耗尽，不扩额重试",
                }
                break
            retry_directives += 1
            previous = {
                "kind": error.retry_kind,
                "specific_problem": str(error),
                "previous_output": stage.get("raw_output"),
                "unchanged_source_sha256": report["materials_sha256"],
            }
        finally:
            report["output_tokens_charged"] += stage.get("output_tokens_charged", 0)
    budget_after = service.repo.round(row["id"])["budget"]
    report["budget_delta"] = {key: budget_after[key] - value for key, value in budget_before.items()}
    report["model_calls"] = sum(stage.get("model_calls", 0) for stage in report["stages"])
    report["retries_used"] = sum(
        bool(stage.get("retry")) and stage.get("model_calls", 0) for stage in report["stages"]
    )
    report["retry_directives_issued"] = retry_directives
    report["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return report
