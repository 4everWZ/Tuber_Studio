"""Guarded live-residency endurance: real WAVs and real structured MiniCPM turns.

No database writes, platform requests, publishing, playback ACKs or mode changes.
The operator establishes live mode first. Default: an unpaced initial 100 speech
requests, then 60 seconds between completed cycles until two hours; every fifth cycle generates
the speech through the unchanged character/LLM provider. This is a defined mixed
workload, not a claim about every arrival rate or maximum GPU throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import psutil
from evaluate_context import MemoryWatch

from tuber_studio.context import ContextManager
from tuber_studio.providers import LlamaCppProvider, public_error, wav_duration

ROOT = Path(os.getenv("TUBER_ROOT", Path(__file__).resolve().parents[1])).resolve()


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def process_ids():
    result = {"llm": [], "tts": []}
    for process in psutil.process_iter(["name", "cmdline"]):
        if process.info["name"] == "llama-server":
            result["llm"].append(process.pid)
        elif any(arg.endswith("/tools/jetson/tts/server.py") for arg in process.info["cmdline"] or []):
            result["tts"].append(process.pid)
    return {key: sorted(value) for key, value in result.items()}


async def check_profile(client):
    response = await client.get("/health", timeout=10)
    response.raise_for_status()
    health = response.json()
    if (
        health["mode"] != "live"
        or health["selected_provider"] != "llm2b"
        or health["busy"]
        or health["error"]
        or not health["providers"]["tts"]["running"]
        or not health["providers"]["llm2b"]["running"]
    ):
        raise RuntimeError("Requires idle MiniCPM5-2B + formal compact TTS in live mode")
    response = await client.get("/llm/2b/props", timeout=10)
    response.raise_for_status()
    props = response.json()
    if props["default_generation_settings"]["n_ctx"] != 131072:
        raise RuntimeError("Actual context is not 131072")
    processes = process_ids()
    if len(processes["llm"]) != 1 or len(processes["tts"]) != 1:
        raise RuntimeError("Expected one formal TTS and one LLM process")
    return {"model": "MiniCPM5-2B", "context": 131072, "kv": health["kv_cache_type"], "pids": processes}


async def evaluate(args):
    config = json.loads((ROOT / "runtime/studio.json").read_text(encoding="utf-8"))
    if config["llm"]["model"] != "MiniCPM5-2B" or config["llm"]["context_tokens"] != 131072:
        raise ValueError("Keep the authorized MiniCPM5-2B/131072 profile")
    token = (ROOT / "runtime/inference-token").read_text().strip()
    admin = (ROOT / "runtime/admin-token").read_text().strip()
    cases = json.loads((ROOT / "evaluations/speech-cases.json").read_text(encoding="utf-8"))
    voices = json.loads((ROOT / "config/voices.json").read_text(encoding="utf-8"))
    voice = next(v for v in voices if v["id"] == config["tts"].get("voice_id", cases["voice_id"]))
    character = next(
        c
        for c in json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
        if c["voice_profile"] == voice["id"]
    )
    config["llm"]["seed"] = cases["seed"]
    provider = LlamaCppProvider(config["llm"], token)
    records, profile, error = [], None, None
    args.output.mkdir(parents=True, exist_ok=True)
    watch = MemoryWatch(args.seconds + 900)
    headers = {"X-Tuber-Inference-Key": token}
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:9012", headers=headers, trust_env=False
    ) as client:
        response = await client.get(
            "http://127.0.0.1:9020/api/status", headers={"Authorization": "Bearer " + admin}, timeout=10
        )
        response.raise_for_status()
        status = response.json()
        if (
            status["state"] != "stopped"
            or status["queue_length"]
            or status["current_job"]
            or status["comment_policy"]["auto_browse"]
            or status["comment_policy"]["auto_reply"]
        ):
            raise RuntimeError("Stop production and automatic comment jobs before endurance evaluation")
        profile = await check_profile(client)

        async def monitor():
            next_report = time.monotonic() + 30
            while True:
                watch.sample()
                if time.monotonic() >= next_report:
                    emit(
                        event="progress",
                        stage=watch.stage,
                        cycles=len(records),
                        elapsed_seconds=round(time.monotonic() - watch.started, 2),
                        memory=watch.latest,
                    )
                    next_report = time.monotonic() + 30
                await asyncio.sleep(0.5)

        async def workload():
            fixture_index = 0
            while time.monotonic() - watch.started < args.seconds or len(records) < args.minimum_speech:
                cycle = len(records) + 1
                watch.stage = "cycle_" + str(cycle)
                started = time.monotonic()
                row = {"cycle": cycle, "llm": None}
                text = None
                if cycle % 5 == 0:
                    question = cases["llm_prompts"][(cycle // 5 - 1) % len(cases["llm_prompts"])]
                    event = {"event_id": f"evaluation:endurance:{cycle}", "text": question}
                    prefix, initial, pending, suffix = ContextManager.parts(character, event, {}, "live")
                    messages = ContextManager.render(prefix, initial, [], pending, suffix)
                    llm_started = time.monotonic()
                    try:
                        reply = await provider.generate(messages, character, event["event_id"], question)
                        text = reply.spoken_text
                        row["llm"] = {
                            "schema_valid": True,
                            "input_tokens": provider.last_tokens,
                            "seconds": round(time.monotonic() - llm_started, 3),
                        }
                    except ValueError as failure:
                        # Preserve schema/role failures in the denominator; continue
                        # testing residency with a fixture, never call this success.
                        row["llm"] = {"schema_valid": False, "error": public_error(failure)}
                fixture = None
                if text is None:
                    fixture = fixture_index % len(cases["texts"])
                    text = cases["texts"][fixture]
                    fixture_index += 1
                speech_started = time.monotonic()
                response = await client.post(
                    "/tts/tts",
                    json={
                        "text": text,
                        "ref_audio_path": voice["server_reference_path"],
                        "prompt_text": voice["reference_text"],
                        "seed": cases["seed"],
                        "media_type": "wav",
                        "streaming_mode": False,
                        **voice["params"],
                    },
                    timeout=config["tts"].get("timeout_seconds", 90),
                )
                response.raise_for_status()
                path = args.output / (
                    f"fixture-{fixture}.wav" if fixture is not None and cycle <= 35 else "latest.wav"
                )
                path.write_bytes(response.content)
                duration = wav_duration(path, require_speech=True)
                row.update(
                    text=text,
                    fixture=fixture,
                    speech_seconds=round(time.monotonic() - speech_started, 3),
                    audio_seconds=duration,
                    bytes=len(response.content),
                    wav_sha256=hashlib.sha256(response.content).hexdigest(),
                    total_seconds=round(time.monotonic() - started, 3),
                )
                if process_ids() != profile["pids"]:
                    raise RuntimeError("Inference process changed; continuous-residency proof ended")
                records.append(row)
                with (args.output / "cycles.jsonl").open("a", encoding="utf-8") as output:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                if len(records) >= args.minimum_speech:
                    remaining = args.seconds - (time.monotonic() - watch.started)
                    if remaining > 0:
                        await asyncio.sleep(min(args.interval, remaining))
            return await check_profile(client)

        work = asyncio.create_task(workload())
        guard = asyncio.create_task(monitor())
        final_profile = None
        try:
            done, _ = await asyncio.wait({work, guard}, return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                await guard
            final_profile = await work
            watch.sample()
        except (Exception, asyncio.CancelledError) as failure:
            error = public_error(failure) or "Evaluation cancelled"
        finally:
            work.cancel()
            guard.cancel()
            await asyncio.gather(work, guard, return_exceptions=True)
            await provider.close()
        invalid = sum(row["llm"] is not None and not row["llm"]["schema_valid"] for row in records)
        elapsed = time.monotonic() - watch.started
        total_ram_kib = psutil.virtual_memory().total // 1024
        report = {
            "profile_before": profile,
            "profile_after": final_profile,
            "requested_seconds": args.seconds,
            "minimum_speech": args.minimum_speech,
            "elapsed_seconds": round(elapsed, 3),
            "speech_completed": len(records),
            "last_stage": watch.stage,
            "llm_calls": sum(row["llm"] is not None for row in records),
            "invalid_llm_replies": invalid,
            "post_burst_interval_seconds": args.interval,
            "memory": watch.summary(),
            "total_ram_kib": total_ram_kib,
            "peak_machine_total_minus_available_kib": total_ram_kib - watch.minimum_available_kib,
            "completed_duration_and_count": error is None and len(records) >= args.minimum_speech,
            "all_llm_reply_schemas_valid": invalid == 0,
            "verified_two_hours_and_100": error is None and elapsed >= 7200 and len(records) >= 100,
            "reason": error,
            "limitations": "Defined synthetic mixed workload; no platform publishing, playback or arbitrary arrival-rate claim. Audio non-silence and valid structured replies do not replace human voice-quality listening.",
        }
        (args.output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        emit(event="result", result=report)
        return report["completed_duration_and_count"] and invalid == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--minimum-speech", type=int, default=100)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if (
        args.seconds <= 0
        or args.minimum_speech <= 0
        or args.interval < 0
        or not args.output.is_relative_to(ROOT / "runtime")
    ):
        parser.error("Positive duration/count and an output inside project runtime are required")
    return 0 if asyncio.run(evaluate(args)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
