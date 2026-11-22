"""Replay only the public material of a completed QA read, including its original clip."""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from evaluations.owner_memory_video import SELECTION
from tuber_studio.agent.public_video import clip_path
from tuber_studio.core.config import ROOT


def reviewed_read(directory, step_id):
    directory = Path(directory).resolve()
    step_id = str(UUID(step_id))
    if not directory.is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Supply an isolated QA runtime under tmp")
    uri = (directory / "studio.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("""SELECT t.id, s.value, t.plan FROM agent_tasks t, json_each(t.steps) s
            WHERE t.status='completed' AND json_extract(s.value,'$.id')=?
            AND json_extract(s.value,'$.tool')='inspect_video'""", (step_id,)).fetchone()
    if not row:
        raise ValueError("No completed QA task owns this video read")
    task_id, step, plan = row[0], json.loads(row[1]), json.loads(row[2])
    receipt = step["result"]
    clip = receipt["clip"]
    expected = {"bvid":SELECTION["bvid"], **SELECTION["ranges"][0]}
    if (step["arguments"] != expected or receipt["selection"] != SELECTION
            or any(clip.get(k) != v for k,v in expected.items())
            or not any(key.startswith(step_id + ":") for key in plan.get("frame_reviews", {}))):
        raise ValueError("Reviewed clip does not match the authorized BV, part and interval")
    movie = clip_path(directory, receipt["request_id"])
    if not movie.is_file() or not clip["cid"] or not clip["frames"]:
        raise ValueError("Original clip or its frame receipt is missing")
    for finding in receipt["findings"]:
        if (finding["part"] != clip["part"] or str(finding["cid"]) != str(clip["cid"])
                or not clip["start"] <= finding["seconds"] < clip["end"]):
            raise ValueError("Finding is outside its recorded public clip")
    fields = ("clip", "findings", "gaps", "selection", "url", "part_duration", "error", "status", "viewing")
    material = {k:receipt[k] for k in fields}
    origin = {"mode":"reused_verified_public_read", "task_id":task_id, "step_id":step_id,
              "selection":SELECTION, "cid":clip["cid"], "clip_frames":clip["frames"]}

    async def replay(request, selection, vision, granted, runtime_dir=None):
        if not granted() or {k:getattr(selection,k) for k in expected} != expected:
            return {"error":"回放授权仅覆盖原公开片段。"}
        dest = clip_path(runtime_dir, request.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(movie, dest)
        return material | {"request_id":request.id}

    return origin, replay
