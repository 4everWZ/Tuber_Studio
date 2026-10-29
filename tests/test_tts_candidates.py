"""Inspection and installation help must remain usable without a CUDA import."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_tool(path, *arguments):
    return subprocess.run(
        [sys.executable, str(ROOT / path), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_candidate_inspection_is_metadata_only_without_model_loading():
    result = run_tool("tools/evaluate_tts_candidates.py", "--action", "inspect", "--root", str(ROOT))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["action"] == "inspect"
    assert report["torch_imported"] is False
    assert report["cuda_probe_performed"] is False
    assert report["dependency_policy"]["installation_performed"] is False


def test_execution_requires_an_explicit_experiment_handoff_before_runtime_imports():
    result = run_tool("tools/evaluate_tts_candidates.py", "--action", "run", "--load-order", "isolated")
    assert result.returncode == 2
    assert "pass --execute" in result.stderr
    assert "Traceback" not in result.stderr


def test_offline_installer_help_does_not_import_the_target_runtime():
    result = run_tool("tools/jetson/tts/install-candidates.py", "--help")
    assert result.returncode == 0, result.stderr
    assert "--bundle" in result.stdout
