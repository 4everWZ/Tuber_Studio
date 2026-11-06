"""Exercise browser account, operations, and request clients in Node."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", ["account_ui.test.cjs", "web_api.test.cjs", "web_operations.test.cjs"])
def test_shared_web_clients(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name(script))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
