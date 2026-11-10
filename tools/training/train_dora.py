"""Da Feiyu Persona Fine-Tuning Pipeline (DoRA / 4-bit QLoRA) on Workstation.

Optimized for 2x RTX 3090 (48GB VRAM total).
Supports:
- Qwen/Qwen3.5-9B (Dense 9B, Native 256K context, Hybrid Linear Attention)
- google/gemma-4-12B (Gemma 4 12B Multimodal)
- google/gemma-4-26B-A4B-it (Gemma 4 26B MoE with 4B active params, Native 256K context)
- Qwen/Qwen3.8-Flash-Next (Qwen3.8 MoE with 512 fine-grained experts, Native 256K context)
- Qwen/Qwen3.8-27B (Dense 27B, Native 256K context)

Freezes vision encoder and projector if present, fine-tuning only the language model layers.
"""

from __future__ import annotations

import argparse
import json
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
    parser = argparse.ArgumentParser(description="Train anime persona using DoRA/QLoRA on 2x 3090.")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default="Qwen/Qwen3.5-9B",
        help="Path or HuggingFace repo ID of the base model",
    )
    parser.add_argument(
        "--data_dir",
        type=Path,
        default=Path("data"),
        help="Directory containing sft_train.json and sft_val.json",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("output_dafeiyu_dora"),
        help="Directory to save checkpoints and final adapter",
    )
    parser.add_argument(
        "--stage",
        type=str,
        choices=["sft"],
        default="sft",
        help="Training stage",
    )
    parser.add_argument(
        "--use_dora",
        action="store_true",
        default=True,
        help="Enable Weight-Decomposed LoRA (DoRA) for full precision mode",
    )
    parser.add_argument(
        "--load_in_4bit",
        action="store_true",
        default=False,
        help="Enable 4-bit NF4 QLoRA (recommended for 26B+ MoE models)",
    )
    parser.add_argument("--r", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha scaling factor")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout rate")
    parser.add_argument("--batch_size", type=int, default=1, help="Per-device train batch size")
    parser.add_argument(
        "--gradient_accumulation_steps", type=int, default=4, help="Gradient accumulation steps"
    )
    parser.add_argument("--learning_rate", type=float, default=1.5e-4, help="Peak learning rate")
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs")
    parser.add_argument("--max_seq_length", type=int, default=512, help="Maximum sequence length")
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

    if frozen_count > 0:
        logger.info(
            "Vision freeze complete: %d / %d parameter tensors frozen to preserve visual/OCR capabilities.",
            frozen_count,
            total_count,
        )


def setup_peft_config(args):
    """Creates a PEFT LoraConfig with DoRA (or QLoRA when load_in_4bit is active)."""
    try:
        from peft import LoraConfig, TaskType
    except ImportError:
        logger.error(
            "peft is required. Please install it on the workstation: pip install peft trl transformers bitsandbytes"
        )
        sys.exit(1)

    model_lower = args.model_name_or_path.lower()
    if "qwen" in model_lower:
        target_modules = [
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
            "in_proj_qkv",
            "out_proj",
        ]
    elif "gemma" in model_lower:
        target_modules = [
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ]
    else:
        target_modules = DEFAULT_TARGET_MODULES

    use_dora = args.use_dora and (not args.load_in_4bit)

    peft_config = LoraConfig(
        r=args.r,
        lora_alpha=args.lora_alpha,
        target_modules=target_modules,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
        use_dora=use_dora,
    )
    logger.info(
        "PEFT configuration created: rank=%d, alpha=%d, use_dora=%s, 4bit=%s, targets=%s",
        args.r,
        args.lora_alpha,
        use_dora,
        args.load_in_4bit,
        target_modules,
    )
    return peft_config


