"""One business writer per data directory, with explicit migration retirement."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path

AUTHORITY_FILE = "business-authority.json"
LOCK_FILE = ".business-writer.lock"


class BusinessAuthorityError(RuntimeError):
    pass


def _regular(path: Path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise BusinessAuthorityError("Business authority paths cannot use links")
    if path.exists() and not path.is_file():
        raise BusinessAuthorityError("Business authority metadata must be a regular file")


def read_business_authority(runtime: Path) -> dict | None:
    path = Path(runtime) / AUTHORITY_FILE
    _regular(path)
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise BusinessAuthorityError("Business authority metadata is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BusinessAuthorityError("Cannot read business authority metadata") from exc
    if (
        not isinstance(value, dict)
        or value.get("format") != 1
        or value.get("state") not in {"active", "staged", "retired"}
        or not isinstance(value.get("writer_host"), str)
        or not value["writer_host"]
    ):
        raise BusinessAuthorityError("Invalid business authority metadata")
    return value


def assert_business_writer(runtime: Path):
    value = read_business_authority(runtime)
    # An existing local installation remains usable until explicitly migrated.
    if value is None:
        return
    if value["state"] != "active":
        raise BusinessAuthorityError(
            "Business data is staged or retired; activate the authoritative copy first"
        )
    if value["writer_host"].casefold() != socket.gethostname().casefold():
        raise BusinessAuthorityError("This business directory belongs to a different writer host")


@contextmanager
def acquire_business_writer(runtime: Path, *, check_authority: bool = True):
    """Hold an OS lock until the application (or maintenance operation) exits."""
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    if runtime.is_symlink() or getattr(runtime, "is_junction", lambda: False)():
        raise BusinessAuthorityError("Business data directory cannot use links")
    path = runtime / LOCK_FILE
    _regular(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BusinessAuthorityError("Business data already has a running writer; stop it first") from exc
        locked = True
        if check_authority:
            assert_business_writer(runtime)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def set_business_authority(
    runtime: Path,
    state: str,
    *,
    migration_id: str,
    writer_host: str | None = None,
    destination: str | None = None,
) -> dict:
    if state not in {"active", "staged", "retired"} or not migration_id:
        raise ValueError("Authority state and migration identity are required")
    runtime = Path(runtime)
    with acquire_business_writer(runtime, check_authority=False):
        current = read_business_authority(runtime)
        value = {
            "format": 1,
            "state": state,
            "writer_host": writer_host or socket.gethostname(),
            "migration_id": migration_id,
        }
        if destination:
            value["destination"] = destination
        if current:
            value["previous_state"] = current["state"]
        path = runtime / AUTHORITY_FILE
        _regular(path)
        descriptor, temporary = tempfile.mkstemp(prefix=".business-authority-", dir=runtime)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return value
