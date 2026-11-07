"""Run the normal review scheduler and stop after a requested real-video cohort.

No publication endpoints are called. Private bodies remain in the business DB;
the progress artifact contains counts, source IDs and saved permissions only.
"""

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from tuber_studio.operations.browsing.evidence import BrowsingEvidence


def material_proof(row, runtime):
    """Require consumed, source-bound content, not merely a title or cache key."""
    if row["status"] not in {"selected", "viewed_without_comment", "comments_checked"}:
        return None
    if not row["path"] or not row["progress_material"]:
        return None
    path = (runtime / row["path"]).resolve()
    if not path.is_relative_to(runtime.resolve()):
        return None
    try:
        material = json.loads(path.read_text(encoding="utf-8"))
        consumed = json.loads(row["progress_material"])
        decisions = json.loads(row["decisions"] or "[]")
        main = json.loads(row["main_decision"] or "null")
    except (OSError, ValueError):
        return None
    if (
        not isinstance(consumed, dict)
        or consumed.get("metadata", {}).get("bvid") != row["bvid"]
        or not consumed.get("source_version")
        or consumed["source_version"] != material.get("source_version")
        or BrowsingEvidence.source_version(consumed) != BrowsingEvidence.source_version(material)
        or material.get("metadata", {}).get("bvid") != row["bvid"]
        or not (decisions or main)
    ):
        return None
    cids = {str(part["cid"]) for part in material["metadata"].get("parts", []) if part.get("cid")}
    # CC tracks carry bvid themselves; visual parts inherit the verified parent
    # video's identity and bind the actual sampled frames by cid and frame ID.
    parts = [part for part in material.get("parts", [])
             if (part.get("bvid") == row["bvid"] or part.get("visual") and "bvid" not in part)
             and str(part.get("cid")) in cids and part.get("segments")]
    segments = [segment for part in parts for segment in part["segments"]
                if isinstance(segment.get("text"), str) and segment["text"].strip() and segment.get("id")]
    if not segments or not material.get("source_version"):
        return None
    frames = {frame["id"]: frame for frame in material.get("visual_frames", [])
              if frame.get("id") and str(frame.get("cid")) in cids and frame.get("image_base64")}
    observed = {evidence["id"] for part in parts if part.get("visual")
                for evidence in part.get("frame_evidence", [])
                if evidence.get("id") in frames
                and any(segment.get("id") == "frame:" + evidence["id"] for segment in part["segments"])
                and evidence.get("content_hash")
                and evidence.get("content_hash") == frames[evidence["id"]].get("content_hash")
                and evidence.get("actual_time") == frames[evidence["id"]].get("actual_time")
                and str(evidence.get("cid")) == str(frames[evidence["id"]].get("cid"))}
    subtitles = sum(len(part["segments"]) for part in parts if not part.get("visual"))
    if not subtitles and not observed:
        return None
    return {"bvid": row["bvid"], "material_key": row["material_key"], "source_version": consumed["source_version"], "read_state": row["read_state"],
            "source_parts": len(parts), "source_segments": len(segments), "cids": sorted(cids),
            "subtitle_segments": subtitles, "observed_frames": len(observed),
            "coverage": material.get("coverage"), "fully_read_available_material": row["read_state"] == "complete"}


