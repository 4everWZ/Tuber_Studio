"""HTTP route tests for `/api/v1/operations/videos/*` Remotion Code-as-Video endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.app import create_app


def test_video_production_routes_full_lifecycle_and_media_serving(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    headers = {"Authorization": "Bearer " + settings.admin_token}

    with TestClient(app, headers=headers) as client:
        runtime = app.state.runtime
        runtime.remotion_skill.renders_root = tmp_path / "video_renders"

        # 1. List overview & check environment readiness structure (no secrets / raw user paths).
        listing = client.get("/api/v1/operations/videos")
        assert listing.status_code == 200
        body = listing.json()
        assert body["jobs"] == []
        assert body["policy"]["publish_mode"] == "preview_review"
        assert {"node", "ffmpeg", "ffprobe", "remotion_studio", "browser", "omni_llm", "omni_tts"} <= set(body["environment"])

        # 2. Policy update
        pol_res = client.put("/api/v1/operations/videos/policy", json={"default_fps": 60, "max_render_concurrency": 1})
        assert pol_res.status_code == 200
        assert pol_res.json()["policy"]["default_fps"] == 60
        assert pol_res.json()["policy"]["max_render_concurrency"] == 1

        # 3. Plan script (falls back cleanly when OfflineChat doesn't match VideoScriptPlan schema)
        plan_res = client.post(
            "/api/v1/operations/videos/plan",
            json={"topic": "测试 Remotion 管线接口", "fps": 30, "max_scenes": 2},
        )
        assert plan_res.status_code == 200
        planned = plan_res.json()
        job_id = planned["job_id"]
        assert planned["ok"] is True
        assert planned["planner_source"] == "fallback"
        assert planned["job"]["status"] == "script_planned"
        assert "project_dir" not in planned["job"]

        # 4. Edit metadata & dimensions via PATCH /plan
        patch_res = client.patch(
            f"/api/v1/operations/videos/{job_id}/plan",
            json={"title": "修订后的测试视频标题", "tags": ["Aqua", "测试"]},
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["job"]["title"] == "修订后的测试视频标题"

        # 5. Generate Remotion project + dry_run audio
        gen_res = client.post(
            f"/api/v1/operations/videos/{job_id}/generate",
            json={"tts_mode": "dry_run"},
        )
        assert gen_res.status_code == 200
        assert gen_res.json()["status"] == "project_ready"
        assert gen_res.json()["total_frames"] > 0

        # 6. Inspect generated code/props/subtitles/audio media endpoints
        for kind, expected_ct in (
            ("props", "application/json"),
            ("manifest", "application/json"),
            ("subtitles", "text/plain"),
            ("subtitles_vtt", "text/vtt"),
            ("code", "text/plain"),
            ("audio", "audio/wav"),
        ):
            media_res = client.get(f"/api/v1/operations/videos/{job_id}/media/{kind}")
            assert media_res.status_code == 200, kind
            assert media_res.headers["content-type"].startswith(expected_ct)
            assert "no-store" in media_res.headers["cache-control"]

        # 7. Render (sync dry_run)
        ren_res = client.post(
            f"/api/v1/operations/videos/{job_id}/render",
            json={"dry_run": True, "background": False},
        )
        assert ren_res.status_code == 200
        assert ren_res.json()["ok"] is True
        assert ren_res.json()["status"] == "ready_for_review"
        assert ren_res.json()["qc_report"]["passed"] is True

        # Video MP4 is now servable via /media/video
        vid_res = client.get(f"/api/v1/operations/videos/{job_id}/media/video")
        assert vid_res.status_code == 200
        assert vid_res.headers["content-type"].startswith("video/mp4")

        # 8. Stage for preview_review (zero platform writes)
        pub_res = client.post(
            f"/api/v1/operations/videos/{job_id}/publish",
            json={"publish_mode": "preview_review", "approved": False},
        )
        assert pub_res.status_code == 200
        assert pub_res.json()["action"] == "staged_for_review"

        # 9. Detail endpoint & deletion
        detail_res = client.get(f"/api/v1/operations/videos/{job_id}")
        assert detail_res.status_code == 200
        assert "video" in detail_res.json()["job"]["media"]

        del_res = client.delete(f"/api/v1/operations/videos/{job_id}")
        assert del_res.status_code == 200
        assert del_res.json()["deleted"] is True
        assert client.get(f"/api/v1/operations/videos/{job_id}").status_code == 404


def test_video_production_background_render_route(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    headers = {"Authorization": "Bearer " + settings.admin_token}

    with TestClient(app, headers=headers) as client:
        runtime = app.state.runtime
        runtime.remotion_skill.renders_root = tmp_path / "video_renders"

        job_id = client.post("/api/v1/operations/videos/plan", json={"topic": "后台渲染测试", "max_scenes": 2}).json()["job_id"]
        assert client.post(f"/api/v1/operations/videos/{job_id}/generate", json={"tts_mode": "dry_run"}).status_code == 200

        ren = client.post(f"/api/v1/operations/videos/{job_id}/render", json={"dry_run": True, "background": True})
        assert ren.status_code == 200
        assert ren.json()["queued"] is True

        # Wait briefly for background dry_run render task to finish
        for _ in range(30):
            detail = client.get(f"/api/v1/operations/videos/{job_id}").json()["job"]
            if detail["status"] == "ready_for_review":
                break
            asyncio.run(asyncio.sleep(0.05))
        assert detail["status"] == "ready_for_review"
        assert (Path(tmp_path) / "video_renders" / job_id / "output.mp4").is_file()
