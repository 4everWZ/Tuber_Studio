"""Workstation-hosted P2 acceptance with two ordinary Chromium logins and owner memory."""

import argparse
import asyncio
import hmac
import ipaddress
import json
import os
import sys
import tempfile
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from fastapi import HTTPException, Request

from evaluations.creative_browser import edition, exercise, isolated_app, positive, send, write
from evaluations.creative_browser_receipt import DEFAULT_BV, Mismatch, load, require
from evaluations.creative_memory_receipt import REPLACE, WRITE_TABLES, counts, verify_memory
from evaluations.omni_chat_loop import ROOT, connection
from evaluations.owner_memory_device import AUTO, CONFIRMED, SEARCH
from evaluations.owner_memory_voice import VOICE_RULE, spoken_rule, voice_on_page
from tuber_studio.agent.private_call import private_service
from tuber_studio.agent.voice_origin import sign
from tuber_studio.auth_sessions import COOKIE_NAME
from tuber_studio.operation_continuations import content_hash

CHECKPOINT = "/api/evaluation/p2-memory"


def workstation_voice_source(db, key):
    row = db.execute("""
        SELECT v.*,h.revision,h.quote FROM confirmed_voice_origins v
        JOIN daily_highlights h ON h.id=v.item_id
        WHERE v.source_key=json_extract(h.source_evidence,'$.memory_source.key')
    """).fetchone()
    if not row:
        return None
    expected = sign(
        key, ["omni.mic.v1", row["ticket_hash"], row["capture_id"], row["samples"], row["machine_text"]]
    )
    return {
        "id": row["item_id"],
        "revision": row["revision"],
        "source_key": row["source_key"],
        "machine_text": row["machine_text"],
        "confirmed_text": row["quote"],
        "capture_id": row["capture_id"],
        "samples": row["samples"],
        "signature_verified": hmac.compare_digest(row["receipt"], expected),
        "source_bound": row["source_key"]
        == "confirmed_voice:" + content_hash([row["ticket_hash"], row["capture_id"]]),
    }


def serve(args):
    require(args.port not in {443, 9443}, "须使用隔离服务端口")
    env = dict(os.environ)
    if not (env.get("OMNI_STUDIO_TOKEN") or env.get("OMNI_CORE_TOKEN_FILE")):
        env["OMNI_CORE_TOKEN_FILE"] = env.get("OMNI_GATEWAY_TOKEN_FILE")
    gateway = connection(env)
    require(gateway, "工作站缺普通应用网关绑定")
    directory = args.runtime.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    traffic = {}
    service = private_service()
    ticket_url = service[0] + "/v1/browser-tickets" if service else None
    with isolated_app(directory, *gateway, traffic=traffic, realtime_ticket_url=ticket_url) as (app, denied):
        db = app.state.store.db
        baseline = counts(
            db,
            (
                "operator_chat_turns",
                "agent_tasks",
                "creative_editions",
                "daily_highlights",
                "memory_source_tombstones",
                *WRITE_TABLES,
            ),
        )
        require(not any(baseline.values()), "新建运行库已有业务记录")
        receipt = {"baseline": baseline, "run": directory.name}

        def checkpoint():
            receipt.update(platform=counts(db, WRITE_TABLES), denied=len(denied), inference=dict(traffic))
            write(directory / "p2-host.json", receipt)

        @app.get("/healthz")
        async def health():
            return {"ok": db.execute("SELECT 1").fetchone()[0] == 1}

        @app.get(CHECKPOINT + "/identity")
        async def identity():
            return {
                "run": directory.name,
                "voice_origin": os.getenv("TUBER_PRIVATE_REALTIME_PUBLIC_ORIGIN") if service else None,
            }

        @app.get(CHECKPOINT)
        async def state(request: Request):
            if not app.state.auth_sessions.valid(request.cookies.get(COOKIE_NAME)):
                raise HTTPException(401)
            if service and (voice := workstation_voice_source(db, service[1])):
                write(directory / "p2-voice-source.json", voice)
            # Raw editorial inputs and login material stay on the workstation.
            for task, revision, plan in db.execute(
                "SELECT id,revision,plan FROM agent_tasks WHERE status='completed'"
            ):
                path = directory / f"editor-{task}-r{revision}.json"
                if not path.exists():
                    memory = (json.loads(plan or "{}") or {}).get("editor_memory", {})
                    write(
                        path,
                        {"task": task, "revision": revision, "citations": memory.get("memory_citations", [])},
                    )
            checkpoint()
            return {"run": directory.name, "platform": receipt["platform"], "denied": len(denied)}

        checkpoint()
        try:
            uvicorn.run(app, host=args.bind, port=args.port, access_log=False, log_level="error")
        finally:
            receipt.update(denied=len(denied), inference=dict(traffic))
            write(directory / "p2-host.json", receipt)


