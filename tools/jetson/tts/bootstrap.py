"""Prepare the fixed Yaoyao production bundle after pinned source downloads.

Each expensive step owns a short-lived process in the image's existing Python
environment. No service, download, dependency installation or fallback is run.
runtime/evaluations is used only because the audited preparation tools require
that boundary; this initializer removes its own scratch tree on every exit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VOICE = "yaoyao_sovits_v2"
TRANSCRIPT = "甘雨姐姐，你没事吧？要不要喝点水，或者吃点别的什么调节一下？"
SOURCES = {
    "gpt-original.ckpt": (155312205, "c1d31f76a9a9344a13fc09e0cb7df3e4dd60d92884839e4e06c60576b3df7382"),
    "sovits-original.pth": (85007424, "325ee1359f0281868bda4c6b5462c52f3e6d5c8bd22708f297019e16961193d7"),
    "reference-original.wav": (551980, "bcce2b972263eb68e94eeed5fd99261f64729c8a6ac9eee4fd6f33db1b8b9c64"),
}
REFERENCE_SHA = "95335007648d2cb1ce896e11bf312de971f9852a00280527fb623ae5b9999aab"
PHASES = ("nltk", "sanitize", "resample", "convert", "expand", "quantize", "bert", "reference", "install")
DEADLINES = {phase: 180 for phase in PHASES} | {"nltk": 60, "sanitize": 90, "bert": 300}


class Interrupted(RuntimeError):
    pass


def interrupt(signum, frame):
    raise Interrupted("Bootstrap interrupted by signal " + str(signum))


def helper(root, name):
    if name not in {"genie_bundle", "genie_sampling", "compact"}:
        raise ValueError("Unknown bootstrap helper")
    path = root / "tools/jetson/tts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("_bootstrap_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked(root, path, *, exists=True):
    return helper(root, "genie_bundle").checked_path(path, root, exists=exists)


def validate_configuration(root):
    import yaml

    studio = json.loads(checked(root, root / "runtime/studio.json").read_text())
    if any(
        studio.get("tts", {}).get(k) != v
        for k, v in {"kind": "gpt_sovits", "voice_id": VOICE, "local_backend": "genie_onnx_int8"}.items()
    ):
        raise ValueError(
            "Bootstrap requires the explicitly selected Yaoyao Genie voice; configuration is preserved"
        )
    voices = json.loads(checked(root, root / "config/voices.json").read_text(encoding="utf-8"))
    voice = next((v for v in voices if v.get("id") == VOICE), None)
    if not voice or any(
        voice.get(k) != v
        for k, v in {
            "provider": "gpt_sovits",
            "reference_text": TRANSCRIPT,
            "reference_asset": "runtime/tts/ref/yaoyao-reference32k.wav",
            "server_reference_path": str(root / "runtime/tts/ref/yaoyao-reference32k.wav"),
            "version": "UnlimitedBurst-c649dfd-v2-e10",
            "weights_version": "yaoyao-v2-e10-e10s200",
        }.items()
    ):
        raise ValueError("Yaoyao voice identity/reference differs; explicit preparation is required")
    config = yaml.safe_load(checked(root, root / "runtime/tts/tts_infer.yaml").read_text())["custom"]
    expected = {
        "version": "v2",
        "device": "cuda",
        "is_half": True,
        "t2s_weights_path": str(root / "runtime/tts/weights/yaoyao-s1-sanitized.ckpt"),
        "vits_weights_path": str(root / "runtime/tts/weights/yaoyao-s2-sanitized.pth"),
        "bert_base_path": str(root / "runtime/tts/pretrained/chinese-roberta-wwm-ext-large"),
        "cnhuhbert_base_path": str(root / "runtime/tts/pretrained/chinese-hubert-base"),
    }
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError("TTS YAML differs from the accepted CUDA/FP16 fixed-reference profile; not modified")


def publish(root, source, target):
    """Publish one prepared file without replacing any Operator-owned bytes."""
    checked(root, target, exists=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    checked(root, target.parent)
    if target.exists():
        if digest(source) != digest(target):
            raise ValueError("Existing prepared file differs and was preserved: " + target.name)
        return
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        checked(root, target)
        if digest(source) != digest(target):
            raise ValueError("Concurrent prepared file differs and was preserved") from None


class HParams:
    """Inert data shell for the single audited legacy utils.HParams pickle name."""


def plain(value):
    if type(value) is HParams:
        return plain(vars(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Checkpoint configuration keys must be strings")
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(plain(item) for item in value)
    if value is None or type(value) in {str, bool, int} or type(value) is float and math.isfinite(value):
        return value
    raise ValueError("Unsupported object in checkpoint configuration")


def sanitize(root, work):
    import torch

    torch.set_num_threads(4)
    for raw, filename in (
        ("gpt-original.ckpt", "yaoyao-s1-sanitized.ckpt"),
        ("sovits-original.pth", "yaoyao-s2-sanitized.pth"),
    ):
        source = checked(root, root / "runtime/tts/sources" / raw)
        size, sha = SOURCES[raw]
        if source.stat().st_size != size or digest(source) != sha:
            raise ValueError("Downloaded Yaoyao checkpoint identity differs")
        with torch.serialization.safe_globals([(HParams, "utils.HParams")]):
            original = torch.load(source, map_location="cpu", weights_only=True)
        if not isinstance(original, dict) or not isinstance(original.get("weight"), dict):
            raise ValueError("Expected original checkpoint weight dictionary")
        if not original["weight"] or any(
            not isinstance(k, str) or not isinstance(v, torch.Tensor) for k, v in original["weight"].items()
        ):
            raise ValueError("Checkpoint weights must contain tensors only")
        clean = {"weight": original["weight"], "config": plain(original["config"])}
        if "info" in original:
            clean["info"] = plain(original["info"])
        target = work / filename
        torch.save(clean, target)
        restored = torch.load(target, map_location="cpu", weights_only=True)
        if set(restored["weight"]) != set(original["weight"]) or any(
            not torch.equal(value, restored["weight"][key]) for key, value in original["weight"].items()
        ):
            raise ValueError("Sanitization changed checkpoint tensors")
        publish(root, target, root / "runtime/tts/weights" / filename)
        del restored, clean, original


def namespace(source):
    package = source / "src/genie_tts"
    spec = importlib.util.spec_from_file_location(
        "genie_tts", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    sys.modules["genie_tts"] = importlib.util.module_from_spec(spec)


def convert(root, work):
    import torch

    torch.set_num_threads(4)
    namespace(Path(os.environ.get("TUBER_GENIE_ROOT", "/opt/Genie-TTS")))
    output = work / "converted"
    module = importlib.import_module("genie_tts.Converter.v2.Converter")
    module.convert(
        str(root / "runtime/tts/weights/yaoyao-s1-sanitized.ckpt"),
        str(root / "runtime/tts/weights/yaoyao-s2-sanitized.pth"),
        str(output),
    )
    if not output.is_dir() or len(list(output.iterdir())) != 7:
        raise ValueError("Genie v2 conversion failed to produce its seven fixed artifacts")


def expand(root, work):
    import numpy as np

    models, restored = work / "models", work / "restored"
    models.mkdir()
    restored.mkdir()
    for source in (work / "converted").iterdir():
        if source.name.endswith(".onnx") or source.name == "t2s_encoder_fp32.bin":
            shutil.copyfile(source, models / source.name)
    for name in ("t2s_shared", "vits"):
        raw = np.fromfile(work / "converted" / (name + "_fp16.bin"), dtype=np.float16)
        raw.astype(np.float32).tofile(models / (name + "_fp32.bin"))
        del raw
    sampling = helper(root, "genie_sampling")
    shutil.copyfile(models / "t2s_shared_fp32.bin", restored / "t2s_shared_fp32.bin")
    for filename, kind in (
        ("t2s_first_stage_decoder_fp32.onnx", "first"),
        ("t2s_stage_decoder_fp32.onnx", "stage"),
    ):
        sampling.patch_graph(models / filename, restored / filename, kind)


def phase(root, work, name):
    if name == "nltk":
        module = runpy.run_path(str(root / "tools/jetson/initialize-data.py"))
        module["ensure_nltk_data"](root / "runtime/nltk_data", offline=True)
        module["verify_nltk_consumers"](root / "runtime/nltk_data")
    elif name == "sanitize":
        sanitize(root, work)
    elif name == "resample":
        source = checked(root, root / "runtime/tts/sources/reference-original.wav")
        if source.stat().st_size != SOURCES[source.name][0] or digest(source) != SOURCES[source.name][1]:
            raise ValueError("Original Yaoyao reference identity differs")
        target = work / "yaoyao-reference32k.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-ar",
                "32000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            check=True,
            timeout=60,
        )
        if digest(target) != REFERENCE_SHA:
            raise ValueError("Resampled reference differs from the accepted PCM reference")
        publish(root, target, root / "runtime/tts/ref/yaoyao-reference32k.wav")
    elif name == "convert":
        convert(root, work)
    elif name == "expand":
        expand(root, work)
    elif name in {"quantize", "bert"}:
        script = root / "tools/jetson/tts" / ("genie_quantize.py" if name == "quantize" else "genie_bert.py")
        args = (
            ["--models", str(work / "restored"), "--output", str(work / "ar")]
            if name == "quantize"
            else ["--root", str(root), "--output", str(work / "bert"), "--threads", "4"]
        )
        os.execv(sys.executable, [sys.executable, str(script), *args, "--execute"])
    elif name == "reference":
        compact = helper(root, "compact")
        directory = root / "runtime/tts/reference_features" / VOICE
        if directory.exists():
            compact.validate_reference(root, voice_id=VOICE)
        else:
            try:
                compact.prepare(root, voice_id=VOICE)
            except BaseException:
                if directory.exists():
                    shutil.rmtree(checked(root, directory))
                raise
    elif name == "install":
        helper(root, "genie_bundle").install_bundle(root, VOICE, work / "models", work / "ar", work / "bert")
    else:
        raise ValueError("Unknown preparation phase")


def ensure_no_workers():
    import psutil

    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        args = process.info["cmdline"] or []
        if process.info["name"] == "llama-server" or any(
            arg.endswith(("/tts/server.py", "/tts/serve.py", "/GPT-SoVITS-v2/api_v2.py")) for arg in args
        ):
            raise RuntimeError("Model workers must be stopped before first bundle preparation")


def run_phase(root, work, name, watch):
    watch.sample()
    environment = os.environ | {
        "TUBER_ROOT": str(root),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TORCH_FORCE_WEIGHTS_ONLY_LOAD": "1",
        "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "0",
        "TMPDIR": str(work),
    }
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--root",
        str(root),
        "--phase",
        name,
        "--work",
        str(work),
    ]
    started = time.monotonic()
    print(json.dumps({"bootstrap_phase": name, "state": "starting"}), flush=True)
    with (work / (name + ".log")).open("wb") as log:
        child = subprocess.Popen(
            command,
            cwd=work,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while child.poll() is None:
                watch.sample()
                if time.monotonic() - started >= DEADLINES[name]:
                    raise TimeoutError("Preparation phase deadline: " + name)
                time.sleep(0.5)
            watch.sample()
            if child.returncode:
                raise RuntimeError(
                    "Preparation phase failed: "
                    + name
                    + "; "
                    + (work / (name + ".log")).read_text(errors="replace")[-2500:]
                )
        finally:
            if child.poll() is None:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGTERM)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(child.pid, signal.SIGKILL)
                    else:
                        child.kill()
                    child.wait(timeout=2)
    print(
        json.dumps({"bootstrap_phase": name, "state": "completed", "seconds": time.monotonic() - started}),
        flush=True,
    )


def prepare(root):
    root = Path(root).absolute()
    checked(root, root)
    bundle = helper(root, "genie_bundle")
    validate_configuration(root)
    final = root / "runtime/tts/genie" / VOICE
    if final.exists() or final.is_symlink():
        return {"reused": True, "bundle_identity": bundle.verify_bundle(root, VOICE)["identity"]}
    import fcntl

    lock = checked(root, root / "runtime/tts/.bootstrap.lock", exists=False)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if final.exists():
            return {"reused": True, "bundle_identity": bundle.verify_bundle(root, VOICE)["identity"]}
        ensure_no_workers()
        protected = [
            root / p for p in ("config/voices.json", "runtime/studio.json", "runtime/tts/tts_infer.yaml")
        ]
        original = {path: digest(path) for path in protected}
        boundary = checked(root, root / "runtime/evaluations", exists=False)
        boundary.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(root / "tools"))
        from evaluate_context import MemoryWatch

        watch = MemoryWatch(900)
        reference = root / "runtime/tts/reference_features" / VOICE
        reference_existed = reference.exists()
        try:
            with tempfile.TemporaryDirectory(prefix="bootstrap-preparation-", dir=boundary) as temporary:
                work = Path(temporary)
                for name in PHASES:
                    ensure_no_workers()
                    if any(digest(path) != expected for path, expected in original.items()):
                        raise ValueError("Operator configuration changed during preparation; stopped")
                    run_phase(root, work, name, watch)
                if any(digest(path) != expected for path, expected in original.items()):
                    raise ValueError("Operator configuration changed during preparation; stopped")
                result = bundle.verify_bundle(root, VOICE)
        finally:
            if not reference_existed and not final.exists() and reference.exists():
                shutil.rmtree(checked(root, reference))
        return {"reused": False, "bundle_identity": result["identity"], "memory": watch.summary()}


def main():
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/tuber"))
    parser.add_argument("--phase", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--work", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.phase:
        boundary = checked(args.root, args.root / "runtime/evaluations")
        work = checked(args.root, args.work)
        if work.parent != boundary or not work.name.startswith("bootstrap-preparation-"):
            raise ValueError("Preparation requires its owned bootstrap scratch directory")
        phase(args.root, work, args.phase)
    else:
        print(json.dumps(prepare(args.root)), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Interrupted as error:
        print(json.dumps({"bootstrap_interrupted": str(error)}), file=sys.stderr, flush=True)
        raise SystemExit(130) from None
