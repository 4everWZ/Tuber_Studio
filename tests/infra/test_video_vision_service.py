from types import SimpleNamespace

import pytest

from tuber_studio.core.config import atomic_json
from tuber_studio.infra.inference.video_vision import ApiVideoEvidenceProvider
from tuber_studio.infra.inference.video_vision_service import restore_admission


@pytest.mark.parametrize("mismatch", [None, "model", "properties", "stale"])
async def test_restored_admission_requires_matching_service_and_current_proof(settings, monkeypatch, mismatch):
    runtime = SimpleNamespace(settings=settings)
    provider = ApiVideoEvidenceProvider(runtime, {"model": "visual", "base_url": "http://127.0.0.1:8000/v1"})
    runtime.browsing = SimpleNamespace(visual_provider=provider)
    properties = {"context_tokens": 32768, "endpoint": str(provider.api.client.base_url)}
    configuration = provider.identity()
    proof = {"passed": True, "configuration": configuration if mismatch != "model" else configuration | {"model": "other"}}
    path = settings.runtime / "video-vision-admission.json"
    atomic_json(path, {"proof": proof, "probe": {"properties": properties}})

    async def observe():
        if mismatch == "stale":
            path.unlink()
        return properties if mismatch != "properties" else properties | {"changed": True}

    monkeypatch.setattr(provider, "_runtime_properties", observe)
    try:
        await restore_admission(runtime)
        assert (provider._admission is not None) == (mismatch is None)
    finally:
        await provider.close()
