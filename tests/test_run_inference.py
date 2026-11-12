import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "run_inference", Path(__file__).resolve().parents[1] / "tools/deployment/run_inference.py"
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)
IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    source, assets, data = (tmp_path / name for name in ("program", "assets", "node-data"))
    source.mkdir()
    for relative in launcher.PUBLIC_PATHS:
        p = source / relative
        if Path(relative).suffix:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("public")
        else:
            p.mkdir(exist_ok=True)
    for relative in (
        "tools/jetson/inference-node.py",
        "tools/jetson/environment.py",
        "src/tuber_studio/inference.py",
    ):
        p = source / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# public source")
    for name in launcher.ASSET_PATHS:
        (assets / name).mkdir(parents=True)
    download_manifest = source / "config/jetson/downloads.json"
    download_manifest.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "config/jetson/downloads.json", download_manifest)
    for entry in json.loads(download_manifest.read_text(encoding="utf-8"))["files"]:
        if entry["path"].endswith(".gguf"):
            (assets / entry["path"]).write_bytes(b"fixed-model-asset")
    args = launcher.parser().parse_args(
        [
            "--source",
            str(source),
            "--data",
            str(data),
            "--assets",
            str(assets),
            "--image",
            "registry.example/tuber:environment",
            "--user",
            "1001:1001",
        ]
    )
    # Production uses the host owner's UID; command construction is portable.
    if hasattr(os, "getuid"):
        if os.getuid() == 0:
            monkeypatch.setattr(launcher.os, "getuid", lambda: 1001)
        args.user = f"{os.getuid()}:{os.getgid()}"
    return args


def test_command_mounts_only_public_source_and_read_only_assets(deployment):
    args = deployment
    args.dry_run = True
    plan = launcher.setup(args)
    command = launcher.run_command(plan, IMAGE, args)
    mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
    assert any(
        f"src={args.data},dst=/workspace/tuber/runtime" == m.removeprefix("type=bind,") for m in mounts
    )
    assert all(
        m.endswith(",readonly")
        for m in mounts
        if "dst=/workspace/tuber/runtime," not in m and not m.endswith("dst=/workspace/tuber/runtime")
    )
    assert not any(f"src={args.source}," in m for m in mounts)
    assert not any(".git" in m or ".env" in m for m in mounts)
    assert command[command.index("--publish") + 1] == "127.0.0.1:9012:9012"
    assert command.count("--publish") == 1 and "9030" not in command and "9020" not in command
    assert command[-7:] == [
        "tools/jetson/inference-node.py",
        "--root",
        "/workspace/tuber",
        "--host",
        "0.0.0.0",
        "--port",
        "9012",
    ]
    assert IMAGE in command and "--pull=never" in command
    assert command[command.index("--health-cmd") + 1] == (
        "/opt/studio-venv/bin/python /workspace/tuber/tools/jetson/inference-node.py "
        "--root /workspace/tuber --port 9012 --healthcheck"
    )
    assert command[command.index("--health-interval") + 1] == "20s"
    assert command[command.index("--health-timeout") + 1] == "10s"
    assert command[command.index("--health-start-period") + 1] == "420s"
    assert command[command.index("--health-retries") + 1] == "3"
    check = launcher.environment_command(plan, IMAGE)
    assert check[check.index("--network") + 1] == "none"
    assert check[check.index("--runtime") + 1] == "runc"
    assert not any("dst=/workspace/tuber/runtime" in value for value in check)


def test_new_node_uses_the_same_9b_in_both_modes(deployment):
    deployment.dry_run = True
    (Path(deployment.assets) / "models/MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf").touch()
    plan = launcher.setup(deployment)
    node = plan["node"]
    assert node["operations_model"] == node["live_model"] == "dafeiyu-qwen35-9b:1.0.0"
    assert node["context_tokens"] == 262144
    assert not any(key.startswith("gemma_") for key in node)
    assert "node-assets" not in plan["mounts"]


def test_9b_preflight_rejects_a_missing_projector_before_starting_docker(deployment):
    deployment.dry_run = True
    projector, = deployment.assets.glob("models/mmproj-*.gguf")
    projector.unlink()
    with pytest.raises(ValueError, match="mmproj-dafeiyu"):
        launcher.setup(deployment)


