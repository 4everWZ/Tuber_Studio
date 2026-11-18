import pytest

from tuber_studio.provider_settings import proposal


def test_background_endpoint_and_key_are_not_shared_with_front_model(settings):
    settings.provider_keys = {"llm": "front-secret", "responses": "old-worker"}
    settings.data["responses"] = {"base_url": "https://old.example/llm/v1", "model": "worker"}
    target = {"responses": {"base_url": "https://new.example/llm/v1", "model": "new-worker"}}
    with pytest.raises(ValueError, match="Key"):
        proposal(settings, target)
    data, keys = proposal(settings, target | {"credentials": {"responses": "new-secret"}})
    assert data["responses"] == target["responses"]
    assert keys == {"llm": "front-secret", "responses": "new-secret"}
    assert not any(secret in str(data) for secret in keys.values())
