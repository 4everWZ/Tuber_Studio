"""Tests for MediaCache meme_inbox category, unlimited 0 policy, item cap, and eviction callback."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tuber_studio.infra.media.cache import MIB, CachePolicy


def _put(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


def test_cache_policy_allows_zero_only_for_meme_inbox():
    default_policy = CachePolicy()
    assert default_policy.meme_inbox_limit_mib == 512
    assert default_policy.meme_inbox_max_items == 200

    unlimited = CachePolicy(meme_inbox_limit_mib=0, meme_inbox_max_items=0)
    assert unlimited.meme_inbox_limit_mib == 0 and unlimited.meme_inbox_max_items == 0

    for field in ("video_limit_mib", "music_limit_mib", "speech_limit_mib", "browsing_limit_mib", "temporary_limit_mib", "assets_limit_mib"):
        with pytest.raises(ValueError):
            CachePolicy(**{field: 0})


async def test_meme_inbox_scan_ignores_pending_temp_and_enforces_byte_and_item_caps(runtime):
    cache = runtime.cache
    inbox_dir = runtime.settings.runtime / "meme_inbox"
    evicted_batches: list[list[Path]] = []
    cache.meme_inbox_evicted_callback = lambda removed: evicted_batches.append(list(removed))

    f1 = _put(inbox_dir, f"{'1' * 64}.png", 4096)
    f2 = _put(inbox_dir, f"{'2' * 64}.gif", 4096)
    f3 = _put(inbox_dir, f"{'3' * 64}.webp", 4096)
    pending = _put(inbox_dir, ".pending-abc123.png", 8192)
    os.utime(f1, (10, 10))
    os.utime(f2, (20, 20))
    os.utime(f3, (30, 30))

    usage = cache.usage("meme_inbox")
    assert usage["files"] == 3
    assert usage["bytes"] == 3 * 4096
    assert usage["unlimited"] is False
    assert usage["max_items"] == 200
    assert pending.exists()

    # Enforce item cap: keep newest 2 (f2, f3), evict f1 and invoke callback
    await cache.configure({"meme_inbox_max_items": 2})
    assert not f1.exists() and f2.exists() and f3.exists() and pending.exists()
    assert evicted_batches == [[f1]]

    # Enforce byte cap: 2 * 6 MiB = 12 MiB against 8 MiB limit evicts older one
    big1 = _put(inbox_dir, f"{'4' * 64}.jpg", 6 * MIB)
    big2 = _put(inbox_dir, f"{'5' * 64}.jpg", 6 * MIB)
    os.utime(big1, (100, 100))
    os.utime(big2, (200, 200))
    await cache.configure({"meme_inbox_limit_mib": 8, "meme_inbox_max_items": 10})
    assert not f2.exists() and not f3.exists() and not big1.exists() and big2.exists()
    assert big1 in evicted_batches[-1]


async def test_meme_inbox_unlimited_zero_and_clear_callback(runtime):
    cache = runtime.cache
    inbox_dir = runtime.settings.runtime / "meme_inbox"
    evicted: list[Path] = []
    cache.meme_inbox_evicted_callback = evicted.extend

    f1 = _put(inbox_dir, f"{'a' * 64}.png", 2 * MIB)
    f2 = _put(inbox_dir, f"{'b' * 64}.gif", 2 * MIB)
    os.utime(f1, (10, 10))
    os.utime(f2, (20, 20))

    # Set both byte limit and item limit to 0 (unlimited)
    status = await cache.configure({"meme_inbox_limit_mib": 0, "meme_inbox_max_items": 0})
    inbox_status = status["categories"]["meme_inbox"]
    assert inbox_status["limit_bytes"] == 0
    assert inbox_status["unlimited"] is True
    assert inbox_status["max_items"] == 0
    assert f1.exists() and f2.exists()

    # Protected file still works when limit_mib=0
    cache.touch(f1, seconds=120)
    async with cache.write("meme_inbox", 1024) as allowed:
        assert allowed == 1024
    assert f1.exists() and f2.exists()

    # Clear evicts unprotected f2 and triggers callback, while keeping leased f1
    res = await cache.clear("meme_inbox")
    assert res["removed_files"] == 1
    assert f1.exists() and not f2.exists()
    assert evicted == [f2]
