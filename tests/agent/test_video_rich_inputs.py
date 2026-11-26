"""Tests for Track B rich video inputs: brief_text, reference_urls, video_mode, 2-10 scenes, and image uploads."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.skills import remotion_assets
from tuber_studio.agent.skills.remotion_assets import (
    build_aqua_video_planner_intent,
    fetch_reference_urls,
)
from tuber_studio.agent.skills.remotion_video import (
    PlanVideoScriptArgs,
    RemotionVideoSkill,
    plan_video_script,
)
from tuber_studio.app import create_app


def _make_png_bytes(color: tuple[int, int, int] = (14, 165, 233)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(color: tuple[int, int, int] = (250, 204, 21)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="JPEG")
    return buf.getvalue()


def test_plan_long_brief_text_github_readme_and_single_url_fallback(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        seen_packets: list[dict] = []

        async def fake_fetch(url: str) -> dict:
            if "good-repo" in url:
                return {
                    "url": "https://github.com/aqua-org/good-repo",
                    "title": "aqua-org/good-repo README",
                    "excerpt": "Aqua Studio is a local Code-as-Video workstation with deterministic audio-frame alignment.",
                    "truncated": False,
                }
            raise TimeoutError("upstream read timed out")

        async def fake_planner(messages, schema, packet):
            seen_packets.append(packet)
            return {
                "title": "Aqua Studio 开源亮点速览",
                "summary": "基于公开 README 介绍开源项目结构。",
                "tags": ["Aqua", "开源项目"],
                "video_mode": "commentary",  # intentionally mismatched to verify enforcement
                "scenes": [
                    {
                        "scene_id": "s1",
                        "template": "TitleCard",
                        "heading": "痛点开场",
                        "visual_props": {"subtitle": "本地视频工作坊"},
                        "lines": [{"text": "剪辑和配音老是对不上帧怎么办？", "avatar_cue": "curious", "emphasis": ["对不上帧"]}],
                    },
                    {
                        "scene_id": "s2",
                        "template": "CodeWalkthrough",
                        "heading": "核心架构",
                        "visual_props": {"language": "python", "code_snippet": "frames = math.ceil(sec * fps)"},
                        "lines": [{"text": "音频先算帧长，动画就再也不飘了。", "avatar_cue": "proud", "emphasis": ["再也不飘"]}],
                    },
                    {
                        "scene_id": "s3",
                        "template": "TimelineSummary",
                        "heading": "项目总结",
                        "visual_props": {"subtitle": "开源自用两相宜"},
                        "lines": [{"text": "地址挂在片尾啦，欢迎来点个赞！", "avatar_cue": "cheerful", "emphasis": ["片尾"]}],
                    },
                ],
                "sources": [
                    {"source_id": "topic:custom", "kind": "operator_topic", "scope": "public", "title": "手写主题"}
                ],
            }

        skill = RemotionVideoSkill(
            runtime,
            runtime.store,
            renders_root=tmp_path,
            planner_fn=fake_planner,
            web_fetch_fn=fake_fetch,
        )
        long_brief = ("这是公开项目架构说明与产品要点。" * 160) + "尾段重点：支持 1080p 渲染与字幕对齐。"
        assert len(long_brief) > 2500

        res = await plan_video_script(
            skill,
            {
                "topic": "Aqua Studio 开源项目展示",
                "video_mode": "project_showcase",
                "brief_text": long_brief,
                "reference_urls": [
                    "  https://github.com/aqua-org/good-repo  ",
                    "",
                    "https://github.com/aqua-org/good-repo",
                    "https://docs.example.org/broken-guide",
                ],
                "max_scenes": 3,
            },
        )
        assert res["ok"] is True
        assert len(seen_packets) == 1
        pkt = seen_packets[0]
        assert pkt["video_mode"] == "project_showcase"
        assert pkt["brief_text"] == long_brief
        assert len(pkt["reference_pages"]) == 2
        assert len(res["reference_diagnostics"]) == 1
        assert "docs.example.org" in res["reference_diagnostics"][0]

        plan = res["plan"]
        assert plan["video_mode"] == "project_showcase"
        assert "https://github.com/aqua-org/good-repo" in plan["summary"]
        assert "https://github.com/aqua-org/good-repo" in plan["scenes"][-1]["visual_props"]["citation_label"]

        sources = plan["sources"]
        by_url = {s["url"]: s for s in sources if s.get("url")}
        assert by_url["https://github.com/aqua-org/good-repo"]["fetch_status"] == "fetched"
        assert by_url["https://github.com/aqua-org/good-repo"]["kind"] == "web_research"
        assert by_url["https://docs.example.org/broken-guide"]["fetch_status"] == "fallback"
        assert by_url["https://docs.example.org/broken-guide"]["fetch_error"]
        assert any(s["kind"] == "operator_topic" and s.get("fetch_status") == "manual" for s in sources)

    asyncio.run(scenario())


def test_reject_unsafe_reference_urls_and_private_ip_dns(settings, tmp_path, monkeypatch):
    app = create_app(settings, adapter=ControlledPlatform())
    headers = {"Authorization": "Bearer " + settings.admin_token}

    # Max 4 unique URLs after strip/dedupe
    args = PlanVideoScriptArgs(
        topic="测试链接去重",
        reference_urls=[
            "https://example.org/1",
            "",
            "https://example.org/1",
            "https://example.org/2",
            "https://example.org/3",
            "https://example.org/4",
        ],
    )
    assert len(args.reference_urls) == 4
    with pytest.raises(ValidationError):
        PlanVideoScriptArgs(
            topic="超限",
            reference_urls=[f"https://example.org/{i}" for i in range(7)],
        )

    bad_urls = [
        "http://example.org/plain-http",
        "https://user:secret@example.org/creds",
        "https://example.org:8443/custom-port",
        "https://localhost/admin",
        "https://127.0.0.1/internal",
        "https://10.0.0.8/lan",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/loopback",
    ]
    with TestClient(app, headers=headers) as client:
        app.state.runtime.remotion_skill.renders_root = tmp_path / "video_renders"
        for bad in bad_urls:
            resp = client.post(
                "/api/v1/operations/videos/plan",
                json={"topic": "测试非法链接拦截", "reference_urls": [bad]},
            )
            assert resp.status_code == 400, f"expected 400 for {bad}, got {resp.status_code}: {resp.text}"

    # DNS resolving to private IP via public_https_address must also raise ValueError
    async def fake_private_dns(_url):
        raise ValueError("网页解析到了非公网地址")

    monkeypatch.setattr(remotion_assets, "public_https_address", fake_private_dns)
    with pytest.raises(ValueError, match="非公网地址"):
        asyncio.run(fetch_reference_urls(["https://rebind.example.org/readme"]))


def test_long_brief_text_scans_full_content_for_credentials_owner_memory_and_dm(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    char = runtime.character
    runtime.unified_memory.add_memory("私有家庭住址与紧急联系人电话", scope="owner", character_id=char["id"])
    with runtime.store.db:
        runtime.store.db.execute(
            "INSERT INTO dm_inbox(platform,message_id,platform_user_id,content,character_id,received_at) VALUES(?,?,?,?,?,?)",
            ("bilibili", "dm_rich_1", "u_42", "私信机密约定周五晚在地铁口碰面", char["id"], 1234.0),
        )

    padding = "公开功能列表介绍与技术架构背景说明。" * 150  # > 2400 chars (well beyond 500-char excerpt)
    assert len(padding) > 2000
    skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)

    for leaked_tail, expected_msg in (
        ("sk-testsecretkey1234567890abcdef", "凭据"),
        ("顺便提一句私有家庭住址与紧急联系人电话", "owner"),
        ("附带一条私信机密约定周五晚在地铁口碰面", "私信"),
    ):
        with pytest.raises(ValueError, match=expected_msg):
            asyncio.run(
                plan_video_script(
                    skill,
                    {
                        "topic": "公开介绍测试",
                        "brief_text": f"{padding}\n{leaked_tail}",
                        "max_scenes": 2,
                    },
                )
            )


def test_video_modes_and_2_to_10_scenes_fallback(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)

        intents = {m: build_aqua_video_planner_intent(m) for m in ("commentary", "self_intro", "project_showcase", "explainer")}
        assert len(set(intents.values())) == 4
        assert "self_intro" in intents["self_intro"]
        assert "project_showcase" in intents["project_showcase"]
        assert "explainer" in intents["explainer"]

        for mode, count in (("self_intro", 2), ("explainer", 6), ("project_showcase", 10)):
            res = await plan_video_script(
                skill,
                {
                    "topic": f"{mode} 模式测试",
                    "video_mode": mode,
                    "brief_text": "第一点功能亮点。\n第二点自动同步。\n第三点本地运行安全。\n第四点支持导出字幕。\n第五点界面预览。",
                    "reference_urls": ["https://github.com/aqua-org/studio"] if mode == "project_showcase" else [],
                    "max_scenes": count,
                },
            ) if mode != "project_showcase" else None
            if mode == "project_showcase":
                skill.web_fetch_fn = lambda url: {"url": url, "title": "studio README", "excerpt": "Open source video studio."}
                res = await plan_video_script(
                    skill,
                    {
                        "topic": "Aqua Studio 开源展示",
                        "video_mode": "project_showcase",
                        "brief_text": "亮点一：音频驱动帧对齐。\n亮点二：六大预设模板。\n亮点三：本地 FFprobe 质检。",
                        "reference_urls": ["https://github.com/aqua-org/studio"],
                        "max_scenes": count,
                    },
                )
            assert res["plan"]["video_mode"] == mode
            assert len(res["plan"]["scenes"]) == count
            if mode == "project_showcase":
                assert "https://github.com/aqua-org/studio" in res["plan"]["summary"]
                assert "https://github.com/aqua-org/studio" in res["plan"]["scenes"][-1]["visual_props"]["citation_label"]

    asyncio.run(scenario())


def test_image_uploads_limit_mime_mediacache_and_project_preservation(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    headers = {"Authorization": "Bearer " + settings.admin_token}
    png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")
    jpg_b64 = base64.b64encode(_make_jpeg_bytes()).decode("ascii")

    with TestClient(app, headers=headers) as client:
        runtime = app.state.runtime
        runtime.remotion_skill.renders_root = tmp_path / "video_renders"
        runtime.cache.root = tmp_path

        planned = client.post(
            "/api/v1/operations/videos/plan",
            json={"topic": "带截图的视频测试", "video_mode": "explainer", "max_scenes": 3},
        )
        assert planned.status_code == 200
        job_id = planned.json()["job_id"]

        # Reject mismatched MIME/magic bytes
        bad_mime = client.post(
            f"/api/v1/operations/videos/{job_id}/uploads",
            json={"filename": "fake.jpg", "image_base64": png_b64, "mime_type": "image/jpeg"},
        )
        assert bad_mime.status_code == 400

        # Upload up to 12 valid images
        for idx in range(12):
            is_jpg = idx % 2 == 1
            up = client.post(
                f"/api/v1/operations/videos/{job_id}/uploads",
                json={
                    "filename": f"shot_{idx}.{'jpg' if is_jpg else 'png'}",
                    "image_base64": jpg_b64 if is_jpg else png_b64,
                    "mime_type": "image/jpeg" if is_jpg else "image/png",
                },
            )
            assert up.status_code == 200, up.text
            assert len(up.json()["uploads"]) == idx + 1

        # 13th upload is rejected
        extra_upload = client.post(
            f"/api/v1/operations/videos/{job_id}/uploads",
            json={"filename": "shot_12.png", "image_base64": png_b64, "mime_type": "image/png"},
        )
        assert extra_upload.status_code == 400

        detail = client.get(f"/api/v1/operations/videos/{job_id}").json()["job"]
        assert len(detail["uploads"]) == 12
        first_upload = detail["uploads"][0]
        assert first_upload["rel_path"].startswith("uploads/")

        fetched = client.get(first_upload["url"])
        assert fetched.status_code == 200
        assert fetched.headers["content-type"].startswith("image/png")

        # MediaCache video category accounts for uploaded files
        cache_video = runtime.cache.status()["categories"]["video"]
        assert cache_video["files"] >= 3
        assert cache_video["bytes"] > 0

        # Bind uploaded image to scene 2 and verify generate_remotion_project preserves it
        scenes = detail["plan"]["scenes"]
        scenes[1]["visual_props"]["uploaded_image_file"] = first_upload["rel_path"]
        patched = client.patch(
            f"/api/v1/operations/videos/{job_id}/plan",
            json={"scenes": scenes},
        )
        assert patched.status_code == 200

        gen = client.post(
            f"/api/v1/operations/videos/{job_id}/generate",
            json={"tts_mode": "dry_run"},
        )
        assert gen.status_code == 200

        proj_dir = Path(tmp_path / "video_renders" / job_id)
        assert (proj_dir / "public" / first_upload["rel_path"]).is_file()
        props = json.loads((proj_dir / "props.json").read_text(encoding="utf-8"))
        assert props["scenes"][1]["visual_props"]["uploaded_image_file"] == first_upload["rel_path"]
