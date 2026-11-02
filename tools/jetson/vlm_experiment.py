"""Owned, foreground llama.cpp experiments; never manages production services.

Run with the project environment. Each run directory is a new audit artifact:
owner.json, metrics.json, memory.jsonl and server.log. No downloads, configuration
changes, context fitting or requests that generate model output are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import psutil

DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "evaluations/candidates/vlm-operations.json"
OWNER_ENV = "TUBER_VLM_EXPERIMENT_ID"
MIB = 1024 * 1024


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        backoffs = (0.005, 0.01, 0.02, 0.04, 0.08)
        retries = 0
        while True:
            try:
                temporary.replace(path)
                break
            except OSError as error:
                # Windows readers briefly deny rename while holding the old file.
                # Retry only sharing violations; permissions/disk errors stay visible.
                if getattr(error, "winerror", None) != 32 or retries >= len(backoffs):
                    raise
                time.sleep(backoffs[retries])
                retries += 1
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict:
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("schema_version") != 1 or not registry.get("profiles"):
        raise ValueError("Unsupported or empty candidate registry")
    return registry


def artifact_url(profile: dict, artifact: dict) -> str:
    return (
        "https://huggingface.co/"
        + profile["conversion_repository"]
        + "/resolve/"
        + profile["conversion_revision"]
        + "/"
        + quote(artifact["file"], safe="/")
    )


def verify_artifacts(profile: dict, model_root: Path, *, spec_type: str = "none") -> dict:
    declared = {role: profile["artifacts"][role] for role in ("model", "projector")}
    if spec_type != "none":
        speculative = profile.get("speculative") or {}
        if spec_type != "draft-mtp" or speculative.get("type") != spec_type:
            raise ValueError("Requested speculative decoder has no registered artifact")
        if speculative.get("target_model_sha256") != declared["model"]["sha256"]:
            raise ValueError("Speculative artifact is registered for a different target model")
        declared["draft"] = speculative["artifact"]
    verified = {}
    for role, artifact in declared.items():
        filename = artifact["local_filename"]
        if Path(filename).name != filename or filename in ("", ".", ".."):
            raise ValueError(f"Invalid registered local filename: {filename!r}")
        path = model_root / filename
        if not path.is_file() or path.stat().st_size != artifact["size_bytes"]:
            raise ValueError(f"{role} missing or size mismatch: {path}")
        actual_sha = sha256_file(path)
        if actual_sha != artifact["sha256"]:
            raise ValueError(f"{role} SHA256 mismatch: {path}")
        verified[role] = {
            **artifact,
            "path": str(path.resolve()),
            "sha256_verified": True,
            "source_url": artifact_url(profile, artifact),
        }
    return verified


def advise_model_cache(artifacts: dict, enabled: bool) -> dict:
    """Advisory eviction of the verified artifacts, never global drop_caches."""
    supported = hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED")
    result = {
        "requested": enabled,
        "supported": supported,
        "files": [],
        "note": "Hash verification warmed cache. DONTNEED is a hint, not proof of cold loading.",
    }
    if not enabled:
        return result
    for role in artifacts:
        path = Path(artifacts[role]["path"])
        entry = {"role": role, "path": str(path), "attempted": False, "success": False}
        if supported:
            try:
                with path.open("rb") as stream:
                    entry["attempted"] = True
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                entry["success"] = True
            except OSError as error:
                entry["error"] = {"errno": error.errno, "message": str(error)}
        else:
            entry["error"] = {"message": "POSIX_FADV_DONTNEED unavailable on this platform"}
        result["files"].append(entry)
    return result


def context_contract(profile: dict, tokens: int, allow_native_extension: bool, strict: int) -> dict:
    if tokens < 1:
        raise ValueError("context-tokens must be positive")
    native = profile["native_context_tokens"]
    extension = tokens > native
    if extension and not allow_native_extension:
        raise ValueError(
            f"Requested {tokens} exceeds native {native}; use --allow-native-extension explicitly"
        )
    return {
        "requested_context_tokens": tokens,
        "native_context_tokens": native,
        "strict_context_tokens": strict,
        "meets_strict_window": tokens >= strict,
        "native_meets_strict_window": native >= strict,
        "experimental_native_extension": extension,
        "long_context_accuracy_validated": False,
    }


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be in 1..65535")
    if not 1 <= args.ubatch <= args.batch <= 4096:
        raise ValueError("Require 1 <= ubatch <= batch <= 4096")
    if not 0 <= args.gpu_layers <= 999 or not 1 <= args.threads <= 256:
        raise ValueError("Invalid gpu-layers or threads")
    if args.image_max_tokens is not None and not 1 <= args.image_max_tokens <= 16384:
        raise ValueError("image-max-tokens must be in 1..16384")
    if (
        min(args.min_available_mib, args.max_swap_growth_mib) < 0
        or not math.isfinite(args.startup_timeout)
        or args.startup_timeout <= 0
    ):
        raise ValueError("Memory thresholds must be nonnegative and startup-timeout positive")
    if args.direct_io == "on" and args.mmap != "off":
        raise ValueError("direct-io on requires explicit --mmap off")
    if not 0 <= args.log_verbosity <= 5:
        raise ValueError("log-verbosity must be in 0..5")
    if args.reasoning_budget is not None and not -1 <= args.reasoning_budget <= 2147483647:
        raise ValueError("reasoning-budget must be -1, 0 or a positive int32 allowance")
    for value in (args.rope_scale, args.rope_freq_scale):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("RoPE scale must be finite and positive")
    if args.rope_scale is not None and args.rope_freq_scale is not None:
        raise ValueError("Specify only one of rope-scale and rope-freq-scale")
    if args.yarn_orig_ctx is not None and not 0 <= args.yarn_orig_ctx <= 2147483647:
        raise ValueError("yarn-orig-ctx must be a nonnegative int32 context")
    if not 1 <= args.draft_n_max <= 2147483647:
        raise ValueError("draft-n-max must be a positive int32 token count")
    if args.draft_ubatch is not None and not 0 <= args.draft_ubatch <= args.batch:
        raise ValueError("draft-ubatch must be 0 (inherit) or a positive value no larger than batch")
    if args.draft_gpu_layers is not None and not 0 <= args.draft_gpu_layers <= 999:
        raise ValueError("draft-gpu-layers must be in 0..999")


def build_command(args: argparse.Namespace, artifacts: dict) -> list[str]:
    validate_arguments(args)
    command = [
        str(Path(args.llama_binary).resolve()),
        "--model",
        artifacts["model"]["path"],
        "--mmproj",
        artifacts["projector"]["path"],
        "--host",
        args.listen_host,
        "--port",
        str(args.port),
        "--alias",
        args.alias or args.profile,
        "--ctx-size",
        str(args.context_tokens),
        "--parallel",
        "1",
        "--fit",
        "off",
        "--cache-ram",
        "0",
        "--ctx-checkpoints",
        "0",
        "--no-context-shift",
        "--batch-size",
        str(args.batch),
        "--ubatch-size",
        str(args.ubatch),
        "--gpu-layers",
        str(args.gpu_layers),
        "--threads",
        str(args.threads),
        "--cache-type-k",
        args.kv_k or args.kv,
        "--cache-type-v",
        args.kv_v or args.kv,
        "--flash-attn",
        args.flash_attn,
        "--jinja",
        "--reasoning",
        args.reasoning,
        "--slots",
    ]
    command.extend(["--log-verbosity", str(args.log_verbosity)])
    if args.mmproj_offload == "cpu":
        command.append("--no-mmproj-offload")
    else:
        command.append("--mmproj-offload")
    command.append("--kv-offload" if args.kv_offload == "gpu" else "--no-kv-offload")
    load_mode = "dio" if args.direct_io == "on" else ("mmap" if args.mmap == "on" else "none")
    command.extend(["--load-mode", load_mode])
    if args.warmup == "off":
        command.append("--no-warmup")
    if args.image_max_tokens is not None:
        command.extend(["--image-max-tokens", str(args.image_max_tokens)])
    for option, value in (
        ("--lazy-mode", args.lazy_mode),
        ("--reasoning-budget", args.reasoning_budget),
        ("--rope-scaling", args.rope_scaling),
        ("--rope-scale", args.rope_scale),
        ("--rope-freq-scale", args.rope_freq_scale),
        ("--yarn-orig-ctx", args.yarn_orig_ctx),
    ):
        if value is not None:
            command.extend([option, str(value)])
    if args.spec_type != "none":
        draft = artifacts.get("draft") or {}
        if not draft.get("sha256_verified"):
            raise ValueError("Speculative decoding requires a verified draft artifact")
        for option, value in (
            ("--spec-type", args.spec_type),
            ("--spec-draft-model", draft["path"]),
            ("--spec-draft-n-max", args.draft_n_max),
            ("--spec-draft-sampling", args.draft_sampling),
            ("--spec-draft-type-k", args.kv_k or args.kv),
            ("--spec-draft-type-v", args.kv_v or args.kv),
            (
                "--spec-draft-ngl",
                args.gpu_layers if args.draft_gpu_layers is None else args.draft_gpu_layers,
            ),
        ):
            command.extend([option, str(value)])
        if args.draft_ubatch is not None:
            command.extend(["--spec-draft-n-ubatch", str(args.draft_ubatch)])
    return command


def process_identity(process: psutil.Process, *, launch_command: list[str] | None = None) -> dict:
    with process.oneshot():
        identity = {
            "pid": process.pid,
            "create_time": process.create_time(),
            "executable": process.exe(),
            "command": process.cmdline(),
        }
    if not identity["command"] and launch_command:
        # /proc can briefly expose an empty cmdline immediately after exec.
        # Our own Popen arguments supply the expected command; stop still checks
        # the live command, executable, creation time and ownership stamp.
        identity["observed_command_at_start"] = []
        identity["command"] = list(launch_command)
        identity["command_source"] = "popen_launch_arguments_after_empty_proc_cmdline"
    return identity


def process_matches(identity: dict, nonce: str) -> psutil.Process | None:
    """Fail closed on reused PIDs, altered commands or missing ownership stamps."""
    try:
        process = psutil.Process(identity["pid"])

        def exited_during_check():
            # The foreground owner and stop CLI may reap the same child concurrently.
            # Linux can return empty exe/cmdline between our first status and reap.
            return process.status() == psutil.STATUS_ZOMBIE or not process.is_running()

        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        if abs(process.create_time() - identity["create_time"]) > 0.01:
            raise ValueError("Refusing to signal a reused PID")
        if os.path.normcase(process.exe()) != os.path.normcase(identity["executable"]):
            if exited_during_check():
                return None
            raise ValueError("Refusing to signal a changed executable")
        if process.cmdline() != identity["command"]:
            if exited_during_check():
                return None
            raise ValueError("Refusing to signal a changed command")
        if process.environ().get(OWNER_ENV) != nonce:
            if exited_during_check():
                return None
            raise ValueError("Refusing to signal a process without this experiment's ownership stamp")
        return process
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as error:
        if not process.is_running():
            return None
        raise ValueError("Cannot verify process ownership") from error


def owned_processes(owner: dict) -> list[psutil.Process]:
    """The parent may already have exited; recover only stamped group members."""
    leader = process_matches(owner["server"], owner["experiment_id"])
    if os.name == "posix":
        if leader is not None:
            try:
                if os.getpgid(leader.pid) != owner["server"]["pid"]:
                    raise ValueError("Refusing to signal a changed process group")
            except ProcessLookupError:
                leader = None
        members = []
        for process in psutil.process_iter():
            try:
                if os.getpgid(process.pid) != owner["server"]["pid"]:
                    continue
                # Even after the leader exits, no unrelated or reused group is signalled.
                if process.status() != psutil.STATUS_ZOMBIE:
                    if process.environ().get(OWNER_ENV) != owner["experiment_id"]:
                        raise ValueError("Refusing to signal an unowned process group member")
                    members.append(process)
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (PermissionError, psutil.AccessDenied) as error:
                raise ValueError("Cannot verify process-group ownership") from error
        return members
    try:
        children = leader.children(recursive=True) if leader is not None else []
    except psutil.NoSuchProcess:
        children = []
    for identity in owner.get("children", []):
        child = process_matches(identity, owner["experiment_id"])
        if child is not None and all(existing.pid != child.pid for existing in children):
            children.append(child)
    for child in children[:]:
        try:
            if process_matches(process_identity(child), owner["experiment_id"]) is None:
                children.remove(child)
        except psutil.NoSuchProcess:
            children.remove(child)
    return children + ([leader] if leader is not None else [])


def wait_owned(
    identities: list[dict], nonce: str, timeout: float, child: subprocess.Popen | None = None
) -> list[dict]:
    """No pidfd_open: some Jetson container kernels reject it with EINVAL."""
    deadline = time.monotonic() + max(0, timeout)
    if child is not None:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    while True:
        alive = [identity for identity in identities if process_matches(identity, nonce) is not None]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def terminate_owned(owner: dict, grace_seconds: float = 8, *, child: subprocess.Popen | None = None) -> dict:
    if not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("grace-seconds must be finite and nonnegative")
    members = owned_processes(owner)
    identities = []
    for process in members:
        try:
            identities.append(process_identity(process))
        except psutil.NoSuchProcess:
            continue
    if os.name == "posix" and members:
        try:
            os.killpg(owner["server"]["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        for identity in identities:
            process = process_matches(identity, owner["experiment_id"])
            if process is not None:
                try:
                    process.terminate()
                except psutil.NoSuchProcess:
                    pass
    alive = wait_owned(identities, owner["experiment_id"], grace_seconds, child)
    # Include descendants created during graceful shutdown before collecting the group.
    for process in owned_processes(owner):
        try:
            identity = process_identity(process)
        except psutil.NoSuchProcess:
            continue
        if all(existing["pid"] != process.pid for existing in alive):
            alive.append(identity)
    killed = []
    if os.name == "posix" and alive:
        try:
            os.killpg(owner["server"]["pid"], signal.SIGKILL)
            killed = [identity["pid"] for identity in alive]
        except ProcessLookupError:
            pass
    else:
        for identity in alive:
            verified = process_matches(identity, owner["experiment_id"])
            if verified is not None:
                try:
                    verified.kill()
                    killed.append(verified.pid)
                except psutil.NoSuchProcess:
                    pass
    wait_owned(alive, owner["experiment_id"], 2, child)
    remaining = [process.pid for process in owned_processes(owner)]
    return {
        "signalled_pids": [item["pid"] for item in identities],
        "force_killed_pids": killed,
        "remaining_pids": remaining,
    }


def memory_sample(server_pid: int | None) -> tuple[dict, list[dict]]:
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    identities, rss = [], 0
    if server_pid is not None:
        try:
            leader = psutil.Process(server_pid)
            for process in [leader, *leader.children(recursive=True)]:
                try:
                    rss += process.memory_info().rss
                    identities.append(process_identity(process))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except psutil.NoSuchProcess:
            pass
    return (
        {
            "timestamp": utc_now(),
            "monotonic_seconds": time.monotonic(),
            "system_total_bytes": memory.total,
            "system_used_bytes": memory.used,
            "system_available_bytes": memory.available,
            "swap_total_bytes": swap.total,
            "swap_used_bytes": swap.used,
            "swap_in_bytes": swap.sin,
            "swap_out_bytes": swap.sout,
            "server_tree_rss_bytes": rss,
            "process_count": len(identities),
        },
        identities,
    )


def update_memory(metrics: dict, sample: dict) -> None:
    summary = metrics.setdefault("memory", {})
    summary["samples"] = summary.get("samples", 0) + 1
    for field in ("server_tree_rss_bytes", "system_used_bytes", "swap_used_bytes"):
        key = "peak_" + field
        summary[key] = max(summary.get(key, 0), sample[field])
    key = "minimum_system_available_bytes"
    summary[key] = min(summary.get(key, sample["system_available_bytes"]), sample["system_available_bytes"])
    summary["latest"] = sample
    if "system_baseline" in metrics:
        summary["peak_swap_growth_bytes"] = max(
            0, summary["peak_swap_used_bytes"] - metrics["system_baseline"]["swap_used_bytes"]
        )


def memory_violation(sample: dict, baseline_swap: int, args: argparse.Namespace) -> str | None:
    if sample["system_available_bytes"] < args.min_available_mib * MIB:
        return "minimum_available_memory_threshold"
    if sample["swap_used_bytes"] - baseline_swap > args.max_swap_growth_mib * MIB:
        return "maximum_swap_growth_threshold"
    return None


def context_from_props(props: dict) -> int:
    settings = props.get("default_generation_settings", {})
    for candidate in (settings.get("n_ctx"), props.get("n_ctx"), props.get("n_ctx_per_seq")):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    raise ValueError("Ready server did not report a positive actual context in /props")


def log_evidence(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 1024 * 1024))
        lines = stream.read().decode("utf-8", errors="replace").splitlines()
    matching = [
        line
        for line in lines
        if re.search(
            r"offload|CUDA[0-9:]|KV buffer|n_ctx(?:_per_seq)?\s*=|flash_attn\s*=|compute buffer|out of memory",
            line,
            re.IGNORECASE,
        )
    ]
    per_sequence = re.findall(r"n_ctx_per_seq\s*=\s*(\d+)", "\n".join(lines))
    return {
        "cuda_and_allocation_lines": matching[-160:],
        "actual_context_per_sequence_from_log": int(per_sequence[-1]) if per_sequence else None,
        "log_tail_limit_bytes": 1024 * 1024,
    }


def stop_run(run_dir: Path, grace_seconds: float = 8) -> dict:
    if not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("grace-seconds must be finite and nonnegative")
    owner = json.loads((run_dir / "owner.json").read_text(encoding="utf-8"))
    if owner.get("schema_version") != 1 or not owner.get("experiment_id") or not owner.get("server"):
        raise ValueError("Invalid experiment ownership record")
    owned_processes(owner)  # Verify before writing a request or sending any signal.
    atomic_json(
        run_dir / "stop.request.json", {"experiment_id": owner["experiment_id"], "requested_at": utc_now()}
    )
    return terminate_owned(owner, grace_seconds)


def serve(args: argparse.Namespace) -> int:
    validate_arguments(args)
    registry = load_registry(Path(args.registry))
    if args.profile not in registry["profiles"]:
        raise ValueError(f"Unknown profile: {args.profile}")
    profile = registry["profiles"][args.profile]
    contract = context_contract(
        profile, args.context_tokens, args.allow_native_extension, registry["strict_context_tokens"]
    )
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if any(run_dir.iterdir()):
        raise ValueError("Run directory must be empty; preserve previous experiment records")
    with socket.socket() as listener:
        if os.name == "posix":
            # Like the real HTTP listener, allow a previous run's TIME_WAIT
            # connections while still refusing a live listener on this port.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.listen_host, args.port))
    metrics = {
        "schema_version": 1,
        "profile": args.profile,
        "name": profile["name"],
        "created_at": utc_now(),
        "state": "verifying",
        "context_contract": contract,
        "registry_path": str(Path(args.registry).resolve()),
        "registry_sha256": sha256_file(Path(args.registry)),
        "profile_manifest": profile,
        "llama_cpp_expected_revision": registry["llama_cpp_revision"],
        "listen_host": args.listen_host,
        "self_check_host": "127.0.0.1",
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "measurement_notes": [
            "RAM sampled every second; very short peaks may fall between samples.",
            "RSS sums server and descendants, which can double-count shared pages.",
            "Kernel reaped-child RSS is an individual high-water mark, not tree aggregate.",
            "On unified-memory Jetson, CUDA memory may be absent from process RSS; inspect system RAM too.",
            "Artifact SHA verification reads weights before startup and warms file cache.",
            "Readiness and context capacity do not establish model quality or GPU parallelism.",
        ],
        "parameters": {key: value for key, value in vars(args).items() if key != "action"},
    }
    baseline, _ = memory_sample(None)
    metrics["system_baseline"] = baseline
    update_memory(metrics, baseline)
    atomic_json(run_dir / "metrics.json", metrics)
    owner = None
    process = None
    old_handlers = {}
    stop_signal = None

    def request_stop(number, _frame):
        nonlocal stop_signal
        stop_signal = number

    reason, exit_code = "runner_failure", 1
    try:
        verifying_started = time.monotonic()
        artifacts = verify_artifacts(profile, Path(args.model_root).resolve(), spec_type=args.spec_type)
        metrics["artifacts"] = artifacts
        command = build_command(args, artifacts)
        binary = Path(command[0])
        if not binary.is_file():
            raise ValueError(f"llama binary missing: {binary}")
        metrics["binary_sha256"] = sha256_file(binary)
        metrics["verification_seconds"] = time.monotonic() - verifying_started
        metrics["model_cache_advice"] = advise_model_cache(artifacts, args.model_cache_advice == "on")
        metrics["command"] = command
        for number in (signal.SIGTERM, signal.SIGINT):
            old_handlers[number] = signal.signal(number, request_stop)
        experiment_id = uuid.uuid4().hex
        environment = dict(os.environ)
        ignored_keys = sorted(key for key in environment if key.startswith("LLAMA_ARG_"))
        for key in ignored_keys:
            environment.pop(key)
        metrics["ignored_llama_environment_keys"] = ignored_keys
        environment[OWNER_ENV] = experiment_id
        if args.cuda_graphs == "off":
            environment["GGML_CUDA_DISABLE_GRAPHS"] = "1"
        else:
            environment.pop("GGML_CUDA_DISABLE_GRAPHS", None)
        metrics["child_environment"] = {
            key: environment.get(key) for key in ("CUDA_VISIBLE_DEVICES", "GGML_CUDA_DISABLE_GRAPHS")
        }
        process_options = (
            {"start_new_session": True}
            if os.name == "posix"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        metrics["state"] = "loading"
        started = time.monotonic()
        with (
            (run_dir / "server.log").open("wb") as log,
            (run_dir / "memory.jsonl").open("w", encoding="utf-8") as samples,
        ):
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment, **process_options
            )
            owner = {
                "schema_version": 1,
                "experiment_id": experiment_id,
                "created_at": utc_now(),
                "runner": process_identity(psutil.Process()),
                "server": process_identity(psutil.Process(process.pid), launch_command=command),
                "children": [],
            }
            atomic_json(run_dir / "owner.json", owner)
            metrics["server_pid"] = process.pid
            atomic_json(run_dir / "metrics.json", metrics)
            print(json.dumps({"event": "loading", "pid": process.pid, "run_dir": str(run_dir)}), flush=True)
            ready = False
            next_sample = 0.0
            with httpx.Client(
                base_url=f"http://127.0.0.1:{args.port}", timeout=0.4, trust_env=False
            ) as client:
                while True:
                    now = time.monotonic()
                    if now >= next_sample:
                        sample, identities = memory_sample(process.pid)
                        samples.write(json.dumps(sample) + "\n")
                        samples.flush()
                        update_memory(metrics, sample)
                        owner["children"] = [
                            identity for identity in identities if identity["pid"] != process.pid
                        ]
                        atomic_json(run_dir / "owner.json", owner)
                        atomic_json(run_dir / "metrics.json", metrics)
                        next_sample = now + 1.0
                        violation = memory_violation(sample, baseline["swap_used_bytes"], args)
                        if violation:
                            reason, exit_code = violation, 2
                            break
                    stop_path = run_dir / "stop.request.json"
                    if stop_signal is not None or stop_path.exists():
                        if stop_path.exists():
                            request = json.loads(stop_path.read_text(encoding="utf-8"))
                            if request.get("experiment_id") != experiment_id:
                                raise ValueError("Stop request ownership mismatch")
                        reason, exit_code = "operator_stop", 0
                        metrics["stop_signal"] = stop_signal
                        break
                    if process.poll() is not None:
                        reason, exit_code = "server_exited", 1
                        break
                    if not ready:
                        if now - started > args.startup_timeout:
                            reason, exit_code = "startup_timeout", 2
                            break
                        try:
                            health = client.get("/health")
                            metrics["latest_health_status"] = health.status_code
                            if health.status_code != 200:
                                time.sleep(0.1)
                                continue
                            response = client.get("/props")
                            metrics["latest_props_status"] = response.status_code
                            if response.status_code == 200:
                                props = response.json()
                                actual = context_from_props(props)
                                metrics["actual_props"] = props
                                metrics["engine_build_info"] = props.get("build_info")
                                metrics["actual_context_tokens"] = actual
                                if actual < args.context_tokens:
                                    reason, exit_code = "actual_context_below_requested", 2
                                    break
                                aliases = props.get("model_alias")
                                if aliases is not None:
                                    aliases = aliases if isinstance(aliases, list) else [aliases]
                                    if (args.alias or args.profile) not in aliases:
                                        raise ValueError("Server alias does not match this experiment")
                                slots = client.get("/slots")
                                if slots.status_code == 200:
                                    actual_slots = slots.json()
                                    metrics["actual_slots_at_ready"] = actual_slots
                                    if not isinstance(actual_slots, list) or len(actual_slots) != 1:
                                        raise ValueError("Actual server must have exactly one slot")
                                    slot_context = actual_slots[0].get("n_ctx")
                                    if isinstance(slot_context, int) and slot_context < args.context_tokens:
                                        reason, exit_code = "actual_slot_context_below_requested", 2
                                        break
                                metrics["loading_seconds"] = time.monotonic() - started
                                metrics["ready_at"] = utc_now()
                                metrics["state"] = "ready"
                                ready = True
                                atomic_json(run_dir / "metrics.json", metrics)
                                print(
                                    json.dumps(
                                        {
                                            "event": "ready",
                                            "actual_context_tokens": actual,
                                            "loading_seconds": metrics["loading_seconds"],
                                        }
                                    ),
                                    flush=True,
                                )
                        except httpx.HTTPError as error:
                            metrics["latest_startup_transport_error"] = type(error).__name__
                    time.sleep(0.1)
    except BaseException as error:
        metrics["error"] = {"type": type(error).__name__, "message": str(error)}
        reason, exit_code = "runner_failure", 1
    finally:
        if process is not None:
            metrics["server_lifetime_before_cleanup_seconds"] = time.monotonic() - started
        if owner is not None:
            try:
                metrics["cleanup"] = terminate_owned(owner, child=process)
                if metrics["cleanup"]["remaining_pids"]:
                    exit_code = 1
            except (ValueError, psutil.Error, OSError) as error:
                metrics["cleanup_error"] = str(error)
                exit_code = 1
        if process is not None:
            try:
                metrics["server_returncode"] = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                metrics["server_returncode"] = None
            if os.name == "posix":
                import resource

                # This foreground runner creates only one direct child, the server.
                # Kernel high-water RSS complements the periodic aggregate samples.
                scale = 1 if sys.platform == "darwin" else 1024
                metrics["memory"]["kernel_reaped_children_peak_rss_bytes"] = (
                    resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
                )
        for number, previous in old_handlers.items():
            signal.signal(number, previous)
        sample, _ = memory_sample(None)
        update_memory(metrics, sample)
        metrics.update(log_evidence(run_dir / "server.log"))
        metrics["state"] = "stopped" if exit_code == 0 else "failed"
        metrics["termination_reason"] = reason
        metrics["finished_at"] = utc_now()
        metrics["exit_code"] = exit_code
        atomic_json(run_dir / "metrics.json", metrics)
        print(
            json.dumps({"event": metrics["state"], "termination_reason": reason, "exit_code": exit_code}),
            flush=True,
        )
    return exit_code


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    start = commands.add_parser("serve", help="Run one pinned model until stopped; keep this process alive")
    start.add_argument("--profile", required=True)
    start.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    start.add_argument("--model-root", required=True)
    start.add_argument("--llama-binary", required=True)
    start.add_argument("--run-dir", required=True)
    start.add_argument("--alias")
    start.add_argument("--listen-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    start.add_argument("--port", type=int, default=9030)
    start.add_argument("--context-tokens", type=int, default=131072)
    start.add_argument("--allow-native-extension", action="store_true")
    start.add_argument("--kv", choices=("q4_0", "q8_0", "f16"), default="q4_0")
    start.add_argument("--kv-k", choices=("q4_0", "q8_0", "f16"))
    start.add_argument("--kv-v", choices=("q4_0", "q8_0", "f16"))
    start.add_argument("--batch", type=int, default=256)
    start.add_argument("--ubatch", type=int, default=64)
    start.add_argument("--gpu-layers", type=int, default=99)
    start.add_argument("--kv-offload", choices=("gpu", "cpu"), default="gpu")
    start.add_argument("--threads", type=int, default=4)
    start.add_argument("--mmproj-offload", choices=("gpu", "cpu"), default="gpu")
    start.add_argument("--mmap", choices=("on", "off"), default="on")
    start.add_argument(
        "--lazy-mode",
        choices=("auto", "on", "off"),
        help="Explicit per-layer/input embedding row loading; native loader maps lazy rows even with DIO",
    )
    start.add_argument("--direct-io", choices=("on", "off"), default="off")
    start.add_argument("--model-cache-advice", choices=("on", "off"), default="off")
    start.add_argument("--log-verbosity", type=int, default=4)
    start.add_argument("--reasoning", choices=("on", "off"), default="on")
    start.add_argument(
        "--reasoning-budget", type=int, help="Explicit thinking allowance; omitted keeps native default"
    )
    start.add_argument("--rope-scaling", choices=("none", "linear", "yarn"))
    rope = start.add_mutually_exclusive_group()
    rope.add_argument("--rope-scale", type=float)
    rope.add_argument("--rope-freq-scale", type=float)
    start.add_argument("--yarn-orig-ctx", type=int)
    start.add_argument("--flash-attn", choices=("on", "off"), default="on")
    start.add_argument("--cuda-graphs", choices=("on", "off"), default="on")
    start.add_argument("--warmup", choices=("on", "off"), default="on")
    start.add_argument("--image-max-tokens", type=int)
    start.add_argument("--spec-type", choices=("none", "draft-mtp"), default="none")
    start.add_argument("--draft-n-max", type=int, default=1)
    start.add_argument("--draft-sampling", choices=("greedy", "probabilistic"), default="greedy")
    start.add_argument(
        "--draft-ubatch", type=int, help="Independent draft microbatch; omitted or 0 inherits target ubatch"
    )
    start.add_argument(
        "--draft-gpu-layers", type=int, help="Independent draft offload; omitted inherits target gpu-layers"
    )
    start.add_argument("--startup-timeout", type=float, default=300)
    start.add_argument("--min-available-mib", type=int, default=256)
    start.add_argument("--max-swap-growth-mib", type=int, default=128)
    stop = commands.add_parser("stop", help="Verify and terminate only this experiment's owned process tree")
    stop.add_argument("--run-dir", required=True)
    stop.add_argument("--grace-seconds", type=float, default=8)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "stop":
            result = stop_run(Path(args.run_dir).resolve(), args.grace_seconds)
            print(json.dumps(result), flush=True)
            return 1 if result["remaining_pids"] else 0
        return serve(args)
    except (ValueError, OSError, json.JSONDecodeError, psutil.Error) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