class MemoryScenario:
    def __init__(self, origin, directory, args):
        self.origin = origin
        self.directory = directory
        self.args = args
        self.report = {}

    def record(self, name, value):
        self.report[name] = value
        write(self.directory / "p2-memory.json", self.report)

    async def api(self, page, method, path, data=None):
        response = await page.request.fetch(self.origin + path, method=method, data=data, max_redirects=0)
        require(response.ok, f"记忆验收接口返回 HTTP {response.status}")
        return await response.json()

    async def checkpoint(self, page):
        host = await self.api(page, "GET", CHECKPOINT)
        require(
            host["run"] == self.args.run and host["denied"] == 0 and not any(host["platform"].values()),
            "工作站隔离实例或平台写入回执不符",
        )

    async def login(self, page, token):
        identity = await self.api(page, "GET", CHECKPOINT + "/identity")
        require(
            identity["run"] == self.args.run and page.url.startswith(self.origin + "/"),
            "当前 IP 入口未绑定本次隔离验收",
        )
        await page.locator("#token").fill(token)
        await page.locator("#login-form button[type=submit]").click()

    async def preference(self, page, message, label, *, confirm=False):
        turn = await send(page, message)
        identity = str(UUID(await turn.get_attribute("data-request-id")))
        source = "turn:" + identity
        self.record(label + "_turn", identity)
        await turn.locator(".operator-chat-reply").wait_for()
        require(not await turn.get_by_role("button", name="查看任务").count(), "偏好话轮被识别为交办")
        if confirm:
            await turn.get_by_text("原话操作", exact=True).click()

            async def accept(dialog):
                if message in dialog.message:
                    await dialog.accept()
                else:
                    await dialog.dismiss()

            page.once("dialog", accept)
            await turn.get_by_role("button", name="记住这句话").click()
            await turn.get_by_text("已保存", exact=True).wait_for()
        try:
            async with asyncio.timeout(self.args.turn_timeout):
                while True:
                    found = await self.api(page, "POST", SEARCH, {"query": source})
                    hits = [r for r in found["items"] if r["source_key"] == source and r["quote"] == message]
                    if hits:
                        (row,) = hits
                        self.record(label, row)
                        return row
                    await asyncio.sleep(2)
        except TimeoutError as error:
            state = await self.api(page, "GET", "/api/daily-memory")
            self.record(label + "_unresolved", {"source": source, "organization": state["organization"]})
            raise Mismatch("期限内未读到绑定本轮原话的偏好，已保留整理状态") from error

    async def prepare(self, page):
        await self.checkpoint(page)
        history = await self.api(page, "GET", "/api/chat/intelligent")
        tasks = await self.api(page, "GET", "/api/agent/tasks")
        notes = await self.api(page, "GET", "/api/daily-memory")
        require(
            not (history["turns"] or history["account_id"] or tasks["tasks"] or notes["organized"]),
            "当前网页已有号主业务数据",
        )
        self.record("run", self.args.run)
        actual = await self.preference(page, CONFIRMED, "layout", confirm=True)
        title = await self.preference(page, AUTO, "original")
        require(actual["id"] != title["id"], "两项独立偏好发生覆盖")

    async def observe(self, page, task, card_turn, label):
        await self.checkpoint(page)
        # Reload the card from persisted history even after A has closed.
        await page.reload()
        card = page.locator(f'.operator-chat-turn[data-request-id="{card_turn}"] .operator-chat-card')
        await card.get_by_role("button", name="查看任务").wait_for()
        citations = task.get("memory_citations", [])
        self.record(label + "_api", citations)
        seen = []
        for details in await card.locator(".owner-memory-citations").all():
            await details.locator("summary").click()
            quotes = await details.locator("blockquote:visible").all_inner_texts()
            sources = await details.locator("small:visible").all_inner_texts()
            seen.extend(
                {"quote": quote, "source": source} for quote, source in zip(quotes, sources, strict=True)
            )
            inspect_voice = details.get_by_role("button", name="核对语音来源")
            if label == "r2" and await inspect_voice.count():
                voice = self.report["voice"]
                expected = (
                    "机器转写："
                    + voice["bound_final_transcript"]
                    + "\n号主确认："
                    + voice["owner_confirmed_text"]
                )
                await inspect_voice.click()
                text = details.locator("p").filter(has_text="机器转写：")
                await text.wait_for()
                actual = await text.text_content()
                self.record("r2_voice_source_visible", actual)
                require(actual == expected, "卡片上的机器原稿或号主确认稿与通话来源不符")
        self.record(label + "_visible", seen)

    async def current(self, page, row):
        current = await self.api(page, "POST", "/api/agent/memory.get", {"id": row["id"]})
        require(current == row, "跨浏览器读回的条目、修订或原话不符")

    async def correct(self, page, first):
        for label in ("layout", "original"):
            await self.current(page, self.report[label])
        await self.observe(page, first, first["id"], "r1")
        await self.correct_voice(page)

    async def correct_voice(self, page):
        original = self.report["original"]
        self.record("voice", {"status": "pending", "stage": "check_voice_origin"})

        def progress(stage, facts):
            self.record("voice", self.report["voice"] | facts | {"stage": stage, "status": "pending"})

        try:
            identity = await self.api(page, "GET", CHECKPOINT + "/identity")
            require(identity.get("voice_origin") == self.origin, "私有通话未绑定本次隔离 HTTPS 入口")
            audio = self.args.voice_wav
            if not audio:
                progress("synthesize_authorized_sentence", {})
                audio = await asyncio.to_thread(spoken_rule, self.directory, VOICE_RULE)
            result = await voice_on_page(
                page,
                self.origin,
                audio,
                original,
                VOICE_RULE,
                signed_only=True,
                correct_qa_preview=not self.args.voice_wav,
                progress=progress,
            )
            self.record("voice", self.report["voice"] | result)
            require(
                result.get("signed_final_received")
                and result.get("confirmation_dialog_accepted")
                and result.get("correction") == {"id": original["id"], "revision": original["revision"] + 1},
                "私有通话未完成已签最终转写与号主确认",
            )
            corrected = await self.api(page, "POST", "/api/agent/memory.get", {"id": original["id"]})
            self.record("corrected", corrected)
            require(
                corrected["quote"] == result["owner_confirmed_text"]
                and corrected["source_key"].startswith("confirmed_voice:")
                and corrected.get("voice_origin")
                == {
                    "machine_text": result["bound_final_transcript"],
                    "capture_id": result["capture_id"],
                    "samples": result["explicit_samples"],
                    "owner_edited": corrected["quote"] != result["bound_final_transcript"],
                },
                "语音来源与最终转写或确认稿不一致",
            )
            await self.checkpoint(page)
        except Exception as error:
            self.record(
                "voice", self.report["voice"] | {"status": "unverified", "failure": type(error).__name__}
            )
            raise
        require(
            corrected["id"] == original["id"] and corrected["revision"] == original["revision"] + 1,
            "语音确认没有推进同一偏好条目",
        )
        await self.current(page, self.report["layout"])

    async def withdraw(self, page, second):
        browser = load(self.directory / "browser.json")
        task = second["id"]
        await self.observe(page, second, browser["changeTurn"], "r2")
        await self.forget_title(page)
        changed = await send(page, REPLACE)
        identity = str(UUID(await changed.get_attribute("data-request-id")))
        self.record("withdrawal_turn", identity)
        await changed.get_by_role("button", name="查看任务").wait_for()
        third = await edition(page, self.origin, task, 3, self.directory, self.args.edition_timeout)
        await self.observe(page, third, identity, "r3")

    async def voice_after_blocked_edition(self, page, snapshot):
        self.record("creation_blocked", {"id": snapshot["id"], "revision": 1, "status": snapshot["status"]})
        try:
            for key in ("original", "layout"):
                await self.current(page, self.report[key])
            await self.correct_voice(page)
            await self.forget_title(page)
            await self.checkpoint(page)
        except Exception as error:
            self.record("independent_voice_failure", {"error": type(error).__name__})

    async def forget_title(self, page):
        row = self.report["corrected"]
        await self.current(page, row)
        deleted = await self.api(
            page, "DELETE", f"/api/daily-memory/organized/{row['id']}?expected_revision={row['revision']}"
        )
        require(deleted == {"id": row["id"], "deleted": True}, "撤回回执不符")
        for key in ("original", "corrected"):
            source = self.report[key]["source_key"]
            result = await self.api(page, "POST", SEARCH, {"query": source})
            require(all(r["id"] != row["id"] for r in result["items"]), "撤回后仍能检索偏好")
        await self.current(page, self.report["layout"])
        self.record("withdrawn", deleted)


