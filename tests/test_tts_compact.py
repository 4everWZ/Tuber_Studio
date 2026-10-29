"""CPU-only cache contract checks; real tensor/audio parity is a device evaluation."""

import importlib.util
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def compact():
    path = Path(__file__).resolve().parents[1] / "tools/jetson/tts/compact.py"
    spec = importlib.util.spec_from_file_location("compact_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Array:
    def __init__(self, dtype, shape):
        self.dtype, self.shape = dtype, shape

    def copy(self):
        return self

    def __lt__(self, other):
        return types.SimpleNamespace(any=lambda: False)


@pytest.fixture
def bundle(compact, tmp_path, monkeypatch):
    arrays = {
        "prompt_semantic": Array("int64", (180,)),
        "reference_phones": Array("int64", (64,)),
        "reference_bert": Array("float16", (1024, 64)),
        "reference_spec": Array("float16", (1, 1025, 200)),
    }
    archive = tmp_path / "features.npz"
    with zipfile.ZipFile(archive, "w") as output:
        for name in arrays:
            output.writestr(name + ".npy", b"header fixture; tensors mocked without installing numpy")
    identity = {"precision": "float16", "reference_text": "a fixed source", "sha256": {"weights": "expected"}}
    metadata = {
        "format": compact.FORMAT,
        "identity": identity,
        "arrays": compact._array_specs(arrays, "float16"),
        "norm_text": "fixed",
        "npz_sha256": compact._digest(archive),
    }
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    calls = []

    class Loaded:
        files = list(arrays)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __getitem__(self, name):
            return arrays[name]

    def load(path, *, allow_pickle):
        assert allow_pickle is False
        calls.append(path)
        return Loaded()

    numpy = types.SimpleNamespace(load=load, isfinite=lambda a: types.SimpleNamespace(all=lambda: True))
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    return tmp_path, identity, metadata, arrays, calls, numpy


def test_bundle_loads_only_fixed_named_arrays_with_pickle_disabled(compact, bundle):
    directory, identity, metadata, arrays, calls, _ = bundle
    actual, loaded = compact._read_bundle(directory, identity)
    assert actual == metadata and loaded == arrays
    assert calls == [directory / "features.npz"]


@pytest.mark.parametrize("change", ["reference_text", "sha256", "precision"])
def test_changed_source_identity_fails_before_tensor_loading(compact, bundle, change):
    directory, identity, _, _, calls, _ = bundle
    altered = {**identity, change: "changed"}
    with pytest.raises(ValueError, match="identity changed"):
        compact._read_bundle(directory, altered)
    assert calls == []


def test_archive_bytes_are_bound_to_metadata(compact, bundle):
    directory, identity, _, _, calls, _ = bundle
    with (directory / "features.npz").open("ab") as output:
        output.write(b"tampered")
    with pytest.raises(ValueError, match="identity mismatch"):
        compact._read_bundle(directory, identity)
    assert calls == []


@pytest.mark.parametrize(
    "name,dtype,shape",
    [
        ("reference_bert", "object", (1024, 64)),
        ("reference_bert", "float32", (1024, 64)),
        ("reference_bert", "float16", (1024, 65)),
        ("reference_spec", "float16", (1, 1024, 200)),
        ("prompt_semantic", "int64", (1, 180)),
    ],
)
def test_malformed_tensor_types_and_shapes_are_rejected(compact, bundle, name, dtype, shape):
    directory, identity, _, arrays, _, _ = bundle
    arrays[name] = Array(dtype, shape)
    with pytest.raises(ValueError):
        compact._read_bundle(directory, identity)


def test_nonfinite_cache_values_are_rejected(compact, bundle):
    directory, identity, _, _, _, numpy = bundle
    numpy.isfinite = lambda a: types.SimpleNamespace(all=lambda: False)
    with pytest.raises(ValueError, match="finite"):
        compact._read_bundle(directory, identity)


def test_archive_cannot_smuggle_other_files(compact, bundle):
    directory, identity, metadata, _, calls, _ = bundle
    path = directory / "features.npz"
    with zipfile.ZipFile(path, "a") as output:
        output.writestr("../../executable.pkl", b"not accepted")
    metadata["npz_sha256"] = compact._digest(path)
    (directory / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="fixed format"):
        compact._read_bundle(directory, identity)
    assert calls == []


def test_file_inputs_cannot_escape_or_follow_links(compact, tmp_path):
    reference = tmp_path / "assets/oc/reference.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"wave fixture")
    assert compact._file(tmp_path, reference, ("assets",)) == reference
    with pytest.raises(ValueError):
        compact._file(tmp_path, "assets/oc/../oc/reference.wav", ("assets",))
    with pytest.raises(ValueError):
        compact._file(tmp_path, Path("/etc/passwd"), ("assets",))
    alias = reference.with_name("alias.wav")
    try:
        alias.symlink_to(reference)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError, match="links"):
        compact._file(tmp_path, alias, ("assets",))


def test_prompt_normalization_uses_original_source_punctuation(compact, tmp_path, monkeypatch):
    path = tmp_path / "GPT_SoVITS/TTS_infer_pack/text_segmentation_method.py"
    path.parent.mkdir(parents=True)
    path.write_text("splits = {'。', '.', '!', '?'}\n")
    monkeypatch.setattr(compact, "_source", lambda: tmp_path)
    assert compact._normal_prompt("\n你好\n", "zh") == "你好。"
    assert compact._normal_prompt("hello", "en") == "hello."
    assert compact._normal_prompt("你好!", "zh") == "你好!"


