"""Real Omni replay of private task-history recall and withdrawal.

Run: uv run --locked python -m evaluations.omni_task_history
Uses OMNI_GATEWAY_URL and an ordinary OMNI_GATEWAY_TOKEN, OMNI_STUDIO_TOKEN or OMNI_CORE_TOKEN_FILE.
"""

import asyncio
import json
import os
import re
import secrets
import tempfile
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from evaluations.omni_chat_loop import ROOT, ReplayMismatch, connection
from tuber_studio.agent.operator_chat import OperatorChat
from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.runtime_authority import acquire_business_writer

CHAT = "/api/chat/intelligent"
PREF = "/api/daily-memory/preferences"
QUESTION = "我上次为什么选那段？"
ORIGINAL = "**开场片段**\n选材理由：两声木鱼之间留出了报幕的节拍，主持人正好能在这里念出节目名。\n来源：<https://archive.example/opening>"
EDIT = "号主后续编辑：本版只保留尾声占位。"


def require(condition, check):
    if not condition:
        raise ReplayMismatch(check)


async def exercise(directory, root, token, env):
    report = {}
    phase = "before"
    wire = {"before": [], "gap": [], "after": []}
    denied = []
    clarified = set()
    send = httpx.AsyncHTTPTransport.handle_async_request
    gateway = httpx.URL(root)

    async def guarded(transport, request):
        url = request.url
        base = gateway.path.rstrip("/") + "/v1/"
        endpoint = url.path.removeprefix(base)
        permitted = ((request.method == "GET" and endpoint == "models") or
                     (request.method == "POST" and endpoint in {"chat/completions", "chat/prompt_tokens"}))
        if url.copy_with(path="/", query=None) != gateway.copy_with(path="/", query=None) or not (
            url.path.startswith(base) and permitted
        ):
            denied.append("transport")
            raise ReplayMismatch("unexpected_outbound_transport")
        if request.method == "POST":
            messages = json.loads(request.content)["messages"]
            payload = json.dumps(messages, ensure_ascii=False)
            wire[phase].append((endpoint, payload))
        return await send(transport, request)

    original_action = OperatorChat._action

    async def read_action(chat, intent, *args):
        if intent.action != "clarify":
            denied.append("domain_action")
            raise ReplayMismatch("unexpected_domain_action/" + intent.action)
        clarified.add(phase)
        return await original_action(chat, intent, *args)

    async with AsyncExitStack() as stack:
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", guarded))
        stack.enter_context(patch.object(OperatorChat, "_action", read_action))
        login = secrets.token_urlsafe(32)
        stack.enter_context(patch.dict(os.environ, {"TUBER_CONFIG": str(ROOT / "config/studio.example.json"),
                                                    "TUBER_ADMIN_TOKEN": login}))
        settings = Settings(runtime=directory)
        settings.data["llm"].update(base_url=root + "/v1", model=env.get("OMNI_MODEL_ALIAS", "omni-default"))
        settings.provider_keys = {"llm": token}
        def writer(path):
            lease = ExitStack()
            lease.enter_context(acquire_business_writer(path))
            stack.callback(lease.close)
            return lease

        # Hold the ordinary writer lock and release it at teardown without starting schedulers.
        with patch("tuber_studio.app.acquire_business_writer", writer):
            app = create_app(settings)
        runtime = app.state.runtime
        # ASGITransport leaves the business lifespan and every scheduler stopped.
        stack.callback(app.state.store.close)
        stack.push_async_callback(runtime.llm.close)
        stack.push_async_callback(app.state.comments.adapter.close)
        client = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("192.0.2.1", 3000)), base_url="http://studio.example",
            trust_env=False))
        require((await client.get(PREF)).status_code == 401, "authenticated_entry")
        (await client.post("/api/auth", json={"token": login})).raise_for_status()

        task_id = str(uuid4())
        task = runtime.tasks.create(task_id, "为下次节目留一段开场素材")
        with runtime.store.db as db:
            runtime.tasks.workspace.record_delivery(db, task, ORIGINAL)
        (await client.patch(f"/api/agent/tasks/{task_id}/artifact",
                            json={"task_revision": 1, "edition": 1, "body": EDIT})).raise_for_status()
        for index in range(51):
            runtime.tasks.create(str(uuid4()), f"清点备用素材 {index + 1}")
        runtime.browsing.discovery.remember_account("history-replay-owner")
        require(runtime.tasks.current(task_id) and not runtime.tasks.executable(task_id), "read_only_scope")
        require(task_id not in [t["id"] for t in (await client.get("/api/agent/tasks")).json()["tasks"]], "outside_activity")

        async def ask(question=QUESTION):
            identity = str(uuid4())
            response = await client.post(CHAT, json={"request_id": identity, "message": question})
            require(response.is_success, f"{phase}_chat_http_{response.status_code}")
            return identity, response.json()

        identity, answer = await ask()
        card = answer.get("card") or {}
        if answer.get("task_reference") != task_id:
            raise ReplayMismatch("missing_task_reference")
        require(card.get("type") == "history_artifact", "missing_edition_card")
        require(card["data"] == {"task_revision": 1, "edition": 1},
                "wrong_edition/" + str(card["data"].get("edition")))
        reply = answer["reply_text"]
        source = f"{CHAT}/turns/{identity}/artifact"
        original = (await client.get(source)).raise_for_status().json()
        require(original["body"] == ORIGINAL and original["author"] == "agent", "open_original")
        require(answer["reply_format"] == "plain" and reply == "角色交付稿 r1 v1原文\n第 2 行：" +
                original["body"].splitlines()[1], "verbatim_reason_only")
        require(any(path == "chat/completions" and "两声木鱼" in payload for path, payload in wire["before"]),
                "original_reached_real_model")
        report["before"] = {"task_revision": 1, "edition": 1, "reply": reply}

        phase = "gap"
        gap_id, gap = await ask(f"只看任务 {task_id} 的 r1 v2，当时为什么这样选？")
        gap_source = (await client.get(f"{CHAT}/turns/{gap_id}/artifact")).raise_for_status().json()
        require(gap_source["edition"] == 2 and gap_source["body"] == EDIT, "gap_edition")
        if gap["reply_text"] != "号主编辑稿 r1 v2里没有可核对的对应理由。" or gap["reply_format"] != "plain":
            return {"status": "failed", "check": "missing_reason_gap", "reply": gap["reply_text"],
                    "reply_format": gap["reply_format"]}
        require(any(p == "chat/completions" and EDIT in body for p, body in wire[phase])
                and all("两声木鱼" not in body for _, body in wire[phase]), "gap_isolation")
        report["gap"] = {"edition": 2, "reply": gap["reply_text"]}

        pref = (await client.get(PREF)).raise_for_status().json()
        result = (await client.put(PREF, json={"enabled": pref["enabled"], "scope": pref["scope"],
                                               "history_enabled": False})).raise_for_status().json()
        require(result["history_enabled"] is False, "withdraw_history")
        phase = "after"
        status = (await client.get(source)).status_code
        require(status == 404, "withdrawn_source_denied")
        historical = (await client.get(f"{CHAT}/turns/{identity}")).raise_for_status().json()
        require(not historical.get("card") and "木鱼" not in historical["reply_text"], "withdrawn_answer_hidden")
        _, answer = await ask()
        reply = answer["reply_text"]
        require(not answer.get("task_reference") and not answer.get("card") and reply.strip(), "withdrawn_citation")
        require(any(p == "chat/completions" for p, _ in wire["after"]), "second_real_model_call")
        for secret in ("木鱼", "报幕", "archive.example", task_id, "尾声占位"):
            require(secret not in reply and all(secret not in payload for _, payload in wire["after"]), "withdrawn_material")
        if phase not in clarified and not re.search(
            r"历史|没.{0,8}(?:记录|依据|看到|材料)|无法|不清楚|看不到|哪一?[段件个份]|提供.{0,6}(?:线索|材料)", reply
        ):
            return {"status": "failed", "check": "missing_evidence_not_acknowledged",
                    "reply": reply, "platform_writes": 0}
        require(not denied and runtime.store.one("SELECT count(*) AS n FROM agent_tasks")["n"] == 52, "zero_side_effects")
        report["after"] = {"source_status": status, "referenced": False, "reply": reply}
        return report | {"status": "passed", "platform_writes": 0}


async def run(env):
    if not (env.get("OMNI_STUDIO_TOKEN") or env.get("OMNI_CORE_TOKEN_FILE")):
        env = {**env, "OMNI_STUDIO_TOKEN": env.get("OMNI_GATEWAY_TOKEN")}
    config = connection(env)
    if not config:
        return {"status": "skipped", "reason": "missing_studio_service_configuration"}
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="omni-history-") as directory:
        async with asyncio.timeout(600):
            return await exercise(Path(directory), *config, env)


def main():
    try:
        report = asyncio.run(run(os.environ))
    except ReplayMismatch as error:
        report = {"status": "failed", "check": str(error)}
    except httpx.HTTPStatusError as error:
        report = {"status": "failed", "http_status": error.response.status_code}
    except Exception as error:
        report = {"status": "failed", "category": type(error).__name__}
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
