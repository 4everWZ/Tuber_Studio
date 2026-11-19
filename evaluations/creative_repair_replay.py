"""Replay saved P2 visual reviews offline, then reconcile each repair with its earlier candidate receipts."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from evaluations.creative_browser_receipt import inspect_file
from tuber_studio.agent.task_tools import apply_repair_candidate_receipts, decode_review_frame
from tuber_studio.agent.visual_specificity import review_visual_specificity


async def replay(directory):
    snapshot = json.loads((directory / "task-evidence.json").read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in snapshot["steps"]}
    rejected_candidates = []
    results = []
    for record in snapshot["plan"]["visual_reviews"]:
        primary = record["primary_frame"]
        step_id, sample = primary.rsplit(":", 1)
        result = steps[step_id]["result"]
        clip = result["clip"]
        finding = result["findings"][int(sample) - 1]
        seconds = finding["seconds"]
        frame = record["frame"]
        if (clip["bvid"], finding["part"], str(finding["cid"]), seconds) != (
                frame["bvid"], frame["part"], str(frame["cid"]), frame["seconds"]):
            raise ValueError("原读源回执与审稿主帧坐标不同")
        media = directory / (str(UUID(result.get("request_id", step_id))) + ".mp4")
        inspect_file(media, clip)
        image = await decode_review_frame(media, clip, seconds)
        responses = iter(record["stages"])

        async def frozen(messages, schema, *, max_tokens):
            return next(responses)["reply"]

        audit = []
        upstream = record["upstream_observations"]
        fields = record["fields"]
        purpose = record["purpose"]
        disputed = await review_visual_specificity(
            fields, image, primary, upstream["sampling"], frozen, 4096,
            inventory=upstream["inventory"], reviews=audit, frame=frame, purpose=purpose)
        if next(responses, None) is not None or audit != [record]:
            raise ValueError(f"{primary} {purpose} 未重现原始审稿")
        if purpose.startswith("repair_options:"):
            field = purpose.removeprefix("repair_options:")
            rejected_candidates.extend(
                row | {"field": field, "stage": "review_repair_candidate", "candidate": fields[index]}
                for index, row in disputed.items())
        if purpose == "repair":
            apply_repair_candidate_receipts(disputed, fields, primary, rejected_candidates)
        results.append({"purpose": purpose, "primary_frame": primary, "seconds": seconds,
                        "window": [clip["start"], clip["end"]], "raw_decisions": record["decisions"],
                        "reviewed_fields": fields, "calls": len(record["stages"]),
                        "effective_objections": disputed})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    results = asyncio.run(replay(args.directory))
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(results)} 份审稿记录，{sum(r['calls'] for r in results)} 个阶段回包")
