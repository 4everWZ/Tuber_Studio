"""Replay held-out OC turns and independent frame cases in the workstation."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from grounded_sft import load_processor, model_inputs, read_jsonl


def generate(model, processor, inputs, maximum):
    import torch

    started = time.perf_counter()
    inputs = {
        key: value.to(device=model.device, dtype=model.dtype if value.is_floating_point() else value.dtype)
        for key, value in inputs.items()
    }
    with torch.inference_mode():
        result = model.generate(**inputs, max_new_tokens=maximum, do_sample=False, use_cache=True)
    torch.cuda.synchronize()
    prefix = inputs["input_ids"].shape[-1]
    return [
        {"text": text.strip(), "elapsed_s_batch": round(time.perf_counter() - started, 3)}
        for text in processor.tokenizer.batch_decode(result[:, prefix:], skip_special_tokens=True)
    ]


def evaluate_oc(model, processor, cases, prompt):
    histories = [[{"role": "system", "content": prompt}] for case in cases]
    output = [dict(case, replies=[]) for case in cases]
    tokenizer = processor.tokenizer
    tokenizer.padding_side = "left"
    for turn in range(max(len(case["turns"]) for case in cases)):
        active = [index for index, case in enumerate(cases) if turn < len(case["turns"])]
        for offset in range(0, len(active), 8):
            batch = active[offset : offset + 8]
            prompts = []
            for index in batch:
                histories[index].append({"role": "user", "content": cases[index]["turns"][turn]})
                prompts.append(
                    processor.apply_chat_template(
                        histories[index], tokenize=False, add_generation_prompt=True, enable_thinking=False
                    )
                )
            results = generate(
                model,
                processor,
                tokenizer(prompts, padding=True, return_tensors="pt", add_special_tokens=False),
                150,
            )
            for index, reply in zip(batch, results, strict=True):
                output[index]["replies"].append(reply)
                histories[index].append({"role": "assistant", "content": reply["text"]})
    return output


def replay_schema(aliases):
    def branch(action, reason, minimum, maximum):
        fields = {
            "action": {"type": "string", "enum": [action]},
            "reason": {"type": "string", "enum": [reason]},
            "text": {"type": "string", "minLength": minimum, "maxLength": 120 if minimum else 0},
            "evidence_ids": {
                "type": "array",
                "minItems": minimum,
                "maxItems": maximum,
                "items": {"type": "string", "enum": aliases} if aliases else {"type": "string"},
            },
        }
        return {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        }

    options = [branch("wait", "insufficient_evidence", 0, 0)]
    if aliases:
        options.insert(0, branch("reply_public", "specific_detail", 1, 8))
    return {"anyOf": options}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--adapter-scale", type=float, default=1.0)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--characters", type=Path, required=True)
    parser.add_argument("--full-card", action="store_true")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--replay", type=Path, help="use the frozen public video replay instead of validation")
    scope.add_argument("--oc-only", action="store_true", help="run only the fixed OC and general QA cases")
    parser.add_argument(
        "--instruction", help="additional system instruction for a separately labelled condition"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from peft import PeftModel
    from peft.tuners.lora import LoraLayer
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model_type = json.loads((args.base / "config.json").read_text(encoding="utf-8"))["model_type"]
    mini = model_type == "minicpmv4_7"
    processor = (
        load_processor(args.base) if mini else AutoProcessor.from_pretrained(args.base, local_files_only=True)
    )
    model = AutoModelForImageTextToText.from_pretrained(
        args.weights or args.base,
        dtype=torch.bfloat16,
        local_files_only=True,
        device_map={"": "cuda:0"},
    )
    if mini:
        model.config.downsample_mode = "4x"
    if args.adapter:
        model = PeftModel.from_pretrained(model, str(args.adapter))
        for module in model.modules():
            if isinstance(module, LoraLayer):
                module.set_scale("default", args.adapter_scale)
    model.eval()
    prompt = "你是大肥鱼。"
    if args.full_card:
        cards = json.loads(args.characters.read_text(encoding="utf-8"))
        card = next(card for card in cards if card["id"] == "whale")
        prompt = "以角色的身份自然回应观众。角色设定：" + json.dumps(
            {key: card[key] for key in ("core", "style", "rules")}, ensure_ascii=False
        )
    if args.instruction:
        prompt += "\n" + args.instruction
    if args.replay:
        rows = read_jsonl(args.replay / "cases.jsonl")
        evidence = []
        for row in rows:
            row["messages"][0]["content"] = prompt + "\n" + row["messages"][0]["content"]
            inputs = model_inputs(processor, args.replay.resolve(), row, generation=True)
            evidence.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "question": row["question"],
                    "criterion": row["criterion"],
                    "allowed": row["allowed"],
                    "references": row["references"],
                    **generate(model, processor, inputs, 220)[0],
                }
            )
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    oc = evaluate_oc(model, processor, read_jsonl(args.cases), prompt)
    video = []
    for row in [] if args.oc_only else read_jsonl(args.data / "validation.jsonl"):
        if row["category"] != "video":
            continue
        for condition in ("actual_frames", "human_observations") if row.get("frames") else ("text",):
            inputs = model_inputs(
                processor,
                args.data.resolve(),
                row,
                text_only=condition == "human_observations",
                generation=True,
            )
            video.append(
                {
                    "id": row["id"],
                    "condition": condition,
                    "reference": row["messages"][-1]["content"],
                    "prompt": row["messages"][-2]["content"],
                    **generate(model, processor, inputs, 256)[0],
                }
            )
    args.output.write_text(
        json.dumps({"oc": oc, "video": video}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
