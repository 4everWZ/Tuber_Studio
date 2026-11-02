import importlib.util
import io
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "inference_node_cli", Path(__file__).resolve().parents[1] / "tools/jetson/inference-node.py"
)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.mark.parametrize("state", ["active", "cold", "switching"])
def test_node_healthcheck_is_read_only_authenticated_and_does_not_require_business_state(
    tmp_path, monkeypatch, state
):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/inference-token").write_text("node-test-key")
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            return io.BytesIO(json.dumps({"mode": "text", "state": state, "providers": {}}).encode())

    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *args: Opener())
    cli.healthcheck(tmp_path, 9012)
    request, timeout = calls[0]
    assert len(calls) == 1 and request.get_method() == "GET"
    assert request.full_url == "http://127.0.0.1:9012/health" and timeout == 5
    assert request.get_header("X-tuber-inference-key") == "node-test-key"
    assert not (tmp_path / "runtime/studio.sqlite3").exists()


def test_node_healthcheck_reports_model_error(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/inference-token").write_text("node-test-key")

    class Opener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(b'{"mode":"text","state":"error","providers":{}}')

    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError):
        cli.healthcheck(tmp_path, 9012)
