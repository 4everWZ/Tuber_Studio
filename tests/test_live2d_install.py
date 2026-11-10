import json

import pytest

from tools.deployment.install_live2d_avatar import preflight
from tuber_studio.live.avatar import SDK


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def export(tmp_path):
    package = tmp_path / "export"
    subdir = package / "rig"
    subdir.mkdir(parents=True)
    write_json(package / "avatar.json", {
        "model": "rig/dafeiyu.model3.json",
        "license": "Local noncommercial test fixture",
        "motions": {"idle": {"group": "Idle"}},
        "expressions": {"cheerful": "Smile"},
    })
    write_json(subdir / "dafeiyu.model3.json", {"Version": 3, "FileReferences": {
        "Moc": "dafeiyu.moc3", "Textures": ["texture.png"],
        "Motions": {"Idle": [{"File": "idle.motion3.json"}]},
        "Expressions": [{"Name": "Smile", "File": "smile.exp3.json"}],
    }})
    (subdir / "dafeiyu.moc3").write_bytes(b"MOC3 fixture")
    (subdir / "texture.png").write_bytes(b"fixture")
    write_json(subdir / "idle.motion3.json", {})
    write_json(subdir / "smile.exp3.json", {})
    (package / "editable.cmo3").write_bytes(b"fixture must stay at source")
    return package


def test_install_copies_only_runtime_closure(export, tmp_path):
    runtime = tmp_path / "runtime"
    check = preflight(export, runtime, "whale")
    assert set(check["sdk_missing"]) == set(SDK)
    assert not runtime.exists()
    with pytest.raises(ValueError, match="SDK"):
        preflight(export, runtime, "whale", install=True)
    sdk = runtime / "live2d" / "sdk"
    sdk.mkdir(parents=True)
    for name in SDK:
        (sdk / name).write_bytes(b"local test fixture")
    result = preflight(export, runtime, "whale", install=True)
    assert result["installed"] and result["files"] == 6 and not result["sdk_missing"]
    installed = runtime / "live2d" / "whale"
    assert (installed / "avatar.json").is_file()
    assert (installed / "rig" / "idle.motion3.json").is_file()
    assert not (installed / "editable.cmo3").exists()
    with pytest.raises(ValueError, match="已有"):
        preflight(export, runtime, "whale", install=True)


def test_preflight_rejects_broken_mapping_and_binary(export, tmp_path):
    manifest = export / "avatar.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    write_json(manifest, data | {"motions": {"idle": {"group": "Idle", "index": 1}}})
    with pytest.raises(ValueError, match="动作映射"):
        preflight(export, tmp_path, "whale")
    write_json(manifest, data | {"expressions": {"cheerful": "Other"}})
    with pytest.raises(ValueError, match="表情映射"):
        preflight(export, tmp_path, "whale")
    write_json(manifest, data)
    (export / "rig" / "dafeiyu.moc3").write_bytes(b"not a Moc")
    with pytest.raises(ValueError, match="MOC3"):
        preflight(export, tmp_path, "whale")
