"""Portable, offline business snapshot and isolated restore; no model downloads.

Stop the legacy business service before its first snapshot. Updated services
hold the same OS writer lock, so snapshot/retirement cannot overlap a writer.
Snapshots retain cache and credentials, while model/engine assets stay on the
inference node. This tool never starts services or submits platform messages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from tuber_studio.runtime_authority import (  # noqa: E402
    AUTHORITY_FILE,
    LOCK_FILE,
    acquire_business_writer,
    set_business_authority,
)

MODEL_ROOTS = {"models", "engines", "tts", "nltk_data", "cache", "uv-cache", ".nv"}
RETAINED_ROOTS = {"experiments", "evaluations", "backups", "deployments"}
EPHEMERAL_ROOTS = {".portable-launch", "logs", "song_component", "__pycache__", "tmp"}
EPHEMERAL_FILES = {
    LOCK_FILE,
    AUTHORITY_FILE,
    ".business-restore.json",
    "portable-status.json",
    "inference-status.json",
    "studio.sqlite3-wal",
    "studio.sqlite3-shm",
}
CACHE_ROOTS = {"audio", "music", "online_music", "browsing", "singing"}
MODEL_SUFFIXES = {".gguf", ".safetensors", ".onnx", ".pth", ".ckpt"}
DATABASE = "studio.sqlite3"
PATH_COLUMNS = (("browsing_materials", "path"), ("media_cache_access", "path"))


def private_json(path: Path, value):
    with path.open("x", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())


def regular(path: Path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Migration does not follow symbolic links or junctions")
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise ValueError("Migration supports only regular files and directories")


def root_directory(path: Path) -> Path:
    path = path.absolute()
    for item in (path, *path.parents):
        regular(item)
    if not path.is_dir():
        raise ValueError("Runtime source must be an existing directory")
    return path.resolve()


def category(relative: Path) -> str:
    first = relative.parts[0]
    if relative.parts[:2] == ("tts", "ref"):
        # The business-side voice cache binds the reference audio digest even
        # though the actual model and synthesis stay on the inference node.
        return "business_persistent"
    if first in MODEL_ROOTS or relative.suffix.lower() in MODEL_SUFFIXES:
        return "inference_assets"
    if first in RETAINED_ROOTS or (len(relative.parts) == 1 and first.startswith("resource-minicpm")):
        return "retained_artifacts"
    if (
        first in EPHEMERAL_ROOTS
        or relative.as_posix() in EPHEMERAL_FILES
        or (len(relative.parts) == 1 and first.startswith("inference-") and relative.suffix == ".log")
        or (len(relative.parts) == 1 and first in {"business-stdout.log", "business-stderr.log"})
    ):
        return "transient"
    return "business_cache" if first in CACHE_ROOTS else "business_persistent"


def walk_files(root: Path, *, business_only=False):
    def visit(directory):
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root)
                kind = category(relative)
                if business_only and not kind.startswith("business_") and relative.as_posix() != "tts":
                    # Excluded engine/environment caches may contain links. A
                    # business snapshot never enters these directories at all.
                    continue
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    if not kind.startswith("business_"):
                        yield relative, kind, entry.stat(follow_symlinks=False)
                        continue
                    raise ValueError("Migration does not follow business symbolic links or junctions")
                regular(path)
                if entry.is_dir(follow_symlinks=False):
                    yield from visit(path)
                elif entry.is_file(follow_symlinks=False):
                    yield relative, kind, entry.stat(follow_symlinks=False)

    yield from visit(root)


def checksum(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def copy_checked(original: Path, target: Path) -> tuple[int, str]:
    """Hash during the required copy instead of rereading up to 15 GiB of media."""
    digest, count = hashlib.sha256(), 0
    with original.open("rb") as incoming:
        before = os.fstat(incoming.fileno())
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
                count += len(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or count != after.st_size:
        raise ValueError("Business file changed during copy; stop its writer first")
    return count, digest.hexdigest()


def database_summary(path: Path, *, immutable=False) -> dict:
    if not path.is_file():
        return {"present": False}
    uri = path.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise ValueError("Business database failed integrity_check")
        foreign_keys = len(list(connection.execute("PRAGMA foreign_key_check")))
        tables = {}
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            identifier = '"' + name.replace('"', '""') + '"'
            tables[name] = connection.execute(f"SELECT COUNT(*) FROM {identifier}").fetchone()[0]
        return {"present": True, "integrity": "ok", "foreign_key_violations": foreign_keys, "tables": tables}


def inventory(source: Path) -> dict:
    source = root_directory(source)
    groups = {
        name: {"files": 0, "bytes": 0}
        for name in (
            "business_persistent",
            "business_cache",
            "inference_assets",
            "retained_artifacts",
            "transient",
        )
    }
    top_levels = {}
    for relative, kind, metadata in walk_files(source):
        groups[kind]["files"] += 1
        groups[kind]["bytes"] += metadata.st_size
        top = top_levels.setdefault(relative.parts[0], {"files": 0, "bytes": 0, "categories": {}})
        top["files"] += 1
        top["bytes"] += metadata.st_size
        categorized = top["categories"].setdefault(kind, {"files": 0, "bytes": 0})
        categorized["files"] += 1
        categorized["bytes"] += metadata.st_size
    return {
        "source": str(source),
        "categories": groups,
        "top_levels": top_levels,
        "database": database_summary(source / DATABASE),
    }


def _fresh_destination(path: Path, source: Path):
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise ValueError("Use a new destination; existing data is never overwritten")
    for parent in path.parents:
        regular(parent)
    resolved = path.resolve()
    if resolved.is_relative_to(source) or source.is_relative_to(resolved):
        raise ValueError("Snapshot and runtime directories must not overlap")
    return resolved


def snapshot(source: Path, bundle: Path) -> dict:
    source = root_directory(source)
    bundle = _fresh_destination(bundle, source)
    with acquire_business_writer(source):
        before = inventory(source)
        bundle.mkdir(parents=True, mode=0o700)
        data = bundle / "data"
        data.mkdir(mode=0o700)
        files, names = [], set()
        for relative, kind, metadata in walk_files(source, business_only=True):
            if not kind.startswith("business_"):
                continue
            if relative.as_posix().casefold() in names:
                raise ValueError("Business file names collide on a case-insensitive target")
            names.add(relative.as_posix().casefold())
            original, target = source / relative, data / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if relative.as_posix() == DATABASE:
                with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as incoming:
                    with closing(sqlite3.connect(target)) as outgoing:
                        incoming.backup(outgoing)
                        # A WAL source propagates its journal mode into backup.
                        # Publish a self-contained offline DB before hashing it.
                        outgoing.commit()
                        if outgoing.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                            raise ValueError("Snapshot database could not become self-contained")
                count, digest = target.stat().st_size, checksum(target)
            else:
                count, digest = copy_checked(original, target)
                after = original.stat()
                if (metadata.st_size, metadata.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Business file changed during snapshot; stop its writer first")
            os.chmod(target, 0o600)
            files.append(
                {
                    "path": relative.as_posix(),
                    "bytes": count,
                    "sha256": digest,
                    "category": kind,
                }
            )
        summary = database_summary(data / DATABASE, immutable=True)
        if summary != before["database"]:
            raise ValueError("Database changed during snapshot; stop the legacy writer first")
        manifest = {
            "format": 1,
            "migration_id": str(uuid4()),
            "source_root": str(source),
            "source_host": socket.gethostname(),
            "files": files,
            "database": summary,
            "inventory": before,
        }
        private_json(bundle / "manifest.json", manifest)
        return {
            "bundle": str(bundle),
            "migration_id": manifest["migration_id"],
            "files": len(files),
            "bytes": sum(item["bytes"] for item in files),
            "database": summary,
        }


def _manifest(bundle: Path):
    bundle = root_directory(bundle)
    path = bundle / "manifest.json"
    regular(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Migration manifest too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("format") != 1 or not isinstance(value.get("files"), list):
        raise ValueError("Unsupported migration manifest")
    names = set()
    if not isinstance(value.get("migration_id"), str) or not value["migration_id"]:
        raise ValueError("Migration identity missing")
    for item in value["files"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid business snapshot entry")
        name = item.get("path")
        if (
            not isinstance(name, str)
            or not name
            or name == "."
            or "\0" in name
            or "\\" in name
            or ":" in name
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name
            or name.casefold() in names
            or category(Path(name)) not in {"business_persistent", "business_cache"}
        ):
            raise ValueError("Invalid or duplicate business snapshot path")
        if (
            type(item.get("bytes")) is not int
            or item["bytes"] < 0
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        ):
            raise ValueError("Invalid business snapshot integrity metadata")
        names.add(name.casefold())
    return bundle, value


def verify(bundle: Path) -> dict:
    bundle, manifest = _manifest(bundle)
    data = root_directory(bundle / "data")
    actual = set()
    for relative, _, _ in walk_files(data):
        actual.add(relative.as_posix())
    expected = {item["path"] for item in manifest["files"]}
    if actual != expected:
        raise ValueError("Snapshot file set differs from manifest")
    for item in manifest["files"]:
        path = data / item["path"]
        if path.stat().st_size != item["bytes"] or checksum(path) != item["sha256"]:
            raise ValueError("Snapshot file integrity mismatch")
    if database_summary(data / DATABASE, immutable=True) != manifest["database"]:
        raise ValueError("Snapshot database summary differs")
    return {
        "verified": True,
        "migration_id": manifest["migration_id"],
        "files": len(expected),
        "database": manifest["database"],
    }


def rebind_runtime_paths(database: Path, old_root: str, destination: Path) -> list[dict]:
    """Only known storage columns; never rewrite a comment, prompt, or receipt."""
    if not database.is_file():
        return []
    old = old_root.replace("\\", "/").rstrip("/")
    changes = []
    with closing(sqlite3.connect(database)) as connection, connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in PATH_COLUMNS:
            if table not in tables:
                continue
            for rowid, raw in connection.execute(f'SELECT rowid,"{column}" FROM "{table}"'):
                if not isinstance(raw, str):
                    continue
                normalized = raw.replace("\\", "/")
                if normalized.startswith(old + "/"):
                    relative = normalized[len(old) + 1 :]
                    if ".." in PurePosixPath(relative).parts:
                        raise ValueError("Stored runtime path traverses its old boundary")
                    target = str(destination.joinpath(*PurePosixPath(relative).parts))
                    if not Path(target).is_file():
                        raise ValueError("A rebound database file is absent from restored data")
                    connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (target, rowid))
                    changes.append({"table": table, "column": column, "rowid": rowid})
    return changes


def restore(bundle: Path, destination: Path) -> dict:
    verification = verify(bundle)
    bundle, manifest = _manifest(bundle)
    destination = _fresh_destination(destination, bundle)
    destination.mkdir(parents=True, mode=0o700)
    # Publish the no-start state before copying any configuration or database.
    set_business_authority(destination, "staged", migration_id=manifest["migration_id"])
    for item in manifest["files"]:
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        count, digest = copy_checked(bundle / "data" / item["path"], target)
        if count != item["bytes"] or digest != item["sha256"]:
            raise ValueError("Restored file differs from verified snapshot")
    changes = rebind_runtime_paths(destination / DATABASE, manifest["source_root"], destination)
    summary = database_summary(destination / DATABASE, immutable=True)
    if summary != manifest["database"]:
        raise ValueError("Restored database summary differs from snapshot")
    result = {
        **verification,
        "destination": str(destination),
        "state": "staged",
        "path_rebindings": changes,
        "source_host": manifest["source_host"],
    }
    private_json(destination / ".business-restore.json", result)
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    actions = result.add_subparsers(dest="action", required=True)
    for name in ("inventory", "snapshot"):
        command = actions.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        if name == "snapshot":
            command.add_argument("--bundle", type=Path, required=True)
    check = actions.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    recover = actions.add_parser("restore")
    recover.add_argument("--bundle", type=Path, required=True)
    recover.add_argument("--destination", type=Path, required=True)
    authority = actions.add_parser("authority")
    authority.add_argument("state", choices=("active", "staged", "retired"))
    authority.add_argument("--runtime", type=Path, required=True)
    authority.add_argument("--migration-id", required=True)
    authority.add_argument("--writer-host")
    authority.add_argument("--destination")
    return result


def main():
    args = parser().parse_args()
    try:
        if args.action == "inventory":
            value = inventory(args.source)
        elif args.action == "snapshot":
            value = snapshot(args.source, args.bundle)
        elif args.action == "verify":
            value = verify(args.bundle)
        elif args.action == "restore":
            value = restore(args.bundle, args.destination)
        else:
            value = set_business_authority(
                args.runtime,
                args.state,
                migration_id=args.migration_id,
                writer_host=args.writer_host,
                destination=args.destination,
            )
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(
            json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
