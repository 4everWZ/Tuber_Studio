"""Add paired visual/subtitle and missing-evidence cases to the original corpus."""

import argparse
import json
import shutil
from pathlib import Path

from build_grounded_corpus import ANCHOR, dump, exchange, sample, source
from grounded_sft import EXPECTED, FOCUSED_EXPECTED, read_corpus

from tuber_studio.operations.browsing.inputs import ResponseDecisionInput
from tuber_studio.operations.browsing.models import RESPONSE_REFERENCE_INSTRUCTION

# Original subtitles authored for these exercises. These state facts about the
# exercise or edit that cannot be read off the two licensed storyboard frames.
CUES = (
    ("这段是第二次试剪。", "字幕说这是第几次试剪？", "字幕说这是第二次试剪。"),
    ("这一段配乐由小岚负责。", "字幕把配乐交给了谁？", "配乐是小岚负责的。"),
    ("片段编号改为 C17。", "字幕里新编号是多少？", "新编号是 C17。"),
    ("编辑把这里叫作第一处转折。", "字幕如何称呼这段？", "编辑称这里为第一处转折。"),
    ("旁白约好周六补录。", "旁白计划哪天补录？", "旁白打算周六补录。"),
    ("作者决定保留原速。", "字幕说这里要调快还是留原速？", "作者决定留原速。"),
    ("最终用的是阿澄提交的剪辑。", "字幕说采纳了谁的剪辑？", "用的是阿澄提交的剪辑。"),
    ("这一版会配一段木琴。", "字幕预告用什么乐器？", "这版预告会配木琴。"),
    ("导演将本段归在开场之后。", "字幕说明这段接在哪儿？", "这段放在开场后面。"),
    ("这个段落还会缩短两秒。", "字幕计划缩短多久？", "还要缩短两秒。"),
    ("解说配音安排在下午。", "字幕说什么时候录解说？", "下午录解说。"),
)
EXPLANATION = (
    "回应当前观众的问题。根据提供的字幕与画面说具体事实。"
    "材料足够时用reply_public，text写角色台词，reason用specific_detail。"
    "问题缺相关证据时用wait，reason用insufficient_evidence，text为空。"
    "只返回JSON对象，包含action、reason、text、evidence_ids。"
    + RESPONSE_REFERENCE_INSTRUCTION
)


def material(identity, text, at, kind):
    return {"id": identity, "text": text, "cid": "1", "start": at, "end": at, "kind": kind}


def rows_for(scene, observations, cue, index):
    frames = scene["frames"]
    at = frames[0]["seconds"]
    caption, subtitle_question, subtitle_answer = cue
    visual_question = scene["messages"][-2]["content"]
    question = (
        f"{visual_question} 还有，{subtitle_question}"
        if index % 2
        else f"{subtitle_question} 顺便看一下，{visual_question}"
    )
    response = (
        scene["messages"][-1]["content"] + subtitle_answer
        if index % 2
        else subtitle_answer + scene["messages"][-1]["content"]
    )
    cc = material("cc", caption, at, "cc")
    distractor = material("other", "这版用于内部审阅。", at - 0.25, "cc")
    shot_refs = [
        material(f"shot-{i}", f"随附画面 {i}", frame["seconds"], "visual")
        for i, frame in enumerate(frames, 1)
    ]
    factual_shots = [
        shot | {"text": observed["text"]}
        for shot, observed in zip(shot_refs, observations, strict=True)
    ]
    slot = index % (len(shot_refs) + 1)

    def order(shots):
        return [*shots[:slot], distractor, cc, *shots[slot:]]

    def packet(segments):
        return ResponseDecisionInput({
            "kind": "reply", "thread": {"question": question}, "evidence": segments,
            "coverage": "sampled_visual" if any(s["kind"] == "visual" for s in segments) else "excerpt",
        })

    def dialogue(segments, enough):
        value = packet(segments)
        supporting = [ref.alias for ref in value.references if ref.canonical_id != "other"]
        target = (
            {"action": "reply_public", "reason": "specific_detail", "text": response, "evidence_ids": supporting}
            if enough else
            {"action": "wait", "reason": "insufficient_evidence", "text": "", "evidence_ids": []}
        )
        value.validate_wire(target)
        return exchange(value.format(), dump(target), ANCHOR + "\n\n" + EXPLANATION)

    split = scene["split"]
    prov = scene["sources"] + source(f"authored-cues-{split}")
    variants = (
        ("paired", order(shot_refs), order(factual_shots), frames, True),
        ("observations", order(factual_shots), None, [], True),
        ("no-cc", shot_refs, factual_shots, frames, False),
        ("no-frame", [distractor, cc], None, [], False),
    )
    for name, given, text, pictures, enough in variants:
        extra = {"frames": pictures, "text_messages": dialogue(text, enough)} if pictures else {}
        yield sample(
            scene["id"] + "-v2-" + name, "video", split, dialogue(given, enough), prov, **extra,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = read_corpus(args.original, pilot=False, expected=EXPECTED)
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(args.original / "frames", args.output / "frames")
    for split, rows in original.items():
        indexed = {row["id"]: row for row in rows}
        for index, scene in enumerate(row for row in list(rows) if row["id"].endswith("-react")):
            joint = indexed[scene["id"].removesuffix("-react") + "-joint"]
            captions = json.loads(joint["messages"][-1]["content"])["captions"]
            rows.extend(rows_for(
                scene, [{"text": caption} for caption in captions], CUES[index % len(CUES)], index,
            ))
        (args.output / f"{split}.jsonl").write_text(
            "".join(dump(row) + "\n" for row in rows), encoding="utf-8",
        )
    read_corpus(args.output, pilot=False, expected=FOCUSED_EXPECTED)


if __name__ == "__main__":
    main()
