"""Evaluate matched baseline/compact PCM; the operator owns resource unloading.

Run inside the project CUDA environment after stopping business/automatic jobs
and unloading GPU workers. The external owner must restore the LLM in finally.
This tool never controls the broker or rewrites configuration/reference caches.
Each fresh process uses one owner thread, production warmup, then three texts
twice with seed 42. Legacy WAV differences never become a quality threshold.
"""

from __future__ import annotations

import argparse
import array
import ast
import asyncio
import hashlib
import json
import math
import os
import signal
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPEATS = 2
DEFAULTS = {
    "aux_ref_audio_paths": [],
    "top_k": 5,
    "top_p": 1,
    "temperature": 1,
    "text_split_method": "cut0",
    "batch_size": 1,
    "batch_threshold": 0.75,
    "speed_factor": 1.0,
    "split_bucket": True,
    "return_fragment": False,
    "fragment_interval": 0.3,
    "seed": 42,
    "parallel_infer": True,
    "repetition_penalty": 1.35,
}


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def save(path, body):
    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def assert_no_workers():
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = path.read_bytes().rstrip(b"\0").decode(errors="replace").split("\0")
        except OSError:
            continue
        if argv and (
            Path(argv[0]).name == "llama-server"
            or any(arg.endswith("/tools/jetson/tts/server.py") for arg in argv)
        ):
            raise RuntimeError("Operator must stop existing project GPU workers before this probe")


def inspect_wav(path):
    with wave.open(str(path), "rb") as wav:
        shape = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes())
        raw = wav.readframes(shape[3])
    if (
        shape[0] != 1
        or shape[1] != 2
        or not 8000 <= shape[2] <= 192000
        or not shape[3]
        or len(raw) != shape[3] * 2
        or not any(raw)
    ):
        raise ValueError("Missing, truncated, invalid or silent PCM16 WAV")
    samples = array.array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return shape, raw, samples


def difference(left, right):
    ls, raw, values = inspect_wav(left)
    rs, refraw, reference = inspect_wav(right)
    row = {
        "left": str(left),
        "right": str(right),
        "left_shape": ls,
        "right_shape": rs,
        "left_sha256": hashlib.sha256(raw).hexdigest(),
        "right_sha256": hashlib.sha256(refraw).hexdigest(),
        "same_shape": ls == rs,
        "comparison_available": ls == rs,
        "unavailable_reason": None
        if ls == rs
        else "WAV shapes differ; sample-aligned numerical differences unavailable",
        "bit_identical": False,
        "changed_samples": None,
        "max_abs_pcm16": None,
        "mse_pcm16": None,
        "rms_difference_pcm16": None,
        "mean_abs_pcm16": None,
    }
    if ls == rs:
        diffs = [int(a) - int(b) for a, b in zip(values, reference)]
        mse = sum(d * d for d in diffs) / len(diffs)
        row.update(
            bit_identical=raw == refraw,
            changed_samples=sum(d != 0 for d in diffs),
            samples=len(diffs),
            max_abs_pcm16=max(map(abs, diffs)),
            mse_pcm16=mse,
            rms_difference_pcm16=math.sqrt(mse),
            mean_abs_pcm16=sum(map(abs, diffs)) / len(diffs),
        )
    return row


def compare_files(left, right, **labels):
    try:
        return {**difference(left, right), **labels}
    except (OSError, EOFError, ValueError, wave.Error) as error:
        return {
            "left": str(left),
            "right": str(right),
            **labels,
            "comparison_available": False,
            "bit_identical": False,
            "max_abs_pcm16": None,
            "mse_pcm16": None,
            "unavailable_reason": f"{type(error).__name__}: {error}",
        }


def same_order_summary(rows, *, sentences=3, repeats=REPEATS, variants_complete=False):
    expected = {(index, repeat) for index in range(sentences) for repeat in range(repeats)}
    pairs = [row for row in rows if row.get("kind") == "same_order"]
    keys = [(row.get("index"), row.get("repeat")) for row in pairs]
    complete = len(keys) == len(expected) and set(keys) == expected
    identical = complete and all(
        row.get("comparison_available") and row.get("bit_identical") for row in pairs
    )
    return {
        "expected_pairs": len(expected),
        "reported_pairs": len(keys),
        "pair_set_complete": complete,
        "missing_pairs": sorted(expected - set(keys)),
        "duplicate_pairs": len(keys) != len(set(keys)),
        "variants_complete": variants_complete,
        "bitwise_passed": bool(variants_complete and identical),
        "scope": "Only these texts, call order, weights, precision and execution protocol; not arbitrary repeat determinism or perceptual quality",
    }


