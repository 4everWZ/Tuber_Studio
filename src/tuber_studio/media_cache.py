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

from .live_media import StrictModel

MIB = 1024 * 1024
MUSIC_MAX_MIB = 15 * 1024
FINAL = re.compile(r"^[a-f0-9]{32,64}\.(wav|mp3|flac|json)$")
TRANSIENT = re.compile(r"^(\.upload-|\.download-|\.singing-)|^[a-f0-9]{32,64}\.part$")


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


class CachePolicy(StrictModel):
    music_limit_mib: int = Field(default=MUSIC_MAX_MIB, ge=96, le=MUSIC_MAX_MIB)
    speech_limit_mib: int = Field(default=512, ge=16, le=MUSIC_MAX_MIB)
    temporary_limit_mib: int = Field(default=1024, ge=16, le=4096)
    browsing_limit_mib: int = Field(default=512, ge=16, le=4096)


class MediaCache:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.root = runtime.settings.runtime
        self.locks = {name: asyncio.Lock() for name in ("music", "speech", "temporary", "browsing")}
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
        identities = {
            song[field]
            for song in self.runtime.media.songs()
            for field in ("audio", "vocal", "accompaniment")
            if song.get(field)
        }
        active = ([self.runtime.current_id] if self.runtime.current_id else []) + self.runtime.queue
        for identity in active:
            job = self.store.job(identity)
            if job and job.get("media"):
                song = json.loads(job["media"])["song"]
                identities.update(
                    song[field] for field in ("audio", "vocal", "accompaniment") if song.get(field)
                )
        online = getattr(self.runtime, "online_songs", None)
        for task in online.tasks.values() if online else ():
            if task.done() and not task.cancelled() and task.exception() is None:
                identities.add(task.result()["song"]["audio"])
        return identities

    @staticmethod
    def _directory_entries(directory, recursive):
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return
        with entries:
            for entry in entries:
                if linked(entry):
                    raise ValueError("缓存文件不能使用链接")
                if entry.is_file(follow_symlinks=False):
                    yield entry
                elif recursive and entry.is_dir(follow_symlinks=False):
                    yield from MediaCache._directory_entries(entry.path, recursive=True)

    def _scan(self, category):
        """Fresh metadata/protection stream shared by summaries and LRU callers."""
        paths = {
            "music": ["music", "online_music", "singing/cache"],
            "speech": ["audio"],
            "temporary": ["singing/output"],
            "browsing": ["browsing"],
        }
        if category not in paths:
            raise ValueError("未知缓存类别")
        pinned = self.music_ids() if category == "music" else set()
        if category == "browsing":
            pinned = self.runtime.browsing_repository.protected_materials()
        speech = set()
        for identity in ([self.runtime.current_id] if self.runtime.current_id else []) + self.runtime.queue:
            job = self.store.job(identity)
            if job and job.get("audio_ref"):
                speech.add(job["audio_ref"].rsplit("/", 1)[-1])
        now = time.monotonic()
        self.leases = {path: expires for path, expires in self.leases.items() if expires > now}
        busy_temp = "temporary" in self.writing
        for relative in paths[category]:
            directory = self.root / relative
            if linked(directory) or any(
                linked(parent)
                for parent in directory.parents
                if parent != self.root and parent.is_relative_to(self.root)
            ):
                raise ValueError("缓存目录不能使用链接")
            for entry in self._directory_entries(directory, recursive=category == "temporary"):
                if category != "temporary" and not (
                    FINAL.fullmatch(entry.name) or TRANSIENT.match(entry.name)
                ):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == getattr(
                    stat, "IO_REPARSE_TAG_MOUNT_POINT", -1
                ):
                    raise ValueError("缓存文件不能使用链接")
                if not stat.S_ISREG(info.st_mode):
                    continue
                transient = bool(TRANSIENT.match(entry.name))
                stem = os.path.splitext(entry.name)[0]
                protected = entry.path in self.leases or stem in pinned or entry.name in speech
                protected |= transient and category in self.writing
                protected |= category == "temporary" and busy_temp
                yield entry.path, info, protected

    def _files(self, category):
        # Only eviction needs access times. Always read them anew, including
        # touches since the most recent status request; summaries never cache LRU.
        accessed = {
            row["path"]: row["last_used"] for row in self.store.all("SELECT * FROM media_cache_access")
        }
        return [
            {
                "path": Path(path),
                "bytes": info.st_size,
                "used_at": accessed.get(os.path.relpath(path, self.root), info.st_mtime),
                "protected": protected,
            }
            for path, info, protected in self._scan(category)
        ]

    def status(self):
        policy = self.policy()
        return {
            "settings": policy,
            "categories": {category: self._usage(category, policy) for category in self.locks},
        }

    def usage(self, category):
        return self._usage(category, self.policy())

    def _usage(self, category, policy):
        size = count = protected_size = 0
        for _path, info, protected in self._scan(category):
            count += 1
            size += info.st_size
            if protected:
                protected_size += info.st_size
        return {
            "bytes": size,
            "files": count,
            "protected_bytes": protected_size,
            "reclaimable_bytes": size - protected_size,
            "limit_bytes": policy[category + "_limit_mib"] * MIB,
            "writing": category in self.writing,
        }

    def touch(self, path, *, seconds=0):
        if not path.resolve().is_relative_to(self.root) or linked(path):
            raise ValueError("缓存路径不在运行目录内")
        if path.is_file():
            self.store.execute(
                "INSERT INTO media_cache_access VALUES(?,?) ON CONFLICT(path) DO UPDATE SET last_used=excluded.last_used",
                (str(path.relative_to(self.root)), time.time()),
            )
        if seconds:
            self.leases[str(path)] = time.monotonic() + seconds

    def release_uploads(self, song):
        for field in ("audio", "vocal", "accompaniment"):
            if song.get(field):
                self.leases.pop(str(self.runtime.media.blob(song[field])), None)

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

    def enforce(self, category, *, reserve=0, clear=False, policy=None):
        policy = policy or self.policy()
        files = self._files(category)
        size = sum(row["bytes"] for row in files)
        limit = 0 if clear else policy[category + "_limit_mib"] * MIB - reserve
        candidates = sorted((row for row in files if not row["protected"]), key=lambda row: row["used_at"])
        reclaimable = sum(row["bytes"] for row in candidates)
        if not clear and size - reclaimable > limit:
            raise ValueError(
                "缓存空间不足：曲库、当前／待播和新上传素材受保护，请先移除不需要的曲库条目或增大上限"
            )
        removed, freed = [], 0
        for row in candidates:
            if size <= limit and not clear:
                break
            try:
                row["path"].unlink(missing_ok=True)
            except PermissionError:
                continue
            removed.append(row["path"])
            self.store.execute(
                "DELETE FROM media_cache_access WHERE path=?", (str(row["path"].relative_to(self.root)),)
            )
            freed += row["bytes"]
            size -= row["bytes"]
        if category == "music":
            self._prune_indexes(removed)
        if not clear and size > limit:
            raise ValueError("部分缓存正在使用，未能释放足够空间")
        return {"removed_files": len(removed), "freed_bytes": freed, "protected_bytes": size}

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
        policy = CachePolicy.model_validate(value).model_dump()
        if any(lock.locked() for lock in self.locks.values()):
            raise ValueError("正在写入缓存，请稍后保存容量设置")
        for category in self.locks:
            protected = sum(row["bytes"] for row in self._files(category) if row["protected"])
            if protected > policy[category + "_limit_mib"] * MIB:
                raise ValueError("新上限低于受保护素材的占用，请先移除相应曲库条目或等待任务完成")
        for category in self.locks:
            self.enforce(category, policy=policy)
        self.store.set("cache_policy", policy)
        return self.status()

    async def clear(self, category):
        async with self.locks.get(category, asyncio.Lock()):
            return self.enforce(category, clear=True)
