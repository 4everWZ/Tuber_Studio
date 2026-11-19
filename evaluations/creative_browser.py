"""Two-process Chromium acceptance for an ordinary private video assignment. See creative_browser.md."""

import argparse
import asyncio
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import uvicorn
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from evaluations.creative_browser_receipt import (
    CHANGE,
    CHAT,
    DEFAULT_BV,
    Mismatch,
    initial,
    require,
    same_position,
    verify,
)
from evaluations.omni_chat_loop import ROOT, connection
from tuber_studio.app import create_app
from tuber_studio.config import Settings


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@asynccontextmanager
async def server(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        web = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        job = asyncio.create_task(web.serve(sockets=[listener]))
        try:
            while not web.started:
                if job.done():
                    await job
                    raise Mismatch("本地工坊未能启动")
                await asyncio.sleep(0.05)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            web.should_exit = True
            await job


@contextmanager
def isolated_app(directory, endpoint, key, *, traffic=None, realtime_ticket_url=None):
    # Override inherited paths before Settings is constructed. The ordinary
    # loopback owner entry works without passing any admin secret to a browser.
    os.environ.pop("TUBER_ADMIN_TOKEN", None)
    os.environ.update(TUBER_CONFIG=str(directory / "studio.json"), TUBER_RUNTIME=str(directory))
    settings = Settings(runtime=directory)
    model = os.getenv("OMNI_MODEL_ALIAS", "omni-default")
    settings.data["llm"].update(base_url=endpoint + "/v1", model=model)
    settings.data["video_vision"].update(base_url=endpoint + "/v1", model=model)
    settings.data["responses"].update(
        base_url=endpoint + "/llm/v1",
        model=os.getenv("OMNI_LLM_MODEL_ALIAS", "llm-default"),
        max_output_tokens=8192,
    )
    settings.provider_keys = {"llm": key, "responses": key}
    # Applies even to new public-read clients and redirected requests.
    transport = httpx.AsyncHTTPTransport.handle_async_request
    posts = {
        httpx.URL(endpoint + path)
        for path in ("/v1/chat/completions", "/v1/chat/prompt_tokens", "/llm/v1/responses")
    }
    denied = []

    async def read_or_infer(self, request):
        host = request.url.host or ""
        if os.getenv("TUBER_PRIVATE_SOURCE_MANIFEST") and any(
            host == domain or host.endswith("." + domain) for domain in ("bilibili.com", "bilivideo.com")
        ):
            if traffic is not None:
                traffic["online_source_reads_prevented"] = traffic.get("online_source_reads_prevented", 0) + 1
            raise Mismatch("本场仅授权使用归档，已在发出在线请求前停止")
        ticket = False
        if realtime_ticket_url and request.url == httpx.URL(realtime_ticket_url) and request.method == "POST":
            try:
                ticket = json.loads(request.content) == {"path": "/v1/realtime"}
            except (UnicodeDecodeError, ValueError):
                pass
        if request.method not in {"GET", "HEAD"} and not (request.method == "POST" and request.url in posts) and not ticket:
            denied.append(request.method)
            raise Mismatch("隔离传输拒绝对外写入")
        response = await transport(self, request)
        if traffic is not None and request.method == "POST" and response.is_success:
            route = request.url.path
            traffic[route] = traffic.get(route, 0) + 1
        return response

    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", read_or_infer):
        app = create_app(settings)
        rt = app.state.runtime
        require(not rt.tasks.scope()[0], "隔离库已有平台账号")
        rt.tasks.tools = {k: rt.tasks.tools[k] for k in ("inspect_video", "web_search", "web_page")}
        yield app, denied
        require(not denied, "隔离传输曾拒绝对外写入")


@asynccontextmanager
async def isolated_studio(directory, endpoint, key):
    with isolated_app(directory, endpoint, key) as (app, _):
        async with server(app) as origin:
            yield origin


@asynccontextmanager
async def device(chromium, origin, args, login=None):
    keep = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
        "HOME",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
        "LANG",
        "LD_LIBRARY_PATH",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    browser = await chromium.launch(
        executable_path=args.chromium, env=env, args=["--disable-background-networking", "--autoplay-policy=no-user-gesture-required"]
    )
    try:
        context = await browser.new_context(service_workers="block")

        async def readonly(route):
            request = route.request
            url = urlsplit(request.url)
            if getattr(args, "archived", False) and any(
                url.hostname == domain or (url.hostname or "").endswith("." + domain)
                for domain in ("bilibili.com", "bilivideo.com")
            ):
                await route.abort()
                return
            if request.url.startswith(origin + "/") or (
                request.method in {"GET", "HEAD"} and url.scheme == "https"
            ):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", readonly)
        page = await context.new_page()
        page.set_default_timeout(args.turn_timeout * 1000)
        await page.goto(origin)
        if login is not None:
            await login(page)
        await page.locator("#operator-chat-input").wait_for()
        yield page
    finally:
        await browser.close()
    require(not browser.is_connected(), "设备 A 浏览器未关闭")


async def send(page, message):
    await page.locator("#operator-chat-input").fill(message)
    await page.locator("#operator-chat-send").click()
    turn = (
        page.locator(".operator-chat-turn[data-request-id]")
        .filter(
            has=page.locator(".operator-chat-user").filter(
                has_text=re.compile("^" + re.escape(message) + "$")
            )
        )
        .last
    )
    await turn.wait_for()
    return turn


async def edition(page, origin, task, revision, directory, timeout):
    data = None
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            while True:
                response = await page.request.get(f"{origin}/api/agent/tasks/{task}")
                require(response.ok, "无法读取原任务")
                data = await response.json()
                require(data["id"] == task and data["revision"] <= revision, "任务引用或修订号错位")
                if data["revision"] == revision and data["status"] in {
                    "completed", "cancelled", "failed", "needs_input", "awaiting_approval",
                }:
                    write(directory / f"r{revision}.json", data)
                    require(data["status"] == "completed", f"原任务 r{revision} 为 {data['status']}")
                    artifact = data.get("artifact") or {}
                    require(artifact.get("task_revision") == revision and artifact.get("task_id") == task,
                            f"缺原任务 r{revision} 的成果")
                    return data
                await asyncio.sleep(1)
    except TimeoutError as error:
        if not deadline.expired():
            raise
        state = data["status"] if data else "尚未读到任务"
        if data is not None:
            write(directory / f"r{revision}-pending.json", data)
        raise Mismatch(f"等待原任务 {task} 的 r{revision} 达到 {timeout} 秒，最后读到的状态为 {state}") from error


async def anchor(page):
    return await page.locator("#operator-chat-timeline").evaluate("""el => {
      const top = el.getBoundingClientRect().top;
      const row = [...el.querySelectorAll('[data-request-id]')].find(r => r.getBoundingClientRect().bottom > top);
      return row && {turn:row.dataset.requestId, offset:row.getBoundingClientRect().top-top, scroll:el.scrollTop};
    }""")


async def drill(page, origin, first, second, report):
    task = report["task"]
    old, current = first["artifact"], second["artifact"]
    await page.reload()
    reopen = page.locator(f'.operator-chat-turn[data-request-id="{task}"]').get_by_role(
        "button", name="查看任务"
    )
    await reopen.scroll_into_view_if_needed()
    reading = await anchor(page)
    await reopen.click()
    panel = page.locator("#chat-activity")
    await panel.get_by_text(f"任务 r2 · 成果 v{current['edition']}", exact=True).wait_for()
    await panel.get_by_text("历史成果", exact=True).click()
    url = f"{origin}/api/agent/tasks/{task}/artifacts/1/{old['edition']}"
    async with page.expect_response(lambda r: r.url == url and r.request.method == "GET") as read:
        await panel.get_by_text(f"任务 r1 · 成果 v{old['edition']}", exact=True).click()
    response = await read.value
    require(response.ok and await response.json() == old, "历史下钻正文与首版不符")
    report["historyEdition"] = [old["task_revision"], old["edition"]]
    frame = re.search(r"^主帧：[^\n]+ · <(https://www\.bilibili\.com/video/BV\w+/\?p=\d+&t=\d+)>$", old["body"], re.M)
    require(frame, "历史正文缺主帧原片时码链接")
    link = (
        panel.locator("details[open] details[open]")
        .get_by_role("link", name=frame[1], exact=True)
        .first
    )
    await link.wait_for()
    before = await anchor(page)
    require(before, "聊天阅读锚点不可见")
    archived = [step for step in first["steps"] if (step.get("result") or {}).get("source_kind") == "archived_video"]
    if archived:
        coordinate = re.search(r"^主帧：[^\n]+ · ([\d.]+) 秒 · <", old["body"], re.M)
        require(coordinate, "历史版缺少实际主帧秒数")
        seconds = float(coordinate[1])
        item = next(s for s in archived if (clip := s["result"].get("clip"))
                    and clip["start"] <= seconds < clip["end"] and
                    any(f.get("seconds") == seconds and f.get("url") == frame[1] for f in s["result"].get("findings", [])))
        path = f"/api/agent/tasks/{task}/clips/{item['id']}"
        require(await link.get_attribute("href") == frame[1], "归档成果的原始出处链接不符")
        report["video"] = frame[1]
        source = await page.context.new_page()
        try:
            response = await source.goto(origin + path)
            require(response and response.ok, "归档原片未能从工作站读回")
            video = source.locator("video")
            await video.evaluate("async v => {v.muted=true; await v.play()}")
            await source.wait_for_function("document.querySelector('video')?.currentTime > 0")
            report["archivePlayback"] = {"step": item["id"], "original_step": item["result"].get("request_id", item["id"]),
                                        "http": response.status,
                                        "seconds": await video.evaluate("v => v.currentTime")}
        finally:
            await source.close()
    else:
        async with page.expect_popup() as popup:
            await link.click()
        source = await popup.value
        try:
            await source.wait_for_url(frame[1], wait_until="commit")
            report["video"] = source.url
        finally:
            await source.close()
    await link.wait_for()
    after = await anchor(page)
    require(same_position(before, after), "原片回跳后聊天阅读位置改变")
    await page.locator("#chat-activity-close").click()
    await panel.wait_for(state="hidden")
    returned = await anchor(page)
    require(same_position(reading, returned), "关闭活动后未回到聊天原阅读位置")
    report.update(
        returnedToAnchor=True,
        beforeAnchor=before,
        afterAnchor=after,
        beforeActivity=reading,
        afterActivity=returned,
    )


async def exercise(origin, directory, args, *, memory=None, login=None):
    archived = getattr(args, "archived", False)
    report = {"schema": 1, "scenario": "archived_unhinted" if archived else "unhinted",
              "initial": initial(args.bvid, args.part, archived=archived), "change": CHANGE}

    def phase(name):
        report["phase"] = name
        write(directory / "browser.json", report)
        print(name, flush=True)

    async with async_playwright() as pw:
        phase("owner preferences in A" if memory else "ordinary assignment")
        async with device(pw.chromium, origin, args, login) as page:
            if memory:
                await memory.prepare(page)
                phase("ordinary assignment")
            original = await send(page, report["initial"])
            require(await original.get_by_role("button", name="查看任务").count(), "交办回复没有任务卡")
            task = str(UUID(await original.get_attribute("data-request-id")))
            report["task"] = task
            task_read = await page.request.get(f"{origin}/api/agent/tasks/{task}")
            require(task_read.ok, "设备 A 任务不可读")
            status = (await task_read.json())["status"]
            require(
                status not in {"completed", "cancelled", "failed", "needs_input", "awaiting_approval"},
                "闲聊前原任务已停止推进",
            )
            report["statusBeforeChat"] = status
            phase("chat before closing A")
            turn = await send(page, CHAT)
            reply = turn.locator(".operator-chat-reply").filter(has_text=re.compile(r"\S"))
            await reply.wait_for()
            text = (await reply.inner_text()).strip()
            report["assistantVisibleBeforeClose"] = bool(text)
            report["sideChat"] = {
                "message": CHAT,
                "reply": text,
                "turn": await turn.get_attribute("data-request-id"),
            }
            write(directory / "browser.json", report)
            require(await turn.get_by_role("button", name="查看任务").count() == 0, "闲聊被识别为交办")
            after_chat = await page.request.get(f"{origin}/api/agent/tasks/{task}")
            require(after_chat.ok and (await after_chat.json())["revision"] == 1, "闲聊改变了原任务的要求")
        async with device(pw.chromium, origin, args, login) as page:
            report["secondBrowserProcess"] = True
            phase("restore in B")
            restored = page.locator(f'.operator-chat-turn[data-request-id="{task}"]')
            await restored.get_by_role("button", name="查看任务").click()
            try:
                first = await edition(page, origin, task, 1, directory, args.edition_timeout)
            except Mismatch:
                terminal = directory / "r1.json"
                if memory and terminal.is_file() and not archived:
                    snapshot = json.loads(terminal.read_text(encoding="utf-8"))
                    if snapshot["id"] == task and snapshot["status"] in {"failed", "needs_input"}:
                        phase("r1 unavailable; independent voice check in B")
                        await memory.voice_after_blocked_edition(page, snapshot)
                raise
            await (
                page.locator("#chat-activity")
                .get_by_text(f"任务 r1 · 成果 v{first['artifact']['edition']}", exact=True)
                .wait_for()
            )
            await page.locator("#chat-activity-close").click()
            if memory:
                phase("read first edition and correct preference in B")
                await memory.correct(page, first)
            phase("natural revision")
            changed = await send(page, CHANGE)
            report["changeTurn"] = await changed.get_attribute("data-request-id")
            report["changeReply"] = await changed.locator(".operator-chat-reply").inner_text()
            write(directory / "browser.json", report)
            require(await changed.get_by_role("button", name="查看任务").count(), "自然改口回复没有任务卡")
            await changed.get_by_role("button", name="查看任务").click()
            second = await edition(page, origin, task, 2, directory, args.edition_timeout)
            phase("open saved edition and return")
            await drill(page, origin, first, second, report)
            if memory:
                phase("read revised edition and withdraw preference in B")
                await memory.withdraw(page, second)
            phase("browser passed")


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("须为正整数")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="在新的私有工作区发起一次真实普通交办")
    receipt = commands.add_parser("receipt", help="只读核查已有无提示双浏览器运行")
    receipt.add_argument("directory", type=Path)
    for sub in (live, receipt):
        sub.add_argument("--bvid", default=DEFAULT_BV)
        sub.add_argument("--part", type=positive, default=2)
    live.add_argument("--chromium", help="可选 Chromium 可执行文件，默认使用 Python Playwright 配套版本")
    live.add_argument("--turn-timeout", type=positive, default=240, metavar="SECONDS")
    live.add_argument("--edition-timeout", type=positive, default=900, metavar="SECONDS")
    args = parser.parse_args()
    if not re.fullmatch(r"BV[A-Za-z0-9]{10}", args.bvid):
        parser.error("--bvid 须为一个完整 BV 号")
    try:
        if args.command == "live":
            gateway = connection({k: os.environ.get(k) for k in ("OMNI_GATEWAY_URL", "OMNI_STUDIO_TOKEN")})
            require(gateway, "请设置 OMNI_GATEWAY_URL 和 OMNI_STUDIO_TOKEN")
            parent = ROOT / "tmp"
            parent.mkdir(exist_ok=True)
            directory = Path(tempfile.mkdtemp(dir=parent, prefix="creative-browser-"))
            print(directory, flush=True)

            async def run():
                async with isolated_studio(directory, *gateway) as origin:
                    await exercise(origin, directory, args)

            with patch.dict(os.environ):
                os.environ.pop("OMNI_STUDIO_TOKEN", None)
                try:
                    asyncio.run(run())
                finally:
                    for name in ("admin-token", "overlay-token"):
                        (directory / name).unlink(missing_ok=True)
        else:
            directory = args.directory
        result = verify(directory, args.bvid, args.part)
        if args.command == "live":
            write(directory / "workflow.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (BrowserError, OSError, sqlite3.Error, TimeoutError, KeyError, ValueError) as error:
        print(f"未通过：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