def main():
    args = parse_args()
    logger.info("Initializing Da Feiyu Persona Training (Stage: %s)...", args.stage)
    logger.info("Model: %s | Device count: %s", args.model_name_or_path, os.environ.get("WORLD_SIZE", "1"))

    try:
        import torch
        from peft import get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            DataCollatorForSeq2Seq,
            Trainer,
            TrainingArguments,
        )
    except ImportError as e:
        logger.error("Required library missing: %s", e)
        sys.exit(1)

    logger.info("CUDA Available: %s (Device count: %d)", torch.cuda.is_available(), torch.cuda.device_count())
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            logger.info(
                "GPU %d: %s (%d GB VRAM)",
                i,
                torch.cuda.get_device_name(i),
                torch.cuda.get_device_properties(i).total_memory // (1024**3),
            )

    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if not tokenizer.chat_template:
        model_lower = args.model_name_or_path.lower()
        if "gemma" in model_lower:
            tokenizer.chat_template = (
                "{{ bos_token }}{% for message in messages %}"
                "{% if message['role'] == 'user' %}{{ '<start_of_turn>user\n' + message['content'] + '<end_of_turn>\n' }}"
                "{% elif message['role'] == 'assistant' %}{{ '<start_of_turn>model\n' + message['content'] + '<end_of_turn>\n' }}"
                "{% endif %}{% endfor %}{% if add_generation_prompt %}{{ '<start_of_turn>model\n' }}{% endif %}"
            )
            logger.info("Injected standard Gemma chat template into base tokenizer.")
        else:
            tokenizer.chat_template = (
                "{% for message in messages %}"
                "{{ '<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>\n' }}"
                "{% endfor %}{% if add_generation_prompt %}{{ '<|im_start|>assistant\n' }}{% endif %}"
            )
            logger.info("Injected standard ChatML chat template into base tokenizer.")

    torch_dtype = torch.bfloat16 if args.bf16 and torch.cuda.is_bf16_supported() else torch.float16
    quant_config = None
    if args.load_in_4bit:
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch_dtype,
        )
        logger.info("4-bit NF4 Quantization enabled for base model.")

    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    device_map = {"": local_rank} if args.load_in_4bit else None

    logger.info("Loading base model %s (dtype=%s, device_map=%s)...", args.model_name_or_path, torch_dtype, device_map)
    is_vision = any(k in args.model_name_or_path.lower() for k in ["cpm-v", "vl", "vision", "qwen-vl"])
    if is_vision:
        try:
            from transformers import AutoModelForImageTextToText

            model = AutoModelForImageTextToText.from_pretrained(
                args.model_name_or_path,
                torch_dtype=torch_dtype if not args.load_in_4bit else None,
                quantization_config=quant_config,
                trust_remote_code=True,
                device_map=device_map,
            )
        except Exception:
            model = AutoModelForCausalLM.from_pretrained(
                args.model_name_or_path,
                torch_dtype=torch_dtype if not args.load_in_4bit else None,
                quantization_config=quant_config,
                trust_remote_code=True,
                device_map=device_map,
            )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            torch_dtype=torch_dtype if not args.load_in_4bit else None,
            quantization_config=quant_config,
            trust_remote_code=True,
            device_map=device_map,
        )

    freeze_vision_modules(model)

    if args.load_in_4bit:
        model = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=args.gradient_checkpointing
        )

    peft_config = setup_peft_config(args)
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    # Load datasets
    train_file = args.data_dir / "sft_train.json"
    val_file = args.data_dir / "sft_val.json"
    if not train_file.exists():
        logger.error("Dataset not found at %s.", train_file)
        sys.exit(1)

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

    header_candidates = [
        "<|im_start|>assistant\n<think>\n\n</think>\n\n",
        "<|im_start|>assistant\n",
        "<start_of_turn>model\n",
        "<|start_header_id|>assistant<|end_header_id|>\n\n",
    ]
    encoded_headers = [tokenizer.encode(h, add_special_tokens=False) for h in header_candidates]
    encoded_headers = [h for h in encoded_headers if len(h) > 0]

    end_token_candidates = ["<|im_end|>", "<end_of_turn>", "<|eot_id|>", "</s>", tokenizer.eos_token]
    end_ids = set()
    for cand in end_token_candidates:
        if cand:
            try:
                cid = tokenizer.convert_tokens_to_ids(cand)
                if cid is not None and cid != tokenizer.unk_token_id:
                    end_ids.add(cid)
            except Exception:
                pass

    def tokenize_function(examples):
        tokenized = tokenizer(examples["text"], truncation=True, max_length=args.max_seq_length)
        all_labels = []
        for ids in tokenized["input_ids"]:
            labels = [-100] * len(ids)
            idx = 0
            found_any = False
            while idx < len(ids):
                match_len = 0
                for h in encoded_headers:
                    if idx + len(h) <= len(ids) and ids[idx : idx + len(h)] == h:
                        match_len = len(h)
                        break

                if match_len > 0:
                    start = idx + match_len
                    stop = len(ids)
                    for j in range(start, len(ids)):
                        if ids[j] in end_ids:
                            stop = j
                            break
                    for k in range(start, min(stop + 1, len(ids))):
                        labels[k] = ids[k]
                    found_any = True
                    idx = stop + 1
                else:
                    idx += 1
            if not found_any:
                labels = ids.copy()
            all_labels.append(labels)
        tokenized["labels"] = all_labels
        return tokenized

    tokenized_train = train_ds.map(tokenize_function, batched=True, remove_columns=["text"])
    tokenized_val = val_ds.map(tokenize_function, batched=True, remove_columns=["text"])

    data_collator = DataCollatorForSeq2Seq(tokenizer=tokenizer, pad_to_multiple_of=8, return_tensors="pt")

    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=1,
        eval_accumulation_steps=1,
        dataloader_drop_last=True,
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
        ddp_find_unused_parameters=False,
        seed=args.seed,
        report_to="none",
    )

    logger.info("Launching Trainer (output_dir=%s)...", training_args.output_dir)
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_train,
        eval_dataset=tokenized_val,
        data_collator=data_collator,
    )

    train_result = trainer.train()
    logger.info("Training complete! Train loss: %.4f", train_result.training_loss)

    logger.info("Saving adapter weights to %s...", args.output_dir)
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)

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

    logger.info("Persona fine-tuning pipeline finished successfully!")


if __name__ == "__main__":
    main()
