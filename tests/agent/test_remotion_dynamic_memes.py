"""Tests for dynamic MemeCatalog integration, usage_scope filtering, pinned memes, and plan uploads in Remotion."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.skills.remotion_assets import (
    build_aqua_video_planner_intent,
    build_fallback_aqua_scenes,
    load_whale_meme_catalog,
    stage_whale_visual_assets,
)
from tuber_studio.agent.skills.remotion_video import (
    RemotionVideoSkill,
    generate_remotion_project,
    plan_video_script,
)
from tuber_studio.app import create_app
from tuber_studio.persona.assets.memes import MemeLabels

SOURCE = {"kind": "user_upload", "author": "operator", "note": "Local test sticker"}
LICENSE = {"name": "CC0 1.0"}


def _make_animated_gif_bytes(colors: tuple[tuple[int, int, int], ...] = ((14, 165, 233), (250, 204, 21), (251, 113, 133))) -> bytes:
    frames = [Image.new("RGBA", (32, 32), (*c, 255)) for c in colors]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=90, loop=0)
    return buf.getvalue()


def _make_png_bytes(width: int = 32, height: int = 32, *, noise: bool = False) -> bytes:
    img = Image.new("RGB", (width, height), (56, 189, 248))
    if noise:
        import os

        img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _register_gif(catalog, gif_bytes: bytes, *, title: str, usage_scope: str = "all_channels", tags=("测试",), description=""):
    return await catalog.register(
        "whale",
        gif_bytes,
        title=title,
        emotions=["cheerful"],
        source=SOURCE,
        license=LICENSE,
        tags=tags,
        description=description,
        usage_scope=usage_scope,
    )


def test_merged_catalog_deduplicates_bundled_and_excludes_interaction_only(settings):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        catalog = app.state.runtime.pets.catalog
        static_only = load_whale_meme_catalog(settings.root)
        static_count = len(static_only)
        assert static_count > 5
        assert all("source_path" in item and item["sha256"] and item["alias_ids"] == [item["id"]] for item in static_only)

        await catalog.seed_existing()
        megaphone_asset_id = "meme_" + uuid5(NAMESPACE_URL, "tuber-studio:existing-whale-meme:archive-megaphone").hex
        catalog.label(megaphone_asset_id, MemeLabels(tags=["开麦", "自定义标签"], description="更新后的开麦说明"))

        blanket_asset_id = "meme_" + uuid5(NAMESPACE_URL, "tuber-studio:existing-whale-meme:archive-blanket").hex
        catalog.set_usage_scope(blanket_asset_id, "interaction_only")

        dyn_all = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((10, 120, 220), (20, 140, 240))),
            title="动态全渠道鲸鱼",
            usage_scope="all_channels",
            tags=("动态", "鲸鱼"),
            description="可用于公开视频的动态表情。",
        )
        dyn_interact = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((200, 50, 80), (220, 80, 100))),
            title="仅互动私藏表情",
            usage_scope="interaction_only",
            tags=("私藏",),
            description="仅用于评论和私信。",
        )

        merged = load_whale_meme_catalog(settings.root, meme_catalog=catalog, character_id="whale")
        ids = [item["id"] for item in merged]
        assert len(ids) == len(set(ids))
        # Static count minus 1 interaction_only bundled item plus 1 all_channels dynamic item.
        assert len(merged) == static_count - 1 + 1

        megaphone = next(item for item in merged if item["id"] == "archive-megaphone")
        assert megaphone_asset_id in megaphone["alias_ids"]
        assert "自定义标签" in megaphone["tags"]
        assert megaphone["description"] == "更新后的开麦说明"

        assert "archive-blanket" not in ids
        assert dyn_all["asset_id"] in ids
        assert dyn_interact["asset_id"] not in ids

        dyn_entry = next(item for item in merged if item["id"] == dyn_all["asset_id"])
        assert Path(dyn_entry["file_path"]).is_file()
        assert dyn_entry["description"] == "可用于公开视频的动态表情。"

    asyncio.run(scenario())


def test_stage_whale_visual_assets_copies_dynamic_gifs_and_extracts_frames(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        catalog = app.state.runtime.pets.catalog
        await catalog.seed_existing()

        dyn = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((12, 160, 220), (240, 200, 40), (100, 220, 160))),
            title="三帧动态图库表情",
            usage_scope="all_channels",
        )
        megaphone_alias = "meme_" + uuid5(NAMESPACE_URL, "tuber-studio:existing-whale-meme:archive-megaphone").hex

        public_dir = tmp_path / "public"
        _, meme_assets = stage_whale_visual_assets(
            settings.root,
            public_dir,
            include_avatar=False,
            requested_meme_ids=[dyn["asset_id"], megaphone_alias, "archive-megaphone"],
            scene_cues=["cheerful", "curious"],
            meme_catalog=catalog,
            character_id="whale",
        )

        assert dyn["asset_id"] in meme_assets
        assert "archive-megaphone" in meme_assets
        assert megaphone_alias not in meme_assets
        assert len(meme_assets) <= 14

        dyn_staged = meme_assets[dyn["asset_id"]]
        assert (public_dir / dyn_staged["file"]).is_file()
        assert len(dyn_staged["frames"]) == 3
        for frame_rel in dyn_staged["frames"]:
            assert frame_rel.startswith(f"memes/frames/{dyn['asset_id']}/")
            assert (public_dir / frame_rel).is_file()

    asyncio.run(scenario())


def test_interaction_only_rejected_in_pin_update_generate_and_cleans_up_uploads(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        catalog = runtime.pets.catalog

        dyn_all = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((30, 180, 210), (50, 200, 230))),
            title="可用表情",
            usage_scope="all_channels",
        )
        dyn_interact = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((210, 40, 40), (230, 90, 90))),
            title="禁用公开视频的互动表情",
            usage_scope="interaction_only",
        )

        seen_packets: list[dict] = []

        async def inspect_planner(_messages, _schema, packet):
            seen_packets.append(packet)
            raise RuntimeError("force cleanup")

        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path / "renders")
        png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")

        # 1. Reject interaction_only in pinned_meme_ids
        with pytest.raises(ValueError, match="仅限互动"):
            await plan_video_script(
                skill,
                {
                    "topic": "测试拒绝仅互动表情",
                    "pinned_meme_ids": [dyn_interact["asset_id"]],
                    "max_scenes": 2,
                },
            )

        # 2. Clean up pre-staged upload directory if custom planner fails
        failing_skill = RemotionVideoSkill(
            runtime,
            runtime.store,
            renders_root=tmp_path / "renders",
            planner_fn=inspect_planner,
        )
        with pytest.raises(RuntimeError, match="force cleanup"):
            await plan_video_script(
                failing_skill,
                {
                    "topic": "失败清理上传目录",
                    "pinned_meme_ids": [dyn_all["asset_id"]],
                    "uploaded_images": [{"filename": "fail.png", "image_base64": png_b64, "mime_type": "image/png"}],
                    "max_scenes": 2,
                },
            )
        assert not failing_skill.renders_root.exists() or list(failing_skill.renders_root.iterdir()) == []
        assert len(seen_packets) == 1
        pkt_ids = {m["id"] for m in seen_packets[0]["available_memes"]}
        assert dyn_all["asset_id"] in pkt_ids
        assert dyn_interact["asset_id"] not in pkt_ids

        # 3. Valid plan, then reject interaction_only in update_job_plan and generate_remotion_project
        planned = await plan_video_script(skill, {"topic": "正常脚本测试", "max_scenes": 2})
        job_id = planned["job_id"]
        bad_scenes = list(planned["plan"]["scenes"])
        bad_scenes[0] = dict(bad_scenes[0])
        bad_scenes[0]["visual_props"] = dict(bad_scenes[0]["visual_props"], meme_asset_id=dyn_interact["asset_id"])

        with pytest.raises(ValueError, match="仅限互动"):
            skill.update_job_plan(job_id, {"scenes": bad_scenes})

        bad_plan = dict(planned["plan"], scenes=bad_scenes)
        with pytest.raises(ValueError, match="仅限互动"):
            await generate_remotion_project(skill, {"plan": bad_plan, "tts_mode": "dry_run"})

    asyncio.run(scenario())


def test_plan_with_pinned_memes_and_uploaded_images_stages_cache_and_fallback_scenes(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        runtime.cache.root = tmp_path
        catalog = runtime.pets.catalog
        await catalog.seed_existing()

        dyn = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((25, 130, 210), (245, 190, 35))),
            title="置顶动态表情",
            usage_scope="all_channels",
            description="用于开场的动态表情。",
        )
        megaphone_alias = "meme_" + uuid5(NAMESPACE_URL, "tuber-studio:existing-whale-meme:archive-megaphone").hex

        intent = build_aqua_video_planner_intent(
            "explainer",
            pinned_meme_ids=[dyn["asset_id"]],
            uploaded_image_files=["uploads/arch.png"],
        )
        assert dyn["asset_id"] in intent
        assert "uploads/arch.png" in intent

        fallback_title, fallback_scenes = build_fallback_aqua_scenes(
            topic="测试置顶与配图",
            style_note="",
            sources=[],
            max_scenes=3,
            available_memes=load_whale_meme_catalog(settings.root, meme_catalog=catalog, character_id="whale"),
            video_mode="explainer",
            pinned_meme_ids=[dyn["asset_id"], megaphone_alias],
            uploaded_image_files=["uploads/arch.png", "uploads/ui.png"],
        )
        assert fallback_title
        assert fallback_scenes[0]["visual_props"]["meme_asset_id"] == dyn["asset_id"]
        assert fallback_scenes[1]["visual_props"]["meme_asset_id"] == "archive-megaphone"
        for scene in (fallback_scenes[1], fallback_scenes[2]):
            assert scene["template"] in {"QuoteSpotlight", "MemeCutaway", "CodeWalkthrough"}
            assert scene["visual_props"].get("uploaded_image_file")

        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path / "video_renders")
        png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")
        planned = await plan_video_script(
            skill,
            {
                "topic": "动态表情与截图联动测试",
                "video_mode": "explainer",
                "max_scenes": 4,
                "pinned_meme_ids": [dyn["asset_id"], megaphone_alias],
                "uploaded_images": [
                    {"filename": "shot_a.png", "image_base64": png_b64, "mime_type": "image/png"},
                    {"filename": "shot_b.png", "image_base64": png_b64, "mime_type": "image/png"},
                ],
            },
        )
        job_id = planned["job_id"]
        job = skill.get_job(job_id)
        assert job is not None and job["project_dir"]
        proj_dir = Path(job["project_dir"])
        assert (proj_dir / "public/uploads/shot_a.png").is_file()
        assert (proj_dir / "public/uploads/shot_b.png").is_file()
        assert len(skill.list_job_uploads(job_id)) == 2
        assert runtime.cache.status()["categories"]["video"]["bytes"] > 0

        scene_memes = [s["visual_props"].get("meme_asset_id") for s in planned["plan"]["scenes"]]
        assert dyn["asset_id"] in scene_memes
        assert "archive-megaphone" in scene_memes
        assert len(scene_memes) == len(set(scene_memes))

        upload_refs = [
            s["visual_props"].get("uploaded_image_file")
            for s in planned["plan"]["scenes"]
            if s["visual_props"].get("uploaded_image_file")
        ]
        assert upload_refs == ["uploads/shot_a.png", "uploads/shot_b.png"]

        generated = await generate_remotion_project(skill, {"job_id": job_id, "tts_mode": "dry_run"})
        assert generated["ok"] is True
        props = json.loads((proj_dir / "props.json").read_text(encoding="utf-8"))
        assert dyn["asset_id"] in props["memeAssets"]
        assert "archive-megaphone" in props["memeAssets"]
        assert len(props["memeAssets"][dyn["asset_id"]]["frames"]) == 2
        assert (proj_dir / "public/uploads/shot_a.png").is_file()

    asyncio.run(scenario())


def test_http_routes_large_plan_and_upload_payloads_and_interaction_only_rejection(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    headers = {"Authorization": "Bearer " + settings.admin_token}

    async def seed():
        catalog = app.state.runtime.pets.catalog
        dyn_interact = await _register_gif(
            catalog,
            _make_animated_gif_bytes(((180, 30, 90), (220, 60, 120))),
            title="HTTP 拦截测试仅互动",
            usage_scope="interaction_only",
        )
        return dyn_interact["asset_id"]

    interact_id = asyncio.run(seed())
    # Generate a >2.4MB PNG (larger than DEFAULT_JSON_LIMIT=2_100_000) to verify middleware allows it.
    large_png = _make_png_bytes(920, 920, noise=True)
    large_b64 = base64.b64encode(large_png).decode("ascii")
    assert len(large_b64) > 2_200_000

    with TestClient(app, headers=headers) as client:
        runtime = app.state.runtime
        runtime.remotion_skill.renders_root = tmp_path / "video_renders"
        runtime.cache.root = tmp_path

        bad_plan = client.post(
            "/api/v1/operations/videos/plan",
            json={"topic": "拒绝互动表情", "pinned_meme_ids": [interact_id]},
        )
        assert bad_plan.status_code == 400

        ok_plan = client.post(
            "/api/v1/operations/videos/plan",
            json={
                "topic": "大图随脚本直传测试",
                "max_scenes": 3,
                "uploaded_images": [
                    {"filename": "big_screen.png", "image_base64": large_b64, "mime_type": "image/png"}
                ],
            },
        )
        assert ok_plan.status_code == 200, ok_plan.text
        job_id = ok_plan.json()["job_id"]
        assert len(ok_plan.json()["job"]["uploads"]) == 1

        # Second large image via /uploads also bypasses DEFAULT_JSON_LIMIT
        up_resp = client.post(
            f"/api/v1/operations/videos/{job_id}/uploads",
            json={"filename": "extra_big.png", "image_base64": large_b64, "mime_type": "image/png"},
        )
        assert up_resp.status_code == 200, up_resp.text
        assert len(up_resp.json()["uploads"]) == 2

        # PATCH /plan with interaction_only meme_asset_id returns 400
        scenes = ok_plan.json()["plan"]["scenes"]
        scenes[0]["visual_props"]["meme_asset_id"] = interact_id
        patch_resp = client.patch(f"/api/v1/operations/videos/{job_id}/plan", json={"scenes": scenes})
        assert patch_resp.status_code == 400
