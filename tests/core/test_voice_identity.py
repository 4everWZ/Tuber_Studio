import json
import wave
from pathlib import Path

import pytest

from tuber_studio.core.config import Settings
from tuber_studio.core.identity import identity_snapshot
from tuber_studio.core.models import Character, canonical_voice_id
from tuber_studio.core.store import Store, dump

ROOT = Path(__file__).resolve().parents[2]


def bundled_cards():
    return json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))


def test_omni_references_and_fixed_sample_have_distinct_assets():
    voices = {v["id"]: v for v in json.loads((ROOT / "config/voices.json").read_text(encoding="utf-8"))}
    whale, yaoyao = voices["whale_reference"], voices["yaoyao_reference"]
    sample, external = voices["whale_sample_v1"], voices["external_api"]
    assert bundled_cards()[0]["voice_profile"] == whale["id"]
    assert whale["provider"] == yaoyao["provider"] == "omni_reference"
    assert whale["reference_asset"] == "assets/whale/ref.wav"
    assert yaoyao["reference_asset"] == "runtime/tts/ref/yaoyao-reference32k.wav"
    assert sample["provider"] == "prerecorded" and sample["sample_asset"] == "assets/whale/sample1.wav"
    assert external["provider"] == "openai_compatible"
    assert "reference_asset" not in sample and "reference_asset" not in external
    manifest = json.loads((ROOT / "assets/whale/manifest.json").read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    for asset, text in ((whale["reference_asset"], whale["reference_text"]),
                        (sample["sample_asset"], sample["sample_text"])):
        assert entries[asset]["text"] == text
        with wave.open(str(ROOT / asset)) as audio:
            assert audio.getnframes() > 0
    for voice in voices.values():
        assert voice["source"] and voice["license_status"] and not voice["commercial_allowed"]
        assert not {"weights_version", "server_reference_path"} & voice.keys()


def test_saved_voice_setting_resolves_the_same_recording(tmp_path, monkeypatch):
    path = tmp_path / "studio.json"
    monkeypatch.setenv("TUBER_CONFIG", str(path))
    path.write_text(json.dumps({"queue_limit": 40, "event_ttl_seconds": 90,
                                "tts": {"voice_id": "yaoyao_sovits_v2"}}), encoding="utf-8")
    settings = Settings(runtime=tmp_path)
    assert settings.data["voice_id"] == "yaoyao_reference"
    voice = settings.voice("yaoyao_sovits_v2")
    assert voice == settings.voice("yaoyao_reference")
    recording = tmp_path / "tts/ref/yaoyao-reference32k.wav"
    recording.parent.mkdir(parents=True)
    with wave.open(str(recording), "wb") as wav:
        wav.setparams((1, 2, 32000, 0, "NONE", "PCM"))
        wav.writeframes(b"\0\0" * 320)
    assert settings.voice_reference_path(voice) == recording.resolve()
    assert settings.voice_reference_path(settings.voice("whale_sovits_v2")) == ROOT / "assets/whale/ref.wav"
    for key in ("external_api", "whale_sample_v1"):
        with pytest.raises(ValueError):
            settings.voice_reference_path(settings.voice(key))


@pytest.mark.parametrize(("old_id", "expected_id"), [
    ("yaoyao_sovits_v2", "yaoyao_reference"), ("whale_sovits_v2", "whale_reference"),
    ("external_api", "external_api"), ("whale_sample_v1", "whale_sample_v1"),
    ("custom_whale_sovits_v2", "custom_whale_sovits_v2"),
])
def test_saved_voice_migration_keeps_version_receipt_and_edit_identity(tmp_path, old_id, expected_id):
    assert canonical_voice_id(old_id) == expected_id
    path = tmp_path / "custom.sqlite3"
    seeds = bundled_cards()
    store = Store(path, seeds)
    card = seeds[0] | {"id": "my-oc", "name": "自家角色", "voice_profile": old_id,
                       "core": {"identity": old_id, "owner_extension": [old_id]}}
    original = Character.model_validate(card).model_dump() | {
        "voice_profile": old_id, "version": 2, "_edit_identity": "edit-2"}
    for version in (1, 2):
        store.save_character(card)
        raw = original | {"version": version, "_edit_identity": f"edit-{version}"}
        store.execute("UPDATE character_versions SET config=? WHERE character_id='my-oc' AND version=?",
                      (dump(raw), version))
    store.activate("my-oc")
    history = store.all("SELECT * FROM character_versions WHERE character_id='my-oc' ORDER BY version")
    store.execute("INSERT INTO drafts(id,comment_id,status,text,character_id,character_version,receipt) "
                  "VALUES(1,1,'published',?,?,2,?)",
                  ("已送达", "my-oc", dump({"voice_id": old_id})))
    receipt = store.one("SELECT * FROM drafts WHERE id=1")
    store.close()

    for _ in range(2):
        store = Store(path, seeds)
        try:
            active = store.character()
            assert active == original | {"voice_profile": expected_id}
            assert identity_snapshot({}, active) == identity_snapshot({}, original)
            store.replace_character(card)
            assert store.all("SELECT * FROM character_versions WHERE character_id='my-oc' ORDER BY version") == history
            assert store.character("my-oc", 1)["voice_profile"] == expected_id
            assert [item["version"] for item in store.versions("my-oc")] == [2, 1]
            assert store.one("SELECT * FROM drafts WHERE id=1") == receipt
        finally:
            store.close()

    store = Store(path, seeds)
    try:
        store.replace_character(card | {"name": "号主修改后的名称"})
        saved = json.loads(store.one("SELECT config FROM character_versions WHERE character_id='my-oc' AND version=2")["config"])
        assert saved["voice_profile"] == expected_id and saved["core"] == card["core"]
        assert store.one("SELECT * FROM drafts WHERE id=1") == receipt
    finally:
        store.close()
