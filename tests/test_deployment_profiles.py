import json
from pathlib import Path

import pytest

from tuber_studio.deployment_profiles import business_configuration, mode_configuration
from tuber_studio.http_api import endpoint
from tuber_studio.inference import ProcessRunner
from tuber_studio.inference_profiles import DAFEIYU_9B_DIGESTS, DAFEIYU_9B_KEY, DAFEIYU_9B_MODEL


def test_business_modes_preserve_operator_data_and_separate_thinking_models(settings):
    original = settings.data | {"llm": settings.data["llm"] | {"max_tokens": 160}}
    original["operator_extension"] = {"keep": "unchanged"}
    data = business_configuration(original, "http://100.64.0.42:9012",
                                  operations_model="Gemma4-E2B", live_model="MiniCPM5-2B")
    settings._validate(data)
    assert data["llm"]["model"] == "Gemma4-E2B"
    assert data["llm"]["context_tokens"] == 131072
    assert data["llm"]["enable_thinking"] is True
    assert "reasoning_budget_tokens" not in data["llm"]
    assert data["llm"]["operations_max_tokens"] == 8192
    assert data["video_vision"]["image_profile"] == "gemma4_image_280"
    live = mode_configuration(data, "live")
    assert live["llm"]["model"] == "MiniCPM5-2B" and live["vision"]["kind"] == "disabled"
    assert live["llm"]["max_tokens"] == 1024
    assert live["tts"]["local_backend"] == "genie_onnx_int8"
    assert live["operator_extension"] == original["operator_extension"]
    assert original["llm"]["kind"] == "demo"


def test_new_profiles_share_the_pinned_fine_tuned_9b(settings):
    data = business_configuration(settings.data, "http://127.0.0.1:9012")
    settings._validate(data)
    live = mode_configuration(data, "live")
    assert data["llm"]["model"] == live["llm"]["model"] == DAFEIYU_9B_MODEL
    assert data["llm"]["context_tokens"] == live["llm"]["context_tokens"] == 262144
    assert data["video_vision"]["model"] == DAFEIYU_9B_MODEL


def test_default_cuda_launch_uses_pinned_model_and_projector(tmp_path):
    manifest = json.loads((Path(__file__).resolve().parents[1] / "config/jetson/downloads.json").read_text())
    entries = {entry["path"]: entry for entry in manifest["files"]}
    runner = ProcessRunner(tmp_path, context_tokens=262144, kv_cache_type="q4_0")
    command = runner.command(DAFEIYU_9B_KEY)
    for role, option in (("model", "-m"), ("projector", "--mmproj")):
        asset = Path(command[command.index(option) + 1]).relative_to(tmp_path / "runtime").as_posix()
        entry = entries[asset]
        assert entry["sha256"] == DAFEIYU_9B_DIGESTS[role]
        assert "/resolve/21ecede9e9a3f07764c8c0ee3bc24d2de7cf5aff/" in entry["urls"][0]
    assert command[command.index("--ctx-size") + 1] == "262144"
    assert command[command.index("--cache-type-k") + 1] == command[command.index("--cache-type-v") + 1] == "q4_0"


@pytest.mark.parametrize("address", ["http://100.128.0.1:9012", "http://1.1.1.1:9012"])
def test_shared_space_exception_does_not_enable_plain_http_public_endpoints(address):
    with pytest.raises(ValueError, match="HTTPS"):
        endpoint(address)
