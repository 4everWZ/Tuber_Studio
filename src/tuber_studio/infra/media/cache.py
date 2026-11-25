"""Owned media storage budgets; source assets and models are never cache targets."""

import asyncio
import json
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import Field

from ...core.models import StrictModel
from ...core.validation import linked

MIB = 1024 * 1024
MUSIC_MAX_MIB = 15 * 1024
VIDEO_MAX_MIB = 50 * 1024
ASSET_MAX_MIB = 8 * 1024
FINAL = re.compile(r"^[a-f0-9]{32,64}\.(wav|mp3|flac|json)$")
CLIP_FILE = re.compile(r"^[0-9a-f-]{36}\.mp4$", re.IGNORECASE)
ASSET_PREVIEW = re.compile(r"^.+\.(mp4|webm|wav|mp3|flac|png|jpg|jpeg|webp)$", re.IGNORECASE)
TRANSIENT = re.compile(r"^(\.upload-|\.download-|\.singing-)|^[a-f0-9]{32,64}\.part$")
CATEGORIES = ("video", "music", "speech", "browsing", "temporary", "assets")


class CachePolicy(StrictModel):
    video_limit_mib: int = Field(default=4096, ge=64, le=VIDEO_MAX_MIB)
    music_limit_mib: int = Field(default=MUSIC_MAX_MIB, ge=96, le=MUSIC_MAX_MIB)
    speech_limit_mib: int = Field(default=512, ge=16, le=MUSIC_MAX_MIB)
    browsing_limit_mib: int = Field(default=512, ge=16, le=ASSET_MAX_MIB)
    temporary_limit_mib: int = Field(default=1024, ge=16, le=ASSET_MAX_MIB)
    assets_limit_mib: int = Field(default=512, ge=16, le=ASSET_MAX_MIB)


