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


@pytest.mark.parametrize(
    "source,expected_code,warning",
    [
        ("answer = 42\n", 0, False),
        ("answer=42\n", 0, True),
        ("import os\n", 1, False),
        ("import os\n\nanswer=42\n", 1, True),
        ("def broken(\n", 1, False),
    ],
)
def test_advisory_formatting_preserves_lint_gate(tmp_path, source, expected_code, warning):
    path = tmp_path / "new_file.py"
    path.write_text(source, encoding="utf-8")
    # The workflow has normal fail-fast semantics, with no continue-on-error.
    # Run a following command only after this real entry point succeeds.
    marker = tmp_path / "next-step.txt"
    driver = (
        "import pathlib, subprocess, sys; "
        "r = subprocess.run([sys.executable, sys.argv[1], '--format-advisory', sys.argv[2]]); "
        "r.returncode == 0 and pathlib.Path(sys.argv[3]).write_text('ran'); "
        "sys.exit(r.returncode)"
    )
    result = subprocess.run(
        [sys.executable, "-c", driver, str(SCRIPT), str(path), str(marker)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected_code, result.stdout + result.stderr
    assert marker.exists() == (expected_code == 0)
    assert ("::warning title=Python formatting::" in result.stdout) == warning
    assert path.read_text(encoding="utf-8") == source


def test_advisory_formatting_does_not_swallow_formatter_tool_errors(monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("style_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    results = iter([0, 2])
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=next(results)))
    assert module.main(["--format-advisory"]) == 1


def test_ci_keeps_tests_blocking_without_running_style_checks():
    workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "check_style.py" not in workflow
    assert "continue-on-error" not in workflow
    assert "pytest -q --junitxml=tmp/ci-results/pytest.xml" in workflow
