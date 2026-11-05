"""Assistant-only SFT for the fixed MiniCPM-V 4.7 checkpoint."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from importlib.metadata import version
from pathlib import Path

MODEL = "openbmb/MiniCPM-V-4.7-1B"
REVISION = "ad9bf379feb71204c888b22923d63322452407f9"
MAX_TOKENS = 4096
EXPECTED = {
    "train": {"oc": 240, "video": 240, "operations": 60, "general": 60},
    "validation": {"oc": 24, "video": 24, "operations": 6, "general": 6},
}
FOCUSED_EXPECTED = {
    split: EXPECTED[split] | {"video": EXPECTED[split]["video"] + extra}
    for split, extra in {"train": 160, "validation": 16}.items()
}
PERSONA_EXPECTED = {
    split: FOCUSED_EXPECTED[split] | {
        "oc": FOCUSED_EXPECTED[split]["oc"] + oc,
        "video": FOCUSED_EXPECTED[split]["video"] + video,
    }
    for split, (oc, video) in {"train": (72, 32), "validation": (12, 8)}.items()
}
ANIME_EXPECTED = {
    split: PERSONA_EXPECTED[split] | {"oc": PERSONA_EXPECTED[split]["oc"] + extra}
    for split, extra in {"train": 72, "validation": 24}.items()
}
DEEPTHINK_EXPECTED = {
    split: ANIME_EXPECTED[split] | {"oc": ANIME_EXPECTED[split]["oc"] + 3 * scenes}
    for split, scenes in {"train": 24, "validation": 6}.items()
}
PAIRED_EXPECTED = {
    split: DEEPTHINK_EXPECTED[split] | {
        "oc": DEEPTHINK_EXPECTED[split]["oc"] + 4 * pairs + 3 * anime,
        "video": FOCUSED_EXPECTED[split]["video"],
    }
    for split, (pairs, anime) in {"train": (12, 14), "validation": (4, 0)}.items()
}
PAIRED_CURATED_EXPECTED = PAIRED_EXPECTED | {
    "train": PAIRED_EXPECTED["train"] | {"oc": PAIRED_EXPECTED["train"]["oc"] - FOCUSED_EXPECTED["train"]["oc"]},
}
PAIRED_TEACHER_EXPECTED = {
    split: PAIRED_CURATED_EXPECTED[split] | {"oc": PAIRED_CURATED_EXPECTED[split]["oc"] + added}
    for split, added in {"train": 24, "validation": 2}.items()
}
CURRICULA = {
    "original": EXPECTED, "mixed-evidence": FOCUSED_EXPECTED, "roleplay": PERSONA_EXPECTED,
    "roleplay-anime": ANIME_EXPECTED, "roleplay-deepthink": DEEPTHINK_EXPECTED,
    "roleplay-paired": PAIRED_EXPECTED, "roleplay-paired-curated": PAIRED_CURATED_EXPECTED,
    "roleplay-paired-teacher": PAIRED_TEACHER_EXPECTED,
}
BOUNDARIES = ("<|im_start|>", "<|im_end|>", "<think>", "</think>", "<image>", "</image>")


def read_jsonl(path):
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def read_corpus(directory, *, pilot, expected=EXPECTED):
    """Validate identity, review state and split provenance before training."""
    splits = {split: read_jsonl(directory / f"{split}.jsonl") for split in expected}
    groups, ids, dialogues = {}, set(), set()
    for split, rows in splits.items():
        counts = Counter(row["category"] for row in rows)
        if not rows or (not pilot and counts != expected[split]):
            raise ValueError(f"{split}: received {dict(counts)}, expected {expected[split]}")
        for row in rows:
            identity = row["id"]
            if identity in ids:
                raise ValueError(f"duplicate sample ID: {identity}")
            ids.add(identity)
            if row.get("review") not in {"human", "verified_source", "model_reviewed"} or not row.get("sources"):
                raise ValueError(f"{identity}: approval and provenance are required")
            if row["review"] == "model_reviewed" and (
                row["category"] != "oc" or row.get("review_score") not in {2, 3} or not row.get("reviewer")
            ):
                raise ValueError(f"{identity}: invalid OC review record")
            for source in row["sources"]:
                for key in ("group", "uri", "license"):
                    if not isinstance(source.get(key), str) or not source[key].strip():
                        raise ValueError(f"{identity}: invalid provenance field {key}")
                if groups.setdefault(source["group"], split) != split:
                    raise ValueError(f"{identity}: source group crosses splits: {source['group']}")
            for dialogue in ("messages", "text_messages"):
                if dialogue not in row:
                    continue
                messages = row[dialogue]
                if messages[0]["role"] != "system" or messages[-1]["role"] != "assistant":
                    raise ValueError(f"{identity}: expected a system prefix and an assistant target")
                if any(message["role"] not in {"user", "assistant"} for message in messages[1:]):
                    raise ValueError(f"{identity}: unexpected dialogue role")
                for message in messages:
                    content = message["content"]
                    if not isinstance(content, str) or any(token in content for token in BOUNDARIES):
                        raise ValueError(f"{identity}: invalid message content")
            sample = json.dumps([row["messages"], row.get("frames", [])], ensure_ascii=False, sort_keys=True)
            if sample in dialogues:
                raise ValueError(f"{identity}: duplicate dialogue")
            dialogues.add(sample)
            if row.get("frames"):
                if row["category"] != "video" or "text_messages" not in row or len(row["frames"]) > 3:
                    raise ValueError(f"{identity}: invalid visual example or missing text control")
    return splits


def load_processor(path):
    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained(path, downsample_mode="4x", local_files_only=True)
    if processor.image_token_divisor != 4:
        raise ValueError("processor did not apply the requested 4x setting")
    return processor


def model_inputs(processor, root, row, *, text_only=False, generation=False):
    from PIL import Image

    use_frames = row.get("frames") and not text_only
    messages = [dict(message) for message in row.get("text_messages", row["messages"])] if text_only else [
        dict(message) for message in row["messages"]
    ]
    images = []
    if use_frames:
        if messages[-2]["role"] != "user":
            raise ValueError(f"{row['id']}: images must be bound to the final user message")
        content = []
        for index, frame in enumerate(row["frames"], 1):
            path = (root / frame["path"]).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError(f"{row['id']}: image path outside the corpus")
            with Image.open(path) as image:
                images.append(image.convert("RGB"))
            alias = f"[{frame['alias']}] " if frame.get("alias") else ""
            content.extend([
                {"type": "text", "text": f"\n{alias}画面 {index}｜{frame['seconds']} 秒\n"},
                {"type": "image"},
            ])
        content.append({"type": "text", "text": "\n" + messages[-2]["content"]})
        messages[-2]["content"] = content
    rendered = processor.apply_chat_template(
        messages[:-1] if generation else messages,
        tokenize=False,
        add_generation_prompt=generation,
        enable_thinking=False,
    )
    return dict(processor(text=rendered, images=images or None, add_special_tokens=False, return_tensors="pt"))


def assistant_spans(tokenizer, ids):
    """Return answer spans including each turn's im_end, excluding its header."""
    header = tokenizer.encode("<|im_start|>assistant\n<think>\n\n</think>\n\n", add_special_tokens=False)
    start_token = tokenizer.convert_tokens_to_ids("<|im_start|>")
    end_token = tokenizer.convert_tokens_to_ids("<|im_end|>")
    spans = []
    for index, token in enumerate(ids):
        if token != start_token or ids[index : index + len(header)] != header:
            continue
        start = index + len(header)
        stop = ids.index(end_token, start)
        if start == stop:
            raise ValueError("empty assistant answer")
        spans.append((start, stop + 1))
    return spans


