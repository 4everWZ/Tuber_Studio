"""Merge the language-only 9B DoRA checkpoint for the paired GGUF export."""

import argparse
import json
import re
from pathlib import Path

from train_dora import BASE_MODEL, vision_parameter


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base_model", choices=(BASE_MODEL,), default=BASE_MODEL)
    parser.add_argument("--revision", required=True, help="Cached 40-character base revision used for training")
    parser.add_argument("--adapter_path", type=Path, required=True, help="Selected DoRA checkpoint directory")
    parser.add_argument("--output_dir", type=Path, required=True, help="Fresh directory for the merged Hugging Face model")
    return parser.parse_args()


def validate_inputs(args):
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("The base revision must be a full Hugging Face commit")
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise FileExistsError("Choose a fresh directory for the merged checkpoint")
    adapter = json.loads((args.adapter_path / "adapter_config.json").read_text(encoding="utf-8"))
    if (
        adapter.get("base_model_name_or_path") != args.base_model
        or adapter.get("revision") != args.revision
        or adapter.get("use_dora") is not True
        or (adapter.get("r"), adapter.get("lora_alpha")) != (16, 32)
    ):
        raise ValueError("Adapter configuration does not match the pinned 9B DoRA baseline")
    targets = adapter.get("target_modules")
    if not isinstance(targets, list) or not targets or any(
        not isinstance(name, str) or not name or vision_parameter(name) for name in targets
    ):
        raise ValueError("The paired export requires the language-only adapter and an unchanged base projector")


def main():
    args = parse_args()
    validate_inputs(args)

    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer

    source = {"revision": args.revision, "local_files_only": True, "trust_remote_code": False}
    base = AutoModelForImageTextToText.from_pretrained(args.base_model, dtype=torch.bfloat16, **source)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, **source)
    model = PeftModel.from_pretrained(base, args.adapter_path, local_files_only=True).merge_and_unload()

    args.output_dir.mkdir(parents=True)
    model.save_pretrained(args.output_dir, max_shard_size="4GB")
    tokenizer.save_pretrained(args.output_dir)
    print("Merged checkpoint saved. Use the pinned llama.cpp commands in tools/training/README.md.")


if __name__ == "__main__":
    main()
