"""Container-local inference broker with fixed destinations and exclusive GPU ownership."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import struct
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

MAX_BODY = 2 * 1024 * 1024
HEADER = "X-Tuber-Inference-Key"


@dataclass(frozen=True)
class Resource:
    url: str
    ready: str
    alias: str | None = None
    model: str | None = None
    optional: bool = False


RESOURCES = {
    "llm2b": Resource("http://127.0.0.1:9013", "/health", "MiniCPM5-2B", "MiniCPM5-2B-Q4_K_M.gguf"),
    "llm1b": Resource("http://127.0.0.1:9010", "/health", "MiniCPM5-1B", "MiniCPM5-1B-Q4_K_M.gguf", True),
    "llm1bq8": Resource(
        "http://127.0.0.1:9014", "/health", "MiniCPM5-1B-Q8_0", "MiniCPM5-1B-Q8_0.gguf", True
    ),
    "tts": Resource("http://127.0.0.1:9011", "/docs"),
}
LLM_POSTS = {"apply-template", "tokenize", "v1/chat/completions", "completion"}


def native_context(path: Path) -> int:
    """Read the selected GGUF's declared context without loading model tensors."""
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
        for _ in range(number("Q")):
            key, kind = string(), number("I")
            if key == "llama.context_length":
                return int(number(formats[kind]))
            skip(kind)
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
    ):
        if kv_cache_type not in {"q8_0", "q4_0"} or type(cuda_managed_memory) is not bool:
            raise ValueError("Invalid fixed inference allocation profile")
        self.root = root.resolve()
        self.llama_binary = llama_binary or Path("/opt/llama/bin/llama-server")
        self.tts_python = tts_python or Path("/opt/studio-venv/bin/python")
        self.processes = {}
        self.logs = {}
        self.kv_cache_type, self.cuda_managed_memory = kv_cache_type, cuda_managed_memory

    def command(self, key):
        resource = RESOURCES[key]
        if resource.model:
            cache = self.kv_cache_type if key == "llm2b" else "q8_0"
            return [
                str(self.llama_binary),
                "-m",
                str(self.root / "runtime/models" / resource.model),
                "--alias",
                resource.alias,
                "--host",
                "127.0.0.1",
                "--port",
                str(httpx.URL(resource.url).port),
                "--jinja",
                "--ctx-size",
                "131072",
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
                "off",
                "--temp",
                "1.0" if key == "llm2b" else "0.7",
                "--top-p",
                "0.95",
                "--min-p",
                "0.0",
                "--batch-size",
                "256",
                "--ubatch-size",
                "64",
                "--cache-ram",
                "0",
                "--no-mmap",
            ]
        return [
            str(self.tts_python),
            str(self.root / "tools/jetson/tts/serve.py"),
            "-c",
            str(self.root / "runtime/tts/tts_infer.yaml"),
            "-a",
            "127.0.0.1",
            "-p",
            "9011",
        ]

    async def validate_all(self):
        profiles = {}
        for key, resource in RESOURCES.items():
            model = (self.root / "runtime/models" / resource.model).resolve() if resource.model else None
            if model is not None and not model.is_file() and resource.optional:
                continue
            required = (
                [model, self.llama_binary]
                if model is not None
                else [
                    self.tts_python,
                    self.root / "tools/jetson/tts/serve.py",
                    self.root / "runtime/tts/tts_infer.yaml",
                ]
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

    async def start(self, key):
        profiles = await self.validate_all()
        if key not in profiles:
            raise RuntimeError("Optional fixed inference profile is not installed")
        await self.stop(key)
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
    def __init__(self, root: Path, runner=None, client=None, *, mode="exclusive"):
        if mode != "exclusive":
            raise ValueError("Unknown inference resource mode")
        self.root = root.resolve()
        self.runner = runner or ProcessRunner(self.root)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(1200, connect=10), trust_env=False)
        self.lock = asyncio.Lock()
        self.active = None
        self.state = "cold"
        self.error = None
        self.properties = {}
        self.natives = {}
        self.loaded = set()
        self.mode = mode
        self._target_touched = False

    async def initialize(self):
        async with self.lock:
            profiles = await self.runner.validate_all()
            for key in profiles:
                resource = RESOURCES[key]
                if resource.model:
                    path = (self.root / "runtime/models" / resource.model).resolve()
                    if not path.is_relative_to(self.root):
                        raise RuntimeError("Selected model path is outside project")
                    self.natives[key] = native_context(path)
                    if self.natives[key] < 131072:
                        raise RuntimeError("Selected model does not natively support 128K")
            await self._activate("llm2b")

    async def _activate(self, key):
        profiles = await self.runner.validate_all()
        if key not in profiles:
            raise RuntimeError("Requested fixed inference profile is not installed")
        self._target_touched = True
        if RESOURCES[key].model and key not in self.natives:
            self.natives[key] = native_context(self.root / "runtime/models" / RESOURCES[key].model)
            if self.natives[key] < 131072:
                raise RuntimeError("Selected model does not natively support 128K")
        # Confirm all other owned process groups have exited before starting the target.
        self.state = "switching"
        try:
            keep = {key}
            for other in RESOURCES:
                if other not in keep:
                    await self.runner.stop(other)
            if not await self.runner.running(key):
                self.active = None
                await self.runner.start(key)
            deadline = time.monotonic() + 180
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
                if properties["default_generation_settings"]["n_ctx"] != 131072:
                    raise RuntimeError("Loaded model context is not 131072")
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
        if sum(running is True for running in observed.values()) > 1:
            state, error = "error", "Unexpected concurrent inference processes"
        return {
            "state": state,
            "active_provider": self.active if observed.get(self.active) else None,
            "error": error,
            "mode": self.mode,
            "kv_cache_type": getattr(self.runner, "kv_cache_type", "q8_0"),
            "cuda_managed_memory": getattr(self.runner, "cuda_managed_memory", False),
            "busy": self.lock.locked(),
            "providers": {
                key: {
                    "running": observed[key],
                    "is_sleeping": not observed[key],
                    "loaded_this_boot": key in self.loaded,
                    "native_context_tokens": self.natives.get(key),
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
        return {
            "status": "ok" if key in self.loaded else "cold",
            "available": key in self.loaded,
            "is_sleeping": not running,
            "active_provider": self.active,
        }

    async def forward(self, key, path, body):
        async with self.lock:
            self._target_touched = False
            try:
                await self._activate(key)
                if path == "warmup":
                    return JSONResponse(await self.read(key, "props"))
                response = await self.client.post(
                    RESOURCES[key].url + "/" + path,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                # httpx post buffers the body before returning. The GPU lease is
                # held through this point even when the upstream sends chunked WAV.
                return Response(
                    response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type", "application/octet-stream"),
                )
            except asyncio.CancelledError:
                self.state, self.error = "stopping", "Inference request was cancelled"
                cleanup = asyncio.create_task(self.runner.stop(key)) if self._target_touched else None
                if cleanup is None:
                    raise
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                self.active, self.state = None, "cold"
                raise
            except Exception:
                self.state, self.error = "error", "Inference request failed"
                if self._target_touched:
                    await self.runner.stop(key)
                self.active = None
                raise HTTPException(503, self.error) from None

    async def close(self):
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
        if not isinstance(raw, str) or not raw or "://" in raw:
            return None
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        if not any(
            path.is_relative_to(base.resolve()) for base in (root / "assets", root / "runtime/tts/ref")
        ):
            return None
        if path.suffix.lower() != ".wav" or not path.is_file():
            return None
        with path.open("rb") as audio:
            header = audio.read(12)
        return path if header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE" else None

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
    except (OSError, ValueError, TypeError):
        return False


class InferenceGuard:
    """Authenticate without BaseHTTPMiddleware task/cancellation indirection."""

    def __init__(self, app, token):
        self.app, self.token = app, token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if not secrets.compare_digest(headers.get(b"x-tuber-inference-key", b""), self.token):
            response = JSONResponse({"detail": "Inference authentication required"}, status_code=401)
            return await response(scope, receive, send)
        length = headers.get(b"content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > MAX_BODY:
                    response = JSONResponse({"detail": "Request body exceeds 2MiB"}, status_code=413)
                    return await response(scope, receive, send)
            except ValueError:
                response = JSONResponse({"detail": "Invalid content length"}, status_code=400)
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(root: Path | None = None, *, broker=None, warm_start=True):
    root = (root or Path(os.getenv("TUBER_ROOT", str(Path(__file__).resolve().parents[2])))).resolve()
    configuration = root / "runtime/studio.json"
    if not configuration.exists():
        configuration = root / "config/studio.example.json"
    data = json.loads(configuration.read_text(encoding="utf-8")) if configuration.exists() else {}
    profile = data.get("inference_broker", {})
    if not isinstance(profile, dict):
        raise ValueError("Invalid inference broker configuration")
    runner = ProcessRunner(
        root,
        kv_cache_type=profile.get("kv_cache_type", "q8_0"),
        cuda_managed_memory=profile.get("cuda_managed_memory", False),
    )
    manager = broker or InferenceBroker(root, runner=runner, mode=profile.get("mode", "exclusive"))
    token = inference_token(root)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if warm_start:
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

    async def dispatch(request: Request, key: str, path: str):
        if request.method == "GET":
            if (key.startswith("llm") and path in {"health", "props"}) or (key == "tts" and path == "docs"):
                return await manager.read(key, path)
            raise HTTPException(404)
        if not (
            (key.startswith("llm") and path in LLM_POSTS | {"warmup"}) or (key == "tts" and path == "tts")
        ):
            raise HTTPException(404)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise HTTPException(413, "Request body exceeds 2MiB")
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "JSON object required") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "JSON object required")
        if body.get("stream", False) is not False or body.get("streaming_mode", False) is not False:
            raise HTTPException(422, "Only buffered inference is supported")
        if key.startswith("llm") and body.get("model", RESOURCES[key].alias) != RESOURCES[key].alias:
            raise HTTPException(422, "Model does not match typed resource")
        if key == "tts" and (
            not allowed_tts_reference(root, body.get("ref_audio_path")) or body.get("aux_ref_audio_paths")
        ):
            raise HTTPException(422, "TTS reference must be the configured project voice")

        async def disconnected():
            # The request body has been fully consumed. Waiting on ASGI receive
            # observes the actual socket disconnect and remains cancellable;
            # Request.is_disconnected's inner cancel scope can swallow a task cancel.
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        work = asyncio.create_task(manager.forward(key, path, bytes(data)))
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

    @app.api_route("/llm/{size}/{path:path}", methods=["GET", "POST"])
    async def llm(size: str, path: str, request: Request):
        if size not in {"1b", "2b", "1bq8"}:
            raise HTTPException(404)
        return await dispatch(request, "llm" + size, path)

    @app.api_route("/tts/{path:path}", methods=["GET", "POST"])
    async def tts(path: str, request: Request):
        return await dispatch(request, "tts", path)

    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=9012, access_log=False, ws="none")


if __name__ == "__main__":
    main()
