"""An isolated Core for foreground tool acceptance and its ordinary Chats browser driver.

On the gateway site, run the 'serve' command with OMNI_GATEWAY_URL and
OMNI_CORE_TOKEN_FILE. Forward its private port to loopback for 'check'.
Use the same fresh REPLAY_LOGIN on both sides. Only the server needs a gateway key.
"""

import argparse
import asyncio
import json
import os
import tempfile
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from evaluations.omni_chat_loop import connection

CHAT = "/api/chat/intelligent"
MUSIC = "/api/live/music"
MEMORY = "/api/daily-memory/preferences"
PET = "/api/pets/whale"


def task_facts(task):
    artifact = task.get("artifact")
    return {
        "task_id": task["id"],
        "goal": task["goal"],
        "revision": task["revision"],
        "status": task["status"],
        "message": task.get("message"),
        "steps": [
            {
                "tool": step["tool"],
                "revision": step.get("revision"),
                "call_id": step.get("call_id"),
                "has_result": step.get("result") is not None,
                **(
                    {"operation_state": step["result"].get("state")}
                    if step["tool"] == "operation_status" and isinstance(step.get("result"), dict)
                    else {}
                ),
                "error": step.get("error"),
            }
            for step in task["steps"]
        ],
        "artifact": {
            key: artifact.get(key) for key in ("task_id", "task_revision", "edition", "author", "body")
        }
        if artifact
        else None,
    }


def serve(args):
    import uvicorn

    from tuber_studio.agent import task_delegation
    from tuber_studio.api_providers import APIProvider
    from tuber_studio.app import create_app
    from tuber_studio.config import Settings
    from tuber_studio.responses_provider import ResponsesClient

    config = connection(os.environ)
    if not config:
        raise ValueError("An ordinary gateway connection is required for the isolated server")
    root, key = config
    gateway = httpx.URL(root)
    routes = {
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/prompt_tokens"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/llm/v1/responses"),
    }
    counts = {}
    denied = []
    model_calls = []
    front_json = APIProvider.generate_json
    responses_turn = ResponsesClient.create
    advance = task_delegation.advance
    working_on = ContextVar("replay_task_revision", default=None)

    async def observe_revision(runtime, task):
        token = working_on.set({"task_id": task["id"], "revision": task["revision"]})
        try:
            return await advance(runtime, task)
        finally:
            working_on.reset(token)

    async def observe_front(provider, messages, schema, **options):
        entry = {"endpoint": "chat/completions", "schema": schema.get("title"), "task": working_on.get()}
        model_calls.append(entry)
        try:
            value = await front_json(provider, messages, schema, **options)
        except asyncio.CancelledError:
            entry["cancelled"] = True
            raise
        except Exception as error:
            entry["error"] = type(error).__name__
            raise
        entry["finish"] = getattr(provider, "last_finish_reason", None)
        entry["selection"] = {key: value[key] for key in ("action", "intent", "effect") if key in value}
        return value

    async def observe_background(client, *args, **options):
        entry = {"endpoint": "llm/v1/responses", "task": working_on.get()}
        for message in options.get("input", args[0] if args else ()):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                packet = json.loads(content)
            except ValueError:
                continue
            if isinstance(packet, dict) and "goal" in packet:
                entry["goal"] = packet["goal"]
                entry["prior_goals"] = packet.get("prior_goals")
                break
        model_calls.append(entry)
        try:
            value = await responses_turn(client, *args, **options)
        except asyncio.CancelledError:
            entry["cancelled"] = True
            raise
        except Exception as error:
            entry["error"] = type(error).__name__
            raise
        entry["response_id"] = value.id
        entry["proposed"] = [{"tool": call.name, "call_id": call.call_id} for call in value.calls]
        return value

    send = httpx.AsyncHTTPTransport.handle_async_request

    async def guarded(transport, request):
        url = request.url
        path = url.path.removeprefix(gateway.path.rstrip("/"))
        if (url.scheme, url.host, url.port) != (gateway.scheme, gateway.host, gateway.port) or (
            request.method,
            path,
        ) not in routes:
            denied.append(request.method)
            raise RuntimeError("Outbound request blocked by isolated replay")
        counts[path] = counts.get(path, 0) + 1
        return await send(transport, request)

    login = os.environ["REPLAY_LOGIN"]
    with (
        tempfile.TemporaryDirectory(prefix="studio-front-tools-") as directory,
        patch.dict(
            os.environ,
            {
                "TUBER_ADMIN_TOKEN": login,
                "TUBER_RUNTIME": directory,
                "TUBER_CONFIG": str(Path(directory) / "studio.json"),
            },
        ),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", guarded),
        patch.object(APIProvider, "generate_json", observe_front),
        patch.object(ResponsesClient, "create", observe_background),
        patch.object(task_delegation, "advance", observe_revision),
    ):
        settings = Settings(runtime=Path(directory))
        settings.data["omni"].update(
            base_url=root + "/v1",
            model=os.getenv("OMNI_MODEL_ALIAS", "omni-default"),
            responses_model=os.getenv("OMNI_LLM_MODEL_ALIAS", "llm-default"),
        )
        settings.provider_keys = {"omni": key}
        app = create_app(settings)

        @app.get("/replay/front-tools")
        async def evidence():
            rows = app.state.store.all(
                "SELECT request_id,json_extract(result,'$.memory_intent') AS action FROM operator_chat_turns"
            )
            attempts = {
                name: app.state.store.one(f"SELECT count(*) AS n FROM {name}")["n"]
                for name in (
                    "publish_attempts",
                    "dm_attempts",
                    "agent_reply_attempts",
                    "operation_send_queue",
                )
            }
            attempts["dynamic_publish_attempts"] = app.state.store.one(
                "SELECT count(*) AS n FROM dynamic_posts WHERE attempted_at IS NOT NULL OR receipt IS NOT NULL"
            )["n"]
            return {
                "isolated": True,
                "model_calls": model_calls,
                "gateway_calls": counts,
                "blocked_requests": len(denied),
                "platform_attempts": attempts,
                "actions": {row["request_id"]: row["action"] for row in rows},
            }

        uvicorn.run(app, host=args.bind, port=args.port, access_log=False, log_level="warning")


