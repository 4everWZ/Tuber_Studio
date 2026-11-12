"""Prepare persistent weights/config using the final image, without loading CUDA."""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

# g2p_en 2.1 checks the legacy ZIP names at import. NLTK 3.10's pos_tag
# additionally reads the English JSON model, so all three packages are required.
NLTK_PACKAGES = {
    "corpora/cmudict": {"cmudict/cmudict": 3820830},
    "taggers/averaged_perceptron_tagger": {
        "averaged_perceptron_tagger/averaged_perceptron_tagger.pickle": 6138625
    },
    "taggers/averaged_perceptron_tagger_eng": {
        "averaged_perceptron_tagger_eng/averaged_perceptron_tagger_eng.weights.json": 5677744,
        "averaged_perceptron_tagger_eng/averaged_perceptron_tagger_eng.classes.json": 285,
        "averaged_perceptron_tagger_eng/averaged_perceptron_tagger_eng.tagdict.json": 25788,
    },
}

# Fixed artifacts only. Filenames/aliases belong to RESOURCES; provenance and
# the publishers' HF LFS digests are pinned here, never supplied by HTTP callers.
LLM_ARTIFACTS = {
    "llm2b": {
        "repository": "openbmb/MiniCPM5-2B-GGUF",
        "revision": "2079a22f3beaa4e306449978533478fe0522f4b3",
        "size": 1561318368,
        "sha256": "ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd",
        "architecture": "llama",
        "native_context": 131072,
    },
    "llm1b": {
        "repository": "openbmb/MiniCPM5-1B-GGUF",
        "revision": "3d55fac80935ae6456986ad2384b5cbcc4d6c948",
        "size": 688065920,
        "sha256": "81b64d05a23b17b34c475f42b3e72fbde62d4b92cc34541f7a8031d0752deafa",
        "architecture": "llama",
        "native_context": 131072,
    },
    "llm1bq8": {
        "repository": "openbmb/MiniCPM5-1B-GGUF",
        "revision": "3d55fac80935ae6456986ad2384b5cbcc4d6c948",
        "size": 1153529216,
        "sha256": "0dc7638539067268774c275a14a6ec9c7e01f7eeb2cff606c8590361fa527e4c",
        "architecture": "llama",
        "native_context": 131072,
    },
    # Community quantization, not a Qwen-published GGUF.
    "llmqwen35": {
        "repository": "bartowski/Qwen_Qwen3.5-2B-GGUF",
        "revision": "7d26695454df6de5fbcce2e58681e62dae06ce43",
        "size": 1396198496,
        "sha256": "57a1085840f497d764a7fc5d346922dbde961efb54cc792ea81d694fd846a1d8",
        "architecture": "qwen35",
        "native_context": 262144,
        "upstream": "Qwen/Qwen3.5-2B",
        "upstream_revision": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "license": "Apache-2.0",
    },
}


def prepare_llm(root: Path, key: str, offline: bool) -> None:
    """Prepare one fixed text model without changing the selected profile."""
    from tuber_studio.inference import RESOURCES, native_context
    from tuber_studio.inference_profiles import DAFEIYU_9B_CONTEXT, DAFEIYU_9B_KEY, DAFEIYU_9B_PROJECTOR

    resource = RESOURCES[key]
    path = root / "runtime/models" / resource.model
    if key == DAFEIYU_9B_KEY:
        from tuber_studio.portable import DOWNLOAD_SECONDS, _download_missing, _existing

        names = {f"models/{resource.model}", f"models/{DAFEIYU_9B_PROJECTOR}"}
        entries = json.loads((root / "config/jetson/downloads.json").read_text(encoding="utf-8"))["files"]
        selected = [entry for entry in entries if entry["path"] in names]
        if {entry["path"] for entry in selected} != names:
            raise ValueError("The pinned 9B model and projector must both be in the download manifest")
        runtime = root / "runtime"
        for entry in selected:
            deadline = time.monotonic() + DOWNLOAD_SECONDS
            target = runtime / entry["path"]
            if offline:
                if not _existing(target, entry, deadline):
                    raise FileNotFoundError(f"Missing offline asset: {entry['path']}")
            else:
                _download_missing(target, entry, runtime, deadline)
        if native_context(path, expected_architecture="qwen35") != DAFEIYU_9B_CONTEXT:
            raise RuntimeError(f"{resource.alias} does not declare its expected native window")
        print(f"{resource.alias} weights and projector verified")
        return

    from prepare_compact import fetch

    artifact = LLM_ARTIFACTS[key]
    mirror = os.environ.get("TUBER_HF_BASE", "https://hf-mirror.com").rstrip("/")
    url = f"{mirror}/{artifact['repository']}/resolve/{artifact['revision']}/{resource.model}"
    fetch(url, path, artifact["size"], offline)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != artifact["sha256"]:
        raise RuntimeError(f"{resource.alias} does not match the publisher's HF LFS digest")
    if native_context(path, expected_architecture=artifact["architecture"]) != artifact["native_context"]:
        raise RuntimeError(f"{resource.alias} does not declare its expected native window")
    print(f"{resource.alias} text weights verified; no model was loaded or selected")


