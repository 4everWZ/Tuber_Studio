"""Fixed, measured inference-node artifacts and launch settings."""

import hashlib
from pathlib import Path

GEMMA_KEY = "llmgemma4e2b"
GEMMA_ENGINE = "runtime/engines/llama.cpp-0ec4027a2a0183de87c007069adddc06e665f7de/build/bin/llama-server"
GEMMA_ENGINE_SHA256 = "232375680d07e7ba9a387ae76b1563c30a953064073820180e8b6831ca6db36e"
GEMMA_ARTIFACTS = {
    "model": {
        "filename": "gemma-4-E2B-it-Q4_0.gguf",
        "sha256": "8e30dff3ac4c8434c49a7036fa15564bdbb6044e42bf04550bf1a096ad7e6a52",
    },
    "projector": {
        "filename": "mmproj-gemma-4-E2B-it-Q8_0.gguf",
        "sha256": "9406f99c16d68cda4f1f0552192dcc99021ea1fc6d2fd50b1dc3ccf30d04b292",
    },
    "vision_only_projector": {
        "filename": "mmproj-gemma-4-E2B-it-Q8_0-visiononly.gguf",
        "sha256": "6d01fb358e40fe508f5617038c45671d2a5d518af9e3c217620803d84b68a4f2",
    },
}


def project_asset(root: Path, value: str) -> Path:
    """Resolve a trusted operator path within this node's own asset root."""
    if not isinstance(value, str) or not value or ".." in Path(value).parts:
        raise ValueError("Inference asset path must stay inside its node")
    raw = Path(value)
    path = (raw if raw.is_absolute() else root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Inference asset path must stay inside its node")
    return path


def artifact_identity(path: Path):
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def verify_artifact(path: Path, expected: str, cache: dict):
    """Hash each immutable identity once; never rehash GB weights per request."""
    identity = artifact_identity(path)
    if cache.get(identity) == expected:
        return
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != expected:
        raise RuntimeError("Installed inference artifact does not match its fixed profile")
    cache[identity] = expected
