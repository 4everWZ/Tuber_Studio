from pathlib import Path

from tuber_studio.deployment_profiles import business_configuration

NODE_URL = "http://jetson.local:9012"


def test_business_voice_reference_resolves_with_independent_runtime(settings):
    settings.data = business_configuration(
        settings.data, NODE_URL, operations_model="Gemma4-E2B", live_model="MiniCPM5-2B",
    )
    voice = next(voice for voice in settings.voices() if voice["id"] == "yaoyao_sovits_v2")
    reference = settings.runtime / Path(voice["reference_asset"]).relative_to("runtime")
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    assert settings.voice_reference_path(voice) == reference.resolve()
