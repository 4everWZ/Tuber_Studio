"""Run the private-call Playwright scenario with two loopback fixtures and no workstation."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE = r"""
const fs = require("node:fs"), path = require("node:path");
const {chromium} = require(require.resolve("playwright", {
  paths:[process.cwd(),path.join(path.dirname(process.execPath),"node_modules/@playwright/cli")]}));
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || undefined,
    args:["--autoplay-policy=no-user-gesture-required","--disable-background-networking","--no-proxy-server"]});
  try {
    const page=await browser.newPage();
    await page.route("**/*",route=>new URL(route.request().url()).hostname==="127.0.0.1"?route.continue():route.abort());
    await page.goto("http://127.0.0.1:18765");
    const scenario=eval("("+fs.readFileSync("tests/browser/private-call.playwright.js","utf8")+")");
    console.log(JSON.stringify(await scenario(page)));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
"""


def stop(process):
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-repo", type=Path, default=ROOT.parent / "omni-avatar-api")
    args = parser.parse_args()
    repo = args.gateway_repo.resolve()
    engine_python = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for port in (18765, 18766, 18767):
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
    tmp = ROOT / "tmp"
    tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp) as folder, ExitStack() as cleanup:
        runtime = Path(folder)
        token = runtime / "fixture-token"
        token.write_text("private-call-fixture", encoding="utf-8")
        config = json.loads((ROOT / "config/studio.example.json").read_text(encoding="utf-8"))
        config["llm"]["kind"] = "demo"
        config.pop("responses", None)
        config.pop("video_vision", None)
        target = runtime / "studio.json"
        target.write_text(json.dumps(config), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("TUBER_", "OMNI_"))}
        if os.name == "nt" and not env.get("CHROMIUM_EXECUTABLE"):
            cache = Path(env["LOCALAPPDATA"]) / "ms-playwright"
            browsers = list(cache.glob("chromium_headless_shell-*/*/chrome-headless-shell.exe"))
            if browsers:
                env["CHROMIUM_EXECUTABLE"] = str(max(browsers, key=lambda binary: binary.stat().st_mtime))
        env.update(TUBER_RUNTIME=folder, TUBER_CONFIG=str(target), TUBER_PRIVATE_REALTIME_ORIGIN="http://127.0.0.1:18766",
                   TUBER_PRIVATE_REALTIME_TOKEN_FILE=str(token))
        for command, port, paths in (
            ([str(engine_python), "tests/browser/private_call_fixture.py"], 18766, str(repo)),
            ([sys.executable, "-m", "tuber_studio", "--port", "18765"], 18765, str(ROOT / "src")),
        ):
            process = subprocess.Popen(command, cwd=ROOT, env=env | {"PYTHONPATH": paths})
            cleanup.callback(stop, process)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError(f"Loopback fixture on {port} did not start")
        subprocess.run(["node", "-e", NODE], cwd=ROOT, check=True, env=env, timeout=90)


if __name__ == "__main__":
    main()
