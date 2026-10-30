import io
import json
import wave

import httpx
import pytest

from tuber_studio.providers import VoiceService


def audio_bytes(audible):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 32000, 32000, "NONE", "PCM"))
        stream.writeframes((b"\x01\x01" if audible else b"\x00\x00") * 32000)
    return output.getvalue()


async def test_dynamic_silent_http_200_is_never_installed_as_success(settings):
    settings.data["tts"]["kind"] = "gpt_sovits"
    voice = settings.voice("whale_sovits_v2")
    service = VoiceService(settings)
    await service.client.aclose()
    payload = audio_bytes(False)
    service.client = httpx.AsyncClient(
        base_url="http://tts",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=payload, headers={"content-type": "audio/wav"})
        ),
    )
    try:
        with pytest.raises(ValueError, match="全静音"):
            await service.synthesize("这次应该生成真实的声音。", voice)
        assert not list(settings.audio.iterdir())
        payload = audio_bytes(True)
        actual = await service.synthesize("这次应该生成真实的声音。", voice)
        assert actual.duration == 1 and actual.path.is_file()
    finally:
        await service.close()


@pytest.fixture
def runtime_reference(settings, tmp_path, monkeypatch):
    voice = settings.voice("yaoyao_sovits_v2")
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config/voices.json").write_text(json.dumps([voice]), encoding="utf-8")
    reference = root / voice["reference_asset"]
    reference.parent.mkdir(parents=True)
    reference.write_bytes(audio_bytes(True))
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
    assets.write_bytes(audio_bytes(True))
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
        path.write_bytes(audio_bytes(True))
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
    (outside / reference.name).write_bytes(audio_bytes(True))
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


async def test_genie_runtime_reference_uses_checked_bundle_and_content_cache(settings, runtime_reference):
    voice, reference = runtime_reference
    settings.data["tts"].update(kind="gpt_sovits", base_url="http://genie", local_backend="genie_onnx_int8")
    service = VoiceService(settings)
    await service.client.aclose()
    calls, state = [], {"ready": True}

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/docs":
            return httpx.Response(
                200 if state["ready"] else 503,
                json={
                    "status": "ready",
                    "backend": "genie_onnx_int8",
                    "voice_id": voice["id"],
                    "bundle_identity": "a" * 64,
                },
            )
        body = json.loads(request.content)
        assert body["ref_audio_path"] == voice["server_reference_path"]
        assert body["prompt_text"] == voice["reference_text"]
        return httpx.Response(200, content=audio_bytes(True))

    service.client = httpx.AsyncClient(base_url="http://genie", transport=httpx.MockTransport(respond))
    try:
        first = await service.synthesize("今天也陪你慢慢聊。", voice)
        assert first.duration == 1 and first.path.is_file()
        assert (await service.synthesize(first.text, voice)).path == first.path
        changed = bytearray(reference.read_bytes())
        changed[-1] ^= 1
        reference.write_bytes(changed)
        assert (await service.synthesize(first.text, voice)).path != first.path
        assert calls == [
            ("GET", "/docs"),
            ("POST", "/tts"),
            ("GET", "/docs"),
            ("GET", "/docs"),
            ("POST", "/tts"),
        ]
        reference.unlink()
        state["ready"] = False
        with pytest.raises(httpx.HTTPStatusError):
            await service.synthesize(first.text, voice)
    finally:
        await service.close()
