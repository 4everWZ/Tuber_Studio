"""Exercise browser account, operations, and request clients in Node."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app


def test_workbench_serves_page_fragments_in_the_shell(settings):
    with TestClient(create_app(settings)) as client:
        page = client.get("/").raise_for_status().text
    assert "<!-- page:" not in page
    for identity in (
        "workspace-sidebar",
        "op-round-settings",
        "matrix-max-per-day",
        "messages-panel",
        "queue-view",
    ):
        assert page.count(f'id="{identity}"') == 1


@pytest.mark.parametrize(
    "script",
    sorted(path.name for path in Path(__file__).parent.glob("*.test.cjs")),
)
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
