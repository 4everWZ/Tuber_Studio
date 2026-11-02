"""Exercise actual shared account rendering through Node's isolated VM."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_shared_account_ui_keeps_last_profile_on_transport_failure():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("account_ui.test.cjs"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