def observe(db, session, runtime):
    scope = (session["id"],)
    watches = db.execute(
        "SELECT s.bvid,t.status,t.material_key,m.path,p.material AS progress_material,p.decisions,p.main_decision,p.read_state FROM browsing_watch_starts s "
        "JOIN browsing_tasks t ON t.id=s.task_id JOIN browsing_rounds r ON r.id=t.round_id "
        "LEFT JOIN browsing_materials m ON m.id=t.material_key "
        "LEFT JOIN browsing_video_progress p ON p.account_id=s.account_id AND p.character_id=s.character_id AND p.bvid=s.bvid "
        "WHERE json_extract(r.config,'$.review_session_id')=?", scope,
    ).fetchall()
    proofs = {proof["bvid"]: proof for row in watches if (proof := material_proof(row, runtime))}
    viewed = sorted(proofs)
    rounds = [dict(row) for row in db.execute(
        "SELECT id,status,budget,error,COALESCE(json_extract(config,'$.interaction_only'),0) AS interaction_only "
        "FROM browsing_rounds WHERE json_extract(config,'$.review_session_id')=? ORDER BY created_at", scope,
    )]
    for row in rounds:
        row["budget"] = json.loads(row["budget"])
    intents = [dict(row) for row in db.execute(
        "SELECT i.kind,i.status,COUNT(*) AS count FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id "
        "WHERE json_extract(r.config,'$.review_session_id')=? GROUP BY i.kind,i.status", scope,
    )]
    messages = [dict(row) for row in db.execute(
        "SELECT contact_type,status,COUNT(*) AS count FROM dm_drafts WHERE created_at>=? AND account_id=? AND character_id=? GROUP BY contact_type,status",
        (session["started_at"], session["account_id"], session["character_id"]),
    )]
    attempts = db.execute(
        "SELECT COUNT(*) FROM operation_write_attempts WHERE started_at>=? AND account_id=?",
        (session["started_at"], session["account_id"]),
    ).fetchone()[0]
    return {"at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(), "viewed": len(viewed), "viewed_bvids": viewed,
            "material_proofs": [proofs[bvid] for bvid in viewed],
            "fully_read_available_material": sum(proof["fully_read_available_material"] for proof in proofs.values()),
            "watch_starts": len(watches), "rounds": rounds, "intents": intents, "private_states": messages,
            "platform_write_attempts": attempts}


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "run.json"
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=60, trust_env=False) as client:
        if args.resume:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response = client.get("/api/automation")
            response.raise_for_status()
            if not response.json()["policy"]["enabled"]:
                response = client.post("/api/automation/review", json={"resume_session_id": manifest["session"]["id"]})
                response.raise_for_status()
        else:
            response = client.get("/api/automation")
            response.raise_for_status()
            current = response.json()
            if current["state"] != "off" or "auto" in current["permissions"].values():
                raise SystemExit("Stop the parent and set every enabled channel to review before starting")
            response = client.post("/api/automation/review")
            response.raise_for_status()
            current = response.json()
            manifest = {"session": current["review_session"], "minimum_videos": args.minimum_videos,
                        "permissions": current["permissions"], "scope": "normal_scheduler_real_sources_review_only"}
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        session = manifest["session"]
        connection = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        previous = None
        try:
            while True:
                current = client.get("/api/automation")
                current.raise_for_status()
                current = current.json()
                if current["review_session"].get("id") != session["id"]:
                    raise RuntimeError("Review session changed; no longer observing this cohort")
                progress = observe(connection, session, args.db.resolve().parent)
                signature = json.dumps({key: value for key, value in progress.items() if key != "at"}, sort_keys=True)
                if signature != previous:
                    print(json.dumps(progress, ensure_ascii=False), flush=True)
                    with (args.output / "progress.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(progress, ensure_ascii=False) + "\n")
                    previous = signature
                if (progress["platform_write_attempts"] or "auto" in current["permissions"].values()
                        or current["review_session"].get("state") not in {"running", "starting"} and current["policy"]["enabled"]):
                    client.post("/api/automation/stop").raise_for_status()
                    raise RuntimeError("Review write boundary changed; stopped without further work")
                running = any(not row["interaction_only"] and row["status"] in {"queued", "running"} for row in progress["rounds"])
                if progress["viewed"] >= manifest["minimum_videos"] and not running:
                    client.post("/api/automation/stop").raise_for_status()
                    manifest["outcome"] = "cohort_collected_pending_semantic_review"
                    manifest["final"] = progress
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                    return
                if not current["policy"]["enabled"]:
                    manifest["outcome"] = "paused"
                    manifest["final"] = progress
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                    return
                time.sleep(10)
        finally:
            connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    parser.add_argument("--db", type=Path, default=Path("runtime/studio.sqlite3"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-videos", type=int, default=30)
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args()
    if arguments.minimum_videos < 30:
        parser.error("This review run requires at least 30 actually viewed videos")
    run(arguments)