@pytest.mark.parametrize(
    "name",
    [
        "studio.sqlite3",
        "bilibili-credentials.json",
        "admin-token",
        "provider-credentials.json",
        ".business-authority.json",
        "business-authority.json",
    ],
)
def test_business_data_is_rejected_before_docker_or_mutation(deployment, name):
    deployment.data.mkdir()
    (deployment.data / name).write_text("private-business-data")
    with pytest.raises(ValueError, match="business state|credentials"):
        launcher.setup(deployment)
    assert not (deployment.data / "inference-token").exists()


def test_database_magic_and_full_business_configuration_are_rejected(deployment):
    deployment.data.mkdir()
    p = deployment.data / "renamed.bin"
    p.write_bytes(b"SQLite format 3\x00" + b"payload")
    with pytest.raises(ValueError, match="database"):
        launcher.setup(deployment)
    p.unlink()
    (deployment.data / "studio.json").write_text('{"llm":{"kind":"demo"}}')
    with pytest.raises(ValueError, match="only TTS"):
        launcher.setup(deployment)


def test_initialization_preserves_existing_pairing_and_configuration(deployment):
    args = deployment
    plan = launcher.setup(args)
    launcher.initialize_data(plan)
    token = (args.data / "inference-token").read_bytes()
    config = args.data / "inference-node.json"
    value = json.loads(config.read_text())
    value["context_tokens"] = 32768
    config.write_text(json.dumps(value))
    before = {p.name: p.read_bytes() for p in args.data.iterdir() if p.is_file()}
    launcher.initialize_data(launcher.setup(args))
    assert before == {p.name: p.read_bytes() for p in args.data.iterdir() if p.is_file()}
    assert len(token) >= 32
    assert set(json.loads((args.data / "studio.json").read_text())) == {"tts", "inference_broker"}
    if os.name == "posix":
        assert (args.data / "inference-token").stat().st_mode & 0o777 == 0o600


def test_model_configuration_cannot_escape_read_only_mounts(deployment):
    deployment.data.mkdir()
    settings = copy.deepcopy(launcher.DEFAULT_NODE)
    settings["operations_model"] = "Gemma4-E2B"
    settings["gemma_model"] = "runtime/tmp/main.gguf"
    (deployment.data / "inference-node.json").write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="read-only asset"):
        launcher.setup(deployment)


def test_existing_data_node_assets_are_reused(deployment):
    deployment.data.mkdir()
    node_assets = deployment.data / "node-assets"
    node_assets.mkdir()
    name = Path(launcher.GEMMA_NODE_ASSETS["gemma_projector"]).name
    (node_assets / name).write_bytes(b"projector")
    assert launcher.setup(deployment)["mounts"]["node-assets"] == node_assets


class DockerFixture:
    def __init__(self, monkeypatch, args, *, existing=False, image=True):
        self.args, self.commands, self.image = args, [], image
        self.containers = {}
        if existing:
            self.containers[args.name] = self.container("old", running=True)
        monkeypatch.setattr(launcher, "docker", self.docker)
        monkeypatch.setattr(launcher.subprocess, "run", self.run)
        monkeypatch.setattr(launcher, "wait_ready", lambda *a: {
            "state": "active", "model": launcher.DEFAULT_NODE["operations_model"],
        })

    def container(self, identity, *, running=False):
        return {
            "Id": identity,
            "State": {"Running": running},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "Config": {"Labels": {"tuber-studio.service": "inference-node"}},
            "Mounts": [
                {"Type": "bind", "Source": str(self.args.data), "Destination": "/workspace/tuber/runtime"}
            ],
        }

    def docker(self, command, **kwargs):
        self.commands.append(command)
        code, output, error = 0, "", ""
        if command[:2] == ["image", "inspect"]:
            code, output, error = (
                (0, json.dumps([{"Id": IMAGE}]), "") if self.image else (1, "", "No such image")
            )
        elif command[:2] == ["container", "inspect"]:
            value = self.containers.get(command[2])
            code, output, error = (0, json.dumps([value]), "") if value else (1, "", "No such container")
        elif command[0] == "pull":
            self.image = True
        elif command[0] == "rename":
            self.containers[command[2]] = self.containers.pop(command[1])
        elif command[0] in {"stop", "start"}:
            self.containers[command[-1]]["State"]["Running"] = command[0] == "start"
        return subprocess.CompletedProcess(command, code, output, error)

    def run(self, command, **kwargs):
        self.commands.append(command)
        if "--detach" in command:
            self.containers[self.args.name] = self.container("new", running=True)
        return subprocess.CompletedProcess(command, 0, "new", "")


