"""Program-only packaging never collects weights or private runtime state."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ready_assets", Path(__file__).parents[1] / "tools/jetson/package-ready.py"
)
assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assets)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    source = Path(__file__).parents[1]
    for name in (
        "config/jetson/downloads.json",
        "config/jetson/tts_infer.yaml",
        "config/studio.example.json",
        "tools/jetson/run-ready.sh",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    private = root / "runtime"
    private.mkdir()
    for name in ("admin-token", "studio.sqlite3", "MiniCPM5.gguf", "studio.json"):
        (private / name).write_bytes(b"operator-private-data")
    return root


def test_image_context_contains_only_presets_and_download_metadata(project):
    output = project / "tmp/build"
    result = assets.package(project, output)
    embedded = {
        str(p.relative_to(output / "builtin/files")).replace("\\", "/")
        for p in (output / "builtin/files").rglob("*")
        if p.is_file()
    }
    assert embedded == {"studio.json", "tts/tts_infer.yaml"}
    assert result["embedded_asset_bytes"] < 10000
    assert result["download_bytes"] > 2_000_000_000
    manifest = json.loads((output / "builtin/manifest.json").read_text())
    assert all("urls" in f or f["path"] in embedded for f in manifest["files"])
    preset = json.loads((output / "builtin/files/studio.json").read_text())
    assert preset["tts"]["voice_id"] == assets.VOICE
    assert preset["llm"]["context_tokens"] == 131072
    assert preset["inference_broker"]["mode"] == "live"
    assert all(b"operator-private-data" not in p.read_bytes() for p in output.rglob("*") if p.is_file())


def test_packaging_does_not_require_weights_or_runtime(project):
    shutil.rmtree(project / "runtime")
    assets.package(project, project / "tmp/build")


@pytest.mark.parametrize("change", ["private", "escape", "http", "link"])
def test_invalid_manifest_or_symlink_preset_is_rejected(project, change, tmp_path):
    path = project / "config/jetson/downloads.json"
    manifest = json.loads(path.read_text())
    if change == "private":
        manifest["files"][0]["path"] = "admin-token"
    elif change == "escape":
        manifest["files"][0]["path"] = "models/../../admin-token"
    elif change == "http":
        manifest["files"][0]["urls"] = ["http://untrusted.test/model"]
    else:
        outside = tmp_path / "secret.json"
        outside.write_text(json.dumps(manifest))
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            pytest.skip("Unprivileged symbolic links unavailable")
    if change != "link":
        path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        assets.package(project, project / "tmp/build")


def test_existing_build_context_is_preserved(project):
    output = project / "tmp/build"
    output.mkdir(parents=True)
    (output / "keep").write_text("unrelated")
    with pytest.raises(ValueError, match="new build context"):
        assets.package(project, output)
    assert (output / "keep").read_text() == "unrelated"
