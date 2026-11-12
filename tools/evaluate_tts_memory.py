"""Compare fixed-reference v2 inference in fresh processes on the Jetson.

Run inside the project's CUDA container with live production stopped. Coexistence
measurements use the locally configured and selected broker model and window.
Whole-machine MemAvailable and swap traffic are sampled during loading AND
synthesis; CUDA tensor sizes alone are not a claim about shared physical RAM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import psutil
from evaluate_context import MemoryWatch, ProbeCancelled

from tuber_studio.inference_profiles import LLM_MODELS, llm_resource_key

PREFIX = "TUBER_TTS_EVAL "
TEXTS = (
    "海底厨房开门啦，今天的饭团要趁热吃。",
    "今晚的小灯笼亮起来了，我们一起逛逛海底夜市吧。",
    "收到三份海苔饭团啦，谢谢小壳！",
)


def emit(**fields):
    print(PREFIX + json.dumps(fields, ensure_ascii=False), flush=True)


def require_idle_business(client, root):
    admin = {"Authorization": "Bearer " + (root / "runtime/admin-token").read_text().strip()}
    response = client.get("http://127.0.0.1:9020/api/status", headers=admin)
    response.raise_for_status()
    status = response.json()
    response = client.get("http://127.0.0.1:9020/api/automation", headers=admin)
    response.raise_for_status()
    automation = response.json()
    if (
        status["state"] != "stopped"
        or status["queue_length"]
        or status["current_job"]
        or status["comment_policy"]["auto_reply"]
        or automation["policy"]["enabled"]
        or automation["active_rounds"]
        or automation["state"] in {"starting", "draining", "stopping"}
    ):
        raise RuntimeError("Stop production and disable automatic comment jobs before evaluation")


def inference_client(root):
    headers = {"X-Tuber-Inference-Key": (root / "runtime/inference-token").read_text().strip()}
    return httpx.Client(base_url="http://127.0.0.1:9012", headers=headers, trust_env=False, timeout=5)


def profile(root, without_llm=False):
    config = json.loads((root / "runtime/studio.json").read_text(encoding="utf-8"))
    key = llm_resource_key(config["llm"]["model"])
    if config["llm"]["kind"] != "llama_cpp" or key not in LLM_MODELS:
        raise RuntimeError("Select a locally installed broker language model before evaluation")
    model, context = LLM_MODELS[key], config["llm"]["context_tokens"]
    with httpx.Client(trust_env=False, timeout=5) as business:
        require_idle_business(business, root)
    with inference_client(root) as client:
        response = client.get("/health")
        response.raise_for_status()
        health = response.json()
        expected = set() if without_llm else {key}
        running = {name for name, info in health["providers"].items() if info.get("running")}
        if (
            health["selected_provider"] != key
            or health["mode"] != "text"
            or health["busy"]
            or health.get("error")
            or health["requested_context_tokens"] != context
            or running != expected
        ):
            raise RuntimeError("Idle broker state does not match the configured model, window or residency")
        actual_context = None
        if not without_llm:
            response = client.get("/llm/props")
            response.raise_for_status()
            props = response.json()
            aliases = props.get("model_alias")
            aliases = [aliases] if isinstance(aliases, str) else aliases
            actual_context = props["default_generation_settings"]["n_ctx"]
            if model not in (aliases or []) or actual_context != context or props.get("is_sleeping"):
                raise RuntimeError("Loaded model or window does not match the configured profile")
    return {
        "model": model,
        "resident_llm": not without_llm,
        "configured_context": context,
        "context_when_resident": actual_context,
        "kv": health["kv_cache_type"],
        "llm_pids": sorted(p.pid for p in psutil.process_iter(["name"]) if p.info["name"] == "llama-server"),
    }


def warm_configured_llm(root):
    selected = profile(root, without_llm=True)
    with inference_client(root) as client:
        response = client.post("/llm/warmup", json={"model": selected["model"]}, timeout=180)
        response.raise_for_status()
    return profile(root)


def baseline(root, output):
    from jetson.tts import serve

    source = Path(os.environ["TUBER_GPT_SOVITS_ROOT"])
    serve.check_source(source)
    os.chdir(source)
    sys.path[:0] = [str(source), str(source / "GPT_SoVITS")]
    sys.meta_path.insert(0, serve.CompatibilityFinder())
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

    configuration = TTS_Config(str(root / "runtime/tts/tts_infer.yaml"))
    configuration.configs_path = str(output / "scratch-tts.yaml")
    return TTS(configuration)


def child(args):
    import numpy as np
    import torch
    from jetson.tts import compact

    voice = next(
        v for v in json.loads((args.root / "config/voices.json").read_text()) if v["id"] == args.voice
    )
    emit(stage="loading", variant=args.child)
    started = time.monotonic()
    pipeline = (
        baseline(args.root, args.output)
        if args.child == "baseline"
        else compact.create_pipeline(args.root, voice_id=args.voice)
    )
    torch.cuda.synchronize()
    emit(stage="loaded", seconds=round(time.monotonic() - started, 3))
    modules = {}
    for name in ("t2s_model", "vits_model", "bert_model", "cnhuhbert_model"):
        model = getattr(pipeline, name, None)
        modules[name] = sum(t.numel() * t.element_size() for t in model.parameters()) if model else 0
    emit(stage="weights", parameter_bytes=modules)
    emit(stage="ar_storage", result=compact.ar_storage_report(pipeline))
    if args.load_order == "tts-first":
        emit(stage="start_llm_load")
        started = time.monotonic()
        loaded = warm_configured_llm(args.root)
        emit(stage="llm_loaded", seconds=round(time.monotonic() - started, 3), profile=loaded)
    if args.child == "baseline":
        emit(stage="start_reference_precompute")
        started = time.monotonic()
        reference = compact.compute_reference(args.root, pipeline, voice_id=args.voice)
        torch.cuda.synchronize()
        emit(stage="reference_saved", seconds=round(time.monotonic() - started, 3), result=reference)
    for index, text in enumerate(TEXTS):
        emit(stage="start_speech", index=index)
        started = time.monotonic()
        inputs = {
            "text": text,
            "ref_audio_path": voice["server_reference_path"],
            "prompt_text": voice["reference_text"],
            "prompt_lang": voice["params"]["prompt_lang"],
            "seed": 42,
            "batch_size": 1,
            **voice["params"],
        }
        torch.cuda.reset_peak_memory_stats()
        chunks = list(pipeline.run(inputs))
        torch.cuda.synchronize()
        elapsed = time.monotonic() - started
        if not chunks or len({rate for rate, _ in chunks}) != 1:
            raise RuntimeError("Missing or inconsistent PCM chunks")
        rate = chunks[0][0]
        audio = np.concatenate([chunk for _, chunk in chunks])
        if audio.dtype != np.int16 or audio.ndim != 1 or not audio.any():
            raise RuntimeError("Invalid or silent synthesis")
        path = args.output / f"{args.child}-{index}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(audio.tobytes())
        emit(
            stage="speech",
            index=index,
            text=text,
            seconds=round(elapsed, 3),
            duration_seconds=len(audio) / rate,
            sample_rate=rate,
            frames=len(audio),
            pcm_sha256=hashlib.sha256(audio.tobytes()).hexdigest(),
            rms_pcm16=float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))),
            cuda_peak_allocated_bytes=torch.cuda.max_memory_allocated(),
            cuda_peak_reserved_bytes=torch.cuda.max_memory_reserved(),
        )
        if args.exercise_llm and index < len(TEXTS) - 1:
            model = profile(args.root)["model"]
            emit(stage="start_llm", after_speech=index)
            started = time.monotonic()
            with inference_client(args.root) as client:
                response = client.post(
                    "/llm/completion",
                    json={
                        "model": model,
                        "prompt": "用户：你好。\n助手：",
                        "n_predict": 4,
                        "ignore_eos": True,
                        "cache_prompt": False,
                        "seed": 42,
                    },
                    timeout=40,
                )
                response.raise_for_status()
            completion = response.json()
            if completion.get("tokens_predicted") != 4 or completion.get("truncated") is not False:
                raise RuntimeError("Interleaved LLM request did not complete four actual tokens")
            emit(
                stage="llm",
                after_speech=index,
                seconds=round(time.monotonic() - started, 3),
                input_tokens=completion.get("tokens_evaluated"),
                output_tokens=4,
                truncated=False,
            )
    emit(stage="completed", variant=args.child)


def measured(args, variant):
    from tuber_studio.inference import ProcessRunner

    # Same scoped cache advice as production. Never reclaim global system cache.
    ProcessRunner(args.root)._advise_owned_model_cache()
    watch = MemoryWatch(args.deadline)
    watch.stage = variant
    log = args.output / f"{variant}.log"
    peak_rss = 0
    phases = {}
    stage = "loading"
    offset = 0
    reason = None
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--root",
        str(args.root),
        "--output",
        str(args.output),
        "--voice",
        args.voice,
        "--child",
        variant,
        "--load-order",
        args.load_order,
        "--variant",
        variant,
    ]
    if args.exercise_llm:
        command.append("--exercise-llm")
    with log.open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            next_progress = time.monotonic() + 5
            while process.poll() is None:
                watch.sample()
                with log.open("rb") as progress:
                    progress.seek(offset)
                    while line := progress.readline():
                        if not line.endswith(b"\n"):
                            break
                        offset = progress.tell()
                        if line.startswith(PREFIX.encode()):
                            event = json.loads(line[len(PREFIX) :].decode())
                            if event["stage"] == "start_speech":
                                stage = "speech_" + str(event["index"])
                            elif event["stage"] == "start_reference_precompute":
                                stage = "reference_precompute"
                            elif event["stage"] == "start_llm":
                                stage = "llm_after_" + str(event["after_speech"])
                            elif event["stage"] == "start_llm_load":
                                stage = "llm_load_with_tts_resident"
                            elif event["stage"] == "loaded":
                                stage = "loaded"
                phase = phases.setdefault(stage, {"minimum_available_kib": watch.latest["available_kib"]})
                phase["minimum_available_kib"] = min(
                    phase["minimum_available_kib"], watch.latest["available_kib"]
                )
                try:
                    peak_rss = max(peak_rss, psutil.Process(process.pid).memory_info().rss)
                except psutil.NoSuchProcess:
                    pass
                if time.monotonic() >= next_progress:
                    emit(
                        stage=variant,
                        elapsed_seconds=round(time.monotonic() - watch.started, 2),
                        memory=watch.latest,
                    )
                    next_progress = time.monotonic() + 5
                time.sleep(0.5)
            watch.sample()
        except (ProbeCancelled, KeyboardInterrupt) as error:
            reason = str(error) or "Interrupted"
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
    events = [
        json.loads(line[len(PREFIX) :]) for line in log.read_text().splitlines() if line.startswith(PREFIX)
    ]
    memtotal_kib = psutil.virtual_memory().total // 1024
    return {
        "variant": variant,
        "completed": reason is None
        and process.returncode == 0
        and any(e.get("stage") == "completed" for e in events),
        "returncode": process.returncode,
        "guard_reason": reason,
        "memory": watch.summary(),
        "total_ram_kib": memtotal_kib,
        "peak_machine_total_minus_available_kib": memtotal_kib - watch.minimum_available_kib,
        "sampled_peak_child_rss_bytes": peak_rss,
        "sampled_phases": phases,
        "events": events,
        "log_tail_on_failure": log.read_text()[-3500:] if process.returncode else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("TUBER_ROOT", ".")))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", default="whale_sovits_v2")
    parser.add_argument("--deadline", type=int, default=180)
    parser.add_argument("--child", choices=("baseline", "compact"), help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=("both", "baseline", "compact"), default="both")
    parser.add_argument(
        "--load-order",
        choices=("llm-first", "tts-first", "isolated"),
        default="llm-first",
        help="tts-first and isolated require the project LLM already stopped; isolated is not coexistence evidence",
    )
    parser.add_argument(
        "--exercise-llm",
        action="store_true",
        help="Four-token LLM execution between speech requests; not quality or long-input evaluation",
    )
    args = parser.parse_args()
    args.root, args.output = args.root.resolve(), args.output.resolve()
    if args.exercise_llm and args.load_order == "isolated":
        parser.error("Cannot exercise a nonresident LLM")
    if args.load_order == "tts-first" and args.variant == "both":
        parser.error("tts-first measures one variant per controlled LLM restart; select --variant")
    if not args.output.is_relative_to(args.root / "runtime"):
        parser.error("Output must stay in project runtime (contains private reference artifacts)")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.child:
        child(args)
        return 0
    before = profile(args.root, args.load_order != "llm-first")
    variants = ("baseline", "compact") if args.variant == "both" else (args.variant,)
    results = []
    for variant in variants:
        if profile(args.root, args.load_order != "llm-first") != before:
            raise RuntimeError("Production profile/process changed during experiment")
        result = measured(args, variant)
        results.append(result)
        (args.output / f"{variant}-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        emit(
            stage="variant_result",
            variant=variant,
            completed=result["completed"],
            guard_reason=result["guard_reason"],
            memory=result["memory"],
        )
        if not result["completed"]:
            break
    try:
        after = profile(args.root, args.load_order == "isolated")
    except (RuntimeError, httpx.HTTPError) as error:
        # Preserve failed loading evidence even when no live LLM was reached.
        after = {"verification_error": str(error)}
    if "verification_error" not in after and any(
        before[key] != after[key] for key in ("model", "configured_context", "kv")
    ):
        after["verification_error"] = "Broker model, window or K/V changed during evaluation"
    speeches = [[e for e in r["events"] if e["stage"] == "speech"] for r in results]
    equal = None
    if len(speeches) == 2:
        equal = len(speeches[0]) == len(speeches[1]) == len(TEXTS) and all(
            a["pcm_sha256"] == b["pcm_sha256"] for a, b in zip(*speeches)
        )
    result = {
        "profile_before": before,
        "profile_after": after,
        "load_order": args.load_order,
        "variants": results,
        "bit_identical_fixed_seed_pcm": equal,
        "interleaved_llm_execution": args.exercise_llm,
        "sampling_seconds": 0.5,
        "limitations": "LLM residency and optional four-token interleaving are explicit. Isolated precomputation/PCM results are not coexistence evidence. No simultaneous generation, long input, listening assessment or sustained-live claim. Whole-machine peak uses sampled total minus available, not weight sizes or exact instantaneous hardware high-water mark.",
    }
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    successful = all(r["completed"] for r in results) and "verification_error" not in after
    emit(
        stage="result",
        output=str(args.output / "result.json"),
        bit_identical_fixed_seed_pcm=equal,
        interleaved_llm_execution=args.exercise_llm,
        completed=successful,
    )
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
