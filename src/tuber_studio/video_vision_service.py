"""Persistent visual admission with real images and paired-node resource samples."""

import asyncio
import io
import json
import math
import struct
import time
import zlib
from contextlib import asynccontextmanager

import httpx

from .config import atomic_json
from .http_api import response_json
from .inference_profiles import GEMMA_KEY, GROUNDED_DAFEIYU_KEY, llm_resource_key
from .operations.browsing.models import DisabledVideoEvidence
from .runtime_modes import ObservationOwner
from .video_vision import ApiVideoEvidenceProvider

RETRYABLE_CAPABILITY_ERRORS = (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError)


async def _settle(operation):
    task = asyncio.ensure_future(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@asynccontextmanager
async def _probe_client(runtime, factory):
    # The observer owns its pool. It never borrows the control/long-task pool.
    headers = runtime.inference.client.headers.copy()
    headers["Connection"] = "close"
    client = factory(
        base_url=str(runtime.inference.client.base_url),
        headers=headers,
        timeout=httpx.Timeout(5, connect=5),
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )
    try:
        yield client
    finally:
        await _settle(client.aclose())


class ProbeSampler:
    """One probe's GET-only resource observer and its measured sampling gaps."""

    def __init__(self, client, provider):
        self.client = client
        self.profile_key = (
            GROUNDED_DAFEIYU_KEY
            if llm_resource_key(provider.config.model) == GROUNDED_DAFEIYU_KEY
            else GEMMA_KEY
        )
        self.expected = {
            "model_sha256": provider.config.model_revision,
            "projector_sha256": provider.config.projector_revision,
            "runtime_sha256": provider.config.runtime_revision,
        }
        self.started = time.monotonic()
        self.samples = []
        self.intervals = []
        self.request_retries = 0
        self.request_error_count = 0
        self.request_errors = []
        self.monitoring_error = self.monitoring_error_detail = None
        self.lock = asyncio.Lock()

    def detail(self, error):
        message = str(error)
        for name in ("X-Tuber-Inference-Key", "Authorization"):
            secret = self.client.headers.get(name)
            if secret:
                message = message.replace(secret, "[redacted]")
                if secret.startswith("Bearer "):
                    message = message.replace(secret[7:], "[redacted]")
        return message[:240]

    async def _capabilities(self):
        for attempt in range(2):
            try:
                return await response_json(self.client, "GET", "/capabilities", timeout=15)
            except Exception as error:
                retry = attempt == 0 and isinstance(error, RETRYABLE_CAPABILITY_ERRORS)
                self.request_error_count += 1
                if len(self.request_errors) < 64:
                    self.request_errors.append(
                        {
                            "error": type(error).__name__,
                            "detail": self.detail(error),
                            "elapsed_seconds": round(time.monotonic() - self.started, 6),
                            "attempt": attempt + 1,
                            "retry": retry,
                        }
                    )
                if not retry:
                    raise
                self.request_retries += 1
                # With keepalive=0 and Connection: close, the next GET creates
                # a fresh connection. No model or platform POST is retried.

    async def sample(self):
        async with self.lock:
            value = await self._capabilities()
            profile = value.get("profiles", {}).get(self.profile_key, {})
            if (
                value.get("mode") != "text"
                or not profile.get("loaded")
                or any(profile.get(key) != expected for key, expected in self.expected.items())
            ):
                raise ValueError("节点实际模型、视觉或引擎与运营配置不一致")
            resource = value.get("resource", {})
            if any(type(resource.get(key)) is not int for key in ("available_bytes", "swap_used_bytes")):
                raise ValueError("节点未返回可测内存状态")
            elapsed = time.monotonic() - self.started
            if self.samples:
                self.intervals.append(elapsed - self.samples[-1]["monotonic_seconds"])
            self.samples.append({"at": time.time(), "monotonic_seconds": elapsed, **resource})

    async def monitor(self, stopped):
        while not stopped.is_set():
            try:
                await self.sample()
            except Exception as error:
                self.monitoring_error = type(error).__name__
                self.monitoring_error_detail = self.detail(error)
                return
            try:
                await asyncio.wait_for(stopped.wait(), timeout=1)
            except TimeoutError:
                pass

    def measurement(self):
        baseline = self.samples[0]["swap_used_bytes"] if self.samples else 0
        return {
            "sample_count": len(self.samples),
            "min_available_bytes": min(s["available_bytes"] for s in self.samples) if self.samples else None,
            "max_swap_growth_bytes": max(0, max(s["swap_used_bytes"] for s in self.samples) - baseline)
            if self.samples
            else None,
            "monitoring_error": self.monitoring_error,
            "monitoring_error_detail": self.monitoring_error_detail,
            "request_retries": self.request_retries,
            "request_error_count": self.request_error_count,
            "request_errors": list(self.request_errors),
            "request_errors_truncated": self.request_error_count - len(self.request_errors),
            "sample_intervals_seconds": [round(value, 6) for value in self.intervals],
            "max_sample_interval_seconds": max(self.intervals, default=0),
            "sampling_span_seconds": self.samples[-1]["monotonic_seconds"]
            - self.samples[0]["monotonic_seconds"]
            if self.samples
            else 0,
            "first_sample_monotonic": self.started + self.samples[0]["monotonic_seconds"]
            if self.samples
            else None,
            "last_sample_monotonic": self.started + self.samples[-1]["monotonic_seconds"]
            if self.samples
            else None,
        }


def configured_video_provider(runtime):
    config = runtime.settings.data.get("video_vision", {})
    if not config.get("enabled"):
        return DisabledVideoEvidence()
    key = runtime.settings.broker_key_for(config["base_url"]) or runtime.settings.provider_key("vision")
    return ApiVideoEvidenceProvider(runtime, config, api_key=key)


async def restore_admission(runtime):
    if runtime.inference.mode_lock.locked():
        return
    owner = ObservationOwner.capture(runtime)
    provider = runtime.browsing.visual_provider
    path = runtime.settings.runtime / "video-vision-admission.json"
    if not isinstance(provider, ApiVideoEvidenceProvider) or not path.is_file() or path.is_symlink():
        return
    if path.stat().st_size > 65536:
        return
    snapshot = path.read_bytes()
    value = json.loads(snapshot)
    proof = value.get("proof", {})
    configuration = provider.identity()
    if proof.get("passed") is not True:
        return
    capabilities = await response_json(owner.client, "GET", "/capabilities", timeout=15)
    key = (
        GROUNDED_DAFEIYU_KEY if llm_resource_key(provider.config.model) == GROUNDED_DAFEIYU_KEY else GEMMA_KEY
    )
    profile = capabilities.get("profiles", {}).get(key, {})
    if not profile.get("loaded") and capabilities.get("state") != "active":
        return
    current = await provider._runtime_properties()
    if not isinstance(current, dict) or current.get("context_tokens", 0) < provider.config.context_tokens:
        return
    if (
        not owner.current(runtime)
        or runtime.inference.mode_lock.locked()
        or runtime.browsing.visual_provider is not provider
        or provider.identity() != configuration
    ):
        return
    try:
        if path.is_symlink() or path.read_bytes() != snapshot:
            return
    except OSError:
        return
    # No await after checking the owner/configuration and durable proof again.
    # A long probe can invalidate the proof without blocking the health observer.
    provider._probe, provider._admission = value["probe"], proof


def boundary_frames(settings, config):
    """Exercise the declared input bounds using existing public artwork, not a quality gold."""
    from PIL import Image

    side = math.isqrt(config.max_pixels)
    if side * side != config.max_pixels:
        raise ValueError("资源探测的像素上限须为可精确构造的方形；未改变配置")
    sources = ("assets/whale/sing_closed.png", "assets/whale/sing_open.png", "assets/whale/idle.gif")
    frames = []
    for n in range(config.max_frames):
        source = settings.asset_path(sources[n % len(sources)])
        with Image.open(source) as original:
            # Upscaling is explicit test load; it adds no visual information.
            image = original.convert("RGB").resize((side, side), Image.Resampling.NEAREST)
            output = io.BytesIO()
            image.save(output, format="PNG", compress_level=9)
        frames.append(
            {
                "id": f"asset-probe-{n}",
                "cid": "asset-probe",
                "actual_time": n,
                "requested_time": n,
                "kind": "bundled_asset_boundary_probe",
                "image": output.getvalue(),
                "mime_type": "image/png",
                "width": side,
                "height": side,
            }
        )
    total = sum(len(frame["image"]) for frame in frames)
    remaining = config.max_bytes - total
    if remaining < 32:
        raise ValueError("当前图像探测负载不能覆盖所配字节上限；未降低配置")
    # A valid ancillary PNG text chunk fills the transport/parser byte bound.
    payload = b"resource-probe\0" + b"x" * (remaining - 12 - len(b"resource-probe\0"))
    chunk = (
        struct.pack(">I", len(payload)) + b"tEXt" + payload + struct.pack(">I", zlib.crc32(b"tEXt" + payload))
    )
    raw = frames[-1]["image"]
    frames[-1]["image"] = raw[:-12] + chunk + raw[-12:]
    return frames


async def probe_visual(runtime, *, client_factory=None):
    provider = runtime.browsing.visual_provider
    if not isinstance(provider, ApiVideoEvidenceProvider) or not runtime.inference.client:
        raise ValueError("请先配置运营视觉端点")
    if runtime.inference.status.get("mode") != "text" or runtime.inference.mode_lock.locked():
        raise ValueError("请先切到运营模式")
    if runtime.generation_lock.locked():
        raise ValueError("请等待当前生成结束后验证视觉")
    async with runtime.inference.mode_lock:
        async with _probe_client(runtime, client_factory or httpx.AsyncClient) as client:
            return await _run_probe(runtime, provider, ProbeSampler(client, provider))


async def _run_probe(runtime, provider, sampler):
    admission_path = runtime.settings.runtime / "video-vision-admission.json"
    provider._probe = provider._admission = None
    atomic_json(admission_path, {"proof": {"passed": False, "started_at": time.time()}}, private=True)
    try:
        await sampler.sample()
        frames = await asyncio.to_thread(boundary_frames, runtime.settings, provider.config)
        metadata = {"parts": [{"cid": "asset-probe", "duration": provider.config.max_frames}]}
        stopped = asyncio.Event()
        worker = asyncio.create_task(sampler.monitor(stopped))
        try:
            result = await provider.probe(frames, metadata=metadata)
            await sampler.sample()
        finally:
            stopped.set()
            await _settle(worker)
    except BaseException as error:
        failed = {
            "passed": False,
            "configuration": provider.identity(),
            "failure_type": type(error).__name__,
            "failure_detail": sampler.detail(error),
            "measurement": sampler.measurement(),
            "completed_at": time.time(),
        }
        try:
            atomic_json(admission_path, {"proof": failed}, private=True)
        except OSError:
            # The initial durable invalidation already prevents old proof reuse.
            pass
        raise
    measured = sampler.measurement()
    passed = (
        measured["monitoring_error"] is None
        and measured["sample_count"] >= 2
        and measured["min_available_bytes"] >= 384 * 2**20
        and measured["max_swap_growth_bytes"] <= 128 * 2**20
    )
    proof = {
        "passed": passed,
        "configuration": provider.identity(),
        "context_tokens": result["properties"]["context_tokens"],
        **{key: getattr(provider.config, key) for key in ("max_frames", "max_pixels", "max_bytes")},
        "measurement": measured,
        "completed_at": time.time(),
        "source_kind": "bundled_asset",
        "input_boundaries": {
            "frames": len(frames),
            "max_pixels": max(f["width"] * f["height"] for f in frames),
            "total_bytes": sum(len(f["image"]) for f in frames),
            "upscale": "nearest",
            "byte_padding": "valid_PNG_ancillary_text",
            "quality_acceptance": False,
        },
    }
    atomic_json(admission_path, {"proof": proof, "probe": provider._probe}, private=True)
    if passed:
        provider._admission = proof
    return {"capabilities": provider.capabilities(), "measurement": measured, "result": result["result"]}
