import asyncio
import base64
import json

import httpx
import pytest
from offline_chat import OfflineChat

from tuber_studio.config import ROOT, Settings
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store


@pytest.fixture
def wait_for_worker_start():
    """Wait for a test barrier while surfacing a worker's early exception."""

    async def wait(worker, started, *, timeout=2):
        signal = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait(
                {worker, signal}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if worker in done:
                await worker
                pytest.fail("Worker finished before the test released the startup barrier")
            if signal not in done:
                pytest.fail(f"Worker did not reach the startup barrier within {timeout}s")
        finally:
            signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)

    return wait


@pytest.fixture
def settings(tmp_path, monkeypatch):
    config = Settings(runtime=tmp_path)
    config.data["omni"].update(
        context_tokens=131072, native_context_tokens=131072, auto_compact_token_limit=117964,
    )
    from tuber_studio import runtime as runtime_module

    create_chat = runtime_module.create_llm
    monkeypatch.setattr(runtime_module, "create_llm", lambda current: OfflineChat() if current is config else create_chat(current))
    create_operations = runtime_module.create_operation_llm
    monkeypatch.setattr(runtime_module, "create_operation_llm",
                        lambda current: OfflineChat() if current is config else create_operations(current))
    config.data["features"]["live_enabled"] = True
    config.data["queue_limit"] = 3
    config.data["playback_wait_seconds"] = 0.15
    return config


@pytest.fixture
def store(settings):
    seeds = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    value = Store(settings.runtime / "test.sqlite3", seeds)
    yield value
    value.close()


@pytest.fixture
async def runtime(settings, store):
    value = Runtime(settings, store)
    sample = (ROOT / "assets/whale/ref.wav").read_bytes()

    def speech(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": settings.data["omni"]["model"]}]})
        assert request.url.path.endswith("/audio/speech")
        words = json.loads(request.content)["input"]
        return httpx.Response(200, content=sample, headers={
            "X-Omni-Transcript": base64.b64encode(words.encode()).decode(),
        })

    await value.voices.client.aclose()
    value.voices.client = httpx.AsyncClient(
        base_url=settings.data["omni"]["base_url"] + "/", transport=httpx.MockTransport(speech),
    )
    await value.open()
    yield value
    await value.close()
