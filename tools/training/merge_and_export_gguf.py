"""Merge a language-model DoRA adapter into its original Hugging Face checkpoint.

For new vision runs use export_grounded_sft.py to produce a paired GGUF.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base_model",
        type=str,
        default="Qwen/Qwen3.5-9B",
        help="Base model path or HF repo",
    )
    parser.add_argument(
        "--adapter_path",
        type=Path,
        default=Path("output_dafeiyu_qwen35_dora"),
        help="Path to trained DoRA/QLoRA adapter",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("runtime/merged_models/dafeiyu_qwen35_merged"),
        help="Directory to save merged HuggingFace model",
    )
    parser.add_argument(
        "--merge_method",
        choices=["linear"],
        default="linear",
        help="Perform a standard PEFT merge",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logger.info("Starting standard PEFT model merge...")
    logger.info("Base Model: %s", args.base_model)
    logger.info("Adapter Path: %s", args.adapter_path)

    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        logger.warning(
            "PyTorch/PEFT not available in current environment: %s\n"
            "This script is ready to run on the Workstation: python tools/training/merge_and_export_gguf.py",
            e,
        )
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Loading base model...")
    try:
        from transformers import AutoModelForImageTextToText

        base_model = AutoModelForImageTextToText.from_pretrained(
            args.base_model,
            dtype=torch.bfloat16,
            trust_remote_code=True,
            device_map="cpu",
        )
    except Exception as e:
        logger.info("AutoModelForImageTextToText failed (%s), falling back to AutoModelForCausalLM...", e)
        base_model = AutoModelForCausalLM.from_pretrained(
            args.base_model,
            dtype=torch.bfloat16,
            trust_remote_code=True,
            device_map="cpu",
        )
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)

    model = PeftModel.from_pretrained(base_model, str(args.adapter_path))
    merged_model = model.merge_and_unload()

    logger.info("Saving merged weights to %s...", args.output_dir)
    merged_model.save_pretrained(args.output_dir, max_shard_size="4GB")
    tokenizer.save_pretrained(args.output_dir)

    print("\n" + "=" * 60)
    print("SUCCESS: Merged model saved to:", args.output_dir)
    print("=" * 60)
    print("\nNext step: Convert to GGUF Q4_K_M for Jetson Orin Nano deployment:")
    print("1. Convert to GGUF:")
    print(
        f"   python third_party/llama.cpp/convert_hf_to_gguf.py {args.output_dir} --outfile {args.output_dir}/model-f16.gguf"
    )
    print("2. Quantize to Q4_K_M:")
    print(f"   ./llama-quantize {args.output_dir}/model-f16.gguf {args.output_dir}/model-Q4_K_M.gguf Q4_K_M")
    print("=" * 60)


if __name__ == "__main__":
    main()
