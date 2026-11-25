import asyncio
import base64
import hashlib
import io
import json
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from tuber_studio.app import create_app
from tuber_studio.core.models import StrictModel
from tuber_studio.infra.media.cache import MediaCache
from tuber_studio.operations.channels.comments.materials import prepare_comment_materials
from tuber_studio.operations.discovery.comments.reader import evaluate_comment_visit
from tuber_studio.operations.inbox.comment_events import enqueue_own_comments
from tuber_studio.operations.kernel.repository import BrowsingRepository
from tuber_studio.persona.assets.meme_harvester import (
    MAX_QUEUE_ROWS,
    MemeHarvester,
    passes_comment_coarse_filter,
)
from tuber_studio.persona.assets.memes import MemeCatalog


def _make_png(width=128, height=128, color=(20, 120, 220), *, draw_box=False, pattern: int = 0) -> bytes:
    img = Image.new("RGB", (width, height), color)
    if pattern:
        draw = ImageDraw.Draw(img)
        digest = hashlib.sha256(f"pattern-{pattern}".encode()).digest()
        cell_w = max(1, width // 8)
        cell_h = max(1, height // 8)
        for row in range(8):
            for col in range(8):
                bit = (digest[(row * 8 + col) // 8] >> ((row * 8 + col) % 8)) & 1
                shade = 245 if bit else 20
                draw.rectangle(
                    [col * cell_w, row * cell_h, (col + 1) * cell_w - 1, (row + 1) * cell_h - 1],
                    fill=(shade, (shade + col * 19) % 256, (shade + row * 23) % 256),
                )
    elif draw_box:
        draw = ImageDraw.Draw(img)
        draw.rectangle([16, 16, width - 16, height - 16], fill=(240, 240, 240))
        draw.ellipse([32, 32, width - 32, height - 32], fill=(40, 80, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gif(width=128, height=128, frames=3) -> bytes:
    imgs = []
    for idx in range(frames):
        im = Image.new("RGB", (width, height), (30 + idx * 40, 100, 200))
        draw = ImageDraw.Draw(im)
        draw.rectangle([20 + idx * 5, 20, width - 20, height - 20], fill=(250, 220, 120))
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=90, loop=0)
    return buf.getvalue()


class FakeInspector:
    """Deterministic inspector using PIL so tests don't require external ffprobe binary."""

    async def inspect(self, content, *, mime=None):
        if not isinstance(content, bytes) or not content:
            raise ValueError("empty")
        with Image.open(io.BytesIO(content)) as img:
            fmt = (img.format or "").upper()
            ext_map = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "GIF": ("image/gif", "gif"), "WEBP": ("image/webp", "webp")}
            if fmt not in ext_map:
                raise ValueError("unsupported")
            actual_mime, ext = ext_map[fmt]
            if mime is not None and mime != actual_mime:
                raise ValueError("MIME mismatch")
            frames = getattr(img, "n_frames", 1)
            return {
                "mime_type": actual_mime,
                "extension": ext,
                "width": img.width,
                "height": img.height,
                "frames": frames,
                "animated": frames > 1,
            }


class FakeVision:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def capabilities(self):
        return {"images": True, "max_frames": 3, "max_pixels": 1024 * 1024, "max_bytes": 1024 * 1024}

    async def classify_meme_image(self, frames, *, context_hint=""):
        self.calls.append((frames, context_hint))
        if self.error is not None:
            raise self.error
        if callable(self.result):
            return self.result(frames, context_hint)
        return self.result


def _setup_harvester(settings, store, *, vision=None, browsing=None, http_client_factory=None):
    catalog = MemeCatalog(settings, store)
    catalog.inspector = FakeInspector()
    harvester = MemeHarvester(
        settings,
        store,
        catalog=catalog,
        cache=None,
        runtime=None,
        visual_provider=vision,
        browsing=browsing,
        http_client_factory=http_client_factory,
    )
    cache = MediaCache(settings, store, meme_inbox_evicted_callback=harvester.on_cache_evicted)
    harvester.cache = cache
    return catalog, cache, harvester


async def test_stage1_dimension_filter_dhash_dedup_and_rejected_blacklist(settings, store):
    vision = FakeVision(
        {
            "is_whale_aqua": True,
            "clean_ok": True,
            "confidence": 0.9,
            "title": "大肥鱼开心",
            "emotions": ["cheerful"],
            "tags": ["大肥鱼"],
            "description": "蓝鲸鱼表情",
        }
    )
    catalog, _, harvester = _setup_harvester(settings, store, vision=vision)

    # Too small / bad aspect ratio rejected
    tiny = _make_png(48, 48)
    r_tiny = await harvester.screen_stage1(tiny)
    assert not r_tiny["accepted"] and r_tiny["reason"] == "dimension_filtered"

    banner = _make_png(640, 100)
    r_banner = await harvester.screen_stage1(banner)
    assert not r_banner["accepted"] and r_banner["reason"] == "dimension_filtered"

    # Register an image in catalog, then feed a visually near-identical PNG (1px tweak) -> dHash dedup catches it
    img_a = _make_png(128, 128, draw_box=True)
    with Image.open(io.BytesIO(img_a)) as im:
        im2 = im.copy()
        im2.putpixel((0, 0), (21, 121, 221))
        buf = io.BytesIO()
        im2.save(buf, format="PNG")
        img_a_near = buf.getvalue()
    await catalog.register(
        "whale",
        img_a,
        title="原图",
        emotions=["idle"],
        source={"kind": "user_upload", "author": "op"},
        license={"name": "local"},
    )
    r_dup = await harvester.screen_stage1(img_a_near)
    assert not r_dup["accepted"] and r_dup["reason"] == "duplicate_catalog"

    # Put a new image into inbox, then check inbox dHash dedup and reject -> blacklist
    img_b = _make_png(160, 160, color=(200, 50, 50), draw_box=True)
    res_b = await harvester.process_candidate_bytes(
        img_b,
        channel="bilibili_creator",
        source={"kind": "bilibili_creator", "author": "up", "author_id": "1001", "url": "https://t.bilibili.com/1"},
        license={"name": "creator"},
    )
    assert res_b["outcome"] == "inbox"
    item_id = res_b["item"]["item_id"]

    r_inbox_dup = await harvester.screen_stage1(img_b)
    assert not r_inbox_dup["accepted"] and r_inbox_dup["reason"] == "duplicate_inbox"

    harvester.reject_inbox_item(item_id)
    r_rejected = await harvester.screen_stage1(img_b)
    assert not r_rejected["accepted"] and r_rejected["reason"] == "rejected_blacklist"


async def test_stage2_outcomes_and_vision_unavailable_fallbacks(settings, store):
    # 1. Explicit vision reject -> 7-day meme_discard_cache
    bad_vision = FakeVision(
        {
            "is_whale_aqua": False,
            "clean_ok": True,
            "confidence": 0.2,
            "title": "无关图",
            "emotions": ["idle"],
            "tags": [],
            "description": "",
        }
    )
    _, _, harvester = _setup_harvester(settings, store, vision=bad_vision)
    gif_bytes = _make_gif()
    res1 = await harvester.process_candidate_bytes(
        gif_bytes,
        channel="bilibili_comment",
        source={"kind": "bilibili_comment", "author": "u1", "author_id": "42", "url": "https://www.bilibili.com/video/BV1xx411c7mD"},
        license={"name": "comment"},
        usage_scope="interaction_only",
    )
    assert res1["outcome"] == "discarded"
    assert store.one("SELECT sha256 FROM meme_discard_cache") is not None
    assert not list(harvester.inbox_dir.glob("*.gif"))
    # Re-screening hits discard cache
    r_cache = await harvester.screen_stage1(gif_bytes)
    assert not r_cache["accepted"] and r_cache["reason"] == "discard_cache"

    # 2. Vision unavailable + remote_feed auto_approve=True (no manifest) -> filename fallback into inbox (NEVER blind register!)
    err_vision = FakeVision(error=RuntimeError("vision offline"))
    catalog2, _, harvester2 = _setup_harvester(settings, store, vision=err_vision)
    png_bytes = _make_png(140, 140, color=(10, 180, 140), draw_box=True)
    res2 = await harvester2.process_candidate_bytes(
        png_bytes,
        channel="remote_feed",
        source={"kind": "remote_feed", "author": "gh", "url": "https://github.com/a/b"},
        license={"name": "MIT"},
        auto_approve=True,
        filename_hint="aqua_happy-wink.png",
    )
    assert res2["outcome"] == "inbox_fallback"
    assert res2["item"]["ai_tagged"] is False
    assert res2["item"]["emotions"] == ["idle"]
    assert res2["item"]["title"] == "aqua happy wink"
    assert catalog2.list("whale", include_disabled=True) == []

    # 3. Vision unavailable + auto_approve=False -> pending_vision queue + retry on drain_queue
    png_c = _make_png(150, 150, color=(220, 180, 20), pattern=1)
    res3 = await harvester2.process_candidate_bytes(
        png_c,
        channel="bilibili_comment",
        source={"kind": "bilibili_comment", "author": "u2", "author_id": "99", "url": "https://www.bilibili.com/video/BV1xx411c7mD"},
        license={"name": "comment"},
        auto_approve=False,
        usage_scope="interaction_only",
        image_url="https://i0.hdslb.com/bfs/test_pending.png",
    )
    assert res3["outcome"] == "pending_vision"
    qrow = store.one("SELECT * FROM meme_harvest_queue WHERE queue_id=?", (res3["queue_id"],))
    assert qrow["status"] == "pending_vision" and qrow["cached_blob"].startswith(".pending-")

    # Restore vision -> drain_queue completes it into meme_inbox
    harvester2._visual_provider = FakeVision(
        {
            "is_whale_aqua": True,
            "clean_ok": True,
            "confidence": 0.88,
            "title": "金黄大肥鱼",
            "emotions": ["excited"],
            "tags": ["大肥鱼"],
            "description": "可爱鲸鱼图",
        }
    )
    drain = await harvester2.drain_queue()
    assert drain["accepted"] == 1
    assert store.one("SELECT * FROM meme_harvest_queue WHERE queue_id=?", (res3["queue_id"],)) is None
    inbox_items = harvester2.list_inbox()["items"]
    assert any(it["title"] == "金黄大肥鱼" and it["usage_scope"] == "interaction_only" for it in inbox_items)


def test_comment_coarse_filter():
    att = [{"kind": "image", "url": "https://i0.hdslb.com/bfs/a.png"}]
    assert passes_comment_coarse_filter("大肥鱼好可爱", att) is True
    assert passes_comment_coarse_filter("", att) is True
    assert passes_comment_coarse_filter("这个表情太好笑了", att) is True
    assert passes_comment_coarse_filter("好可爱的图", []) is False
    assert passes_comment_coarse_filter("加微123456 扫码领福利", att) is False
    assert passes_comment_coarse_filter("查看我的二维码进群", att) is False
    long_complaint = "今天天气真糟糕啊，我写了一堆完全无关的抱怨和长篇吐槽。" * 25
    assert len(long_complaint) > 400
    assert passes_comment_coarse_filter(long_complaint, att) is False
    assert passes_comment_coarse_filter(long_complaint + " 顺便发个大肥鱼表情包", att) is True


async def test_channel_a_ssrf_etag_manifest_and_github_dir(settings, store, monkeypatch):
    gif_bytes = _make_gif()
    png_bytes = _make_png(140, 140, color=(90, 140, 210), draw_box=True)
    etag_calls = 0

    async def fake_public_dns(url):
        host = url.host
        if host == "private.example.org":
            raise ValueError("网页解析到了非公网地址")
        return host, "93.184.215.14"

    monkeypatch.setattr("tuber_studio.persona.assets.meme_harvester.public_https_address", fake_public_dns)

    def handler(request: httpx.Request):
        nonlocal etag_calls
        host = request.headers.get("host", "")
        path = request.url.path
        if host == "cdn.example.org" and path == "/manifest.json":
            etag_calls += 1
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304, headers={"ETag": '"v1"'})
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                json={
                    "entries": [
                        {
                            "title": "测试摇摆",
                            "emotions": ["cheerful"],
                            "tags": ["跳舞"],
                            "path": "/images/dance.gif",
                            "author": "FeedAuthor",
                        }
                    ]
                },
            )
        if host == "cdn.example.org" and path == "/images/dance.gif":
            return httpx.Response(200, content=gif_bytes)
        if host == "redir.example.org":
            return httpx.Response(302, headers={"Location": "https://private.example.org/evil.json"})
        if host == "api.github.com" and path == "/repos/org/repo/contents/memes":
            return httpx.Response(
                200,
                headers={"ETag": '"gh1"'},
                json=[
                    {
                        "type": "file",
                        "name": "aqua_wave.png",
                        "sha": "abc123",
                        "size": len(png_bytes),
                        "download_url": "https://raw.githubusercontent.com/org/repo/main/memes/aqua_wave.png",
                        "html_url": "https://github.com/org/repo/blob/main/memes/aqua_wave.png",
                    }
                ],
            )
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, content=png_bytes)
        return httpx.Response(404)

    vision = FakeVision(error=RuntimeError("vision unavailable"))
    catalog, _, harvester = _setup_harvester(
        settings,
        store,
        vision=vision,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    # SSRF literal checks
    with pytest.raises(ValueError, match="HTTPS"):
        harvester.add_source({"kind": "remote_feed", "name": "bad", "url": "http://example.com/m.json"})
    with pytest.raises(ValueError, match="内网|HTTPS"):
        harvester.add_source({"kind": "remote_feed", "name": "bad2", "url": "https://127.0.0.1/m.json"})

    # Redirect to private DNS fails during sync
    bad_redir = harvester.add_source(
        {"kind": "remote_feed", "name": "redir", "target_kind": "manifest", "url": "https://redir.example.org/m.json"}
    )
    redir_res = await harvester.sync_source(bad_redir["source_id"])
    assert redir_res["status"] == "error" and "非公网" in redir_res["error"]

    # Manifest with auto_approve=True + title + emotions skips Stage 2 and preserves GIF bytes
    sub_m = harvester.add_source(
        {
            "kind": "remote_feed",
            "name": "ManifestFeed",
            "target_kind": "manifest",
            "url": "https://cdn.example.org/manifest.json",
            "auto_approve": True,
        }
    )
    res_m1 = await harvester.sync_source(sub_m["source_id"])
    assert res_m1["status"] == "ok" and res_m1["accepted"] == 1
    assets = catalog.list("whale")
    assert len(assets) == 1 and assets[0]["title"] == "测试摇摆"
    assert catalog.read(assets[0]["asset_id"], "whale")["content"] == gif_bytes

    # Second sync hits 304 Not Modified via If-None-Match
    res_m2 = await harvester.sync_source(sub_m["source_id"])
    assert res_m2["status"] == "not_modified" and etag_calls == 2

    # GitHub dir with auto_approve=True and vision unavailable falls back to inbox (NOT catalog!)
    sub_gh = harvester.add_source(
        {
            "kind": "remote_feed",
            "name": "GitHubDir",
            "target_kind": "github_dir",
            "url": "https://github.com/org/repo/tree/main/memes",
            "auto_approve": True,
        }
    )
    res_gh = await harvester.sync_source(sub_gh["source_id"])
    assert res_gh["status"] == "ok" and res_gh["accepted"] == 1
    assert len(catalog.list("whale")) == 1  # Still only the manifest one!
    assert len(harvester.list_inbox()["items"]) == 1
    assert harvester.list_inbox()["items"][0]["ai_tagged"] is False


async def test_channel_b_comment_piggybacking_hooks_and_queue_bound(settings, store):
    BrowsingRepository(store)
    png_bytes = _make_png(128, 128, color=(50, 160, 220), draw_box=True)
    vision = FakeVision(
        {
            "is_whale_aqua": True,
            "clean_ok": True,
            "confidence": 0.92,
            "title": "评论区捡图",
            "emotions": ["cheerful"],
            "tags": ["大肥鱼"],
            "description": "评论配图",
        }
    )
    _, _, harvester = _setup_harvester(settings, store, vision=vision)

    # 1. materials.py reuses downloaded bytes directly without a second CDN fetch
    class Adapter:
        async def read_comment_image(self, url):
            return png_bytes

    class VisualCap:
        def capabilities(self):
            return {"version": "v1", "images": True, "max_frames": 3, "max_pixels": 1024 * 1024, "max_bytes": 1024 * 1024}

        async def analyze_comment_images(self, task, frames, *, deadline=None):
            return ["一张可爱的鲸鱼图"]

    async def direct_read(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    browsing = SimpleNamespace(
        adapter=Adapter(),
        visual_provider=VisualCap(),
        store=store,
        runtime=SimpleNamespace(meme_harvester=harvester),
        _read=direct_read,
    )
    source_row = {
        "comment_id": "9001",
        "platform_user_id": "777",
        "display_name": "热心观众",
        "content": "发个大肥鱼图",
        "attachments": [{"kind": "image", "url": "https://i0.hdslb.com/bfs/comment_a.png@100w.webp"}],
    }
    obs = await prepare_comment_materials(browsing, {"parent": "9001", "bvid": "BV1xx411c7mD"}, source_row)
    assert obs[0]["status"] == "read"
    q_row = store.one("SELECT * FROM meme_harvest_queue WHERE image_url=?", ("https://i0.hdslb.com/bfs/comment_a.png",))
    assert q_row is not None and q_row["cached_blob"].startswith(".pending-")

    # Drain queue -> goes to inbox with interaction_only + provenance
    drain = await harvester.drain_queue()
    assert drain["accepted"] == 1
    inbox = harvester.list_inbox()["items"]
    assert len(inbox) == 1
    assert inbox[0]["usage_scope"] == "interaction_only"
    assert inbox[0]["source"]["kind"] == "bilibili_comment"
    assert inbox[0]["source"]["author_id"] == "777"
    assert "BV1xx411c7mD" in inbox[0]["source"]["url"]

    # 2. enqueue_own_comments & comment_harvest_enabled toggle
    await harvester.update_config({"comment_harvest_enabled": False})
    enqueue_own_comments(
        browsing,
        "100",
        0,
        {"resource_type": "video", "oid": "1", "bvid": "BV1xx411c7mD"},
        [source_row | {"comment_id": "9002", "thread_id": "9002", "created_at": "1", "attachments": [{"kind": "image", "url": "https://i1.hdslb.com/bfs/comment_b.png"}]}],
    )
    assert store.one("SELECT COUNT(*) AS n FROM meme_harvest_queue")["n"] == 0

    await harvester.update_config({"comment_harvest_enabled": True})
    # Overflow bound check: enqueue > MAX_QUEUE_ROWS
    bulk_items = [
        {
            "comment_id": str(10000 + i),
            "thread_id": str(10000 + i),
            "platform_user_id": "888",
            "display_name": "观众",
            "content": "表情包",
            "created_at": "1",
            "attachments": [{"kind": "image", "url": f"https://i1.hdslb.com/bfs/bulk_{i}.png"}],
        }
        for i in range(MAX_QUEUE_ROWS + 15)
    ]
    enqueue_own_comments(
        browsing,
        "100",
        0,
        {"resource_type": "video", "oid": "1", "bvid": "BV1xx411c7mD"},
        bulk_items,
    )
    assert store.one("SELECT COUNT(*) AS n FROM meme_harvest_queue")["n"] == MAX_QUEUE_ROWS
    # Oldest 15 were pruned, newest remains
    assert store.one("SELECT 1 FROM meme_harvest_queue WHERE image_url=?", ("https://i1.hdslb.com/bfs/bulk_0.png",)) is None
    assert store.one("SELECT 1 FROM meme_harvest_queue WHERE image_url=?", (f"https://i1.hdslb.com/bfs/bulk_{MAX_QUEUE_ROWS + 14}.png",)) is not None

    # 3. reader.py evaluate_comment_visit passive hook (with preview=False)
    store.execute("DELETE FROM meme_harvest_queue")
    called_items = [
        {
            "comment_id": "5555",
            "thread_id": "5555",
            "platform_user_id": "666",
            "display_name": "访客",
            "content": "可爱图",
            "created_at": "10",
            "attachments": [{"kind": "image", "url": "https://i2.hdslb.com/bfs/visit_pic.png"}],
        }
    ]

    class FakeRepo:
        def complete(self, *args, **kwargs):
            pass

    fake_browsing = SimpleNamespace(
        store=store,
        runtime=SimpleNamespace(meme_harvester=harvester),
        repo=FakeRepo(),
        _check_round=lambda row: None,
        video_comment_context=lambda row, bvid: asyncio.sleep(0, result={"items": called_items}),
        commit_comment_context=lambda ctx: None,
    )
    # Insert dummy browsing_tasks row for evaluate_comment_visit
    store.execute("INSERT INTO browsing_tasks(id, payload) VALUES('t1', '{}')")
    store.execute(
        "CREATE TABLE IF NOT EXISTS browsing_comment_revisits("
        "account_id TEXT, character_id TEXT, character_version INTEGER, review_session TEXT, "
        "bvid TEXT, round_id TEXT, page INTEGER, next_at REAL, error TEXT)"
    )
    row = {"id": "r1", "account_id": "100", "character_id": "whale", "character_version": 1, "preview": False, "config": {"autonomous_batch": True}}
    result = {"id": "t1", "owner": "o1", "generation": 1, "metadata": {"bvid": "BV1xx411c7mD", "aid": 1, "owner_uid": "200"}, "material": None, "material_key": None, "heartbeat": asyncio.create_task(asyncio.sleep(0))}
    await evaluate_comment_visit(fake_browsing, result, row, {"failures": 0})
    assert store.one("SELECT 1 FROM meme_harvest_queue WHERE image_url=?", ("https://i2.hdslb.com/bfs/visit_pic.png",)) is not None


async def test_channel_c_bilibili_creator_dynamic_and_article_cursor(settings, store):
    img_dyn = _make_png(128, 128, color=(80, 120, 240), pattern=0)
    img_art = _make_png(132, 132, color=(40, 200, 120), pattern=2)
    vision = FakeVision(
        {
            "is_whale_aqua": True,
            "clean_ok": True,
            "confidence": 0.85,
            "title": "二创大肥鱼",
            "emotions": ["cheerful"],
            "tags": ["同人"],
            "description": "UP主创作",
        }
    )

    class CreatorAdapter:
        def __init__(self):
            self.dynamic_calls = 0

        async def wbi_params(self, params):
            return params

        async def _get(self, path, params):
            if path == "/x/polymer/web-dynamic/v1/feed/space":
                self.dynamic_calls += 1
                return {
                    "items": [
                        {
                            "id_str": "9992",
                            "modules": {
                                "module_author": {"mid": 321, "name": "二创大佬"},
                                "module_dynamic": {
                                    "desc": {"text": "画了新图"},
                                    "major": {"draw": {"items": [{"src": "https://i0.hdslb.com/bfs/dyn_9992.png@100w.webp"}]}},
                                },
                            },
                        },
                        {
                            "id_str": "9991",
                            "modules": {
                                "module_author": {"mid": 321, "name": "二创大佬"},
                                "module_dynamic": {
                                    "desc": {"text": "旧图"},
                                    "major": {"draw": {"items": [{"src": "https://i0.hdslb.com/bfs/dyn_9991.png"}]}},
                                },
                            },
                        },
                    ]
                }
            if path == "/x/article/view":
                return {
                    "id": int(params["id"]),
                    "title": "大肥鱼图集专栏",
                    "author": {"mid": 654, "name": "专栏画师"},
                    "type": 0,
                    "content": '<p>正文</p><img src="https://i1.hdslb.com/bfs/art_1.png" />',
                }
            raise AssertionError(f"unexpected path {path}")

        async def read_comment_image(self, url):
            if "dyn_" in url:
                return img_dyn
            return img_art

    adapter = CreatorAdapter()

    async def read_via_gate(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    browsing = SimpleNamespace(adapter=adapter, _read=read_via_gate)
    _, _, harvester = _setup_harvester(settings, store, vision=vision, browsing=browsing)

    sub_dyn = harvester.add_source(
        {
            "kind": "bilibili_creator",
            "target_kind": "space_dynamic",
            "name": "二创大佬",
            "url": "https://space.bilibili.com/321",
        }
    )
    assert sub_dyn["auto_approve"] is False
    res_dyn = await harvester.sync_source(sub_dyn["source_id"])
    assert res_dyn["processed"] == 2 and res_dyn["accepted"] == 1  # second image is dHash duplicate of first? wait same bytes -> same sha!
    # Sync again with cursor="9992": stops before 9992 -> processed == 0!
    res_dyn2 = await harvester.sync_source(sub_dyn["source_id"])
    assert res_dyn2["processed"] == 0

    sub_art = harvester.add_source(
        {
            "kind": "bilibili_creator",
            "target_kind": "article",
            "name": "图集专栏",
            "url": "https://www.bilibili.com/read/cv8888",
        }
    )
    res_art = await harvester.sync_source(sub_art["source_id"])
    assert res_art["processed"] == 1 and res_art["accepted"] == 1
    items = harvester.list_inbox()["items"]
    assert any(it["source"]["kind"] == "bilibili_creator" and it["source"]["author_id"] == "654" for it in items)


async def test_inbox_approve_reject_batch_and_cache_eviction_db_sync(settings, store):
    vision = FakeVision(
        {
            "is_whale_aqua": True,
            "clean_ok": True,
            "confidence": 0.91,
            "title": "待审图",
            "emotions": ["idle"],
            "tags": ["测试"],
            "description": "描述",
        }
    )
    catalog, cache, harvester = _setup_harvester(settings, store, vision=vision)

    img1 = _make_png(128, 128, color=(10, 20, 200), pattern=0)
    img2 = _make_png(160, 120, color=(200, 20, 20), pattern=1)
    img3 = _make_png(120, 160, color=(20, 200, 20), pattern=2)

    r1 = await harvester.process_candidate_bytes(
        img1, channel="bilibili_creator", source={"kind": "bilibili_creator", "author": "a1", "url": "https://t.bilibili.com/1"}, license={"name": "l1"}
    )
    r2 = await harvester.process_candidate_bytes(
        img2, channel="bilibili_creator", source={"kind": "bilibili_creator", "author": "a2", "url": "https://t.bilibili.com/2"}, license={"name": "l2"}
    )
    r3 = await harvester.process_candidate_bytes(
        img3, channel="bilibili_creator", source={"kind": "bilibili_creator", "author": "a3", "url": "https://t.bilibili.com/3"}, license={"name": "l3"}
    )
    id1, id2, id3 = r1["item"]["item_id"], r2["item"]["item_id"], r3["item"]["item_id"]

    # Approve id1 with edited metadata and usage_scope
    approved = await harvester.approve_inbox_item(id1, title="已核准", usage_scope="interaction_only", tags=["精修"])
    assert approved["approved"] is True
    assert approved["asset"]["usage_scope"] == "interaction_only"
    assert approved["asset"]["title"] == "已核准"
    assert store.one("SELECT * FROM meme_inbox WHERE item_id=?", (id1,)) is None

    # Batch reject remaining two
    batch = await harvester.batch_inbox_action("reject", [id2, id3])
    assert set(batch["succeeded"]) == {id2, id3}
    assert catalog.is_rejected(r2["item"]["sha256"]) is True
    assert harvester.list_inbox()["total"] == 0

    # Cache eviction DB sync: add 2 items and configure meme_inbox_max_items=1
    img4 = _make_png(144, 144, color=(120, 10, 180), pattern=3)
    img5 = _make_png(156, 156, color=(180, 180, 10), pattern=4)
    r4 = await harvester.process_candidate_bytes(
        img4, channel="bilibili_creator", source={"kind": "bilibili_creator", "author": "a4", "url": "https://t.bilibili.com/4"}, license={"name": "l4"}
    )
    await asyncio.sleep(0.02)
    r5 = await harvester.process_candidate_bytes(
        img5, channel="bilibili_creator", source={"kind": "bilibili_creator", "author": "a5", "url": "https://t.bilibili.com/5"}, license={"name": "l5"}
    )
    assert harvester.list_inbox()["total"] == 2
    await cache.configure({"meme_inbox_max_items": 1})
    remaining = harvester.list_inbox()["items"]
    assert len(remaining) == 1
    assert remaining[0]["item_id"] == r5["item"]["item_id"]
    assert store.one("SELECT * FROM meme_inbox WHERE item_id=?", (r4["item"]["item_id"],)) is None


def test_persona_rest_routes_for_harvester_inbox_sources_and_usage_scope(settings, monkeypatch):
    class Platform:
        async def close(self):
            pass

    app = create_app(settings, Platform())
    app.state.memes.inspector = FakeInspector()

    with TestClient(app) as client:
        client.post("/api/v1/system/auth/login", json={"token": settings.admin_token}).raise_for_status()

        # Upload meme with usage_scope
        png = _make_png(128, 128, color=(30, 140, 210), draw_box=True)
        uploaded = client.post(
            "/api/v1/persona/memes",
            json={
                "character": "whale",
                "title": "仅互动图",
                "emotions": ["cheerful"],
                "source": {"kind": "user_upload", "author": "op"},
                "license": {"name": "local"},
                "content_base64": base64.b64encode(png).decode(),
                "usage_scope": "interaction_only",
            },
        )
        uploaded.raise_for_status()
        asset_id = uploaded.json()["asset_id"]
        assert uploaded.json()["usage_scope"] == "interaction_only"

        # Toggle usage_scope via PUT /memes/{asset_id}
        toggled = client.put(f"/api/v1/persona/memes/{asset_id}", json={"usage_scope": "all_channels"})
        toggled.raise_for_status()
        assert toggled.json()["usage_scope"] == "all_channels"

        # Harvester config GET/PUT syncs with MediaCache
        cfg = client.get("/api/v1/persona/memes/harvester/config")
        cfg.raise_for_status()
        assert cfg.json()["comment_harvest_enabled"] is True

        updated_cfg = client.put(
            "/api/v1/persona/memes/harvester/config",
            json={
                "comment_harvest_enabled": False,
                "default_sync_interval_hours": 6,
                "meme_inbox_limit_mib": 0,
                "meme_inbox_max_items": 0,
            },
        )
        updated_cfg.raise_for_status()
        assert updated_cfg.json()["comment_harvest_enabled"] is False
        assert updated_cfg.json()["meme_inbox_limit_mib"] == 0
        assert updated_cfg.json()["meme_inbox_max_items"] == 0
        assert app.state.runtime.cache.usage("meme_inbox")["unlimited"] is True

        # Sources CRUD
        created = client.post(
            "/api/v1/persona/memes/sources",
            json={
                "kind": "remote_feed",
                "target_kind": "manifest",
                "name": "社区图包",
                "url": "https://example.org/memes/manifest.json",
                "auto_approve": False,
            },
        )
        created.raise_for_status()
        source_id = created.json()["source_id"]
        assert client.get("/api/v1/persona/memes/sources").json()["items"][0]["source_id"] == source_id
        patched = client.patch(f"/api/v1/persona/memes/sources/{source_id}", json={"enabled": False})
        patched.raise_for_status()
        assert patched.json()["enabled"] is False
        assert client.delete(f"/api/v1/persona/memes/sources/{source_id}").json()["deleted"] is True

        # Seed an inbox item and test GET inbox, preview, approve, reject, batch
        harvester = app.state.runtime.meme_harvester
        harvester._visual_provider = FakeVision(
            {
                "is_whale_aqua": True,
                "clean_ok": True,
                "confidence": 0.9,
                "title": "收件箱预览",
                "emotions": ["cheerful"],
                "tags": ["预览"],
                "description": "测试",
            }
        )
        gif = _make_gif()
        res = asyncio.run(
            harvester.process_candidate_bytes(
                gif,
                channel="bilibili_comment",
                source={"kind": "bilibili_comment", "author": "c1", "author_id": "11", "url": "https://www.bilibili.com/video/BV1xx411c7mD"},
                license={"name": "comment"},
                usage_scope="interaction_only",
            )
        )
        item_id = res["item"]["item_id"]
        inbox_resp = client.get("/api/v1/persona/memes/inbox")
        inbox_resp.raise_for_status()
        assert inbox_resp.json()["total"] == 1
        preview_resp = client.get(f"/api/v1/persona/memes/inbox/{item_id}/preview")
        preview_resp.raise_for_status()
        assert preview_resp.content == gif and preview_resp.headers["content-type"] == "image/gif"

        approve_resp = client.post(
            f"/api/v1/persona/memes/inbox/{item_id}/approve",
            json={"title": "API核准", "usage_scope": "all_channels"},
        )
        approve_resp.raise_for_status()
        assert approve_resp.json()["approved"] is True
        assert approve_resp.json()["asset"]["title"] == "API核准"

        # Seed two more inbox items to exercise reject + batch + sources/sync routes
        png2 = _make_png(140, 140, color=(200, 80, 30), pattern=1)
        png3 = _make_png(150, 150, color=(60, 200, 180), pattern=2)
        r2 = asyncio.run(
            harvester.process_candidate_bytes(
                png2,
                channel="bilibili_creator",
                source={"kind": "bilibili_creator", "author": "c2", "author_id": "22", "url": "https://t.bilibili.com/2"},
                license={"name": "creator"},
            )
        )
        r3 = asyncio.run(
            harvester.process_candidate_bytes(
                png3,
                channel="bilibili_creator",
                source={"kind": "bilibili_creator", "author": "c3", "author_id": "33", "url": "https://t.bilibili.com/3"},
                license={"name": "creator"},
            )
        )
        reject_resp = client.post(f"/api/v1/persona/memes/inbox/{r2['item']['item_id']}/reject")
        reject_resp.raise_for_status()
        assert reject_resp.json()["rejected"] is True
        batch_resp = client.post(
            "/api/v1/persona/memes/inbox/batch",
            json={"action": "approve", "item_ids": [r3["item"]["item_id"]], "usage_scope": "interaction_only"},
        )
        batch_resp.raise_for_status()
        assert batch_resp.json()["succeeded"] == [r3["item"]["item_id"]]

        sync_resp = client.post("/api/v1/persona/memes/sources/sync", json={})
        sync_resp.raise_for_status()
        assert "sources" in sync_resp.json() and "queue" in sync_resp.json()
