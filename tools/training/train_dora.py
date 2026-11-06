"""Legacy text-only DoRA SFT used in the original persona experiment.

Optimized for Workstation with 2x RTX 3090 (48GB VRAM total).
Supports both MiniCPM-V-4.7-1B and Gemma 4 E2B.
Freezes vision encoder and projector, fine-tuning only the language model layers.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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
    parser = argparse.ArgumentParser(description="Train anime persona using DoRA and SimPO on 2x 3090.")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default="openbmb/MiniCPM-V-4.7-1B",
        help="Path or HuggingFace repo ID of the base model",
    )
    parser.add_argument(
        "--data_dir",
        type=Path,
        default=Path("runtime/training_data"),
        help="Directory containing sft_train.json or simpo_train.json",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("runtime/checkpoints/dafeiyu_dora"),
        help="Directory to save checkpoints and final adapter",
    )
    parser.add_argument(
        "--stage",
        type=str,
        choices=["sft"],
        default="sft",
        help="This legacy trainer implements the SFT stage",
    )
    parser.add_argument(
        "--use_dora", action="store_true", default=True, help="Enable Weight-Decomposed LoRA (DoRA)"
    )
    parser.add_argument("--r", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha scaling factor")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout rate")
    parser.add_argument("--batch_size", type=int, default=4, help="Per-device train batch size")
    parser.add_argument(
        "--gradient_accumulation_steps", type=int, default=4, help="Gradient accumulation steps"
    )
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="Peak learning rate")
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs")
    parser.add_argument("--max_seq_length", type=int, default=1024, help="Maximum sequence length")
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        default=True,
        help="Enable gradient checkpointing to reduce VRAM",
    )
    parser.add_argument(
        "--bf16", action="store_true", default=True, help="Use BF16 mixed precision (Ampere native)"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    return parser.parse_args()


def freeze_vision_modules(model):
    """Freezes vision encoder and projector layers, keeping only LLM backbone trainable."""
    frozen_count = 0
    total_count = 0
    for name, param in model.named_parameters():
        total_count += 1
        if any(pattern in name.lower() for pattern in FREEZE_PATTERNS):
            param.requires_grad = False
            frozen_count += 1

    logger.info(
        "Vision freeze complete: %d / %d parameter tensors frozen to preserve visual and OCR capability.",
        frozen_count,
        total_count,
    )


def setup_dora_config(args):
    """Creates a PEFT LoraConfig with use_dora=True."""
    try:
        from peft import LoraConfig, TaskType
    except ImportError:
        logger.error(
            "peft is required. Please install it on the workstation: pip install peft trl transformers"
        )
        sys.exit(1)

    if "gemma" in args.model_name_or_path.lower():
        target_modules = r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
    else:
        target_modules = DEFAULT_TARGET_MODULES

    peft_config = LoraConfig(
        r=args.r,
        lora_alpha=args.lora_alpha,
        target_modules=target_modules,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
        use_dora=args.use_dora,
    )
    logger.info(
        "DoRA configuration created: rank=%d, alpha=%d, use_dora=%s, targets=%s",
        args.r,
        args.lora_alpha,
        args.use_dora,
        target_modules,
    )
    return peft_config


def main():
    args = parse_args()
    logger.info("Initializing Da Feiyu Persona Training (Stage: %s)...", args.stage)
    logger.info("Model: %s | Device count: %s", args.model_name_or_path, os.environ.get("WORLD_SIZE", "1"))

    # Check dependencies
    try:
        import torch
        from peft import get_peft_model
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            TrainingArguments,
        )
    except ImportError as e:
        logger.warning(
            "Training dependencies (torch, transformers, peft) not fully installed in current lightweight environment: %s\n"
            "This script is ready to run on the Workstation: python -m torch.distributed.run --nproc_per_node=2 tools/training/train_dora.py",
            e,
        )
        return

    logger.info("CUDA Available: %s (Device count: %d)", torch.cuda.is_available(), torch.cuda.device_count())
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            logger.info(
                "GPU %d: %s (%d GB VRAM)",
                i,
                torch.cuda.get_device_name(i),
                torch.cuda.get_device_properties(i).total_memory // (1024**3),
            )

    # Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Model
    torch_dtype = torch.bfloat16 if args.bf16 and torch.cuda.is_bf16_supported() else torch.float16
    logger.info("Loading base model %s with dtype=%s...", args.model_name_or_path, torch_dtype)

    try:
        from transformers import AutoModelForImageTextToText

        model = AutoModelForImageTextToText.from_pretrained(
            args.model_name_or_path,
            dtype=torch_dtype,
            trust_remote_code=True,
            device_map="auto" if not os.environ.get("WORLD_SIZE") else None,
        )
    except Exception as e:
        logger.info("AutoModelForImageTextToText failed (%s), falling back to AutoModelForCausalLM...", e)
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            dtype=torch_dtype,
            trust_remote_code=True,
            device_map="auto" if not os.environ.get("WORLD_SIZE") else None,
        )

    # Freeze vision and apply DoRA
    freeze_vision_modules(model)
    peft_config = setup_dora_config(args)
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    # Load datasets
    train_file = args.data_dir / "sft_train.json"
    val_file = args.data_dir / "sft_val.json"
    if not train_file.exists():
        logger.error("Dataset not found at %s. Please run prepare_persona_dataset.py first.", train_file)
        sys.exit(1)

    import json

    from datasets import Dataset

    with open(train_file, encoding="utf-8") as f:
        raw_train = json.load(f)
    with open(val_file, encoding="utf-8") as f:
        raw_val = json.load(f)

    def format_samples(batch):
        texts = []
        for msgs in batch["messages"]:
            formatted = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
            texts.append(formatted)
        return {"text": texts}

    train_ds = Dataset.from_list(raw_train)
    val_ds = Dataset.from_list(raw_val)
    train_ds = train_ds.map(format_samples, batched=True, remove_columns=["messages", "category"])
    val_ds = val_ds.map(format_samples, batched=True, remove_columns=["messages", "category"])

    def tokenize_function(examples):
        tokenized = tokenizer(examples["text"], truncation=True, max_length=args.max_seq_length)
        tokenized["labels"] = [ids.copy() for ids in tokenized["input_ids"]]
        return tokenized

    tokenized_train = train_ds.map(tokenize_function, batched=True, remove_columns=["text"])
    tokenized_val = val_ds.map(tokenize_function, batched=True, remove_columns=["text"])

    from transformers import DataCollatorForSeq2Seq, Trainer

    data_collator = DataCollatorForSeq2Seq(tokenizer=tokenizer, pad_to_multiple_of=8, return_tensors="pt")

    # Training arguments
    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        bf16=args.bf16,
        gradient_checkpointing=args.gradient_checkpointing,
        gradient_checkpointing_kwargs={"use_reentrant": False} if args.gradient_checkpointing else None,
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch",
        save_total_limit=2,
        seed=args.seed,
        report_to="none",
    )

    logger.info(
        "Workstation training configuration finalized (output_dir=%s). Launching Trainer...",
        training_args.output_dir,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_train,
        eval_dataset=tokenized_val,
        data_collator=data_collator,
    )

    train_result = trainer.train()
    logger.info("Training complete! Train loss: %.4f", train_result.training_loss)

    logger.info("Saving DoRA adapter weights to %s...", args.output_dir)
    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)

    # Save metrics
    metrics = {
        "train_loss": train_result.training_loss,
        "train_runtime_seconds": train_result.metrics.get("train_runtime", 0),
        "train_samples_per_second": train_result.metrics.get("train_samples_per_second", 0),
        "epochs": args.epochs,
        "model": args.model_name_or_path,
        "dora_rank": args.r,
        "dora_alpha": args.lora_alpha,
    }
    with open(args.output_dir / "training_metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    logger.info("DoRA training pipeline finished successfully!")


if __name__ == "__main__":
    main()
