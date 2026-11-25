"""Tests for meme dHash, MemeCatalog extensions, vision classification, and dynamic usage_scope enforcement."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw

from tuber_studio.infra.inference.video_vision import ApiVideoEvidenceProvider
from tuber_studio.operations.channels.attachments import images as ops_images
from tuber_studio.operations.channels.dynamics import Dynamics
from tuber_studio.persona.assets.meme_dhash import compute_dhash, dhash_hamming, dhash_matches
from tuber_studio.persona.assets.meme_selection import select_meme
from tuber_studio.persona.assets.memes import MemeCatalog

SOURCE = {"kind": "user_upload", "author": "tester", "note": "fixture"}
LICENSE = {"name": "fixture-only"}


def _pattern_png(size=(128, 128), *, color_a=(20, 40, 200), color_b=(240, 230, 50), alt=False) -> bytes:
    img = Image.new("RGB", size, color_a)
    draw = ImageDraw.Draw(img)
    w, h = size
    for i in range(0, w, 16):
        if (i // 16) % 2 == (1 if alt else 0):
            draw.rectangle([i, 0, min(w - 1, i + 12), h - 1], fill=color_b)
    draw.ellipse([w // 4, h // 4, (3 * w) // 4, (3 * h) // 4], fill=(220, 60, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid_gradient_png(size=(128, 128), *, invert=False) -> bytes:
    img = Image.new("L", size)
    for y in range(size[1]):
        for x in range(size[0]):
            val = int((x * 255) / max(1, size[0] - 1))
            img.putpixel((x, y), 255 - val if invert else val)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _animated_webp(frames: int = 4) -> bytes:
    imgs = []
    for idx in range(frames):
        im = Image.new("RGB", (64, 64), (20, 20, 20))
        draw = ImageDraw.Draw(im)
        draw.rectangle([idx * 10, 8, idx * 10 + 20, 56], fill=(240, 200, 40))
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, format="WEBP", save_all=True, append_images=imgs[1:], duration=60, loop=0)
    return buf.getvalue()


def _animated_gif(frames: int = 5) -> bytes:
    imgs = []
    for idx in range(frames):
        im = Image.new("RGB", (64, 64), (10, 30, 80))
        draw = ImageDraw.Draw(im)
        draw.rectangle([0, idx * 8, 63, idx * 8 + 16], fill=(250, 240, 100))
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=50, loop=0)
    return buf.getvalue()


def test_dhash_still_animated_and_matching(settings):
    png = _pattern_png((160, 160))
    # Resize and re-encode as JPEG -> perceptual hash should stay within still threshold
    with Image.open(io.BytesIO(png)) as im:
        resized = im.resize((96, 96), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=85)
        jpg = buf.getvalue()

    h_png = compute_dhash(png)
    h_jpg = compute_dhash(jpg)
    assert len(h_png) == 16 and h_png == h_png.lower()
    assert ":" not in h_png
    assert dhash_hamming(h_png, h_jpg) <= 6
    assert dhash_matches(h_png, h_jpg)

    distinct_a = compute_dhash(_solid_gradient_png(invert=False))
    distinct_b = compute_dhash(_solid_gradient_png(invert=True))
    assert dhash_hamming(distinct_a, distinct_b) > 6
    assert not dhash_matches(distinct_a, distinct_b)

    gif_hash = compute_dhash((settings.root / "assets/whale/cheerful.gif").read_bytes())
    webp_hash = compute_dhash(_animated_webp(4))
    assert ":" in gif_hash and len(gif_hash.split(":")) == 2
    assert all(len(part) == 16 for part in gif_hash.split(":"))
    assert ":" in webp_hash and len(webp_hash.split(":")) == 2

    # Bound frame scan to MAX_FRAMES=300 for >300 frame GIFs
    big_gif = _animated_gif(310)
    big_hash = compute_dhash(big_gif)
    assert ":" in big_hash and len(big_hash.split(":")) == 2

    # Threshold combinations
    zero = "0" * 16
    near_still = f"{0x1f:016x}"  # 5 bits
    far_still = f"{0xff:016x}"   # 8 bits
    assert dhash_matches(zero, near_still)
    assert not dhash_matches(zero, far_still)
    assert dhash_matches(f"{zero}:{zero}", f"{near_still}:{near_still}")  # sum=10 <= 12
    assert dhash_matches(f"{zero}:{zero}", f"{0x3f:016x}:{0x3f:016x}")  # 6+6=12 <= 12 and <=6 each
    assert not dhash_matches(f"{zero}:{zero}", f"{far_still}:{far_still}")  # 16 > 12
    assert dhash_matches(f"{zero}:{far_still}", near_still)  # anim-vs-still checks first frame <= 6
    assert not dhash_matches("", zero)
    assert not dhash_matches(zero, "")
    with pytest.raises(ValueError):
        dhash_hamming("", zero)


def test_idempotent_db_migration_and_source_kinds(settings, store):
    store.db.executescript("""
        DROP TABLE IF EXISTS meme_labels;
        DROP TABLE IF EXISTS meme_pins;
        DROP TABLE IF EXISTS meme_assets;
        DROP TABLE IF EXISTS meme_rejected_hashes;
        CREATE TABLE meme_assets(
            asset_id TEXT PRIMARY KEY, title TEXT NOT NULL, emotions TEXT NOT NULL,
            sha256 TEXT NOT NULL, storage_kind TEXT NOT NULL, blob_name TEXT NOT NULL,
            image_info TEXT NOT NULL, bytes INTEGER NOT NULL, source TEXT NOT NULL,
            license TEXT NOT NULL, enabled INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL);
    """)
    MemeCatalog(settings, store)
    # Second init must stay idempotent
    MemeCatalog(settings, store)
    cols = {row["name"] for row in store.all("PRAGMA table_info(meme_assets)")}
    assert {"usage_scope", "dhash"} <= cols
    assert store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='meme_rejected_hashes'")
    assert store.one("SELECT name FROM sqlite_master WHERE type='index' AND name='meme_rejected_hashes_sha256'")


async def test_catalog_usage_scope_dhash_dedup_rejected_and_seed_backfill(settings, store):
    catalog = MemeCatalog(settings, store)
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")

    png = _pattern_png((128, 128))
    gif = (settings.root / "assets/whale/cheerful.gif").read_bytes()

    all_asset = await catalog.register(
        "whale",
        png,
        title="全渠道表情",
        emotions=["cheerful"],
        source={"kind": "remote_feed", "author": "feed-maintainer", "author_id": "gh-123"},
        license=LICENSE,
    )
    assert all_asset["usage_scope"] == "all_channels"
    assert len(all_asset["dhash"]) == 16
    assert all_asset["source"]["author_id"] == "gh-123"

    inter_asset = await catalog.register(
        "whale",
        gif,
        title="仅互动表情",
        emotions=["shy"],
        source={"kind": "bilibili_comment", "author": "评论网友", "author_id": "45678"},
        license=LICENSE,
        usage_scope="interaction_only",
    )
    assert inter_asset["usage_scope"] == "interaction_only"
    assert ":" in inter_asset["dhash"]

    with pytest.raises(ValueError, match="使用范围"):
        await catalog.register(
            "whale", png, title="bad", emotions=["idle"], source=SOURCE, license=LICENSE, usage_scope="invalid"
        )

    assert [item["asset_id"] for item in catalog.list("whale", usage_scope="all_channels")] == [all_asset["asset_id"]]
    assert [item["asset_id"] for item in catalog.list("whale", usage_scope="interaction_only")] == [
        inter_asset["asset_id"]
    ]

    # update_availability & set_usage_scope
    with pytest.raises(ValueError):
        catalog.update_availability(all_asset["asset_id"])
    updated = catalog.update_availability(inter_asset["asset_id"], enabled=False, usage_scope="all_channels")
    assert not updated["enabled"] and updated["usage_scope"] == "all_channels"
    restored = catalog.set_usage_scope(inter_asset["asset_id"], "interaction_only")
    assert restored["usage_scope"] == "interaction_only" and not restored["enabled"]

    # find_duplicate: sha256 first, then dhash_matches, skipping deleted rows
    assert catalog.find_duplicate(all_asset["sha256"])["asset_id"] == all_asset["asset_id"]
    with Image.open(io.BytesIO(png)) as im:
        buf = io.BytesIO()
        im.resize((96, 96)).save(buf, format="PNG")
        near_png = buf.getvalue()
    near_dhash = compute_dhash(near_png)
    dup = catalog.find_duplicate("0" * 64, near_dhash)
    assert dup is not None and dup["asset_id"] == all_asset["asset_id"]

    # Deleted row does not match find_duplicate
    catalog.enable(all_asset["asset_id"], False)
    catalog.delete(all_asset["asset_id"])
    assert catalog.find_duplicate(all_asset["sha256"], all_asset["dhash"]) is None

    # Rejected blacklist
    rej = catalog.record_rejected(
        "a" * 64, all_asset["dhash"], source_kind="bilibili_comment", source_url="https://www.bilibili.com/video/BV1xx"
    )
    assert rej["sha256"] == "a" * 64
    assert catalog.is_rejected("a" * 64)
    assert catalog.is_rejected("b" * 64, near_dhash)
    assert not catalog.is_rejected("b" * 64, compute_dhash(_solid_gradient_png(invert=True)))
    # Upsert on same sha256 updates without creating duplicate row
    catalog.record_rejected("a" * 64, source_kind="remote_feed")
    assert len(store.all("SELECT * FROM meme_rejected_hashes WHERE sha256=?", ("a" * 64,))) == 1

    # seed_existing backfills empty dhash on existing non-deleted rows
    store.execute("UPDATE meme_assets SET dhash='' WHERE asset_id=?", (inter_asset["asset_id"],))
    seeded = await catalog.seed_existing()
    assert len(seeded) >= 33
    reloaded = catalog.read(inter_asset["asset_id"], "whale", allow_disabled=True)["asset"]
    assert reloaded["dhash"] == inter_asset["dhash"]


async def test_classify_meme_image_schema_and_nested_offline_generation(runtime):
    config = {
        "base_url": "http://127.0.0.1:18080/v1",
        "model": "vlm-test",
        "max_frames": 4,
        "max_pixels": 1024 * 1024,
        "max_bytes": 2 * 1024 * 1024,
    }
    provider = ApiVideoEvidenceProvider(runtime, config)
    await provider.api.client.aclose()
    state = {
        "response": {
            "is_whale_aqua": True,
            "clean_ok": True,
            "confidence": 0.92,
            "title": "大肥鱼开心转圈",
            "emotions": ["cheerful", "excited"],
            "tags": ["大肥鱼", "转圈"],
            "description": "白发大肥鱼开心地转圈。",
        },
        "requests": [],
    }

    def handle(request):
        state["requests"].append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "vlm-test", "max_model_len": 32768}]})
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        if payload["max_tokens"] == 1:
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": None}}],
                      "usage": {"prompt_tokens": 20, "completion_tokens": 1}},
            )
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(state["response"])}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 25},
            },
        )

    provider.api.client = httpx.AsyncClient(
        base_url="http://127.0.0.1:18080/v1/", transport=httpx.MockTransport(handle)
    )
    png = _pattern_png((64, 64))
    frames = [{"image": png, "mime_type": "image/png", "width": 64, "height": 64, "time_ms": 0}]
    try:
        # Standalone call acquires offline_generation("background")
        res = await provider.classify_meme_image(frames, context_hint="评论附图")
        assert res["is_whale_aqua"] is True and res["clean_ok"] is True and res["confidence"] == pytest.approx(0.92)
        assert res["emotions"] == ["cheerful", "excited"] and res["tags"] == ["大肥鱼", "转圈"]

        # Nested call inside runtime.offline_generation("background") must NOT deadlock
        async with asyncio.timeout(2):
            async with runtime.offline_generation("background"):
                res2 = await provider.classify_meme_image(frames)
        assert res2["title"] == "大肥鱼开心转圈"

        # Invalid confidence / emotions rejected
        state["response"] = dict(state["response"], confidence=1.5)
        with pytest.raises(ValueError, match="置信度"):
            await provider.classify_meme_image(frames)
    finally:
        await provider.close()


async def test_usage_scope_enforced_on_dynamic_channel_only(settings, store, runtime, monkeypatch):
    catalog = MemeCatalog(settings, store)
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")

    png_all = _pattern_png((96, 96))
    png_inter = _solid_gradient_png((96, 96))
    all_meme = await catalog.register(
        "whale", png_all, title="公开表情", emotions=["cheerful"], source=SOURCE, license=LICENSE, usage_scope="all_channels"
    )
    inter_meme = await catalog.register(
        "whale",
        png_inter,
        title="互动专属表情",
        emotions=["shy"],
        source={"kind": "bilibili_comment", "author": "网友"},
        license=LICENSE,
        usage_scope="interaction_only",
    )

    # 1. meme_selection: dynamic filters out interaction_only; direct_message includes both
    class SelectorLLM:
        def __init__(self):
            self.offered_schemas = []

        async def generate_json(self, messages, schema, *, max_tokens):
            self.offered_schemas.append(schema)
            enum = schema["properties"]["asset_id"]["anyOf"][1]["enum"]
            return {"asset_id": enum[0]}

    llm = SelectorLLM()
    fake_rt = SimpleNamespace(operation_llm=llm)
    dyn_pick = await select_meme(fake_rt, catalog, "whale", {"text": "今天开心"}, "好耶", mode="dynamic")
    assert dyn_pick["asset_id"] == all_meme["asset_id"]
    dyn_enum = llm.offered_schemas[-1]["properties"]["asset_id"]["anyOf"][1]["enum"]
    assert dyn_enum == [all_meme["asset_id"]]

    # 2. OperationImages: dynamic shortlist excludes interaction_only and rejects GalleryImage if interaction_only
    store.set("illustration_policy", {"use_memes": True, "generate_images": False})
    images = runtime.operation_images
    images.catalog = catalog
    dyn_scope = ops_images.ImageScope(account_id="owner", character_id="whale", channel="dynamic", source_id="post-1")
    job = images.decide(dyn_scope, request_key="dyn-k1", context={"source": "测试"}, draft="互动专属表情")
    row = store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (job["job_id"],))
    material = images._choice_material(dyn_scope, row)
    assert [item["asset_id"] for item in material["gallery"]] == [all_meme["asset_id"]]

    async def pick_interaction(*_args, **_kwargs):
        return ops_images.GalleryImage(asset_id=inter_meme["asset_id"], sha256=inter_meme["sha256"])

    monkeypatch.setattr(ops_images, "choose", pick_interaction)
    await images._process(row)
    assert images.status(dyn_scope, job["job_id"])["status"] == "awaiting_choice"

    # direct_message scope allows interaction_only
    dm_scope = ops_images.ImageScope(account_id="owner", character_id="whale", channel="direct_message", source_id="dm-1")
    dm_job = images.decide(dm_scope, request_key="dm-k1", context={"incoming": "hi"}, draft="互动专属表情")
    dm_row = store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (dm_job["job_id"],))
    await images._process(dm_row)
    assert images.status(dm_scope, dm_job["job_id"])["asset_id"] == inter_meme["asset_id"]

    # 3. Dynamics publisher rejects pinning/resolving interaction_only meme
    account = SimpleNamespace(
        adapter=SimpleNamespace(),
        credential_lock=asyncio.Lock(),
        credentials={},
        credentials_configured=True,
        display_account_id=lambda: "900",
    )
    dynamics = Dynamics(settings, store, runtime, SimpleNamespace(account=account), account=account, memes=catalog)
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO dynamic_decisions(id,character_id,character_version,manual,action,reason,sources,source_ids,created_at) "
                "VALUES(1,'whale',1,1,'post','reason',?,?,1.0)",
                (json.dumps([{"id": "op1", "type": "operator_topic", "text": "话题"}]), json.dumps(["op1"])),
            )
            store.db.execute(
                "INSERT INTO dynamic_posts(id,decision_id,account_id,character_id,character_version,status,manual,content,content_hash,sources,source_ids,created_at,updated_at) "
                "VALUES(1,1,'900','whale',1,'pending_review',1,'动态正文','h1',?,?,1.0,1.0)",
                (json.dumps([{"id": "op1", "type": "operator_topic", "text": "话题"}]), json.dumps(["op1"])),
            )
        with pytest.raises(ValueError, match="互动渠道"):
            dynamics.review(1, "edit", meme_id=inter_meme["asset_id"])
        assert not store.one("SELECT * FROM meme_pins WHERE owner='dynamic-draft:1'")

        edited = dynamics.review(1, "edit", meme_id=all_meme["asset_id"])
        assert edited["meme_id"] == all_meme["asset_id"]
        # Switching usage_scope to interaction_only afterwards blocks approve & _outgoing
        catalog.set_usage_scope(all_meme["asset_id"], "interaction_only")
        with pytest.raises(ValueError, match="互动渠道"):
            dynamics.review(1, "approve")
        with pytest.raises(ValueError, match="互动渠道"):
            dynamics._outgoing(dynamics._post(1), "whale")
    finally:
        await dynamics.close()