def test_existing_image_is_pinned_without_pull_and_environment_precedes_run(deployment, monkeypatch):
    fake = DockerFixture(monkeypatch, deployment)
    result = launcher.deploy(launcher.setup(deployment), deployment)
    assert result["image_id"] == IMAGE
    assert not any(cmd[0] == "pull" for cmd in fake.commands)
    executions = [c for c in fake.commands if c[:2] == ["docker", "run"]]
    assert len(executions) == 2 and "runc" in executions[0] and "nvidia" in executions[1]
    assert all(IMAGE in c and "--pull=never" in c for c in executions)


def test_missing_image_is_pulled_once_then_pinned(deployment, monkeypatch):
    fake = DockerFixture(monkeypatch, deployment, image=False)
    launcher.deploy(launcher.setup(deployment), deployment)
    assert [c for c in fake.commands if c[0] == "pull"] == [["pull", deployment.image]]
    assert all(IMAGE in c for c in fake.commands if c[:2] == ["docker", "run"])


def test_dry_run_does_not_write_pull_or_start(deployment, monkeypatch):
    deployment.dry_run = True
    fake = DockerFixture(monkeypatch, deployment, image=False)
    result = launcher.deploy(launcher.setup(deployment), deployment)
    assert result["would_pull"] and not deployment.data.exists()
    assert all(c[1] == "inspect" or c[0] == "ps" for c in fake.commands)
    assert "inference-token" not in json.dumps(result)


def test_replace_requires_same_data_and_explicit_flag(deployment, monkeypatch):
    deployment.data.mkdir()
    fake = DockerFixture(monkeypatch, deployment, existing=True)
    with pytest.raises(ValueError, match="--replace"):
        launcher.deploy(launcher.setup(deployment), deployment)
    deployment.replace = True
    other = deployment.data.parent / "unrelated-data"
    other.mkdir()
    fake.containers[deployment.name]["Mounts"][0]["Source"] = str(other)
    with pytest.raises(ValueError, match="same inference data"):
        launcher.deploy(launcher.setup(deployment), deployment)
    assert not any(c[0] in {"stop", "rename"} for c in fake.commands)


def test_successful_replace_retains_stopped_previous_container(deployment, monkeypatch):
    deployment.data.mkdir()
    deployment.replace = True
    fake = DockerFixture(monkeypatch, deployment, existing=True)
    result = launcher.deploy(launcher.setup(deployment), deployment)
    previous = fake.containers[result["previous_container"]]
    assert previous["Id"] == "old" and not previous["State"]["Running"]
    assert fake.containers[deployment.name]["Id"] == "new"
    assert not any(c[0] == "rm" for c in fake.commands)


def test_failed_new_worker_restores_previous_container(deployment, monkeypatch):
    deployment.data.mkdir()
    deployment.replace = True
    fake = DockerFixture(monkeypatch, deployment, existing=True)

    def fail(*args):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(launcher, "wait_ready", fail)
    with pytest.raises(RuntimeError, match="worker failed"):
        launcher.deploy(launcher.setup(deployment), deployment)
    assert fake.containers[deployment.name]["Id"] == "old"
    assert fake.containers[deployment.name]["State"]["Running"]
    assert any("-failed-" in name for name in fake.containers)
    assert not any(c[0] == "rm" for c in fake.commands)


def test_missing_exact_image_id_cannot_trigger_an_unpinned_pull(deployment, monkeypatch):
    deployment.image = IMAGE
    fake = DockerFixture(monkeypatch, deployment, image=False)
    with pytest.raises(ValueError, match="Exact local image ID"):
        launcher.deploy(launcher.setup(deployment), deployment)
    assert not any(c[0] == "pull" for c in fake.commands)
    assert not deployment.data.exists()


def test_environment_failure_keeps_existing_service_running(deployment, monkeypatch):
    deployment.data.mkdir()
    deployment.replace = True
    fake = DockerFixture(monkeypatch, deployment, existing=True)
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "")
    )
    with pytest.raises(RuntimeError, match="Environment preflight"):
        launcher.deploy(launcher.setup(deployment), deployment)
    assert fake.containers[deployment.name]["State"]["Running"]
    assert not any(c[0] in {"stop", "rename", "pull"} for c in fake.commands)


def test_dry_run_never_outputs_the_persistent_token(deployment, monkeypatch):
    plan = launcher.setup(deployment)
    launcher.initialize_data(plan)
    secret = (deployment.data / "inference-token").read_text()
    deployment.dry_run = True
    DockerFixture(monkeypatch, deployment)
    output = json.dumps(launcher.deploy(launcher.setup(deployment), deployment))
    assert secret not in output


