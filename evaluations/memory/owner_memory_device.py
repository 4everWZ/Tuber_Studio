"""Exercise Studio owner's real UI and a second physical client's authenticated memory reads.

uv run --locked python -m evaluations.memory.owner_memory_device --device <ssh-alias>
Only an ephemeral Studio login is sent to the second device over SSH. No platform account is configured.
"""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from playwright.async_api import async_playwright

from tuber_studio.app import create_app
from tuber_studio.core.config import ROOT, Settings

CONFIRMED = "Group related points and separate distinct concerns. Use headings and lists when they improve readability."
AUTO = "以后写直播素材提纲，每个标题都用问句，以问号结尾。"
SEARCH = "/api/agent/memory.search"

# The remote client gets its own Studio cookie and verifies access is denied before authentication.
REMOTE = """
import json, platform, sys, urllib.request, urllib.error, http.cookiejar
origin, login, method, path, data = json.loads(sys.stdin.readline())
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def call(method, path, data):
    body = json.dumps(data).encode() if data is not None else None
    return json.load(opener.open(urllib.request.Request(origin+path, body, {"Content-Type":"application/json"}, method=method), timeout=30))
try:
    call("GET", "/api/daily-memory/preferences", None)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise RuntimeError("unauthenticated request accepted")
assert call("POST", "/api/auth", {"token":login})["authenticated"]
print(json.dumps({"arch":platform.machine(),"value":call(method,path,data)}))
"""


async def device_call(device, port, login, method, path, data=None):
    import shlex

    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=6", "-o", "ExitOnForwardFailure=yes", "-R",
               f"127.0.0.1:{port}:127.0.0.1:{port}", "--", device, "python3 -c " + shlex.quote(REMOTE)]
    payload = json.dumps([f"http://127.0.0.1:{port}", login, method, path, data]).encode()
    result = await asyncio.to_thread(subprocess.run, command, input=payload, capture_output=True, timeout=55)
    if result.returncode:
        raise RuntimeError("Independent client could not authenticate or finish its request")
    return json.loads(result.stdout)


