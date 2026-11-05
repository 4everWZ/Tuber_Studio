"""Export a selected MiniCPM language adapter with its 4x vision projector."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from grounded_sft import REVISION, load_processor

ENGINE = "0ec4027a2a0183de87c007069adddc06e665f7de"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--reference-model", type=Path, help="merged BF16 reference used to train this delta adapter")
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--adapter-scale", type=float, default=1)
    parser.add_argument("--llama-cpp", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from peft import PeftModel
    from peft.tuners.lora import LoraLayer
    from transformers import AutoModelForImageTextToText

    if args.base.resolve().name != REVISION or not 0 < args.adapter_scale <= 1:
        raise SystemExit("pinned BF16 base and a positive adapter scale <= 1 are required")
    engine = subprocess.check_output(
        ["git", "-C", str(args.llama_cpp), "rev-parse", "HEAD"], text=True
    ).strip()
    if engine != ENGINE:
        raise SystemExit(f"expected llama.cpp {ENGINE}")
    args.output.mkdir(parents=True, exist_ok=False)
    merged = args.output / "merged"
    processor = load_processor(args.base)
    model = AutoModelForImageTextToText.from_pretrained(
        args.reference_model or args.base, dtype=torch.bfloat16, local_files_only=True,
    )
    model = PeftModel.from_pretrained(model, str(args.adapter))
    for module in model.modules():
        if isinstance(module, LoraLayer):
            module.set_scale("default", args.adapter_scale)
    model = model.merge_and_unload(safe_merge=True)
    model.config.downsample_mode = "4x"
    model.config.use_cache = True
    model.save_pretrained(merged)
    processor.save_pretrained(merged)
    preprocessor = json.loads((args.base / "preprocessor_config.json").read_text(encoding="utf-8"))
    (merged / "preprocessor_config.json").write_text(
        json.dumps(preprocessor | {"downsample_mode": "4x"}, indent=2), encoding="utf-8"
    )
    converter = [sys.executable, str(args.llama_cpp / "convert_hf_to_gguf.py"), str(merged)]
    fp16, projector, model_file = (
        args.output / "model-f16.gguf", args.output / "mmproj-4x-f16.gguf",
        args.output / "MiniCPM-V-4.7-1B-Dafeiyu-Grounded-Q4_K_M.gguf",
    )
    subprocess.run([*converter, "--no-mtp", "--outtype", "f16", "--outfile", str(fp16)], check=True)
    subprocess.run([*converter, "--mmproj", "--outtype", "f16", "--outfile", str(projector)], check=True)
    subprocess.run(
        [str(args.llama_cpp / "build/bin/llama-quantize"), str(fp16), str(model_file), "Q4_K_M"],
        check=True,
    )
    files = {}
    for artifact in (model_file, projector):
        with artifact.open("rb") as stream:
            files[artifact.name] = {"bytes": artifact.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    (args.output / "bundle.json").write_text(json.dumps({
        "model_alias": "MiniCPM-V-4.7-1B-Dafeiyu-Grounded", "base_revision": REVISION, "engine_revision": engine,
        "adapter": str(args.adapter), "adapter_scale": args.adapter_scale, "artifacts": files,
        **({"reference_model": str(args.reference_model)} if args.reference_model else {}),
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
