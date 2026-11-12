"""Train a language-only OC adapter with verified native assistant supervision."""

from __future__ import annotations

import argparse
import logging
import os
import re
from importlib.metadata import version
from pathlib import Path

from sft_dataset import digest, json_bytes, read_split_files
from sft_tokenization import balance_training, token_counts, tokenize_split

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
BASE_MODEL = "Qwen/Qwen3.5-9B"

# Target projection layers for standard attention and MLP backbones
DEFAULT_TARGET_MODULES = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
]

# Modules to explicitly freeze (Vision Encoders & Projectors)
FREEZE_PATTERNS = [
    "vision_tower",
    "merger",
    "vision_model",
    "vpm",
    "resampler",
    "projector",
    "mm_projector",
    "visual",
    "siglip",
]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default=BASE_MODEL,
        choices=(BASE_MODEL,),
    )
    parser.add_argument(
        "--data_dir",
        type=Path,
        required=True,
        help="Directory containing compiled train/dev splits and their grouped manifest",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        required=True,
        help="Fresh directory for checkpoints or the preparation report",
    )
    parser.add_argument("--r", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha scaling factor")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout rate")
    parser.add_argument("--batch_size", type=int, default=1, help="Per-device train batch size")
    parser.add_argument(
        "--gradient_accumulation_steps", type=int, default=4, help="Gradient accumulation steps"
    )
    parser.add_argument("--learning_rate", type=float, default=1.5e-4, help="Peak learning rate")
    parser.add_argument("--epochs", type=int, default=2, help="Number of training epochs")
    parser.add_argument("--max_seq_length", type=int, default=1024, help="Maximum sequence length")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--revision", required=True, help="Pin the Hugging Face base revision")
    parser.add_argument("--local_files_only", action="store_true")
    parser.add_argument("--holdout", type=Path, required=True, help="Frozen evaluation JSONL kept out of training")
    parser.add_argument("--prepare_only", action="store_true", help="Verify exact training inputs before loading weights")
    return parser.parse_args()


def vision_parameter(name):
    return any(part in FREEZE_PATTERNS for part in name.lower().split("."))


def setup_peft_config(args, model):
    from peft import LoraConfig, TaskType

    suffixes = set(DEFAULT_TARGET_MODULES)
    if model.config.model_type.startswith("qwen"):
        suffixes.update(("in_proj_qkv", "out_proj"))
    targets = [
        name for name, _ in model.named_modules()
        if name.rsplit(".", 1)[-1] in suffixes and not vision_parameter(name)
    ]
    if not targets:
        raise ValueError("No language-model adapter targets matched")
    return LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=targets,
        use_dora=True,
        bias="none",
    )


def validate_run(args):
    if args.output_dir.exists():
        raise FileExistsError("Training output directory already exists; choose a new one")
    if min(args.batch_size, args.max_seq_length, args.gradient_accumulation_steps, args.epochs) < 1:
        raise ValueError("Batch size, gradient accumulation, length and epochs must be positive")
    if (
        args.model_name_or_path != BASE_MODEL
        or not re.fullmatch(r"[0-9a-f]{40}", args.revision or "")
        or (args.r, args.lora_alpha) != (16, 32)
        or args.epochs > 2
    ):
        raise ValueError("Use pinned Qwen3.5-9B, rank 16/alpha 32 DoRA, and at most two epochs")


