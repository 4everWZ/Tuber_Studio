"""Opt-in Chromium acceptance: pytest -qs tests/browser/live_handoff_check.py."""
import asyncio
import json
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path

import uvicorn

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.tasks.runtime import TaskDecision
from tuber_studio.app import create_app
from tuber_studio.core.models import Reply


async def test_overlay_finished_handoff(settings):
    platform = ControlledPlatform()
    app = create_app(settings, adapter=platform)
    runtime = app.state.runtime
    voice = next(item for item in settings.voices() if item["id"] == "whale_sample_v1")

    async def decide(goal, steps, tools):
        if not steps:
            return TaskDecision(action="tool", tool="live_handoff", arguments={"text": voice["sample_text"]})
        return TaskDecision(action="finish", message="已核对本场播放器回执。")

    async def generate(*args, **kwargs):
        return Reply(spoken_text=voice["sample_text"])

    async def speech(*args, **kwargs):
        return await runtime.voices.prerecorded(voice["sample_asset"], voice["sample_text"])

    runtime.tasks.make_plan = None
    runtime.tasks.decide = decide
    runtime.llm.generate = generate
    runtime.voices.synthesize_live = speech
    env = dict(os.environ)
    if os.name == "nt" and not env.get("CHROMIUM_EXECUTABLE"):
        browsers = (Path(env["LOCALAPPDATA"]) / "ms-playwright").glob("chromium_headless_shell-*/*/chrome-headless-shell.exe")
        env["CHROMIUM_EXECUTABLE"] = str(max(browsers, key=lambda path: path.stat().st_mtime))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    if running.done():
                        await running
                    await asyncio.sleep(.05)
            browser = await asyncio.create_subprocess_exec(
                "node", str(Path(__file__).with_name("live-handoff.cjs")), str(listener.getsockname()[1]),
                env=env, stdout=asyncio.subprocess.PIPE)
            try:
                output, _ = await asyncio.wait_for(browser.communicate(), 70)
                assert browser.returncode == 0
            finally:
                if browser.returncode is None:
                    browser.kill()
                    await browser.wait()
        finally:
            server.should_exit = True
            await running
    observed = json.loads(output)
    with closing(sqlite3.connect((settings.runtime / "studio.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        for index, state in enumerate(("completed", "cancelled")):
            identity = observed["tasks"][index]
            row = db.execute("SELECT status,steps FROM agent_tasks WHERE id=?", (identity,)).fetchone()
            step = json.loads(row[1])[-1]
            request_id = step.get("resource_id", step["id"])
            job_id, = db.execute("""SELECT j.id FROM jobs j JOIN events e ON e.id=j.event_id
                WHERE json_extract(e.event_key,'$[0]')='operator_handoff'
                AND json_extract(e.event_key,'$[2]')=?""", (request_id,)).fetchone()
            status, text = db.execute("SELECT status,text FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert status == state
            spoken = db.execute("SELECT reply_text FROM conversations WHERE job_id=?", (job_id,)).fetchall()
            assert spoken == ([(voice["sample_text"],)] if index == 0 else [])
            assert step["result"]["status"] == state
            if index == 0:
                assert row[0] == "completed" and step["result"]["spoken"] == text == voice["sample_text"]
            else:
                assert not step["result"].get("spoken")
        assert not db.execute("SELECT count(*) FROM account_writes").fetchone()[0]
    assert not platform.sent
    print(json.dumps({"media": observed["media"], "job_states": ["completed", "cancelled"],
                      "spoken_rows": [1, 0], "platform_writes": 0}))
