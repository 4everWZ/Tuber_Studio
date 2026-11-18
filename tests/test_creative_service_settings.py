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


def test_selecting_workstation_omni_moves_video_and_keeps_model_card_protocol(settings):
    settings.data["video_vision"] = {"enabled": True, "adapter": "omni", "model": "old",
                                     "base_url": "https://previous.example/v1", "model_revision": "old-pin"}
    target = {"kind": "openai_compatible", "model": "my-omni", "base_url": "https://studio.example/new/v1"}
    data, _ = proposal(settings, {"llm": target, "use_omni_video": True})
    assert data["llm"]["context_source"] == "model_card"
    assert data["llm"]["structured_output"] == "structural_tag"
    assert data["video_vision"]["base_url"] == target["base_url"]
    assert data["video_vision"]["model"] == target["model"]
    assert "model_revision" not in data["video_vision"]