def production_warmup(root):
    """Read the literal request from production instead of maintaining a second one."""
    tree = ast.parse((root / "tools/jetson/tts/server.py").read_text(encoding="utf-8"))
    engine = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SpeechEngine"
    )
    load = next(node for node in engine.body if isinstance(node, ast.FunctionDef) and node.name == "load")
    calls = [
        node
        for node in ast.walk(load)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "request"
    ]
    if len(calls) != 1 or len(calls[0].args) != 1:
        raise ValueError("Production warmup contract changed; update the parity harness")
    body = ast.literal_eval(calls[0].args[0])
    if not isinstance(body, dict) or not body.get("text") or body.get("seed") != 42:
        raise ValueError("Expected one explicit seeded production warmup")
    return body


def backend_flags(torch):
    return {
        "cudnn_deterministic": torch.backends.cudnn.deterministic,
        "cudnn_benchmark": torch.backends.cudnn.benchmark,
        "cuda_matmul_tf32": torch.backends.cuda.matmul.allow_tf32,
        "cudnn_tf32": torch.backends.cudnn.allow_tf32,
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
    }


def child(root, output, variant, voice_id):
    from evaluate_tts_memory import TEXTS, baseline

    assert_no_workers()
    report = {"variant": variant, "main_native_thread": threading.get_native_id(), "speech": []}

    def owner():
        # Torch/CUDA is first imported and used on this single owner thread.
        if "torch" in sys.modules:
            raise RuntimeError("Torch was imported before the controlled owner thread")
        import server
        import torch

        report["owner_native_thread"] = threading.get_native_id()
        voice = server.select_voice(root, voice_id)
        metadata = server.compact.validate_reference(root, voice_id=voice)
        signature = server.compact.reference_signature(root, voice_id=voice, metadata=metadata)
        report["voice"] = voice
        report["reference_metadata"] = metadata
        report["reference_signature"] = signature
        if variant == "baseline":
            pipeline = baseline(root, output)
            # Original reference computation, deliberately without save_reference:
            # never mutate the existing cache used as production evidence.
            server.compact._compute_reference(pipeline, metadata["identity"])
            pipeline.fixed_reference_metadata = metadata
            pipeline.fixed_reference_signature = signature
        else:
            pipeline = server.compact.create_pipeline(root, voice_id=voice, prepare_if_needed=False)
        engine = server.SpeechEngine(root, voice, pipeline)
        report["torch"] = {
            "version": torch.__version__,
            "cuda": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
        }
        report["backend_flags_before_warmup"] = backend_flags(torch)
        report["pipeline_type"] = type(pipeline).__qualname__
        report["warmup_request"] = production_warmup(root)
        warmup = engine.request(report["warmup_request"])
        report["warmup_params"] = warmup
        emit("warmup_start", variant=variant)
        data = engine.synthesize(warmup)
        torch.cuda.synchronize()
        (output / f"{variant}-warmup.wav").write_bytes(data)
        report["warmup_complete"] = True
        report["backend_flags_after_warmup"] = backend_flags(torch)
        for index, text in enumerate(TEXTS):
            for repeat in range(REPEATS):
                params = {
                    **DEFAULTS,
                    **engine.request({"text": text, "text_lang": "zh", "seed": 42, "batch_size": 1}),
                }
                emit("speech_start", variant=variant, index=index, repeat=repeat)
                started = time.monotonic()
                audio = engine.synthesize(params)
                torch.cuda.synchronize()
                path = output / f"{variant}-{index}-r{repeat}.wav"
                path.write_bytes(audio)
                shape, raw, _ = inspect_wav(path)
                digest = hashlib.sha256(raw).hexdigest()
                row = {
                    "index": index,
                    "repeat": repeat,
                    "params": params,
                    "native_thread": threading.get_native_id(),
                    "elapsed_seconds": time.monotonic() - started,
                    "wave_shape": shape,
                    "pcm_sha256": digest,
                }
                report["speech"].append(row)
                save(output / f"{variant}-result.json", report)
                emit("speech", variant=variant, **row)
        report["backend_flags_after_run"] = backend_flags(torch)
        source = server.compact._source()
        loaded_sources = {}
        for module in list(sys.modules.values()):
            raw = vars(module).get("__file__") if module is not None else None
            if isinstance(raw, str):
                path = Path(raw).resolve()
                if path.is_relative_to(source) and path.suffix == ".py" and path.is_file():
                    loaded_sources[str(path.relative_to(source))] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
        report["loaded_upstream_source_sha256"] = loaded_sources
        report["source_root"] = str(source)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="parity-owner") as executor:
        try:
            executor.submit(owner).result()
            report["complete"] = True
        except Exception as error:
            report["error"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            save(output / f"{variant}-result.json", report)


def preflight(root):
    import httpx

    assert_no_workers()
    with httpx.Client(trust_env=False, timeout=5) as client:
        response = client.get(
            "http://127.0.0.1:9020/api/status",
            headers={"Authorization": "Bearer " + (root / "runtime/admin-token").read_text().strip()},
        )
        response.raise_for_status()
        state = response.json()
        policy = state["comment_policy"]
        if (
            state["state"] != "stopped"
            or state["queue_length"]
            or state["current_job"]
            or policy["auto_browse"]
            or policy["auto_reply"]
        ):
            raise RuntimeError("Stop business/automatic jobs before evaluation")
        response = client.get(
            "http://127.0.0.1:9012/health",
            headers={"X-Tuber-Inference-Key": (root / "runtime/inference-token").read_text().strip()},
        )
        response.raise_for_status()
        health = response.json()
        if (
            health["mode"] != "text"
            or health["busy"]
            or any(value["running"] for value in health["providers"].values())
        ):
            raise RuntimeError("External owner must unload GPU workers and leave idle text mode")
    return {
        "business_stopped": True,
        "automatic_jobs_disabled": True,
        "gpu_workers_absent": True,
        "broker_mode": health["mode"],
    }


def cache_hashes(root):
    directory = root / "runtime/tts/reference_features"
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and p.suffix in {".npz", ".json"}
    }


