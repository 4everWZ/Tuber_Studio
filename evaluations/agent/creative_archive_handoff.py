"""Export a persisted archive edition, its delivered frames and its actual clip files for private review."""

import argparse
import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from evaluations.agent.creative_browser_receipt import Mismatch, inspect_file, require
from tuber_studio.agent.tasks.public_video import clip_path


def export(runtime, manifest, directory, revision, task_id):
    package = json.loads(manifest.read_text(encoding="utf-8"))
    with closing(
        sqlite3.connect((runtime / "studio.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        edition = db.execute(
            "SELECT * FROM creative_editions WHERE task_id=? AND task_revision=? "
            "ORDER BY edition DESC LIMIT 1",
            (task_id, revision),
        ).fetchone()
        require(edition is not None, "本次任务修订尚无已落库成品")
        task = db.execute("SELECT steps FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    destination = directory / f"r{revision}-v{edition['edition']}"
    destination.mkdir(parents=True, exist_ok=True)
    body = edition["body"]
    (destination / "edition.md").write_bytes(body.encode("utf-8"))
    (destination / "edition.json").write_text(
        json.dumps(dict(edition), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reads = [
        s for s in json.loads(task["steps"]) if s["revision"] <= revision and s["tool"] == "inspect_video"
    ]
    report = {
        "task": task_id,
        "revision": revision,
        "edition": edition["edition"],
        "source": package["source"],
        "origin_manifest": "manifest.json",
        "items": [],
    }
    for block in body.split("\n\n"):
        if not block.startswith("**"):
            continue
        match = re.search(r"^主帧：(BV\w+) · 单帧采样 · 分 P (\d+) · ([\d.]+) 秒 · <([^>]+)>$", block, re.M)
        item = {"title": block.splitlines()[0].strip("*"), "bindings": []}
        report["items"].append(item)
        if not match:
            continue
        bv, part, seconds, url = match[1], int(match[2]), float(match[3]), match[4]
        item.update(bvid=bv, part=part, seconds=seconds, url=url)
        for step in reads:
            result = step.get("result") or {}
            clip = result.get("clip") or {}
            if (clip.get("bvid"), clip.get("part"), str(clip.get("cid"))) != (
                bv,
                part,
                str(package["source"]["cid"]),
            ):
                continue
            if not clip["start"] <= seconds < clip["end"]:
                continue
            field = (
                f"第 {len(report['items'])} 项 · {bv} P{part} · 连续取材入点 {clip['start']:g} 秒、"
                f"出点 {clip['end']:g} 秒（不含），保存 {clip['frames']} 个连续原帧。"
            )
            if field not in body:
                continue
            findings = [
                f
                for f in result.get("findings", [])
                if f.get("seconds") == seconds
                and f.get("url") == url
                and str(f.get("cid")) == str(clip["cid"])
                and f.get("source_kind") == "archived_video"
            ]
            if not findings:
                continue
            sources = [
                w
                for w in package["clips"]
                if w["source_url"] == clip.get("archive_url")
                and w["source_start_seconds"]
                <= clip["start"]
                < clip["end"]
                <= w["source_end_seconds_exclusive"]
            ]
            original = clip_path(runtime, result.get("request_id", step["id"]))
            if len(sources) != 1 or not original.is_file():
                item.setdefault("unverified", []).append(
                    {"step": step["id"], "reason": "缺少唯一归档窗口或交付文件"}
                )
                continue
            source = sources[0]
            try:
                inspect_file(original, clip)
            except Mismatch as error:
                item.setdefault("unverified", []).append({"step": step["id"], "reason": str(error)})
                continue
            shutil.copyfile(original, destination / original.name)
            item["bindings"].append(
                {
                    "step": step["id"],
                    "clip": clip,
                    "delivered_file": original.name,
                    "source_file": source["file"],
                    "source_start": source["source_start_seconds"],
                    "source_end_exclusive": source["source_end_seconds_exclusive"],
                    "findings": findings,
                }
            )
    (destination / "evidence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("runtime", "manifest", "directory"):
        parser.add_argument(key, type=Path)
    parser.add_argument("revision", type=int)
    parser.add_argument("task")
    args = parser.parse_args()
    print(export(args.runtime, args.manifest, args.directory, args.revision, args.task).name)
