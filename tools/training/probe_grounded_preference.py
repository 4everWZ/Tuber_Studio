"""Score fixed spoken replies directly, without sampling or A/B output labels."""
import argparse
from pathlib import Path
import json

import torch
from peft import PeftModel
from transformers import AutoModelForImageTextToText

from grounded_sft import load_processor, read_jsonl
from probe_grounded_live import plain_messages


def continuation_score(model, tokenizer, prompt, answer):
    encoded = tokenizer(prompt + answer, add_special_tokens=False, return_offsets_mapping=True, return_tensors="pt")
    offsets = encoded.pop("offset_mapping")[0].tolist()
    positions = torch.tensor(
        [i for i, (start, end) in enumerate(offsets) if end > len(prompt) and start < len(prompt) + len(answer)],
        device=model.device,
    )
    if not len(positions) or positions[0] == 0:
        raise ValueError("answer tokens were not separated from the prompt")
    encoded = encoded.to(model.device)
    logits = model(**encoded, use_cache=False).logits[0, positions - 1]
    targets = encoded.input_ids[0, positions, None]
    log_probs = logits.float().log_softmax(dim=-1).gather(-1, targets)
    return {"mean_logp": float(log_probs.mean()), "tokens": len(positions)}


def score(args, model, processor):
    specs = {row["pair"]: row for row in read_jsonl(args.interventions)}
    for case in read_jsonl(args.cases):
        spec = specs[case["pair"]]
        previous, question = case["turns"]
        rows = {}
        for mode, latest in (
            ("history", question),
            ("recapped", f"前面我说过「{previous}」现在问你，{question}"),
        ):
            prompt = processor.apply_chat_template(
                plain_messages(previous, spec["history_reply"], latest),
                tokenize=False, add_generation_prompt=True, enable_thinking=False,
            )
            rows[mode] = [
                continuation_score(model, processor.tokenizer, prompt, answer)
                for answer in spec["options"]
            ]
        yield case | {"candidates": spec["options"], "scores": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "adapter", "cases", "interventions", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    processor = load_processor(args.base)
    model = AutoModelForImageTextToText.from_pretrained(
        args.base, dtype=torch.bfloat16, local_files_only=True, device_map={"": "cuda:0"},
    )
    model = PeftModel.from_pretrained(model, str(args.adapter)).eval()
    with torch.inference_mode():
        results = {"v7": list(score(args, model, processor))}
        with model.disable_adapter():
            results["base"] = list(score(args, model, processor))
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
