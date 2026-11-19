"""Configure a visual API client and restore its previously verified evidence."""

import json

from .video_vision import ApiVideoEvidenceProvider


def configured_video_provider(runtime):
    return ApiVideoEvidenceProvider(runtime, runtime.settings.video_config,
                                    api_key=runtime.settings.provider_key("omni"))


async def restore_admission(runtime):
    provider = runtime.browsing.visual_provider
    path = runtime.settings.runtime / "video-vision-admission.json"
    if not isinstance(provider, ApiVideoEvidenceProvider) or not path.is_file() or path.is_symlink():
        return
    if path.stat().st_size > 65536:
        return
    snapshot = path.read_bytes()
    value = json.loads(snapshot)
    proof = value.get("proof", {})
    configuration = provider.identity()
    if proof.get("passed") is not True or proof.get("configuration") != configuration:
        return
    current = await provider._runtime_properties()
    if not isinstance(current, dict):
        return
    if current != value.get("probe", {}).get("properties"):
        return
    if (
        runtime.browsing.visual_provider is not provider
        or provider.identity() != configuration
    ):
        return
    try:
        if path.is_symlink() or path.read_bytes() != snapshot:
            return
    except OSError:
        return
    # No await after checking the owner/configuration and durable proof again.
    # A long probe can invalidate the proof without blocking the health observer.
    provider._probe, provider._admission = value["probe"], proof
