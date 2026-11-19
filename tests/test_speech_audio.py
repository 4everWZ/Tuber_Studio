import io
import json
import wave

import pytest


def audio_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 32000, 32000, "NONE", "PCM"))
        stream.writeframes(b"\x01\x01" * 32000)
    return output.getvalue()


@pytest.fixture
def runtime_reference(settings, tmp_path, monkeypatch):
    voice = settings.voice("yaoyao_reference")
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    reference = settings.runtime / "tts/ref/yaoyao-reference32k.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(audio_bytes())
    monkeypatch.setattr(settings, "root", root)
    return voice, reference


def test_reference_resolver_preserves_assets_compatibility_and_public_asset_boundary(
    settings, runtime_reference
):
    voice, reference = runtime_reference
    assert settings.voice_reference_path(voice) == reference
    with pytest.raises(ValueError, match="素材"):
        settings.asset_path(voice["reference_asset"])
    assets = settings.root / "assets/oc/ref.wav"
    assets.parent.mkdir(parents=True)
    assets.write_bytes(audio_bytes())
    voice = dict(voice, reference_asset="assets/oc/ref.wav")
    (settings.root / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    assert settings.voice_reference_path(voice) == settings.asset_path(voice["reference_asset"]) == assets


@pytest.mark.parametrize(
    "relative",
    [
        "runtime/provider-credentials.json",
        "runtime/models/reference.wav",
        "runtime/audio/reference.wav",
        "runtime/tts/ref/../ref/reference.wav",
        "runtime/tts/ref/reference.pth",
        "runtime/tts/ref/missing.wav",
        "runtime/tts/ref",
        "file:///etc/passwd",
        "/etc/passwd",
    ],
)
def test_reference_resolver_rejects_other_files_and_traversal(settings, runtime_reference, relative):
    voice, _ = runtime_reference
    # Place disallowed files on disk, so failure must enforce scope/format rather than just existence.
    if relative.startswith("runtime/") and relative not in {"runtime/tts/ref/missing.wav", "runtime/tts/ref"}:
        path = settings.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio_bytes())
    voice = dict(voice, reference_asset=relative)
    (settings.root / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        settings.voice_reference_path(voice)


def test_reference_resolver_rejects_even_an_absolute_in_scope_path(settings, runtime_reference):
    voice, reference = runtime_reference
    voice = dict(voice, reference_asset=str(reference))
    (settings.root / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    with pytest.raises(ValueError, match="相对路径"):
        settings.voice_reference_path(voice)


@pytest.mark.parametrize("change", [{"id": "unconfigured"}, {"reference_asset": "assets/other.wav"}])
def test_reference_resolver_rejects_unconfigured_or_modified_voice(settings, runtime_reference, change):
    voice, _ = runtime_reference
    with pytest.raises(ValueError):
        settings.voice_reference_path(voice | change)


def test_reference_resolver_rejects_other_provider_and_invalid_wave(settings, runtime_reference):
    voice, reference = runtime_reference
    other = voice | {"provider": "prerecorded"}
    (settings.root / "config/voices.json").write_text(json.dumps([other]), encoding="utf-8")
    with pytest.raises(ValueError):
        settings.voice_reference_path(other)
    (settings.root / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    reference.write_bytes(b"not a wave")
    with pytest.raises(ValueError, match="WAV"):
        settings.voice_reference_path(voice)


@pytest.mark.parametrize("directory", [False, True])
def test_reference_resolver_rejects_symlink_escape(settings, runtime_reference, tmp_path, directory):
    voice, reference = runtime_reference
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / reference.name).write_bytes(audio_bytes())
    reference.unlink()
    try:
        if directory:
            reference.parent.rmdir()
            reference.parent.symlink_to(outside, target_is_directory=True)
        else:
            reference.symlink_to(outside / reference.name)
    except OSError:
        pytest.skip("This platform does not permit unprivileged symlinks")
    with pytest.raises(ValueError, match="links"):
        settings.voice_reference_path(voice)
