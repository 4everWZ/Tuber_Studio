"""Runner contracts use tiny subprocesses and a fake HTTP server, never model weights."""

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/jetson/vlm_experiment.py"
SPEC = importlib.util.spec_from_file_location("vlm_experiment", SCRIPT)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


@pytest.mark.parametrize("observed", [[], ["/different", "--changed"]])
def test_spawn_identity_uses_known_launch_command_only_when_proc_cmdline_is_empty(observed):
    class Process:
        pid = 123

        def oneshot(self):
            return nullcontext()

        def create_time(self):
            return 12.5

        def exe(self):
            return "/known/server"

        def cmdline(self):
            return observed

    command = ["/known/server", "--model", "/known/weights"]
    identity = experiment.process_identity(Process(), launch_command=command)
    assert identity["command"] == (observed or command)
    if not observed:
        assert identity["observed_command_at_start"] == []
        assert identity["command_source"] == "popen_launch_arguments_after_empty_proc_cmdline"
        command.append("--later-mutation")
        assert identity["command"] != command
    else:
        assert "command_source" not in identity


@pytest.mark.parametrize("failure", ["transient_sharing", "persistent_sharing", "permissions", "disk"])
def test_atomic_metrics_retry_only_bounded_windows_sharing_violations(tmp_path, monkeypatch, failure):
    target = tmp_path / "metrics.json"
    target.write_text('{"state":"old"}', encoding="utf-8")
    original = Path.replace
    calls, sleeps = [], []

    def replace(path, destination):
        calls.append((path, destination))
        if failure == "transient_sharing" and len(calls) > 2:
            return original(path, destination)
        error = OSError("fixture replace failure")
        if failure != "disk":
            error.winerror = 32 if "sharing" in failure else 5
        raise error

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(experiment.time, "sleep", sleeps.append)
    if failure == "transient_sharing":
        experiment.atomic_json(target, {"state": "complete", "counter": 123})
        assert json.loads(target.read_text()) == {"state": "complete", "counter": 123}
        assert len(calls) == 3 and sleeps == [0.005, 0.01]
    else:
        with pytest.raises(OSError, match="fixture replace failure"):
            experiment.atomic_json(target, {"state": "complete"})
        assert json.loads(target.read_text()) == {"state": "old"}
        if failure == "persistent_sharing":
            assert len(calls) == 6 and len(sleeps) == 5 and sum(sleeps) <= 0.2
        else:
            assert len(calls) == 1 and sleeps == []
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("mismatch", ["executable", "command", "ownership stamp"])
@pytest.mark.parametrize("exiting", [False, True])
def test_identity_mismatch_rechecks_concurrent_exit_without_accepting_changed_live_process(
    monkeypatch, mismatch, exiting
):
    class Process:
        checks = 0

        def status(self):
            self.checks += 1
            return psutil.STATUS_ZOMBIE if exiting and self.checks > 1 else psutil.STATUS_RUNNING

        def create_time(self):
            return 123.0

        def is_running(self):
            return True

        def exe(self):
            return "" if mismatch == "executable" else "/owned/llama-server"

        def cmdline(self):
            return [] if mismatch == "command" else ["owned"]

        def environ(self):
            return {}

    monkeypatch.setattr(psutil, "Process", lambda pid: Process())
    identity = {"pid": 123, "create_time": 123.0, "executable": "/owned/llama-server", "command": ["owned"]}
    if exiting:
        assert experiment.process_matches(identity, "nonce") is None
    else:
        expected = "ownership stamp" if mismatch == "ownership stamp" else "changed " + mismatch
        with pytest.raises(ValueError, match=expected):
            experiment.process_matches(identity, "nonce")


def arguments(tmp_path, *extra):
    return experiment.parser().parse_args(
        [
            "serve",
            "--profile",
            "mini4_7",
            "--model-root",
            str(tmp_path / "model root"),
            "--llama-binary",
            str(tmp_path / "llama-server"),
            "--run-dir",
            str(tmp_path / "run"),
            *extra,
        ]
    )


