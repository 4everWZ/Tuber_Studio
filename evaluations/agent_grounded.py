"""Replay the application Agent with a real model and isolated Chinese tool receipts."""

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from tuber_studio.agent.task_tools import create_task_runtime
from tuber_studio.core.store import Store
from tuber_studio.infra.inference.api_providers import APIProvider
from tuber_studio.infra.inference.http_api import response_json
from tuber_studio.infra.inference.providers import public_error

ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "evaluations/cases/agent-grounded-v1.json"
TERMINAL = {"completed", "failed", "needs_input", "awaiting_approval", "cancelled"}
AWQ_CHECKPOINT = "927faefa0bce96e557945e106f3b3d8b4d39f4b2"
AWQ_WINDOW = 40960


class ReplayWindowClosed(TimeoutError):
    pass


def replay_tool(case, name, calls):
    fixtures = case["fixtures"].get(name, [])

    async def run(request, arguments):
        index = calls[name]
        calls[name] += 1
        if index >= len(fixtures):
            return {"error": "没有对应的隔离回执。"}
        fixture = fixtures[index]
        if fixture.get("arguments", arguments.model_dump()) != arguments.model_dump():
            return {"error": "工具参数与隔离来源不符。"}
        return fixture["result"]

    return run


def replay_approval(case, name):
    def review(arguments):
        if case.get("approval") != {"tool": name, "arguments": arguments.model_dump()}:
            raise ValueError("本次隔离任务不允许该拟议动作")
        return {"target": "模拟农场", "guard": "隔离审批夹具"}

    return review


def requirements(row, case, calls, plans):
    expected = case["expected"]
    steps = row["steps"]
    actual_tools = [step["tool"] for step in steps]
    expected_calls = Counter(expected["tools"])
    if proposal := case.get("approval"):
        expected_calls[proposal["tool"]] -= 1
    errors = []
    if row["status"] != expected["status"]:
        errors.append(f"状态为 {row['status']}，预期 {expected['status']}")
    if actual_tools != expected["tools"] or calls != expected_calls:
        errors.append(f"步骤或执行范围不符，实际步骤 {actual_tools}，实际执行 {dict(calls)}")
    for name, fixtures in case["fixtures"].items():
        receipts = [step.get("result") for step in steps if step["tool"] == name]
        if receipts != [fixture["result"] for fixture in fixtures]:
            errors.append(f"{name} 的回执不符")
    if proposal and (not steps or not steps[-1].get("approval") or "result" in steps[-1]):
        errors.append("拟议动作未停在等待审批")
    for text in expected["text"]:
        if text not in row["message"]:
            errors.append(f"序列化结果缺少依据 {text}")
    conditions = {step["tool"]: step.get("condition") for step in plans[-1]["steps"]} if plans else {}
    for tool, condition in expected.get("conditions", {}).items():
        if conditions.get(tool) != condition:
            errors.append(f"{tool} 未绑定用户要求的回执条件")
    return errors


@asynccontextmanager
async def allow_generation(lane):
    yield


async def run_case(db, provider, case, timeout):
    store = Store(db, [])
    runtime = SimpleNamespace(llm=provider, operation_llm=provider, operator_generation=allow_generation, character={"_edit_identity": "eval"})
    browsing = SimpleNamespace(discovery=SimpleNamespace(history_scope=lambda: ("fixture", "fixture", 1)))
    task = create_task_runtime(runtime, store, browsing)
    calls, plans, outputs, failures = Counter(), [], [], []
    decide = task.decide

    async def record(stage, callback, items, *args):
        try:
            decision = await callback(*args)
        except Exception as error:
            failures.append({"stage": stage, "error": type(error).__name__, "detail": public_error(error),
                             "finish_reason": getattr(provider, "last_finish_reason", None),
                             "content": getattr(provider, "last_content", None)})
            raise
        items.append(decision.model_dump())
        return decision

    task.decide = lambda *args: record("step", decide, outputs, *args)
    if make_plan := getattr(task, "make_plan", None):
        task.make_plan = lambda *args: record("plan", make_plan, plans, *args)
    for name, tool in task.tools.items():
        task.tools[name] = replace(tool, run=replay_tool(case, name, calls), stop=None,
                                   review=replay_approval(case, name) if tool.review else None)
    identity = str(uuid4())
    started = time.monotonic()
    task.start()
    try:
        task.create(identity, case["goal"])
        try:
            async with asyncio.timeout(timeout):
                while (row := task.current(identity))["status"] not in TERMINAL:
                    await asyncio.sleep(0.1)
        except TimeoutError:
            row = task.current(identity)
        return {"id": case["id"], "elapsed_seconds": round(time.monotonic() - started, 1),
                "status": row["status"], "message": row["message"], "steps": row["steps"],
                "model_plans": plans, "model_decisions": outputs, "model_failures": failures,
                "errors": requirements(row, case, calls, plans)}
    finally:
        await task.close()
        store.close()


