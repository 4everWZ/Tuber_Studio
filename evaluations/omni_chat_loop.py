"""Run with uv run --locked python -m evaluations.omni_chat_loop.

Inject OMNI_GATEWAY_URL and OMNI_STUDIO_TOKEN or OMNI_CORE_TOKEN_FILE (Studio
application credential only). Optional aliases: OMNI_MODEL_ALIAS and
OMNI_LLM_MODEL_ALIAS. No deployment discovery, platform clients or business DB.
"""

import argparse
import asyncio
import json
import os
import tempfile
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from tuber_studio.api_providers import APIProvider
from tuber_studio.config import validate_api_key
from tuber_studio.deployment_profiles import operation_profile
from tuber_studio.http_api import endpoint
from tuber_studio.models import StrictModel
from tuber_studio.responses_provider import ResponsesClient
from tuber_studio.store import Store

ROOT = Path(__file__).resolve().parents[1]
REQUEST = "替我核实演播间现在有没有开播，留份私下的交接便笺，等我回来再看。"
CHANGE = "刚才那份便笺换个重点：不用看演播间了，只核对自主运营有没有启动，还是只留给我。"


class NoArguments(StrictModel):
    pass


class ReplayMismatch(Exception):
    pass


def connection(env):
    root = env.get("OMNI_GATEWAY_URL")
    token, file = env.get("OMNI_STUDIO_TOKEN"), env.get("OMNI_CORE_TOKEN_FILE")
    if not root or not (token or file):
        return None
    if token and file:
        raise ValueError("Choose one Studio credential source")
    root = endpoint(root)
    if file:
        try:
            token = Path(file).read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            return None
        if not token:
            return None
    validate_api_key(token)
    return root, token


async def exercise(store, omni, operations, responses, *, token, explicit=False):
    from tuber_studio.agent.operator_chat import ChatIntent, ChatTurn, OperatorChat
    from tuber_studio.agent.task_runtime import Tool
    from tuber_studio.agent.task_tools import create_task_runtime

    class IsolatedChat(OperatorChat):
        async def _action(self, intent, *args):
            if intent.action not in {"agent_task", "steer_task", "clarify"}:
                raise ReplayMismatch(f"unexpected_chat_action/{intent.action}")
            return await super()._action(intent, *args)

    seeds = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    character = next(seed for seed in seeds if seed["id"] == "whale") | {"version": 1}
    runtime = SimpleNamespace(
        store=store, character=character, llm=omni, operation_llm=operations, operator_generation=nullcontext,
        offline_generation=nullcontext,
        settings=SimpleNamespace(data={"omni": omni.config}, responses_config=responses.config.model_dump(),
                                 provider_key=lambda name: token if name == "omni" else None),
        daily_memory=SimpleNamespace(preference=lambda scope: {"enabled": False}),
        context=SimpleNamespace(record=lambda *args, **kwargs: None),
        validate_memory_delivery=lambda *args, **kwargs: None,
    )
    browsing = SimpleNamespace(discovery=SimpleNamespace(history_scope=lambda: ("rehearsal", "whale", 1)))
    tasks = runtime.tasks = create_task_runtime(runtime, store, browsing)
    tasks.tools.clear()
    runtime.memory_organizer = SimpleNamespace(scope=tasks.scope, schedule=lambda: None)
    chat = IsolatedChat(runtime, store, browsing, SimpleNamespace())
    reads = []

    def local_snapshot(name, description, state):
        async def read(request, arguments):
            reads.append(name)
            return {"state": state}

        tasks.register(name, Tool(description, NoArguments, read, reusable=True))

    local_snapshot("operation_status", "读取自主运营状态快照", "off")
    local_snapshot("live_status", "读取演播间的直播状态快照", "stopped")
    tasks.responses_client = lambda: responses.create
    identity = uuid4()
    for message, revision in ((REQUEST, 1), (CHANGE, 2)):
        intent = None
        if explicit:
            intent = (ChatIntent(action="agent_task") if revision == 1
                      else ChatIntent(action="steer_task", reference=str(identity)))
        result = await chat.send(ChatTurn(request_id=identity if revision == 1 else uuid4(), message=message,
                                          intent=intent))
        card = result.get("card") or {}
        task = card.get("data", {})
        if (card.get("type"), task.get("id"), task.get("revision")) != ("agent_task", str(identity), revision):
            raise ReplayMismatch("delegate" if revision == 1 else "steer")
    if CHANGE not in task["goal"] or len(tasks.activity()) != 1:
        raise ReplayMismatch("revised_goal")
    tasks.start()
    try:
        while (task := tasks.current(str(identity)))["status"] in {"queued", "waiting", "running", "scheduled"}:
            await asyncio.sleep(0.1)
        plan = task["plan"] or {}
        if ("candidate" not in plan or reads != ["operation_status"]
                or len(task["steps"]) != 1 or not task["steps"][0].get("call_id")
                or task["steps"][0].get("result") != {"state": "off"}):
            raise ReplayMismatch("responses_delivery")
        evidence_id = task["steps"][0]["id"]
        selected = plan.get("selected", {}).get("items", [])
        edition = tasks.workspace.latest(str(identity))
        if (task["status"] != "completed" or "未启动" not in task["message"] or not selected
                or any(set(item["sources"]) != {evidence_id} for item in selected)
                or not edition or edition["task_revision"] != 2 or edition["body"] != task["message"]):
            raise ReplayMismatch("omni_evidence_edit")
        return {"status": "passed", "task_revision": task["revision"], "local_reads": reads, "platform_writes": 0}
    finally:
        await tasks.close()


async def run(env, *, explicit=False):
    config = connection(env)
    if config is None:
        return {"status": "skipped", "reason": "missing_studio_service_configuration"}
    root, token = config
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    async with AsyncExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="omni-chat-", dir=scratch))
        store = Store(Path(directory) / "rehearsal.sqlite3", [])
        stack.callback(store.close)
        omni = APIProvider({"kind": "openai_compatible", "base_url": root + "/v1",
                            "model": env.get("OMNI_MODEL_ALIAS", "omni-default"),
                            "context_source": "model_card", "prompt_token_endpoint": True,
                            "structured_output": "structural_tag", "max_tokens": 4096}, token)
        stack.push_async_callback(omni.close)
        responses = await stack.enter_async_context(ResponsesClient(
            {"base_url": root + "/llm/v1", "model": env.get("OMNI_LLM_MODEL_ALIAS", "llm-default")}, token))
        operations = APIProvider(operation_profile(omni.config | {"responses_model": responses.config.model}),
                                 token, background=True)
        stack.push_async_callback(operations.close)
        async with asyncio.timeout(300):
            return await exercise(store, omni, operations, responses, explicit=explicit, token=token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", choices=("chat", "quick-action"), default="chat")
    entry = parser.parse_args().entry
    try:
        report = asyncio.run(run(os.environ, explicit=entry == "quick-action"))
    except ReplayMismatch as error:
        report = {"status": "failed", "check": str(error)}
    except Exception as error:
        report = {"status": "failed", "category": type(error).__name__}
    print(json.dumps(report | {"entry": entry}, ensure_ascii=False))
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
