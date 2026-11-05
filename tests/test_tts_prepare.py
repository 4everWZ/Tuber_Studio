"""Protect operator-owned voice configuration during repeated initialization."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/jetson/tts"))
import prepare_compact  # noqa: E402

sys.path.pop(0)


def test_existing_read_only_operator_configuration_is_preserved(tmp_path):
    template = tmp_path / "template.yaml"
    template.write_text("custom: default-voice\n", encoding="utf-8")
    target = tmp_path / "tts_infer.yaml"
    original = b"custom:\n  t2s_weights_path: /private/new-oc/model.ckpt\n"
    target.write_bytes(original)
    modified = target.stat().st_mtime_ns
    target.chmod(0o444)
    try:
        assert not prepare_compact.ensure_configuration(template, target, tmp_path / "data")
        assert target.read_bytes() == original
        assert target.stat().st_mtime_ns == modified
    finally:
        target.chmod(0o600)


def test_missing_configuration_uses_template_and_selected_data_root(tmp_path):
    template = tmp_path / "template.yaml"
    template.write_text("weights: /workspace/tuber/runtime/tts/voice.pth\n", encoding="utf-8")
    target = tmp_path / "data/runtime/tts/tts_infer.yaml"
    root = tmp_path / "data"
    assert prepare_compact.ensure_configuration(template, target, root)
    assert target.read_text(encoding="utf-8") == f"weights: {root}/runtime/tts/voice.pth\n"


def test_check_only_creates_neither_configuration_nor_parent_directory(tmp_path):
    target = tmp_path / "absent/runtime/tts/tts_infer.yaml"
    assert not prepare_compact.ensure_configuration(
        tmp_path / "also-absent-template.yaml", target, tmp_path / "absent", check_only=True
    )
    assert not target.parent.exists()


@pytest.mark.parametrize("option", ["--offline", "--check-only"])
def test_initializer_compact_voice_mode_reuses_local_assets(tmp_path, monkeypatch, option):
    spec = importlib.util.spec_from_file_location(
        "compact_initializer", ROOT / "tools/jetson/initialize-data.py"
    )
    initializer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(initializer)
    runtime = tmp_path / "runtime/tts"
    assets = {
        runtime / "GPT-SoVITS-v2/api_v2.py": b"source",
        runtime / "pretrained/hubert/config.json": b"encoder",
        runtime / "weights/da_fei_yu-e12.ckpt": b"weights",
    }
    for path, content in assets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setenv("TUBER_ROOT", str(tmp_path))
    monkeypatch.delenv("TUBER_GPT_SOVITS_ROOT", raising=False)
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(prepare_compact, "HF_FILES", {"hubert/config.json": 7})
    monkeypatch.setattr(prepare_compact, "VOICE_FILES", {"weights/da_fei_yu-e12.ckpt": 7})
    monkeypatch.setattr(prepare_compact, "verify_dependencies", lambda path: None)
    monkeypatch.setattr(prepare_compact, "check_source", lambda path: None)
    monkeypatch.setattr(
        prepare_compact.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("Offline fetch")
    )

    initializer.main(["--compact-voice-only", option])

    config = runtime / "tts_infer.yaml"
    if option == "--offline":
        expected = (ROOT / "config/jetson/tts-compact-dafeiyu.yaml").read_bytes()
        assert config.read_bytes() == expected.replace(b"/workspace/tuber", str(tmp_path).encode())
    else:
        assert not config.exists()
    assert {path: path.read_bytes() for path in assets} == assets
    assert sorted(path.name for path in runtime.parent.iterdir()) == ["tts"]
