"""Fixed, measured inference-node artifacts and launch settings."""

import hashlib
from pathlib import Path

DAFEIYU_KEY = "llmdafeiyu"
DAFEIYU_MODEL = "MiniCPM-V-4.7-1B"
DAFEIYU_WEIGHTS = "MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf"
DAFEIYU_ALIASES = {
    DAFEIYU_KEY,
    DAFEIYU_MODEL.lower(),
    DAFEIYU_WEIGHTS.lower(),
    "minicpm-v-4_7-1b",
    "minicpm-v-4.7-1b-dafeiyu",
    "minicpm-v-4_7-1b-dafeiyu",
    "minicpm_v_dafeiyu",
    "minicpm-v-4.7-1b (大肥鱼专属微调)",
}
GEMMA_KEY = "llmgemma4e2b"
GROUNDED_DAFEIYU_KEY = "llmdafeiyu_dora"
GROUNDED_DAFEIYU_MODEL = "MiniCPM-V-4.7-1B-Dafeiyu-Grounded"
GROUNDED_DAFEIYU_BUNDLE = "authentic-dora-20261107"
GROUNDED_DAFEIYU_WEIGHTS = GROUNDED_DAFEIYU_MODEL + "-Q4_K_M.gguf"
VISION_ROUTES = {"gemma4e2b": GEMMA_KEY, "dafeiyu": GROUNDED_DAFEIYU_KEY}
LLM_MODELS = {
    "llm2b": "MiniCPM5-2B",
    "llm1b": "MiniCPM5-1B",
    DAFEIYU_KEY: DAFEIYU_MODEL,
    GROUNDED_DAFEIYU_KEY: GROUNDED_DAFEIYU_MODEL,
    "llm1bq8": "MiniCPM5-1B-Q8_0",
    "llmqwen35": "Qwen3.5-2B",
    GEMMA_KEY: "Gemma4-E2B",
}
LLM_ALIASES = (
    {
        alias: key
        for key, model in LLM_MODELS.items()
        for alias in (key, key.removeprefix("llm"), model.lower())
    }
    | dict.fromkeys(DAFEIYU_ALIASES, DAFEIYU_KEY)
    | {
        "minicpm5-2b (本地默认)": "llm2b",
        "minicpm5-1b-q8": "llm1bq8",
        "qwen_qwen3.5-2b": "llmqwen35",
        "gemma-4-e2b-it": GEMMA_KEY,
    }
)
GEMMA_ENGINE = "runtime/engines/llama.cpp-0ec4027a2a0183de87c007069adddc06e665f7de/build/bin/llama-server"
GEMMA_ENGINE_SHA256 = "232375680d07e7ba9a387ae76b1563c30a953064073820180e8b6831ca6db36e"
GROUNDED_DAFEIYU_ARTIFACTS = {
    "model": "8fab6fa834020011a37bfd82fb6c74509ea6c57d826820480d8f99182b7d74cf",
    "projector": "20977a90fd8d6381a84588d5675f97abaae2a1d882126c8e86c7d0eadfe183ca",
    "engine": GEMMA_ENGINE_SHA256,
}
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


def llm_resource_key(model_name):
    return LLM_ALIASES.get(model_name.strip().lower()) if isinstance(model_name, str) else None


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
