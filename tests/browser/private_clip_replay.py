"""Replay verified P2 receipts in an offline Studio and exercise the chat card in Chromium."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import uvicorn

from tuber_studio.agent.tasks.public_video import clip_path
from tuber_studio.app import create_app
from tuber_studio.core.config import ROOT, Settings


def replay(source, reference, folder):
    settings = Settings(runtime=folder)
    app = create_app(settings)
    runtime, store = app.state.runtime, app.state.store
    coverage = json.loads((reference / "coverage.json").read_text(encoding="utf-8"))
    with sqlite3.connect((source / "studio.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row, = db.execute("SELECT id,goal,revision,status,steps,message FROM agent_tasks")
        turn, = db.execute("SELECT * FROM operator_chat_turns WHERE request_id=?", (row["id"],))
        steps = [s for s in json.loads(row["steps"]) if (s.get("result") or {}).get("clip")]
        assert row["status"] in {"failed", "completed"} and len(steps) == 2
        assert json.loads(turn[4])["card"] == {"type": "agent_task", "data": {"id": row["id"]}}
        for step, start in zip(steps, [11, 35], strict=True):
            receipt = step["result"]
            clip = receipt["clip"]
            assert step["tool"] == "inspect_video" and receipt["request_id"] == step["id"]
            assert (clip["bvid"], clip["part"], str(clip["cid"])) == ("BV1Hy4y1J7xH", 2, "301983263")
            assert (clip["start"], clip["end"], clip["frames"]) == (start, start + 3, 75)
            assert any(c["part"] == 2 and str(c["cid"]) == str(clip["cid"]) and
                       (c["start"], c["end"]) == (start, start + 3) for c in coverage)
            assert any(f["kind"] == "实际视频帧" and f["cid"] == clip["cid"] and
                       f["part"] == 2 and start <= f["seconds"] < start + 3 for f in receipt["findings"])
            movie = clip_path(source, step["id"]).read_bytes()
            assert movie == (reference / f"decoded-{start}.mp4").read_bytes()
            dest = clip_path(folder, step["id"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(movie)
    tasks = runtime.tasks
    task = tasks.create(row["id"], row["goal"])
    tasks._save(task, revision=row["revision"], steps=steps, status=row["status"], message=row["message"])
    other = tasks.create(str(uuid4()), "独立的对照任务")
    tasks._save(other, status="cancelled")
    char = runtime.character
    now = time.time()
    for i in range(19):
        key, message, answer = (turn[0], turn[3], turn[4]) if i == 9 else (
            str(uuid4()), f"阅读位置 {i}", json.dumps({"reply_text": "素材札记", "account_id": "", "card": None}),
        )
        store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                      (key, char["id"], char["version"], message, answer, now - 20 + i))
    return app, row["id"], other["id"]


async def main(source, reference):
    def offline(event, args):
        if event == "socket.getaddrinfo" and args[0] not in {None, "localhost", "127.0.0.1", "::1"}:
            raise PermissionError("Offline replay")
        if event == "socket.connect" and args[0].family in {socket.AF_INET, socket.AF_INET6}:
            raise PermissionError("Offline replay")

    (ROOT / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="clip-replay-") as folder, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        sys.addaudithook(offline)
        os.environ["TUBER_CONFIG"] = str(Path(folder) / "studio.json")
        os.environ["TUBER_ADMIN_TOKEN"] = secrets.token_urlsafe(32)
        app, task, other = replay(source, reference, Path(folder))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Studio failed to start")
                await asyncio.sleep(.05)
            await asyncio.to_thread(subprocess.run, ["node", Path(__file__).with_suffix(".cjs"),
                                    f"http://127.0.0.1:{listener.getsockname()[1]}", task, other, reference.resolve()],
                                    check=True, timeout=90)
        finally:
            server.should_exit = True
            await serving


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("source", type=Path, help="isolated run with the two verified inspect_video receipts")
    args.add_argument("reference", type=Path, help="private P2 reference directory with coverage.json and decoded clips")
    options = args.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(options.source, options.reference))
