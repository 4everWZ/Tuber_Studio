"""Permanent bundle identity/publication contracts, without model inference."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def bundle():
    path = Path(__file__).resolve().parents[1] / "tools/jetson/tts/genie_bundle.py"
    spec = importlib.util.spec_from_file_location("test_genie_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def permanent(bundle, tmp_path, monkeypatch):
    root = tmp_path
    voice = "test_voice"
    directory = root / "runtime/tts/genie" / voice
    directory.mkdir(parents=True)
    required = (set(bundle.PATHS.values()) - {"bert"}) | {
        "bert/receipt.json",
        "bert/bert_int8.onnx.data",
        "vits/vits_fp32.bin",
        *("ar/" + name + ".data" for name in bundle.AR_NAMES),
    }
    for name in required:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    implementations = {}
    for name in bundle.IMPLEMENTATIONS:
        path = root / "tools/jetson/tts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixed-implementation:" + name)
        implementations[name] = bundle.file_record(path)
    for relative in (
        "config/voices.json",
        "runtime/tts/tts_infer.yaml",
        "runtime/tts/pretrained/bert/config.json",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original-source:" + relative)
    reference = root / "runtime/tts/reference_features" / voice
    reference.mkdir(parents=True)
    for name in ("metadata.json", "features.npz"):
        (reference / name).write_bytes(name.encode())
    identity = {
        "voice_id": voice,
        "paths": {"bert/config.json": str(root / "runtime/tts/pretrained/bert/config.json")},
        "source_sha256": {},
    }
    arrays = {"checked": object()}
    metadata = {"identity": identity}
    monkeypatch.setattr(
        bundle, "_reference", lambda r, v: (None, reference, identity, metadata, arrays, {"fixed": True})
    )
    manifest = {
        "format": bundle.FORMAT,
        "kind": bundle.KIND,
        "voice_id": voice,
        "genie_revision": bundle.REVISION,
        "paths": bundle.PATHS,
        "source_identity": identity,
        "implementations": implementations,
        "reference": {
            "metadata": str((reference / "metadata.json").relative_to(root)),
            "features": str((reference / "features.npz").relative_to(root)),
            "metadata_identity": bundle.file_record(reference / "metadata.json"),
            "features_identity": bundle.file_record(reference / "features.npz"),
        },
        "files": {name: bundle.file_record(directory / name) for name in required},
    }
    manifest["bundle_identity"] = bundle._manifest_identity(manifest)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return root, voice, directory, arrays


def test_missing_bundle_requires_explicit_preparation(bundle, tmp_path):
    with pytest.raises(ValueError, match="explicitly"):
        bundle.verify_bundle(tmp_path, "missing_voice")


@pytest.mark.parametrize("voice", ["../other", "voice/name", "", "/absolute"])
def test_voice_cannot_escape_permanent_root(bundle, tmp_path, voice):
    with pytest.raises(ValueError, match="voice"):
        bundle._voice_directory(tmp_path, voice)


def test_existing_directory_is_not_replaced(bundle, tmp_path):
    stage, final = tmp_path / "stage", tmp_path / "existing"
    stage.mkdir()
    (stage / "new").write_text("new")
    final.mkdir()
    with pytest.raises(OSError):
        bundle._publish(stage, final)
    assert (stage / "new").read_text() == "new"
    assert list(final.iterdir()) == []


def test_new_directory_is_published_atomically(bundle, tmp_path):
    stage, final = tmp_path / "stage", tmp_path / "current"
    stage.mkdir()
    (stage / "manifest.json").write_text("complete")
    bundle._publish(stage, final)
    assert not stage.exists()
    assert (final / "manifest.json").read_text() == "complete"


def test_permanent_bundle_needs_no_evaluation_paths(bundle, permanent):
    root, voice, directory, arrays = permanent
    result = bundle.verify_bundle(root, voice)
    assert result["directory"] == directory
    assert result["reference_arrays"] is arrays
    assert result["identity"] == result["bundle_identity"]
    assert len(result["identity"]) == 64
    assert result["fixed_signature"] == {"fixed": True}
    assert not (root / "runtime/evaluations").exists()
    assert all("evaluations" not in str(path) for path in result["paths"].values())


def test_changed_external_weights_fail_startup_hash(bundle, permanent):
    root, voice, directory, _ = permanent
    path = directory / "ar" / (bundle.AR_NAMES[0] + ".data")
    path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(ValueError, match="identity mismatch"):
        bundle.verify_bundle(root, voice)


def test_warm_guard_uses_stats_and_rejects_source_change(bundle, permanent, monkeypatch):
    root, voice, _, _ = permanent
    result = bundle.verify_bundle(root, voice)
    monkeypatch.setattr(bundle, "digest", lambda path: pytest.fail("Warm guard must not hash files"))
    bundle.verify_unchanged(result["stat_signature"])
    (root / "config/voices.json").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        bundle.verify_unchanged(result["stat_signature"])


def test_changed_implementation_fails_startup(bundle, permanent):
    root, voice, _, _ = permanent
    (root / "tools/jetson/tts/genie_backend.py").write_text("other implementation")
    with pytest.raises(ValueError, match="identity mismatch"):
        bundle.verify_bundle(root, voice)


def test_manifest_cannot_redirect_reference_cache(bundle, permanent):
    root, voice, directory, _ = permanent
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["reference"]["features"] = "runtime/evaluations/other.npz"
    manifest["bundle_identity"] = bundle._manifest_identity(manifest)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="controlled compact cache"):
        bundle.verify_bundle(root, voice)