def prepare_training(args, tokenizer, end_tokens=()):
    raw_train, raw_dev, split_fingerprints = read_split_files(args.data_dir)
    train = tokenize_split(raw_train, tokenizer, args.max_seq_length, end_tokens=end_tokens)
    dev = tokenize_split(raw_dev, tokenizer, args.max_seq_length, end_tokens=end_tokens)
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    global_batch = world_size * args.batch_size
    selected = balance_training(train, seed=args.seed, global_batch=global_batch)
    tokens = token_counts(selected)
    source_directory = Path(__file__).resolve().parent
    metadata = {
        "schema": "dafeiyu-adapter-training-v1",
        "status": "prepared",
        "model": args.model_name_or_path,
        "model_revision": args.revision,
        "template_sha256": digest(tokenizer.chat_template.encode("utf-8")),
        "split_sha256": split_fingerprints,
        "holdout_sha256": digest(args.holdout.read_bytes()),
        "tokenizer_class": type(tokenizer).__name__,
        "assistant_loss": "native-content-and-eos-v1",
        "allowed_stop_token_ids": sorted({tokenizer.eos_token_id, *end_tokens}),
        "visual_training": "text-only; multimodal rows rejected",
        "seed": args.seed,
        "dora_rank": args.r,
        "dora_alpha": args.lora_alpha,
        "dora_dropout": args.lora_dropout,
        "load_in_4bit": False,
        "epochs": args.epochs,
        "learning_rate": args.learning_rate,
        "max_seq_length": args.max_seq_length,
        "global_batch": global_batch,
        "gradient_accumulation": args.gradient_accumulation_steps,
        "train_rows_before_replay": len(train),
        "train_rows_per_epoch": len(selected),
        "dev_rows": len(dev),
        "original_train_supervised_tokens": token_counts(train),
        "train_supervised_tokens_per_epoch": tokens,
        "dev_supervised_tokens": token_counts(dev),
        "general_token_share": tokens.get("general_ability", 0) / sum(tokens.values()),
        "selection_sha256": digest(json_bytes([inputs for _, inputs, _ in selected])),
        "training_code_sha256": {
            name: digest((source_directory / name).read_bytes())
            for name in ("train_dora.py", "sft_dataset.py", "sft_tokenization.py")
        },
        "library_versions": {name: version(name) for name in ("torch", "transformers", "peft", "datasets", "accelerate")},
    }
    return [row[1] for row in selected], [row[1] for row in dev], metadata


def main():
    args = parse_args()
    validate_run(args)
    import torch
    from datasets import Dataset
    from peft import get_peft_model
    from transformers import (
        AutoConfig,
        AutoModelForImageTextToText,
        AutoTokenizer,
        DataCollatorForSeq2Seq,
        GenerationConfig,
        Trainer,
        TrainingArguments,
        set_seed,
    )

    set_seed(args.seed)
    source = {
        "revision": args.revision,
        "local_files_only": args.local_files_only,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, **source)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    config = AutoConfig.from_pretrained(args.model_name_or_path, **source)
    try:
        generation = GenerationConfig.from_pretrained(args.model_name_or_path, **source)
    except OSError:
        generation = GenerationConfig.from_model_config(config)
    end_tokens = generation.eos_token_id
    if isinstance(end_tokens, int):
        end_tokens = (end_tokens,)
    train_rows, dev_rows, metadata = prepare_training(args, tokenizer, end_tokens or ())
    logger.info("Prepared SFT metrics %s", metadata)
    if args.prepare_only:
        args.output_dir.mkdir(parents=True)
        (args.output_dir / "training_metrics.json").write_bytes(json_bytes(metadata))
        return

    model = AutoModelForImageTextToText.from_pretrained(
        args.model_name_or_path,
        dtype=torch.bfloat16,
        **source,
    )
    model.config.use_cache = False
    model = get_peft_model(model, setup_peft_config(args, model), revision=args.revision)
    if any(param.requires_grad and vision_parameter(name) for name, param in model.named_parameters()):
        raise ValueError("Visual parameters are unexpectedly trainable")
    model.print_trainable_parameters()

    collator = DataCollatorForSeq2Seq(tokenizer=tokenizer, pad_to_multiple_of=8, return_tensors="pt")
    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=1,
        eval_accumulation_steps=1,
        dataloader_drop_last=False,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch",
        ddp_find_unused_parameters=False,
        seed=args.seed,
        data_seed=args.seed,
        report_to="none",
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=Dataset.from_list(train_rows),
        eval_dataset=Dataset.from_list(dev_rows),
        data_collator=collator,
        processing_class=tokenizer,
    )
    metadata_path = args.output_dir / "training_metrics.json"
    if trainer.is_world_process_zero():
        args.output_dir.mkdir(parents=True, exist_ok=True)
        metadata_path.write_bytes(json_bytes(metadata))
    train_result = trainer.train()
    trainer.save_model(args.output_dir)
    if trainer.is_world_process_zero():
        metadata.update(
            status="complete",
            train_loss=train_result.training_loss,
            train_runtime_seconds=train_result.metrics.get("train_runtime"),
            train_samples_per_second=train_result.metrics.get("train_samples_per_second"),
            global_steps=trainer.state.global_step,
            checkpoints=sorted(path.name for path in args.output_dir.glob("checkpoint-*")),
            evaluations=[row for row in trainer.state.log_history if "eval_loss" in row],
        )
        metadata_path.write_bytes(json_bytes(metadata))
    logger.info("Persona fine-tuning finished")


if __name__ == "__main__":
    main()
