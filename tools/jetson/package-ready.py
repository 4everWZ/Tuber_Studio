"""Build a program-only context from public download metadata and small presets.

The builder never opens runtime/: private data and weights cannot enter images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

VOICE = "yaoyao_sovits_v2"


def checked(base: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or raw.anchor or ".." in raw.parts or "\\" in relative:
        raise ValueError("Preset path must be relative")
    path = base / raw
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)()
        for p in (path, *path.parents)
        if p != base
    ):
        raise ValueError("Preset links are disabled")
    if not path.resolve().is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("Preset escaped its fixed directory")
    return path


def package(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.absolute()
    if not output.is_relative_to(root / "tmp") or output.exists():
        raise ValueError("Use a new build context inside this project tmp/")
    if output.resolve() != output:
        raise ValueError("Build output cannot traverse links")
    manifest = json.loads(checked(root, "config/jetson/downloads.json").read_text(encoding="utf-8"))
    records = manifest["files"]
    for entry in records:
        name = entry["path"]
        if not name.startswith(("models/", "tts/sources/", "tts/pretrained/", "nltk_data/")):
            raise ValueError("Unexpected downloadable asset destination")
        if ".." in Path(name).parts or "\\" in name or ":" in name:
            raise ValueError("Unsafe downloadable asset path")
        if not entry.get("urls") or any(
            urlsplit(url).scheme != "https" or not urlsplit(url).hostname or urlsplit(url).username
            for url in entry["urls"]
        ):
            raise ValueError("Downloads require public HTTPS URLs")
    files = output / "builtin/files"
    files.mkdir(parents=True)
    preset = json.loads(checked(root, "config/studio.example.json").read_text(encoding="utf-8"))
    preset["features"]["live_enabled"] = True
    preset["inference_broker"]["mode"] = "live"
    preset["tts"].update(voice_id=VOICE, local_backend="genie_onnx_int8")
    seeds = {
        "studio.json": json.dumps(preset, ensure_ascii=False, indent=2).encode(),
        "tts/tts_infer.yaml": checked(root, "config/jetson/tts_infer.yaml").read_bytes(),
    }
    for name, content in seeds.items():
        target = files / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append(
            {
                "path": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": 0o600,
                "policy": "preserve",
            }
        )
    (output / "builtin/manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.copyfile(checked(root, "tools/jetson/run-ready.sh"), output / "run-ready.sh")
    return {
        "download_files": len(records) - len(seeds),
        "download_bytes": sum(item["bytes"] for item in records if "urls" in item),
        "embedded_asset_bytes": sum(map(len, seeds.values())),
        "output": str(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.root, args.output)), flush=True)


if __name__ == "__main__":
    main()
