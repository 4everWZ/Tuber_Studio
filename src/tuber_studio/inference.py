"""Container-local inference broker with fixed destinations and explicit work modes."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import struct
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .config import normalize_broker_llm
from .inference_cache import cache_summary, scan_cache
from .inference_observation import BackgroundObservation
from .inference_profiles import (
    DAFEIYU_9B_CONTEXT,
    DAFEIYU_9B_DIGESTS,
    DAFEIYU_9B_KEY,
    DAFEIYU_9B_MODEL,
    DAFEIYU_9B_PROJECTOR,
    DAFEIYU_9B_WEIGHTS,
    DAFEIYU_KEY,
    DAFEIYU_WEIGHTS,
    GEMMA_ARTIFACTS,
    GEMMA_ENGINE,
    GEMMA_ENGINE_SHA256,
    GEMMA_KEY,
    GROUNDED_DAFEIYU_ARTIFACTS,
    GROUNDED_DAFEIYU_BUNDLE,
    GROUNDED_DAFEIYU_KEY,
    GROUNDED_DAFEIYU_WEIGHTS,
    LLM_MODELS,
    VISION_ROUTES,
    artifact_identity,
    llm_resource_key,
    project_asset,
    verify_artifact,
)
from .inference_validation import (
    project_wav,
    validate_context_tokens,
    validate_llm_request,
    validate_speech_request,
    validate_vision_request,
)

MAX_BODY = 2 * 1024 * 1024
MAX_VISION_BODY = 12 * 1024 * 1024
HEADER = "X-Tuber-Inference-Key"
MODEL_CACHE_SUFFIXES = {".gguf", ".bin", ".pth", ".ckpt", ".safetensors", ".onnx", ".data"}


@dataclass(frozen=True)
class Resource:
    url: str
    ready: str
    alias: str | None = None
    model: str | None = None


RESOURCES = {
    "llm2b": Resource("http://127.0.0.1:9013", "/health", LLM_MODELS["llm2b"], "MiniCPM5-2B-Q4_K_M.gguf"),
    DAFEIYU_9B_KEY: Resource("http://127.0.0.1:9018", "/health", DAFEIYU_9B_MODEL, DAFEIYU_9B_WEIGHTS),
    "llm1b": Resource("http://127.0.0.1:9010", "/health", LLM_MODELS["llm1b"], "MiniCPM5-1B-Q4_K_M.gguf"),
    DAFEIYU_KEY: Resource("http://127.0.0.1:9016", "/health", LLM_MODELS[DAFEIYU_KEY], DAFEIYU_WEIGHTS),
    GROUNDED_DAFEIYU_KEY: Resource(
        "http://127.0.0.1:9031",
        "/health",
        LLM_MODELS[GROUNDED_DAFEIYU_KEY],
        f"{GROUNDED_DAFEIYU_BUNDLE}/{GROUNDED_DAFEIYU_WEIGHTS}",
    ),
    "llm1bq8": Resource("http://127.0.0.1:9014", "/health", LLM_MODELS["llm1bq8"], "MiniCPM5-1B-Q8_0.gguf"),
    "llmqwen35": Resource(
        "http://127.0.0.1:9015", "/health", LLM_MODELS["llmqwen35"], "Qwen_Qwen3.5-2B-Q4_K_M.gguf"
    ),
    GEMMA_KEY: Resource(
        "http://127.0.0.1:9030", "/health", LLM_MODELS[GEMMA_KEY], "gemma-4-E2B-it-Q4_0.gguf"
    ),
    "tts": Resource("http://127.0.0.1:9011", "/docs"),
}
LLM_POSTS = {"apply-template", "tokenize", "v1/chat/completions", "completion", "warmup"}


def resolve_llm_resource_key(model_name):
    key = llm_resource_key(model_name)
    if key is None:
        raise HTTPException(422, "Unknown inference model")
    return key


def resource_cache_type(key: str, configured: str) -> str | None:
    if not RESOURCES[key].model:
        return None
    return configured if key in {"llm1b", "llm2b", DAFEIYU_KEY, *VISION_ROUTES.values()} else "q8_0"


def native_context(path: Path, *, expected_architecture: str | None = None) -> int:
    """Read the declared language-model window, never a vision context field."""
    formats = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
    with path.open("rb") as source:

        def number(fmt):
            return struct.unpack("<" + fmt, source.read(struct.calcsize("<" + fmt)))[0]

        def string():
            return source.read(number("Q")).decode("utf-8")

        def skip(kind):
            if kind == 8:
                source.seek(number("Q"), 1)
            elif kind == 9:
                subtype, count = number("I"), number("Q")
                if subtype in formats:
                    source.seek(struct.calcsize("<" + formats[subtype]) * count, 1)
                else:
                    for _ in range(count):
                        skip(subtype)
            else:
                number(formats[kind])

        if source.read(4) != b"GGUF" or number("I") not in {2, 3}:
            raise ValueError("Invalid selected GGUF")
        number("Q")
        architecture, contexts = None, {}
        for _ in range(number("Q")):
            key, kind = string(), number("I")
            if key == "general.architecture":
                if kind != 8:
                    raise ValueError("Invalid GGUF architecture metadata")
                architecture = string()
                if expected_architecture and architecture != expected_architecture:
                    raise ValueError("GGUF architecture does not match the selected artifact")
            elif key.endswith(".context_length") and kind in {0, 1, 2, 3, 4, 5, 10, 11}:
                contexts[key] = number(formats[kind])
            else:
                skip(kind)
            if architecture and (context := contexts.get(architecture + ".context_length")) is not None:
                if context <= 0:
                    raise ValueError("Invalid native model context")
                return context
    raise ValueError("Selected GGUF has no native context metadata")


class ProcessRunner:
    """Own only fixed inference subprocess groups inside the application container."""

    def __init__(
        self,
        root: Path,
        *,
        llama_binary: Path | None = None,
        tts_python: Path | None = None,
        kv_cache_type="q8_0",
        cuda_managed_memory=False,
        context_tokens=131072,
        node_settings=None,
    ):
        if kv_cache_type not in {"q8_0", "q4_0"} or type(cuda_managed_memory) is not bool:
            raise ValueError("Invalid fixed inference allocation profile")
        self.root = root.resolve()
        self.llama_binary = llama_binary or Path("/opt/llama/bin/llama-server")
        self.tts_python = tts_python or Path("/opt/studio-venv/bin/python")
        self.processes = {}
        self.logs = {}
        self.kv_cache_type, self.cuda_managed_memory = kv_cache_type, cuda_managed_memory
        self.context_tokens = validate_context_tokens(context_tokens)
        self.node_settings = node_settings
        self.verified_artifacts = {}

    def model_path(self, key):
        if key == GEMMA_KEY and self.node_settings:
            value = self.node_settings.get("gemma_model", "runtime/models/" + RESOURCES[key].model)
            return project_asset(self.root, value)
        return (self.root / "runtime/models" / RESOURCES[key].model).resolve()

    def gemma_assets(self):
        settings = self.node_settings or {}
        vision_only = settings.get("gemma_vision_only", self.node_settings is not None)
        projector = GEMMA_ARTIFACTS["vision_only_projector" if vision_only else "projector"]
        return {
            "model": self.model_path(GEMMA_KEY),
            "projector": project_asset(
                self.root,
                settings.get("gemma_projector", "runtime/models/" + projector["filename"]),
            ),
            "engine": project_asset(self.root, settings.get("gemma_engine", GEMMA_ENGINE)),
        }

    def artifact_identifiers(self, key):
        result = {"model_sha256": None, "projector_sha256": None, "runtime_sha256": None}
        if key in VISION_ROUTES.values():
            for role, path in self.vision_assets(key).items():
                try:
                    digest = self.verified_artifacts.get(artifact_identity(path))
                except OSError:
                    digest = None
                result[("runtime" if role == "engine" else role) + "_sha256"] = digest
        return result

    def vision_assets(self, key):
        if key == GEMMA_KEY:
            return self.gemma_assets()
        if key == DAFEIYU_9B_KEY:
            model = self.model_path(key)
            return {"model": model, "projector": model.with_name(DAFEIYU_9B_PROJECTOR), "engine": self.llama_binary}
        model = project_asset(self.root, "runtime/models/" + RESOURCES[GROUNDED_DAFEIYU_KEY].model)
        return {
            "model": model,
            "projector": project_asset(self.root, str(model.with_name("mmproj-4x-f16.gguf"))),
            "engine": project_asset(self.root, GEMMA_ENGINE),
        }

    def command(self, key):
        resource = RESOURCES[key]
        if key == GEMMA_KEY:
            assets = self.gemma_assets()
            return [
                str(assets["engine"]),
                "--model",
                str(assets["model"]),
                "--mmproj",
                str(assets["projector"]),
                "--alias",
                resource.alias,
                "--host",
                "127.0.0.1",
                "--port",
                "9030",
                "--jinja",
                "--ctx-size",
                str(self.context_tokens),
                "--parallel",
                "1",
                "--fit",
                "off",
                "--gpu-layers",
                "99",
                "--flash-attn",
                "on",
                "--cache-type-k",
                self.kv_cache_type,
                "--cache-type-v",
                self.kv_cache_type,
                "--reasoning",
                "on",
                "--temp",
                "0.2",
                "--top-p",
                "0.95",
                "--top-k",
                "40",
                "--min-p",
                "0",
                "--repeat-penalty",
                "1",
                "--batch-size",
                "256",
                "--ubatch-size",
                "64",
                "--threads",
                "4",
                "--cache-ram",
                "0",
                "--ctx-checkpoints",
                "0",
                "--no-context-shift",
                "--load-mode",
                "dio",
                "--lazy-mode",
                "on",
                "--mmproj-offload",
            ]
        if resource.model:
            grounded = self.vision_assets(key) if key == GROUNDED_DAFEIYU_KEY else None
            cache = resource_cache_type(key, self.kv_cache_type)
            command = [
                str(grounded["engine"] if grounded else self.llama_binary),
                "-m",
                str(self.model_path(key)),
                "--alias",
                resource.alias,
                "--host",
                "127.0.0.1",
                "--port",
                str(httpx.URL(resource.url).port),
                "--jinja",
                "--ctx-size",
                str(self.context_tokens),
                "--parallel",
                "1",
                "--fit",
                "off",
                "--n-gpu-layers",
                "99",
                "--flash-attn",
                "on",
                "--cache-type-k",
                cache,
                "--cache-type-v",
                cache,
                "--reasoning",
                (self.node_settings or {}).get("reasoning", "auto"),
                "--temp",
                "1.0" if key in {"llm2b", "llmqwen35"} else "0.7",
                "--top-p",
                "1.0" if key == "llmqwen35" else "0.95",
                "--min-p",
                "0.0",
                "--batch-size",
                "256",
                "--ubatch-size",
                "64",
                "--cache-ram",
                "0",
            ]
            if grounded:
                return command + [
                    "--mmproj",
                    str(grounded["projector"]),
                    "--mmproj-offload",
                    "--load-mode",
                    "dio",
                    "--ctx-checkpoints",
                    "0",
                    "--no-context-shift",
                    "--threads",
                    "4",
                    "--no-webui",
                ]
            command.extend(["--no-mmap", "--direct-io"])
            if key == DAFEIYU_9B_KEY:
                command.extend(["--mmproj", str(self.vision_assets(key)["projector"]),
                                "--ctx-checkpoints", "0", "--no-context-shift"])
            if (self.node_settings or {}).get("speculative_ngram", False):
                command.extend(["--spec-type", "ngram-mod"])
            if key == "llmqwen35":
                command.extend(
                    [
                        "--top-k",
                        "20",
                        "--presence-penalty",
                        "2.0",
                        "--repeat-penalty",
                        "1.0",
                        # Hybrid recurrent state cannot be shifted like dense KV.
                        # Bound host snapshots; ContextManager owns compaction.
                        "--ctx-checkpoints",
                        "0",
                        "--cache-reuse",
                        "0",
                        "--no-context-shift",
                    ]
                )
            return command
        return [
            str(self.tts_python),
            str(self.root / "tools/jetson/tts/server.py"),
            "-a",
            "127.0.0.1",
            "-p",
            "9011",
        ]

    async def validate_all(self):
        return await asyncio.to_thread(self._validate_all_sync)

    def _validate_all_sync(self):
        profiles = {}
        configuration = self.root / "runtime/studio.json"
        settings = (
            self.node_settings
            if self.node_settings is not None
            else (json.loads(configuration.read_text()) if configuration.is_file() else {})
        )
        genie = settings.get("tts", {}).get("local_backend", "compact") == "genie_onnx_int8"
        for key, resource in RESOURCES.items():
            model = self.model_path(key) if resource.model else None
            # Only the selected/requested model must be installed. _activate
            # rejects an absent fixed profile before changing any processes.
            if model is not None and not model.is_file():
                continue
            if key in VISION_ROUTES.values():
                if all(path.is_file() for path in self.vision_assets(key).values()):
                    profiles[key] = {"command": self.command(key)}
                continue
            required = (
                [model, self.llama_binary]
                if model is not None
                else [
                    self.tts_python,
                    self.root / "tools/jetson/tts/server.py",
                    self.root / "tools/jetson/tts/compact.py",
                    self.root / "tools/jetson/tts/inference_only.py",
                    self.root / "tools/jetson/tts/serve.py",
                    self.root / "runtime/tts/tts_infer.yaml",
                ]
            )
            if key == "tts" and genie:
                required.extend(
                    self.root / "tools/jetson/tts" / name
                    for name in ("genie_backend.py", "genie_bundle.py", "genie_sampling.py", "genie_bert.py")
                )
            if any(path is None or not path.is_file() for path in required):
                raise RuntimeError("Fixed inference profile prerequisite is missing")
            if model is not None and not model.is_relative_to(self.root):
                raise RuntimeError("Fixed inference model is outside this project")
            profiles[key] = {"command": self.command(key)}
        return profiles

    async def running(self, key):
        if key not in RESOURCES:
            raise ValueError("Unknown fixed inference profile")
        process = self.processes.get(key)
        return process is not None and process.returncode is None

    async def stop(self, key):
        if key not in RESOURCES:
            raise ValueError("Unknown fixed inference profile")
        process = self.processes.get(key)
        if process is None:
            return
        # Every child gets its own session. Never signal arbitrary caller PIDs.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 15)
        except TimeoutError:
            pass
        # Also terminate grandchildren if their leader exited before them.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        self.processes.pop(key, None)
        if handle := self.logs.pop(key, None):
            handle.close()

    def _advise_owned_model_cache(self):
        """Best-effort advice for unused model pages, never global cache reclaim."""
        advise = getattr(os, "posix_fadvise", None)
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
        if advise is None or dontneed is None:
            return 0
        try:
            runtime = (self.root / "runtime").resolve()
        except (OSError, RuntimeError):
            return 0
        directories = []
        for relative in ("models", "node-assets", "tts/weights", "tts/pretrained", "tts/genie"):
            try:
                directory = (runtime / relative).resolve()
                if directory.is_relative_to(runtime) and directory.is_dir():
                    directories.append(directory)
            except (OSError, RuntimeError):
                continue
        advised, seen = 0, set()
        for directory in directories:
            try:
                for candidate in directory.rglob("*"):
                    if candidate.suffix.lower() not in MODEL_CACHE_SUFFIXES:
                        continue
                    descriptor = None
                    try:
                        path = candidate.resolve()
                        if (
                            path in seen
                            or not path.is_relative_to(runtime)
                            or not any(path.is_relative_to(base) for base in directories)
                            or path.suffix.lower() not in MODEL_CACHE_SUFFIXES
                            or not path.is_file()
                        ):
                            continue
                        seen.add(path)
                        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                        descriptor = os.open(path, flags)
                        advise(descriptor, 0, 0, dontneed)
                        advised += 1
                    except (OSError, ValueError, RuntimeError):
                        continue
                    finally:
                        if descriptor is not None:
                            try:
                                os.close(descriptor)
                            except OSError:
                                pass
            except (OSError, RuntimeError):
                continue
        return advised

    async def start(self, key):
        profiles = await self.validate_all()
        if key not in profiles:
            raise RuntimeError("Optional fixed inference profile is not installed")
        resource = RESOURCES[key]
        if (
            resource.model
            and await asyncio.to_thread(lambda: native_context(self.model_path(key))) < self.context_tokens
        ):
            raise RuntimeError("Selected model does not natively support the requested context")
        if key == GROUNDED_DAFEIYU_KEY:
            for role, path in self.vision_assets(key).items():
                await asyncio.to_thread(
                    verify_artifact,
                    path,
                    GROUNDED_DAFEIYU_ARTIFACTS[role],
                    self.verified_artifacts,
                )
        elif key == DAFEIYU_9B_KEY:
            assets = self.vision_assets(key)
            for role, digest in DAFEIYU_9B_DIGESTS.items():
                await asyncio.to_thread(verify_artifact, assets[role], digest, self.verified_artifacts)
        elif key == GEMMA_KEY:
            assets = self.gemma_assets()
            await asyncio.to_thread(
                verify_artifact, assets["engine"], GEMMA_ENGINE_SHA256, self.verified_artifacts
            )
            for role in ("model", "projector"):
                expected = GEMMA_ARTIFACTS[role]["sha256"]
                if role == "projector" and (self.node_settings or {}).get(
                    "gemma_vision_only", self.node_settings is not None
                ):
                    expected = GEMMA_ARTIFACTS["vision_only_projector"]["sha256"]
                await asyncio.to_thread(verify_artifact, assets[role], expected, self.verified_artifacts)
        await self.stop(key)
        self._advise_owned_model_cache()
        log = self.root / "runtime" / ("inference-" + key + ".log")
        handle = log.open("ab")
        environment = os.environ.copy()
        environment.update({"TUBER_ROOT": str(self.root), "TUBER_GPT_SOVITS_ROOT": "/opt/GPT-SoVITS-v2"})
        environment.pop("GGML_CUDA_ENABLE_UNIFIED_MEMORY", None)
        if key == "llm2b" and self.cuda_managed_memory:
            environment["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] = "1"
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *profiles[key]["command"],
                cwd=self.root,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Finish acquiring the child handle before propagating cancellation;
            # otherwise a child created during exec could escape ownership.
            while not launch.done():
                try:
                    await asyncio.shield(launch)
                except asyncio.CancelledError:
                    continue
            try:
                process = launch.result()
            except BaseException:
                handle.close()
                raise
            self.logs[key], self.processes[key] = handle, process
            await asyncio.shield(self.stop(key))
            raise
        except BaseException:
            handle.close()
            raise
        self.logs[key], self.processes[key] = handle, process

    async def close(self):
        for key in tuple(self.processes):
            await self.stop(key)


class InferenceBroker:
    def __init__(
        self,
        root: Path,
        runner=None,
        client=None,
        *,
        mode="text",
        selected=DAFEIYU_9B_KEY,
        operations_selected=None,
        live_selected=None,
        configuration_path=None,
    ):
        mode = "text" if mode == "exclusive" else mode
        if mode not in {"text", "live"}:
            raise ValueError("Unknown inference resource mode")
        if selected not in RESOURCES or not RESOURCES[selected].model:
            raise ValueError("Unknown selected inference model")
        self.root = root.resolve()
        self.runner = runner or ProcessRunner(
            self.root,
            context_tokens=DAFEIYU_9B_CONTEXT if selected == DAFEIYU_9B_KEY else 131072,
            kv_cache_type="q4_0" if selected == DAFEIYU_9B_KEY else "q8_0",
        )
        self.context_tokens = validate_context_tokens(getattr(self.runner, "context_tokens", 131072))
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(1200, connect=10), trust_env=False)
        self.lock = asyncio.Lock()
        self.active = None
        self.state = "cold"
        self.error = None
        self.properties = {}
        self.natives = {}
        self.loaded = set()
        self.mode = mode
        self.selected = selected
        self.operations_selected = operations_selected
        self.live_selected = live_selected
        for key in (operations_selected, live_selected):
            if key is not None and (key not in RESOURCES or not RESOURCES[key].model):
                raise ValueError("Unknown mode-specific inference model")
        self.configuration_path = configuration_path
        self._target_touched = False
        self._installed_profiles = None
        self._artifact_identifiers = {}
        self._asset_epoch = 0
        self._observation_stop = threading.Event()
        self._metadata_observation = BackgroundObservation(
            self._observe_asset_metadata, interval=5, minimum_interval=1
        )
        self._storage_observation = BackgroundObservation(
            self._observe_asset_storage, interval=300, retry_interval=30, minimum_interval=30
        )
        self._initial_swap = psutil.swap_memory().used

    async def _profiles(self):
        profiles = await self.runner.validate_all()
        self._installed_profiles = profiles
        return profiles

    async def _observe_asset_metadata(self):
        epoch = self._asset_epoch
        if hasattr(self.runner, "artifact_identifiers"):
            for key in VISION_ROUTES.values():
                identities = await asyncio.to_thread(self.runner.artifact_identifiers, key)
                if epoch == self._asset_epoch:
                    self._artifact_identifiers[key] = identities
        profiles = await self._profiles()
        return {"profiles": sorted(profiles)}

    async def _observe_asset_storage(self):
        return await asyncio.to_thread(self.storage_summary)

    def _unknown_storage(self):
        return [
            {
                "kind": name,
                "path": str(self.root / "runtime" / name),
                "used_bytes": None,
                "reclaimable_bytes": None,
                "usage_complete": False,
                "protected": name != "inference-cache",
            }
            for name in ("models", "engines", "tts", "node-assets", "nltk_data", "inference-cache")
        ]

    def resource_snapshot(self):
        memory, swap = psutil.virtual_memory(), psutil.swap_memory()
        rss = 0
        for process in [psutil.Process(), *psutil.Process().children(recursive=True)]:
            try:
                rss += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {
            "available_bytes": memory.available,
            "total_bytes": memory.total,
            "process_tree_rss_bytes": rss,
            "swap_used_bytes": swap.used,
            "swap_growth_bytes": max(0, swap.used - self._initial_swap),
        }

    def check_resources(self):
        settings = getattr(self.runner, "node_settings", None)
        if settings is None:
            return
        snapshot = self.resource_snapshot()
        if snapshot["available_bytes"] < settings.get("min_available_mib", 384) * 1024**2:
            raise HTTPException(503, "Inference paused: available memory is below the configured minimum")
        if snapshot["swap_growth_bytes"] > settings.get("max_swap_growth_mib", 128) * 1024**2:
            raise HTTPException(503, "Inference paused: swap growth exceeds the configured maximum")

    def storage_summary(self):
        result = []
        runtime = (self.root / "runtime").resolve()
        for relative in ("models", "engines", "tts", "node-assets", "nltk_data"):
            directory = runtime / relative
            total = count = 0
            if directory.is_dir() and not directory.is_symlink():
                for path in directory.rglob("*"):
                    if self._observation_stop.is_set():
                        return self._unknown_storage()
                    try:
                        if (
                            path.is_symlink()
                            or not path.resolve().is_relative_to(directory)
                            or not path.is_file()
                        ):
                            continue
                        total += path.stat().st_size
                        count += 1
                    except OSError:
                        continue
            result.append(
                {
                    "kind": relative,
                    "path": str(directory),
                    "used_bytes": total,
                    "file_count": count,
                    "protected": True,
                    "reclaimable_bytes": 0,
                }
            )
        return [*result, cache_summary(self.root)]

    async def clear_cache(self):
        self._require_idle_text()
        async with self.lock:
            if any(value is not False for value in [await self.runner.running(key) for key in RESOURCES]):
                raise HTTPException(
                    409, "Release all inference workers with idle before clearing their disk cache"
                )
            worker = asyncio.create_task(asyncio.to_thread(scan_cache, self.root, clear=True))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A cancelled to_thread await does not stop its filesystem work.
                # Retain the model lease until deletion has actually finished.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                worker.result()
                raise
            self._storage_observation.invalidate()
            return {
                key: result[key]
                for key in (
                    "removed_files",
                    "removed_bytes",
                    "protected",
                    "protected_count",
                    "errors",
                    "error_count",
                    "complete",
                    "scanned_entries",
                )
            }

    async def capabilities(self):
        status = await self.observe()
        self._metadata_observation.request()
        self._storage_observation.request()
        installed = self._installed_profiles
        profiles = {}
        for key, resource in RESOURCES.items():
            properties = self.properties.get(key, {})
            loaded = status["providers"][key]["running"] is True
            profiles[key] = {
                "alias": resource.alias,
                "installed": key in installed if installed is not None else True if loaded else None,
                "loaded": loaded,
                "native_context_tokens": self.natives.get(key),
                "context_tokens": properties.get("default_generation_settings", {}).get("n_ctx")
                if loaded
                else None,
                "vision": bool(loaded and properties.get("modalities", {}).get("vision") is True),
                "supports_vision": key in VISION_ROUTES.values(),
                "tts": key == "tts",
                "quantization": {GEMMA_KEY: "Q4_0", GROUNDED_DAFEIYU_KEY: "Q4_K_M", DAFEIYU_9B_KEY: "Q4_K_M"}.get(key),
                "projector_quantization": {GEMMA_KEY: "Q8_0", GROUNDED_DAFEIYU_KEY: "F16", DAFEIYU_9B_KEY: "Q8_0"}.get(key),
                **(
                    self._artifact_identifiers.get(key)
                    or {"model_sha256": None, "projector_sha256": None, "runtime_sha256": None}
                ),
            }
        return {
            "protocol": "tuber-inference-node",
            "protocol_version": 1,
            "state": status["state"],
            "mode": status["mode"],
            "model": status["model"],
            "selected_provider": self.selected,
            "profiles": profiles,
            "requested_context_tokens": self.context_tokens,
            "resource": self.resource_snapshot(),
            "storage": self._storage_observation.value or self._unknown_storage(),
            "storage_status": self._storage_observation.status(),
            "metadata_status": self._metadata_observation.status(),
            "installation_error": self._metadata_observation.error,
        }

    def request_inventory_refresh(self):
        self._metadata_observation.invalidate()
        self._storage_observation.invalidate()
        self._metadata_observation.request()
        self._storage_observation.request()
        return {
            "metadata_status": self._metadata_observation.status(),
            "storage_status": self._storage_observation.status(),
        }

    def model_path(self, key):
        if hasattr(self.runner, "model_path"):
            return self.runner.model_path(key)
        return (self.root / "runtime/models" / RESOURCES[key].model).resolve()

    async def initialize(self):
        async with self.lock:
            profiles = await self._profiles()
            for key in profiles:
                resource = RESOURCES[key]
                if resource.model:
                    path = await asyncio.to_thread(self.model_path, key)
                    if not path.is_relative_to(self.root):
                        raise RuntimeError("Selected model path is outside project")
                    self.natives[key] = await asyncio.to_thread(native_context, path)
            await asyncio.to_thread(self._validate_context, self.selected)
            await self._apply_mode(self.mode)

    def _validate_context(self, key):
        if not RESOURCES[key].model:
            return
        if key not in self.natives:
            self.natives[key] = native_context(self.model_path(key))
        if self.natives[key] < self.context_tokens:
            raise RuntimeError("Selected model does not natively support the requested context")

    async def _apply_mode(self, mode):
        keep = {self.selected, "tts"} if mode == "live" else {self.selected}
        await self._establish_residents(keep)

    async def _establish_residents(self, residents):
        for key in RESOURCES:
            if key not in residents:
                await self.runner.stop(key)
        if "tts" in residents:
            if not await self.runner.running("tts"):
                # The BERT/voice cold-load peak must not overlap resident KV.
                # Keep an already-ready pair untouched during normal live use.
                for key in residents:
                    if RESOURCES[key].model:
                        await self.runner.stop(key)
            await self._activate("tts", keep=residents)
        for key in sorted(residents - {"tts"}):
            await self._activate(key, keep=residents)

    def _persist_configuration(self, update):
        path = self.configuration_path or self.root / "runtime/studio.json"
        if path.is_symlink():
            raise RuntimeError("Studio configuration must be a regular runtime file")
        source = path if path.exists() else self.root / "config/studio.example.json"
        data = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
        normalize_broker_llm(data)
        update(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".studio-mode-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                if path.exists():
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _persist_mode(self, mode):
        def update(data):
            if self.configuration_path is not None:
                data["mode"] = mode
                return
            profile = data.setdefault("inference_broker", {})
            if not isinstance(profile, dict):
                raise ValueError("Invalid inference broker configuration")
            profile["mode"] = mode

        self._persist_configuration(update)

    def _persist_context(self):
        def update(data):
            if self.configuration_path is not None:
                data["context_tokens"] = self.context_tokens
                return
            profile = data.setdefault("inference_broker", {})
            if not isinstance(profile, dict):
                raise ValueError("Invalid inference broker configuration")
            profile["context_tokens"] = self.context_tokens
            llm = data.setdefault("llm", {})
            if not isinstance(llm, dict):
                raise ValueError("Invalid language model configuration")
            if llm.get("kind", "llama_cpp") == "llama_cpp":
                llm["context_tokens"] = self.context_tokens

        self._persist_configuration(update)

    def _persist_selected(self, key):
        def update(data):
            alias = RESOURCES[key].alias or key
            if self.configuration_path is not None:
                data["operations_model"] = alias
                data.pop("model", None)
                return
            profile = data.setdefault("inference_broker", {})
            if not isinstance(profile, dict):
                raise ValueError("Invalid inference broker configuration")
            profile["model"] = alias
            llm = data.setdefault("llm", {})
            if not isinstance(llm, dict):
                raise ValueError("Invalid language model configuration")
            if llm.get("kind", "llama_cpp") == "llama_cpp":
                llm["model"] = alias
            operations = data.get("work_mode_profiles", {}).get("text", {}).get("llm")
            if operations is not None and operations.get("kind") == "llama_cpp":
                operations["model"] = alias

        self._persist_configuration(update)

    async def _restore(self, residents, active):
        await self._establish_residents(residents)
        self.active = active if active in residents else None

    @asynccontextmanager
    async def _residency_change(self):
        previous_mode, previous_active, previous_selected = self.mode, self.active, self.selected
        previous_context, previous_properties = self.context_tokens, self.properties.copy()
        residents = {key for key in RESOURCES if await self.runner.running(key)}
        try:
            yield
        except BaseException as failure:
            self.mode = previous_mode
            self.selected = previous_selected
            context_changed = self.context_tokens != previous_context
            self.context_tokens = self.runner.context_tokens = previous_context
            self.properties = {} if context_changed else previous_properties

            async def restore():
                if context_changed:
                    # A process with the same key may already be ready at the
                    # failed new window. It must exit before restoring old KV.
                    for key in RESOURCES:
                        await self.runner.stop(key)
                    self.properties = previous_properties
                await self._restore(residents, previous_active)

            rollback = asyncio.create_task(restore())
            while not rollback.done():
                try:
                    await asyncio.shield(rollback)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                rollback.result()
            except BaseException:
                self.state, self.error = (
                    "error",
                    "Resource transition failed; previous resources could not be restored",
                )
            else:
                self.state = "active" if residents else "cold"
                self.error = (
                    None if isinstance(failure, asyncio.CancelledError) else "Resource transition failed"
                )
            raise

    async def switch_mode(self, mode):
        if mode not in {"text", "live"}:
            raise HTTPException(422, "Mode must be text or live")
        async with self.lock:
            try:
                async with self._residency_change():
                    selected = self.live_selected if mode == "live" else self.operations_selected
                    if selected is not None:
                        await asyncio.to_thread(self._validate_context, selected)
                        self.selected = selected
                    await self._apply_mode(mode)
                    self._persist_mode(mode)
                    self.mode = mode
            except Exception:
                raise HTTPException(503, self.error) from None
        return await self.observe()

    async def switch_model(self, key):
        async with self.lock:
            await self._switch_model(key)
        return await self.observe()

    async def _switch_model(self, key):
        if key not in RESOURCES or not RESOURCES[key].model:
            raise HTTPException(422, f"Unknown model key {key}")
        if self.mode != "text":
            raise HTTPException(409, "Model switching requires text mode")
        if self.selected == key and self.active == key:
            return
        try:
            async with self._residency_change():
                await asyncio.to_thread(self._validate_context, key)
                self.selected = key
                await self._apply_mode(self.mode)
                self._persist_selected(key)
                self.operations_selected = key
        except Exception:
            raise HTTPException(503, self.error or "Model transition failed") from None

    def _require_idle_text(self):
        if self.lock.locked():
            raise HTTPException(409, "Inference is busy; retry after the current operation")
        if self.mode != "text":
            raise HTTPException(409, "This operation requires text mode")

    async def change_context(self, requested):
        requested = validate_context_tokens(requested)
        self._require_idle_text()
        async with self.lock:
            try:
                profiles = await self._profiles()
                # Check the artifact itself before stopping any resident resource.
                native = await asyncio.to_thread(lambda: native_context(self.model_path(self.selected)))
            except (OSError, RuntimeError, ValueError, KeyError, struct.error):
                raise HTTPException(503, "Selected model capacity could not be verified") from None
            if self.selected not in profiles:
                raise HTTPException(503, "Selected fixed inference profile is not installed")
            if requested > native:
                raise HTTPException(422, "Requested context exceeds the selected model's native context")
            self.natives[self.selected] = native
            try:
                async with self._residency_change():
                    for key in RESOURCES:
                        await self.runner.stop(key)
                    self.active = None
                    self.context_tokens = self.runner.context_tokens = requested
                    # A property from an earlier window is not proof of the new one.
                    self.properties = {}
                    await self._activate(self.selected)
                    result = await self.observe()
                    self._persist_context()
                    # No await after the atomic commit: cancellation before commit
                    # rolls back; a committed context returns as one complete change.
            except Exception:
                raise HTTPException(503, self.error or "Context transition failed") from None
        return result | {"busy": False}

    async def idle(self):
        self._require_idle_text()
        async with self.lock:
            try:
                async with self._residency_change():
                    for key in RESOURCES:
                        await self.runner.stop(key)
                    self.active, self.state, self.error = None, "cold", None
                    result = await self.observe()
            except Exception:
                raise HTTPException(503, self.error or "Could not release inference resources") from None
        return result | {"busy": False}

    async def _activate(self, key, *, keep=None):
        already_running = await self.runner.running(key)
        if not already_running:
            profiles = await self._profiles()
            if key not in profiles:
                raise RuntimeError("Requested fixed inference profile is not installed")
        await asyncio.to_thread(self._validate_context, key)
        self._target_touched = True
        # Confirm all other owned process groups have exited before starting the target.
        self.state = "switching"
        try:
            keep = keep if keep is not None else {key}
            for other in RESOURCES:
                if other not in keep:
                    await self.runner.stop(other)
            if not await self.runner.running(key):
                self.active = None
                await self.runner.start(key)
                self._asset_epoch += 1
                if hasattr(self.runner, "artifact_identifiers"):
                    self._artifact_identifiers[key] = await asyncio.to_thread(
                        self.runner.artifact_identifiers, key
                    )
            deadline = time.monotonic() + (300 if key == GEMMA_KEY else 180)
            while True:
                if not await self.runner.running(key):
                    raise RuntimeError("Requested inference process exited while loading")
                try:
                    response = await self.client.get(RESOURCES[key].url + RESOURCES[key].ready, timeout=5)
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Requested inference resource did not become ready") from None
                    await asyncio.sleep(0.5)
            resource = RESOURCES[key]
            if resource.alias:
                response = await self.client.get(resource.url + "/props", timeout=10)
                response.raise_for_status()
                properties = response.json()
                aliases = properties.get("model_alias")
                aliases = [aliases] if isinstance(aliases, str) else aliases
                if resource.alias not in (aliases or []):
                    raise RuntimeError("Loaded model alias does not match selected resource")
                if properties["default_generation_settings"]["n_ctx"] != self.context_tokens:
                    raise RuntimeError("Loaded model context does not match the requested context")
                self.properties[key] = properties
            self.active, self.state, self.error = key, "active", None
            self.loaded.add(key)
        except BaseException:
            self.error, self.state = "Inference resource activation failed", "error"
            # A failed or cancelled startup must not leave a loading GPU task.
            await asyncio.shield(self.runner.stop(key))
            self.active = None
            raise

    async def observe(self):
        observed = {}
        for key in RESOURCES:
            try:
                observed[key] = await self.runner.running(key)
            except Exception:
                observed[key] = None
        state, error = self.state, self.error
        if not self.lock.locked() and self.active and observed[self.active] is not True:
            state, error = "error", error or "Previously active inference process exited"
        if not self.lock.locked():
            running = {key for key, alive in observed.items() if alive is True}
            allowed = {self.selected, "tts"} if self.mode == "live" else running - {"tts"}
            if not running.issubset(allowed) or (self.mode == "text" and len(running) > 1):
                state, error = "error", "Unexpected concurrent inference processes"
            elif self.mode == "live" and running != allowed:
                state, error = "error", "Live mode resources are not both ready"
        configured_cache = getattr(self.runner, "kv_cache_type", "q8_0")
        cache_provider = (
            self.active
            if self.active and observed.get(self.active) and RESOURCES[self.active].model
            else self.selected
        )
        return {
            "state": state,
            "active_provider": self.active if observed.get(self.active) else None,
            "error": error,
            "mode": self.mode,
            "selected_provider": self.selected,
            "model": RESOURCES[self.selected].alias,
            "operations_provider": self.operations_selected or self.selected,
            "live_provider": self.live_selected or self.selected,
            "requested_context_tokens": self.context_tokens,
            "kv_cache_type": resource_cache_type(cache_provider, configured_cache),
            "cuda_managed_memory": getattr(self.runner, "cuda_managed_memory", False),
            "busy": self.lock.locked(),
            "providers": {
                key: {
                    "running": observed[key],
                    "is_sleeping": not observed[key],
                    "loaded_this_boot": key in self.loaded,
                    "native_context_tokens": self.natives.get(key),
                    "context_tokens": self.properties.get(key, {})
                    .get("default_generation_settings", {})
                    .get("n_ctx"),
                    "cache_type_k": resource_cache_type(key, configured_cache),
                    "cache_type_v": resource_cache_type(key, configured_cache),
                }
                for key in RESOURCES
            },
        }

    async def read(self, key, path):
        try:
            running = await self.runner.running(key)
        except RuntimeError:
            running = False
        if path == "props":
            if key not in self.properties:
                raise HTTPException(503, "Resource has not been loaded during this broker boot")
            return self.properties[key] | {"is_sleeping": not running, "active_provider": self.active}
        if key == "tts" and path == "docs" and running:
            try:
                response = await self.client.get(RESOURCES[key].url + "/docs", timeout=5)
                response.raise_for_status()
                if len(response.content) > 65_536:
                    raise ValueError("Oversized fixed speech health response")
                body = response.json()
                if not isinstance(body, dict) or body.get("status") not in {"ready", "ok"}:
                    raise ValueError("Speech is not ready")
                metadata = {
                    name: body[name]
                    for name in ("status", "backend", "voice_id", "bundle_identity")
                    if name in body
                }
                if any(not isinstance(value, str) or len(value) > 128 for value in metadata.values()):
                    raise ValueError("Invalid fixed speech health metadata")
            except (httpx.HTTPError, ValueError):
                raise HTTPException(
                    503, "The resident speech worker is not ready; check its assets and restart"
                ) from None
            return metadata | {"available": True, "is_sleeping": False, "active_provider": self.active}
        return {
            "status": "ok" if key in self.loaded else "cold",
            "available": key in self.loaded,
            "is_sleeping": not running,
            "active_provider": self.active,
        }

    async def forward_llm(self, key, path, body):
        async with self.lock:
            try:
                validate_llm_request(path, body, context_tokens=self.context_tokens)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            if body.get("stream", False) is not False:
                raise HTTPException(422, "Only buffered inference is supported")
            key = key or self.selected
            if key != self.selected:
                await self._switch_model(key)
            if path not in {"warmup", "tokenize"}:
                body["model"] = RESOURCES[key].alias
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            return await self._forward(key, path, data)

    async def forward(self, key, path, body):
        async with self.lock:
            return await self._forward(key, path, body)

    async def _forward(self, key, path, body):
        self.check_resources()
        if key == "tts" and self.mode != "live":
            raise HTTPException(409, "Switch to live mode before synthesizing speech")
        if self.mode == "live" and key not in {self.selected, "tts"}:
            raise HTTPException(409, "Live mode only permits the selected LLM and TTS")
        if self.mode == "text" and self.operations_selected is not None and key != self.selected:
            raise HTTPException(409, "Text mode only permits the selected operations model")
        self._target_touched = False
        if self.mode == "live":
            try:
                async with self._residency_change():
                    await self._apply_mode("live")
            except Exception:
                raise HTTPException(503, self.error) from None
            self.active = key
        try:
            if self.mode != "live":
                await self._activate(key)
            self.check_resources()
            if path == "warmup":
                return JSONResponse(await self.read(key, "props"))
            response = await self.client.post(
                RESOURCES[key].url + "/" + path,
                content=body,
                headers={"Content-Type": "application/json"},
            )
            # httpx post buffers the body before returning. The GPU lease is
            # held through this point even when the upstream sends chunked WAV.
            if response.status_code >= 500:
                # A failed CUDA worker must not remain advertised as ready.
                # A subsequent live request rebuilds TTS first when required.
                await self.runner.stop(key)
                self.active, self.state = None, "error"
                self.error = "Inference worker failed; the next request must reload it"
            return Response(
                response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/octet-stream"),
            )
        except HTTPException:
            raise
        except asyncio.CancelledError:
            self.state, self.error = "stopping", "Inference request was cancelled"
            cleanup = asyncio.create_task(self.runner.stop(key)) if self._target_touched else None
            if cleanup is None:
                self.active, self.state, self.error = None, "cold", None
                raise
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            self.active, self.state, self.error = None, "cold", None
            raise
        except Exception:
            self.state, self.error = "error", "Inference request failed"
            if self._target_touched:
                await self.runner.stop(key)
            self.active = None
            raise HTTPException(503, self.error) from None

    async def close(self):
        self._observation_stop.set()
        await asyncio.gather(self._metadata_observation.close(), self._storage_observation.close())
        await self.client.aclose()
        if hasattr(self.runner, "close"):
            await self.runner.close()


def inference_token(root: Path):
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / "inference-token"
    if path.is_symlink():
        raise RuntimeError("Inference token must be a regular project runtime file")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(32))
    path.chmod(0o600)
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("Inference token is invalid")
    return value


def allowed_tts_reference(root: Path, value) -> bool:
    """An operator-configured, existing project WAV is the only file input."""

    def reference(raw):
        try:
            return project_wav(root, raw)
        except (OSError, ValueError, RuntimeError):
            return None

    try:
        requested = reference(value)
        if requested is None:
            return False
        voices = json.loads((root / "config/voices.json").read_text(encoding="utf-8"))
        if not isinstance(voices, list):
            return False
        allowed = {
            reference(voice.get("server_reference_path")) for voice in voices if isinstance(voice, dict)
        }
        return requested in allowed
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


class InferenceGuard:
    """Authenticate without BaseHTTPMiddleware task/cancellation indirection."""

    def __init__(self, app, token):
        self.app, self.token = app, token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        bearer = headers.get(b"authorization", b"")
        bearer = bearer[7:] if bearer.startswith(b"Bearer ") else b""
        if not (
            secrets.compare_digest(headers.get(b"x-tuber-inference-key", b""), self.token)
            or secrets.compare_digest(bearer, self.token)
        ):
            response = JSONResponse({"detail": "Inference authentication required"}, status_code=401)
            return await response(scope, receive, send)
        length = headers.get(b"content-length")
        limit = MAX_VISION_BODY if scope.get("path", "").startswith("/vlm/") else MAX_BODY
        if length:
            try:
                if int(length) < 0 or int(length) > limit:
                    response = JSONResponse(
                        {"detail": "Request body exceeds the endpoint limit"}, status_code=413
                    )
                    return await response(scope, receive, send)
            except ValueError:
                response = JSONResponse({"detail": "Invalid content length"}, status_code=400)
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(root: Path | None = None, *, broker=None, warm_start=True, node_config: Path | None = None):
    root = (root or Path(os.getenv("TUBER_ROOT", str(Path(__file__).resolve().parents[2])))).resolve()
    configuration = node_config or root / "runtime/studio.json"
    if node_config is not None and not configuration.is_file():
        raise ValueError("Inference node configuration is missing")
    if not configuration.exists():
        configuration = root / "config/studio.example.json"
    data = json.loads(configuration.read_text(encoding="utf-8")) if configuration.exists() else {}
    normalize_broker_llm(data)
    if node_config is not None:
        node = data
        for field, default in (("min_available_mib", 384), ("max_swap_growth_mib", 128)):
            value = node.get(field, default)
            if type(value) is not int or value < 0:
                raise ValueError("Inference resource thresholds must be nonnegative integer MiB")
        if node.get("kv_cache_type", "q4_0") != "q4_0":
            raise ValueError("The measured inference-node profile requires Q4 KV")
        if type(node.get("gemma_vision_only", True)) is not bool:
            raise ValueError("Gemma vision-only flag must be boolean")
        mode = node.get("mode", "text")
        selected_alias = (
            node.get("live_model", DAFEIYU_9B_MODEL)
            if mode == "live"
            else node.get("operations_model", DAFEIYU_9B_MODEL)
        )
        data = {
            "inference_broker": node,
            "llm": {
                "kind": "llama_cpp",
                "model": selected_alias,
                "context_tokens": node.get("context_tokens", DAFEIYU_9B_CONTEXT if selected_alias == DAFEIYU_9B_MODEL else 131072),
            },
            "tts": node.get("tts", {}) | {"kind": "gpt_sovits"},
        }
    profile = data.get("inference_broker", {})
    if not isinstance(profile, dict):
        raise ValueError("Invalid inference broker configuration")
    llm = data.get("llm", {})
    if not isinstance(llm, dict) or llm.get("kind", "llama_cpp") not in (
        "llama_cpp",
        "openai_compatible",
        "demo",
    ):
        raise ValueError("Invalid language model configuration")
    local = llm.get("kind", "llama_cpp") == "llama_cpp"
    settings = llm if local else profile
    selected_alias = settings.get("model", DAFEIYU_9B_MODEL)
    default_context = DAFEIYU_9B_CONTEXT if selected_alias == DAFEIYU_9B_MODEL else 131072
    runner = ProcessRunner(
        root,
        kv_cache_type=profile.get("kv_cache_type", "q4_0" if selected_alias == DAFEIYU_9B_MODEL else "q8_0"),
        cuda_managed_memory=profile.get("cuda_managed_memory", False),
        context_tokens=settings.get("context_tokens", default_context),
        node_settings=profile if node_config is not None else None,
    )
    selected = next((key for key, resource in RESOURCES.items() if resource.alias == selected_alias), None)
    mode = profile.get("mode", "text") if local else "text"
    if data.get("tts", {}).get("kind") in {"openai_compatible", "prerecorded"}:
        mode = "text"
    extra = {}
    if node_config is not None:
        for field, alias in (
            ("operations_selected", profile.get("operations_model", DAFEIYU_9B_MODEL)),
            ("live_selected", profile.get("live_model", DAFEIYU_9B_MODEL)),
        ):
            key = next((key for key, value in RESOURCES.items() if value.alias == alias), None)
            if key is None:
                raise ValueError("Unknown mode-specific inference profile")
            extra[field] = key
        extra["configuration_path"] = node_config
    manager = broker or InferenceBroker(root, runner=runner, mode=mode, selected=selected, **extra)
    token = inference_token(root)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if warm_start and local:
                try:
                    await manager.initialize()
                except (RuntimeError, OSError, ValueError, KeyError, httpx.HTTPError, struct.error):
                    manager.state, manager.active = "error", None
                    manager.error = manager.error or "Inference initialization failed; inspect resource logs"
                    if hasattr(manager.runner, "close"):
                        await manager.runner.close()
            yield
        finally:
            await manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broker = manager
    app.add_middleware(InferenceGuard, token=token)

    @app.get("/health")
    async def health():
        return await manager.observe()

    @app.get("/capabilities")
    async def capabilities():
        return await manager.capabilities()

    @app.get("/props")
    async def props():
        return await manager.read(manager.selected, "props")

    async def read_body(request, limit=MAX_BODY):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, "Request body exceeds the endpoint limit")
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "JSON object required") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "JSON object required")
        return body, bytes(data)

    @app.post("/capabilities/refresh")
    async def refresh_inventory(request: Request):
        body, _ = await read_body(request)
        if body:
            raise HTTPException(422, "Inventory refresh accepts only an empty object")
        return manager.request_inventory_refresh()

    async def cancellable(request, operation):
        async def disconnected():
            # The request body has been fully consumed. Waiting on ASGI receive
            # observes the actual socket disconnect and remains cancellable;
            # Request.is_disconnected's inner cancel scope can swallow a task cancel.
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        work = asyncio.create_task(operation)
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                return Response(status_code=499)
            return await work
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    @app.post("/mode")
    async def change_mode(request: Request):
        body, _ = await read_body(request)
        if set(body) != {"mode"} or body["mode"] not in ("text", "live"):
            raise HTTPException(422, "Mode must be text or live")
        return await cancellable(request, manager.switch_mode(body["mode"]))

    @app.post("/context")
    async def change_context(request: Request):
        body, _ = await read_body(request)
        if set(body) != {"context_tokens"}:
            raise HTTPException(422, "Only context_tokens is accepted")
        try:
            requested = validate_context_tokens(body["context_tokens"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return await cancellable(request, manager.change_context(requested))

    @app.post("/idle")
    async def idle(request: Request):
        body, _ = await read_body(request)
        if body:
            raise HTTPException(422, "Idle accepts only an empty object")
        return await cancellable(request, manager.idle())

    @app.post("/cache/clear")
    async def clear_cache(request: Request):
        body, _ = await read_body(request)
        if body:
            raise HTTPException(422, "Cache cleanup accepts only an empty object")
        return await cancellable(request, manager.clear_cache())

    @app.api_route("/tts", methods=["GET", "POST"])
    async def tts_root(request: Request):
        return await tts("", request)

    @app.api_route("/tts/{path:path}", methods=["GET", "POST"])
    async def tts(path: str, request: Request):
        if request.method == "GET":
            if path in {"", "tts", "docs", "health"}:
                return await manager.read("tts", "health" if path == "health" else "docs")
            raise HTTPException(404)
        if path not in {"", "tts"}:
            raise HTTPException(404)
        body, _ = await read_body(request)
        try:
            validate_speech_request(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if not allowed_tts_reference(root, body.get("ref_audio_path")):
            raise HTTPException(422, "TTS reference must be the configured project voice")
        body["ref_audio_path"] = str(project_wav(root, body["ref_audio_path"]))
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return await cancellable(request, manager.forward("tts", "tts", data))

    async def llm_post(request, path=None):
        if path == "chat/completions":
            path = "v1/chat/completions"
        if path is not None and path not in LLM_POSTS:
            raise HTTPException(404)
        body, _ = await read_body(request)
        if path is None:
            path = "v1/chat/completions" if "messages" in body else "completion"
        target = resolve_llm_resource_key(body["model"]) if "model" in body else None
        if path in {"warmup", "tokenize"}:
            body.pop("model", None)
        return await cancellable(request, manager.forward_llm(target, path, body))

    @app.api_route("/llm", methods=["GET", "POST"])
    async def llm_root(request: Request):
        if request.method == "GET":
            return await manager.read(manager.selected, "health")
        return await llm_post(request)

    @app.api_route("/llm/{path:path}", methods=["GET", "POST"])
    async def llm(path: str, request: Request):
        if request.method == "POST":
            return await llm_post(request, path)
        if path in {"health", "props"}:
            return await manager.read(manager.selected, path)
        if path in {"models", "v1/models"}:
            return await list_models()
        raise HTTPException(404)

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def openai_chat_completions(request: Request):
        return await llm_post(request, "v1/chat/completions")

    @app.get("/v1/models")
    @app.get("/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "owned_by": "tuber-studio",
                    "active": key == manager.selected,
                }
                for key, model in LLM_MODELS.items()
            ],
        }

    @app.post("/model")
    @app.post("/select")
    async def select_model(request: Request):
        body, _ = await read_body(request)
        target = resolve_llm_resource_key(body.get("model", body.get("key")))
        return await cancellable(request, manager.switch_model(target))

    @app.api_route("/vlm/{profile}/{path:path}", methods=["GET", "POST"])
    async def vision(profile: str, path: str, request: Request):
        key = VISION_ROUTES.get(profile)
        if key is None:
            raise HTTPException(404)
        if request.method == "GET":
            if path in {"props", "v1/props", "health"}:
                return await manager.read(key, "props" if path.endswith("props") else "health")
            if path == "v1/models":
                properties = await manager.read(key, "props")
                if properties.get("is_sleeping"):
                    raise HTTPException(503, "The vision model is not resident")
                return {"object": "list", "data": [{"id": RESOURCES[key].alias, "object": "model"}]}
            raise HTTPException(404)
        if path != "v1/chat/completions":
            raise HTTPException(404)
        body, data = await read_body(request, MAX_VISION_BODY)
        try:
            validate_vision_request(body, context_tokens=manager.context_tokens)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if body.get("stream", False) is not False:
            raise HTTPException(422, "Only buffered inference is supported")
        if body.get("model", RESOURCES[key].alias) != RESOURCES[key].alias:
            raise HTTPException(422, "Model does not match typed resource")
        return await cancellable(request, manager.forward(key, path, data))

    return app


def main():
    import os

    import uvicorn

    host = os.environ.get("TUBER_INFERENCE_HOST", "0.0.0.0")
    uvicorn.run(create_app(), host=host, port=9012, access_log=False, ws="none")


if __name__ == "__main__":
    main()
