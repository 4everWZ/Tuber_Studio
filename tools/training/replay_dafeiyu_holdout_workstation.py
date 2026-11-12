"""Replay the frozen release suite on a pinned local BF16 checkpoint, with optional DoRA."""

import argparse
import json
from pathlib import Path

from evaluations.dafeiyu_holdout import (
    DEFAULT_CASES,
    DEFAULT_CHARACTERS,
    DEFAULT_MANIFEST,
    SUITE_COUNTS,
    cases_digest,
    checked_result,
    identifier,
    load_frozen,
    messages_for_turn,
    replay_protocol,
    select_replay_cases,
    write_json,
)


def reply_from_tokens(tokenizer, tokens, eos_ids):
    stop = next((index for index, token in enumerate(tokens) if token in eos_ids), None)
    return {
        "text": tokenizer.decode(tokens if stop is None else tokens[:stop], skip_special_tokens=True).strip(),
        "finish_reason": "length" if stop is None else "stop",
    }


def replay(cases, holdout_sha, protocol, model, tokenizer, *, artifact, revision, output, batch_size):
    import torch

    partial = output.with_suffix(output.suffix + ".partial")
    if output.exists() or partial.exists():
        raise FileExistsError("Choose a new replay output path")
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    eos = model.generation_config.eos_token_id
    eos_ids = {eos} if isinstance(eos, int) else set(eos or [])
    if tokenizer.eos_token_id is None:
        raise ValueError("The pinned tokenizer has no native stop")
    eos_ids.add(tokenizer.eos_token_id)
    stops = sorted(eos_ids)
    replies = {case["id"]: [] for case in cases}
    metadata = {
        "holdout_sha256": holdout_sha,
        "protocol_sha256": cases_digest(protocol),
        "artifact": identifier(artifact),
        "base_revision": identifier(revision),
        "case_ids": list(replies),
        "native_eos_ids": stops,
    }

    def result():
        return metadata | {
            "rows": [
                {"id": case["id"], "replies": replies[case["id"]]}
                for case in cases
                if len(replies[case["id"]]) == len(case["turns"])
            ]
        }

    for turn in range(max(len(case["turns"]) for case in cases)):
        active = []
        for case in cases:
            if turn >= len(case["turns"]):
                continue
            messages = messages_for_turn(case, [reply["text"] for reply in replies[case["id"]]], protocol)
            text = tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=protocol["enable_thinking"],
            )
            active.append((case["id"], text))
        active.sort(key=lambda item: len(item[1]))
        for index in range(0, len(active), batch_size):
            batch = active[index : index + batch_size]
            inputs = tokenizer(
                [text for _, text in batch], add_special_tokens=False, return_tensors="pt", padding=True
            ).to(model.device)
            with torch.inference_mode():
                sequences = model.generate(
                    **inputs,
                    do_sample=False,
                    use_cache=True,
                    max_new_tokens=protocol["max_tokens"],
                    pad_token_id=tokenizer.pad_token_id,
                    eos_token_id=stops,
                )
            new_tokens = sequences[:, inputs["input_ids"].shape[1] :].tolist()
            for (case_id, _), tokens in zip(batch, new_tokens, strict=True):
                replies[case_id].append(reply_from_tokens(tokenizer, tokens, eos_ids))
            current = result()
            write_json(partial, current)
            print(json.dumps({"turn": turn + 1, "complete_cases": len(current["rows"])}), flush=True)

    checked_result(result(), cases, holdout_sha)
    partial.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--characters", type=Path, default=DEFAULT_CHARACTERS)
    parser.add_argument(
        "--suite", choices=(*SUITE_COUNTS, "all"), action="append",
        help="default: general and OC; operations and all include historical synthetic cases",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(args.output.suffix + ".partial").exists():
        parser.error("Choose a new replay output path")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer, set_seed

    cases, holdout_sha = load_frozen(args.cases, args.manifest)
    cases = select_replay_cases(cases, args.suite)
    protocol = replay_protocol(args.characters, args.max_tokens)
    set_seed(protocol["seed"])
    source = {"revision": args.revision, "local_files_only": True}
    tokenizer = AutoTokenizer.from_pretrained(args.model, **source)
    if not tokenizer.chat_template or tokenizer.pad_token_id is None:
        raise ValueError("Replay needs the pinned native template and a padding token")
    tokenizer.padding_side = "left"
    model = AutoModelForImageTextToText.from_pretrained(
        args.model, **source, device_map={"": "cuda:0"}, dtype=torch.bfloat16
    )
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter, local_files_only=True).merge_and_unload()
    model.eval()
    replay(
        cases,
        holdout_sha,
        protocol,
        model,
        tokenizer,
        artifact=args.artifact,
        revision=args.revision,
        output=args.output,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
