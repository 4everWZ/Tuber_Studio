"""Run the private-call Playwright scenario with two loopback fixtures and no workstation."""

import argparse
import json
import os
import shutil
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
    const whep=process.env.STUDIO_TEST_WHEP==="1";
    await page.goto("http://127.0.0.1:"+(whep?18865:18765));
    const name = process.env.STUDIO_DISPLAY_TEST === "1" ? "character-display" : whep ? "private-whep" : "private-call";
    const scenario=eval("("+fs.readFileSync("tests/browser/"+name+".playwright.js","utf8")+")");
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
    parser.add_argument("--whep", action="store_true")
    parser.add_argument("--display", action="store_true", help="Test display settings, calls, live preview and overlay")
    parser.add_argument("--reference-defaults", type=Path, help="Seed the isolated runtime with the two approved default images")
    parser.add_argument("--rig-dir", type=Path, help="Exercise registered regular/model.inp and q/model.inp on WHEP")
    args = parser.parse_args()
    if args.rig_dir:
        args.whep = True
        for name in ("regular", "q"):
            if not (args.rig_dir / name / "model.inp").is_file():
                parser.error("Both real rig files must exist")
    repo = args.gateway_repo.resolve()
    engine_python = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    base = 18865 if args.whep else 18765
    for port in range(base, base + (4 if args.whep else 3)):
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
    tmp = ROOT / "tmp"
    tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp) as folder, ExitStack() as cleanup:
        runtime = Path(folder)
        if args.reference_defaults:
            from tuber_studio.persona.characters.references import DEFAULTS
            dest = runtime / "private-artifacts/artwork-defaults"
            dest.mkdir(parents=True)
            for name, _ in DEFAULTS.values():
                shutil.copyfile(args.reference_defaults / name, dest / name)
        origin = f"http://127.0.0.1:{base + 1}"
        config = json.loads((ROOT / "config/studio.example.json").read_text(encoding="utf-8"))
        config["omni"]["base_url"] = origin + "/v1"
        if args.display:
            config["features"]["live_enabled"] = True
        target = runtime / "studio.json"
        target.write_text(json.dumps(config), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("TUBER_", "OMNI_"))}
        if os.name == "nt" and not env.get("CHROMIUM_EXECUTABLE"):
            cache = Path(env["LOCALAPPDATA"]) / "ms-playwright"
            browsers = list(cache.glob("chromium_headless_shell-*/*/chrome-headless-shell.exe"))
            if browsers:
                env["CHROMIUM_EXECUTABLE"] = str(max(browsers, key=lambda binary: binary.stat().st_mtime))
        (runtime / "provider-credentials.json").write_text(json.dumps({
            "omni": {"base_url": origin + "/v1", "key": "private-call-fixture"},
        }), encoding="utf-8")
        if args.whep:
            env["STUDIO_TEST_RIG_DIR"] = str(args.rig_dir.resolve()) if args.rig_dir else ""
            (runtime / "private-call-avatars.json").write_text(json.dumps({
                "character_id": "whale", "origin": origin, "profiles": {
                    key: {"avatar_id": rig if args.rig_dir else "portrait_" + char * 32,
                          "driver": "inochi2d" if args.rig_dir else "ditto", "accepted": True}
                    for key, rig, char in (("original", "regular", "a"), ("q", "q", "b"))}}))
        env.update(STUDIO_DISPLAY_TEST=str(int(args.display)), STUDIO_TEST_WHEP=str(int(args.whep)), TUBER_RUNTIME=folder, TUBER_CONFIG=str(target))
        for command, port, paths in (
            ([str(engine_python), "tests/browser/private_call_fixture.py"], base + 1, str(repo)),
            ([sys.executable, "-m", "tuber_studio", "--port", str(base)], base, str(ROOT / "src")),
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
