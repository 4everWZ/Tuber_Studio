"""Bootstrap scope, interrupted subprocess cleanup and immutable publication."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bootstrap():
    return load("bootstrap_test", Path(__file__).parents[1] / "tools/jetson/tts/bootstrap.py")


@pytest.fixture
def root(bootstrap, tmp_path, monkeypatch):
    # Isolate configuration policy from PyYAML, which belongs to the Jetson image.
    # The fixture uses JSON (also valid YAML); no parser behavior is changed here.
    monkeypatch.setitem(sys.modules, "yaml", SimpleNamespace(safe_load=json.loads))
    base = tmp_path / "project"
    (base / "config").mkdir(parents=True)
    (base / "runtime/tts").mkdir(parents=True)
    (base / "runtime/studio.json").write_text(
        json.dumps(
            {"tts": {"kind": "gpt_sovits", "voice_id": bootstrap.VOICE, "local_backend": "genie_onnx_int8"}}
        )
    )
    voice = {
        "id": bootstrap.VOICE,
        "provider": "gpt_sovits",
        "reference_text": bootstrap.TRANSCRIPT,
        "reference_asset": "runtime/tts/ref/yaoyao-reference32k.wav",
        "server_reference_path": str(base / "runtime/tts/ref/yaoyao-reference32k.wav"),
        "version": "UnlimitedBurst-c649dfd-v2-e10",
        "weights_version": "yaoyao-v2-e10-e10s200",
    }
    (base / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    config = {
        "version": "v2",
        "device": "cuda",
        "is_half": True,
        "t2s_weights_path": str(base / "runtime/tts/weights/yaoyao-s1-sanitized.ckpt"),
        "vits_weights_path": str(base / "runtime/tts/weights/yaoyao-s2-sanitized.pth"),
        "bert_base_path": str(base / "runtime/tts/pretrained/chinese-roberta-wwm-ext-large"),
        "cnhuhbert_base_path": str(base / "runtime/tts/pretrained/chinese-hubert-base"),
    }
    (base / "runtime/tts/tts_infer.yaml").write_text(json.dumps({"custom": config}))
    bundle = load("bootstrap_bundle_test", Path(__file__).parents[1] / "tools/jetson/tts/genie_bundle.py")
    monkeypatch.setattr(bootstrap, "helper", lambda r, name: bundle)
    return base, bundle


@pytest.mark.parametrize("field,value", [("voice_id", "other"), ("local_backend", "compact")])
def test_incompatible_operator_configuration_is_rejected_without_changes(bootstrap, root, field, value):
    base, _ = root
    path = base / "runtime/studio.json"
    data = json.loads(path.read_text())
    data["tts"][field] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="configuration is preserved"):
        bootstrap.prepare(base)
    assert path.read_bytes() == before
    assert not (base / "runtime/evaluations").exists()


@pytest.mark.parametrize(
    "change", [{"device": "cpu"}, {"is_half": False}, {"vits_weights_path": "/other/model.pth"}]
)
def test_reference_precision_and_paths_cannot_silently_change(bootstrap, root, change):
    base, _ = root
    path = base / "runtime/tts/tts_infer.yaml"
    config = json.loads(path.read_text())
    config["custom"].update(change)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="CUDA/FP16"):
        bootstrap.validate_configuration(base)


def test_verified_bundle_reuse_does_not_require_raw_downloads_or_start_workers(bootstrap, root, monkeypatch):
    base, bundle = root
    (base / "runtime/tts/genie" / bootstrap.VOICE).mkdir(parents=True)
    calls = []
    monkeypatch.setattr(bundle, "verify_bundle", lambda r, v: calls.append((r, v)) or {"identity": "a" * 64})
    monkeypatch.setattr(bootstrap, "ensure_no_workers", lambda: pytest.fail("Must not prepare on reuse"))
    assert bootstrap.prepare(base) == {"reused": True, "bundle_identity": "a" * 64}
    assert calls == [(base, bootstrap.VOICE)]
    assert not (base / "runtime/tts/sources").exists()


def test_custom_existing_bundle_reuses_selected_voice_without_fixed_validation(bootstrap, root, monkeypatch):
    base, bundle = root
    path = base / "runtime/studio.json"
    data = json.loads(path.read_text())
    data["tts"]["voice_id"] = "custom_oc_v2"
    path.write_text(json.dumps(data))
    (base / "runtime/tts/genie/custom_oc_v2").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(bundle, "verify_bundle", lambda r, v: calls.append((r, v)) or {"identity": "b" * 64})
    monkeypatch.setattr(
        bootstrap, "validate_configuration", lambda r: pytest.fail("Fixed Yaoyao only applies to generation")
    )
    monkeypatch.setattr(bootstrap, "ensure_no_workers", lambda: pytest.fail("Must not prepare on reuse"))
    monkeypatch.setattr(bootstrap, "run_phase", lambda *a: pytest.fail("Must not run a generation phase"))
    assert bootstrap.prepare(base) == {"reused": True, "bundle_identity": "b" * 64}
    assert calls == [(base, "custom_oc_v2")]
    assert json.loads(path.read_text())["tts"]["voice_id"] == "custom_oc_v2"
    assert not (base / "runtime/tts/sources").exists()
    assert not (base / "runtime/evaluations").exists()


def test_missing_custom_bundle_requires_manual_preparation_without_mutation(bootstrap, root, monkeypatch):
    base, bundle = root
    path = base / "runtime/studio.json"
    data = json.loads(path.read_text())
    data["tts"]["voice_id"] = "custom_oc_v2"
    path.write_text(json.dumps(data))
    before = {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    monkeypatch.setattr(bundle, "verify_bundle", lambda *a: pytest.fail("No bundle exists to verify"))
    monkeypatch.setattr(bootstrap, "validate_configuration", lambda r: pytest.fail("Must not select Yaoyao"))
    monkeypatch.setattr(bootstrap, "ensure_no_workers", lambda: pytest.fail("Must not prepare another voice"))
    with pytest.raises(ValueError, match="prepare it manually"):
        bootstrap.prepare(base)
    assert {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()} == before
    assert not (base / "runtime/tts/.bootstrap.lock").exists()
    assert not (base / "runtime/evaluations").exists()


@pytest.mark.parametrize("voice", ["../other", "/absolute", "custom/voice", ""])
def test_selected_voice_must_be_a_safe_id_before_bundle_lookup(bootstrap, root, monkeypatch, voice):
    base, bundle = root
    path = base / "runtime/studio.json"
    data = json.loads(path.read_text())
    data["tts"]["voice_id"] = voice
    path.write_text(json.dumps(data))
    monkeypatch.setattr(
        bundle, "verify_bundle", lambda *a: pytest.fail("Unsafe ID cannot reach bundle lookup")
    )
    with pytest.raises(ValueError, match="configured local Genie voice"):
        bootstrap.prepare(base)


def test_invalid_existing_bundle_is_preserved_without_rebuild(bootstrap, root, monkeypatch):
    base, bundle = root
    directory = base / "runtime/tts/genie" / bootstrap.VOICE
    directory.mkdir(parents=True)
    marker = directory / "manifest.json"
    marker.write_text("broken")

    def reject(*args):
        raise ValueError("invalid bundle")

    monkeypatch.setattr(bundle, "verify_bundle", reject)
    with pytest.raises(ValueError, match="invalid bundle"):
        bootstrap.prepare(base)
    assert marker.read_text() == "broken"


def fake_preparation(monkeypatch, bootstrap):
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None))
    monkeypatch.setitem(
        sys.modules,
        "evaluate_context",
        SimpleNamespace(
            MemoryWatch=lambda _: SimpleNamespace(sample=lambda: None, summary=lambda: {"guard": "checked"})
        ),
    )
    monkeypatch.setattr(bootstrap, "ensure_no_workers", lambda: None)


def test_failed_phase_stops_later_work_and_cleans_only_owned_scratch(bootstrap, root, monkeypatch):
    base, _ = root
    fake_preparation(monkeypatch, bootstrap)
    boundary = base / "runtime/evaluations"
    boundary.mkdir()
    unrelated = boundary / "other-job"
    unrelated.mkdir()
    marker = unrelated / "keep"
    marker.write_text("unrelated")
    calls = []

    def step(r, work, name, watch):
        calls.append(name)
        (work / "temporary").write_text("owned")
        if name == "quantize":
            raise RuntimeError("conversion failed")

    monkeypatch.setattr(bootstrap, "run_phase", step)
    with pytest.raises(RuntimeError, match="conversion failed"):
        bootstrap.prepare(base)
    assert calls == list(bootstrap.PHASES[:6])
    assert marker.read_text() == "unrelated"
    assert list(boundary.iterdir()) == [unrelated]


def test_configuration_drift_stops_before_next_phase(bootstrap, root, monkeypatch):
    base, _ = root
    fake_preparation(monkeypatch, bootstrap)
    calls = []

    def step(r, work, name, watch):
        calls.append(name)
        (base / "runtime/studio.json").write_text("Operator changed this")

    monkeypatch.setattr(bootstrap, "run_phase", step)
    with pytest.raises(ValueError, match="Operator configuration changed"):
        bootstrap.prepare(base)
    assert calls == ["nltk"]
    assert (base / "runtime/studio.json").read_text() == "Operator changed this"


def test_publish_preserves_existing_files_and_rejects_different_bytes(bootstrap, root):
    base, _ = root
    source, target = base / "new", base / "runtime/tts/weights/model.ckpt"
    source.write_bytes(b"verified")
    bootstrap.publish(base, source, target)
    bootstrap.publish(base, source, target)
    source.unlink()
    source.write_bytes(b"different")
    with pytest.raises(ValueError, match="preserved"):
        bootstrap.publish(base, source, target)
    assert target.read_bytes() == b"verified"


def test_inert_checkpoint_configuration_refuses_arbitrary_objects(bootstrap):
    shell = bootstrap.HParams()
    shell.model = {"version": "v2", "layers": [1, 2]}
    assert bootstrap.plain(shell) == {"model": {"version": "v2", "layers": [1, 2]}}
    with pytest.raises(ValueError, match="Unsupported object"):
        bootstrap.plain({"payload": object()})
    with pytest.raises(ValueError, match="keys"):
        bootstrap.plain({1: "invalid"})


@pytest.mark.parametrize("reason", [RuntimeError("memory guard"), KeyboardInterrupt(), None])
def test_aborted_phase_reaps_its_real_child(bootstrap, tmp_path, monkeypatch, reason):
    original_popen = subprocess.Popen
    children = []

    def popen(command, **options):
        child = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **options)
        children.append(child)
        return child

    monkeypatch.setattr(bootstrap.subprocess, "Popen", popen)
    count = 0

    def sample():
        nonlocal count
        count += 1
        if count == 2:
            if reason is None:
                bootstrap.interrupt(15, None)
            raise reason

    with pytest.raises((RuntimeError, KeyboardInterrupt), match=None if reason is not None else "signal 15"):
        bootstrap.run_phase(tmp_path, tmp_path, "sanitize", SimpleNamespace(sample=sample))
    assert len(children) == 1 and children[0].poll() is not None
