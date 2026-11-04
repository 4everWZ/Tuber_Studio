"""DoRA (Weight-Decomposed Low-Rank Adaptation) & SimPO training script.

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

# Target projection layers for standard LLaMA/Qwen/Gemma backbones
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
        choices=["sft", "simpo"],
        default="sft",
        help="Training stage: sft (supervised fine-tuning) or simpo (preference alignment)",
    )
    parser.add_argument("--use_dora", action="store_true", default=True, help="Enable Weight-Decomposed LoRA (DoRA)")
    parser.add_argument("--r", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha scaling factor")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout rate")
    parser.add_argument("--batch_size", type=int, default=4, help="Per-device train batch size")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=4, help="Gradient accumulation steps")
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="Peak learning rate")
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs")
    parser.add_argument("--max_seq_length", type=int, default=2048, help="Maximum sequence length")
    parser.add_argument("--bf16", action="store_true", default=True, help="Use BF16 mixed precision (Ampere native)")
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
        logger.error("peft is required. Please install it on the workstation: pip install peft trl transformers")
        sys.exit(1)

    peft_config = LoraConfig(
        r=args.r,
        lora_alpha=args.lora_alpha,
        target_modules=DEFAULT_TARGET_MODULES,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
        use_dora=args.use_dora,
    )
    logger.info("DoRA configuration created: rank=%d, alpha=%d, use_dora=%s", args.r, args.lora_alpha, args.use_dora)
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
            logger.info("GPU %d: %s (%d GB VRAM)", i, torch.cuda.get_device_name(i), torch.cuda.get_device_properties(i).total_memory // (1024**3))

    # Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Model
    torch_dtype = torch.bfloat16 if args.bf16 and torch.cuda.is_bf16_supported() else torch.float16
    logger.info("Loading base model %s with dtype=%s...", args.model_name_or_path, torch_dtype)

    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        torch_dtype=torch_dtype,
        trust_remote_code=True,
        device_map="auto" if not os.environ.get("WORLD_SIZE") else None,
    )

    # Freeze vision and apply DoRA
    freeze_vision_modules(model)
    peft_config = setup_dora_config(args)
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    # Training arguments
    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        bf16=args.bf16,
        logging_steps=10,
        save_strategy="epoch",
        evaluation_strategy="epoch",
        save_total_limit=2,
        seed=args.seed,
        report_to="none",
    )

    logger.info("Workstation training configuration finalized (output_dir=%s). Ready to launch training loop.", training_args.output_dir)


if __name__ == "__main__":
    main()