def test_registry_pins_all_requested_models_and_projectors():
    registry = experiment.load_registry()
    candidates = {
        key: profile for key, profile in registry["profiles"].items() if not profile.get("variant_of")
    }
    assert set(candidates) == {"mini4_7", "lfm3vl", "qwen35_4b", "gemma4_e2b", "gemma4_e4b"}
    for profile in registry["profiles"].values():
        if profile.get("variant_of"):
            parent = candidates[profile["variant_of"]]
            assert profile["original_model"] == parent["original_model"]
            assert profile["native_context_tokens"] == parent["native_context_tokens"]
        assert len(profile["conversion_revision"]) == len(profile["original_model"]["revision"]) == 40
        assert profile["license"]
        assert profile["provenance"]
        assert set(profile["artifacts"]) == {"model", "projector"}
        for artifact in profile["artifacts"].values():
            assert len(bytes.fromhex(artifact["sha256"])) == 32
            assert artifact["size_bytes"] > 0
            assert profile["conversion_revision"] in experiment.artifact_url(profile, artifact)
        observed = profile["gguf_observed"]
        assert observed["http_status"] == 206
        assert not observed["whole_file_sha256_verified_by_this_observation"]
        assert observed["native_context_tokens"] == profile["native_context_tokens"]
        assert observed["expected_whole_file_sha256"] == profile["artifacts"]["model"]["sha256"]
        assert observed["source_url"] == experiment.artifact_url(profile, profile["artifacts"]["model"])
        assert sum(observed["tensor_type_histogram"].values()) == observed["tensor_count"]
        assert observed["parsed_metadata_and_tensor_info_bytes"] <= observed["partial_read_bytes"]
        projector = observed["projector"]
        if projector["whole_file_sha256_verified_by_this_observation"]:
            assert len(bytes.fromhex(projector["partial_read_sha256"])) == 32
            assert projector["parsed_metadata_and_tensor_info_bytes"] <= projector["partial_read_bytes"]
        else:
            assert projector["http_status"] == 206
        assert projector["expected_whole_file_sha256"] == profile["artifacts"]["projector"]["sha256"]
        assert sum(projector["tensor_type_histogram"].values()) == projector["tensor_count"]
    mini = registry["profiles"]["mini4_7"]
    assert mini["artifacts"]["model"]["file"].startswith("thinking/")
    assert mini["artifacts"]["projector"]["file"] == "mmproj-model-f16.gguf"
    assert mini["artifacts"]["projector"]["quantization"] == "BF16"
    assert mini["artifacts"]["projector"]["filename_quantization_label"] == "F16"
    mini4x = registry["profiles"]["mini4_7_4x"]
    assert mini4x["variant_of"] == "mini4_7"
    assert mini4x["artifacts"]["model"] == mini["artifacts"]["model"]
    assert mini4x["artifacts"]["projector"]["file"] == "mmproj-model-f16-4x.gguf"
    assert mini["gguf_observed"]["projector"]["scale_factor"] == 4
    assert mini4x["gguf_observed"]["projector"]["scale_factor"] == 2
    assert mini4x["artifacts"]["projector"]["sha256"] != mini["artifacts"]["projector"]["sha256"]
    assert registry["profiles"]["qwen35_4b"]["provenance"] == "third_party_conversion"
    assert registry["profiles"]["gemma4_e2b"]["artifacts"]["projector"]["quantization"] == "Q8_0"
    for key in ("gemma4_e2b", "gemma4_e4b"):
        profile = registry["profiles"][key]
        speculative = profile["speculative"]
        assert speculative["type"] == "draft-mtp"
        assert speculative["target_model_sha256"] == profile["artifacts"]["model"]["sha256"]
        assert speculative["source_model"]["qat"]
        assert speculative["artifact"]["quantization"] == "Q4_0"
        assert len(bytes.fromhex(speculative["artifact"]["sha256"])) == 32
        assert profile["conversion_revision"] in experiment.artifact_url(profile, speculative["artifact"])


