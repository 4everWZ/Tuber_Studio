"""Deliver tracked public program files separately from the environment image."""

import argparse
import importlib.util
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("tuber_environment", ROOT / "tools/jetson/environment.py")
environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(environment)
PATHS = (
    "src",
    "config",
    "assets",
    "tools",
    "third_party",
    "components/song_requests",
    "evaluations/semantic-cases.json",
    "evaluations/speech-cases.json",
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    "NOTICE",
)


def package(root, output, repository="4everWZ/Tuber_Studio"):
    root, output = Path(root).resolve(), Path(output).absolute()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid image repository")
    if not output.is_relative_to(root / "tmp") or output.exists() or output.resolve() != output:
        raise ValueError("Use a new program archive inside project tmp/")
    identity = environment.contract(root)
    image = "ghcr.io/" + repository.lower() + ":env-" + identity["sha256"][:24]
    files = subprocess.check_output(["git", "ls-files", "-z", "--", *PATHS], cwd=root).decode().split("\0")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for name in sorted(filter(None, files)):
            path = root / name
            if not path.is_file() or any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("Program files cannot contain missing files or links")
            if path.suffix.lower() in {
                ".gguf",
                ".ckpt",
                ".pth",
                ".safetensors",
                ".onnx",
                ".pt",
            } and not name.startswith("third_party/Genie-TTS/"):
                raise ValueError("Program bundle cannot contain model weights")
            archive.add(path, arcname="program/" + name, recursive=False)
        content = (image + "\n").encode()
        member = tarfile.TarInfo("program/environment-image.txt")
        member.size, member.mode = len(content), 0o644
        archive.addfile(member, io.BytesIO(content))
    return {
        "program_archive": str(output),
        "environment_image": image,
        "environment_sha256": identity["sha256"],
        "source_files": len(list(filter(None, files))),
        "bytes": output.stat().st_size,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", default="4everWZ/Tuber_Studio")
    args = parser.parse_args()
    print(json.dumps(package(args.root, args.output, args.repository)))
