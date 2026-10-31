"""Stable identity for the container environment, independent of application code."""

import argparse
import hashlib
import json
from pathlib import Path

FILES = (
    "pyproject.toml",
    "uv.lock",
    "tools/jetson/Dockerfile",
    "tools/jetson/tts/requirements.txt",
    "tools/jetson/tts/genie-requirements.txt",
)


def contract(root):
    root = Path(root).resolve()
    vendor = root / "third_party/Genie-TTS"
    names = [
        *FILES,
        *sorted(
            path.relative_to(root).as_posix()
            for path in vendor.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        ),
    ]
    if not vendor.is_dir() or len(names) == len(FILES):
        raise ValueError("Missing pinned Genie dependency sources")
    identities = {}
    for name in names:
        path = root / name
        if not path.is_file() or any(
            part.is_symlink() or getattr(part, "is_junction", lambda: False)()
            for part in (path, *path.parents)
        ):
            raise ValueError("Environment inputs must be regular owned files")
        data = path.read_bytes()
        if path.suffix.lower() in {".py", ".md", ".txt", ".toml", ".lock"} or path.name == "Dockerfile":
            data = data.replace(b"\r\n", b"\n")
        identities[name] = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()
    return {"format": 1, "sha256": digest, "inputs": identities}


def verify(
    root,
    installed=Path("/opt/tuber-environment.json"),
    node_lock=Path("/opt/tuber-song-node/package-lock.json"),
):
    expected = contract(root)
    if not installed.is_file() or json.loads(installed.read_text()) != expected:
        raise ValueError(
            "Container environment differs from program dependencies; select/pull the matching environment image explicitly"
        )
    if node_lock.is_file():
        wanted = Path(root) / "components/song_requests/package-lock.json"
        if not wanted.is_file() or wanted.read_bytes().replace(
            b"\r\n", b"\n"
        ) != node_lock.read_bytes().replace(b"\r\n", b"\n"):
            raise ValueError(
                "Optional Node environment differs from the song component lock; rebuild/select its matching environment variant"
            )
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = contract(args.root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
