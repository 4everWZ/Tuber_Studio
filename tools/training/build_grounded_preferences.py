"""Build one fixed preference-and-retention pass from authored exchanges."""
import argparse
import json
from pathlib import Path

from grounded_sft import read_jsonl
from tuber_studio.browsing_inputs import ResponseDecisionInput
from tuber_studio.browsing_models import RESPONSE_INSTRUCTION, RESPONSE_REFERENCE_INSTRUCTION
from tuber_studio.context import ContextManager
from tuber_studio.dynamics import dynamic_decision_messages
from tuber_studio.persona import GROUNDED_DAFEIYU_MODEL
from tuber_studio.providers import single_system_messages

HERE = Path(__file__).parent
SETTINGS = {"llm": {"kind": "llama_cpp", "model": GROUNDED_DAFEIYU_MODEL}}
TRIPLES = {"rain-signal", "countdown", "wrong-switch", "extra-button", "silent-show", "new-time", "visit-day"}
VIDEO_PAIRS = (
    ("caminandes-00", "caminandes-05"), ("caminandes-07", "caminandes-12"),
    ("caminandes-19", "caminandes-22"), ("sintel-01", "sintel-04"),
    ("sintel-06", "sintel-09"), ("sintel-11", "sintel-14"),
)
RETENTION = (
    "caminandes-01-joint", "caminandes-03-joint", "caminandes-08-contrast", "caminandes-13-joint",
    "caminandes-15-draft", "caminandes-18-missing", "sintel-00-joint", "sintel-02-joint",
    "sintel-05-contrast", "sintel-07-draft", "sintel-08-missing", "sintel-10-joint",
    "encoding", "network", "ice", "music", "photography", "timezones", "humidity", "causal",
    "chatanime-38-live-3", "chatanime-53-live-3", "chatanime-46-live-2", "chatanime-54-live-2",
    "deepthink-oc-pocket-sketches-live-2", "deepthink-oc-patch-win-live-2",
    "teacher-oc-wall-flash-live-2", "teacher-oc-guard-booth-live-2",
    "operation-00-select", "operation-02-public", "operation-04-thread", "operation-08-private",
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def chat_prompt(character, mode, first, final, triple):
    event_type = {"live": "chat", "comment_draft": "comment", "direct_message": "message"}[mode]
    turns = [(first, "嗯，你接着说。")]
    if triple:
        turns.insert(0, ("安排有变化，我把现在的说法重新讲一遍。", "好，我听现在这一版。"))
    history = []
    for index, (question, answer) in enumerate(turns, 1):
        event = {"event_id": f"m{index}", "type": event_type, "text": question}
        _, _, pending, _ = ContextManager.parts(character, event, {}, mode, settings=SETTINGS)
        history.extend([pending, {"role": "assistant", "content": answer}])
    event = {"event_id": f"m{len(turns) + 1}", "type": event_type, "text": final}
    prefix, initial, current, suffix = ContextManager.parts(character, event, {}, mode, settings=SETTINGS)
    wire = single_system_messages(ContextManager.render(prefix, initial, history, current, suffix))
    return wire, {"spoken_text": "", "emotion": "idle", "animation_id": "talk", "reply_to": [event["event_id"]]}


def record(key, mode, messages, target, chosen, rejected, *, frames=None, sources=None):
    field = next(name for name in ("spoken_text", "content", "text") if name in target)
    return {
        "id": key, "mode": mode, "messages": messages,
        "chosen": target | {field: chosen}, "rejected": target | {field: rejected},
        "text_field": field, "frames": frames or [], "sources": sources or [],
    }


def authored(character):
    for line in (HERE / "grounded_preferences.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        mode, group, a, b, question, good_a, good_b = map(str.strip, line.split("|"))
        for arm, first, chosen, rejected in (("a", a, good_a, good_b), ("b", b, good_b, good_a)):
            if mode == "dynamic":
                source = {"id": "public1", "text": first, "fictional": "小剧场" in first}
                messages = dynamic_decision_messages(
                    character, SETTINGS, [], [source], current_time="2026-11-05T22:00:00+08:00",
                )
                target = {
                    "action": "post", "reason": "分享已公开的内容", "content": "", "source_ids": ["public1"],
                    "next_evaluation_seconds": 1800,
                }
            else:
                messages, target = chat_prompt(character, mode, first, question, group in TRIPLES)
            yield record(f"{group}-{arm}", mode, messages, target, chosen, rejected)


def video_preferences(rows):
    for a, b in VIDEO_PAIRS:
        texts = [json.loads(rows[f"{key}-draft"]["messages"][-1]["content"])["text"] for key in (a, b)]
        for arm, key, chosen, rejected in (("a", a, texts[0], texts[1]), ("b", b, texts[1], texts[0])):
            original = rows[f"{key}-joint"]
            evidence = [
                {"id": f"shot-{i}", "text": f"随附画面 {i}", "cid": "1", "kind": "visual",
                 "start": frame["seconds"], "end": frame["seconds"]}
                for i, frame in enumerate(original["frames"], 1)
            ]
            material = ResponseDecisionInput({
                "kind": "top_level", "thread": {"question": "结合这两格画面给一条具体的评论。"},
                "coverage": "sampled_visual", "evidence": evidence,
            })
            refs = [ref.alias for ref in material.references]
            messages = [
                {"role": "system", "content": "你是大肥鱼。\n\n" + RESPONSE_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION},
                {"role": "user", "content": "公开来源材料：" + material.format()},
            ]
            target = {"action": "reply_public", "reason": "specific_detail", "text": "", "evidence_ids": refs}
            frames = [frame | {"alias": alias} for frame, alias in zip(original["frames"], refs, strict=True)]
            yield record(
                f"frames-{a}-{b}-{arm}", "video", messages, target, chosen, rejected,
                frames=frames, sources=original["sources"],
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("corpus", "characters", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    rows = {r["id"]: r for r in read_jsonl(args.corpus / "train.jsonl")}
    character = next(r for r in json.loads(args.characters.read_text(encoding="utf-8")) if r["id"] == "whale")
    prefs = [*authored(character), *video_preferences(rows)]
    retention = [rows[key] for key in RETENTION]
    if len(prefs) != 96 or len(retention) != 32 or len({r["id"] for r in prefs}) != len(prefs):
        raise ValueError("expected 96 distinct preference records and 32 retained records")
    for row in prefs:
        if row["chosen"] == row["rejected"]:
            raise ValueError(f"same answer on both sides: {row['id']}")
    args.output.write_text(dump({"preferences": prefs, "retention": retention}) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
