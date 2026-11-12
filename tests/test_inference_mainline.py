import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from tuber_studio.inference import InferenceBroker, resolve_llm_resource_key
from tuber_studio.inference_profiles import (
    DAFEIYU_9B_DIGESTS,
    DAFEIYU_9B_KEY,
    DAFEIYU_9B_PROJECTOR,
    DAFEIYU_9B_WEIGHTS,
    GROUNDED_DAFEIYU_ARTIFACTS,
    GROUNDED_DAFEIYU_BUNDLE,
    GROUNDED_DAFEIYU_KEY,
    GROUNDED_DAFEIYU_WEIGHTS,
)


@pytest.mark.parametrize("retired", ["llmdafeiyu_v6", "dafeiyu_v6"])
def test_retired_weights_cannot_silently_select_the_current_model(retired):
    with pytest.raises(HTTPException) as error:
        resolve_llm_resource_key(retired)
    assert error.value.status_code == 422
    assert resolve_llm_resource_key(GROUNDED_DAFEIYU_KEY) == GROUNDED_DAFEIYU_KEY


async def test_unconfigured_broker_selects_the_current_9b_resource(tmp_path):
    broker = InferenceBroker(tmp_path)
    try:
        assert broker.selected == DAFEIYU_9B_KEY
    finally:
        await broker.close()


def test_program_downloads_pair_the_current_weights_and_projector():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "config/jetson/downloads.json").read_text(encoding="utf-8"))
    files = {item["path"]: item for item in manifest["files"]}
    current = {f"models/{DAFEIYU_9B_WEIGHTS}": DAFEIYU_9B_DIGESTS["model"],
               f"models/{DAFEIYU_9B_PROJECTOR}": DAFEIYU_9B_DIGESTS["projector"]}
    assert {path: item["sha256"] for path, item in files.items() if path.startswith("models/")} == current
    compatible = {item["path"]: item for item in manifest["compatibility"]["dafeiyu-dora"]}
    for role, name in [("model", GROUNDED_DAFEIYU_WEIGHTS), ("projector", "mmproj-4x-f16.gguf")]:
        entry = compatible[f"models/{GROUNDED_DAFEIYU_BUNDLE}/{name}"]
        assert entry["sha256"] == GROUNDED_DAFEIYU_ARTIFACTS[role]
        assert entry["bytes"] > 600_000_000
        assert all("/68f9542e3c7dee1b41a1be12fe97f3095819b952/" in url for url in entry["urls"])
    assert "models/MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf" not in files
