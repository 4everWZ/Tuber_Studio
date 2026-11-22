import json

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.core.authority import BusinessAuthorityError, acquire_business_writer
from tuber_studio.core.config import RETIRED_SECTIONS, Settings


def test_first_authoritative_start_rewrites_the_old_connection(tmp_path):
    path = tmp_path / "studio.json"
    old = {"tts": {"voice_id": "yaoyao_sovits_v2"}}
    path.write_text(json.dumps(old))
    settings = Settings(runtime=tmp_path)
    with acquire_business_writer(tmp_path), pytest.raises(BusinessAuthorityError):
        create_app(settings)
    assert json.loads(path.read_text()) == old
    with TestClient(create_app(settings)):
        assert json.loads(path.read_text()) == settings.data
        assert settings.data["voice_id"] == "yaoyao_reference"
        assert not settings.data.keys() & RETIRED_SECTIONS


def test_new_configuration_has_one_connection_and_two_protocols(tmp_path):
    settings = Settings(runtime=tmp_path)
    omni = settings.data["omni"]
    assert not settings.data.keys() & RETIRED_SECTIONS
    assert settings.responses_config["base_url"] == omni["base_url"].removesuffix("/v1") + "/llm/v1"
    assert settings.video_config["base_url"] == settings.responses_config["base_url"]
    assert settings.video_config["model"] == settings.responses_config["model"]
    assert settings.video_config["enable_thinking"]
    assert settings.data["voice_id"] == "whale_reference"
    assert not (tmp_path / "inference-token").exists()


def test_old_remote_voice_name_cannot_be_interpreted_as_a_reference_profile(tmp_path):
    config = tmp_path / "studio.json"
    config.write_text(json.dumps({"tts": {"kind": "openai_compatible", "voice": "yaoyao_sovits_v2"}}))
    settings = Settings(runtime=tmp_path)
    assert settings.data["voice_id"] == "external_api"


@pytest.mark.parametrize("old_voice,current", [
    ("whale_sovits_v2", "whale_reference"), ("yaoyao_sovits_v2", "yaoyao_reference"),
    ("whale_sample_v1", "whale_sample_v1"), ("external_api", "external_api"),
])
def test_existing_gateway_profile_migrates_in_memory_and_persists_on_save(tmp_path, old_voice, current):
    root = "https://studio.example/proxy"
    settings = Settings(runtime=tmp_path)
    legacy = {k: v for k, v in settings.data.items() if k not in {"omni", "voice_id"}}
    legacy.update(llm={"base_url": root + "/v1", "kind": "openai_compatible", "model": "my-omni",
                       "enable_thinking": False, "operations_max_tokens": 3011},
                  tts={"kind": "gpt_sovits", "base_url": "https://retired.example", "voice_id": old_voice},
                  video_vision={"adapter": "omni", "model": "my-omni", "base_url": root + "/v1", "model_revision": "pin"},
                  responses={"base_url": root + "/llm/v1", "model": "worker", "parallel_reads": 3},
                  inference_broker={"enabled": True}, work_mode_profiles={"live": {}}, operator_extension="original")
    settings.path.write_text(json.dumps(legacy), encoding="utf-8")
    settings.credentials_path.write_text(json.dumps({
        "llm": {"base_url": root + "/v1", "key": "front-key"},
        "tts": {"base_url": "https://retired.example", "key": "old-key"},
    }), encoding="utf-8")
    loaded = Settings(runtime=tmp_path)
    assert loaded.data["voice_id"] == loaded.voice(old_voice)["id"] == current
    assert loaded.data["operator_extension"] == "original"
    assert loaded.data["omni"]["operations_max_tokens"] == 3011
    assert loaded.video_config["base_url"] == root + "/llm/v1"
    assert loaded.video_config["model"] == "worker"
    # The old pin described the foreground checkpoint, not the background model.
    assert loaded.video_config["model_revision"] == "" and loaded.video_config["enable_thinking"]
    assert not loaded.data["omni"]["enable_thinking"]
    assert (loaded.responses_config["model"], loaded.responses_config["parallel_reads"]) == ("worker", 3)
    assert loaded.provider_keys == {"omni": "front-key"}
    assert "old-key" not in str(loaded.public_data())
    assert json.loads(loaded.path.read_text()) == legacy
    # An unrelated public settings save must migrate the scoped key as well.
    loaded.save_public(loaded.data)
    again = Settings(runtime=tmp_path)
    assert again.data == loaded.data and again.provider_key("omni") == "front-key"
    assert not again.data.keys() & RETIRED_SECTIONS
    assert set(json.loads(again.credentials_path.read_text())) == {"omni"}


def test_video_checkpoint_pin_is_specific_to_background_model(tmp_path):
    settings = Settings(runtime=tmp_path)
    current = settings.data["omni"] | {
        "video_options": {"model_revision": "old-front-pin", "background_model_revision": "background-pin"},
    }
    settings.path.write_text(json.dumps(settings.data | {"omni": current}), encoding="utf-8")
    loaded = Settings(runtime=tmp_path)
    assert loaded.video_config["model_revision"] == "background-pin"
    assert "model_revision" not in loaded.data["omni"]["video_options"]