def test_command_keeps_requested_context_and_one_slot_without_fitting(tmp_path):
    args = arguments(
        tmp_path,
        "--alias",
        "literal alias; no shell",
        "--mmproj-offload",
        "cpu",
        "--mmap",
        "off",
        "--kv-v",
        "f16",
        "--ubatch",
        "32",
        "--image-max-tokens",
        "512",
    )
    artifacts = {"model": {"path": "a model.gguf"}, "projector": {"path": "a projector.gguf"}}
    command = experiment.build_command(args, artifacts)
    for flag, value in [
        ("--ctx-size", "131072"),
        ("--parallel", "1"),
        ("--fit", "off"),
        ("--cache-ram", "0"),
        ("--ctx-checkpoints", "0"),
        ("--cache-type-k", "q4_0"),
        ("--cache-type-v", "f16"),
        ("--ubatch-size", "32"),
        ("--reasoning", "on"),
        ("--alias", "literal alias; no shell"),
        ("--image-max-tokens", "512"),
        ("--load-mode", "none"),
        ("--host", "127.0.0.1"),
        ("--log-verbosity", "4"),
    ]:
        assert command[command.index(flag) + 1] == value
    assert "--no-context-shift" in command
    assert "--no-mmproj-offload" in command
    assert "--kv-offload" in command
    assert "--no-mmap" not in command  # Removed by the pinned llama.cpp revision.
    assert "a model.gguf" in command


def test_private_container_listen_host_and_explicit_direct_io_map_current_engine_cli(tmp_path):
    artifacts = {"model": {"path": "m"}, "projector": {"path": "p"}}
    args = arguments(tmp_path, "--listen-host", "0.0.0.0", "--mmap", "off", "--direct-io", "on")
    command = experiment.build_command(args, artifacts)
    assert command[command.index("--host") + 1] == "0.0.0.0"
    assert command[command.index("--load-mode") + 1] == "dio"
    assert "--direct-io" not in command  # The pinned C++ CLI calls this --load-mode dio.
    default = experiment.build_command(arguments(tmp_path), artifacts)
    assert default[default.index("--load-mode") + 1] == "mmap"
    with pytest.raises(ValueError, match="explicit --mmap off"):
        experiment.build_command(arguments(tmp_path, "--direct-io", "on"), artifacts)
    with pytest.raises(SystemExit):
        arguments(tmp_path, "--listen-host", "8.8.8.8")
    cpu_kv = experiment.build_command(arguments(tmp_path, "--kv-offload", "cpu"), artifacts)
    assert "--no-kv-offload" in cpu_kv
    assert "--kv-offload" not in cpu_kv


def test_native_lfm_baseline_and_explicit_extension_have_different_claims():
    registry = experiment.load_registry()
    lfm = registry["profiles"]["lfm3vl"]
    baseline = experiment.context_contract(lfm, 128000, False, 131072)
    assert not baseline["meets_strict_window"]
    assert not baseline["native_meets_strict_window"]
    assert not baseline["experimental_native_extension"]
    with pytest.raises(ValueError, match="allow-native-extension"):
        experiment.context_contract(lfm, 131072, False, 131072)
    extension = experiment.context_contract(lfm, 131072, True, 131072)
    assert extension["meets_strict_window"]
    assert extension["experimental_native_extension"]
    assert not extension["native_meets_strict_window"]
    assert not extension["long_context_accuracy_validated"]


def test_optional_reasoning_and_yarn_settings_are_explicit_and_do_not_change_defaults(tmp_path):
    artifacts = {"model": {"path": "m"}, "projector": {"path": "p"}}
    default = experiment.build_command(arguments(tmp_path), artifacts)
    assert "--reasoning-budget" not in default and "--rope-scaling" not in default
    args = arguments(
        tmp_path,
        "--reasoning-budget",
        "1536",
        "--rope-scaling",
        "yarn",
        "--rope-freq-scale",
        "0.9765625",
        "--yarn-orig-ctx",
        "128000",
    )
    command = experiment.build_command(args, artifacts)
    for flag, value in [
        ("--reasoning-budget", "1536"),
        ("--rope-scaling", "yarn"),
        ("--rope-freq-scale", "0.9765625"),
        ("--yarn-orig-ctx", "128000"),
    ]:
        assert command[command.index(flag) + 1] == value
    assert command[command.index("--ctx-size") + 1] == "131072"
    assert command[command.index("--reasoning") + 1] == "on"
    with pytest.raises(ValueError, match="reasoning-budget"):
        experiment.build_command(arguments(tmp_path, "--reasoning-budget", "-2"), artifacts)
    with pytest.raises(ValueError, match="positive"):
        experiment.build_command(arguments(tmp_path, "--rope-scale", "nan"), artifacts)


