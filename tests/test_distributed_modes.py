from pathlib import Path

import pytest

from tuber_studio.config import Settings


def test_reference_voice_resolves_with_independent_runtime(tmp_path):
    settings = Settings(runtime=tmp_path)
    voice = settings.voice("yaoyao_sovits_v2")
    assert voice["id"] == "yaoyao_reference"
    reference = tmp_path / Path(voice["reference_asset"]).relative_to("runtime")
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    assert settings.voice_reference_path(voice) == reference.resolve()
    assert settings.voice_reference_path(settings.voice("whale_reference")) == settings.root / "assets/whale/ref.wav"
    for identity in ("external_api", "whale_sample_v1"):
        with pytest.raises(ValueError):
            settings.voice_reference_path(settings.voice(identity))