class MediaCache:
    def __init__(
        self,
        settings,
        store,
        *,
        music_ids_provider=None,
        active_speech_provider=None,
        browsing_protected_provider=None,
        video_protected_provider=None,
        song_blob_resolver=None,
    ):
        self.store = store
        self.root = settings.runtime
        self._music_ids_provider = music_ids_provider or (lambda: set())
        self._active_speech_provider = active_speech_provider or (lambda: set())
        self._browsing_protected_provider = browsing_protected_provider or (lambda: set())
        self._video_protected_provider = video_protected_provider or (lambda: set())
        self._song_blob_resolver = song_blob_resolver
        self.locks = {name: asyncio.Lock() for name in CATEGORIES}
        self.leases = {}
        self.writing = set()
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS media_cache_access(path TEXT PRIMARY KEY,last_used REAL NOT NULL)"
        )

    def policy(self):
        old = self.store.get("song_requests", {}).get("cache_limit_mib", MUSIC_MAX_MIB)
        return CachePolicy.model_validate(
            {"music_limit_mib": old, **self.store.get("cache_policy", {})}
        ).model_dump()

    def music_ids(self):
        return set(self._music_ids_provider())

    def _assert_safe_dir(self, directory: Path) -> None:
        if linked(directory) or any(
            linked(parent)
            for parent in directory.parents
            if parent != self.root and parent.is_relative_to(self.root)
        ):
            raise ValueError("缓存目录不能使用链接")

    @staticmethod
    def _assert_entry_not_linked(entry) -> os.stat_result | None:
        if linked(entry):
            kind = "缓存目录不能使用链接" if entry.is_dir(follow_symlinks=False) else "缓存文件不能使用链接"
            raise ValueError(kind)
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == getattr(
            stat, "IO_REPARSE_TAG_MOUNT_POINT", -1
        ):
            raise ValueError("缓存目录不能使用链接" if stat.S_ISDIR(info.st_mode) else "缓存文件不能使用链接")
        return info

    @staticmethod
    def _directory_entries(directory, recursive):
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return
        with entries:
            for entry in entries:
                info = MediaCache._assert_entry_not_linked(entry)
                if info is None:
                    continue
                if stat.S_ISREG(info.st_mode):
                    yield entry, info
                elif recursive and stat.S_ISDIR(info.st_mode):
                    yield from MediaCache._directory_entries(entry.path, recursive=True)

    @staticmethod
    def _walk_job_dir(job_dir: Path) -> tuple[int, int, float, list[Path]]:
        size = files = 0
        latest_mtime = job_dir.stat(follow_symlinks=False).st_mtime
        children: list[Path] = []
        for entry, info in MediaCache._directory_entries(job_dir, recursive=True):
            files += 1
            size += info.st_size
            latest_mtime = max(latest_mtime, info.st_mtime)
            children.append(Path(entry.path))
        return size, files, latest_mtime, children

    def _prune_active_leases(self) -> None:
        now = time.monotonic()
        self.leases = {path: expires for path, expires in self.leases.items() if expires > now}

    def _scan_video(self):
        base = self.root / "video_renders"
        self._assert_safe_dir(base)
        try:
            entries = os.scandir(base)
        except FileNotFoundError:
            return
        pinned = {str(item) for item in self._video_protected_provider()}
        busy_video = "video" in self.writing
        with entries:
            for entry in entries:
                info = self._assert_entry_not_linked(entry)
                if info is None or not stat.S_ISDIR(info.st_mode):
                    continue
                job_dir = Path(entry.path)
                size, file_count, latest_mtime, children = self._walk_job_dir(job_dir)
                protected = (
                    entry.name in pinned
                    or busy_video
                    or entry.path in self.leases
                    or any(str(child) in self.leases for child in children)
                )
                yield {
                    "path": job_dir,
                    "bytes": size,
                    "files": file_count,
                    "mtime": latest_mtime,
                    "protected": protected,
                    "is_dir": True,
                }

    def _scan_files_category(self, category: str):
        roots = {
            "music": [("music", False, FINAL), ("online_music", False, FINAL), ("singing/cache", False, FINAL)],
            "speech": [("audio", False, FINAL)],
            "temporary": [("singing/output", True, None)],
            "browsing": [("browsing", False, FINAL), ("browsing/private-clips", False, CLIP_FILE)],
            "assets": [("private-call-previews", True, ASSET_PREVIEW)],
        }
        pinned = self.music_ids() if category == "music" else set()
        if category == "browsing":
            pinned = set(self._browsing_protected_provider())
        speech = set(self._active_speech_provider())
        busy_temp = category in {"temporary", "assets"} and category in self.writing
        for relative, recursive, pattern in roots[category]:
            directory = self.root / relative
            self._assert_safe_dir(directory)
            for entry, info in self._directory_entries(directory, recursive=recursive):
                transient = bool(TRANSIENT.match(entry.name))
                if pattern is not None and not (pattern.fullmatch(entry.name) or transient):
                    continue
                stem = os.path.splitext(entry.name)[0]
                protected = entry.path in self.leases or stem in pinned or entry.name in speech
                protected |= transient and category in self.writing
                protected |= busy_temp
                yield {
                    "path": Path(entry.path),
                    "bytes": info.st_size,
                    "files": 1,
                    "mtime": info.st_mtime,
                    "protected": protected,
                    "is_dir": False,
                }

    def _scan(self, category):
        """Fresh metadata/protection stream shared by summaries and LRU callers."""
        if category not in self.locks:
            raise ValueError("未知缓存类别")
        self._prune_active_leases()
        if category == "video":
            yield from self._scan_video()
        else:
            yield from self._scan_files_category(category)

    def _files(self, category):
        # Only eviction needs access times. Always read them anew, including
        # touches since the most recent status request; summaries never cache LRU.
        accessed = {
            row["path"]: row["last_used"] for row in self.store.all("SELECT * FROM media_cache_access")
        }
        return [
            {
                "path": row["path"],
                "bytes": row["bytes"],
                "files": row["files"],
                "used_at": accessed.get(os.path.relpath(row["path"], self.root), row["mtime"]),
                "protected": row["protected"],
                "is_dir": row["is_dir"],
            }
            for row in self._scan(category)
        ]

    def status(self):
        policy = self.policy()
        categories = {category: self._usage(category, policy) for category in self.locks}
        return {
            "settings": policy,
            "categories": categories,
            "summary": {
                "bytes": sum(item["bytes"] for item in categories.values()),
                "files": sum(item["files"] for item in categories.values()),
                "protected_bytes": sum(item["protected_bytes"] for item in categories.values()),
                "reclaimable_bytes": sum(item["reclaimable_bytes"] for item in categories.values()),
                "limit_bytes": sum(item["limit_bytes"] for item in categories.values()),
            },
        }

    def usage(self, category):
        return self._usage(category, self.policy())

    def _usage(self, category, policy):
        size = count = protected_size = 0
        for row in self._scan(category):
            count += row["files"]
            size += row["bytes"]
            if row["protected"]:
                protected_size += row["bytes"]
        return {
            "bytes": size,
            "files": count,
            "protected_bytes": protected_size,
            "reclaimable_bytes": size - protected_size,
            "limit_bytes": policy[category + "_limit_mib"] * MIB,
            "writing": category in self.writing,
        }

    def touch(self, path, *, seconds=0):
        path = Path(path)
        if not path.resolve().is_relative_to(self.root) or linked(path):
            raise ValueError("缓存路径不在运行目录内")
        if path.is_file() or path.is_dir():
            self.store.execute(
                "INSERT INTO media_cache_access VALUES(?,?) ON CONFLICT(path) DO UPDATE SET last_used=excluded.last_used",
                (str(path.relative_to(self.root)), time.time()),
            )
        if seconds:
            self.leases[str(path)] = time.monotonic() + seconds

    def release_uploads(self, song):
        if self._song_blob_resolver is None:
            return
        for field in ("audio", "vocal", "accompaniment"):
            if song.get(field):
                self.leases.pop(str(self._song_blob_resolver(song[field])), None)

    def _prune_indexes(self, removed):
        identities = {path.stem for path in removed}
        index = self.store.get("online_music_cache", {})
        self.store.set(
            "online_music_cache", {key: row for key, row in index.items() if row["audio"] not in identities}
        )
        directory = self.root / "singing/cache"
        if directory.is_dir() and not linked(directory):
            for path in directory.glob("*.json"):
                if linked(path):
                    continue
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                    if row.get("audio") in identities or row.get("vocal") in identities:
                        path.unlink(missing_ok=True)
                except (ValueError, OSError, AttributeError):
                    continue

    def _remove_dir_tree(self, target: Path) -> tuple[int, bool]:
        """Remove a cached job directory atomically without following links."""
        removed_files = 0
        failed = False
        for entry, _info in self._directory_entries(target, recursive=True):
            child = Path(entry.path)
            try:
                child.unlink(missing_ok=True)
                removed_files += 1
            except PermissionError:
                failed = True
        for root_dir, dirnames, _ in os.walk(target, topdown=False, followlinks=False):
            cur = Path(root_dir)
            if linked(cur):
                raise ValueError("缓存目录不能使用链接")
            for name in dirnames:
                sub = cur / name
                if linked(sub):
                    raise ValueError("缓存目录不能使用链接")
                try:
                    sub.rmdir()
                except OSError:
                    failed = True
        try:
            target.rmdir()
        except OSError:
            failed = True
        return removed_files, failed

    def _prune_empty_subdirs(self, base: Path) -> None:
        if not base.is_dir() or linked(base):
            return
        for root_dir, _dirnames, _ in os.walk(base, topdown=False, followlinks=False):
            cur = Path(root_dir)
            if cur == base or linked(cur):
                continue
            try:
                cur.rmdir()
            except OSError:
                continue

    def enforce(self, category, *, reserve=0, clear=False, policy=None):
        policy = policy or self.policy()
        files = self._files(category)
        size = sum(row["bytes"] for row in files)
        limit = 0 if clear else policy[category + "_limit_mib"] * MIB - reserve
        candidates = sorted((row for row in files if not row["protected"]), key=lambda row: row["used_at"])
        reclaimable = sum(row["bytes"] for row in candidates)
        if not clear and size - reclaimable > limit:
            raise ValueError(
                "缓存空间不足：曲库、当前／待播、进行中任务或受保护素材占用超出上限，请先移除不需要的内容或增大上限"
            )
        removed, removed_files, freed = [], 0, 0
        for row in candidates:
            if size <= limit and not clear:
                break
            if row["is_dir"]:
                deleted_count, failed = self._remove_dir_tree(row["path"])
                removed_files += deleted_count
                if failed:
                    continue
            else:
                try:
                    row["path"].unlink(missing_ok=True)
                except PermissionError:
                    continue
                removed_files += 1
            removed.append(row["path"])
            self.store.execute(
                "DELETE FROM media_cache_access WHERE path=?", (str(row["path"].relative_to(self.root)),)
            )
            freed += row["bytes"]
            size -= row["bytes"]
        if category == "music":
            self._prune_indexes(removed)
        elif category in {"temporary", "assets"} and removed:
            for sub in (["singing/output"] if category == "temporary" else ["private-call-previews"]):
                self._prune_empty_subdirs(self.root / sub)
        if not clear and size > limit:
            raise ValueError("部分缓存正在使用，未能释放足够空间")
        return {"removed_files": removed_files, "freed_bytes": freed, "protected_bytes": size}

    @asynccontextmanager
    async def write(self, category, maximum):
        async with self.locks[category]:
            files = self._files(category)
            protected = sum(row["bytes"] for row in files if row["protected"])
            allowed = min(maximum, self.policy()[category + "_limit_mib"] * MIB - protected)
            if allowed <= 0:
                raise ValueError("缓存空间不足，请清理可回收内容或调整容量")
            self.enforce(category, reserve=allowed)
            self.writing.add(category)
            try:
                yield allowed
            finally:
                self.writing.discard(category)

    async def configure(self, value):
        merged = {**self.policy(), **(value if isinstance(value, dict) else {})}
        policy = CachePolicy.model_validate(merged).model_dump()
        if any(lock.locked() for lock in self.locks.values()):
            raise ValueError("正在写入缓存，请稍后保存容量设置")
        for category in self.locks:
            protected = sum(row["bytes"] for row in self._scan(category) if row["protected"])
            if protected > policy[category + "_limit_mib"] * MIB:
                raise ValueError("新上限低于受保护素材的占用，请先移除相应曲库条目或等待任务完成")
        for category in self.locks:
            self.enforce(category, policy=policy)
        self.store.set("cache_policy", policy)
        return self.status()

    async def clear(self, category):
        async with self.locks.get(category, asyncio.Lock()):
            return self.enforce(category, clear=True)

    async def clear_all(self):
        removed_files = freed_bytes = protected_bytes = 0
        per_category = {}
        for category in self.locks:
            result = await self.clear(category)
            per_category[category] = result
            removed_files += result["removed_files"]
            freed_bytes += result["freed_bytes"]
            protected_bytes += result["protected_bytes"]
        return {
            "removed_files": removed_files,
            "freed_bytes": freed_bytes,
            "protected_bytes": protected_bytes,
            "categories": per_category,
        }