def test_invalid_batches_do_not_silently_change_parameters(tmp_path):
    args = arguments(tmp_path, "--batch", "32", "--ubatch", "64")
    with pytest.raises(ValueError, match="ubatch"):
        experiment.build_command(args, {"model": {"path": "m"}, "projector": {"path": "p"}})


@pytest.mark.parametrize(
    ("loading", "expected"),
    [
        (("--mmap", "on"), "mmap"),
        (("--mmap", "off", "--direct-io", "on"), "dio"),
        (("--mmap", "off"), "none"),
    ],
)
def test_lazy_embedding_rows_are_explicit_and_preserve_loading_mode_and_context(tmp_path, loading, expected):
    artifacts = {"model": {"path": "m"}, "projector": {"path": "p"}}
    assert "--lazy-mode" not in experiment.build_command(arguments(tmp_path), artifacts)
    args = arguments(tmp_path, "--lazy-mode", "on", *loading)
    command = experiment.build_command(args, artifacts)
    assert command[command.index("--lazy-mode") + 1] == "on"
    assert command[command.index("--ctx-size") + 1] == "131072"
    assert command[command.index("--load-mode") + 1] == expected


@pytest.fixture
def fake_registry(tmp_path):
    root = tmp_path / "weights"
    root.mkdir()
    artifacts = {}
    for role in ("model", "projector"):
        content = ("tiny fixture " + role).encode()
        path = root / (role + ".gguf")
        path.write_bytes(content)
        artifacts[role] = {
            "file": path.name,
            "local_filename": path.name,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    profile = {
        "name": "fake HTTP fixture, not a model",
        "native_context_tokens": 131072,
        "conversion_repository": "fixture/not-downloaded",
        "conversion_revision": "1" * 40,
        "artifacts": artifacts,
    }
    registry = {
        "schema_version": 1,
        "strict_context_tokens": 131072,
        "llama_cpp_revision": "0" * 40,
        "profiles": {"mini4_7": profile},
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    return path, root, profile


def test_weights_are_not_repaired_overwritten_or_accepted_after_corruption(fake_registry):
    _, root, profile = fake_registry
    verified = experiment.verify_artifacts(profile, root)
    assert verified["model"]["sha256_verified"]
    model = root / "model.gguf"
    original = model.read_bytes()
    corrupted = b"X" + original[1:]
    model.write_bytes(corrupted)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        experiment.verify_artifacts(profile, root)
    assert model.read_bytes() == corrupted
    model.unlink()
    with pytest.raises(ValueError, match="missing or size mismatch"):
        experiment.verify_artifacts(profile, root)
    assert not model.exists()


def test_speculative_artifact_is_optional_until_requested_and_bound_to_the_target(fake_registry):
    _, root, profile = fake_registry
    with pytest.raises(ValueError, match="no registered artifact"):
        experiment.verify_artifacts(profile, root, spec_type="draft-mtp")
    content = b"tiny assistant fixture"
    profile["speculative"] = {
        "type": "draft-mtp",
        "target_model_sha256": profile["artifacts"]["model"]["sha256"],
        "artifact": {
            "file": "draft.gguf",
            "local_filename": "draft.gguf",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }
    # Ordinary runs work without installing the optional assistant.
    assert set(experiment.verify_artifacts(profile, root)) == {"model", "projector"}
    with pytest.raises(ValueError, match="draft missing or size mismatch"):
        experiment.verify_artifacts(profile, root, spec_type="draft-mtp")
    (root / "draft.gguf").write_bytes(content)
    verified = experiment.verify_artifacts(profile, root, spec_type="draft-mtp")
    assert verified["draft"]["sha256_verified"]
    assert verified["draft"]["source_url"] == experiment.artifact_url(
        profile, profile["speculative"]["artifact"]
    )
    (root / "draft.gguf").write_bytes(b"X" + content[1:])
    with pytest.raises(ValueError, match="draft SHA256 mismatch"):
        experiment.verify_artifacts(profile, root, spec_type="draft-mtp")
    profile["speculative"]["target_model_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="different target model"):
        experiment.verify_artifacts(profile, root, spec_type="draft-mtp")


def test_mtp_command_preserves_target_settings_and_needs_verified_assistant(tmp_path):
    artifacts = {"model": {"path": "main"}, "projector": {"path": "vision"}}
    default = experiment.build_command(arguments(tmp_path), artifacts)
    assert not any(flag.startswith("--spec-") for flag in default)
    args = arguments(
        tmp_path, "--spec-type", "draft-mtp", "--draft-n-max", "2", "--kv-v", "q8_0", "--draft-ubatch", "1"
    )
    with pytest.raises(ValueError, match="verified draft artifact"):
        experiment.build_command(args, artifacts)
    artifacts["draft"] = {"path": "assistant with spaces.gguf", "sha256_verified": True}
    command = experiment.build_command(args, artifacts)
    for flag, value in (
        ("--spec-type", "draft-mtp"),
        ("--spec-draft-model", "assistant with spaces.gguf"),
        ("--spec-draft-n-max", "2"),
        ("--spec-draft-sampling", "greedy"),
        ("--spec-draft-type-k", "q4_0"),
        ("--spec-draft-type-v", "q8_0"),
        ("--spec-draft-ngl", "99"),
        ("--spec-draft-n-ubatch", "1"),
        ("--cache-type-k", "q4_0"),
        ("--cache-type-v", "q8_0"),
        ("--ctx-size", "131072"),
        ("--parallel", "1"),
        ("--reasoning", "on"),
        ("--fit", "off"),
    ):
        assert command[command.index(flag) + 1] == value
    with pytest.raises(ValueError, match="draft-n-max"):
        experiment.build_command(arguments(tmp_path, "--draft-n-max", "0"), artifacts)
    with pytest.raises(SystemExit):
        arguments(tmp_path, "--draft-sampling", "stochastic")
    for value in ("-1", "257"):
        with pytest.raises(ValueError, match="draft-ubatch"):
            experiment.build_command(arguments(tmp_path, "--draft-ubatch", value), artifacts)


def test_cpu_draft_does_not_move_target_layers_or_change_window_and_kv(tmp_path):
    artifacts = {
        "model": {"path": "main"},
        "projector": {"path": "vision"},
        "draft": {"path": "assistant", "sha256_verified": True},
    }
    args = arguments(tmp_path, "--spec-type", "draft-mtp", "--draft-gpu-layers", "0")
    command = experiment.build_command(args, artifacts)
    assert command[command.index("--spec-draft-ngl") + 1] == "0"
    assert command[command.index("--gpu-layers") + 1] == "99"
    assert command[command.index("--ctx-size") + 1] == "131072"
    assert command[command.index("--cache-type-k") + 1] == "q4_0"
    assert command[command.index("--spec-draft-type-k") + 1] == "q4_0"
    for value in ("-1", "1000"):
        with pytest.raises(ValueError, match="draft-gpu-layers"):
            experiment.build_command(arguments(tmp_path, "--draft-gpu-layers", value), artifacts)


def test_cache_advice_is_explicit_and_only_opens_verified_two_files(fake_registry, monkeypatch):
    _, root, profile = fake_registry
    artifacts = experiment.verify_artifacts(profile, root)
    calls = []

    def advice(fd, offset, length, flag):
        os.fstat(fd)  # Descriptor must still be open during the request.
        calls.append((fd, offset, length, flag))

    monkeypatch.setattr(os, "posix_fadvise", advice, raising=False)
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    assert experiment.advise_model_cache(artifacts, False)["files"] == []
    assert calls == []
    result = experiment.advise_model_cache(artifacts, True)
    assert {item["path"] for item in result["files"]} == {
        artifacts["model"]["path"],
        artifacts["projector"]["path"],
    }
    assert all(item["success"] for item in result["files"])
    assert len(calls) == 2 and all(call[1:] == (0, 0, 4) for call in calls)
    for fd, *_ in calls:
        with pytest.raises(OSError):
            os.fstat(fd)

    def unsupported(*args):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(os, "posix_fadvise", unsupported)
    failed = experiment.advise_model_cache(artifacts, True)
    assert all(not item["success"] and item["error"]["errno"] == 22 for item in failed["files"])


def test_memory_thresholds_and_summary_are_measured_without_context_adaptation(tmp_path):
    args = arguments(tmp_path)
    sample = {
        "server_tree_rss_bytes": 100,
        "system_used_bytes": 200,
        "system_available_bytes": 128 * experiment.MIB,
        "swap_used_bytes": 0,
    }
    assert experiment.memory_violation(sample, 0, args) == "minimum_available_memory_threshold"
    sample["system_available_bytes"] = 1000 * experiment.MIB
    sample["swap_used_bytes"] = 129 * experiment.MIB
    assert experiment.memory_violation(sample, 0, args) == "maximum_swap_growth_threshold"
    metrics = {}
    experiment.update_memory(metrics, sample)
    experiment.update_memory(metrics, {**sample, "server_tree_rss_bytes": 300, "swap_used_bytes": 0})
    assert metrics["memory"]["peak_server_tree_rss_bytes"] == 300
    assert metrics["memory"]["peak_swap_used_bytes"] == 129 * experiment.MIB
    assert args.context_tokens == 131072


def sleep_process(nonce):
    environment = {**os.environ, experiment.OWNER_ENV: nonce}
    options = {"start_new_session": True} if os.name == "posix" else {}
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=environment, **options)


def owner_for(process, nonce):
    return {
        "schema_version": 1,
        "experiment_id": nonce,
        "server": experiment.process_identity(psutil.Process(process.pid)),
        "children": [],
    }


def test_stop_refuses_changed_pid_command_and_nonce_without_signalling(tmp_path):
    child = sleep_process("fixture-owned")
    try:
        owner = owner_for(child, "fixture-owned")
        for field, value in [
            ("create_time", owner["server"]["create_time"] - 10),
            ("command", ["some other command"]),
            ("executable", "some other executable"),
        ]:
            invalid = {**owner, "server": {**owner["server"], field: value}}
            experiment.atomic_json(tmp_path / "owner.json", invalid)
            with pytest.raises(ValueError, match="Refusing"):
                experiment.stop_run(tmp_path, 0.1)
            assert child.poll() is None
            assert not (tmp_path / "stop.request.json").exists()
        experiment.atomic_json(tmp_path / "owner.json", {**owner, "experiment_id": "wrong-nonce"})
        with pytest.raises(ValueError, match="ownership stamp"):
            experiment.stop_run(tmp_path, 0.1)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_stop_avoids_pidfd_and_leaves_unrelated_process_alive(tmp_path, monkeypatch):
    child, unrelated = sleep_process("owned"), sleep_process("unrelated")
    try:
        experiment.atomic_json(tmp_path / "owner.json", owner_for(child, "owned"))

        def unusable_pidfd(*_args, **_kwargs):
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr(psutil, "wait_procs", unusable_pidfd)
        result = experiment.stop_run(tmp_path, 0.5)
        assert result["remaining_pids"] == []
        child.wait(timeout=5)
        assert unrelated.poll() is None
        assert experiment.stop_run(tmp_path, 0.1)["remaining_pids"] == []
    finally:
        for process in (child, unrelated):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


FAKE_SERVER = r"""
import argparse,json,subprocess,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--port',type=int);p.add_argument('--context',type=int);p.add_argument('--alias')
p.add_argument('--slot-context',type=int)
p.add_argument('--child-file');a=p.parse_args()
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path(a.child_file).write_text(str(child.pid))
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  result=({'default_generation_settings':{'n_ctx':a.context},'model_alias':a.alias}
          if self.path=='/props' else [{'n_ctx':a.slot_context or a.context}])
  payload=json.dumps(result).encode();self.send_response(200);self.end_headers();self.wfile.write(payload)
 def log_message(self,*args):pass
print('llama_context: n_ctx_per_seq = '+str(a.context),flush=True)
print('load_tensors: offloaded 4/4 layers to GPU',flush=True)
HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
"""


def launch_fake_runner(tmp_path, fake_registry, *, actual_context=131072, actual_slot_context=131072):
    registry, root, _ = fake_registry
    helper = tmp_path / "fake_server.py"
    helper.write_text(FAKE_SERVER, encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    run = tmp_path / "experiment"
    args = [
        "serve",
        "--profile",
        "mini4_7",
        "--registry",
        str(registry),
        "--model-root",
        str(root),
        "--llama-binary",
        sys.executable,
        "--run-dir",
        str(run),
        "--port",
        str(port),
        "--min-available-mib",
        "0",
        "--max-swap-growth-mib",
        "1048576",
        "--startup-timeout",
        "8",
    ]
    command = [
        sys.executable,
        str(helper),
        "--port",
        str(port),
        "--context",
        str(actual_context),
        "--alias",
        "mini4_7",
        "--child-file",
        str(tmp_path / "grandchild.pid"),
        "--slot-context",
        str(actual_slot_context),
    ]
    launcher = (
        "import importlib.util;"
        f"s=importlib.util.spec_from_file_location('runner',{str(SCRIPT)!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"m.build_command=lambda args,artifacts:{command!r};"
        f"raise SystemExit(m.serve(m.parser().parse_args({args!r})))"
    )
    output = (tmp_path / "runner.log").open("wb")
    process = subprocess.Popen([sys.executable, "-c", launcher], stdout=output, stderr=output)
    output.close()
    return process, run


def wait_metrics(run, process, state):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        path = run / "metrics.json"
        if path.exists():
            metrics = json.loads(path.read_text(encoding="utf-8"))
            if metrics.get("state") == state:
                return metrics
        if process.poll() is not None:
            break
        time.sleep(0.05)
    pytest.fail(f"Runner did not reach {state}: {path.read_text() if path.exists() else 'no metrics'}")


def cleanup_runner(process, run):
    if (run / "owner.json").exists():
        experiment.stop_run(run, 0.5)
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=5)


def test_foreground_runner_reports_actual_context_ram_and_collects_owned_tree(
    tmp_path, fake_registry, monkeypatch
):
    monkeypatch.setenv("LLAMA_ARG_CTX_SIZE", "4096")
    monkeypatch.setenv("LLAMA_ARG_API_KEY", "fixture-private-value")
    process, run = launch_fake_runner(tmp_path, fake_registry)
    try:
        metrics = wait_metrics(run, process, "ready")
        assert metrics["actual_context_tokens"] == 131072
        assert metrics["loading_seconds"] > 0
        assert metrics["actual_props"]["default_generation_settings"]["n_ctx"] == 131072
        assert metrics["listen_host"] == metrics["self_check_host"] == "127.0.0.1"
        assert "LLAMA_ARG_CTX_SIZE" in metrics["ignored_llama_environment_keys"]
        assert "LLAMA_ARG_API_KEY" in metrics["ignored_llama_environment_keys"]
        assert "fixture-private-value" not in (run / "metrics.json").read_text()
        assert process.poll() is None
        stop = subprocess.run(
            [sys.executable, str(SCRIPT), "stop", "--run-dir", str(run), "--grace-seconds", "0.5"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert stop.returncode == 0, stop.stderr
        assert process.wait(timeout=8) == 0
        final = json.loads((run / "metrics.json").read_text())
        assert final["termination_reason"] == "operator_stop"
        assert final["cleanup"]["remaining_pids"] == []
        assert final["memory"]["peak_server_tree_rss_bytes"] > 0
        assert final["memory"]["minimum_system_available_bytes"] > 0
        assert final["actual_context_per_sequence_from_log"] == 131072
        assert final["cuda_and_allocation_lines"] == [
            "llama_context: n_ctx_per_seq = 131072",
            "load_tensors: offloaded 4/4 layers to GPU",
        ]
        assert (run / "memory.jsonl").read_text().strip()
        try:
            grandchild = psutil.Process(int((tmp_path / "grandchild.pid").read_text()))
            assert not grandchild.is_running() or grandchild.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        cleanup_runner(process, run)


@pytest.mark.parametrize(
    ("actual", "slot", "reason"),
    [
        (8192, 8192, "actual_context_below_requested"),
        (131072, 8192, "actual_slot_context_below_requested"),
    ],
)
def test_runner_rejects_actual_context_shrinking_without_publishing_ready(
    tmp_path, fake_registry, actual, slot, reason
):
    process, run = launch_fake_runner(
        tmp_path, fake_registry, actual_context=actual, actual_slot_context=slot
    )
    try:
        assert process.wait(timeout=12) == 2
        metrics = json.loads((run / "metrics.json").read_text())
        assert metrics["termination_reason"] == reason
        assert metrics["actual_context_tokens"] == actual
        assert metrics["context_contract"]["requested_context_tokens"] == 131072
        assert "ready_at" not in metrics
        assert metrics["cleanup"]["remaining_pids"] == []
    finally:
        cleanup_runner(process, run)
