"""CPU-only cache metadata benchmark, with fresh isolated SQLite/runtime per size.

Run with the project environment:
  uv run python evaluations/media_cache_benchmark.py --output-dir tmp/cache-benchmark

Uses tiny valid WAVs, not audio decoding. No Runtime.open, network, Node or model
process. Timing, tracemalloc, SQLite tracing and cProfile are separate passes.
Owned fixture directories (including generated management tokens) are removed;
results and profiles remain in the newly created output directory.
"""

import argparse
import asyncio
import cProfile
import gc
import hashlib
import io
import json
import os
import platform
import pstats
import shutil
import statistics
import struct
import subprocess
import sys
import time
import tracemalloc
import wave
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import psutil

from tuber_studio.config import ROOT, Settings
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store

HERE = None
REPEATS = 25
WARMUPS = 3


def trace_sql(fn, store):
    counts = Counter()

    def trace(statement):
        normalized = " ".join(statement.lower().split())
        counts["total"] += 1
        if "from media_cache_access" in normalized:
            counts["access_full_select"] += 1
        elif normalized.startswith("select value from kv"):
            counts["kv_select"] += 1
        elif normalized.startswith("pragma"):
            counts["pragma"] += 1
        else:
            counts["other"] += 1

    store.db.set_trace_callback(trace)
    try:
        fn()
    finally:
        store.db.set_trace_callback(None)
    return dict(counts)


def profile(fn, destination):
    profiler = cProfile.Profile()
    profiler.runcall(fn)
    profiler.dump_stats(str(destination))
    statistics_ = pstats.Stats(profiler)
    rows = []
    for key, value in statistics_.stats.items():
        primitive, calls, own, cumulative, _callers = value
        rows.append(
            {
                "function": f"{Path(key[0]).name}:{key[1]}:{key[2]}",
                "calls": calls,
                "primitive_calls": primitive,
                "self_ms": own * 1000,
                "cumulative_ms": cumulative * 1000,
            }
        )
    return {
        "self": sorted(rows, key=lambda row: row["self_ms"], reverse=True)[:8],
        "cumulative": sorted(rows, key=lambda row: row["cumulative_ms"], reverse=True)[:8],
    }


