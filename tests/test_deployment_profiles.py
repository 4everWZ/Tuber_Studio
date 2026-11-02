import pytest

from tuber_studio.deployment_profiles import business_configuration, mode_configuration
from tuber_studio.http_api import endpoint


def test_business_modes_preserve_operator_data_and_separate_thinking_models(settings):
    original = settings.data.copy()
    original["operator_extension"] = {"keep": "unchanged"}
    data = business_configuration(original, "http://100.95.31.18:9012")
    settings._validate(data)
    assert data["llm"]["model"] == "Gemma4-E2B"
    assert data["llm"]["context_tokens"] == 131072
    assert data["llm"]["enable_thinking"] is True
    assert "reasoning_budget_tokens" not in data["llm"]
    assert data["llm"]["operations_max_tokens"] == 8192
    assert data["video_vision"]["image_profile"] == "gemma4_image_280"
    live = mode_configuration(data, "live")
    assert live["llm"]["model"] == "MiniCPM5-2B" and live["vision"]["kind"] == "disabled"
    assert live["tts"]["local_backend"] == "genie_onnx_int8"
    assert live["operator_extension"] == original["operator_extension"]
    assert original["llm"]["kind"] == "demo"


@pytest.mark.parametrize("address", ["http://100.128.0.1:9012", "http://1.1.1.1:9012"])
def test_shared_space_exception_does_not_enable_plain_http_public_endpoints(address):
    with pytest.raises(ValueError, match="HTTPS"):
        endpoint(address)
