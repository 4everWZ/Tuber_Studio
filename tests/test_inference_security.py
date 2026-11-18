import pytest

from tuber_studio.inference_validation import project_wav


def test_reference_uses_explicit_business_data_directory_without_reading_the_old_runtime(tmp_path):
    root, runtime = tmp_path / "program", tmp_path / "business-data"
    root.mkdir()
    voice = runtime / "tts/ref/voice.wav"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    assert project_wav(root, "runtime/tts/ref/voice.wav", runtime=runtime) == voice.resolve()
    with pytest.raises((ValueError, FileNotFoundError)):
        project_wav(root, "runtime/tts/ref/voice.wav")
    foreign = runtime / "private.wav"
    foreign.write_bytes(voice.read_bytes())
    with pytest.raises(ValueError):
        project_wav(root, str(foreign), runtime=runtime)


def test_wav_reference_rejects_links_and_traversal_even_when_target_is_a_wave(tmp_path):
    allowed = tmp_path / "assets/oc/ref.wav"
    allowed.parent.mkdir(parents=True)
    allowed.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    assert project_wav(tmp_path, "assets/oc/ref.wav") == allowed
    with pytest.raises(ValueError):
        project_wav(tmp_path, "assets/oc/../oc/ref.wav")
    with pytest.raises(ValueError):
        project_wav(tmp_path, "file:///etc/passwd")
    alias = allowed.with_name("alias.wav")
    try:
        alias.symlink_to(allowed)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError):
        project_wav(tmp_path, str(alias))


def test_entire_reference_directory_cannot_link_outside_project(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    reference = outside / "ref.wav"
    reference.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    try:
        (tmp_path / "assets").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError):
        project_wav(tmp_path, "assets/ref.wav")
