import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from tuber_studio.runtime_authority import (
    BusinessAuthorityError,
    acquire_business_writer,
    assert_business_writer,
)

SPEC = importlib.util.spec_from_file_location(
    "business_migration", Path(__file__).resolve().parents[1] / "tools/deployment/migrate_business.py"
)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def source_fixture(path):
    path.mkdir()
    for name, data in {
        "admin-token": b"private-session-key",
        "overlay-token": b"private-overlay-key",
        "bilibili-credentials.json": b'{"sessdata":"private-cookie"}',
        "studio.json": b'{"preserve":"operator"}',
        "browsing/source.json": b'{"quoted":"original context"}',
        "online_music/song.flac": b"fLaC-keep-original",
        "memes/upload.png": b"meme-asset",
        "models/main.gguf": b"model-do-not-copy",
        "tts/genie/voice/model.bin": b"voice-do-not-copy",
        "tts/ref/reference.wav": b"small-reference-digest-source",
        "engines/llama-server": b"engine-do-not-copy",
        "tmp/old-vlm-evaluation/raw.json": b"experiment-do-not-copy",
        "evaluations/old-run/report.json": b"archived-evaluation-do-not-copy",
        "backups/old/studio.sqlite3": b"old-backup-do-not-copy",
        "deployments/old/src/app.py": b"old-source-do-not-copy",
        "inference-llm2b.log": b"inference-log-do-not-copy",
        "resource-minicpm-measurement.wav": b"old-test-audio-do-not-copy",
        "singing/output/temporary.wav": b"business-temporary-audio-keep",
    }.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    with sqlite3.connect(path / "studio.sqlite3") as connection:
        connection.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, platform_uid TEXT);"
            "CREATE TABLE relations(user_id INTEGER, affinity INTEGER);"
            "CREATE TABLE comments(id INTEGER PRIMARY KEY, content TEXT);"
            "CREATE TABLE browsing_materials(id TEXT PRIMARY KEY,path TEXT);"
            "CREATE TABLE admin_sessions(id TEXT, token_hash TEXT);"
            "CREATE TABLE pending_actions(id TEXT,status TEXT);"
        )
        connection.execute("INSERT INTO users VALUES(1,'uid-123')")
        connection.execute("INSERT INTO relations VALUES(1,79)")
        connection.execute("INSERT INTO comments VALUES(1,?)", (str(path / "browsing/source.json"),))
        connection.execute(
            "INSERT INTO browsing_materials VALUES('source',?)", (str(path / "browsing/source.json"),)
        )
        connection.execute("INSERT INTO admin_sessions VALUES('session','hash')")
        connection.execute("INSERT INTO pending_actions VALUES('action','unknown')")
    return path


def test_snapshot_restore_keeps_business_identity_credentials_cache_and_unknown_actions(tmp_path):
    source = source_fixture(tmp_path / "source")
    bundle, target = tmp_path / "backup", tmp_path / "restored"
    snapshot = migration.snapshot(source, bundle)
    assert snapshot["database"]["tables"]["users"] == 1
    assert "private-cookie" not in json.dumps(snapshot)
    assert migration.verify(bundle)["verified"]
    result = migration.restore(bundle, target)
    assert result["state"] == "staged"
    assert not (target / "models").exists()
    assert not (target / "tts/genie").exists()
    assert (target / "tts/ref/reference.wav").read_bytes() == b"small-reference-digest-source"
    assert not (target / "engines").exists()
    for name in (
        "tmp",
        "evaluations",
        "backups",
        "deployments",
        "inference-llm2b.log",
        "resource-minicpm-measurement.wav",
    ):
        assert not (target / name).exists()
        assert (source / name).exists()
    assert (target / "singing/output/temporary.wav").read_bytes() == b"business-temporary-audio-keep"
    for name in (
        "admin-token",
        "overlay-token",
        "studio.json",
        "bilibili-credentials.json",
        "online_music/song.flac",
    ):
        assert (target / name).read_bytes() == (source / name).read_bytes()
    with sqlite3.connect(target / "studio.sqlite3") as connection:
        assert connection.execute("SELECT affinity FROM relations").fetchone() == (79,)
        assert connection.execute("SELECT status FROM pending_actions").fetchone() == ("unknown",)
        assert connection.execute("SELECT path FROM browsing_materials").fetchone() == (
            str(target / "browsing/source.json"),
        )
        # A path written in an actual comment is original user data, not a storage reference.
        assert connection.execute("SELECT content FROM comments").fetchone() == (
            str(source / "browsing/source.json"),
        )
    with pytest.raises(BusinessAuthorityError):
        assert_business_writer(target)
    assert not (source / "business-authority.json").exists()
    assert (source / "models/main.gguf").is_file()