def collect_comparisons(output, sentences, legacy_dir):
    rows = []
    for index in range(sentences):
        for variant in ("baseline", "compact"):
            rows.append(
                compare_files(
                    output / f"{variant}-{index}-r0.wav",
                    output / f"{variant}-{index}-r1.wav",
                    kind="self_repeat",
                    variant=variant,
                    index=index,
                )
            )
        for repeat in range(REPEATS):
            rows.append(
                compare_files(
                    output / f"baseline-{index}-r{repeat}.wav",
                    output / f"compact-{index}-r{repeat}.wav",
                    kind="same_order",
                    index=index,
                    repeat=repeat,
                )
            )
        if legacy_dir is not None:
            for variant in ("baseline", "compact"):
                rows.append(
                    compare_files(
                        output / f"{variant}-{index}-r0.wav",
                        legacy_dir / f"baseline-{index}.wav",
                        kind="legacy",
                        variant=variant,
                        index=index,
                        protocol_matched=False,
                    )
                )
    return rows


def protocol_matches(baseline_result, compact_result):
    """Require the same identity, normalized calls and owner-thread protocol."""
    for result in (baseline_result, compact_result):
        if not result.get("complete") or not result.get("warmup_complete"):
            return False
        speech = result.get("speech", [])
        if len(speech) != 6 or any(row["native_thread"] != result["owner_native_thread"] for row in speech):
            return False
    baseline_sources = baseline_result.get("loaded_upstream_source_sha256", {})
    compact_sources = compact_result.get("loaded_upstream_source_sha256", {})
    if (
        not baseline_sources
        or not compact_sources
        or any(
            baseline_sources[key] != compact_sources[key]
            for key in baseline_sources.keys() & compact_sources.keys()
        )
    ):
        return False
    return (
        baseline_result["reference_metadata"] == compact_result["reference_metadata"]
        and baseline_result["torch"] == compact_result["torch"]
        and baseline_result["warmup_params"] == compact_result["warmup_params"]
        and baseline_result["backend_flags_after_warmup"] == compact_result["backend_flags_after_warmup"]
        and [(row["index"], row["repeat"], row["params"]) for row in baseline_result["speech"]]
        == [(row["index"], row["repeat"], row["params"]) for row in compact_result["speech"]]
    )


