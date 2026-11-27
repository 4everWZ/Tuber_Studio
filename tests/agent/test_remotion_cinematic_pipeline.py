"""Tests for the cinematic Remotion pipeline: editorial triage, deep research, two-stage planner, audio/QC, and routes."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.chat.router.prompt_builder import _build_router_messages
from tuber_studio.agent.skills.remotion_audio_qc import (
    extract_audio_envelope,
    extract_scene_contact_sheet,
    prepare_bgm_and_sfx_kit,
    run_vlm_visual_review,
)
from tuber_studio.agent.skills.remotion_planner import (
    normalize_cinematic_scene_defaults,
    resolve_duration_and_scenes,
)
from tuber_studio.agent.skills.remotion_research import (
    capture_workbench_screenshots,
    fetch_deep_reference_materials,
)
from tuber_studio.agent.skills.remotion_video import (
    PlanVideoScriptArgs,
    RemotionVideoSkill,
    assert_public_video_sources,
    generate_remotion_project,
    plan_video_script,
)
from tuber_studio.app import create_app
from tuber_studio.core.editorial import assess_editorial_channel
from tuber_studio.operations.channels.dynamics import dynamic_decision_messages


def _make_png_bytes(color: tuple[int, int, int] = (56, 189, 248), size: tuple[int, int] = (64, 36)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_wav_bytes(duration_seconds: float, *, amplitude: int = 4000, sample_rate: int = 16000) -> bytes:
    nframes = max(160, int(round(duration_seconds * sample_rate)))
    pcm = b"".join(struct.pack("<h", int(amplitude * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))) for i in range(nframes))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        wf.writeframes(pcm)
    return buf.getvalue()


def test_operations_recap_uses_public_receipt_counts_and_freezes_the_scheduled_draft(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
    skill.configure_policy({"default_width": 1280, "default_height": 720})
    source = {
        "source_id": "public-reply:900",
        "kind": "public_comment",
        "title": "原楼回复",
        "url": "https://example.com/comment",
    }
    highlight = {"title": "一次接话", "text": "我看完了，想再听一点幕后。", "source_id": source["source_id"]}
    brief = {
        "title": "一段运营回放",
        "period_label": "2026-11-20 至 2026-11-22",
        "watched_videos": 12,
        "accepted_comments": 2,
        "public_comments": 0,
        "public_dynamics": 3,
        "sources": [source],
        "highlights": [highlight],
        "reflection": "这次我想多看一点原楼。",
    }
    saved = skill.plan_operations_recap(brief, job_key="fixture-window")
    repeat = skill.plan_operations_recap(brief | {"watched_videos": 20}, job_key="fixture-window")
    draft = skill.get_job(saved["job_id"])
    assert saved == repeat
    assert draft["publish_mode"] == "preview_review" and draft["status"] == "script_planned"
    assert len(skill.list_jobs()) == 1
    assert (saved["plan"]["width"], saved["plan"]["height"]) == (1280, 720)
    title, counts, excerpt, reflection = saved["plan"]["scenes"]
    assert title["visual_props"]["subtitle"] == brief["period_label"]
    assert [item["value"] for item in counts["visual_props"]["chart_items"]] == [12, 2, 0, 3]
    assert [line["reveal_step"] for line in counts["lines"]] == [0, 1, 2, 3]
    assert counts["lines"][1]["text"] == "评论拿到了 2 条提交回执。"
    assert counts["lines"][2]["text"] == "从公开页读回的评论有 0 条。"
    assert excerpt["visual_props"]["subtitle"] == highlight["text"]
    assert excerpt["visual_props"]["citation_label"] == source["title"]
    assert reflection["heading"] == "我的复盘感想"
    assert source["url"] in saved["plan"]["summary"]
    with pytest.raises(ValueError, match="公开来源"):
        skill.plan_operations_recap(brief | {"sources": []})
    with pytest.raises(ValueError, match="超过取得回执"):
        skill.plan_operations_recap(brief | {"public_comments": 3})
    with pytest.raises(ValueError, match="私"):
        skill.plan_operations_recap(brief | {
            "sources": [source | {"source_id": "dm:900"}],
            "highlights": [highlight | {"source_id": "dm:900"}],
        })


def test_recap_render_settles_failure_and_blocks_competing_work(settings, tmp_path):
    async def scenario():
        runtime = create_app(settings, adapter=ControlledPlatform()).state.runtime
        entered, release = asyncio.Event(), asyncio.Event()

        async def successful_renderer(cmd, _cwd, _timeout):
            Path(cmd[cmd.index("render") + 2]).write_bytes(b"fixture-output")
            return 0, "", ""

        async def stalled_renderer(_cmd, _cwd, _timeout):
            entered.set()
            await release.wait()
            raise TimeoutError("渲染超时")

        skill = RemotionVideoSkill(
            runtime, runtime.store, renders_root=tmp_path, tts_fn=lambda *_: _make_wav_bytes(0.45),
            subprocess_runner=successful_renderer,
            ffprobe_runner=lambda _path, duration: {
                "has_video": True, "has_audio": True, "duration_seconds": duration,
                "width": 1920, "height": 1080,
            },
            render_semaphore=asyncio.Semaphore(1),
        )
        job = skill.plan_operations_recap({
            "title": "运营回放",
            "period_label": "复盘时段",
            "watched_videos": 2,
            "public_comments": 1,
            "public_dynamics": 0,
        })["job_id"]
        await skill.generate_remotion_project({"job_id": job})
        assert (await skill.render_remotion_video({"job_id": job}))["ok"]
        skill.subprocess_runner = stalled_renderer
        pending = asyncio.create_task(skill.render_remotion_video({"job_id": job}))
        await entered.wait()
        for step in (
            skill.generate_remotion_project, skill.render_remotion_video, skill.stage_or_publish_bilibili_video
        ):
            with pytest.raises(ValueError, match="正在.*渲染"):
                await step({"job_id": job})
        release.set()
        with pytest.raises(TimeoutError, match="渲染超时"):
            await pending
        record = skill.get_job(job)
        assert record["status"] == "render_failed"
        assert record["qc_report"] is None
        with pytest.raises(ValueError, match="尚未通过"):
            await skill.stage_or_publish_bilibili_video({"job_id": job, "approved": True})
        skill.subprocess_runner = successful_renderer
        assert (await skill.render_remotion_video({"job_id": job}))["status"] == "ready_for_review"

    asyncio.run(scenario())


def test_editorial_channel_triage_and_prompt_boundaries(settings):
    comment_fit = assess_editorial_channel("这期片段好笑", candidate_bvids=["BV1xx411c7mD"])
    assert comment_fit.recommended_channel == "comment"

    dyn_fit = assess_editorial_channel("今天干完白饭切低功耗保鲜摸鱼一下")
    assert dyn_fit.recommended_channel == "dynamic"

    vlog_fit = assess_editorial_channel(
        "LoRA 微调前后对比实验日志",
        brief_text="记录数据集清洗、rank=16 参数配置与微调前后回复对比效果。" * 6,
    )
    assert vlog_fit.recommended_channel == "video"
    assert vlog_fit.video_mode == "daily_vlog"
    assert vlog_fit.duration_preset == "standard"

    roundup_fit = assess_editorial_channel(
        "本周刷到的几部硬核视频盘点",
        candidate_bvids=["BV1aa1111111", "BV1bb2222222"],
    )
    assert roundup_fit.recommended_channel == "video"
    assert roundup_fit.video_mode == "watched_roundup"

    deep_fit = assess_editorial_channel(
        "Tuber Studio 源码级深度拆解",
        brief_text="架构与时间轴原理说明。" * 200,
        include_workspace_docs=True,
    )
    assert deep_fit.recommended_channel == "video"
    assert deep_fit.video_mode == "project_showcase"
    assert deep_fit.duration_preset == "deep_dive"

    router_msgs = _build_router_messages(
        {"name": "Aqua"},
        SimpleNamespace(message="帮我做一期 LoRA 微调前后对比视频"),
        {"candidates": [], "recent_dialogue": []},
    )
    sys_prompt = router_msgs[0]["content"]
    assert "video_production" in sys_prompt
    assert "prepare_publication" in sys_prompt

    app = create_app(settings, adapter=ControlledPlatform())
    dyn_msgs = dynamic_decision_messages(
        app.state.runtime.character,
        [],
        [],
        current_time="2026-11-26T12:00:00Z",
    )
    assert "视频成片" in dyn_msgs[0]["content"]


def test_deep_research_allowlist_and_loopback_capture_guard(tmp_path):
    ws = tmp_path / "repo"
    (ws / "docs").mkdir(parents=True)
    (ws / "config").mkdir(parents=True)
    (ws / ".git").mkdir(parents=True)
    (ws / "src/tuber_studio/core").mkdir(parents=True)
    (ws / "README.md").write_text("# Tuber Studio\n公开工作坊说明文档。", encoding="utf-8")
    (ws / "docs/arch.md").write_text("# 架构\n公开架构细节说明。", encoding="utf-8")
    (ws / ".env").write_text("API_KEY=SECRET_DO_NOT_LEAK", encoding="utf-8")
    (ws / "config/secrets.json").write_text('{"sessdata": "SECRET_COOKIE"}', encoding="utf-8")
    (ws / ".git/config").write_text("SECRET_GIT_TOKEN", encoding="utf-8")

    async def fake_fetch(url: str) -> dict:
        return {
            "url": url,
            "title": "Public Guide",
            "excerpt": "Long public guide excerpt. " + ("detail " * 250),
            "truncated": False,
        }

    bundle = asyncio.run(
        fetch_deep_reference_materials(
            ["https://example.org/guide"],
            fetch_fn=fake_fetch,
            include_workspace_docs=True,
            workspace_root=ws,
            topic="架构展示",
            video_mode="project_showcase",
            candidates=[{"bvid": "BV1cc3333333", "title": "候选视频", "summary": "摘要要点"}],
        )
    )
    joined_pages = json.dumps(bundle["pages"], ensure_ascii=False)
    assert "公开工作坊说明文档" in joined_pages
    assert "SECRET_DO_NOT_LEAK" not in joined_pages
    assert "SECRET_COOKIE" not in joined_pages
    assert "SECRET_GIT_TOKEN" not in joined_pages
    assert any(m.startswith("tuber_studio.core") for m in bundle["modules"])
    assert len(bundle["pages"][0]["excerpt"]) > 1000
    assert bundle["watched_digest"][0]["bvid"] == "BV1cc3333333"

    blocked_capture = asyncio.run(capture_workbench_screenshots(tmp_path / "shots", base_url="https://example.com"))
    assert blocked_capture["ok"] is False
    assert "127.0.0.1" in blocked_capture["error"]


def test_public_isolation_scans_new_cinematic_scene_fields(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime
    char_id = runtime.character["id"]
    runtime.unified_memory.add_memory("主人私密家庭住址与银行卡信息", scope="owner", character_id=char_id)
    with runtime.store.db:
        runtime.store.db.execute(
            "INSERT INTO dm_inbox(platform,message_id,platform_user_id,content,character_id,received_at) VALUES(?,?,?,?,?,?)",
            ("bilibili", "dm_cine_1", "u_77", "私信机密约定周六线下碰头", char_id, 1000.0),
        )

    base_source = [{"source_id": "topic:safe", "kind": "operator_topic", "scope": "public", "title": "公开话题"}]
    leaks = (
        ("chapter_title", {"chapter_title": "主人私密家庭住址与银行卡信息"}, "owner"),
        ("stat_callout", {"visual_props": {"stat_callout": "私信机密约定周六线下碰头"}}, "私信"),
        ("diagram_nodes", {"visual_props": {"diagram_nodes": [{"id": "n1", "label": "主人私密家庭住址与银行卡信息"}]}}, "owner"),
        ("code_steps", {"visual_props": {"code_steps": [{"title": "s1", "code_snippet": "sk-secretkey1234567890abcd"}]}}, "凭据"),
        ("ui_focus_regions", {"visual_props": {"ui_focus_regions": [{"x": 10, "y": 20, "label": "私信机密约定周六线下碰头"}]}}, "私信"),
        ("visual_focus", {"lines": [{"text": "正常台词", "visual_focus": "主人私密家庭住址与银行卡信息"}]}, "owner"),
    )
    for _field, override, match_msg in leaks:
        scene = {"scene_id": "s1", "template": "ArchitectureFlow", "heading": "公开标题", "lines": [{"text": "正常台词"}]} | override
        bad_plan = {"title": "测试", "summary": "公开简介", "tags": ["Aqua"], "scenes": [scene], "sources": base_source}
        with pytest.raises(ValueError, match=match_msg):
            assert_public_video_sources(bad_plan, store=runtime.store, unified_memory=runtime.unified_memory, character_id=char_id)


def test_two_stage_director_duration_presets_and_cinematic_fallback(settings, tmp_path, monkeypatch):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime

        assert resolve_duration_and_scenes("commentary", None, None) == ("short", 5)
        assert resolve_duration_and_scenes("daily_vlog", None, None) == ("standard", 9)
        assert resolve_duration_and_scenes("explainer", "deep_dive", None) == ("deep_dive", 14)
        assert resolve_duration_and_scenes("explainer", "deep_dive", 3) == ("deep_dive", 3)

        stage_calls = []

        class FakeTwoStageLLM:
            async def generate_json(self, messages, schema, *, max_tokens=2048):
                payload = json.loads(messages[1]["content"])
                stage_calls.append(payload.get("director_stage"))
                if payload.get("director_stage") == "A_outline":
                    return {
                        "title": "LoRA 微调双阶段导演实测",
                        "summary": "两阶段生成的电影级分镜计划。",
                        "tags": ["Aqua", "LoRA"],
                        "chapters": [
                            {"chapter_number": 1, "chapter_title": "动机与背景", "theme": "cinema", "scene_templates": ["TitleCard", "ArchitectureFlow"]},
                            {"chapter_number": 2, "chapter_title": "实测对比", "theme": "dark_tech", "scene_templates": ["ChartCompare", "KineticStatement"]},
                        ],
                        "target_scene_count": 4,
                    }
                return {
                    "title": "LoRA 微调双阶段导演实测",
                    "summary": "两阶段生成的电影级分镜计划。",
                    "tags": ["Aqua", "LoRA"],
                    "scenes": [
                        {
                            "scene_id": "s1", "template": "TitleCard", "heading": "LoRA 微调到底值不值",
                            "chapter_title": "动机与背景", "lines": [{"text": "今天实测一下微调前后的语气变化！", "emphasis": ["语气变化"]}],
                        },
                        {
                            "scene_id": "s2", "template": "ArchitectureFlow", "heading": "训练数据流",
                            "chapter_title": "动机与背景",
                            "visual_props": {
                                "diagram_nodes": [{"id": "raw", "label": "原始语料"}, {"id": "lora", "label": "LoRA 权重"}],
                                "diagram_edges": [{"source": "raw", "target": "lora", "label": "SFT"}],
                            },
                            "lines": [{"text": "先把语料洗干净，再叠一层轻量权重。"}],
                        },
                        {
                            "scene_id": "s3", "template": "KineticStatement", "heading": "核心结论",
                            "chapter_title": "实测对比",
                            "visual_props": {"stat_callout": "+42%"},
                            "lines": [{"text": "少即是多，干净样本比堆步数更关键。"}],
                        },
                    ],
                    "sources": [{"source_id": "topic:lora", "kind": "operator_topic", "scope": "public", "title": "LoRA 实测"}],
                }

        runtime.operation_llm = FakeTwoStageLLM()
        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
        res = await plan_video_script(skill, {"topic": "LoRA 微调实测", "video_mode": "daily_vlog", "duration_preset": "standard"})
        assert res["ok"] is True
        assert stage_calls == ["A_outline", "B_scenes"]
        assert res["planner_source"] == "llm"
        assert res["editorial_fit"]["video_mode"] == "daily_vlog"
        assert res["plan"]["duration_preset"] == "standard"
        assert res["plan"]["scenes"][1]["visual_props"]["diagram_nodes"][0]["id"] == "raw"
        assert res["plan"]["scenes"][2]["avatar_layout"] == "hidden"

        # Fallback across daily_vlog & watched_roundup with standard duration emits rich templates
        monkeypatch.setattr(type(runtime), "operation_llm", property(lambda _s: None))
        fb_skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
        args_default = PlanVideoScriptArgs(topic="近期 LoRA 与工坊开发日志", video_mode="daily_vlog")
        assert args_default.max_scenes is None
        fb_res = await plan_video_script(fb_skill, args_default)
        assert fb_res["planner_source"] == "fallback"
        assert len(fb_res["plan"]["scenes"]) == 9
        tpls = {s["template"] for s in fb_res["plan"]["scenes"]}
        assert {"TitleCard", "ChapterBumper", "ChartCompare"} <= tpls
        for scene in fb_res["plan"]["scenes"]:
            if scene["template"] == "ChartCompare":
                assert scene["visual_props"]["chart_items"] == []
                assert any("没有可直接比较的实测数字" in line["text"] for line in scene["lines"])
            if scene["template"] == "KineticStatement":
                assert scene["visual_props"]["stat_callout"] == ""

        normalized = normalize_cinematic_scene_defaults({"video_mode": "explainer", "scenes": [{"scene_id": "s1", "template": "ArchitectureFlow", "heading": "架构", "lines": [{"text": "看看数据流"}]}]})
        assert normalized["scenes"][0]["avatar_layout"] == "compact"
        assert len(normalized["scenes"][0]["visual_props"]["diagram_nodes"]) >= 3

    asyncio.run(scenario())


def test_audio_envelope_sfx_kit_mp4_upload_autobind_and_contact_sheet(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)

        # Unit check audio envelope + BGM/SFX kit
        wav_path = tmp_path / "sample.wav"
        wav_path.write_bytes(_make_wav_bytes(1.0))
        env = extract_audio_envelope(wav_path, fps=30, total_frames=30)
        assert len(env) == 30
        assert all(0.0 <= v <= 1.0 for v in env)
        assert max(env) > 0.5

        bgm_rel, sfx_map = prepare_bgm_and_sfx_kit(settings.root, tmp_path / "audio_kit", duration_seconds=5.0)
        assert bgm_rel == "audio/bgm.wav"
        assert set(sfx_map) == {"transition", "whoosh", "pop", "chime", "chapter_hit"}
        for rel in sfx_map.values():
            assert (tmp_path / "audio_kit" / Path(rel).name).is_file()

        # Plan with 2 scenes + upload an MP4 clip + generate project -> auto-binds uploaded_video_file
        planned = await plan_video_script(skill, {"topic": "工作台演示录像测试", "video_mode": "project_showcase", "max_scenes": 3})
        job_id = planned["job_id"]
        mp4_b64 = base64.b64encode(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom_clip").decode("ascii")
        up = skill.upload_job_image(job_id, filename="demo_walkthrough.mp4", image_base64=mp4_b64, mime_type="video/mp4")
        assert up["ok"] is True
        assert up["upload"]["kind"] == "video"

        with pytest.raises(ValueError, match="MP4"):
            skill.upload_job_image(job_id, filename="bad.mp4", image_base64=base64.b64encode(b"not-a-mp4-clip").decode("ascii"), mime_type="video/mp4")

        gen = await generate_remotion_project(skill, {"job_id": job_id, "tts_mode": "dry_run"})
        proj_dir = Path(gen["project_dir"])
        props = json.loads((proj_dir / "props.json").read_text(encoding="utf-8"))
        assert len(props["audioEnvelope"]) == props["totalFrames"]
        assert set(props["sfxFiles"]) == {"transition", "whoosh", "pop", "chime", "chapter_hit"}
        assert props["duration_preset"] == "standard"
        assert any(s["visual_props"].get("uploaded_video_file") == "uploads/demo_walkthrough.mp4" for s in props["scenes"])
        assert "reveal_step" in props["cues"][0] and "line_index_in_scene" in props["cues"][0]

        # Extract scene contact sheet via injected runner
        fake_mp4 = proj_dir / "output.mp4"
        fake_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")

        async def fake_ffmpeg(cmd, *, cwd: Path, timeout: float):
            Path(cmd[-1]).write_bytes(_make_png_bytes((14, 165, 233)))
            return 0, "", ""

        sheet_path, frames = await extract_scene_contact_sheet(
            fake_mp4,
            props["scenes"],
            fps=30,
            project_dir=proj_dir,
            run_exec=fake_ffmpeg,
            ffmpeg_bin="ffmpeg",
        )
        assert sheet_path is not None and sheet_path.is_file()
        assert len(frames) == len(props["scenes"])
        resolved_sheet, media_type = skill.resolve_job_media(job_id, "contact_sheet")
        assert resolved_sheet == sheet_path.resolve() and media_type == "image/png"

        # Non-blocking VLM review degrades gracefully when vision provider is unconfigured, and runs when configured
        no_vlm = await run_vlm_visual_review(runtime, sheet_path, frames, planned["plan"])
        assert no_vlm["ok"] is True and no_vlm["reviewed"] is False

        class FakeVisionLLM:
            async def generate_json(self, _messages, _schema, *, max_tokens=1024):
                return {
                    "overall_score": 88,
                    "monotony_risk": "low",
                    "layout_issues": [],
                    "suggestions": ["主图对比度良好"],
                    "scenes": [{"scene_index": 1, "verdict": "pass", "note": "构图清晰"}],
                }

        runtime.vision_llm = FakeVisionLLM()
        reviewed = await skill.review_job_visuals(job_id)
        assert reviewed["ok"] is True and reviewed["reviewed"] is True and reviewed["overall_score"] == 88

    asyncio.run(scenario())


def test_cinematic_http_routes_pitch_upload_video_capture_and_visual_review(settings, tmp_path):
    app = create_app(settings, adapter=ControlledPlatform())
    headers = {"Authorization": "Bearer " + settings.admin_token}

    with TestClient(app, headers=headers) as client:
        runtime = app.state.runtime
        runtime.remotion_skill.renders_root = tmp_path / "video_renders"

        # 1. Pitch ideas endpoint
        pitch_res = client.post("/api/v1/operations/videos/pitch-ideas", json={"brief_hint": "LoRA 微调实录", "max_ideas": 4})
        assert pitch_res.status_code == 200, pitch_res.text
        ideas = pitch_res.json()["ideas"]
        assert len(ideas) >= 2
        assert any(item["channel"] == "video" for item in ideas)
        assert any(item["channel"] == "dynamic" for item in ideas)

        # 2. Plan + patch duration_preset + upload webm clip
        plan_res = client.post(
            "/api/v1/operations/videos/plan",
            json={"topic": "HTTP 路由全流程验证", "video_mode": "daily_vlog", "duration_preset": "short", "max_scenes": 3},
        )
        assert plan_res.status_code == 200
        job_id = plan_res.json()["job_id"]
        assert "editorial_fit" in plan_res.json()

        patch_res = client.patch(f"/api/v1/operations/videos/{job_id}/plan", json={"duration_preset": "deep_dive"})
        assert patch_res.status_code == 200
        assert patch_res.json()["job"]["plan"]["duration_preset"] == "deep_dive"

        webm_b64 = base64.b64encode(b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01webm_sample").decode("ascii")
        up_res = client.post(
            f"/api/v1/operations/videos/{job_id}/uploads",
            json={"filename": "clip.webm", "image_base64": webm_b64, "mime_type": "video/webm"},
        )
        assert up_res.status_code == 200, up_res.text
        assert up_res.json()["upload"]["kind"] == "video"

        # 3. capture-ui rejects non-loopback URLs gracefully
        cap_res = client.post(
            f"/api/v1/operations/videos/{job_id}/capture-ui",
            json={"base_url": "https://evil.example.com", "max_captures": 2},
        )
        assert cap_res.status_code == 200
        assert cap_res.json()["ok"] is False
        assert len(cap_res.json()["uploads"]) == 1

        # 4. Generate + serve contact_sheet + visual-review route
        assert client.post(f"/api/v1/operations/videos/{job_id}/generate", json={"tts_mode": "dry_run"}).status_code == 200
        proj_dir = tmp_path / "video_renders" / job_id
        (proj_dir / "contact_sheet.png").write_bytes(_make_png_bytes())

        sheet_res = client.get(f"/api/v1/operations/videos/{job_id}/media/contact_sheet")
        assert sheet_res.status_code == 200
        assert sheet_res.headers["content-type"].startswith("image/png")

        rev_res = client.post(f"/api/v1/operations/videos/{job_id}/visual-review")
        assert rev_res.status_code == 200
        assert rev_res.json()["ok"] is True
        assert "monotony_risk" in rev_res.json()