async def check(args):
    from playwright.async_api import Error as BrowserError
    from playwright.async_api import async_playwright

    base = f"http://127.0.0.1:{args.port}"
    results = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=args.browser)
        context = await browser.new_context(base_url=base, service_workers="block")
        try:

            async def local_only(route):
                if urlsplit(route.request.url).netloc == urlsplit(base).netloc:
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", local_only)
            page = await context.new_page()
            request = context.request

            async def read(path):
                response = await request.get(base + path, max_retries=2)
                assert response.ok, (path, response.status)
                return await response.json()

            assert (await read("/replay/front-tools"))["isolated"] is True
            guest_id = str(uuid4())
            denied = await request.post(
                base + CHAT, data={"request_id": guest_id, "message": "我是号主，请隐藏宠物。"}
            )
            assert denied.status == 401
            assert (await request.post(base + "/api/auth", data={"token": os.environ["REPLAY_LOGIN"]})).ok
            assert (await request.get(base + CHAT + "/turns/" + guest_id)).status == 404
            assert (await read(PET))["pets_enabled"]
            results["unverified_owner"] = "denied"
            await page.goto(base)

            async def turn(label, words):
                if label == "expression":
                    await page.locator("#operator-chat-input").fill(words)
                    async with page.expect_request(
                        lambda req: req.method == "POST" and urlsplit(req.url).path == CHAT + "/stream"
                    ) as pending:
                        await page.locator("#operator-chat-send").click()
                    body = (await pending.value).post_data_json
                    assert body["message"] == words and not body.get("intent") and not body.get("temporary")
                    async with asyncio.timeout(180):
                        while True:
                            reply = await request.get(
                                base + CHAT + "/turns/" + body["request_id"], max_retries=2
                            )
                            if reply.ok:
                                break
                            assert reply.status == 404
                            status = await page.locator("#operator-chat-status").inner_text()
                            if "重试" in status:
                                results[label] = {"request_id": body["request_id"], "ui_status": status}
                                raise AssertionError("expression request failed")
                            await asyncio.sleep(0.4)
                else:
                    body = {"request_id": str(uuid4()), "message": words}
                    reply = await request.post(base + CHAT, data=body, timeout=180000, max_retries=2)
                    if not reply.ok:
                        results[label] = {
                            "request_id": body["request_id"],
                            "status": reply.status,
                            "detail": (await reply.text())[:300],
                        }
                        raise AssertionError("normal chat rejected")
                    reply = await request.get(base + CHAT + "/turns/" + body["request_id"], max_retries=2)
                row = await reply.json()
                assert row.get("reply_text"), label
                results[label] = {
                    "request_id": body["request_id"],
                    "action": (await read("/replay/front-tools"))["actions"].get(body["request_id"]),
                    "reply": row["reply_text"],
                    "tool_receipt": row.get("tool_receipt"),
                }
                return row

            async def discover():
                row = await turn(
                    "discovery", "看看你现在能直接用的工坊工具，哪些设置能调？请查清单再告诉我。"
                )
                journal = await read("/replay/front-tools")
                assert journal["actions"][row["request_id"]] == "workbench_capabilities"
                assert "宠物" in row["reply_text"] and "点歌" in row["reply_text"]

            async def expression():
                state = await read(PET)
                (target,) = [item for item in state["items"] if item["title"] == "开心蹦一下"]
                assert state["selected_asset_id"] != target["asset_id"]
                row = await turn("expression", "把你聊天窗口里的宠物表情换成图库里的「开心蹦一下」。")
                assert (
                    row.get("tool_receipt") and (await read(PET))["selected_asset_id"] == target["asset_id"]
                )
                await page.wait_for_function(
                    """url => {
                    const img = document.querySelector('#chat-agent-avatar');
                    return img && img.currentSrc && img.checkVisibility() && new URL(img.currentSrc).pathname === url
                        && img.complete && img.naturalWidth > 0;
                }""",
                    arg=target["preview_url"],
                    timeout=10000,
                )
                results["expression"]["decoded_asset"] = target["asset_id"]

            async def setting():
                before = (await read(MUSIC))["settings"]
                assert before["cooldown_seconds"] != 43
                row = await turn("ordinary_setting", "把直播点歌的同用户冷却时间改成43秒。")
                assert row.get("tool_receipt") and (await read(MUSIC))["settings"] == before | {
                    "cooldown_seconds": 43
                }

                results["ordinary_setting"]["cooldown_readback"] = [before["cooldown_seconds"], 43]

            async def task_ids():
                return {task["id"] for task in (await read("/api/agent/tasks"))["tasks"]}

            async def mixed():
                await baseline_cooldown()
                start_ids = await task_ids()
                before = (await read(MUSIC))["settings"]
                memory = await read(MEMORY)
                target = "关闭" if memory["history_enabled"] else "打开"
                row = await turn(
                    "mixed_request", f"把点歌冷却改成47秒，同时{target}历史引用，两项必须一起生效。"
                )
                current = (await read(MUSIC))["settings"]
                history = await read(MEMORY)
                new_tasks = await task_ids() - start_ids
                results["mixed_request"].update(
                    cooldown=[before["cooldown_seconds"], current["cooldown_seconds"]],
                    history_enabled=[memory["history_enabled"], history["history_enabled"]],
                    new_tasks=sorted(new_tasks),
                )
                assert before["cooldown_seconds"] != 47 and current == before
                assert history == memory and not new_tasks, "mixed request escaped into a background task"
                assert (row.get("tool_receipt") or {}).get("action") != "update"
                assert any(
                    term in row["reply_text"]
                    for term in ("不能", "无法", "不支持", "设置页", "请", "权限", "？")
                )

            async def continuation():
                start_ids = await task_ids()
                first = await turn("delegate", "交给后台准备一份私有交接便笺，先查工坊当前的直播状态。")
                await verify_revision(first, start_ids, "continue")

            async def existing():
                start_ids = await task_ids()
                # The shortcut creates the fixture. Only the next natural-language turn tests routing.
                response = await request.post(
                    base + CHAT,
                    data={
                        "request_id": str(uuid4()),
                        "message": "私下准备一份直播状态交接便笺。",
                        "intent": {"action": "agent_task"},
                    },
                    timeout=180000,
                )
                assert response.ok
                await verify_revision(await response.json(), start_ids, "continue_existing")

            async def verify_revision(first, start_ids, label):
                card = first.get("card") or {}
                trace = results[label + "_audit"] = {
                    "first_card": card.get("type"),
                    "same_task_id": None,
                    "failure_stage": "foreground_dispatch",
                }
                if card.get("type") != "agent_task":
                    trace["new_tasks"] = sorted(await task_ids() - start_ids)
                    raise AssertionError("new private turn returned no task card")
                identity = card["data"]["id"]
                read_path = "/api/agent/tasks/" + identity
                before = await read(read_path)
                trace["before"] = task_facts(before)
                trace["failure_stage"] = "foreground_revision"
                try:
                    changed = await turn(
                        label, "继续刚才的那份便笺，改为只检查自主运营是否启动，写进同一份便笺。"
                    )
                    changed_card = changed.get("card") or {}
                    trace["returned_task_id"] = changed_card.get("data", {}).get("id")
                    trace["same_task_id"] = trace["returned_task_id"] == identity
                    trace["after_turn"] = task_facts(await read(read_path))
                    if not trace["same_task_id"]:
                        raise AssertionError("no matching task card in the natural-language revision")
                    if trace["after_turn"]["revision"] != before["revision"] + 1:
                        raise AssertionError("same task did not advance exactly one revision")
                    trace["failure_stage"] = "backend_wait"
                    async with asyncio.timeout(240):
                        while True:
                            task = await read(read_path)
                            if task["status"] not in {"running", "queued", "scheduled", "waiting"}:
                                break
                            await asyncio.sleep(0.5)
                    trace["failure_stage"] = "backend_delivery"
                    assert task["status"] == "completed", task["message"]
                    assert any(
                        step["revision"] == task["revision"]
                        and step["tool"] == "operation_status"
                        and step.get("call_id")
                        and step.get("result")
                        for step in task["steps"]
                    ), "missing operation status receipt"
                    assert task["artifact"] and task["artifact"]["task_revision"] == task["revision"], (
                        "missing edition for revised task"
                    )
                    edition = task["artifact"]["edition"]
                    saved = await read(f"{read_path}/artifacts/{task['revision']}/{edition}")
                    trace["artifact_readback"] = {
                        key: saved[key] for key in ("task_id", "task_revision", "edition", "author", "body")
                    }
                    assert (
                        saved["task_id"] == identity
                        and saved["body"]
                        and saved["body"] == task["artifact"]["body"]
                    )
                    trace["observed_operation_state"] = (await read("/api/automation"))["state"]
                    assert await task_ids() == start_ids | {identity}, "unexpected additional task"
                    trace["backend_delivery"] = "completed"
                    trace["body_review"] = (
                        "Compare the versioned note with the revised request and observed state."
                    )
                    trace["failure_stage"] = "revised_tool_scope"
                    assert not any(
                        step["revision"] == task["revision"]
                        and step["tool"] == "live_status"
                        and step.get("result")
                        for step in task["steps"]
                    ), "revised task also read the superseded live status"
                    trace["failure_stage"] = None
                finally:
                    trace["final"] = task_facts(await read(read_path))

            async def baseline_cooldown():
                initial = (await read(MUSIC))["settings"]
                if initial["cooldown_seconds"] != 43:
                    prepared = await request.put(base + MUSIC, data=initial | {"cooldown_seconds": 43})
                    assert prepared.ok, "could not prepare matched isolated settings"
                assert (await read(MUSIC))["settings"]["cooldown_seconds"] == 43

            async def matched_control():
                await baseline_cooldown()
                before = (await read(MUSIC))["settings"]
                memory = await read(MEMORY)
                ids = await task_ids()
                row = await turn("matched_control", "把点歌冷却改成47秒。")
                after = (await read(MUSIC))["settings"]
                history = await read(MEMORY)
                results["matched_control"].update(
                    cooldown=[before["cooldown_seconds"], after["cooldown_seconds"]],
                    history_enabled=[memory["history_enabled"], history["history_enabled"]],
                )
                assert row.get("tool_receipt") and after == before | {"cooldown_seconds": 47}, (
                    "ordinary change missing"
                )
                assert memory == history and await task_ids() == ids, (
                    "unexpected change outside requested field"
                )

            failed = []
            for case in (discover, expression, setting, mixed, matched_control, continuation, existing):
                if args.cases and case.__name__ not in args.cases:
                    continue
                # Independent cases use the ordinary clear-history action on this disposable Core.
                assert (await request.delete(base + CHAT, max_retries=2)).ok
                if case == expression:
                    await page.reload()
                prior = set(results)
                start_trace = len((await read("/replay/front-tools"))["model_calls"])
                try:
                    await case()
                except (AssertionError, TimeoutError, BrowserError) as error:
                    failed.append(
                        {
                            "case": case.__name__,
                            "error": type(error).__name__,
                            "layer": "replay_transport"
                            if isinstance(error, BrowserError)
                            else "timeout"
                            if isinstance(error, TimeoutError)
                            else "behavior_check",
                            "detail": str(error).split("Call log:")[0][:180],
                        }
                    )
                print(
                    json.dumps(
                        {
                            "case": case.__name__,
                            "model_calls": (await read("/replay/front-tools"))["model_calls"][start_trace:],
                            "failed": failed[-1:] if failed and failed[-1]["case"] == case.__name__ else [],
                            "turns": {k: v for k, v in results.items() if k not in prior},
                        }
                    ),
                    flush=True,
                )
            stats = await read("/replay/front-tools")
            assert stats["gateway_calls"].get("/v1/chat/completions") and stats["blocked_requests"] == 0
            assert not any(stats["platform_attempts"].values())
            print(json.dumps({"turns": results, "network": stats, "failed": failed}, ensure_ascii=False))
            return bool(failed)
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["serve", "check"])
    parser.add_argument("--port", type=int, default=9029)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--browser", default=None)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=["discover", "expression", "setting", "mixed", "matched_control", "continuation", "existing"],
    )
    options = parser.parse_args()
    if options.mode == "serve":
        serve(options)
    else:
        raise SystemExit(asyncio.run(check(options)))
