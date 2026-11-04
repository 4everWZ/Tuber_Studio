"""Exercise the actual local/CI style command with valid and invalid source."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/ci/check_style.py"


@pytest.mark.parametrize(
    "source,lint,formatting",
    [
        ("answer = 42\n", "passed", "passed"),
        ("import os\n", "failed", "passed"),
        ("answer=42\n", "passed", "failed"),
        ("import os\n\nanswer=42\n", "failed", "failed"),
    ],
)
def test_style_command_reports_both_gates_without_rewriting_source(tmp_path, source, lint, formatting):
    path = tmp_path / "example.py"
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), path.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == int("failed" in (lint, formatting)), result.stdout + result.stderr
    assert f"Ruff lint: {lint}" in result.stdout
    assert f"Ruff format: {formatting}" in result.stdout
    assert path.read_text(encoding="utf-8") == source