def test_configured_absolute_container_assets_and_ipv6_interface(deployment):
    deployment.data.mkdir()
    settings = copy.deepcopy(launcher.DEFAULT_NODE)
    settings["operations_model"] = "Gemma4-E2B"
    for relative in launcher.GEMMA_NODE_ASSETS.values():
        asset = deployment.assets.joinpath(*Path(relative).parts[1:])
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"fixed-compatible-asset")
    settings["gemma_model"] = "/workspace/tuber/" + launcher.GEMMA_NODE_ASSETS["gemma_model"]
    (deployment.data / "inference-node.json").write_text(json.dumps(settings))
    deployment.host = "::1"
    command = launcher.run_command(launcher.setup(deployment), IMAGE, deployment)
    assert command[command.index("--publish") + 1] == "[::1]:9012:9012"


def test_asset_root_cannot_be_reused_as_writable_node_data(deployment):
    deployment.data = deployment.assets
    with pytest.raises(ValueError, match="independent"):
        launcher.setup(deployment)


def test_second_running_container_cannot_share_node_data(deployment, monkeypatch):
    deployment.data.mkdir()
    fake = DockerFixture(monkeypatch, deployment)
    fake.containers["other-node"] = fake.container("other", running=True)
    original = fake.docker

    def docker(command, **kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "other-node\n", "")
        return original(command, **kwargs)

    monkeypatch.setattr(launcher, "docker", docker)
    with pytest.raises(ValueError, match="Another running container"):
        launcher.deploy(launcher.setup(deployment), deployment)


def test_ready_check_rejects_a_wrong_loaded_context(deployment, monkeypatch):
    import io

    launcher.initialize_data(launcher.setup(deployment))
    status = {
        "state": "active", "mode": "text", "model": launcher.DEFAULT_NODE["operations_model"],
        "requested_context_tokens": 4096,
    }

    class Opener:
        def open(self, request, timeout):
            assert timeout == 5
            return io.BytesIO(json.dumps(status).encode())

    monkeypatch.setattr(launcher.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(RuntimeError, match="differs"):
        launcher.wait_ready(launcher.setup(deployment), deployment)


def test_stop_failure_restores_original_restart_policy_without_renaming(deployment, monkeypatch):
    deployment.data.mkdir()
    deployment.replace = True
    fake = DockerFixture(monkeypatch, deployment, existing=True)
    original = fake.docker

    def docker(command, **kwargs):
        if command[0] == "stop":
            raise RuntimeError("stop failed")
        return original(command, **kwargs)

    monkeypatch.setattr(launcher, "docker", docker)
    with pytest.raises(RuntimeError, match="stop failed"):
        launcher.deploy(launcher.setup(deployment), deployment)
    assert fake.containers[deployment.name]["Id"] == "old"
    assert fake.containers[deployment.name]["State"]["Running"]
    assert ["update", "--restart=unless-stopped", deployment.name] in fake.commands
    assert not any(c[0] == "rename" for c in fake.commands)


def test_failed_first_install_stops_new_worker_and_keeps_diagnostics(deployment, monkeypatch):
    fake = DockerFixture(monkeypatch, deployment)

    def fail(*args):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(launcher, "wait_ready", fail)
    with pytest.raises(RuntimeError, match="worker failed"):
        launcher.deploy(launcher.setup(deployment), deployment)
    assert deployment.name not in fake.containers
    assert len(fake.containers) == 1
    assert not next(iter(fake.containers.values()))["State"]["Running"]


def test_code_replacement_preserves_existing_container_resource_policy(deployment, monkeypatch):
    deployment.data.mkdir()
    deployment.replace = True
    fake = DockerFixture(monkeypatch, deployment, existing=True)
    fake.containers[deployment.name]["HostConfig"].update(
        {
            "ReadonlyRootfs": True,
            "CapDrop": None,
            "PidsLimit": None,
            "Ulimits": [],
            "SecurityOpt": ["no-new-privileges=true"],
            "NetworkMode": "node-private",
            "ShmSize": 67108864,
            "IpcMode": "private",
            "Tmpfs": {"/tmp": "rw,nosuid,size=268435456"},
        }
    )
    launcher.deploy(launcher.setup(deployment), deployment)
    command = next(c for c in fake.commands if "--detach" in c)
    assert "--cap-drop" not in command and "--pids-limit" not in command and "--ulimit" not in command
    assert command[command.index("--network") + 1] == "node-private"
    assert command[command.index("--tmpfs") + 1] == "/tmp:rw,nosuid,size=268435456"
    assert command.count("--tmpfs") == 1
