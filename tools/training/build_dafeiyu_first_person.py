"""Build a paired V6 rebuild with and without the first-person OC rewrites."""

import argparse
import json
import random
import shutil
from pathlib import Path

from augment_grounded_persona import ANIMATIONS, authored
from build_grounded_corpus import dump, exchange, historical_grounded_character, sample, source
from draft_oc_dialogues import CHATANIME_SOURCE
from grounded_sft import FIRST_PERSON_EXPECTED, PAIRED_EXPECTED, read_corpus

from tuber_studio.context import ContextManager
from tuber_studio.dynamics import Dynamics, dynamic_decision_messages
from tuber_studio.models import OperationReply, Reply
from tuber_studio.persona import GROUNDED_DAFEIYU_MODEL
from tuber_studio.providers import single_system_messages

SETTINGS = {"llm": {"kind": "llama_cpp", "model": GROUNDED_DAFEIYU_MODEL}}
CHANNELS = {
    "live": ("live", "chat", Reply, "live_reply"),
    "comment": ("comment_draft", "comment", OperationReply, "operation_reply"),
    "dm": ("direct_message", "message", OperationReply, "operation_reply"),
}
REVISIONS = {
    "original-oc-small-choice": (
        "行，星图组端一片面包上路。队名管方向，队标管饿不饿。",
        "可以呀。你打算画哪种？我想要烤得边上有点焦的那种。",
    ),
    "chatanime-13": (
        "我也盯到衣角了，三笔就让布像真的折了一下。你先翻回那页，我想再瞄一眼。",
        "发张衣角给我看嘛，我想试试能不能也认出来。",
    ),
    "chatanime-04": (
        "你想让人先认出角色，那就先裁一张近的。大厅全景可以留给下一页，它也有自己的出场机会。",
        "我想先看清角色的脸。你裁一张近的看看，大厅那张我也想留着。",
    ),
    "chatanime-16": ("剪辑那位还没看群", "剪辑那位还没看到通知"),
    "chatanime-17": ("跟群里说轴要等片源", "跟剪片的人说轴要等片源"),
    "chatanime-19": ("被群友拿去发", "被另一个摊主拿去发了"),
    "deepthink-oc-wrong-channel": ("我把同人曲传错群了，现在游戏群里的人把跑调那句当战歌。", "我在游戏语音房放错歌了，队友现在把跑调那句当战歌。"),
}
TRANSFER_REPLACES = {"chatanime-22", "chatanime-29", "chatanime-52", "chatanime-17-followup"}


def dialogue_rows(character, channel, identity, provenance, fields):
    question, reply, followup, answer, first_mood, second_mood = fields
    yield sample(
        identity, "oc", "train",
        exchange(question, reply, character["compact_system_prompt"]) + exchange(followup, answer)[1:],
        provenance,
    )
    mode, event_type, schema, format_name = CHANNELS[channel]
    history = []
    for index, (text, spoken, mood) in enumerate(((question, reply, first_mood), (followup, answer, second_mood)), 1):
        event = {"event_id": f"m{index}", "type": event_type, "text": text}
        prefix, initial, pending, suffix = ContextManager.parts(character, event, {}, mode, settings=SETTINGS)
        messages = single_system_messages(ContextManager.render(prefix, initial, history, pending, suffix))
        target = schema(
            spoken_text=spoken, emotion=mood, animation_id=ANIMATIONS[mood], reply_to=[event["event_id"]],
        )
        yield sample(
            f"{identity}-{index}", "oc", "train",
            messages + [{"role": "assistant", "content": target.model_dump_json()}],
            provenance, format=format_name,
        )
        history.extend([pending, {"role": "assistant", "content": spoken}])


