"""Add original Da Feiyu dialogue and grounded reaction demonstrations."""

import argparse
import json
import shutil
from pathlib import Path

from build_grounded_corpus import (
    ANCHOR,
    RESPONSE_INSTRUCTION,
    dump,
    exchange,
    historical_grounded_character,
    historical_parts,
    sample,
    source,
)
from draft_oc_dialogues import CHATANIME_SOURCE
from grounded_sft import (
    ANIME_EXPECTED,
    DEEPTHINK_EXPECTED,
    FOCUSED_EXPECTED,
    PAIRED_CURATED_EXPECTED,
    PAIRED_EXPECTED,
    PAIRED_TEACHER_EXPECTED,
    PERSONA_EXPECTED,
    read_corpus,
)

from tuber_studio.context import ContextManager
from tuber_studio.models import Reply
from tuber_studio.operations.browsing.inputs import ResponseDecisionInput
from tuber_studio.operations.browsing.models import RESPONSE_REFERENCE_INSTRUCTION
from tuber_studio.providers import single_system_messages

HERE = Path(__file__).parent
ANIMATIONS = {"idle": "talk", "cheerful": "talk_happy", "excited": "talk_happy", "shy": "shy"}


def authored(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            yield tuple(part.strip() for part in line.split("|"))


def authored_dialogues(chatanime):
    path = HERE / ("grounded_chatanime_dialogues.txt" if chatanime else "grounded_oc_dialogues.txt")
    for key, first, first_reply, second, second_reply, first_emotion, second_emotion in authored(path):
        if chatanime:
            split = "validation" if int(key) % 7 == 0 else "train"
            identity = f"chatanime-{int(key):02d}"
            provenance = source(identity, CHATANIME_SOURCE, "Apache-2.0")
        else:
            split = "validation" if key.startswith("val-") else "train"
            identity = f"original-oc-{key}"
            provenance = source(identity)
        yield (
            identity,
            split,
            provenance,
            (first, first_reply, second, second_reply, first_emotion, second_emotion),
        )


def deepthink_dialogues():
    for split, key, *turns in authored(HERE / "grounded_deepthink_dialogues.txt"):
        if split not in {"train", "validation"}:
            raise ValueError(f"{key}: invalid split {split}")
        if key.isdecimal():
            identity = f"chatanime-{int(key):02d}"
            provenance = source(identity, CHATANIME_SOURCE, "Apache-2.0")
        else:
            identity = f"deepthink-oc-{key}"
            provenance = source(identity)
        yield identity, split, provenance, turns


def paired_dialogues():
    for split, key, first_a, first_b, ack, followup, answer_a, answer_b, mood in authored(
        HERE / "grounded_paired_dialogues.txt"
    ):
        group = f"paired-live-{key}"
        if split not in {"train", "validation"} or first_a == first_b or answer_a == answer_b:
            raise ValueError(f"{group}: invalid history pair")
        for arm, first, answer in (("a", first_a, answer_a), ("b", first_b, answer_b)):
            yield f"{group}-{arm}", split, source(group), (first, ack, followup, answer, "idle", mood)


def expanded_chatanime_dialogues():
    for key, *turns in authored(HERE / "grounded_chatanime_followups.txt"):
        number, variant, remainder = key.partition("-")
        group = f"chatanime-{int(number):02d}"
        identity = f"{group}{variant}{remainder}"
        yield identity, "train", source(group, CHATANIME_SOURCE, "Apache-2.0"), turns


def live_rows(character, identity, split, provenance, turns):
    character = historical_grounded_character(character)
    history = []
    for index, (question, answer, emotion) in enumerate(turns, 1):
        event = {"event_id": f"m{index}", "type": "chat", "text": question}
        prefix, initial, pending, suffix = historical_parts(event, "live", character["compact_system_prompt"])
        wire = single_system_messages(ContextManager.render(prefix, initial, [], pending, suffix))
        target = Reply(
            spoken_text=answer,
            emotion=emotion,
            animation_id=ANIMATIONS[emotion],
            reply_to=[event["event_id"]],
        ).model_dump_json()
        yield sample(
            f"{identity}-live-{index}",
            "oc",
            split,
            [wire[0], *history, *wire[1:], {"role": "assistant", "content": target}],
            provenance,
            format="live_reply",
        )
        history.extend([wire[-1], {"role": "assistant", "content": answer}])


def conversation_rows(character, dialogues, *, live_only=False):
    for identity, split, provenance, (
        first,
        first_reply,
        second,
        second_reply,
        first_emotion,
        second_emotion,
    ) in dialogues:
        if not live_only:
            dialogue = exchange(first, first_reply) + exchange(second, second_reply)[1:]
            yield sample(identity, "oc", split, dialogue, provenance)
        turns = ((first, first_reply, first_emotion), (second, second_reply, second_emotion))
        yield from live_rows(character, identity, split, provenance, turns)


def teacher_rows(character):
    for mode, key, *turns in authored(HERE / "grounded_teacher_dialogues.txt"):
        identity = f"teacher-oc-{key}"
        for row in conversation_rows(character, [(identity, "train", source(identity), turns)]):
            yield row | {"teacher_mode": mode}
    history = {
        identity: (split, provenance, turns) for identity, split, provenance, turns in deepthink_dialogues()
    }
    for mode, key, user, answer, emotion in authored(HERE / "grounded_chatanime_continuations.txt"):
        identity = f"chatanime-{int(key):02d}"
        split, provenance, (first, first_reply, second, second_reply, first_emotion, second_emotion) = (
            history[identity]
        )
        turns = (
            (first, first_reply, first_emotion),
            (second, second_reply, second_emotion),
            (user, answer, emotion),
        )
        *_, final = live_rows(character, identity, split, provenance, turns)
        yield final | {"teacher_mode": mode}


def reaction_rows(splits):
    indexed = {row["id"]: row for rows in splits.values() for row in rows}
    for key, question, reaction in authored(HERE / "grounded_oc_video.txt"):
        original = indexed[f"{key}-react"]
        captions = json.loads(indexed[f"{key}-joint"]["messages"][-1]["content"])["captions"]
        frames = original["frames"]
        observed = [
            {
                "id": f"shot-{i}",
                "text": caption,
                "cid": "1",
                "start": frame["seconds"],
                "end": frame["seconds"],
                "kind": "visual",
            }
            for i, (frame, caption) in enumerate(zip(frames, captions, strict=True), 1)
        ]
        visible = [item | {"text": f"随附画面 {i}"} for i, item in enumerate(observed, 1)]
        origin = original["sources"] + source(f"authored-oc-reaction-{original['split']}")
        identity = key + "-oc-reaction"
        observation = "\n".join(f"画面 {i}：{text}" for i, text in enumerate(captions, 1))
        yield sample(
            identity,
            "video",
            original["split"],
            exchange(question, reaction),
            origin,
            frames=frames,
            text_messages=exchange(observation + "\n" + question, reaction),
        )

        def packet(segments):
            return ResponseDecisionInput(
                {
                    "kind": "reply",
                    "thread": {"question": question},
                    "evidence": segments,
                    "coverage": "sampled_visual",
                }
            )

        def structured(value):
            target = {
                "action": "reply_public",
                "reason": "specific_detail",
                "text": reaction,
                "evidence_ids": [ref.alias for ref in value.references],
            }
            value.validate_wire(target)
            return exchange(
                value.format(),
                dump(target),
                ANCHOR + "\n\n" + RESPONSE_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION,
            )

        value = packet(visible)
        aliases = {ref.canonical_id: ref.alias for ref in value.references}
        yield sample(
            identity + "-wire",
            "video",
            original["split"],
            structured(value),
            origin,
            frames=[frame | {"alias": aliases[f"shot-{i}"]} for i, frame in enumerate(frames, 1)],
            text_messages=structured(packet(observed)),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--characters", type=Path, required=True)
    parser.add_argument("--chatanime", action="store_true", help="include the authored ChatAnime rewrites")
    parser.add_argument(
        "--deepthink",
        action="store_true",
        help="include both sets of curated ChatAnime rewrites and OC dialogues",
    )
    parser.add_argument(
        "--paired-context",
        nargs="?",
        const="all",
        choices=("all", "curated", "teacher"),
        help="add context pairs, retain V2 video, and select the roleplay source mix",
    )
    args = parser.parse_args()
    splits = read_corpus(args.original, pilot=False, expected=FOCUSED_EXPECTED)
    character = next(
        row for row in json.loads(args.characters.read_text(encoding="utf-8")) if row["id"] == "whale"
    )
    additions = list(conversation_rows(character, authored_dialogues(False)))
    if not args.paired_context:
        additions.extend(reaction_rows(splits))
    expected = PERSONA_EXPECTED
    if args.chatanime or args.deepthink or args.paired_context:
        additions.extend(conversation_rows(character, authored_dialogues(True)))
        expected = ANIME_EXPECTED
    if args.deepthink or args.paired_context:
        additions.extend(conversation_rows(character, deepthink_dialogues()))
        expected = DEEPTHINK_EXPECTED
    if args.paired_context:
        additions.extend(conversation_rows(character, paired_dialogues(), live_only=True))
        additions.extend(conversation_rows(character, expanded_chatanime_dialogues()))
        expected = PAIRED_EXPECTED
    if args.paired_context in {"curated", "teacher"}:
        splits["train"] = [row for row in splits["train"] if row["category"] != "oc"]
        expected = PAIRED_CURATED_EXPECTED
    if args.paired_context == "teacher":
        additions.extend(teacher_rows(character))
        expected = PAIRED_TEACHER_EXPECTED
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(args.original / "frames", args.output / "frames")
    for split, rows in splits.items():
        rows.extend(row for row in additions if row["split"] == split)
        (args.output / f"{split}.jsonl").write_text(
            "".join(dump(row) + "\n" for row in rows), encoding="utf-8"
        )
    read_corpus(args.output, pilot=False, expected=expected)


if __name__ == "__main__":
    main()