def test_private_config_copy_preserves_production_yaml_bytes(compact, tmp_path):
    source = tmp_path / "production.yaml"
    source.write_bytes(b"custom:\n  is_half: true\n")
    directory = tmp_path / "evaluation"
    directory.mkdir()
    private = Path(compact._private_config(directory, source))
    private.write_text("changed by original upstream save_configs")
    assert source.read_bytes() == b"custom:\n  is_half: true\n"


def test_compute_reference_uses_existing_pipeline_without_loading_another(compact, tmp_path, monkeypatch):
    pipeline, identity, calls = object(), {"fixed": "identity"}, []
    monkeypatch.setattr(compact, "_settings", lambda root, voice: (tmp_path, tmp_path, None, identity))
    monkeypatch.setattr(compact, "_compute_reference", lambda p, i: calls.append(("compute", p, i)))
    monkeypatch.setattr(
        compact, "_save_reference", lambda p, d, i: calls.append(("save", p, d, i)) or {"saved": True}
    )
    monkeypatch.setattr(compact, "_upstream", lambda root: pytest.fail("Must not import/load models"))
    assert compact.compute_reference(tmp_path, pipeline) == {"saved": True}
    assert calls == [("compute", pipeline, identity), ("save", pipeline, tmp_path, identity)]


def test_stale_reference_is_prepared_once_in_a_short_lived_shared_group_child(compact, tmp_path, monkeypatch):
    results, calls = [ValueError("old cache"), ({"valid": True}, {})], []
    monkeypatch.setattr(compact, "_settings", lambda root, voice: (tmp_path, tmp_path, None, {}))

    def read(*args):
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(compact, "_read_bundle", read)
    monkeypatch.setattr(compact.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    assert compact.ensure_reference(tmp_path) == {"valid": True}
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[-3:] == ["--prepare-reference", "--voice-id", "whale_sovits_v2"]
    assert options["check"] and options["start_new_session"] is False
    assert options["env"]["TUBER_ROOT"] == str(tmp_path)
    assert "shell" not in options


def test_valid_cache_does_not_start_a_reference_worker(compact, tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "_settings", lambda root, voice: (tmp_path, tmp_path, None, {}))
    monkeypatch.setattr(compact, "_read_bundle", lambda *args: ({"valid": True}, {}))
    monkeypatch.setattr(
        compact.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected full-model worker")
    )
    assert compact.ensure_reference(tmp_path) == {"valid": True}
    assert compact.validate_reference(tmp_path) == {"valid": True}


def test_invalid_worker_result_is_not_retried_or_used_as_resident_fallback(compact, tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "_settings", lambda root, voice: (tmp_path, tmp_path, None, {}))
    monkeypatch.setattr(
        compact, "_read_bundle", lambda *args: (_ for _ in ()).throw(ValueError("still invalid"))
    )
    calls = []
    monkeypatch.setattr(compact.subprocess, "run", lambda *a, **k: calls.append("prepare"))
    with pytest.raises(ValueError, match="still invalid"):
        compact.ensure_reference(tmp_path)
    assert calls == ["prepare"]


def test_live_signature_ignores_mode_but_catches_voice_reference_and_weight_changes(
    compact, tmp_path, monkeypatch
):
    (tmp_path / "config").mkdir()
    (tmp_path / "runtime/tts/weights").mkdir(parents=True)
    (tmp_path / "config/voices.json").write_text(
        json.dumps([{"id": "oc", "provider": "gpt_sovits", "reference_text": "hello"}])
    )
    config = tmp_path / "runtime/studio.json"
    config.write_text(json.dumps({"tts": {"voice_id": "oc"}, "inference_broker": {"mode": "text"}}))
    (tmp_path / "runtime/tts/tts_infer.yaml").write_text("fixed settings")
    reference = tmp_path / "runtime/tts/ref.wav"
    reference.write_bytes(b"fixed audio")
    weight = tmp_path / "runtime/tts/weights/model.bin"
    weight.write_bytes(b"fixed weights")
    metadata = {"identity": {"paths": {"reference": str(reference), "weights": str(weight)}}}
    digest, hashed = compact._digest, []
    monkeypatch.setattr(compact, "_digest", lambda path: hashed.append(path) or digest(path))
    before = compact.reference_signature(tmp_path, voice_id="oc", metadata=metadata)
    assert weight not in hashed
    config.write_text(json.dumps({"tts": {"voice_id": "oc"}, "inference_broker": {"mode": "live"}}))
    assert compact.reference_signature(tmp_path, voice_id="oc", metadata=metadata) == before
    reference.write_bytes(b"other audio")
    assert compact.reference_signature(tmp_path, voice_id="oc", metadata=metadata) != before
    reference.write_bytes(b"fixed audio")
    current = compact.reference_signature(tmp_path, voice_id="oc", metadata=metadata)
    replacement = weight.with_suffix(".replacement")
    replacement.write_bytes(weight.read_bytes())
    replacement.replace(weight)
    assert compact.reference_signature(tmp_path, voice_id="oc", metadata=metadata) != current
