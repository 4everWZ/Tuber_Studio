"""Offline image bootstrap and observed readiness; legacy stack stays independent.

The image supplies a controlled manifest and immutable payload. Runtime files
are published without replacing existing data. Only a once-per-boot typed mode
request may establish resources; Docker health checks are strictly observers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import stat
import tempfile
import time
from pathlib import Path, PurePosixPath

import httpx

ROOT = Path("/workspace/tuber")
BUILTIN = Path("/opt/tuber-builtin")
DEADLINE_SECONDS = 420
CHUNK_BYTES = 1024 * 1024
TOKENS = ("admin-token", "overlay-token", "inference-token")
PRESERVE = {"studio.json", "tts/tts_infer.yaml"}
RESERVED = {
    *TOKENS,
    "provider-credentials.json",
    "bilibili-credentials.json",
    "studio.sqlite3",
    "portable-status.json",
}
LOGGER = logging.getLogger(__name__)


def _deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Portable initialization deadline reached")


def _checked(path, boundary):
    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    if ".." in path.parts or not path.is_relative_to(boundary):
        raise ValueError("Portable file escaped its fixed directory")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Portable paths cannot use symbolic links or junctions")
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("Resolved portable file escaped its directory")
    return path


def _open_regular(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Portable input must be a regular file")
    return os.fdopen(descriptor, "rb")


def _advise(stream):
    if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED"):
        try:
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def _hash_file(path, *, deadline=None):
    digest, count = hashlib.sha256(), 0
    with _open_regular(path) as stream:
        try:
            before = os.fstat(stream.fileno())
            while chunk := stream.read(CHUNK_BYTES):
                _deadline(deadline)
                digest.update(chunk)
                count += len(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("Portable asset changed while being verified")
        finally:
            _advise(stream)
    return count, digest.hexdigest()


def _small_json(path, limit=4 * 1024 * 1024):
    with _open_regular(path) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Portable metadata exceeded its size limit")
    return json.loads(data)


def _manifest(builtin):
    builtin = _checked(builtin, builtin)
    manifest = _small_json(_checked(builtin / "manifest.json", builtin))
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "files"}
        or type(manifest["format"]) is not int
        or manifest["format"] != 1
    ):
        raise ValueError("Unsupported portable manifest")
    if not isinstance(manifest["files"], list):
        raise ValueError("Portable manifest files must be a list")
    entries, paths = [], set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) - {"path", "bytes", "sha256", "mode", "policy"}:
            raise ValueError("Unsupported portable file entry")
        name = entry.get("path")
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or ":" in name
            or "\0" in name
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name
            or name == "."
            or any(part in RESERVED for part in PurePosixPath(name).parts)
        ):
            raise ValueError("Invalid or reserved portable asset path")
        if name in paths or any(parent.as_posix() in paths for parent in PurePosixPath(name).parents):
            raise ValueError("Duplicate or overlapping portable paths")
        if any(existing.startswith(name + "/") for existing in paths):
            raise ValueError("Overlapping portable paths")
        policy = entry.get("policy", "immutable")
        if policy not in {"immutable", "preserve"} or (policy == "preserve" and name not in PRESERVE):
            raise ValueError("Only Operator studio/YAML configuration can use preserve policy")
        if (
            type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or type(entry.get("mode")) is not int
            or entry["mode"] not in {0o444, 0o600}
        ):
            raise ValueError("Invalid portable asset identity or permissions")
        paths.add(name)
        entries.append(entry | {"policy": policy})
    return entries


def _existing(path, entry, deadline):
    if not path.exists():
        return False
    if not path.is_file():
        raise ValueError("Existing portable asset is not a regular file")
    if entry["policy"] == "immutable" and _hash_file(path, deadline=deadline) != (
        entry["bytes"],
        entry["sha256"],
    ):
        raise ValueError("Existing immutable asset differs; it has been preserved")
    return True


def _copy_missing(source, target, entry, runtime, builtin, deadline):
    source = _checked(source, builtin)
    target = _checked(target, runtime)
    if _existing(target, entry, deadline):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    _checked(target.parent, runtime)
    descriptor, temporary = tempfile.mkstemp(prefix=".tuber-init-", dir=target.parent)
    temporary = Path(temporary)
    published = False
    try:
        digest, count = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as output, _open_regular(source) as original:
            before = os.fstat(original.fileno())
            try:
                while chunk := original.read(CHUNK_BYTES):
                    _deadline(deadline)
                    output.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
                output.flush()
                os.fsync(output.fileno())
                after = os.fstat(original.fileno())
                if (count, digest.hexdigest()) != (entry["bytes"], entry["sha256"]):
                    raise ValueError("Builtin payload does not match its manifest")
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError("Builtin payload changed during initialization")
                _advise(output)
            finally:
                _advise(original)
        if os.name != "nt":
            temporary.chmod(entry["mode"])
        _checked(target, runtime)
        try:
            # link publishes atomically and refuses an existing destination;
            # rename/replace would overwrite a concurrent initializer's data.
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _checked(target, runtime)
            _existing(target, entry, deadline)
            return False
        published = True
        return True
    finally:
        temporary.unlink(missing_ok=True)
        if published and os.name == "nt":
            # Windows readonly is an inode attribute shared by hard links; set
            # it after removing our temporary link. Linux publishes full mode.
            _checked(target, runtime).chmod(entry["mode"])


def _token(runtime, name):
    target = _checked(runtime / name, runtime)
    if target.exists():
        _read_token(runtime, name)
        return False
    descriptor, name = tempfile.mkstemp(prefix=".tuber-credential-", dir=runtime)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(secrets.token_urlsafe(32).encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        _checked(target, runtime)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _read_token(runtime, target.name)
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _read_token(runtime, name="inference-token"):
    path = _checked(runtime / name, runtime)
    with _open_regular(path) as stream:
        if os.name == "posix" and os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("Runtime credentials must remain private")
        data = stream.read(257)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", data):
        raise ValueError("Invalid runtime credential file")
    return data.decode("ascii")


def initialize_runtime(builtin=BUILTIN, runtime=ROOT / "runtime", *, deadline=None):
    """Install only missing checked files; preserve and validate existing data."""
    builtin, runtime = Path(builtin).absolute(), Path(runtime).absolute()
    entries = _manifest(builtin)
    _checked(runtime, runtime).mkdir(parents=True, exist_ok=True, mode=0o700)
    copied, preserved = [], []
    for entry in entries:
        _deadline(deadline)
        source = _checked(builtin / "files" / entry["path"], builtin)
        target = _checked(runtime / entry["path"], runtime)
        added = _copy_missing(source, target, entry, runtime, builtin, deadline)
        (copied if added else preserved).append(entry["path"])
    created_tokens = [name for name in TOKENS if _token(runtime, name)]
    return {"copied": copied, "preserved": preserved, "created_credentials": created_tokens}


def _configuration(root):
    data = _small_json(_checked(root / "runtime/studio.json", root))
    llm, broker = data.get("llm", {}), data.get("inference_broker", {})
    mode = broker.get("mode", "text")
    if mode not in {"text", "live"}:
        raise ValueError("Portable startup requires text or live resource mode")
    local = llm.get("kind", "llama_cpp") == "llama_cpp"
    if not local or data.get("tts", {}).get("kind") != "gpt_sovits":
        mode = "text"
    return data, local, mode


async def _json(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError("Internal observer response exceeded its size limit")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Internal observer did not return an object")
    return body


async def observe_ready(client, root=ROOT):
    """Read-only check of current configuration, actual residency and identity."""
    from .inference import RESOURCES

    root = Path(root)
    data, local, mode = _configuration(root)
    response = await client.get("http://127.0.0.1:9020/", timeout=3)
    response.raise_for_status()
    if data.get("inference_broker", {}).get("enabled") is not True:
        return {"ready": True, "scope": "web and independently managed providers"}
    health = await _json(client, "GET", "/health", timeout=5)
    if health.get("error") or health.get("state") in {"error", "stopping"}:
        raise ValueError("Inference broker reports a resource error")
    providers = health.get("providers", {})
    running = {name for name, provider in providers.items() if provider.get("running") is True}
    if not local:
        if running or health.get("busy"):
            raise ValueError("External-provider startup has unexpected local workers")
        return {"ready": True, "scope": "web and external provider configuration", "mode": "text"}
    selected = next(
        (key for key, resource in RESOURCES.items() if resource.alias == data["llm"].get("model")), None
    )
    if selected is None:
        raise ValueError("Configured local model is not a fixed resource")
    needed = {selected}
    tts = data.get("tts", {})
    if mode == "live":
        if tts.get("kind") != "gpt_sovits":
            raise ValueError("Local live residency requires its configured local speech backend")
        needed.add("tts")
    if health.get("mode") != mode or health.get("selected_provider") != selected or running != needed:
        raise ValueError("Actual workers do not match the configured resource mode")
    if not health.get("busy") and health.get("state") != "active":
        raise ValueError("Configured local resources are not active")
    upstream = await _json(client, "GET", RESOURCES[selected].url + "/health", timeout=5)
    if upstream.get("status") != "ok":
        raise ValueError("The actual local model server is not ready")
    props = await _json(client, "GET", f"/llm/{selected[3:]}/props", timeout=5)
    aliases = props.get("model_alias")
    aliases = [aliases] if isinstance(aliases, str) else aliases
    window = data["llm"]["context_tokens"]
    if (
        props.get("is_sleeping") is not False
        or data["llm"]["model"] not in (aliases or [])
        or props.get("default_generation_settings", {}).get("n_ctx") != window
        or providers[selected].get("native_context_tokens", 0) < window
    ):
        raise ValueError("Actual local model identity or context is not ready")
    result = {"ready": True, "mode": mode, "model": data["llm"]["model"], "context_tokens": window}
    if mode == "live":
        speech = await _json(client, "GET", "/tts/docs", timeout=5)
        voice_id = tts.get("voice_id")
        if voice_id is None:
            voices = _small_json(_checked(root / "config/voices.json", root))
            voice_id = next((voice["id"] for voice in voices if voice.get("provider") == "gpt_sovits"), None)
        backend = tts.get("local_backend", "compact")
        actual_backend = "gpt_sovits_v2_compact" if backend == "compact" else backend
        if (
            speech.get("status") != "ready"
            or speech.get("is_sleeping") is not False
            or speech.get("voice_id") != voice_id
            or speech.get("backend") != actual_backend
            or (
                backend == "genie_onnx_int8"
                and not re.fullmatch(r"[0-9a-f]{64}", speech.get("bundle_identity", ""))
            )
        ):
            raise ValueError("Actual local voice or bundle identity is not ready")
        result.update(voice_id=voice_id, backend=actual_backend)
        if "bundle_identity" in speech:
            result["bundle_identity"] = speech["bundle_identity"]
    return result


def _status(root, body):
    runtime = root / "runtime"
    target = _checked(runtime / "portable-status.json", runtime)
    descriptor, name = tempfile.mkstemp(prefix=".portable-status-", dir=runtime)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(body | {"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _checked(target, runtime)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _client(root):
    token = _read_token(root / "runtime")

    async def authorize(request):
        if request.url.scheme == "http" and request.url.host == "127.0.0.1" and request.url.port == 9012:
            request.headers["X-Tuber-Inference-Key"] = token

    return httpx.AsyncClient(
        base_url="http://127.0.0.1:9012",
        trust_env=False,
        event_hooks={"request": [authorize]},
        timeout=httpx.Timeout(5, connect=2),
    )


async def healthcheck(root=ROOT):
    async with _client(Path(root)) as client:
        return await observe_ready(client, Path(root))


async def _startup_once(root, stack_task, client):
    while True:
        if stack_task.done():
            await stack_task
            raise RuntimeError("Stack exited before readiness")
        try:
            response = await client.get("http://127.0.0.1:9020/", timeout=2)
            response.raise_for_status()
            await _json(client, "GET", "/health", timeout=2)
            break
        except httpx.HTTPError:
            await asyncio.sleep(0.25)
    data, local, mode = _configuration(root)
    if local and data.get("inference_broker", {}).get("enabled"):
        # Exactly one typed establishment attempt, even if legacy initialization
        # failed. There is no reload loop and no generated business/event input.
        await _json(client, "POST", "/mode", json={"mode": mode}, timeout=None)
    return await observe_ready(client, root)


async def serve_portable(root=ROOT, *, serve=None, stopped=None, startup_timeout=DEADLINE_SECONDS):
    from . import stack

    root = Path(root)
    own_signals = stopped is None
    stopped = stopped or asyncio.Event()
    _status(root, {"state": "starting"})
    loop = asyncio.get_running_loop()
    if own_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopped.set)
    stack_task = asyncio.create_task((serve or stack.serve)(stopped))
    stopper = asyncio.create_task(stopped.wait())
    startup = None
    try:
        async with _client(root) as client:
            startup = asyncio.create_task(
                asyncio.wait_for(_startup_once(root, stack_task, client), startup_timeout)
            )
            done, _ = await asyncio.wait({startup, stack_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if startup in done and not stopped.is_set():
                try:
                    actual = await startup
                except Exception:
                    LOGGER.exception("Portable resource startup failed; UI remains available for inspection")
                    _status(
                        root, {"state": "error", "error": "Resource readiness failed; inspect inference logs"}
                    )
                else:
                    _status(root, {"state": "ready", "actual": actual})
                # The UI remains alive after bounded startup failure. A health
                # check always reads real resources, so manual recovery can pass.
                done, _ = await asyncio.wait({stack_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if stack_task in done:
                await stack_task
                if not stopped.is_set():
                    raise RuntimeError("Stack exited unexpectedly")
    except Exception:
        _status(root, {"state": "error", "error": "Stack startup or listener failed; inspect container logs"})
        raise
    finally:
        if startup is not None and not startup.done():
            startup.cancel()
        if startup is not None:
            await asyncio.gather(startup, return_exceptions=True)
        stopped.set()
        try:
            await stack_task
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            if own_signals:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--initialize-only", action="store_true")
    mode.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if os.environ.get("TUBER_ROOT", str(ROOT)) != str(ROOT) or os.environ.get(
        "TUBER_RUNTIME", str(ROOT / "runtime")
    ) != str(ROOT / "runtime"):
        raise ValueError("Portable entry requires the canonical container root/runtime")
    try:
        if args.healthcheck:
            asyncio.run(healthcheck())
            return 0
        deadline = time.monotonic() + DEADLINE_SECONDS
        initialized = initialize_runtime(deadline=deadline)
        print(json.dumps({"portable_initialized": initialized}, ensure_ascii=False), flush=True)
        if not args.initialize_only:
            _deadline(deadline)
            asyncio.run(serve_portable(startup_timeout=deadline - time.monotonic()))
        return 0
    except Exception:
        LOGGER.exception("Portable startup/observation failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
