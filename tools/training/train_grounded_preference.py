"""One-pass V6-referenced preference update, with a fixed retention quarter."""
import argparse
import json
import random
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForImageTextToText, set_seed

from grounded_sft import Examples, MAX_TOKENS, REVISION, assistant_spans, load_processor, model_inputs

BETA = 0.1
MICROBATCHES = 4
CHECKPOINTS = (8, 16, 32)
LEARNING_RATE = 2e-5


def scored_answer(processor, corpus, row, side):
    target = json.dumps(row[side], ensure_ascii=False, separators=(",", ":"))
    field = row["text_field"]
    marker = json.dumps(field) + ":"
    start = target.index(marker) + len(marker) + 1
    stop = start + len(json.dumps(row[side][field], ensure_ascii=False)) - 2
    messages = [*row["messages"], {"role": "assistant", "content": target}]
    data = model_inputs(processor, corpus, {"id": row["id"], "messages": messages, "frames": row["frames"]})
    tokens = data["input_ids"][0].tolist()
    if len(tokens) > MAX_TOKENS:
        raise ValueError(f"{row['id']}: prompt exceeds {MAX_TOKENS} tokens")
    begin, end = assistant_spans(processor.tokenizer, tokens)[-1]
    encoded = processor.tokenizer(target, add_special_tokens=False, return_offsets_mapping=True)
    if tokens[begin:end - 1] != encoded.input_ids:
        raise ValueError(f"{row['id']}: target tokens differ from native template")
    positions = [
        begin + offset for offset, (left, right) in enumerate(encoded.offset_mapping)
        if left < stop and right > start
    ]
    if not positions:
        raise ValueError(f"{row['id']}: no supervised prose tokens")
    return data, positions


def device_inputs(model, data):
    return {
        key: value.to(device=model.device, dtype=model.dtype if value.is_floating_point() else value.dtype)
        for key, value in data.items()
    }


def spoken_logp(model, answer):
    data, positions = answer
    encoded = device_inputs(model, data)
    indices = torch.tensor(positions, device=model.device)
    logits = model(**encoded, use_cache=False).logits[0, indices - 1].float()
    targets = encoded["input_ids"][0, indices, None]
    return logits.log_softmax(dim=-1).gather(-1, targets).mean()


def preference_backward(model, row, processor, corpus):
    chosen = scored_answer(processor, corpus, row, "chosen")
    rejected = scored_answer(processor, corpus, row, "rejected")
    with torch.no_grad():
        with model.disable_adapter():
            reference_margin = spoken_logp(model, chosen) - spoken_logp(model, rejected)
        margin = spoken_logp(model, chosen) - spoken_logp(model, rejected)
        scaled = BETA * (margin - reference_margin)
        loss = -torch.nn.functional.logsigmoid(scaled)
        coefficient = -BETA * torch.sigmoid(-scaled) / MICROBATCHES
    # Recompute each answer separately so a pair of visual graphs is never resident together.
    (coefficient * spoken_logp(model, chosen)).backward()
    (-coefficient * spoken_logp(model, rejected)).backward()
    return float(loss), float(margin), float(reference_margin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "reference", "corpus", "data", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.corpus = args.corpus.resolve(strict=True)
    if args.base.resolve().name != REVISION or not torch.cuda.is_bf16_supported():
        raise SystemExit("pinned base processor and a BF16 GPU are required")
    dataset = json.loads(args.data.read_text(encoding="utf-8"))
    preferences, retention_rows = dataset["preferences"], dataset["retention"]
    if len(preferences) != 96 or len(retention_rows) != 32:
        raise ValueError("expected one pass over 96 preferences and 32 retained records")
    args.output.mkdir(parents=True, exist_ok=False)
    set_seed(42)
    rng = random.Random(42)
    rng.shuffle(preferences)
    rng.shuffle(retention_rows)
    processor = load_processor(args.base)
    for row in preferences:
        for side in ("chosen", "rejected"):
            scored_answer(processor, args.corpus, row, side)
    base = AutoModelForImageTextToText.from_pretrained(
        args.reference, dtype=torch.bfloat16, local_files_only=True,
        attn_implementation="sdpa", device_map={"": "cuda:0"},
    )
    base.config.use_cache = False
    targets = [
        name for name, module in base.named_modules()
        if name.startswith("model.language_model.") and isinstance(module, torch.nn.Linear)
        and name.rsplit(".", 1)[-1] in {"q_proj", "k_proj", "v_proj", "o_proj"}
    ]
    if not targets:
        raise ValueError("language attention targets are missing")
    model = get_peft_model(base, LoraConfig(r=8, lora_alpha=16, lora_dropout=0, target_modules=targets))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
    for module in model.modules():
        if isinstance(module, torch.nn.Dropout):
            module.p = 0.0
    parameters = [param for param in model.parameters() if param.requires_grad]
    optimizer = torch.optim.AdamW(parameters, lr=LEARNING_RATE, weight_decay=0)
    retention = Examples(retention_rows, processor, args.corpus, text_only=False, current_live_turn_only=True)
    with (args.output / "run.jsonl").open("w", encoding="utf-8") as log:
        for step in range(1, 33):
            optimizer.zero_grad(set_to_none=True)
            batch = preferences[(step - 1) * 3:step * 3]
            outcomes = [preference_backward(model, row, processor, args.corpus) for row in batch]
            retained = device_inputs(model, retention[step - 1])
            retained_loss = model(**retained, use_cache=False).loss
            (retained_loss / MICROBATCHES).backward()
            norm = torch.nn.utils.clip_grad_norm_(parameters, 1.0, error_if_nonfinite=True)
            optimizer.step()
            record = {
                "step": step, "preferences": [row["id"] for row in batch],
                "retention": retention_rows[step - 1]["id"], "pair_loss": [r[0] for r in outcomes],
                "current_margin": [r[1] for r in outcomes], "reference_margin": [r[2] for r in outcomes],
                "retention_loss": float(retained_loss.detach()), "gradient_norm": float(norm),
            }
            print(record, flush=True)
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            if step in CHECKPOINTS:
                model.save_pretrained(args.output / f"step-{step}")


if __name__ == "__main__":
    main()
