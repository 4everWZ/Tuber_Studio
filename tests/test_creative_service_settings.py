import pytest

from tuber_studio.config import Settings
from tuber_studio.provider_settings import proposal


def test_background_limits_share_gateway_preserve_unspecified_options(tmp_path):
    settings = Settings(runtime=tmp_path)
    settings.provider_keys = {"omni": "test-key"}
    data, keys = proposal(settings, {"omni": {"responses_options": {"parallel_reads": 3}, "enable_thinking": False}})
    assert keys == {"omni": "test-key"}
    assert data["omni"]["responses_options"] == settings.data["omni"]["responses_options"] | {"parallel_reads": 3}
    assert not data["omni"]["enable_thinking"]
    for patch in ({"responses_options": {"base_url": "https://elsewhere.example/v1"}},
                  {"responses_options": {"parallel_reads": 0}}, {"kind": "demo"}):
        with pytest.raises(ValueError):
            proposal(settings, {"omni": patch})