async def run(device, live, voice_wav, gateway_checkout=None, voice_only=False, reviewed_receipt=None,
              timeout_seconds=1440, check_cooperation=False):
    from contextlib import AsyncExitStack

    from evaluations.agent.omni_chat_loop import connection

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    token = secrets.token_urlsafe(32)
    config = connection(os.environ) if live else None
    if check_cooperation and not config:
        raise ValueError("Chat admission replay requires an ordinary application binding")
    if voice_only and not config:
        raise ValueError("Independent voice replay requires an ordinary application binding")
    first_note = AUTO if voice_only else CONFIRMED
    report = {"platform_writes": 0, "source": "owner-authorized QA speech and title preferences" if voice_only
              else "owner-supplied AGENTS.md / Global Working Rules and authorized QA title preferences"}
    output = Path(tempfile.mkdtemp(dir=ROOT / "tmp", prefix="owner-memory-evidence-")) / "result.json"
    print("Evidence:", output.relative_to(ROOT), flush=True)
    def record(name, value):
        report[name] = value
        pending = output.with_suffix(".writing")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # A live Windows reader can temporarily open the snapshot without FILE_SHARE_DELETE.
        for delay in (0.02, 0.05, 0.1, 0.2, 0.4):
            try:
                pending.replace(output)
                break
            except PermissionError as error:
                if getattr(error, "winerror", None) not in {5, 32, 33}:
                    raise
                time.sleep(delay)
        else:
            pending.replace(output)
    record("stage", "starting")
    record("run_timeout_seconds", timeout_seconds)
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp", ignore_cleanup_errors=True) as directory, patch.dict(os.environ, {"TUBER_ADMIN_TOKEN": token}):
        settings = Settings(runtime=Path(directory))
        if config:
            origin, key = config
            settings.data["omni"]["base_url"] = origin + "/v1"
            settings.provider_keys = {"omni": key}
        app = create_app(settings)
        runtime = app.state.runtime
        # No operations scheduler or platform action tool can run in this instance.
        tasks = runtime.tasks
        tasks.tools = {"inspect_video": tasks.tools["inspect_video"]}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            local = f"http://127.0.0.1:{port}"
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error",
                                                   timeout_graceful_shutdown=4))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            serving.result()
                        await asyncio.sleep(.05)
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(asyncio.timeout_at(deadline))
                    from tuber_studio.agent.memory_organizer import admission_receipt
                    admission = {}
                    def save_judgment(source_key, judgment):
                        admission.setdefault(source_key, []).append(judgment)
                        record("chat_admission_judgments", admission)
                    stack.callback(admission_receipt.reset, admission_receipt.set(save_judgment))
                    playwright = await stack.enter_async_context(async_playwright())
                    browser = await playwright.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
                    stack.push_async_callback(browser.close)
                    context = await browser.new_context(service_workers="block")
                    stack.push_async_callback(context.close)
                    await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(local+"/") else route.abort())
                    assert (await context.request.post(local+"/api/auth", data={"token":token})).ok
                    if check_cooperation:
                        from evaluations.memory.owner_memory_budget import admission_wire
                        from evaluations.memory.owner_memory_cooperation import verify_chat_admission
                        stack.enter_context(admission_wire(runtime.llm, record))
                        record("stage", "checking_ordinary_chat_preferences")
                        async with httpx.AsyncClient(base_url=local, trust_env=False, timeout=180) as qa:
                            (await qa.post("/api/auth", json={"token": token})).raise_for_status()
                            await verify_chat_admission(qa, runtime, device, port, token, deadline, record)
                    page = await context.new_page()
                    await page.goto(local + "/#personalization")
                    await page.locator("#owner-memory-statement").fill(first_note)
                    async def confirm(dialog):
                        assert first_note in dialog.message
                        await dialog.accept()
                    page.once("dialog", confirm)
                    await page.get_by_role("button", name="保存私有记忆").click()
                    await page.locator("#owner-memory-save").get_by_text("已保存").wait_for()
                    await page.close()
                    record("stage", "confirmed_in_chromium")
                    remote = await device_call(device, port, token, "POST", SEARCH, {"query": first_note})
                    row, = remote["value"]["items"]
                    assert row["quote"] == first_note and row["source_key"].startswith("settings:")
                    report.update(confirmation={"browser":browser.version, "quote":row["quote"], "revision":row["revision"]},
                                  independent_device={"architecture":remote["arch"], "fresh_authenticated_session":True,
                                                      "read_after_page_close":True})
                    client = await stack.enter_async_context(httpx.AsyncClient(base_url=local, trust_env=False, timeout=180))
                    (await client.post("/api/auth", json={"token":token})).raise_for_status()
                    record("stage", "independent_device_authenticated")
                    async def correct_voice(target):
                        from evaluations.memory.owner_memory_voice import (
                            VOICE_RULE,
                            checkout_binding,
                            live_voice_binding,
                            spoken_rule,
                            voice_probe,
                        )
                        try:
                            speech = voice_wav or await asyncio.to_thread(spoken_rule, Path(directory), VOICE_RULE)
                            async with checkout_binding(config, gateway_checkout) as voice_service, live_voice_binding(voice_service):
                                result = await voice_probe(playwright, os.getenv("CHROMIUM_EXECUTABLE"), local,
                                                           token, speech, target, VOICE_RULE)
                            result["gateway"] = "local service checkout, deployed ordinary Omni upstream" if gateway_checkout else "deployed service"
                            result["speaker"] = "supplied WAV" if voice_wav else "synthetic Mandarin QA speech"
                        except (OSError, subprocess.SubprocessError, ValueError, httpx.HTTPError) as error:
                            result = {"status":"unverified", "reason":type(error).__name__}
                        record("final_user_transcript", result)
                        if isinstance(result.get("correction"), dict):
                            updated = (await device_call(device, port, token, "POST", "/api/agent/memory.get",
                                                         {"id":target["id"]}))["value"]
                            assert updated["revision"] == result["correction"]["revision"]
                            assert updated["quote"] == result["owner_confirmed_text"]
                            record("voice_cross_device_revision", updated)
                            return updated
                    if voice_only:
                        record("stage", "confirming_selected_voice")
                        updated = await correct_voice(row)
                        if not updated:
                            raise RuntimeError("Independent voice correction did not complete")
                        from evaluations.memory.owner_memory_video import forget_current
                        record("forgotten_revisions", await forget_current(client, port, device, token, [updated]))
                    elif config:
                        from evaluations.memory.owner_memory_budget import load_capacity, wire_receipts
                        from evaluations.memory.owner_memory_video import compare
                        record("responses_capacity", await load_capacity(config, settings.responses_config["model"]))
                        stack.enter_context(wire_receipts(record))
                        if reviewed_receipt:
                            from evaluations.memory.owner_memory_receipt import reviewed_read
                            origin, replay = reviewed_read(*reviewed_receipt)
                            stack.enter_context(patch("tuber_studio.agent.tasks.tools.factory.read_public_part", replay))
                            record("material_origin", origin)
                        report["video"] = await compare(client, runtime, port, device, token, row, record, correct_voice, deadline)
                    else:
                        report["final_user_transcript"] = {"status":"unverified", "reason":"ordinary_service_binding_missing"}
                        report["video"] = {"status":"unverified", "reason":"ordinary_service_binding_missing" if live else "device_only"}
                        await device_call(device, port, token, "DELETE", f"/api/daily-memory/organized/{row['id']}?expected_revision={row['revision']}")
                    assert not (await client.post(SEARCH, json={"query":first_note})).json()["items"]
                    report["independent_device"]["forget_visible_on_original_client"] = True
                    if live and gateway_checkout:
                        voice_ok = bool(report.get("voice_cross_device_revision"))
                        report["acceptance_passed"] = voice_ok and (voice_only or (
                            report.get("voice_style_observed") is True and report["video"]["semantic_status"] == "observed"))
                    record("stage", "closing_clients")
            except Exception as error:
                last = traceback.extract_tb(error.__traceback__)[-1]
                if isinstance(error, TimeoutError) and "deadline_diagnostic" not in report:
                    record("deadline_diagnostic", {"stage":report["stage"], "run_timeout_seconds":timeout_seconds,
                                                   "total_expired":asyncio.get_running_loop().time() >= deadline})
                record("failed_at", {"stage":report["stage"], "exception":type(error).__name__,
                                     "file":Path(last.filename).name, "function":last.name, "line":last.lineno,
                                     "winerror":getattr(error, "winerror", None)})
                raise
            finally:
                report["teardown"] = {"remaining_http_tasks":len(server.server_state.tasks),
                                      "open_connections":len(server.server_state.connections)}
                record("stage", "stopping_server")
                server.should_exit = True
                await serving
                async with AsyncExitStack() as cleanup:
                    cleanup.callback(app.state.store.close)
                    cleanup.push_async_callback(app.state.comments.adapter.close)
                    cleanup.push_async_callback(app.state.browsing.evidence.close)
                    cleanup.push_async_callback(runtime.llm.close)
                    cleanup.push_async_callback(runtime.memory_organizer.close)
                report["teardown"]["server_exited"] = serving.done()
                if "failed_at" in report:
                    record("stage", "failed")
    record("stage", "finished")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--check-cooperation", action="store_true", help="check new-chat automatic preference admission before the edition replay")
    parser.add_argument("--voice-only", action="store_true", help="confirm the authorized speech independently of video research")
    parser.add_argument("--gateway-checkout", type=Path, help="use the independent gateway checkout for voice only")
    parser.add_argument("--voice-wav", type=Path, help="speech fixture authorized for private Realtime input")
    parser.add_argument("--reviewed-receipt", nargs=2, metavar=("QA_RUNTIME", "STEP_ID"),
                        help="reuse an exact completed public-video read and its clip for all three editions")
    parser.add_argument("--timeout-seconds", type=int, default=1440, help="total replay deadline, including admission waits")
    args = parser.parse_args()
    result = asyncio.run(run(args.device, args.live, args.voice_wav, args.gateway_checkout, args.voice_only,
                             args.reviewed_receipt, args.timeout_seconds, args.check_cooperation))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("acceptance_passed") is False:
        raise SystemExit(1)
