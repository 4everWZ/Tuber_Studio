"""Offline Chinese/English-mixed CUDA candidate setup, in an assigned device window.

This performs no Torch/gsv_tts imports and no model loading. It never installs into
or modifies the base CUDA/studio environment. Other language/device extras remain
uninstalled until separately provisioned and reviewed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import platform
import subprocess
import sys
import venv
from pathlib import Path


def info(name):
    d = md.distribution(name)
    return {"version": d.version, "location": str(d.locate_file(""))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/tuber"))
    parser.add_argument("--bundle", type=Path, help="Defaults to ROOT/runtime/evaluations/tts-candidates")
    args = parser.parse_args()
    root = args.root.resolve()
    bundle = (args.bundle or root / "runtime/evaluations/tts-candidates").resolve()
    if platform.machine() not in {"aarch64", "arm64"} or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Requires the intended aarch64 Python3.12 environment")
    if not bundle.is_relative_to(root / "runtime/evaluations"):
        raise RuntimeError("Bundle must stay under this project runtime/evaluations")
    protected = {
        name: info(name) for name in ("torch", "torchaudio", "torchvision", "torchao", "transformers")
    }
    if not protected["torch"]["version"].startswith("2.7.") or not Path(
        protected["torch"]["location"]
    ).is_relative_to("/opt/venv"):
        raise RuntimeError("Original Torch2.7 at /opt/venv must be reused")
    manifest = json.loads((bundle / "wheels/manifest.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        path = bundle / "wheels" / record["filename"]
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not record["verified"] or digest != record["sha256"]:
            raise RuntimeError("Wheel integrity mismatch " + path.name)
    if len(manifest["files"]) != 6:
        raise RuntimeError("Expected six reviewed wheel artifacts")
    target = bundle / "venv"
    if target.is_symlink() or target.resolve() != target:
        raise RuntimeError("Candidate venv cannot redirect to an existing environment")
    if target.exists() and not (target / "pyvenv.cfg").exists():
        raise RuntimeError("Existing candidate directory is not a venv")
    if not target.exists():
        venv.EnvBuilder(with_pip=False, system_site_packages=False).create(target)
    site = target / "lib/python3.12/site-packages"
    if not site.resolve().is_relative_to(target):
        raise RuntimeError("Candidate site-packages cannot redirect outside its venv")
    executable = target / "bin/python"
    actual_prefix = subprocess.check_output(
        [str(executable), "-c", "import sys; print(sys.prefix)"], text=True
    ).strip()
    if Path(actual_prefix).resolve() != target:
        raise RuntimeError("Candidate interpreter is not scoped to the requested venv")
    base_sites = sorted(
        {record["location"] for record in protected.values()} | {str(md.distribution("pip").locate_file(""))}
    )
    (site / "tuber-original-runtime.pth").write_text("\n".join(base_sites) + "\n", encoding="utf-8")
    wanted = {"gsv-tts-lite", "av", "pysbd"}
    paths = [
        str(bundle / "wheels" / record["filename"])
        for record in manifest["files"]
        if record["distribution"] in wanted
    ]
    subprocess.run([str(executable), "-m", "pip", "install", "--no-index", "--no-deps", *paths], check=True)
    query = (
        'import importlib.metadata as m,json;print(json.dumps({n:{"version":m.version(n),"location":str(m.distribution(n).locate_file(""))} for n in '
        + repr(tuple(protected))
        + "}))"
    )
    after = json.loads(subprocess.check_output([str(executable), "-c", query], text=True))
    if after != protected:
        raise RuntimeError("Protected original runtime was shadowed in candidate environment")
    import yaml

    config = yaml.safe_load((root / "runtime/tts/tts_infer.yaml").read_text())["custom"]
    models = bundle / "assets/lite"
    for name, key in [
        ("chinese-hubert-base", "cnhuhbert_base_path"),
        ("chinese-roberta-wwm-ext-large", "bert_base_path"),
    ]:
        source = Path(config[key])
        source = (source if source.is_absolute() else root / source).resolve(strict=True)
        if not source.is_relative_to(root / "runtime/tts/pretrained"):
            raise RuntimeError("Unexpected existing encoder source")
        link = models / name
        if link.exists() or link.is_symlink():
            if link.resolve() != source:
                raise RuntimeError("Do not overwrite an existing candidate model path")
        else:
            link.symlink_to(source, target_is_directory=True)
    for relative in ["g2p/zh/opencpop-strict.txt", "sv/pretrained_eres2netv2w24s4ep4.ckpt"]:
        if not (models / relative).is_file():
            raise RuntimeError("Missing provisioned Lite asset " + relative)
    receipt = {
        "scope": "Chinese CUDA and provisioned English-mixed speech cases; Japanese extras excluded",
        "protected_runtime_unchanged": True,
        "protected": after,
        "installed_distributions": sorted(wanted),
        "optional_extras_not_installed": ["sounddevice", "onnxruntime", "pyopenjtalk-plus"],
        "gpu_or_model_imports_performed": False,
        "models_dir": str(models),
        "candidate_python": str(executable),
    }
    (bundle / "installation-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
