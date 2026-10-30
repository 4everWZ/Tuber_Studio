"""CPU-only checks for the owned program image and its small named build context.

This does not inspect arbitrary third-party CUDA dependency data or establish
GPU/voice quality. In-image invocation checks that project runtime is empty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlsplit

PRESETS = {"studio.json", "tts/tts_infer.yaml"}
PRIVATE = {
    "api_key",
    "sessdata",
    "csrf",
    "authorization",
    "admin_token",
    "overlay_token",
    "inference_token",
    "password",
}


def regular(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Program assets cannot contain links")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Embedded program presets must be regular files no larger than 1MiB")
    return path.read_bytes()


def public_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in PRIVATE and item not in (None, "", False):
                raise ValueError("A program preset contains credential data")
            public_values(item)
    elif isinstance(value, list):
        for item in value:
            public_values(item)


def audit_builtin(builtin):
    builtin = Path(builtin)
    manifest = json.loads(regular(builtin / "manifest.json"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unsupported public download manifest")
    local, downloadable, paths = set(), [], set()
    for record in manifest["files"]:
        name = record["path"]
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or name in paths:
            raise ValueError("Unsafe or duplicate public asset destination")
        paths.add(name)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or type(record["bytes"]) is not int
            or record["bytes"] < 1
        ):
            raise ValueError("Public artifacts require exact size and SHA256")
        if "urls" in record:
            if not name.startswith(("models/", "tts/sources/", "tts/pretrained/", "nltk_data/")):
                raise ValueError("Generated or private runtime data cannot enter the raw download manifest")
            if not isinstance(record["urls"], list) or not record["urls"]:
                raise ValueError("Public artifacts need HTTPS sources")
            for raw in record["urls"]:
                url = urlsplit(raw)
                if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
                    raise ValueError("Public sources cannot contain credentials or other protocols")
                if any(
                    key.lower() in PRIVATE | {"token", "access_token", "signature"}
                    for key, value in parse_qsl(url.query)
                ):
                    raise ValueError("Public manifest cannot contain private/signed query credentials")
            downloadable.append(record)
        else:
            if name not in PRESETS or record.get("policy") != "preserve":
                raise ValueError("Only the two small Operator presets can be embedded")
            content = regular(builtin / "files" / name)
            if len(content) != record["bytes"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("Embedded preset does not match the public manifest")
            local.add(name)
    if local != PRESETS or not downloadable:
        raise ValueError("Program bundle needs both presets and public artifact metadata")
    expected = {"manifest.json", *("files/" + name for name in PRESETS)}
    actual = set()
    for item in builtin.rglob("*"):
        if item.is_symlink():
            raise ValueError("Program bundle links are forbidden")
        if item.is_file():
            regular(item)
            actual.add(item.relative_to(builtin).as_posix())
    if actual != expected:
        raise ValueError("Program bundle contains unexpected payloads, weights or credentials")
    studio = json.loads(regular(builtin / "files/studio.json"))
    public_values(studio)
    if (
        studio["llm"]["model"] != "MiniCPM5-2B"
        or studio["llm"]["context_tokens"] != 131072
        or studio["tts"]["voice_id"] != "yaoyao_sovits_v2"
        or studio["tts"]["local_backend"] != "genie_onnx_int8"
        or studio["inference_broker"]["mode"] != "live"
    ):
        raise ValueError("Program preset does not describe the approved default resources")
    return {
        "program_only": True,
        "embedded_presets": sorted(local),
        "embedded_bytes": sum((builtin / "files" / name).stat().st_size for name in PRESETS),
        "download_artifact_count": len(downloadable),
        "download_bytes": sum(item["bytes"] for item in downloadable),
        "download_destinations": sorted(item["path"] for item in downloadable),
        "model_weights_embedded_in_builtin": False,
        "credentials_embedded_in_builtin": False,
        "gpu_or_voice_acceptance_verified": False,
    }


def audit_assets(assets):
    assets = Path(assets)
    report = audit_builtin(assets / "builtin")
    regular(assets / "run-ready.sh")
    files = {item.relative_to(assets).as_posix() for item in assets.rglob("*") if item.is_file()}
    expected = {"run-ready.sh", "builtin/manifest.json", *("builtin/files/" + name for name in PRESETS)}
    if files != expected:
        raise ValueError("Named build context contains files beyond the launcher and program presets")
    report["scope"] = "generated named build context; no model downloads or CUDA execution"
    return report


def audit_image(root, builtin):
    root = Path(root)
    report = audit_builtin(Path(builtin))
    runtime = root / "runtime"
    if runtime.is_symlink() or (runtime.exists() and any(runtime.iterdir())):
        raise ValueError("Program image must not contain an initialized runtime")
    for name in ("tmp", ".git", ".venv", ".env"):
        if (root / name).exists():
            raise ValueError("Private, checkout or evaluation data entered the owned image source tree")
    for name in ("src", "tools", "config", "assets"):
        for path in (root / name).rglob("*"):
            if path.is_file() and path.suffix.lower() in {
                ".gguf",
                ".ckpt",
                ".pth",
                ".safetensors",
                ".pt",
                ".onnx",
                ".bin",
            }:
                raise ValueError("Model artifacts entered owned program source/assets")
    regular(Path("/opt/tuber-launch/run-ready.sh"))
    report["scope"] = (
        "owned project/runtime/builtin paths inside the built image; CUDA dependency filesystem not audited"
    )
    report["empty_runtime_verified"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--assets", type=Path)
    source.add_argument("--image-root", type=Path)
    parser.add_argument("--builtin", type=Path, default=Path("/opt/tuber-builtin"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_assets(args.assets) if args.assets else audit_image(args.image_root, args.builtin)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
