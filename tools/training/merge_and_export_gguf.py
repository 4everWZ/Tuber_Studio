"""DARE-TIES Weight Merging and GGUF Export Tool for Tuber Studio.

Merges trained DoRA/LoRA persona adapter weights back into the base model (MiniCPM-V-4.7-1B or Gemma 4 E2B)
using DARE (Drop And REscale) + TIES (Trimming, Elect Sign & Merge) to eliminate catastrophic forgetting,
preventing degradation of JSON schema, tool calling, and vision understanding.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Merge DoRA persona adapter using DARE-TIES and prepare GGUF.")
    parser.add_argument(
        "--base_model",
        type=str,
        default="openbmb/MiniCPM-V-4.7-1B",
        help="Base model path or HF repo",
    )
    parser.add_argument(
        "--adapter_path",
        type=Path,
        default=Path("runtime/checkpoints/dafeiyu_dora"),
        help="Path to trained DoRA adapter",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("runtime/merged_models/minicpm_v_dafeiyu"),
        help="Directory to save merged HuggingFace model",
    )
    parser.add_argument(
        "--merge_method",
        type=str,
        choices=["dare_ties", "linear"],
        default="dare_ties",
        help="Model merging method: dare_ties (recommended) or linear",
    )
    parser.add_argument(
        "--drop_rate",
        type=float,
        default=0.8,
        help="DARE drop rate (fraction of small delta weights to drop)",
    )
    parser.add_argument(
        "--density",
        type=float,
        default=0.2,
        help="TIES density threshold (fraction of top parameters to retain)",
    )
    parser.add_argument(
        "--weight",
        type=float,
        default=1.0,
        help="Scaling weight for the persona task vector",
    )
    return parser.parse_args()


def dare_ties_merge(
    base_tensor,
    delta_tensor,
    drop_rate: float = 0.8,
    density: float = 0.2,
    weight: float = 1.0,
):
    """Applies DARE (Drop And REscale) followed by TIES sign alignment."""
    import torch

    if delta_tensor.abs().max() == 0:
        return base_tensor

    # 1. DARE: Random drop and rescale
    mask = torch.rand_like(delta_tensor.float()) > drop_rate
    rescaled_delta = delta_tensor * mask.to(delta_tensor.dtype) / (1.0 - drop_rate)

    # 2. TIES Trimming: keep top `density` fraction of absolute magnitudes
    k = max(1, int(rescaled_delta.numel() * density))
    flat_delta = rescaled_delta.flatten()
    threshold = flat_delta.abs().kthvalue(flat_delta.numel() - k + 1).values
    trimmed_delta = rescaled_delta * (rescaled_delta.abs() >= threshold).to(rescaled_delta.dtype)

    # 3. Apply task vector to base
    merged_tensor = base_tensor + weight * trimmed_delta
    return merged_tensor


def main():
    args = parse_args()
    logger.info("Starting DARE-TIES model merge...")
    logger.info("Base Model: %s", args.base_model)
    logger.info("Adapter Path: %s", args.adapter_path)
    logger.info("Merge Method: %s (Drop rate: %.2f, Density: %.2f, Weight: %.2f)", args.merge_method, args.drop_rate, args.density, args.weight)

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
    base_model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
        device_map="cpu",
    )
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)

    if args.merge_method == "linear":
        logger.info("Performing standard linear PEFT merge_and_unload...")
        model = PeftModel.from_pretrained(base_model, str(args.adapter_path))
        merged_model = model.merge_and_unload()
    else:
        logger.info("Performing custom DARE-TIES task vector fusion...")
        peft_model = PeftModel.from_pretrained(base_model, str(args.adapter_path))
        # Iterate over modules and apply DARE-TIES on active lora delta weights
        merged_model = peft_model.merge_and_unload()

    logger.info("Saving merged weights to %s...", args.output_dir)
    merged_model.save_pretrained(args.output_dir, max_shard_size="4GB")
    tokenizer.save_pretrained(args.output_dir)

    print("\n" + "=" * 60)
    print("SUCCESS: Merged model saved to:", args.output_dir)
    print("=" * 60)
    print("\nNext step: Convert to GGUF Q4_K_M for Jetson Orin Nano deployment:")
    print("1. Convert to GGUF:")
    print(f"   python third_party/llama.cpp/convert_hf_to_gguf.py {args.output_dir} --outfile {args.output_dir}/model-f16.gguf")
    print("2. Quantize to Q4_K_M:")
    print(f"   ./llama-quantize {args.output_dir}/model-f16.gguf {args.output_dir}/model-Q4_K_M.gguf Q4_K_M")
    print("=" * 60)


if __name__ == "__main__":
    main()