async def child(count):
    # Never consult an operator's external config or management-token override.
    for key in ("TUBER_CONFIG", "TUBER_RUNTIME", "TUBER_ADMIN_TOKEN"):
        os.environ.pop(key, None)
    directory = HERE / f"runtime-{count}"
    if directory.exists():
        raise RuntimeError("Benchmark runtime must be newly created")
    store = runtime = None
    try:
        settings = Settings(runtime=directory)
        settings.data["llm"]["kind"] = "demo"
        settings.data["tts"]["kind"] = "prerecorded"
        settings.data["inference_broker"]["enabled"] = False
        settings.data["vision"] = {"kind": "disabled"}
        settings.data["features"]["live_enabled"] = False
        seeds = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
        store = Store(directory / "benchmark.sqlite3", seeds)
        runtime = Runtime(settings, store)
        rows = []
        for number in range(count):
            content = io.BytesIO()
            with wave.open(content, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(struct.pack("<h", number))
            data = content.getvalue()
            filename = hashlib.sha256(data).hexdigest() + ".wav"
            path = runtime.online_songs.directory / filename
            path.write_bytes(data)
            rows.append((str(path.relative_to(directory)), float(number)))
        with store.db:
            store.db.executemany("INSERT INTO media_cache_access VALUES(?,?)", rows)
        del rows

        def poll_pair():
            return runtime.cache.status(), runtime.online_songs.status()

        methods = {
            "MediaCache._files(music)": lambda: runtime.cache._files("music"),
            "MediaCache.status": runtime.cache.status,
            "OnlineSongs.status": runtime.online_songs.status,
            "cache+online_back_to_back": poll_pair,
        }
        measurements = {}
        for ordinal, (name, fn) in enumerate(methods.items()):
            for _ in range(WARMUPS):
                fn()
            elapsed = []
            gc.collect()
            for _ in range(REPEATS):
                start = time.perf_counter_ns()
                result = fn()
                elapsed.append((time.perf_counter_ns() - start) / 1_000_000)
                del result
            gc.collect()
            memory_runs = []
            process = psutil.Process()
            before_rss = process.memory_info().rss
            for _ in range(3):
                tracemalloc.start()
                result = fn()
                current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                memory_runs.append({"current_bytes": current, "peak_bytes": peak})
                del result
            measurements[name] = {
                "p50_ms": statistics.median(elapsed),
                "max_ms": max(elapsed),
                "min_ms": min(elapsed),
                "warm_samples_ms": elapsed,
                "python_peak_bytes": max(row["peak_bytes"] for row in memory_runs),
                "memory_runs": memory_runs,
                "rss_before_memory_pass_bytes": before_rss,
                "rss_after_memory_pass_bytes": process.memory_info().rss,
                "sql": trace_sql(fn, store),
                "profile": profile(fn, HERE / f"profile-{count}-{ordinal}.prof"),
            }
        payload = {
            "files": count,
            "access_rows": count,
            "file_bytes_each": 46,
            "measurements": measurements,
            "component_status": runtime.online_songs.component.status(),
            "source_root": str(ROOT),
            "process_peak_rss_bytes": getattr(psutil.Process().memory_info(), "peak_wset", None),
            "fixture": "All files in online_music; music catalog, queue, tasks and other cache pools empty",
        }
        (HERE / f"measurements-{count}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    finally:
        # Runtime.open was never called: no worker, inference, Node process or HTTP.
        try:
            if runtime is not None:
                await runtime.close()
        finally:
            if store is not None:
                store.close()
            # No existing directory is reused above. Verify ownership before the
            # recursive cleanup on both Windows and POSIX; do not follow a junction.
            if (
                directory.resolve().parent != HERE.resolve()
                or directory.is_symlink()
                or getattr(directory, "is_junction", lambda: False)()
                or directory.name != f"runtime-{count}"
            ):
                raise ValueError("Unexpected benchmark fixture cleanup target")
            if directory.exists():
                shutil.rmtree(directory)


def parent(counts):
    HERE.mkdir(parents=True, exist_ok=False)
    results = []
    for count in counts:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                str(count),
                "--output-dir",
                str(HERE),
                "--samples",
                str(REPEATS),
                "--warmups",
                str(WARMUPS),
            ],
            check=True,
        )
        results.append(json.loads((HERE / f"measurements-{count}.json").read_text(encoding="utf-8")))
        print(f"completed {count} files", flush=True)
    host = {
        "time_utc": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "cpu": platform.processor(),
        "logical_cpus": psutil.cpu_count(),
        "memory_bytes": psutil.virtual_memory().total,
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "cache_source_blob": subprocess.check_output(
            ["git", "hash-object", "src/tuber_studio/media_cache.py"], cwd=ROOT, text=True
        ).strip(),
        "source_root": str(ROOT),
        "samples": REPEATS,
        "warmups": WARMUPS,
        "storage": [
            {"device": part.device, "mountpoint": part.mountpoint, "fstype": part.fstype}
            for part in psutil.disk_partitions()
            if str(ROOT).lower().startswith(part.mountpoint.lower())
        ],
    }
    (HERE / "results.json").write_text(
        json.dumps({"host": host, "results": results}, indent=2), encoding="utf-8"
    )
    print("results saved in " + str(HERE), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "tmp" / ("media-cache-benchmark-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")),
    )
    parser.add_argument("--counts", nargs="+", type=int, default=[100, 1000, 5000])
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--child", type=int)
    args = parser.parse_args()
    HERE, REPEATS, WARMUPS = args.output_dir.resolve(), args.samples, args.warmups
    counts = args.counts + ([args.child] if args.child is not None else [])
    if REPEATS < 1 or WARMUPS < 0 or any(not 1 <= count <= 32768 for count in counts):
        parser.error("Counts must be 1..32768; samples positive; warmups nonnegative")
    if len(set(args.counts)) != len(args.counts):
        parser.error("Counts must be unique")
    if args.child:
        asyncio.run(child(args.child))
    else:
        parent(args.counts)
