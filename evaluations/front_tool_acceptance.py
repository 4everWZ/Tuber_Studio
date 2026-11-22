"""An isolated Core for foreground tool acceptance and its ordinary Chats browser driver.

On the gateway site, run the 'serve' command with OMNI_GATEWAY_URL and
OMNI_CORE_TOKEN_FILE. Forward its private port to loopback for 'check'.
Use the same fresh REPLAY_LOGIN on both sides. Only the server needs a gateway key.
"""

import argparse
import asyncio
import json
import os
import re
import tempfile
from contextlib import ExitStack, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from evaluations.omni_chat_loop import connection

CHAT = "/api/chat/intelligent"
MUSIC = "/api/live/music"
MEMORY = "/api/daily-memory/preferences"
PET = "/api/pets/whale"
OWNER = {"uid": "123", "name": "回放号主"}
RESTART_TASK = "0733ad92-1ab7-4d0b-9cbc-17e3db8a3e19"


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

    from tuber_studio.agent import operator_facts, task_delegation, task_tools
    from tuber_studio.agent.personal_agent import object_scope
    from tuber_studio.api_providers import APIProvider
    from tuber_studio.app import create_app
    from tuber_studio.config import Settings
    from tuber_studio.responses_provider import ResponsesClient
    from tuber_studio.web_fetch import public_ip
    from tuber_studio.web_search import NEWS_FEEDS, SEARCH_HOSTS

    config = connection(os.environ)
    if not config:
        raise ValueError("An ordinary gateway connection is required for the isolated server")
    root, key = config
    gateway = httpx.URL(root)
    routes = {
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/prompt_tokens"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/llm/v1/models"),
        ("POST", "/llm/v1/chat/prompt_tokens"),
        ("POST", "/llm/v1/chat/completions"),
        ("POST", "/llm/v1/responses"),
    }
    counts = {}
    public_reads = {"search": 0, "page": 0}
    denied = []
    model_calls = []
    front_json = APIProvider.generate_json
    front_text = APIProvider.generate_text
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
        entry["selection"] = {key: value[key] for key in (
            "action", "intent", "operation", "settings_group", "reference", "patch", "terms", "localized", "clock_fields",
        ) if key in value}
        return value

    async def observe_front_text(provider, messages, **options):
        entry = {"endpoint": "chat/completions", "schema": "plain_text", "task": working_on.get()}
        model_calls.append(entry)
        try:
            text = await front_text(provider, messages, **options)
        except asyncio.CancelledError:
            entry["cancelled"] = True
            raise
        except Exception as error:
            entry["error"] = type(error).__name__
            raise
        entry["finish"] = getattr(provider, "last_finish_reason", None)
        entry["usage"] = getattr(provider, "last_usage", {})
        entry["chars"] = len(text)
        return text

    async def observe_background(client, *args, **options):
        role = "routing" if options.get("tool_choice") == "required" and working_on.get() is None else "execution"
        entry = {"endpoint": "llm/v1/responses", "role": role, "task": working_on.get()}
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
        if os.getenv("REPLAY_PUBLIC_WEB") == "1" and request.method == "GET" and url.scheme == "https":
            if str(url) in NEWS_FEEDS or url.host in SEARCH_HOSTS and url.path in {"/search", "/news/search"}:
                public_reads["search"] += 1
                return await send(transport, request)
            try:
                pinned_public_read = bool(request.extensions.get("sni_hostname")) and public_ip(url.host)
            except ValueError:
                pinned_public_read = False
            if pinned_public_read:
                public_reads["page"] += 1
                return await send(transport, request)
        if (url.scheme, url.host, url.port) != (gateway.scheme, gateway.host, gateway.port) or (
            request.method,
            path,
        ) not in routes:
            denied.append(request.method)
            raise RuntimeError("Outbound request blocked by isolated replay")
        counts[path] = counts.get(path, 0) + 1
        return await send(transport, request)

    login = os.environ["REPLAY_LOGIN"]
    persistent = os.getenv("REPLAY_RUNTIME_DIR")
    directory_context = nullcontext(persistent) if persistent else tempfile.TemporaryDirectory(prefix="studio-front-tools-")
    with (
        directory_context as directory,
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
        patch.object(APIProvider, "generate_text", observe_front_text),
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
        runtime = app.state.runtime
        binding = runtime.character.get("bilibili_owner") or {}
        if any(binding.get(key) != value for key, value in OWNER.items()):
            card = {key: value for key, value in runtime.character.items() if not key.startswith("_")}
            runtime.store.replace_character(card | {"bilibili_owner": OWNER})

        async def idle_memory_organizer():
            return 0

        runtime.memory_organizer.run_once = idle_memory_organizer
        owner_lookups = []

        async def read_owner(uid):
            assert uid == OWNER["uid"]
            owner_lookups.append(uid)
            return SimpleNamespace(**OWNER)

        runtime.account.owner_profile = read_owner

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
                "public_reads": public_reads,
                "call_scope": object_scope(runtime),
                "platform_attempts": attempts,
                "owner_profile_reads": len(owner_lookups),
                "actions": {row["request_id"]: row["action"] for row in rows},
            }

        with ExitStack() as sources:
            if os.getenv("REPLAY_PUBLIC_FIXTURE") == "1":
                address = "https://example.org/today"

                async def search_fixture(query, *, news=False):
                    assert news
                    public_reads["search"] += 1
                    return [address]

                async def open_fixture(url):
                    assert url == address
                    public_reads["page"] += 1
                    return {"url": address, "title": "公开来源回放", "excerpt": "这份来源用于验证检索和链接。",
                            "published_at": operator_facts.now(runtime.browsing).replace(
                                hour=0, minute=0, second=0, microsecond=0).isoformat()}

                sources.enter_context(patch.object(task_tools, "search_public_web", search_fixture))
                sources.enter_context(patch.object(task_tools, "fetch_excerpt", open_fixture))
            uvicorn.run(app, host=args.bind, port=args.port, access_log=False, log_level="warning")


