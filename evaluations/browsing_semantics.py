"""Read-only native-model semantic probe. Never enables automation or publishes.

Run with the repository environment and an isolated --runtime. Optional model
overrides affect only this process. Output records structural errors separately
from semantic expectations; these text-only cases do not prove visual capability.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from copy import deepcopy
from pathlib import Path

from tuber_studio.browsing import SYSTEM_RULES, BrowsingService
from tuber_studio.browsing_models import RESPONSE_INSTRUCTION, BrowsingPolicy, ResponseDecision
from tuber_studio.comments import Comments
from tuber_studio.config import Settings
from tuber_studio.config import atomic_json as write_report
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.dynamics import Dynamics
from tuber_studio.providers import LlamaCppProvider
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store

STRICT_CONTEXT_TOKENS = 131072
OVERRIDES = {
    "model_base_url": "base_url",
    "model": "model",
    "context_tokens": "context_tokens",
    "native_context_tokens": "native_context_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "presence_penalty": "presence_penalty",
    "repeat_penalty": "repeat_penalty",
    "seed": "seed",
}
PRIVATE_KEY = re.compile(
    r"api.?key|password|secret|authorization|cookie|csrf|sessdata|(?:^|[_-])token$|[_-]key$", re.I
)


def staged_evaluator():
    """Support file execution, package execution and importlib-loaded test entrypoints."""
    if __package__:
        from .staged_operations import evaluate_staged_case
    else:
        # Direct files and importlib probes need not have the repository on sys.path.
        path = Path(__file__).resolve().with_name("staged_operations.py")
        name = "_tuber_staged_evaluation_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError("Staged evaluation sibling module is unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        evaluate_staged_case = module.evaluate_staged_case
    return evaluate_staged_case


def strategy_options(args, total):
    strategy = getattr(args, "response_strategy", "combined")
    if strategy == "combined":
        return None
    if strategy not in {"staged", "evidence_first"}:
        raise ValueError("Unknown response strategy")
    decision = getattr(args, "staged_decision_tokens", 1024)
    evidence = getattr(args, "evidence_tokens", 1024)
    retries = getattr(args, "max_retries", 1)
    if not 512 <= total <= 4096 or not 0 < decision < total or evidence < 1:
        raise ValueError("Staged output must share at most 4096 tokens with a smaller decision allowance")
    if strategy == "evidence_first" and evidence + decision >= total:
        raise ValueError("Evidence and decision allowances must leave room for the body")
    if retries not in {0, 1}:
        raise ValueError("At most one retry is permitted across all stages")
    return {
        "strategy": strategy,
        "total_output_tokens": total,
        "decision_tokens": decision,
        "evidence_tokens": evidence,
        "max_retries": retries,
        "source_guard_mode": getattr(args, "source_guard_mode", "necessary"),
        "decision_character": getattr(args, "decision_character", "full"),
    }


def stage_usage(result):
    """Only sum usage fields actually reported by every model call, never invent zeros."""
    calls = [stage for stage in result.get("stages", []) if stage.get("model_calls", 0)]
    usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if calls and all(type(stage.get("usage", {}).get(key)) is int for stage in calls):
            usage[key] = sum(stage["usage"][key] for stage in calls)
    return usage, bool(calls) and all(stage.get("usage_verified") is True for stage in calls)


def visible_output(content):
    if not isinstance(content, str):
        return ""
    return re.sub(r"<think\b[^>]*>.*?(?:</think>|$)", "", content, flags=re.I | re.S).strip()


def reply_for_review(output):
    visible = visible_output(output)
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", visible, flags=re.S)
    candidate = match[1] if match else visible
    try:
        value = json.loads(candidate)
    except ValueError:
        return visible
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return visible


def install_private_label_experiment(provider, observations):
    """One isolated native instance; no action, evidence or default-provider rewrite."""
    if not isinstance(provider, LlamaCppProvider):
        raise ValueError("Private reason-label experiment requires the native provider")
    generate = provider.generate_json

    async def generate_with_label(messages, schema, *, max_tokens):
        variant = deepcopy(schema)

        def visit(value):
            if isinstance(value, dict):
                reason = value.get("properties", {}).get("reason", {})
                if isinstance(reason.get("enum"), list):
                    reason["enum"] = [
                        "explicit_private_request" if item == "user_requested" else item
                        for item in reason["enum"]
                    ]
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(variant)
        raw = await generate(messages, variant, max_tokens=max_tokens)
        canonical = deepcopy(raw)
        if canonical.get("reason") == "explicit_private_request":
            canonical["reason"] = "user_requested"
        observations.append({"raw_model_decision": deepcopy(raw), "canonical_decision": deepcopy(canonical)})
        return canonical

    provider.generate_json = generate_with_label


def nonsecret(value):
    if isinstance(value, dict):
        return {key: nonsecret(item) for key, item in value.items() if not PRIVATE_KEY.search(key)}
    if isinstance(value, list):
        return [nonsecret(item) for item in value]
    return value


def validate_isolation(args):
    root, target = args.root.resolve(), args.runtime.resolve()
    production = {root / "runtime"}
    if os.getenv("TUBER_RUNTIME"):
        production.add(Path(os.environ["TUBER_RUNTIME"]).resolve())
    configured = Path(os.getenv("TUBER_CONFIG", str(root / "runtime/studio.json"))).resolve()
    production.add(configured.parent)
    if any(target == directory or directory.is_relative_to(target) for directory in production):
        raise ValueError(
            "--runtime must be a dedicated evaluation directory, never production or its ancestor"
        )
    if (target / "studio.sqlite3").exists():
        raise ValueError("--runtime contains a production studio.sqlite3")
    database = target / "evaluation.sqlite3"
    if database.is_symlink() or any(
        database.exists()
        and (directory / "studio.sqlite3").exists()
        and database.samefile(directory / "studio.sqlite3")
        for directory in production
    ):
        raise ValueError("Evaluation database must not link to a production database")
    protected = {
        configured,
        root / "config/studio.example.json",
        root / "config/characters.json",
        args.cases.resolve(),
        database,
    }
    for key in ("character_file", "inference_key_file"):
        if getattr(args, key, None):
            protected.add(getattr(args, key).resolve())
    if args.output.is_symlink() or args.output.resolve() in protected:
        raise ValueError("Report output must not overwrite configuration, inputs or database")


def evaluation_settings(args):
    validate_isolation(args)
    settings = Settings(root=args.root, runtime=args.runtime)
    settings.data = deepcopy(settings.data)
    explicit = {}
    for option, key in OVERRIDES.items():
        value = getattr(args, option, None)
        if value is not None:
            settings.data["llm"][key] = value
            explicit[option] = value
    thinking = getattr(args, "thinking", None)
    if thinking is not None:
        settings.data["llm"]["enable_thinking"] = thinking == "on"
        explicit["thinking"] = thinking
    if getattr(args, "temperature", None) is not None and (
        not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2
    ):
        raise ValueError("temperature must be finite and in 0..2")
    for option, low, high in (("top_p", 0, 1), ("min_p", 0, 1), ("presence_penalty", -2, 2)):
        value = getattr(args, option, None)
        if value is not None and (not math.isfinite(value) or not low <= value <= high):
            raise ValueError(f"{option} must be finite and in {low}..{high}")
    top_k = getattr(args, "top_k", None)
    if top_k is not None and (type(top_k) is not int or top_k < 0):
        raise ValueError("top_k must be a nonnegative integer")
    repeat = getattr(args, "repeat_penalty", None)
    if repeat is not None and (not math.isfinite(repeat) or repeat <= 0):
        raise ValueError("repeat_penalty must be finite and positive; 1 disables the penalty")
    if getattr(args, "model_base_url", None) is not None:
        settings.data["llm"]["kind"] = "llama_cpp"
        # A direct experiment endpoint must not retain production broker side effects.
        # An explicitly selected, existing broker endpoint keeps its authorized key path.
        if settings.broker_key_for(args.model_base_url) is None:
            settings.data.setdefault("inference_broker", {})["enabled"] = False
    settings._validate()
    if getattr(args, "inference_key_file", None):
        settings.inference_token = args.inference_key_file.read_text(encoding="utf-8-sig").strip()
    policy_values = {}
    if getattr(args, "decision_max_tokens", None) is not None:
        policy_values["decision_max_tokens"] = args.decision_max_tokens
        explicit["decision_max_tokens"] = args.decision_max_tokens
    elif getattr(args, "response_strategy", "combined") != "combined":
        policy_values["decision_max_tokens"] = 4096
    policy = BrowsingPolicy(**policy_values).model_dump()
    label = getattr(args, "private_request_label", "user_requested")
    if label != "user_requested" and (
        getattr(args, "response_strategy", "combined") != "combined"
        or settings.data["llm"]["kind"] != "llama_cpp"
    ):
        raise ValueError("Minimal private reason-label experiment requires combined native evaluation")
    strategy_options(args, policy["decision_max_tokens"])
    if policy["decision_max_tokens"] >= settings.data["llm"]["context_tokens"] * 95 // 100:
        raise ValueError("Decision output budget exceeds the effective context; budget was not changed")
    return settings, policy, explicit


def install_public_character(store, path):
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict) or not {"core", "style", "rules"} <= payload.keys():
        raise ValueError("Character file must contain public core/style/rules")
    public = {key: payload[key] for key in ("core", "style", "rules")}
    character = store.character()
    character.update(public)
    saved = store.save_character(character)
    return {
        "source_file": str(path.resolve()),
        "imported_fields": list(public),
        "public_content_sha256": hashlib.sha256(
            json.dumps(public, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "id": saved["id"],
        "version": saved["version"],
    }


async def evaluate(args):
    settings, config, explicit = evaluation_settings(args)
    strategy = getattr(args, "response_strategy", "combined")
    options = strategy_options(args, config["decision_max_tokens"])
    evaluate_staged = staged_evaluator() if options else None
    cases = json.loads(args.cases.read_text(encoding="utf-8-sig"))
    seeds = json.loads((args.root / "config/characters.json").read_text(encoding="utf-8"))
    store = Store(settings.runtime / "evaluation.sqlite3", seeds)
    character_info = {
        "id": store.character()["id"],
        "version": store.character()["version"],
        "source_file": None,
        "imported_fields": [],
    }
    try:
        if getattr(args, "character_file", None):
            character_info = install_public_character(store, args.character_file)
    except BaseException:
        store.close()
        raise
    runtime = Runtime(settings, store)
    label_observations = []
    private_label = getattr(args, "private_request_label", "user_requested")
    instruction = RESPONSE_INSTRUCTION
    if private_label == "explicit_private_request":
        instruction = instruction.replace("user_requested", private_label)
        install_private_label_experiment(runtime.llm, label_observations)
    comments = Comments(settings, store, runtime, adapter=object())
    messages = DirectMessages(settings, store, runtime, comments)
    dynamics = Dynamics(settings, store, runtime, comments)
    service = BrowsingService(settings, store, runtime, comments, messages, dynamics)
    service.account = "evaluation-only"
    service.ready_signature = service.credential_signature()
    llm = settings.data["llm"]
    request_audit = []
    generation_outputs = []

    async def observe_request(request):
        if request.url.path.rstrip("/").endswith("/chat/completions"):
            # Fingerprint the exact outgoing bytes and field order, retaining
            # neither messages, private source text nor model reasoning.
            value = json.loads(request.content)

            def encode(item):
                return json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()

            request_audit.append(
                {
                    "payload_sha256": hashlib.sha256(request.content).hexdigest(),
                    "messages_sha256": hashlib.sha256(encode(value.get("messages"))).hexdigest(),
                    "response_format_sha256": hashlib.sha256(
                        encode(value.get("response_format"))
                    ).hexdigest(),
                    "max_tokens": value.get("max_tokens"),
                }
            )

    async def observe_response(response):
        if not response.request.url.path.rstrip("/").endswith("/chat/completions"):
            return
        sent = json.loads(response.request.content)
        if sent.get("max_tokens", 0) <= 1:
            return
        await response.aread()
        try:
            value = response.json()
            content = value["choices"][0]["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return
        generation_outputs.append(
            {"http_status": response.status_code, "visible_output": visible_output(content)}
        )

    client = getattr(runtime.llm, "client", None)
    if client is not None:
        client.event_hooks["request"].append(observe_request)
        client.event_hooks["response"].append(observe_response)
    report = {
        "protocol": "browsing-semantics-v2-oc",
        "model": llm["model"],
        "enable_thinking": llm.get("enable_thinking"),
        "decision_max_tokens": config["decision_max_tokens"],
        "provider_class": type(runtime.llm).__name__,
        "llm_config": nonsecret(llm),
        "sampling": runtime.llm.sampling() if hasattr(runtime.llm, "sampling") else None,
        "loaded_instruction_hashes": {
            "response_instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "canonical_response_instruction_sha256": hashlib.sha256(
                RESPONSE_INSTRUCTION.encode()
            ).hexdigest(),
            "system_rules_sha256": hashlib.sha256(SYSTEM_RULES.encode()).hexdigest(),
        },
        "request_audit_scope": "Actual outgoing generation payload/messages/response-format SHA256 only; no message or reasoning text retained; field ordering preserved",
        "seed": llm.get("seed"),
        "explicit_overrides": explicit,
        "character": character_info,
        "response_strategy": strategy,
        "private_request_label": private_label,
        "private_label_scope": "Model-side reason literal only; same schema structure and branches; backend maps reason to canonical user_requested without rewriting action or evidence",
        "strategy_parameters": options,
        "context_contract": {
            "context_tokens": llm["context_tokens"],
            "native_context_tokens": llm.get("native_context_tokens", 131072),
            "native_context_source": "explicit_cli"
            if getattr(args, "native_context_tokens", None) is not None
            else "configured_model",
            "strict_context_tokens": STRICT_CONTEXT_TOKENS,
            "meets_strict_window": llm["context_tokens"] >= STRICT_CONTEXT_TOKENS,
            "native_meets_strict_window": llm.get("native_context_tokens", 131072) >= STRICT_CONTEXT_TOKENS,
        },
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "evaluation_scope": "Existing text-only ResponseDecision product schema; no image input",
        "review_policy": "Review OC interaction, factual accuracy and task completion from the visible reply. Action, literal-text and delivery checks are separate diagnostics; no aggregate rejection score or required disclaimer wording.",
        "timing_scope": "Per-case wall clock includes round setup, token counting, generation and validation; no TTFT measured",
        "platform_messages_sent": 0,
        "production_automation_changed": False,
        "production_configuration_written": False,
    }
    results = []
    try:
        for case in cases:
            started = time.perf_counter()
            audit_start = len(request_audit)
            label_start = len(label_observations)
            output_start = len(generation_outputs)
            runtime.llm.last_usage = {}
            runtime.llm.last_finish_reason = None
            runtime.llm.last_http_status = None
            row, _ = service.repo.create_round("evaluation-only", runtime.character, config, 0, True)
            try:
                if evaluate_staged is not None:
                    result = await evaluate_staged(service, row, case, **options)
                    result["expected"] = case["criterion"]
                    result["requires_human_fact_review"] = True
                    actual = result.get("actual")
                else:
                    decision = await service._model(
                        row,
                        ResponseDecision,
                        case["data"],
                        instruction,
                    )
                    actual = decision.model_dump()
                # A backend rejection retains its original model choice and error;
                # it must never become an apparently correct model skip.
                if actual is not None:
                    matches = actual["action"] in case["expected_actions"]
                    reference_ok = set(actual["evidence_ids"]) <= set(case["allowed_evidence_ids"])
                    if actual["action"] in {"reply_public", "handoff_private"}:
                        reference_ok = (
                            reference_ok and bool(actual["evidence_ids"]) and bool(actual["text"].strip())
                        )
                    text_ok = all(word not in actual["text"] for word in case.get("forbidden_text", []))
                    facets = {
                        "id": case["id"],
                        "structural_complete": True,
                        "action_matches": matches,
                        "references_valid": reference_ok,
                        "forbidden_text_absent": text_ok,
                        "actual": actual,
                        "expected": case["criterion"],
                        "requires_human_fact_review": True,
                    }
                    result = (result | facets) if evaluate_staged is not None else facets
            except Exception as error:
                result = {
                    "id": case["id"],
                    "structural_complete": False,
                    "error": str(error),
                    "expected": case["criterion"],
                    "requires_human_fact_review": True,
                }
            service.repo.finish_round(row["id"], "completed")
            result["budget"] = service.repo.round(row["id"])["budget"]
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000
            if evaluate_staged is None:
                result["usage"] = getattr(runtime.llm, "last_usage", {})
                result["finish_reason"] = getattr(runtime.llm, "last_finish_reason", None)
                result["generation_http_status"] = getattr(runtime.llm, "last_http_status", None)
            else:
                result["usage"], result["usage_complete"] = stage_usage(result)
            result["actual_server_context_tokens"] = getattr(runtime.llm, "server_tokens", None)
            result["generation_request_audit"] = request_audit[audit_start:]
            result["generation_outputs"] = generation_outputs[output_start:]
            result["evaluation_group"] = case.get("evaluation_group", "core")
            result["semantic_review"] = {
                "status": "pending",
                "oc_interaction": None,
                "factual_accuracy": None,
                "task_completion": None,
            }
            if result.get("actual"):
                result["reply_text_for_review"] = result["actual"].get("text", "")
            else:
                body = next(
                    (
                        s.get("raw_output")
                        for s in reversed(result.get("stages", []))
                        if s.get("stage") == "body"
                    ),
                    None,
                )
                result["reply_text_for_review"] = reply_for_review(
                    body
                    if evaluate_staged is not None
                    else (
                        generation_outputs[-1]["visible_output"]
                        if len(generation_outputs) > output_start
                        else ""
                    )
                )
            if private_label != "user_requested":
                result["reason_label_observations"] = label_observations[label_start:]
            results.append(result)
            write_report(args.output, report | {"results": results})
            print(
                json.dumps(
                    {
                        "case": case["id"],
                        "structural_complete": result["structural_complete"],
                        "action_matches": result.get("action_matches"),
                        "elapsed_ms": result["elapsed_ms"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        await service.close()
        await runtime.close()
        store.close()
    return report | {"results": results}


def arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inference-key-file", type=Path)
    parser.add_argument("--model-base-url")
    parser.add_argument("--model")
    parser.add_argument("--context-tokens", type=int)
    parser.add_argument("--native-context-tokens", type=int)
    parser.add_argument("--thinking", choices=("on", "off"))
    parser.add_argument("--decision-max-tokens", type=int)
    parser.add_argument("--character-file", type=Path)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--min-p", type=float)
    parser.add_argument("--presence-penalty", type=float)
    parser.add_argument("--repeat-penalty", "--repetition-penalty", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--private-request-label",
        choices=("user_requested", "explicit_private_request"),
        default="user_requested",
    )
    parser.add_argument(
        "--response-strategy",
        "--strategy",
        choices=("combined", "staged", "evidence_first"),
        default="combined",
    )
    parser.add_argument("--staged-decision-tokens", type=int, default=1024)
    parser.add_argument("--evidence-tokens", type=int, default=1024)
    parser.add_argument("--max-retries", type=int, choices=(0, 1), default=1)
    parser.add_argument("--source-guard-mode", choices=("necessary", "conservative"), default="necessary")
    parser.add_argument("--decision-character", choices=("full", "core_rules"), default="full")
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(evaluate(arguments()))
