"""Exercise Studio owner's real UI and a second physical client's authenticated memory reads.

uv run --locked python -m evaluations.owner_memory_device --device <ssh-alias>
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
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from playwright.async_api import async_playwright

from tuber_studio.app import create_app
from tuber_studio.config import ROOT, Settings


CONFIRMED = "Group related points and separate distinct concerns. Use headings and lists when they improve readability."
AUTO = "Lead with the answer, conclusion, or actionable result."
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


async def run(device, live, voice_wav):
    from evaluations.omni_chat_loop import connection
    from contextlib import AsyncExitStack

    token = secrets.token_urlsafe(32)
    config = connection(os.environ) if live else None
    report = {"platform_writes": 0, "source": "owner-supplied AGENTS.md / Global Working Rules"}
    output = Path(tempfile.mkdtemp(dir=ROOT / "tmp", prefix="owner-memory-evidence-")) / "result.json"
    print("Evidence:", output.relative_to(ROOT), flush=True)
    def record(name, value):
        report[name] = value
        pending = output.with_suffix(".writing")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(output)
    record("stage", "starting")
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp", ignore_cleanup_errors=True) as directory, patch.dict(os.environ, {"TUBER_ADMIN_TOKEN": token}):
        settings = Settings(runtime=Path(directory))
        if config:
            origin, key = config
            for kind, suffix in (("llm", "/v1"), ("responses", "/llm/v1"), ("video_vision", "/v1")):
                settings.data[kind]["base_url"] = origin + suffix
            settings.provider_keys = {"llm": key, "responses": key, "vision": key}
        else:
            settings.data["llm"]["kind"] = "demo"
        app = create_app(settings)
        runtime = app.state.runtime
        # No operations scheduler or platform action tool can run in this instance.
        tasks = runtime.tasks
        tasks.tools = {"inspect_video": tasks.tools["inspect_video"]}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            local = f"http://127.0.0.1:{port}"
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            serving.result()
                        await asyncio.sleep(.05)
                async with AsyncExitStack() as stack:
                    playwright = await stack.enter_async_context(async_playwright())
                    browser = await playwright.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
                    stack.push_async_callback(browser.close)
                    context = await browser.new_context(service_workers="block")
                    await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(local+"/") else route.abort())
                    assert (await context.request.post(local+"/api/auth", data={"token":token})).ok
                    page = await context.new_page()
                    await page.goto(local + "/#personalization")
                    await page.locator("#owner-memory-statement").fill(CONFIRMED)
                    async def confirm(dialog):
                        assert CONFIRMED in dialog.message
                        await dialog.accept()
                    page.once("dialog", confirm)
                    await page.get_by_role("button", name="保存私有记忆").click()
                    await page.locator("#owner-memory-save").get_by_text("已保存").wait_for()
                    await page.close()
                    record("stage", "confirmed_in_chromium")
                    remote = await device_call(device, port, token, "POST", SEARCH, {"query": CONFIRMED})
                    row, = remote["value"]["items"]
                    assert row["quote"] == CONFIRMED and row["source_key"].startswith("settings:")
                    report.update(confirmation={"browser":browser.version, "quote":row["quote"], "revision":row["revision"]},
                                  independent_device={"architecture":remote["arch"], "fresh_authenticated_session":True,
                                                      "read_after_page_close":True})
                    client = await stack.enter_async_context(httpx.AsyncClient(base_url=local, trust_env=False, timeout=180))
                    (await client.post("/api/auth", json={"token":token})).raise_for_status()
                    record("stage", "independent_device_authenticated")
                    if voice_wav and (await client.get("/api/chat/private-call")).raise_for_status().json()["configured"]:
                        from evaluations.owner_memory_voice import voice_probe
                        report["final_user_transcript"] = await voice_probe(playwright, os.getenv("CHROMIUM_EXECUTABLE"),
                                                                          local, token, voice_wav)
                    elif config:
                        from evaluations.owner_memory_voice import service_probe
                        try:
                            report["final_user_transcript"] = await service_probe(config, Path(directory), voice_wav)
                        except (OSError, subprocess.SubprocessError, ValueError, httpx.HTTPError) as error:
                            report["final_user_transcript"] = {"status":"unverified", "reason":type(error).__name__}
                    else:
                        report["final_user_transcript"] = {"status":"unverified", "reason":"ordinary_service_binding_missing"}
                    record("stage", "final_transcript_checked")
                    if config:
                        from evaluations.owner_memory_video import compare
                        report["video"] = await compare(client, runtime, port, device, token, row, record)
                    else:
                        report["video"] = {"status":"unverified", "reason":"ordinary_service_binding_missing" if live else "device_only"}
                        await device_call(device, port, token, "DELETE", f"/api/daily-memory/organized/{row['id']}?expected_revision={row['revision']}")
                    assert not (await client.post(SEARCH, json={"query":CONFIRMED})).json()["items"]
                    report["independent_device"]["forget_visible_on_original_client"] = True
                    record("stage", "finished")
            finally:
                server.should_exit = True
                await serving
                if hasattr(runtime.llm, "close"):
                    await runtime.llm.close()
                await app.state.comments.adapter.close()
                app.state.store.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--voice-wav", type=Path, help="speech fixture authorized for private Realtime input")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.device, args.live, args.voice_wav)), ensure_ascii=False, indent=2))