class Examples:
    def __init__(self, rows, processor, root, *, text_only, current_live_turn_only=False):
        self.rows, self.processor, self.root = rows, processor, root
        self.text_only = text_only
        self.current_live_turn_only = current_live_turn_only

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        import torch

        row = self.rows[index]
        inputs = model_inputs(self.processor, self.root, row, text_only=self.text_only)
        tokens = inputs["input_ids"][0].tolist()
        if len(tokens) > MAX_TOKENS:
            raise ValueError(f"{row['id']}: {len(tokens)} tokens exceeds {MAX_TOKENS}")
        spans = assistant_spans(self.processor.tokenizer, tokens)
        expected = row.get("text_messages", row["messages"]) if self.text_only else row["messages"]
        if len(spans) != sum(message["role"] == "assistant" for message in expected):
            raise ValueError(f"{row['id']}: assistant mask disagrees with the native template")
        if self.current_live_turn_only and row.get("format") == "live_reply":
            spans = spans[-1:]
        labels = torch.full_like(inputs["input_ids"], -100)
        for start, stop in spans:
            labels[0, start:stop] = inputs["input_ids"][0, start:stop]
        return inputs | {"labels": labels}


def single_example(batch):
    if len(batch) != 1:
        raise ValueError("collator requires microbatch size 1")
    return batch[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help=f"local {MODEL} snapshot at {REVISION}")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strategy", choices=("text", "vision"), required=True)
    parser.add_argument("--curriculum", choices=CURRICULA, default="original")
    parser.add_argument("--current-live-turn-only", action="store_true", help="mask spoken history on marked live-reply rows")
    parser.add_argument("--roleplay-repeat", type=int, default=1, help="training-only repeat of authored roleplay rows")
    parser.add_argument("--init-adapter", type=Path, help="initialize a new run from adapter weights")
    parser.add_argument("--use-dora", action="store_true", help="initialize a DoRA adapter instead of LoRA")
    parser.add_argument("--learning-rate", type=float, default=5e-5)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--pilot", action="store_true", help="one update and validation, separate output")
    parser.add_argument("--pilot-steps", type=int, default=1)
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    if args.init_adapter and args.resume:
        parser.error("choose either adapter initialization or checkpoint resume")
    if args.init_adapter and args.use_dora:
        parser.error("adapter initialization already determines whether DoRA is used")
    if args.epochs < 1 or not 0 < args.learning_rate < float("inf"):
        parser.error("epochs and learning rate must be positive, and learning rate must be finite")
    if args.roleplay_repeat < 1 or (args.roleplay_repeat != 1 and args.curriculum != "roleplay"):
        parser.error("roleplay repetition requires the roleplay curriculum and a positive repeat count")

    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForImageTextToText, Trainer, TrainingArguments, set_seed

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise SystemExit("CUDA with BF16 support is required")
    if args.base.resolve().name != REVISION:
        raise SystemExit(f"expected the pinned Hugging Face snapshot {REVISION}")
    root = args.data.resolve(strict=True)
    splits = read_corpus(root, pilot=args.pilot, expected=CURRICULA[args.curriculum])
    if args.current_live_turn_only and not any(row.get("format") == "live_reply" for row in splits["train"]):
        parser.error("current live-turn masking requires a corpus with marked live replies")
    training_rows = splits["train"]
    if args.roleplay_repeat > 1:
        authored = [
            row for row in training_rows
            if row["id"].startswith("original-oc-") or row["id"].endswith(("-oc-reaction", "-oc-reaction-wire"))
        ]
        training_rows = training_rows + authored * (args.roleplay_repeat - 1)
    processor = load_processor(args.base)
    datasets = {
        split: Examples(
            rows, processor, root, text_only=args.strategy == "text",
            current_live_turn_only=args.current_live_turn_only,
        )
        for split, rows in splits.items()
    }
    for split, dataset in datasets.items():
        for index, row in enumerate(dataset.rows):
            if row.get("frames") or index == 0:
                sample = dataset[index]
                if args.strategy == "vision" and row.get("frames") and "pixel_values" not in sample:
                    raise ValueError(f"{row['id']}: no visual tensor")
        print(f"{split}: {len(dataset)}", flush=True)
    if args.roleplay_repeat > 1:
        datasets["train"] = Examples(
            training_rows, processor, root, text_only=args.strategy == "text",
            current_live_turn_only=args.current_live_turn_only,
        )

    set_seed(42)
    base = AutoModelForImageTextToText.from_pretrained(
        args.base, dtype=torch.bfloat16, local_files_only=True, attn_implementation="sdpa"
    )
    base.config.downsample_mode = "4x"
    base.config.use_cache = False
    targets = [
        name for name, module in base.named_modules()
        if name.startswith("model.language_model.")
        and isinstance(module, torch.nn.Linear)
        and name.rsplit(".", 1)[-1] in {"q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}
    ]
    if not targets:
        raise ValueError("no language projections matched the pinned checkpoint")
    model = (
        PeftModel.from_pretrained(base, str(args.init_adapter), is_trainable=True)
        if args.init_adapter
        else get_peft_model(base, LoraConfig(
            r=16, lora_alpha=32, lora_dropout=0.05, target_modules=targets, use_dora=args.use_dora,
        ))
    )
    if any(
        "model.language_model." not in name
        for name, parameter in model.named_parameters() if parameter.requires_grad
    ):
        raise ValueError("trainable parameters escaped the language backbone")
    model.print_trainable_parameters()
    options = TrainingArguments(
        output_dir=str(args.output),
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        max_steps=args.pilot_steps if args.pilot else -1,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=2 if args.pilot else 16,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        bf16=True,
        prediction_loss_only=True,
        remove_unused_columns=False,
        save_strategy="epoch",
        eval_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        save_total_limit=2,
        logging_steps=1,
        report_to=[],
        seed=42,
    )
    trainer = Trainer(
        model=model,
        args=options,
        train_dataset=datasets["train"],
        eval_dataset=datasets["validation"],
        data_collator=single_example,
    )
    result = trainer.train(resume_from_checkpoint=str(args.resume) if args.resume else None)
    trainer.save_model(str(args.output / "adapter"))
    processor.save_pretrained(args.output / "processor")
    (args.output / "run.json").write_text(json.dumps({
        "base": MODEL, "revision": REVISION, "strategy": args.strategy, "pilot": args.pilot,
        "curriculum": args.curriculum, "init_adapter": str(args.init_adapter) if args.init_adapter else None,
        "learning_rate": args.learning_rate, "epochs": args.epochs,
        "current_live_turn_only": args.current_live_turn_only,
        "adapter_method": "dora" if model.peft_config["default"].use_dora else "lora",
        "roleplay_repeat": args.roleplay_repeat, "train_examples_per_epoch": len(training_rows),
        "counts": {split: dict(Counter(row["category"] for row in rows)) for split, rows in splits.items()},
        "train": result.metrics, "checkpoints": trainer.state.log_history,
        "dependencies": {name: version(name) for name in ("torch", "transformers", "peft", "accelerate")},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
