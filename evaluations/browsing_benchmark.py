"""Reproducible CPU/network evidence preparation benchmark, never platform writes.

Run inside the project environment, for example:
  python evaluations/browsing_benchmark.py --root . --output tmp/preparation.json

Each case is a separate process with an isolated SQLite/cache directory. Source
responses and latency are simulated by httpx.MockTransport; application parsing,
request pacing, single-flight, persistent cache, cancellation and offline lock
admission are the real implementations. No model is opened or invoked.
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil

from tuber_studio.bilibili_browsing import BrowsingAdapter
from tuber_studio.browsing import BrowsingService, read_round
from tuber_studio.browsing_evidence import BrowsingEvidence
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.browsing_store import BrowsingRepository
from tuber_studio.comments import Comments
from tuber_studio.config import Settings
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.dynamics import Dynamics
from tuber_studio.media_cache import MediaCache
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store

VIDEOS = [f"BV1{number:09d}" for number in range(1, 5)]
BODY = [
    {"from": 0, "to": 5, "content": "准备两杯相同温度的清水。"},
    {"from": 5, "to": 12, "content": "其中一杯加入食盐，另一杯不加，分别放入鸡蛋。"},
    {"from": 12, "to": 20, "content": "加盐的一杯中，鸡蛋浮了起来。"},
]


class ControlledSource:
    def __init__(self, latency, *, restricted=False, blocked=False):
        self.latency, self.restricted, self.blocked = latency, restricted, blocked
        self.paths = Counter()
        self.starts = []
        self.active = self.peak_active = self.rate_limits = self.cancelled_requests = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, request):
        if request.method != "GET":
            raise AssertionError("The benchmark must not submit model or platform writes")
        self.paths[request.url.path] += 1
        self.starts.append(time.perf_counter())
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.entered.set()
        try:
            if self.blocked:
                await self.release.wait()
            await asyncio.sleep(self.latency)
            if self.restricted:
                self.rate_limits += 1
                return httpx.Response(429)
            path, query = request.url.path, request.url.params
            if path == "/x/web-interface/view":
                video = query["bvid"]
                number = VIDEOS.index(video) + 1
                data = {
                    "bvid": video,
                    "aid": 100 + number,
                    "title": f"受控盐水实验 {number}",
                    "desc": "比较有盐与无盐清水中鸡蛋的浮沉，只有字幕中展示的观察结果。",
                    "owner": {"mid": 200, "name": "受控样本作者"},
                    "duration": 20,
                    "pages": [{"cid": 300 + number, "part": "完整实验", "duration": 20}],
                    "pubdate": 1700000000,
                    "tid": 201,
                }
            elif path == "/x/web-interface/view/detail/tag":
                data = [{"tag_name": "受控实验"}]
            elif path == "/x/web-interface/nav":
                data = {
                    "mid": 900,
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png",
                    },
                }
            elif path == "/x/player/wbi/v2":
                cid = query["cid"]
                data = {
                    "cid": int(cid),
                    "subtitle": {
                        "subtitles": [
                            {
                                "id": int(cid),
                                "lan": "zh-CN",
                                "type": 0,
                                "subtitle_url": f"https://aisubtitle.hdslb.com/{cid}.json",
                            }
                        ]
                    },
                }
            elif request.url.host == "aisubtitle.hdslb.com":
                return httpx.Response(200, json={"body": BODY})
            else:
                raise AssertionError(f"Unexpected benchmark endpoint: {path}")
            return httpx.Response(200, json={"code": 0, "data": data})
        except asyncio.CancelledError:
            self.cancelled_requests += 1
            raise
        finally:
            self.active -= 1


class ObservedBrowsingService(BrowsingService):
    async def _gate(self):
        started = time.perf_counter()
        await super()._gate()
        self.gate_waits.append(time.perf_counter() - started)


class Fixture:
    def __init__(self, root, directory, latency, spacing, *, restricted=False, blocked=False):
        # Explicitly override only this child process's config location. Never
        # discover or write the production TUBER_CONFIG or its credentials.
        os.environ["TUBER_CONFIG"] = str(directory / "studio.json")
        self.settings = Settings(root=root, runtime=directory)
        self.settings.data["llm"]["kind"] = "demo"
        self.settings.data["tts"]["kind"] = "prerecorded"
        self.settings.data["inference_broker"]["enabled"] = False
        self.settings.data["features"]["live_enabled"] = False
        seeds = json.loads((root / "config/characters.json").read_text(encoding="utf-8"))
        self.store = Store(directory / "benchmark.sqlite3", seeds)
        self.runtime = Runtime(self.settings, self.store)
        self.source = ControlledSource(latency, restricted=restricted, blocked=blocked)
        self.adapter = BrowsingAdapter()
        self.comments = Comments(self.settings, self.store, self.runtime, self.adapter)
        self.messages = DirectMessages(self.settings, self.store, self.runtime, self.comments)
        self.dynamics = Dynamics(self.settings, self.store, self.runtime, self.comments)
        self.service = ObservedBrowsingService(
            self.settings,
            self.store,
            self.runtime,
            self.comments,
            self.messages,
            self.dynamics,
            read_interval=spacing,
        )
        self.service.gate_waits = []
        self.config = BrowsingPolicy().model_dump()

    async def open(self):
        await self.adapter.client.aclose()
        self.adapter.client = httpx.AsyncClient(
            base_url="https://api.bilibili.com",
            timeout=10,
            transport=httpx.MockTransport(self.source.respond),
        )

    async def close(self):
        await self.service.evidence.close()
        await self.messages.close()
        await self.comments.close()
        await self.runtime.close()
        self.store.close()

    def round(self):
        row, _ = self.runtime.browsing_repository.create_round(
            "benchmark", self.runtime.character, self.config, 0, True
        )
        return row


def distribution(values):
    return {
        "count": len(values),
        "total_seconds": sum(values),
        "mean_seconds": statistics.mean(values) if values else 0,
        "max_seconds": max(values, default=0),
    }


def kernel_peak_rss():
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except ImportError:
        return getattr(psutil.Process().memory_info(), "peak_wset", None)


async def measure(fixture, videos, workers):
    row, process = fixture.round(), psutil.Process()
    queue_waits, durations, outputs, samples = [], [], [], []
    before_reads, before_hits, before_merged = (
        len(fixture.source.starts),
        fixture.service.evidence.hits,
        fixture.service.evidence.merged,
    )
    before_gates = len(fixture.service.gate_waits)
    fixture.source.peak_active = fixture.source.active
    baseline_rss = process.memory_info().rss
    system_before = psutil.virtual_memory().available
    semaphore, finished = asyncio.Semaphore(workers), asyncio.Event()

    async def sample():
        while not finished.is_set():
            samples.append(process.memory_info().rss)
            try:
                await asyncio.wait_for(finished.wait(), 0.01)
            except TimeoutError:
                pass

    async def prepare(video):
        submitted = time.perf_counter()
        async with semaphore:
            started = time.perf_counter()
            queue_waits.append(started - submitted)
            value = await fixture.service.evidence.prepare(video, fixture.config)
            durations.append(time.perf_counter() - started)
            outputs.append((video, value))

    token, started = read_round.set(row["id"]), time.perf_counter()
    sampler = asyncio.create_task(sample())
    try:
        await asyncio.gather(*(prepare(video) for video in videos))
        elapsed = time.perf_counter() - started
    finally:
        finished.set()
        await sampler
        read_round.reset(token)
    samples.append(process.memory_info().rss)
    budget = fixture.runtime.browsing_repository.round(row["id"])["budget"]
    fixture.runtime.browsing_repository.finish_round(row["id"], "completed")
    starts = fixture.source.starts[before_reads:]
    result = {
        "workers": workers,
        "videos": len(videos),
        "wall_seconds": elapsed,
        "videos_per_second": len(videos) / elapsed,
        "http_reads": len(starts),
        "database_budget": budget,
        "cache_hits": fixture.service.evidence.hits - before_hits,
        "singleflight_merges": fixture.service.evidence.merged - before_merged,
        "queue_wait": distribution(queue_waits),
        "preparation_latency": distribution(durations),
        "request_gate_wait": distribution(fixture.service.gate_waits[before_gates:]),
        "request_min_start_spacing_seconds": min((b - a for a, b in zip(starts, starts[1:])), default=None),
        "http_rate_limit_responses": fixture.source.rate_limits,
        "observed_peak_inflight_http": fixture.source.peak_active,
        "rss_baseline_bytes": baseline_rss,
        "sampled_peak_rss_bytes": max(samples),
        "kernel_process_peak_rss_bytes": kernel_peak_rss(),
        "rss_sample_period_seconds": 0.01,
        "system_available_before_bytes": system_before,
        "system_available_after_bytes": psutil.virtual_memory().available,
        "source_versions": {video: material["source_version"] for video, (_, material) in outputs},
        "material_id_count": len({identity for _, (identity, _) in outputs}),
        "cache": fixture.runtime.cache.usage("browsing"),
    }
    assert not fixture.store.all("SELECT * FROM account_writes")
    assert budget["model_calls"] == 0 and budget["input_tokens"] == 0 and budget["output_tokens"] == 0
    return result


async def auxiliary(fixture, case):
    if case == "singleflight":
        result = await measure(fixture, [VIDEOS[0]] * 4, 4)
        assert (
            result["http_reads"] == 6
            and result["singleflight_merges"] == 6
            and result["material_id_count"] == 1
        )
        return result
    if case == "restriction":
        row = fixture.round()
        token = read_round.set(row["id"])
        try:
            await fixture.service.evidence.prepare(VIDEOS[0], fixture.config)
            raise AssertionError("429 must fail, not become empty evidence")
        except ValueError as error:
            assert getattr(error, "kind", None) == "restricted"
            return {
                "error_kind": error.kind,
                "http_reads": len(fixture.source.starts),
                "rate_limit_responses": fixture.source.rate_limits,
                "material_count": fixture.store.one("SELECT COUNT(*) AS n FROM browsing_materials")["n"],
            }
        finally:
            read_round.reset(token)
    if case == "cancellation":
        row = fixture.round()
        token = read_round.set(row["id"])
        children = [
            asyncio.create_task(fixture.service.evidence.prepare(video, fixture.config)) for video in VIDEOS
        ]
        read_round.reset(token)
        try:
            await asyncio.wait_for(fixture.source.entered.wait(), 2)
            fixture.runtime.browsing_repository.finish_round(row["id"], "cancelled")
            started = time.perf_counter()
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            await fixture.service.evidence.cancel_pending()
            result = {
                "cancel_seconds": time.perf_counter() - started,
                "children_done": all(child.done() for child in children),
                "inflight_http": fixture.source.active,
                "pending_flights": len(fixture.service.evidence.pending),
                "waiters": len(fixture.service.evidence._waiters),
                "cancelled_http": fixture.source.cancelled_requests,
                "http_reads": len(fixture.source.starts),
                "model_calls": fixture.runtime.browsing_repository.round(row["id"])["budget"]["model_calls"],
            }
            assert (
                result["children_done"]
                and not result["inflight_http"]
                and not result["pending_flights"]
                and not result["waiters"]
            )
            return result
        finally:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
    if case == "live_priority":
        entered = asyncio.Event()
        fixture.runtime.state, fixture.runtime.queue = "live", ["controlled-pending-live-event"]

        async def offline():
            async with fixture.runtime.offline_generation():
                entered.set()

        offline_task = asyncio.create_task(offline())
        preparation = asyncio.create_task(fixture.service.evidence.prepare(VIDEOS[0], fixture.config))
        started = time.perf_counter()
        try:
            await asyncio.wait_for(fixture.source.entered.wait(), 2)
            await asyncio.sleep(0.05)
            blocked = not entered.is_set()
            fixture.runtime.queue.clear()
            fixture.runtime.state = "stopped"
            await asyncio.wait_for(entered.wait(), 2)
            elapsed = time.perf_counter() - started
            await preparation
            assert blocked and not fixture.runtime.generation_lock.locked()
            return {
                "background_generation_blocked_while_live_pending": blocked,
                "background_admitted_after_live_queue_cleared": entered.is_set(),
                "admission_seconds": elapsed,
                "real_gpu_generation_invoked": False,
                "http_reads": len(fixture.source.starts),
                "lock_released": not fixture.runtime.generation_lock.locked(),
            }
        finally:
            fixture.runtime.queue.clear()
            fixture.runtime.state = "stopped"
            offline_task.cancel()
            preparation.cancel()
            await asyncio.gather(offline_task, preparation, return_exceptions=True)
    raise ValueError("Unknown auxiliary case")


async def child(args):
    directory = Path(args.case_directory).resolve()
    directory.mkdir()
    fixture = Fixture(
        args.root.resolve(),
        directory,
        args.latency_ms / 1000,
        args.spacing_ms / 1000,
        restricted=args.case == "restriction",
        blocked=args.case == "cancellation",
    )
    try:
        await fixture.open()
        if args.case in {"cold", "hot"}:
            if args.case == "hot":
                await measure(fixture, VIDEOS, 4)
            result = await measure(fixture, VIDEOS, args.workers)
            result["cache_condition"] = args.case
            assert result["http_reads"] == (24 if args.case == "cold" else 0)
            assert result["cache_hits"] == (0 if args.case == "cold" else 4)
        else:
            result = await auxiliary(fixture, args.case)
        result["http_methods"] = ["GET"]
        return result
    finally:
        await fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--latency-ms", type=float, default=300)
    parser.add_argument("--spacing-ms", type=float, default=250)
    parser.add_argument(
        "--case", choices=["cold", "hot", "singleflight", "restriction", "cancellation", "live_priority"]
    )
    parser.add_argument("--workers", type=int, choices=[1, 4], default=1)
    parser.add_argument("--case-directory")
    args = parser.parse_args()
    if args.latency_ms < 0 or args.spacing_ms < 0:
        parser.error("Delays cannot be negative")
    if args.case:
        print(json.dumps(asyncio.run(child(args)), ensure_ascii=False))
        return
    base = (args.runtime_root or args.root / "tmp").resolve()
    base.mkdir(parents=True, exist_ok=True)
    results, probes = [], {}
    with tempfile.TemporaryDirectory(prefix="browsing-preparation-", dir=base) as work:
        cases = [(cache, worker) for cache in ("cold", "hot") for worker in (1, 4)]
        cases += [(name, 4) for name in ("singleflight", "restriction", "cancellation", "live_priority")]
        for index, (case, workers) in enumerate(cases):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--root",
                str(args.root.resolve()),
                "--case",
                case,
                "--workers",
                str(workers),
                "--case-directory",
                str(Path(work) / f"case-{index}"),
                "--latency-ms",
                str(args.latency_ms),
                "--spacing-ms",
                str(args.spacing_ms),
            ]
            completed = subprocess.run(command, text=True, capture_output=True, encoding="utf-8")
            if completed.returncode:
                print(completed.stderr, file=sys.stderr)
                completed.check_returncode()
            value = json.loads(completed.stdout)
            if case in {"cold", "hot"}:
                results.append(value)
            else:
                probes[case] = value
            print(f"completed {case} workers={workers}", file=sys.stderr, flush=True)
    assert all(result["source_versions"] == results[0]["source_versions"] for result in results)
    comparisons = {}
    for cache in ("cold", "hot"):
        serial, parallel = [result for result in results if result["cache_condition"] == cache]
        comparisons[cache] = {
            "workers_1_to_4_speedup": serial["wall_seconds"] / parallel["wall_seconds"],
            "same_source_versions": True,
            "http_read_delta": parallel["http_reads"] - serial["http_reads"],
        }
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "Real preparation/cache/scheduler code on this host; deterministic simulated source and latency, no Bilibili network or GPU generation",
        "host": {
            "os": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total,
        },
        "implementation_sources_sha256": {
            cls.__module__: hashlib.sha256(
                Path(inspect.getfile(cls)).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            for cls in (
                BrowsingAdapter,
                BrowsingService,
                BrowsingEvidence,
                BrowsingRepository,
                MediaCache,
                Runtime,
            )
        },
        "fixture": {
            "videos": VIDEOS,
            "subtitle_segments_per_video": len(BODY),
            "sha256": hashlib.sha256(
                json.dumps([VIDEOS, BODY], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "response_delay_seconds": args.latency_ms / 1000,
            "application_request_interval_seconds": args.spacing_ms / 1000,
            "adapter_minimum_request_interval_seconds": 0.1,
            "cold_http_reads_per_video": 6,
        },
        "measurements": results,
        "comparisons": comparisons,
        "controlled_probes": probes,
        "limitations": [
            "Response latency and API payloads are simulated, not real platform throughput or access verification.",
            "RSS is this isolated CPU preparation process; kernel peak includes process initialization and hot-cache population.",
            "Live priority tests the actual offline-generation lock using controlled live queue events, not OBS playback or GPU concurrency.",
            "One measurement per configuration; very short hot-cache timings are not statistically stable performance claims.",
        ],
        "model_generations": 0,
        "platform_writes": 0,
        "environment_image_changed": False,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
