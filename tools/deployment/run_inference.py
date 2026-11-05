"""Launch the inference-only Docker service with an existing environment and assets.

Run on the Linux inference host. --dry-run inspects paths/images but does not
write configuration, pull images, or change containers. A replacement retains
the previous container and restores it if the new service cannot become ready.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from uuid import uuid4

ROOT = "/workspace/tuber"
PUBLIC_PATHS = (
    "src",
    "tools",
    "config",
    "assets",
    "third_party",
    "components",
    "pyproject.toml",
    "uv.lock",
    "evaluations/semantic-cases.json",
    "evaluations/speech-cases.json",
)
ASSET_PATHS = ("models", "engines", "tts", "nltk_data", "node-assets")
BUSINESS_FILES = {
    "admin-token",
    "overlay-token",
    "bilibili-credentials.json",
    "provider-credentials.json",
    ".business-authority.json",
    "business-authority.json",
    ".business-writer.lock",
    ".business-restore.json",
}
DEFAULT_NODE = {
    "mode": "text",
    "operations_model": "Gemma4-E2B",
    "live_model": "MiniCPM5-2B",
    "context_tokens": 131072,
    "kv_cache_type": "q4_0",
    "cuda_managed_memory": False,
    "min_available_mib": 384,
    "max_swap_growth_mib": 128,
    "gemma_vision_only": True,
    "gemma_model": "runtime/models/operations-vlm/gemma-4-E2B-it-Q4_0.gguf",
    "gemma_projector": "runtime/node-assets/mmproj-gemma-4-E2B-it-Q8_0-visiononly.gguf",
    "gemma_engine": (
        "runtime/engines/llama.cpp-0ec4027a2a0183de87c007069adddc06e665f7de/build/bin/llama-server"
    ),
    "tts": {"local_backend": "genie_onnx_int8", "voice_id": "yaoyao_sovits_v2"},
}


def checked_path(raw, *, exists=True):
    path = Path(raw).absolute()
    if any(c in str(path) for c in ",\r\n"):
        raise ValueError("Bind paths cannot contain commas or newlines")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Deployment paths cannot traverse links")
    if exists and not path.exists():
        raise ValueError(f"Required deployment path is missing: {path}")
    return path.resolve()


def load_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a configuration object: {path.name}")
    return value


def check_data(data):
    if data == Path(data.anchor) or data == Path.home().resolve():
        raise ValueError("Use a dedicated inference data directory")
    if not data.exists():
        return
    if not data.is_dir():
        raise ValueError("Inference data must be a directory")
    for directory, dirs, files in os.walk(data, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Inference data cannot contain links")
            if name in BUSINESS_FILES or name.startswith("studio.sqlite"):
                raise ValueError("Inference data contains business state or platform credentials")
            if path.is_file():
                if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                    raise ValueError("Inference data cannot contain a database")
                with path.open("rb") as source:
                    if source.read(16) == b"SQLite format 3\x00":
                        raise ValueError("Inference data cannot contain a database")
            elif not path.is_dir():
                raise ValueError("Inference data supports only regular files and directories")
    studio = data / "studio.json"
    if studio.exists() and set(load_json(studio)) - {"tts", "inference_broker"}:
        raise ValueError("Inference studio.json must contain only TTS and broker settings")
    token = data / "inference-token"
    if token.exists() and len(token.read_text(encoding="utf-8").strip()) < 32:
        raise ValueError("Existing inference token is invalid; it was not overwritten")


def setup(args):
    source = checked_path(args.source)
    data = checked_path(args.data, exists=False)
    assets = checked_path(args.assets)
    if data in (source, assets) or source.is_relative_to(data) or assets.is_relative_to(data):
        raise ValueError("Inference data must be independent of source and asset roots")
    check_data(data)
    for relative in PUBLIC_PATHS:
        checked_path(source / relative)
    for relative in (
        "tools/jetson/inference-node.py",
        "tools/jetson/environment.py",
        "src/tuber_studio/inference.py",
    ):
        if not checked_path(source / relative).is_file():
            raise ValueError("Source must contain the inference node program")
    node_assets = args.node_assets or (
        data / "node-assets" if (data / "node-assets").is_dir() else assets / "node-assets"
    )
    mounts = {
        name: checked_path(node_assets if name == "node-assets" else assets / name) for name in ASSET_PATHS
    }
    if any(not path.is_dir() for path in mounts.values()):
        raise ValueError("Inference assets must be directories")
    if any(data == path or data.is_relative_to(path) for path in mounts.values()):
        raise ValueError("Writable data cannot be inside a read-only asset directory")
    node_file = data / "inference-node.json"
    node = load_json(node_file) if node_file.exists() else json.loads(json.dumps(DEFAULT_NODE))
    for field, allowed in (
        ("gemma_model", {"models"}),
        ("gemma_engine", {"engines"}),
        ("gemma_projector", {"models", "node-assets"}),
    ):
        path = PurePosixPath(node.get(field, DEFAULT_NODE[field]))
        if path.is_absolute():
            try:
                path = path.relative_to(ROOT)
            except ValueError:
                raise ValueError("Configured inference asset is outside the container root") from None
        if (
            ".." in path.parts
            or len(path.parts) < 3
            or path.parts[0] != "runtime"
            or path.parts[1] not in allowed
        ):
            raise ValueError("Configured model assets must use read-only asset mounts")
        target = checked_path(mounts[path.parts[1]].joinpath(*path.parts[2:]))
        if not target.is_file():
            raise ValueError("Configured inference asset must be a regular file")
    image = args.image
    if not image:
        reference = source / "environment-image.txt"
        image = (
            reference.read_text().strip()
            if reference.is_file()
            else "ghcr.io/4everwz/tuber_studio:environment-r36.4"
        )
    if not image or image.startswith("-") or re.search(r"\s|://", image):
        raise ValueError("Invalid Docker image reference")
    identity = args.user
    if not identity:
        if not hasattr(os, "getuid"):
            raise ValueError("Specify --user UID:GID when planning from a non-POSIX host")
        identity = f"{os.getuid()}:{os.getgid()}"
    if not re.fullmatch(r"[1-9]\d*:\d+", identity):
        raise ValueError("Run the node as a non-root UID:GID")
    if not args.dry_run and hasattr(os, "getuid") and str(os.getuid()) != identity.split(":")[0]:
        raise ValueError("Run the launcher as the inference data owner")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        raise ValueError("Invalid container name")
    host = str(ipaddress.ip_address(args.host))
    if not 1 <= args.port <= 65535 or not math.isfinite(args.ready_timeout) or args.ready_timeout <= 0:
        raise ValueError("Invalid port or readiness timeout")
    return {
        "source": source,
        "data": data,
        "mounts": mounts,
        "node": node,
        "image_reference": image,
        "user": identity,
        "host": host,
    }


def bind(path, destination, *, readonly=True):
    return ["--mount", f"type=bind,src={path},dst={destination}" + (",readonly" if readonly else "")]


def public_mounts(plan):
    result = []
    for relative in PUBLIC_PATHS:
        result += bind(plan["source"] / relative, ROOT + "/" + relative)
    return result


def security(plan):
    policy = plan.get("replacement_security")
    if policy is None:
        policy = [
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "256",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=256m",
            "--tmpfs",
            "/run:rw,nosuid,nodev,noexec,size=16m",
        ]
    return [
        *policy,
        "--user",
        plan["user"],
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONPATH=" + ROOT + "/src",
    ]


def preserve_container_policy(plan, container):
    """Code replacement keeps existing resource and isolation settings."""
    config = container["HostConfig"]
    if "ReadonlyRootfs" not in config:
        return
    network = config.get("NetworkMode", "bridge")
    if network in {"host", "none"} or network.startswith("container:"):
        raise ValueError("Inference service requires its own bridge network namespace")
    plan["network"] = network
    policy = ["--read-only"] if config["ReadonlyRootfs"] else []
    for field, flag in (
        ("CapAdd", "--cap-add"),
        ("CapDrop", "--cap-drop"),
        ("SecurityOpt", "--security-opt"),
    ):
        for value in config.get(field) or []:
            policy += [flag, value]
    for field, flag in (
        ("PidsLimit", "--pids-limit"),
        ("ShmSize", "--shm-size"),
        ("Memory", "--memory"),
        ("MemorySwap", "--memory-swap"),
        ("MemoryReservation", "--memory-reservation"),
    ):
        value = config.get(field)
        if value:
            policy += [flag, str(value)]
    if config.get("IpcMode"):
        policy += ["--ipc", config["IpcMode"]]
    for value in config.get("Ulimits") or []:
        policy += ["--ulimit", f"{value['Name']}={value['Soft']}:{value['Hard']}"]
    for path, options in (config.get("Tmpfs") or {}).items():
        policy += ["--tmpfs", path + ":" + options]
    plan["replacement_security"] = policy


def environment_command(plan, image):
    check = (
        "from pathlib import Path; import sys; "
        "sys.path.insert(0, '/workspace/tuber/tools/jetson'); "
        "from environment import verify; verify(Path('/workspace/tuber'))"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--runtime",
        "runc",
        "--network",
        "none",
        *security(plan),
        *public_mounts(plan),
        "--entrypoint",
        "/opt/studio-venv/bin/python",
        image,
        "-c",
        check,
    ]


def run_command(plan, image, args):
    host = "[" + plan["host"] + "]" if ":" in plan["host"] else plan["host"]
    command = [
        "docker",
        "run",
        "--detach",
        "--pull=never",
        "--name",
        args.name,
        "--runtime",
        "nvidia",
        *security(plan),
        "--network",
        plan.get("network", "bridge"),
        "--publish",
        f"{host}:{args.port}:9012",
        "--restart",
        "unless-stopped",
        "--stop-timeout",
        "45",
        "--health-cmd",
        "/opt/studio-venv/bin/python "
        + ROOT
        + "/tools/jetson/inference-node.py --root "
        + ROOT
        + " --port 9012 --healthcheck",
        "--health-interval",
        "20s",
        "--health-timeout",
        "10s",
        "--health-start-period",
        "420s",
        "--health-retries",
        "3",
        "--workdir",
        ROOT,
        "--label",
        "tuber-studio.service=inference-node",
        "--env",
        "TUBER_ROOT=" + ROOT,
        "--env",
        "TUBER_GPT_SOVITS_ROOT=/opt/GPT-SoVITS-v2",
        "--env",
        "OMP_NUM_THREADS=2",
        "--env",
        "HOME=" + ROOT + "/runtime",
        "--env",
        "XDG_CACHE_HOME=" + ROOT + "/runtime/tmp",
        "--env",
        "NLTK_DATA=" + ROOT + "/runtime/nltk_data",
        "--env",
        "NUMBA_CACHE_DIR=" + ROOT + "/runtime/tmp/numba",
        "--env",
        "TORCH_HOME=" + ROOT + "/runtime/tmp/torch",
    ]
    command += public_mounts(plan) + bind(plan["data"], ROOT + "/runtime", readonly=False)
    for name, path in plan["mounts"].items():
        command += bind(path, ROOT + "/runtime/" + name)
    return [
        *command,
        "--entrypoint",
        "/opt/studio-venv/bin/python",
        image,
        "tools/jetson/inference-node.py",
        "--root",
        ROOT,
        "--host",
        "0.0.0.0",
        "--port",
        "9012",
    ]


def docker(arguments, *, check=True):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError("Docker command failed: " + " ".join(arguments[:2]))
    return result


def inspect(kind, value):
    result = docker([kind, "inspect", value], check=False)
    if result.returncode:
        if re.search(r"No such (image|object|container)", result.stderr, re.I):
            return None
        raise RuntimeError("Docker inspection failed; check daemon access")
    return json.loads(result.stdout)[0]


def same_data(container, data):
    actual = [
        item["Source"]
        for item in container.get("Mounts", [])
        if item.get("Destination") == ROOT + "/runtime" and item.get("Type") == "bind"
    ]
    return len(actual) == 1 and checked_path(actual[0]) == data


def private_write(path, content):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def initialize_data(plan):
    data, node = plan["data"], plan["node"]
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    (data / "tmp").mkdir(exist_ok=True, mode=0o700)
    private_write(data / "inference-node.json", json.dumps(node, indent=2) + "\n")
    private_write(
        data / "studio.json",
        json.dumps(
            {
                "tts": node.get("tts", {}),
                "inference_broker": {
                    "context_tokens": node.get("context_tokens", 131072),
                    "mode": node.get("mode", "text"),
                    "kv_cache_type": node.get("kv_cache_type", "q4_0"),
                },
            },
            indent=2,
        )
        + "\n",
    )
    private_write(data / "inference-token", secrets.token_urlsafe(32))


def wait_ready(plan, args):
    host = plan["host"]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = "[" + host + "]"
    token = (plan["data"] / "inference-token").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"http://{host}:{args.port}/health", headers={"X-Tuber-Inference-Key": token}
    )
    deadline = time.monotonic() + args.ready_timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=5) as response:
                status = json.load(response)
            if status.get("state") == "active":
                node = plan["node"]
                mode = node.get("mode", "text")
                model = (
                    node.get("live_model", "MiniCPM5-2B")
                    if mode == "live"
                    else node.get("operations_model", "Gemma4-E2B")
                )
                if (
                    status.get("mode") != mode
                    or status.get("model") != model
                    or status.get("requested_context_tokens") != node.get("context_tokens", 131072)
                ):
                    raise RuntimeError("Loaded inference profile differs from the saved node configuration")
                return {
                    key: status.get(key) for key in ("state", "model", "mode", "requested_context_tokens")
                }
            if status.get("state") == "error":
                raise RuntimeError("Inference worker failed; inspect the node container logs")
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise RuntimeError("Inference node authentication failed") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError("Inference node did not become ready within the startup deadline")


def deploy(plan, args):
    existing = inspect("container", args.name)
    if existing and (not args.replace or not same_data(existing, plan["data"])):
        raise ValueError("Replacement requires --replace and the same inference data directory")
    if existing:
        preserve_container_policy(plan, existing)
    running = docker(["ps", "--format", "{{.Names}}"]).stdout.splitlines()
    for name in running:
        if name != args.name:
            other = inspect("container", name)
            if other and same_data(other, plan["data"]):
                raise ValueError("Another running container already owns this inference data directory")
    image = inspect("image", plan["image_reference"])
    if image is None and plan["image_reference"].startswith("sha256:"):
        raise ValueError("Exact local image ID is absent; provide its registry tag or digest to download")
    if args.dry_run:
        pinned = image["Id"] if image else plan["image_reference"]
        return {
            "dry_run": True,
            "image_present": image is not None,
            "would_pull": image is None,
            "configuration_preserved": True,
            "data": str(plan["data"]),
            "environment_check": environment_command(plan, pinned),
            "run": run_command(plan, pinned, args),
            "replaces_existing": existing is not None,
        }
    if image is None:
        docker(["pull", plan["image_reference"]])
        image = inspect("image", plan["image_reference"])
    if not image or not re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("Id", "")):
        raise RuntimeError("Docker did not return an immutable image ID")
    pinned = image["Id"]
    environment = subprocess.run(environment_command(plan, pinned), capture_output=True, text=True)
    if environment.returncode:
        raise RuntimeError(
            "Environment preflight failed; select the image matching this program's dependencies"
        )
    initialize_data(plan)
    backup = None
    restart = None
    try:
        if existing:
            restart = existing["HostConfig"]["RestartPolicy"]
            backup = args.name + "-previous-" + uuid4().hex[:10]
            docker(["update", "--restart=no", args.name])
            docker(["stop", "--timeout", "45", args.name])
            print(json.dumps({"event": "stopped", "name": args.name}), flush=True)
            docker(["rename", args.name, backup])
        result = subprocess.run(run_command(plan, pinned, args), capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("New inference container could not start")
        status = wait_ready(plan, args)
    except Exception:
        failed = inspect("container", args.name)
        if failed and (not existing or failed["Id"] != existing["Id"]):
            if failed.get("Config", {}).get("Labels", {}).get(
                "tuber-studio.service"
            ) == "inference-node" and same_data(failed, plan["data"]):
                docker(["update", "--restart=no", args.name])
                docker(["stop", "--timeout", "45", args.name])
                docker(["rename", args.name, args.name + "-failed-" + uuid4().hex[:10]])
        if existing:
            if backup and inspect("container", backup):
                docker(["rename", backup, args.name])
            policy = restart["Name"]
            if policy == "on-failure" and restart.get("MaximumRetryCount"):
                policy += ":" + str(restart["MaximumRetryCount"])
            docker(["update", "--restart=" + policy, args.name])
            if existing["State"]["Running"]:
                docker(["start", args.name])
        raise
    return {
        "state": "ready",
        "image_id": pinned,
        "name": args.name,
        "previous_container": backup,
        "token_file": str(plan["data"] / "inference-token"),
        **status,
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--data", type=Path, required=True)
    result.add_argument(
        "--assets", type=Path, required=True, help="Root containing models/engines/tts/nltk_data"
    )
    result.add_argument("--node-assets", type=Path, help="Optional independent vision-projector directory")
    result.add_argument("--image")
    result.add_argument("--name", default="tuber-inference")
    result.add_argument(
        "--host",
        default=os.environ.get("TUBER_INFERENCE_HOST", "127.0.0.1"),
        help="Host interface for the authenticated broker only (set 0.0.0.0 for LAN/remote access)",
    )
    result.add_argument(
        "--port", type=int, default=9012, help="Published host port; native worker ports stay private"
    )
    result.add_argument("--user", help="Non-root UID:GID; defaults to the current POSIX user")
    result.add_argument("--ready-timeout", type=float, default=180)
    result.add_argument("--replace", action="store_true")
    result.add_argument("--dry-run", action="store_true")
    return result


def main():
    args = parser().parse_args()
    try:
        print(json.dumps(deploy(setup(args), args), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