def valid_archive(path: Path, members: dict[str, int]) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None and all(
                archive.getinfo(name).file_size == size for name, size in members.items()
            )
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def download_nltk(relative: str, target: Path, members: dict[str, int]) -> None:
    """Fetch fixed official packages directly, without downloader index or prompts."""
    urls = (
        f"https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/{relative}.zip",
        f"https://api.github.com/repos/nltk/nltk_data/contents/packages/{relative}.zip?ref=gh-pages",
    )
    partial = target.with_suffix(".zip.part")
    failures = []
    for url in urls:
        deadline = time.monotonic() + 30
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw+json"})
            with urllib.request.urlopen(request, timeout=10) as response, partial.open("wb") as output:
                total = 0
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > 16 * 1024 * 1024 or time.monotonic() > deadline:
                        raise RuntimeError("NLTK download exceeded size/time bound")
                    output.write(chunk)
            if not valid_archive(partial, members):
                raise RuntimeError("NLTK archive CRC or required member sizes invalid")
            partial.replace(target)
            return
        except (OSError, RuntimeError) as error:
            failures.append(str(error))
        finally:
            partial.unlink(missing_ok=True)
    raise RuntimeError(f"Cannot download official NLTK package {relative}: {'; '.join(failures)}")


def ensure_nltk_data(directory: Path, offline: bool) -> None:
    if directory.exists():
        directory.chmod(directory.stat().st_mode & ~0o022)
    for relative, members in NLTK_PACKAGES.items():
        archive_path = directory / (relative + ".zip")
        if not valid_archive(archive_path, members):
            if offline:
                raise RuntimeError(f"Offline NLTK prerequisite missing or corrupt: {archive_path}")
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            download_nltk(relative, archive_path, members)
        # NLTK 3.10 rejects data writable by other accounts. Restrict only this
        # managed data tree; the surrounding runtime keeps its operator policy.
        for managed in (directory, archive_path.parent, archive_path):
            managed.chmod(managed.stat().st_mode & ~0o022)
        if all(
            (archive_path.parent / name).is_file() and (archive_path.parent / name).stat().st_size == size
            for name, size in members.items()
        ):
            extracted = True
        else:
            extracted = False
        if not extracted:
            # Extract only the known consumer files: no archive-controlled paths.
            with zipfile.ZipFile(archive_path) as archive:
                for name in members:
                    target = archive_path.parent / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    partial = target.with_suffix(target.suffix + ".part")
                    partial.write_bytes(archive.read(name))
                    partial.replace(target)
        for name in members:
            target = archive_path.parent / name
            for managed in (target.parent, target):
                managed.chmod(managed.stat().st_mode & ~0o022)


def verify_nltk_consumers(directory: Path) -> None:
    """Exercise the actual CPU consumers using only this persistent data directory."""
    os.environ["NLTK_DATA"] = str(directory)
    import nltk
    from nltk.corpus import cmudict

    nltk.data.path[:] = [str(directory)]
    for relative in NLTK_PACKAGES:
        nltk.data.find(relative + ".zip")
    lexicon = cmudict.dict()
    if len(lexicon) < 100000 or "hello" not in lexicon:
        raise RuntimeError("NLTK pronunciation lexicon is incomplete")
    if not nltk.pos_tag(["hello", "world"]):
        raise RuntimeError("NLTK English tagger returned no tokens")
    from g2p_en import G2p

    phones = G2p()("hello world")
    if "HH" not in phones or "W" not in phones:
        raise RuntimeError(f"g2p_en pronunciation smoke failed: {phones}")
    print(f"Offline NLTK consumers ready: {len(lexicon)} words; hello world -> {phones}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--check-only", action="store_true", help="Read-only offline compact voice validation"
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--candidate", choices=["qwen35", "dafeiyu9b", "minicpm2b"], help="Prepare only this text profile")
    scope.add_argument(
        "--compact-voice-only", action="store_true", help="Prepare only the compact DaFeiYu voice"
    )
    args = parser.parse_args(argv)
    if args.check_only and not args.compact_voice_only:
        parser.error("--check-only requires --compact-voice-only")
    root = Path(os.environ.get("TUBER_ROOT", "/workspace/tuber"))
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tts"))

    if args.candidate:
        prepare_llm(root, {"qwen35": "llmqwen35", "dafeiyu9b": "llmdafeiyu9b", "minicpm2b": "llm2b"}[args.candidate], args.offline)
        return
    from prepare_compact import prepare_voice

    if args.compact_voice_only:
        prepare_voice(root, offline=args.offline, check_only=args.check_only)
        return

    from tuber_studio.inference_profiles import DAFEIYU_9B_KEY, DAFEIYU_9B_MODEL, llm_resource_key

    nltk_directory = root / "runtime/nltk_data"
    ensure_nltk_data(nltk_directory, args.offline)
    verify_nltk_consumers(nltk_directory)
    configuration = root / "runtime/studio.json"
    source = configuration if configuration.exists() else root / "config/studio.example.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    alias = data.get("llm", {}).get("model", DAFEIYU_9B_MODEL)
    selected = llm_resource_key(alias)
    if selected not in {*LLM_ARTIFACTS, DAFEIYU_9B_KEY}:
        raise RuntimeError("Configured LLM does not match a fixed downloadable profile")
    prepare_llm(root, selected, args.offline)
    if not configuration.is_file():
        configuration.parent.mkdir(parents=True, exist_ok=True)
        configuration.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    prepare_voice(root, offline=args.offline)


if __name__ == "__main__":
    main()
