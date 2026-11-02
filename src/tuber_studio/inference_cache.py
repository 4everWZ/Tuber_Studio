"""Bounded cleanup of the node's dedicated, rebuildable disk cache."""

import os
import stat
from pathlib import Path

MAX_ENTRIES = 4096
MAX_DETAILS = 64


def _directory_link(path):
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink, value.st_mode


def scan_cache(root: Path, *, clear=False, limit=MAX_ENTRIES):
    directory = root.resolve() / "runtime/inference-cache"
    result = {
        "path": str(directory),
        "scanned_entries": 0,
        "complete": True,
        "known_top_level_bytes": 0,
        "known_reclaimable_bytes": 0,
        "file_count": 0,
        "removed_files": 0,
        "removed_bytes": 0,
        "protected_count": 0,
        "protected": [],
        "error_count": 0,
        "errors": [],
    }
    unknown_children = False

    def detail(kind, name, reason):
        result[kind + "_count"] += 1
        if len(result[kind if kind == "protected" else "errors"]) < MAX_DETAILS:
            result[kind if kind == "protected" else "errors"].append({"name": name, "reason": reason})

    def finish():
        usage_complete = result["complete"] and not unknown_children and not result["error_count"]
        reclaimable_complete = result["complete"] and not result["error_count"]
        result["usage_complete"] = usage_complete
        result["reclaimable_complete"] = reclaimable_complete
        result["used_bytes"] = result["known_top_level_bytes"] if usage_complete else None
        result["reclaimable_bytes"] = result["known_reclaimable_bytes"] if reclaimable_complete else None
        return result

    if _directory_link(directory.parent) or _directory_link(directory):
        detail("protected", ".", "Cache directory links are protected")
        result["complete"] = False
        return finish()
    if not directory.exists():
        return finish()
    if not directory.is_dir():
        detail("protected", ".", "Dedicated cache path is not a directory")
        result["complete"] = False
        return finish()
    descriptor = None
    try:
        parent = directory.stat()
        # Linux uses a directory handle for both enumeration and unlink. Changing
        # a path into a symlink cannot redirect deletion into model/business data.
        handles = os.scandir in os.supports_fd and os.unlink in os.supports_dir_fd
        if handles:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        with os.scandir(descriptor if descriptor is not None else directory) as entries:
            for entry in entries:
                if result["scanned_entries"] >= limit:
                    result["complete"] = False
                    break
                result["scanned_entries"] += 1
                try:
                    # Windows DirEntry.stat may report st_nlink=0 from cached
                    # directory metadata. Query the file before classifying links.
                    info = (
                        os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                        if descriptor is not None
                        else (directory / entry.name).lstat()
                    )
                    if not stat.S_ISREG(info.st_mode):
                        unknown_children |= stat.S_ISDIR(info.st_mode)
                        detail(
                            "protected", entry.name, "Directories, links and special entries are protected"
                        )
                        continue
                    result["file_count"] += 1
                    result["known_top_level_bytes"] += info.st_size
                    if info.st_nlink != 1:
                        detail("protected", entry.name, "Shared hard links are protected")
                        continue
                    result["known_reclaimable_bytes"] += info.st_size
                    if not clear:
                        continue
                    if descriptor is not None:
                        current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                        if _identity(current) != _identity(info):
                            detail("protected", entry.name, "Cache file changed during cleanup")
                            continue
                        os.unlink(entry.name, dir_fd=descriptor)
                    else:
                        if (
                            _directory_link(directory)
                            or _identity(directory.stat())[:2] != _identity(parent)[:2]
                        ):
                            detail("protected", entry.name, "Cache directory changed during cleanup")
                            result["complete"] = False
                            break
                        path = directory / entry.name
                        if _identity(path.lstat()) != _identity(info):
                            detail("protected", entry.name, "Cache file changed during cleanup")
                            continue
                        path.unlink()
                    result["removed_files"] += 1
                    result["removed_bytes"] += info.st_size
                except OSError as error:
                    detail("error", entry.name, type(error).__name__)
    except OSError as error:
        detail("error", ".", type(error).__name__)
        result["complete"] = False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return finish()


def cache_summary(root):
    scanned = scan_cache(root)
    return {
        "kind": "inference-cache",
        "path": scanned["path"],
        "used_bytes": scanned["used_bytes"],
        "reclaimable_bytes": scanned["reclaimable_bytes"],
        "known_top_level_bytes": scanned["known_top_level_bytes"],
        "file_count": scanned["file_count"],
        "usage_complete": scanned["usage_complete"],
        "reclaimable_complete": scanned["reclaimable_complete"],
        "protected": False,
        "protected_entries": scanned["protected_count"],
        "scanned_entries": scanned["scanned_entries"],
        "error_count": scanned["error_count"],
        "usage_scope": "logical_top_level_files",
    }