async def stop_child(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 10)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def supervise(root, output, voice_id, legacy_dir):
    from evaluate_context import MemoryWatch
    from evaluate_tts_memory import TEXTS

    interrupted = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, interrupted.set)
    report = {
        "format": 1,
        "scope": "matched protocol PCM diagnostic; no quality tolerance or legacy golden change",
        "protocol": {
            "warmup": "production SpeechEngine.load request",
            "texts": TEXTS,
            "repeats": REPEATS,
            "seed": 42,
            "one_owner_thread_per_process": True,
            "fresh_process_per_variant": True,
        },
        "guards": {
            "deadline_per_variant_seconds": 180,
            "minimum_available_mib": 160,
            "maximum_swap_25s_mib": 64,
            "sample_period_seconds": 0.5,
        },
        "restoration_owner": "External caller must restore the unloaded LLM in finally",
        "legacy": {
            "directory": str(legacy_dir) if legacy_dir else None,
            "protocol_matched": False,
            "used_as_quality_gate": False,
            "scope": "Historical comparison only; call order/thread/warmup were not controlled here",
        },
        "variants": {},
    }
    before_config, before_cache = None, None
    try:
        report["preflight"] = preflight(root)
        before_config = (root / "runtime/studio.json").read_bytes()
        before_cache = cache_hashes(root)
        report["config_sha256"] = hashlib.sha256(before_config).hexdigest()
        report["reference_cache_sha256"] = before_cache
        files = [
            Path(__file__).resolve(),
            *[
                root / path
                for path in (
                    "tools/evaluate_tts_memory.py",
                    "tools/jetson/tts/server.py",
                    "tools/jetson/tts/compact.py",
                    "tools/jetson/tts/serve.py",
                    "tools/jetson/tts/inference_only.py",
                )
            ],
        ]
        report["tool_source_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        for variant in ("baseline", "compact"):
            preflight(root)
            watch = MemoryWatch(180)
            watch.stage = variant
            with (output / f"{variant}.log").open("wb") as log:
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--root",
                    str(root),
                    "--output",
                    str(output),
                    "--child",
                    variant,
                ]
                if voice_id is not None:
                    command += ["--voice", voice_id]
                process = await asyncio.create_subprocess_exec(
                    *command, stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True
                )
                monitor = asyncio.create_task(watch.run())
                waiting = asyncio.create_task(process.wait())
                interrupt = asyncio.create_task(interrupted.wait())
                try:
                    done, _ = await asyncio.wait(
                        (monitor, waiting, interrupt), return_when=asyncio.FIRST_COMPLETED
                    )
                    if interrupt in done:
                        raise RuntimeError("Operator signal received")
                    if monitor in done:
                        await monitor
                    if await waiting != 0:
                        raise RuntimeError(f"{variant} failed; see its log and partial result")
                    watch.sample()
                finally:
                    monitor.cancel()
                    interrupt.cancel()
                    await asyncio.gather(monitor, interrupt, return_exceptions=True)
                    await stop_child(process)
                    await waiting
                    report["variants"][variant] = {
                        "pid": process.pid,
                        "returncode": process.returncode,
                        "memory": watch.summary(),
                    }
                    result_path = output / f"{variant}-result.json"
                    if result_path.is_file():
                        report["variants"][variant]["result"] = json.loads(
                            result_path.read_text(encoding="utf-8")
                        )
                    save(output / "result.json", report)
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
    finally:
        report["comparisons"] = collect_comparisons(output, len(TEXTS), legacy_dir)
        report["production_config_unchanged"] = (
            before_config is not None and (root / "runtime/studio.json").read_bytes() == before_config
        )
        report["reference_cache_unchanged"] = before_cache is not None and cache_hashes(root) == before_cache
        variants = report["variants"]
        completed = all(
            variants.get(v, {}).get("returncode") == 0 and variants[v].get("result", {}).get("complete")
            for v in ("baseline", "compact")
        )
        report["protocol_matches"] = bool(
            completed and protocol_matches(variants["baseline"]["result"], variants["compact"]["result"])
        )
        report["same_order"] = same_order_summary(
            report["comparisons"],
            sentences=len(TEXTS),
            variants_complete=bool(
                completed
                and report["protocol_matches"]
                and "failure" not in report
                and report["production_config_unchanged"]
                and report["reference_cache_unchanged"]
            ),
        )
        save(output / "result.json", report)
        emit(
            "finished",
            result=str(output / "result.json"),
            failure=report.get("failure"),
            same_order_bitwise_passed=report["same_order"]["bitwise_passed"],
        )
    return 0 if report["same_order"]["bitwise_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(os.environ.get("TUBER_ROOT", Path(__file__).resolve().parents[1]))
    )
    parser.add_argument("--voice", help="Configured GPT-SoVITS voice; defaults to production selection")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-dir", type=Path, help="Optional historical baseline-N.wav directory")
    parser.add_argument("--child", choices=("baseline", "compact"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ["TUBER_ROOT"] = str(root)
    sys.path[:0] = [str(root / "tools/jetson/tts"), str(root / "tools"), str(root / "src")]
    output = (root / args.output).resolve()
    allowed = [root / p for p in ("runtime/tmp", "runtime/evaluations", "tmp")]
    if not any(output.is_relative_to(p) and output != p for p in allowed):
        parser.error("Output must be a new project runtime/tmp, runtime/evaluations or tmp subdirectory")
    legacy = (root / args.legacy_dir).resolve() if args.legacy_dir is not None else None
    if legacy is not None and not legacy.is_dir():
        parser.error("Legacy directory must already exist")
    if args.child:
        child(root, output, args.child, args.voice)
    else:
        output.mkdir(parents=True, exist_ok=False)
        raise SystemExit(asyncio.run(supervise(root, output, args.voice, legacy)))


if __name__ == "__main__":
    main()