async def replay(args, cases):
    config = {"base_url": args.endpoint, "model": args.model, "structured_output": "json_schema",
              "context_tokens": args.context_tokens, "native_context_tokens": args.context_tokens,
              "temperature": 0.2, "prefill_timeout_seconds": 110}
    if args.official_awq:
        config.update(context_source="model_card", prompt_token_endpoint=True, max_tokens=4096, temperature=0.6, top_k=20,
                      top_p=0.95, enable_thinking=True)
    token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else None
    provider = APIProvider(config, token)
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    deadline = time.monotonic() + args.total_timeout if args.total_timeout is not None else float("inf")
    try:
        if args.official_awq:
            health = await response_json(provider.client, "GET", provider.client.base_url.copy_with(path="/healthz"))
            await provider.ensure_capacity(refresh=True)
            if (health.get("checkpoint") != AWQ_CHECKPOINT or health.get("checkpoint_root_verified") is not True
                    or provider.server_tokens != AWQ_WINDOW):
                raise ValueError("未确认官方 AWQ checkpoint 和 40960 窗口，未发生成请求。")
        with tempfile.TemporaryDirectory(dir=scratch, prefix="grounded-eval-") as directory:
            results, requests = [], []

            async def note_request(request):
                if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
                    return
                now = time.time()
                if args.dispatch_deadline is not None and now >= args.dispatch_deadline:
                    raise ReplayWindowClosed("隔离流量窗口已结束，未发出下一次推理请求。")
                requests.append(now)

            provider.client.event_hooks["request"].append(note_request)
            for index, case in enumerate(cases):
                remaining = min(args.timeout, deadline - time.monotonic())
                if remaining <= 0:
                    break
                requests.clear()
                result = await run_case(Path(directory) / f"case-{index}.sqlite3", provider, case, remaining)
                result["request_epochs"] = list(requests)
                if any(failure["error"] == "ReplayWindowClosed" for failure in result["model_failures"]):
                    result["incomplete_reason"] = "dispatch_window_closed"
                results.append(result)
                if (result["status"] not in TERMINAL
                        or result.get("incomplete_reason")
                        or any("timeout" in failure["error"].lower() for failure in result["model_failures"])):
                    break
            return results
    finally:
        await provider.close()


if __name__ == "__main__":
    cases = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Candidate gateway OpenAI-compatible /v1 base URL")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--model", default="omni-default")
    parser.add_argument("--context-tokens", type=int)
    parser.add_argument("--official-awq", action="store_true", help="Require the official AWQ/40960 candidate with thinking enabled")
    parser.add_argument("--timeout", type=float, default=330, help="Per-case wall time in seconds")
    parser.add_argument("--total-timeout", type=float, help="Wall time for the whole serial replay")
    parser.add_argument("--dispatch-deadline", type=float, help="Do not issue new HTTP requests after this Unix epoch")
    parser.add_argument("--case", action="append", choices=[case["id"] for case in cases])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.official_awq and arguments.context_tokens not in (None, AWQ_WINDOW):
        parser.error("the official AWQ candidate requires a 40960-token model card")
    if arguments.context_tokens is None:
        arguments.context_tokens = AWQ_WINDOW if arguments.official_awq else 12288
    if arguments.timeout <= 0 or (arguments.total_timeout is not None and arguments.total_timeout <= 0):
        parser.error("timeout values must be positive")
    selected = [case for case in cases if not arguments.case or case["id"] in arguments.case]
    results = asyncio.run(replay(arguments, selected))
    report = {"suite": CASE_PATH.stem, "model_alias": arguments.model, "context_tokens": arguments.context_tokens,
              "passed": sum(not item["errors"] for item in results), "results": results,
              "not_run": [case["id"] for case in selected[len(results):]]}
    if arguments.official_awq:
        report.update(checkpoint=AWQ_CHECKPOINT, thinking=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['passed']}/{len(selected)} cases passed")
    raise SystemExit(report["passed"] != len(selected))