def test_sqlite_backup_includes_committed_wal_rows(tmp_path):
    source = source_fixture(tmp_path / "source")
    connection = sqlite3.connect(source / "studio.sqlite3")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO users VALUES(2,'recent-wal-row')")
        connection.commit()
        migration.snapshot(source, tmp_path / "backup")
        data = tmp_path / "backup/data"
        expected = {
            item["path"] for item in json.loads((tmp_path / "backup/manifest.json").read_text())["files"]
        }
        assert {p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()} == expected
        assert not (data / "studio.sqlite3-wal").exists()
        assert not (data / "studio.sqlite3-shm").exists()
        assert migration.verify(tmp_path / "backup")["database"]["tables"]["users"] == 2
        assert {p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()} == expected
        assert not (data / "studio.sqlite3-wal").exists()
        assert not (data / "studio.sqlite3-shm").exists()
        with sqlite3.connect(tmp_path / "backup/data/studio.sqlite3") as copy:
            assert copy.execute("SELECT platform_uid FROM users WHERE id=2").fetchone() == ("recent-wal-row",)
    finally:
        connection.close()


def test_snapshot_refuses_running_service_and_existing_destinations(tmp_path):
    source = source_fixture(tmp_path / "source")
    with acquire_business_writer(source):
        with pytest.raises(BusinessAuthorityError, match="running writer"):
            migration.snapshot(source, tmp_path / "backup")
    with pytest.raises(ValueError, match="must not overlap"):
        migration.snapshot(source, source / "backup")
    migration.snapshot(source, tmp_path / "backup")
    with pytest.raises(ValueError, match="never overwritten"):
        migration.snapshot(source, tmp_path / "backup")
    with pytest.raises(ValueError, match="never overwritten"):
        migration.restore(tmp_path / "backup", source)


def test_corrupted_or_extra_snapshot_file_cannot_be_restored(tmp_path):
    source = source_fixture(tmp_path / "source")
    bundle = tmp_path / "backup"
    migration.snapshot(source, bundle)
    (bundle / "data/admin-token").write_bytes(b"changed-key")
    with pytest.raises(ValueError, match="integrity mismatch"):
        migration.restore(bundle, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_cross_platform_rebinding_is_scoped_to_runtime_root_and_known_columns(tmp_path):
    source = source_fixture(tmp_path / "source")
    db = source / "studio.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE browsing_materials SET path='/old/runtime/browsing/source.json'")
    changed = migration.rebind_runtime_paths(db, "/old/runtime", source)
    assert len(changed) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT path FROM browsing_materials").fetchone() == (
            str(source / "browsing/source.json"),
        )
        assert connection.execute("SELECT content FROM comments").fetchone() == (
            str(source / "browsing/source.json"),
        )


@pytest.mark.parametrize("bad_path", ["../outside", ".", "C:/outside", "models/main.gguf"])
def test_manifest_cannot_restore_outside_business_boundary(tmp_path, bad_path):
    source = source_fixture(tmp_path / "source")
    bundle = tmp_path / "backup"
    migration.snapshot(source, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = bad_path
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid or duplicate"):
        migration.restore(bundle, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_environment_cache_links_are_excluded_without_following_or_weakening_business_paths(tmp_path):
    source = source_fixture(tmp_path / "source")
    external = tmp_path / "environment-cache-target"
    external.mkdir()
    (external / "environment-cache.bin").write_bytes(b"not-business")
    for root in ("uv-cache", ".nv"):
        directory = source / root
        directory.mkdir()
        try:
            (directory / "external-cache").symlink_to(external, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks unavailable")
    inventory = migration.inventory(source)
    assert inventory["top_levels"]["uv-cache"]["categories"]["inference_assets"]["files"] == 1
    bundle = tmp_path / "backup"
    migration.snapshot(source, bundle)
    assert not (bundle / "data/uv-cache").exists()
    assert not (bundle / "data/.nv").exists()
    (source / "browsing/external-cache").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="business symbolic links"):
        migration.snapshot(source, tmp_path / "forbidden-backup")
