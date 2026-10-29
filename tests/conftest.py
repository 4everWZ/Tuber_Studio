import json

import pytest

from tuber_studio.config import ROOT, Settings
from tuber_studio.runtime import Runtime
from tuber_studio.store import Store


@pytest.fixture
def settings(tmp_path):
    config = Settings(runtime=tmp_path)
    config.data["llm"]["kind"] = "demo"
    config.data["inference_broker"]["enabled"] = False
    config.data["tts"]["kind"] = "prerecorded"
    # These legacy worker tests explicitly exercise the retained live subsystem.
    config.data["features"]["live_enabled"] = True
    config.data["queue_limit"] = 3
    config.data["playback_wait_seconds"] = 0.15
    return config


@pytest.fixture
def store(settings):
    seeds = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    for seed in seeds:
        seed["voice_profile"] = "whale_sample_v1"
    value = Store(settings.runtime / "test.sqlite3", seeds)
    yield value
    value.close()


@pytest.fixture
async def runtime(settings, store):
    value = Runtime(settings, store)
    await value.open()
    yield value
    await value.close()
