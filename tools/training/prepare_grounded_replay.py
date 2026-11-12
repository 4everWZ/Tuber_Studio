"""Bind historical Grounded replay questions to cached CC and storyboard frames."""

import argparse
import json
import shutil
from pathlib import Path

from grounded_sft import read_jsonl

from tuber_studio.operations.browsing.inputs import ResponseDecisionInput
from tuber_studio.operations.browsing.models import RESPONSE_REFERENCE_INSTRUCTION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--storyboards", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = {}
    for path in sorted(args.cache.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("parts") and value.get("metadata", {}).get("bvid"):
            records.setdefault(value["metadata"]["bvid"], value)
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "cases.jsonl").open("w", encoding="utf-8") as output:
        for case in read_jsonl(args.cases):
            source = records[case["bvid"]]
            part = source["parts"][0]
            selected, frames = [], []
            frame_ids = []
            if "segments" in case:
                start, end = case["segments"]
                selected.extend(
                    dict(item, cid=str(part["cid"])) for item in part["segments"][start : end + 1]
                )
                if len(selected) != end - start + 1:
                    raise ValueError(f"{case['id']}: public subtitle sample missing")
            if "frames" in case:
                storyboard = json.loads(
                    (args.storyboards / f"{case['bvid']}.json").read_text(encoding="utf-8")
                )
                for number in case["frames"]:
                    frame = storyboard["frames"][number]
                    if str(frame["cid"]) != str(part["cid"]) or frame["bvid"] != case["bvid"]:
                        raise ValueError(f"{case['id']}: wrong storyboard source")
                    filename = frame["image_path"]
                    shutil.copyfile(args.storyboards / filename, args.output / filename)
                    selected.append(
                        {
                            "id": frame["id"],
                            "cid": str(frame["cid"]),
                            "text": f"随附画面 {len(frames) + 1}",
                            "start": frame["actual_time"],
                            "end": frame["actual_time"],
                        }
                    )
                    frames.append({"path": filename, "seconds": frame["actual_time"]})
                    frame_ids.append(frame["id"])
            packet = ResponseDecisionInput(
                {
                    "kind": "reply",
                    "thread": {"question": case["question"]},
                    "coverage": "sampled_visual" if frames else "excerpt",
                    "evidence": selected,
                }
            )
            aliases = {ref.canonical_id: ref.alias for ref in packet.references}
            for frame, frame_id in zip(frames, frame_ids, strict=True):
                frame["alias"] = aliases[frame_id]
            instruction = (
                "回应当前观众的问题。根据提供的字幕与画面说具体事实。"
                "材料足够时用reply_public，text写角色台词，reason用specific_detail。"
                "问题缺相关证据时用wait，reason用insufficient_evidence，text为空。"
                "只返回JSON对象，包含action、reason、text、evidence_ids。" + RESPONSE_REFERENCE_INSTRUCTION
            )
            row = case | {
                "frames": frames,
                "allowed": sorted(packet.allowed_aliases),
                "references": {ref.alias: ref.source() for ref in packet.references},
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": packet.format()},
                    {"role": "assistant", "content": ""},
                ],
            }
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