def workstation_origin(value):
    origin = value.rstrip("/")
    url = urlsplit(origin)
    address = ipaddress.ip_address(url.hostname)
    require(
        url.scheme == "https"
        and url.port
        and url.port not in {443, 9443}
        and not any(
            (
                url.username,
                url.password,
                url.path,
                url.query,
                url.fragment,
                address.is_loopback,
                address.is_unspecified,
            )
        ),
        "须使用工作站 IP 的独立 HTTPS 端口，例如 9444",
    )
    return origin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    host = commands.add_parser("host", help="在工作站新建只读平台取材的隔离 Studio")
    host.add_argument("runtime", type=Path)
    host.add_argument("--bind", required=True)
    host.add_argument("--port", required=True, type=positive)
    browser = commands.add_parser("browser", help="两次独立登录工作站 IP")
    browser.add_argument("--origin", required=True)
    browser.add_argument("--run", required=True, help="工作站新建运行目录名")
    browser.add_argument(
        "--login-stdin", required=True, action="store_true", help="从私有标准输入读取隔离 Studio 登录令牌"
    )
    browser.add_argument("--turn-timeout", type=positive, default=240)
    browser.add_argument("--edition-timeout", type=positive, default=1500)
    browser.add_argument("--chromium")
    browser.add_argument(
        "--voice-wav", type=Path, help="获准朗读陈述句约定的 WAV，默认使用已有中文合成音频夹具"
    )
    receipt = commands.add_parser("receipt", help="只读复核工作站运行库、原件和浏览器证据")
    receipt.add_argument("runtime", type=Path)
    receipt.add_argument("browser_receipts", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "host":
            serve(args)
        elif args.command == "browser":
            origin = workstation_origin(args.origin)
            require(not sys.stdin.isatty(), "隔离登录须经私有管道传入")
            login = sys.stdin.readline().strip()
            require(login, "缺隔离 Studio 登录输入")
            args.bvid, args.part = DEFAULT_BV, 2
            parent = ROOT / "tmp"
            parent.mkdir(exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="p2-memory-browser-", dir=parent))
            print(directory.name, flush=True)
            scenario = MemoryScenario(origin, directory, args)
            asyncio.run(
                exercise(origin, directory, args, memory=scenario, login=partial(scenario.login, token=login))
            )
        else:
            result = verify_memory(args.runtime, args.browser_receipts)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Mismatch as error:
        print(f"未通过：{error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Browser/SSH errors can include private URLs or command input.
        print(f"未完成（{type(error).__name__}），请核对私有目录中的阶段回执。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
