"""Fetch the v2 runtime and prototype-only voice; reuse local files offline."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from serve import check_source

SOURCE_URL = "https://codeload.github.com/RVC-Boss/GPT-SoVITS/tar.gz/refs/tags/20240821v2"
HF_FILES = {
    "chinese-hubert-base/config.json": 1449,
    "chinese-hubert-base/preprocessor_config.json": 212,
    "chinese-hubert-base/pytorch_model.bin": 188811417,
    "chinese-roberta-wwm-ext-large/config.json": 963,
    "chinese-roberta-wwm-ext-large/pytorch_model.bin": 651225145,
    "chinese-roberta-wwm-ext-large/tokenizer.json": 268962,
}
VOICE_FILES = {
    "weights/da_fei_yu-e12.ckpt": 155313395,
    "weights/da_fei_yu_e15_s300.pth": 85008003,
    "ref/dafeiyu_ref.wav": 278572,
    "ref/prompt.txt": 66,
}


def fetch(url: str, path: Path, expected_size: int | None, offline: bool) -> None:
    if path.is_file() and (expected_size is None or path.stat().st_size == expected_size):
        return
    if offline:
        raise RuntimeError(f"Offline prerequisite missing or incomplete: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    print(f"Downloading {path.name}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Tuber-Studio-prototype"})
    with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if expected_size is not None and partial.stat().st_size != expected_size:
        raise RuntimeError(f"Incomplete download: {partial}")
    partial.replace(path)


def verify_dependencies(requirements: Path) -> None:
    mismatches = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        package, version = line.split("==")
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        if actual != version:
            mismatches.append(f"{package}: expected {version}, got {actual}")
    # Metadata inspection avoids creating a CUDA context during setup checks.
    for package, version in (("torch", "2.7.0"), ("torchaudio", "2.7.0")):
        actual = importlib.metadata.version(package)
        if actual.split("+")[0] != version:
            mismatches.append(f"{package}: expected Jetson {version}, got {actual}")
    if mismatches:
        raise RuntimeError("Dependency mismatch:\n" + "\n".join(mismatches))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="Read-only offline validation")
    args = parser.parse_args()
    offline = args.offline or args.check_only
    root = Path(os.environ.get("TUBER_ROOT", "/workspace/tuber"))
    tools = Path(__file__).resolve().parent
    runtime = root / "runtime/tts"
    source = runtime / "GPT-SoVITS-v2"
    verify_dependencies(tools / "requirements.txt")
    if not (source / "api_v2.py").is_file():
        if offline:
            raise RuntimeError(f"Offline source prerequisite missing: {source}")
        if source.exists():
            raise RuntimeError(f"Refusing to replace incomplete existing source directory: {source}")
        runtime.mkdir(parents=True, exist_ok=True)
        (root / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tts-source-", dir=root / "tmp") as temporary:
            temporary = Path(temporary)
            archive = temporary / "v2.tar.gz"
            fetch(SOURCE_URL, archive, None, False)
            with tarfile.open(archive) as tar:
                tar.extractall(temporary / "source", filter="data")
            extracted = list((temporary / "source").iterdir())
            if len(extracted) != 1 or not (extracted[0] / "api_v2.py").is_file():
                raise RuntimeError("Unexpected official GPT-SoVITS source archive")
            shutil.move(str(extracted[0]), source)
    check_source(source)
    hf = os.environ.get("TTS_HF_BASE", "https://hf-mirror.com").rstrip("/")
    for relative, size in HF_FILES.items():
        fetch(
            f"{hf}/lj1995/GPT-SoVITS/resolve/main/{relative}",
            runtime / "pretrained" / relative,
            size,
            offline,
        )
    for relative, size in VOICE_FILES.items():
        url = (
            "https://www.modelscope.cn/api/v1/datasets/sirmn6/da-fei-yu-gpt-sovits/repo?Revision=master&FilePath="
            + relative
        )
        fetch(url, runtime / relative, size, offline)
    if not args.check_only:
        shutil.copyfile(tools / "tts_infer.yaml", runtime / "tts_infer.yaml")
    print("TTS prerequisites ready; voice is CC BY-NC 4.0, attribution: 尹紫鹭幻想社（Bilibili）")


if __name__ == "__main__":
    main()