def dynamic_row(character, identity, fields):
    kind, first, second, body = fields
    if kind not in {"fiction", "public"}:
        raise ValueError(f"{identity}: invalid source kind")
    sources = [
        {"id": f"public{index}", "text": text, "fictional": kind == "fiction"}
        for index, text in enumerate(filter(None, (first, second)), 1)
    ]
    messages = dynamic_decision_messages(character, SETTINGS, [], sources, current_time="2026-11-06T20:00:00+08:00")
    target = Dynamics._parse({
        "action": "post", "reason": "有可分享的公开素材", "content": body,
        "source_ids": [item["id"] for item in sources], "next_evaluation_seconds": 1800,
    }, sources)
    return sample(
        identity, "oc", "train", messages + [{"role": "assistant", "content": dump(target)}],
        source(identity), format="dynamic_decision",
    )


def revise_dialogues(rows):
    for identity, (old, new) in REVISIONS.items():
        touched = 0
        for row in rows:
            if row["id"] != identity and not row["id"].startswith(identity + "-live-"):
                continue
            for message in row["messages"]:
                if old in message["content"]:
                    message["content"] = message["content"].replace(old, new)
                    touched += 1
        if touched < 2:
            raise ValueError(f"{identity}: expected the source and native reply to match the recorded V6 text")


def anime_transfer_rows():
    for number, name, card, first, reply, second, answer in authored(
        Path(__file__).with_name("grounded_anime_transfer_dialogues.txt")
    ):
        yield sample(
            f"anime-transfer-{name}", "oc", "train", exchange(first, reply, card) + exchange(second, answer)[1:],
            source(f"chatanime-{number}", CHATANIME_SOURCE, "Apache-2.0"),
        )


def build(original, output, characters, *, control):
    splits = read_corpus(original, pilot=False, expected=PAIRED_EXPECTED)
    whale = next(row for row in json.loads(characters.read_text(encoding="utf-8")) if row["id"] == "whale")
    character = historical_grounded_character(whale)
    added = []
    for channel, name, *fields in authored(Path(__file__).with_name("grounded_dafeiyu_first_person.txt")):
        identity = f"first-person-{channel}-{name}"
        if channel == "dynamic":
            added.append(dynamic_row(character, identity, fields))
        else:
            if name.startswith("chatanime-"):
                group = "-".join(name.split("-", 2)[:2])
                provenance = source(group, CHATANIME_SOURCE, "Apache-2.0")
            elif name.startswith(("qq-", "tail-")):
                provenance = source(identity, "urn:tuber:reference:qq-persona:e196985e")
            else:
                provenance = source(identity)
            added.extend(dialogue_rows(character, channel, identity, provenance, fields))
    retained, old = [], []
    for row in splits["train"]:
        (old if row["id"].startswith("rp-") else retained).append(row)
    if len(old) != 240 or any(row["review"] != "model_reviewed" for row in old):
        raise ValueError("expected the 240 legacy V6 drafts")
    if control:
        selected = {row["id"] for row in random.Random(42).sample(old, len(added))}
        splits["train"] = [row for row in splits["train"] if not row["id"].startswith("rp-") or row["id"] in selected]
    else:
        transfer = list(anime_transfer_rows())
        removed_ids = TRANSFER_REPLACES | {
            f"{identity}-live-{turn}" for identity in TRANSFER_REPLACES for turn in (1, 2)
        }
        kept = [row for row in retained if row["id"] not in removed_ids]
        if len(retained) - len(kept) != len(transfer):
            raise ValueError("the anime transfer set must replace the same number of OC rows")
        retained = kept
        revise_dialogues(retained)
        splits["train"] = retained + added + transfer
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(original / "frames", output / "frames")
    for split, rows in splits.items():
        (output / f"{split}.jsonl").write_text("".join(dump(row) + "\n" for row in rows), encoding="utf-8")
    read_corpus(output, pilot=False, expected=FIRST_PERSON_EXPECTED)
    print(json.dumps({"control": control, "legacy_drafts": len(added) if control else 0,
                      "new_oc_rows": 0 if control else len(added), "training": len(splits["train"]),
                      "validation": len(splits["validation"])}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--characters", type=Path, required=True)
    parser.add_argument("--control", action="store_true", help="keep an equal number of randomly sampled old drafts")
    args = parser.parse_args()
    build(args.original, args.output, args.characters, control=args.control)


if __name__ == "__main__":
    main()