async def check(args):
    from playwright.async_api import Error as BrowserError
    from playwright.async_api import async_playwright

    base = f"http://127.0.0.1:{args.port}"
    results = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=args.browser, args=[
            "--no-proxy-server", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        ])
        context = await browser.new_context(base_url=base, service_workers="block")
        try:

            await context.route(
                lambda url: urlsplit(url).netloc != urlsplit(base).netloc,
                lambda route: route.abort(),
            )
            await context.add_init_script("""
                const fetch = window.fetch.bind(window);
                window.replayStreams = {};
                window.fetch = async (input, init) => {
                    const response = await fetch(input, init);
                    if (new URL(input, location.href).pathname === '/api/chat/intelligent/stream') {
                        const {request_id} = JSON.parse(init.body);
                        response.clone().text().then(text => { window.replayStreams[request_id] = text; });
                    }
                    return response;
                };
            """)
            page = await context.new_page()
            page.set_default_timeout(90000)
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
            await page.goto(base, wait_until="commit")

            async def turn(label, words):
                browser_turn = label == "expression" or label.startswith(
                    ("dialogue_", "canvas_", "owner_", "continue", "personal_", "fresh_")
                )
                if browser_turn:
                    await page.locator("#operator-chat-input").fill(words)
                    async with page.expect_request(
                        lambda req: req.method == "POST" and urlsplit(req.url).path == CHAT + "/stream"
                    ) as pending:
                        await page.locator("#operator-chat-send").click()
                    stream_request = await pending.value
                    body = stream_request.post_data_json
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
                                raise AssertionError("browser chat request failed")
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
                if browser_turn:
                    stream_response = await stream_request.response()
                    assert stream_response and stream_response.ok
                    assert (await stream_response.header_value("content-type")).startswith("text/event-stream")
                    captured = await page.wait_for_function("id => window.replayStreams[id]", arg=body["request_id"])
                    parts = (await captured.json_value()).split("\n\n")
                    terminal, = [json.loads(part.split("\ndata:", 1)[1]) for part in parts if part.startswith("event: done\n")]
                    assert terminal["reply_text"] == row["reply_text"]
                    await page.locator(f'[data-request-id="{body["request_id"]}"] .operator-chat-reply').wait_for()
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
                if state["selected_asset_id"] == target["asset_id"]:
                    reset = await request.put(base + PET, data={"asset_id": None})
                    assert reset.ok and (await reset.json())["selected_asset_id"] is None
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

            async def clock_dialogue():
                zone = ZoneInfo("Asia/Taipei")
                task_start = await task_ids()
                before = datetime.now(zone).replace(second=0, microsecond=0)
                time_reply = await turn("clock_time", "小鱼，现在几点了呀")
                after = datetime.now(zone)
                possible = set()
                while before <= after:
                    possible.add(before.strftime("%H:%M"))
                    before += timedelta(minutes=1)
                clock_pattern = r"(?<!\d)\d{2}:\d{2}(?!\d)"
                assert possible.intersection(re.findall(clock_pattern, time_reply["reply_text"]))
                casual = await turn("clock_to_chat", "小鱼，有没有什么要分享的")
                assert not re.search(clock_pattern, casual["reply_text"])
                dated = await turn("clock_date", "今天几号呀")
                current = datetime.now(zone)
                assert f"{current.year}年{current.month}月{current.day}日" in re.sub(r"\s+", "", dated["reply_text"])
                assert all(not row.get("tool_receipt") and not row.get("card") for row in (time_reply, casual, dated))
                assert await task_ids() == task_start

            async def dialogue():
                before = await read(PET), (await read("/api/live/studio"))["scene"], await task_ids()
                for index, question in enumerate((
                    "小鱼，你的主人是谁呀", "我说你的主人呀", "小鱼，今天怎么样了", "你在干什么呀",
                    "今儿过得还好吗？", "鱼鱼在忙啥呀？",
                )):
                    label = f"dialogue_{index + 1}"
                    row = await turn(label, question)
                    assert results[label]["action"] == "chat" and not row.get("card") and not row.get("tool_receipt")
                    if index < 2:
                        assert OWNER["name"] in row["reply_text"]
                    assert all(word not in row["reply_text"] for word in ("Jetson", "宠物当前", "画布当前", "可用形象"))
                assert before == (await read(PET), (await read("/api/live/studio"))["scene"], await task_ids())

            async def canvas():
                before = (await read("/api/live/studio"))["scene"]
                row = await turn("canvas_read", "查一下现在直播画布的设置。")
                assert row["tool_receipt"]["group"] == "scene" and row["tool_receipt"]["action"] == "read"
                row = await turn("canvas_followup", "那高度呢？")
                assert row["tool_receipt"]["values"] == {"height": before["height"]}
                row = await turn("canvas_change_topic", "你今天过得怎么样呀？")
                assert not row.get("card") and not row.get("tool_receipt")
                assert (await read("/api/live/studio"))["scene"] == before

            async def owner():
                before = (await read("/replay/front-tools"))["owner_profile_reads"]
                row = await turn("owner_lookup", "去看看你主人绑定的 B 站主页，告诉我账号是谁。")
                assert row["card"]["type"] == "owner_profile" and row["card"]["data"]["name"] == OWNER["name"]
                assert (await read("/replay/front-tools"))["owner_profile_reads"] == before + 1

            async def personal():
                before = (await read("/replay/front-tools"))["owner_profile_reads"]
                reply = await turn("personal_relation", "你是哪个人养的呀？")
                assert OWNER["name"] in reply["reply_text"]
                reply = await turn("personal_link", "那我去哪能找到他的主页？数字编号也带上。")
                assert reply["card"]["data"]["uid"] == OWNER["uid"]
                assert reply["card"]["data"]["url"] == f"https://space.bilibili.com/{OWNER['uid']}"
                reply = await turn("personal_link_mixed", "他的b站link有没有呀")
                assert reply["card"]["data"]["url"] == f"https://space.bilibili.com/{OWNER['uid']}"
                assert (await read("/replay/front-tools"))["owner_profile_reads"] == before
                reply = await turn("personal_change_topic", "你平常喜欢在窗边坐着聊天吗？")
                assert not reply.get("card") and not reply.get("tool_receipt")

            async def fresh_news():
                before = (await read("/replay/front-tools"))["public_reads"]
                reply = await turn("fresh_news", "小鱼，去搜一下今天有没有什么新闻。")
                after = (await read("/replay/front-tools"))["public_reads"]
                assert after["search"] > before["search"] and after["page"] > before["page"]
                assert reply["tool_receipt"]["group"] == "web"
                rows = [source for group in reply.get("sources", []) for source in group["entries"]]
                assert rows and all(source.get("published_at") and source["href"].startswith("https://") for source in rows)
                await page.locator(".chat-source-chip summary, .chat-sources summary").last.click()
                assert await page.locator(".chat-source-preview a").first.is_visible()
                results["fresh_news"]["sources"] = rows

            async def call_text():
                start_ids = await task_ids()
                scope = (await read("/replay/front-tools"))["call_scope"]
                reference = b"isolated voice reference"
                async def ticket(route):
                    await route.fulfill(json={
                        "socket": base.replace("http://", "ws://") + "/replay/voice",
                        "ticket": "isolated-transport-fixture", "call_scope": scope, "character": "大肥鱼",
                        "instructions": "", "performance": {"pets_enabled": False}, "avatar": {},
                        "voice": {"reference_sha256": sha256(reference).hexdigest()},
                    })

                async def socket(ws):
                    ws.send(json.dumps({"type": "session.created", "session": {"capabilities": {
                        "implementation_level": "model_native_duplex", "chunk_period_ms": 1000,
                    }}}))

                await page.route("**/api/chat/private-call", lambda route: route.fulfill(json={"configured": True}))
                await page.route("**/api/chat/private-call/reference", lambda route: route.fulfill(body=reference, content_type="audio/wav"))
                await page.route("**/api/chat/private-call/ticket", ticket)
                await page.route_web_socket("**/replay/voice?*", socket)
                await page.reload(wait_until="commit")
                await page.locator("#private-call-open").click()
                await page.locator("#private-call-start").click()
                await page.locator("#private-call-work-input").fill("交给后台准备一份私有便笺，把当前直播状态记下来。")
                async with page.expect_response(lambda resp: resp.url == base + CHAT and resp.request.method == "POST") as posted:
                    await page.locator("#private-call-work button").click()
                response = await posted.value
                assert response.ok and response.request.post_data_json["call_scope"] == scope
                reply = await response.json()
                assert (reply.get("card") or {}).get("type") == "agent_task", reply.get("reply_text")
                identity = reply["card"]["data"]["id"]
                assert await task_ids() == start_ids | {identity}
                await page.locator("#private-call-end").click()
                handset = await context.new_page()
                try:
                    await handset.set_viewport_size({"width": 390, "height": 844})
                    await handset.goto(base, wait_until="commit")
                    turn_id = response.request.post_data_json["request_id"]
                    await handset.locator(f'[data-request-id="{turn_id}"]').wait_for()
                    assert (await read(f"/api/agent/tasks/{identity}"))["id"] == identity
                finally:
                    await handset.close()
                results["call_text"] = {"task_found": True, "new_tasks": len((await task_ids()) - start_ids)}

            async def restart_seed():
                tomorrow = (datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(days=1)).date().isoformat()
                quote = f"{tomorrow} 09:00"
                response = await request.post(base + CHAT, data={
                    "request_id": RESTART_TASK, "message": f"在 {quote} 查当前游戏局面，留一句私有笔记。",
                    "intent": {"action": "agent_task", "arrangement": {
                        "kind": "once", "quote": quote, "local_date": tomorrow, "local_time": "09:00",
                    }},
                })
                assert response.ok and (await response.json())["card"]["data"]["status"] == "scheduled"
                paused = await request.patch(base + f"/api/agent/tasks/{RESTART_TASK}", data={"revision": 1, "action": "pause"})
                assert paused.ok and (await paused.json())["status"] == "paused"
                results["restart_seed"] = {"paused": True, "revision": 1}

            async def restart_resume():
                task = await read(f"/api/agent/tasks/{RESTART_TASK}")
                assert task["status"] == "paused" and task["revision"] == 1
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.locator(f'[data-request-id="{RESTART_TASK}"]').wait_for()
                restored = await request.patch(base + f"/api/agent/tasks/{RESTART_TASK}", data={"revision": 1, "action": "resume"})
                assert restored.ok
                updated = await restored.json()
                assert updated["status"] == "scheduled" and updated["due"] == task["due"]
                results["restart_resume"] = {"restored": True, "same_due": True, "revision": updated["revision"]}

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
                trace["failure_stage"] = "interleaved_chat"
                casual = await turn(label + "_casual", "今晚风好大，你喜欢听风声吗？")
                assert casual["reply_text"].strip() and not casual.get("tool_receipt") and not casual.get("card")
                assert await task_ids() == start_ids | {identity}, "casual turn started work"
                trace["failure_stage"] = "foreground_revision"
                try:
                    changed = await turn(
                        label, "继续刚才的那份便笺，改为只检查自主运营是否启动。只调用一次 operation_status，把读回值写进同一份便笺。"
                    )
                    changed_card = changed.get("card") or {}
                    trace["returned_task_id"] = changed_card.get("data", {}).get("id")
                    trace["same_task_id"] = trace["returned_task_id"] == identity
                    trace["after_turn"] = task_facts(await read(read_path))
                    if not trace["same_task_id"]:
                        raise AssertionError("no matching task card in the natural-language revision")
                    if trace["after_turn"]["revision"] != before["revision"] + 1:
                        raise AssertionError("same task did not advance exactly one revision")
                    assert await task_ids() == start_ids | {identity}, "unexpected additional task"
                    assert "只检查自主运营是否启动" in trace["after_turn"]["goal"]
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
                    async def deliveries():
                        return [row for row in (await read(CHAT))["turns"] if row.get("assistant_only")
                                and not row.get("hidden") and (row.get("task_delivery") or {}).get("task_id") == identity]

                    delivered = await deliveries()
                    assert len(delivered) == 1 and delivered[0]["task_delivery"]["revision"] == task["revision"]
                    await page.reload(wait_until="commit")
                    assert await deliveries() == delivered
                    await page.locator(f'[data-request-id="{delivered[0]["request_id"]}"]').wait_for()
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
            for case in (clock_dialogue, dialogue, canvas, owner, personal, fresh_news, call_text, discover, expression, setting, mixed, matched_control,
                         continuation, existing, restart_seed, restart_resume):
                if args.cases and case.__name__ not in args.cases:
                    continue
                # Independent cases use the ordinary clear-history action on this disposable Core.
                if case is not restart_resume:
                    assert (await request.delete(base + CHAT, max_retries=2)).ok
                await page.reload(wait_until="commit")
                prior = set(results)
                start_trace = len((await read("/replay/front-tools"))["model_calls"])
                try:
                    await case()
                except (AssertionError, KeyError, TimeoutError, BrowserError) as error:
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
            restart_only = args.cases and set(args.cases) <= {"restart_seed", "restart_resume"}
            assert (restart_only or stats["gateway_calls"].get("/v1/chat/completions")) and stats["blocked_requests"] == 0
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
        choices=["clock_dialogue", "dialogue", "canvas", "owner", "personal", "fresh_news", "call_text", "discover", "expression", "setting", "mixed",
                 "matched_control", "continuation", "existing", "restart_seed", "restart_resume"],
    )
    options = parser.parse_args()
    if options.mode == "serve":
        serve(options)
    else:
        raise SystemExit(asyncio.run(check(options)))
