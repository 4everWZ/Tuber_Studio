"""Protect operator-owned voice configuration during repeated initialization."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/jetson/tts"))
from prepare import ensure_configuration  # noqa: E402

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
        assert not ensure_configuration(template, target, tmp_path / "data")
        assert target.read_bytes() == original
        assert target.stat().st_mtime_ns == modified
    finally:
        target.chmod(0o600)


def test_missing_configuration_uses_template_and_selected_data_root(tmp_path):
    template = tmp_path / "template.yaml"
    template.write_text("weights: /workspace/tuber/runtime/tts/voice.pth\n", encoding="utf-8")
    target = tmp_path / "data/runtime/tts/tts_infer.yaml"
    root = tmp_path / "data"
    assert ensure_configuration(template, target, root)
    assert target.read_text(encoding="utf-8") == f"weights: {root}/runtime/tts/voice.pth\n"


def test_check_only_creates_neither_configuration_nor_parent_directory(tmp_path):
    target = tmp_path / "absent/runtime/tts/tts_infer.yaml"
    assert not ensure_configuration(
        tmp_path / "also-absent-template.yaml", target, tmp_path / "absent", check_only=True
    )
    assert not target.parent.exists()
