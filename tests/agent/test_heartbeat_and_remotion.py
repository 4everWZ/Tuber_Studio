"""Unit & integration tests for Unified Always-On Heartbeat, EventBus, and Remotion Video Skill."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import wave
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from tests.support.operations_helpers import BV, ControlledPlatform
from tuber_studio.agent.heartbeat import AgentEventBus, HeartbeatEvent, HeartbeatService, sanitize_pulse_text
from tuber_studio.agent.skills import (
    RemotionVideoSkill,
    assert_public_video_sources,
    execute_remotion_tool,
    generate_remotion_project,
    plan_video_script,
    register_remotion_tools,
    remotion_tool_schemas,
    render_remotion_video,
    stage_or_publish_bilibili_video,
)
from tuber_studio.app import create_app
from tuber_studio.core.store import dump
from tuber_studio.operations.facade import canonical_intent, select_video_candidates


def _make_wav_bytes(duration_seconds: float, sample_rate: int = 16000) -> bytes:
    import io
    import math
    import struct

    nframes = max(160, int(round(duration_seconds * sample_rate)))
    pcm = b"".join(struct.pack("<h", int(1800 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))) for i in range(nframes))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def test_event_bus_dedup_cooldown_and_handler_isolation():
    async def scenario():
        clock_now = [1000.0]
        bus = AgentEventBus(default_cooldown_seconds=60.0, dedup_window_seconds=300.0, clock=lambda: clock_now[0])
        seen: list[str] = []

        def broken_handler(_ev):
            raise RuntimeError("boom")

        async def healthy_handler(ev: HeartbeatEvent):
            seen.append(f"{ev.kind}:{ev.title}")

        bus.subscribe("hot_topic", broken_handler)
        bus.subscribe("*", healthy_handler)

        ev1 = HeartbeatEvent(
            kind="hot_topic",
            source="facade",
            dedup_key="hot:BV1xx",
            title="视频热点",
            summary="一条公开视频测试",
            payload={"bvid": BV},
        )
        r1 = await bus.publish(ev1)
        assert r1.accepted is True
        assert r1.reason == "emitted"
        assert r1.handler_errors == ["RuntimeError"]
        assert seen == ["hot_topic:视频热点"]

        # Same payload inside dedup window is suppressed.
        clock_now[0] = 1010.0
        r2 = await bus.publish(ev1)
        assert r2.accepted is False
        assert r2.reason == "deduplicated"

        # Changed payload inside cooldown is blocked by cooldown.
        ev_changed = ev1.model_copy(update={"title": "视频热点更新"})
        clock_now[0] = 1020.0
        r3 = await bus.publish(ev_changed)
        assert r3.accepted is False
        assert r3.reason == "cooldown"

        # Advancing past cooldown allows re-emission.
        clock_now[0] = 1075.0
        r4 = await bus.publish(ev_changed)
        assert r4.accepted is True
        assert len(seen) == 2

    asyncio.run(scenario())


def test_heartbeat_tick_curiosity_sanitization_and_rate_limits(settings):
    async def scenario():
        clock_now = [1_760_000_000.0]
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        store = runtime.store
        browsing = runtime.browsing

        # Seed a watched popular candidate video & an uncertain DM draft for facade snapshot.
        with store.db:
            store.db.execute(
                "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    "",
                    store.character()["id"],
                    BV,
                    dump(
                        {
                            "metadata": {
                                "bvid": BV,
                                "title": "【科普】Remotion 代码生成视频实战",
                                "tags": ["Remotion", "视频工程"],
                                "description": "用 TypeScript 模板驱动音画同步渲染。",
                            },
                            "parts": [{"segments": [{"text": "先生成语音再算出精准帧数"}]}],
                        }
                    ),
                    dump({"status": "completed"}),
                    clock_now[0],
                ),
            )
            store.db.execute(
                "INSERT INTO browsing_tasks(id,bvid,status,payload,updated_at,created_at) VALUES(?,?,?,?,?,?)",
                (
                    "task_hot_1",
                    BV,
                    "selected",
                    dump({"sources": [{"source": "popular"}], "title": "【科普】Remotion 代码生成视频实战"}),
                    clock_now[0],
                    clock_now[0],
                ),
            )

        candidates = select_video_candidates(browsing, store=store, limit=5)
        assert candidates and candidates[0]["bvid"] == BV

        seen_prompts = []
        research_calls = []

        async def fake_search(query: str):
            research_calls.append(query)
            return ["http://insecure.example.com/skip", "https://docs.remotion.dev/render"]

        async def fake_fetch(url: str):
            return {"url": url, "title": "Remotion CLI Docs", "excerpt": "Remotion renders React components deterministically per frame."}

        async def fake_brief(messages, _char):
            seen_prompts.append(messages)
            # Deliberately emit retired label + <think> + raw traceback to test strict sanitization.
            return (
                "<think>internal thoughts</think>"
                "工坊号主，我刚看到 Remotion 视频工程热点，顺手查了官方文档。"
                "\nTraceback (most recent call last):\n  File \"x.py\", line 12\n"
                "SESSDATA=supersecret123"
            )

        delivered_pulses = []
        hb = HeartbeatService(
            runtime,
            store,
            clock=lambda: clock_now[0],
            web_search_fn=fake_search,
            web_fetch_fn=fake_fetch,
            brief_generator=fake_brief,
            on_pulse=lambda p: delivered_pulses.append(p),
        )
        hb.configure({"pulse_cooldown_seconds": 300.0, "max_pulses_per_day": 1, "event_cooldown_seconds": 60.0})

        res1 = await hb.tick()
        assert res1["ok"] is True
        assert res1["emitted"] >= 1
        assert res1["pulse"] is not None
        assert research_calls, "curiosity web search must fire on hot_topic"
        brief_text = res1["pulse"]["brief"]
        assert "工坊号主" not in brief_text
        assert "Traceback" not in brief_text
        assert "SESSDATA" not in brief_text
        assert "<think>" not in brief_text
        assert len(delivered_pulses) == 1
        assert seen_prompts and "Aqua" in json.dumps(seen_prompts[0], ensure_ascii=False)

        # Non-urgent discovery blip inside pulse_cooldown is blocked, while 412 risk cooldown is urgent and penetrates.
        clock_now[0] += 30.0
        res_non_urgent = await hb.tick(
            snapshot={
                "account_id": "",
                "control_state": "running",
                "discovery": {"state": "running", "error": "暂时波动"},
                "inbox": {"pending_events": 0, "dm_pending": 0},
                "dynamics": {"pending_review": 0, "uncertain": 0},
                "writes": {"uncertain_count": 0},
                "hot_topics": [],
            }
        )
        assert res_non_urgent["rate_limited"] == "pulse_cooldown"
        assert res_non_urgent["pulse"] is None

        res2 = await hb.tick(
            snapshot={
                "account_id": "",
                "control_state": "running",
                "discovery": {"state": "running", "error": "412风控冷却"},
                "inbox": {"pending_events": 0, "dm_pending": 0},
                "dynamics": {"pending_review": 0, "uncertain": 0},
                "writes": {"uncertain_count": 0},
                "hot_topics": [],
            }
        )
        assert res2["rate_limited"] is None
        assert res2["pulse"] is not None
        assert res2["pulse"]["severity"] == "urgent"

        # Past pulse_cooldown, max_pulses_per_day=1 blocks non-urgent emission cleanly.
        clock_now[0] += 400.0
        res3 = await hb.tick(
            snapshot={
                "account_id": "",
                "control_state": "running",
                "discovery": {"state": "running", "error": "连接超时"},
                "inbox": {"pending_events": 0, "dm_pending": 0},
                "dynamics": {"pending_review": 0, "uncertain": 0},
                "writes": {"uncertain_count": 0},
                "hot_topics": [],
            }
        )
        assert res3["rate_limited"] == "daily_pulse_limit"

        # Setting max_pulses_per_day=0 means unlimited per workspace contract!
        hb.configure({"max_pulses_per_day": 0, "pulse_cooldown_seconds": 10.0})
        clock_now[0] += 30.0
        res4 = await hb.tick(
            snapshot={
                "account_id": "",
                "control_state": "running",
                "discovery": {"state": "running", "error": None},
                "inbox": {"pending_events": 0, "dm_pending": 0},
                "dynamics": {"pending_review": 0, "uncertain": 1},
                "writes": {"uncertain_count": 1},
                "hot_topics": [],
            }
        )
        assert res4["rate_limited"] is None
        assert res4["pulse"] is not None
        assert len(hb.list_pulses()) == 3
        assert sanitize_pulse_text('{"brief": "工坊号主，这是清洗后文本"}', owner_label="你") == "你，这是清洗后文本"

        # Curiosity research must reject unrelated foreign-language forum pages for CJK queries while accepting relevant matches.
        async def mixed_search(query: str):
            return [
                "https://forum.example.fr/youtube-hotmail",
                "https://news.example.cn/jetson-local-ai",
            ]

        async def mixed_fetch(url: str):
            if "forum.example.fr" in url:
                return {
                    "url": url,
                    "title": "Connexion Youtube avec Hotmail",
                    "excerpt": "Bonjour, je cherche comment connecter mon compte Hotmail sur Youtube.",
                }
            return {
                "url": url,
                "title": "本地 Jetson 运行 AI 实测",
                "excerpt": "实测本地 Jetson 运行视频吐槽模型与散热控制表现。",
            }

        hb._web_search_fn = mixed_search
        hb._web_fetch_fn = mixed_fetch
        filtered = await hb.research_topic("本地 Jetson 视频吐槽", bvid="BV1testFilter", bypass_cooldown=True)
        assert filtered["ok"] is True
        assert len(filtered["sources"]) == 1
        assert "forum.example.fr" not in filtered["sources"][0]["url"]
        assert "Jetson" in filtered["sources"][0]["excerpt"]

        from tuber_studio.agent.heartbeat.service import _extract_research_query

        assert _extract_research_query({"title": "BV1hFsQ6hEmA"}) == ""
        assert _extract_research_query({"title": "【热议】Jensen 传闻到底怎么回事", "tags": ["AI"]})

    asyncio.run(scenario())


async def test_heartbeat_app_lifecycle_chat_feed_and_sse_delivery(settings):
    app = create_app(settings, adapter=ControlledPlatform())
    runtime = app.state.runtime

    async with app.router.lifespan_context(app):
        # Verify startup attached heartbeat & remotion tools without adding new OpenAPI paths.
        status = runtime.status()
        assert "heartbeat" in status
        assert status["heartbeat"]["running"] is True
        assert "video_production" in status
        assert {"plan_video_script", "generate_remotion_project", "render_remotion_video", "stage_or_publish_bilibili_video"} <= set(runtime.tasks.tools)

        # Subscribe to delivery_events SSE stream BEFORE emitting a proactive heartbeat pulse.
        events_route = next(r for r in app.routes if getattr(r, "path", None) == "/api/v1/agent/chat/events")
        sse_gen = (await events_route.endpoint()).body_iterator
        first_chunk = await asyncio.wait_for(sse_gen.__anext__(), timeout=2.0)
        assert first_chunk.startswith("event: ready")

        # Trigger a pulse manually (offline, using fallback Aqua voice).
        result = await runtime.heartbeat.tick(
            snapshot={
                "account_id": "uid_test",
                "control_state": "running",
                "discovery": {"state": "running", "error": None},
                "inbox": {"pending_events": 0, "dm_pending": 0},
                "dynamics": {"pending_review": 2, "uncertain": 0},
                "writes": {"uncertain_count": 0},
                "hot_topics": [],
            },
            force_brief=True,
        )
        assert result["pulse"] is not None
        pulse_id = result["pulse"]["id"]

        delivery_chunk = await asyncio.wait_for(sse_gen.__anext__(), timeout=2.0)
        assert delivery_chunk.startswith("event: delivery")
        assert pulse_id in delivery_chunk
        await sse_gen.aclose()

        # Verify pulse is visible via existing GET /api/v1/agent/chat endpoint!
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
            resp = await client.get("/api/v1/agent/chat")
            assert resp.status_code == 200
            turns = resp.json()["turns"]
            pulse_turn = next(t for t in turns if t["request_id"] == pulse_id)
            assert pulse_turn["assistant_only"] is True
            assert pulse_turn["heartbeat_pulse"]["id"] == pulse_id
            assert "工坊号主" not in pulse_turn["reply_text"]

    assert runtime.heartbeat.status()["running"] is False


def test_remotion_source_isolation_schema_and_alignment(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        char = runtime.character

        # Seed owner-private memory and private DM content.
        owner_item = runtime.unified_memory.add_memory(
            "我的私人银行卡密码提示和家庭住址信息",
            scope="owner",
            character_id=char["id"],
        )
        pub_item = runtime.unified_memory.add_memory(
            "Aqua 擅长把复杂技术原理拆成清晰分镜图解",
            scope="public",
            character_id=char["id"],
        )
        with runtime.store.db:
            runtime.store.db.execute(
                "INSERT INTO dm_inbox(platform,message_id,platform_user_id,content,character_id,received_at) VALUES(?,?,?,?,?,?)",
                ("bilibili", "m1", "user_9", "私信密谈约定周末线下见面地址", char["id"], 1000.0),
            )

        # Build a bad plan referencing owner memory ID -> rejected.
        bad_plan_by_id = {
            "title": "测试脚本",
            "summary": "公开总结",
            "tags": ["Aqua"],
            "scenes": [
                {"scene_id": "s1", "template": "TitleCard", "heading": "开场", "lines": [{"text": "大家好"}]}
            ],
            "sources": [{"source_id": str(owner_item["id"]), "kind": "public_memory", "scope": "public", "title": "误标来源"}],
        }
        with pytest.raises(ValueError, match="owner"):
            assert_public_video_sources(bad_plan_by_id, store=runtime.store, unified_memory=runtime.unified_memory, character_id=char["id"])

        # Leaked owner text inside scene dialogue -> rejected.
        bad_plan_leak = {
            "title": "测试脚本",
            "summary": "公开总结",
            "tags": ["Aqua"],
            "scenes": [
                {"scene_id": "s1", "template": "TitleCard", "heading": "开场", "lines": [{"text": "听说我的私人银行卡密码提示和家庭住址信息很特别"}]}
            ],
            "sources": [{"source_id": str(pub_item["id"]), "kind": "public_memory", "scope": "public", "title": "公开印象"}],
        }
        with pytest.raises(ValueError, match="owner"):
            assert_public_video_sources(bad_plan_leak, store=runtime.store, unified_memory=runtime.unified_memory, character_id=char["id"])

        # Leaked DM text -> rejected.
        bad_plan_dm = {
            "title": "测试脚本",
            "summary": "公开总结",
            "tags": ["Aqua"],
            "scenes": [
                {"scene_id": "s1", "template": "TitleCard", "heading": "开场", "lines": [{"text": "私信密谈约定周末线下见面地址就在明天"}]}
            ],
            "sources": [{"source_id": "topic:t1", "kind": "operator_topic", "scope": "public", "title": "公开话题"}],
        }
        with pytest.raises(ValueError, match="私信"):
            assert_public_video_sources(bad_plan_dm, store=runtime.store, unified_memory=runtime.unified_memory, character_id=char["id"])

        # Reject invalid template name or non-HTTPS source URL via strict schema.
        with pytest.raises(ValidationError):
            assert_public_video_sources(
                {
                    "title": "坏模板",
                    "summary": "x",
                    "tags": ["Aqua"],
                    "scenes": [{"scene_id": "s1", "template": "ArbitraryCodeEval", "heading": "x", "lines": [{"text": "x"}]}],
                    "sources": [{"source_id": "t1", "kind": "operator_topic", "scope": "public", "url": "http://bad.local"}],
                }
            )

        # Valid audio-first alignment with exact custom TTS WAV lengths (2 scenes x 2 lines = 4.0s -> 120 frames).
        durations = [0.8, 1.2]
        idx = [0]

        def fake_tts(_text, _voice):
            d = durations[idx[0] % len(durations)]
            idx[0] += 1
            return _make_wav_bytes(d)

        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path, tts_fn=fake_tts)
        planned = await plan_video_script(skill, {"topic": "Python 协程并发原理图解", "fps": 30, "max_scenes": 2})
        assert planned["ok"] is True
        job_id = planned["job_id"]

        project = await generate_remotion_project(skill, {"job_id": job_id, "tts_mode": "dry_run"})
        assert project["ok"] is True
        # (0.8s + 1.2s)*2 = 4.0s speech + 2*0.45s line pause + 0.75s scene pause + 0.60s outro hold = 6.25s (~188 frames @ 30fps)
        assert project["total_frames"] >= 185
        assert project["total_duration_seconds"] == pytest.approx(188 / 30.0, abs=0.05)

        proj_dir = Path(project["project_dir"])
        assert (proj_dir / "subtitles.srt").is_file()
        assert (proj_dir / "manifest.json").is_file()
        assert (proj_dir / "avatar_cues.json").is_file()
        assert (proj_dir / "public/audio/bgm.wav").is_file()
        assert (proj_dir / "public/audio/transition.wav").is_file()
        props = json.loads((proj_dir / "props.json").read_text(encoding="utf-8"))
        assert props["totalFrames"] == project["total_frames"]
        assert props["includeAvatar"] is True
        assert props["bgmEnabled"] is True
        # Assert pauses create non-zero breathing gaps between consecutive cues
        assert props["cues"][1]["start_seconds"] - props["cues"][0]["end_seconds"] >= 0.40
        assert props["cues"][2]["start_seconds"] - props["cues"][1]["end_seconds"] >= 0.70
        # Assert expanded 2D Aqua GIF cast (base + story/motion GIFs) and non-duplicate scene stickers
        assert {"idle", "talking", "hum", "meal", "tail", "token", "turn"} <= set(props["avatarAssets"])
        assert (proj_dir / "public" / props["avatarAssets"]["meal"]["frames"][0]).is_file()
        scene_memes = [s["visual_props"].get("meme_asset_id") for s in props["scenes"] if s["visual_props"].get("meme_asset_id")]
        assert len(scene_memes) == len(set(scene_memes)) and len(props["memeAssets"]) >= 2
        assert isinstance(props["cues"][0]["emphasis"], list) and len(props["cues"][0]["emphasis"]) >= 1
        assert "import { AbsoluteFill" in (proj_dir / "src/AquaVideo.tsx").read_text(encoding="utf-8")
        assert "Python 协程并发原理图解" not in (proj_dir / "src/AquaVideo.tsx").read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_remotion_render_concurrency_qc_gate_and_idempotent_publish(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        creds = {"uid": "123456", "sessdata": "secret_sess_9999", "csrf": "secret_csrf_8888"}
        runtime.account.credentials = dict(creds)
        runtime.account.verified_account_id = "123456"
        runtime.account.verified_credential = dict(creds)

        active = 0
        peak_active = 0

        async def mock_render(cmd, cwd: Path, _timeout: float):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.05)
            out_file = Path(cmd[cmd.index("render") + 2])
            out_file.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom-" + cwd.name.encode("utf-8"))
            active -= 1
            return 0, "ok", ""

        # Probe fails first on silent_audio, then passes cleanly.
        qc_mode = ["bad"]

        def mock_probe(_output_path: Path, expected_duration: float):
            if qc_mode[0] == "bad":
                return {"has_video": True, "has_audio": True, "silent_audio": True, "duration_seconds": expected_duration, "width": 1920, "height": 1080}
            return {"has_video": True, "has_audio": True, "silent_audio": False, "mean_volume_db": -18.5, "duration_seconds": expected_duration, "width": 1920, "height": 1080}

        biliup_calls = []
        observed_cookie_contents = []
        observed_cookie_paths = []
        biliup_mode = ["ok"]

        async def mock_biliup(cmd, _cwd: Path, _timeout: float):
            biliup_calls.append(list(cmd))
            cookie_path = Path(cmd[cmd.index("--user-cookie") + 1])
            observed_cookie_paths.append(cookie_path)
            observed_cookie_contents.append(json.loads(cookie_path.read_text(encoding="utf-8")))
            if os.name != "nt":
                assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o600
            if biliup_mode[0] == "no_receipt":
                return 0, "upload finished without id", ""
            return 0, "投稿成功 bvid=BV1ab4y1c7DE aid=987654", ""

        sem = asyncio.Semaphore(1)
        skill = RemotionVideoSkill(
            runtime,
            runtime.store,
            browsing=runtime.browsing,
            renders_root=tmp_path,
            subprocess_runner=mock_render,
            ffprobe_runner=mock_probe,
            biliup_runner=mock_biliup,
            render_semaphore=sem,
        )

        # Test concurrency guard across two separate jobs simultaneously.
        p1 = await plan_video_script(skill, {"topic": "并发保护测试一", "max_scenes": 2})
        p2 = await plan_video_script(skill, {"topic": "并发保护测试二", "max_scenes": 2})
        g1 = await generate_remotion_project(skill, {"job_id": p1["job_id"], "tts_mode": "dry_run"})
        g2 = await generate_remotion_project(skill, {"job_id": p2["job_id"], "tts_mode": "dry_run"})

        r1, r2 = await asyncio.gather(
            render_remotion_video(skill, {"job_id": g1["job_id"], "dry_run": False}),
            render_remotion_video(skill, {"job_id": g2["job_id"], "dry_run": False}),
        )
        assert peak_active == 1, "global semaphore must serialize overlapping renders"
        assert r1["status"] == "qc_failed"
        assert r2["status"] == "qc_failed"

        # Attempting to stage a qc_failed video must raise.
        with pytest.raises(ValueError, match="质检"):
            await stage_or_publish_bilibili_video(skill, {"job_id": g1["job_id"]})

        # Pass QC cleanly now.
        qc_mode[0] = "ok"
        r1_ok = await render_remotion_video(skill, {"job_id": g1["job_id"], "dry_run": False})
        r2_ok = await render_remotion_video(skill, {"job_id": g2["job_id"], "dry_run": False})
        assert r1_ok["ok"] is True and r1_ok["status"] == "ready_for_review"
        assert r2_ok["ok"] is True and r2_ok["status"] == "ready_for_review"

        # Default preview_review NEVER calls biliup.
        staged = await stage_or_publish_bilibili_video(skill, {"job_id": g1["job_id"]})
        assert staged["action"] == "staged_for_review"
        assert biliup_calls == []

        # Enable auto_publish policy, verify off & emergency gates both keep it in preview review.
        skill.configure_policy({"publish_mode": "auto_publish", "auto_publish_enabled": True})
        gated_off = await stage_or_publish_bilibili_video(skill, {"job_id": g1["job_id"]})
        assert gated_off["ok"] is False and gated_off["action"] == "gated_to_review"
        runtime.browsing.operations.configure({"emergency": True})
        gated = await stage_or_publish_bilibili_video(skill, {"job_id": g1["job_id"]})
        assert gated["ok"] is False and gated["action"] == "gated_to_review"
        assert biliup_calls == []

        # Clear emergency & enable operations control -> auto_publish executes biliup & records canonical intent!
        runtime.browsing.operations.configure(
            {"emergency": False, "enabled": True},
            account_id=skill._account_id() or "local",
            character=runtime.character,
        )
        pub1 = await stage_or_publish_bilibili_video(skill, {"job_id": g1["job_id"]})
        assert pub1["ok"] is True
        assert pub1["action"] == "published"
        assert pub1["receipt"]["bvid"] == "BV1ab4y1c7DE"
        expected_intent = canonical_intent("bilibili", "video_publish", r1_ok["content_hash"], runtime.character["id"])
        assert pub1["intent"] == expected_intent
        assert len(biliup_calls) == 1
        assert biliup_calls[0][:4] == ["biliup", "--user-cookie", biliup_calls[0][2], "upload"]
        assert "--submit" in biliup_calls[0] and biliup_calls[0][biliup_calls[0].index("--submit") + 1] == "web"
        assert not observed_cookie_paths[0].exists(), "temp cookie file & dir must be removed in finally"
        assert observed_cookie_contents[0]["cookie_info"]["cookies"][0]["value"] == "secret_sess_9999"

        # Repeat publish on same job -> idempotent already_published without second upload.
        pub1_dup = await stage_or_publish_bilibili_video(skill, {"job_id": g1["job_id"]})
        assert pub1_dup["action"] == "already_published"
        assert len(biliup_calls) == 1

        # Cross-job canonical_intent idempotency when rendered output hash matches an existing published job.
        p_clone = await plan_video_script(skill, {"topic": "幂等克隆测试", "max_scenes": 2})
        g_clone = await generate_remotion_project(skill, {"job_id": p_clone["job_id"], "tts_mode": "dry_run"})
        r_clone = await render_remotion_video(skill, {"job_id": g_clone["job_id"], "dry_run": False})
        assert r_clone["ok"] is True
        skill._update_job(g_clone["job_id"], content_hash=r1_ok["content_hash"])
        pub_clone = await stage_or_publish_bilibili_video(skill, {"job_id": g_clone["job_id"]})
        assert pub_clone["action"] == "already_published"
        assert len(biliup_calls) == 1

        # Missing biliup receipt puts distinct job 2 into publish_uncertain and blocks retry!
        biliup_mode[0] = "no_receipt"
        uncertain = await stage_or_publish_bilibili_video(skill, {"job_id": g2["job_id"]})
        assert uncertain["ok"] is False and uncertain["status"] == "publish_uncertain"
        blocked = await stage_or_publish_bilibili_video(skill, {"job_id": g2["job_id"], "approved": True})
        assert blocked["action"] == "blocked_uncertain"

    asyncio.run(scenario())


def test_remotion_openai_tool_schemas_and_task_runtime(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
        register_remotion_tools(runtime.tasks, runtime=runtime, store=runtime.store, skill=skill)

        chat_tools = remotion_tool_schemas(format="chat")
        resp_tools = remotion_tool_schemas(format="responses")
        assert [t["function"]["name"] for t in chat_tools] == [
            "plan_video_script",
            "generate_remotion_project",
            "render_remotion_video",
            "stage_or_publish_bilibili_video",
        ]
        assert [t["name"] for t in resp_tools] == [
            "plan_video_script",
            "generate_remotion_project",
            "render_remotion_video",
            "stage_or_publish_bilibili_video",
        ]

        plan_res = await execute_remotion_tool(skill, "plan_video_script", json.dumps({"topic": "工具调度端到端"}))
        assert plan_res["ok"] is True
        gen_res = await execute_remotion_tool(skill, "generate_remotion_project", {"job_id": plan_res["job_id"], "tts_mode": "dry_run"})
        assert gen_res["ok"] is True
        ren_res = await execute_remotion_tool(skill, "render_remotion_video", {"job_id": plan_res["job_id"], "dry_run": True})
        assert ren_res["ok"] is True
        stage_res = await execute_remotion_tool(skill, "stage_or_publish_bilibili_video", {"job_id": plan_res["job_id"]})
        assert stage_res["action"] == "staged_for_review"

        bad_json = await execute_remotion_tool(skill, "plan_video_script", "{not-json")
        assert bad_json["ok"] is False

    asyncio.run(scenario())


def test_remotion_planner_uses_operation_llm_without_provider_key_and_records_diagnostics(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        assert settings.provider_key("omni") is None

        llm_calls = []

        class FakeOpLLM:
            async def generate_json(self, messages, schema, *, max_tokens=2048):
                llm_calls.append({"messages": messages, "schema_title": schema.get("title"), "max_tokens": max_tokens})
                return {
                    "title": "模型直出视频策划",
                    "summary": "通过 operation_llm 生成的结构化分镜脚本。",
                    "tags": ["Aqua", "Remotion"],
                    "fps": 30,
                    "width": 1280,
                    "height": 720,
                    "include_avatar": False,
                    "scenes": [
                        {
                            "scene_id": "s_intro",
                            "template": "TitleCard",
                            "heading": "开场导览",
                            "visual_props": {"subtitle": "无需 provider_key 也能走模型"},
                            "lines": [{"line_id": "l1", "text": "哈喽，我是 Aqua，我们直接用模型策划分镜。", "avatar_cue": "cheerful"}],
                            "avatar_cue": "cheerful",
                        }
                    ],
                    "sources": [
                        {
                            "source_id": "topic:demo",
                            "kind": "operator_topic",
                            "scope": "public",
                            "title": "公开话题",
                            "excerpt": "演示模型直出分镜",
                        }
                    ],
                }

        runtime.operation_llm = FakeOpLLM()
        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
        planned = await plan_video_script(skill, {"topic": "验证无 key 也能调用 LLM", "max_scenes": 2})
        assert planned["ok"] is True
        assert planned["planner_source"] == "llm"
        assert planned["planner_error"] is None
        assert len(llm_calls) == 1
        assert llm_calls[0]["max_tokens"] == 6144

        # Verify delivery helper describes Remotion receipts cleanly with #videos/{job_id}.
        from tuber_studio.agent.tasks.delivery import LABELS, _describe

        assert "plan_video_script" in LABELS
        lines, ok = _describe("plan_video_script", planned)
        assert ok is True
        assert any(f"#videos/{planned['job_id']}" in line for line in lines)

        # Verify available_memes are injected into LLM planner packet.
        user_packet = json.loads(llm_calls[0]["messages"][1]["content"])
        assert len(user_packet["available_memes"]) >= 5
        assert any(m["id"] == "motion-eat" for m in user_packet["available_memes"])

    asyncio.run(scenario())


def test_remotion_skill_registers_project_and_evicts_old_renders_with_media_cache(settings, tmp_path):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        skill = runtime.remotion_skill
        renders = skill.renders_root
        old_dir = renders / "rem_old_idle"
        old_dir.mkdir(parents=True, exist_ok=True)
        old_mp4 = old_dir / "output.mp4"
        old_mp4.write_bytes(b"0" * (65 * 1024 * 1024))
        os.utime(old_mp4, (1, 1))

        await runtime.cache.configure({"video_limit_mib": 64})
        assert not old_dir.exists()

        # Re-create an oversized old render dir (> 64 MiB limit when combined with new job), ensure generate_remotion_project keeps new active job & evicts old:
        old_dir.mkdir(parents=True, exist_ok=True)
        old_mp4.write_bytes(b"1" * (65 * 1024 * 1024))
        os.utime(old_mp4, (1, 1))
        planned = await plan_video_script(skill, {"topic": "缓存联动测试", "max_scenes": 2})
        gen = await generate_remotion_project(skill, {"job_id": planned["job_id"], "tts_mode": "dry_run"})
        assert gen["ok"] is True
        assert Path(gen["project_dir"]).exists()
        assert not old_dir.exists()

    asyncio.run(scenario())


def test_remotion_stage_publish_uses_web_api_without_biliup_cli(settings, tmp_path, monkeypatch):
    async def scenario():
        app = create_app(settings, adapter=ControlledPlatform())
        runtime = app.state.runtime
        runtime.account.credentials = {"sessdata": "s-web", "csrf": "c-web", "uid": "42"}

        orig_which = shutil.which
        monkeypatch.setattr(shutil, "which", lambda cmd: None if cmd == "biliup" else orig_which(cmd))

        skill = RemotionVideoSkill(
            runtime,
            runtime.store,
            browsing=runtime.browsing,
            renders_root=tmp_path,
        )
        env = skill.environment_status()
        assert env["biliup"]["ready"] is True
        assert env["biliup"]["mode"] == "web_api"
        assert env["biliup"]["web_credentials"] is True
        assert env["biliup"]["cli_installed"] is False

        p = await plan_video_script(skill, {"topic": "原生 Web 投稿回归", "max_scenes": 2})
        g = await generate_remotion_project(skill, {"job_id": p["job_id"], "tts_mode": "dry_run"})
        job_dir = Path(g["project_dir"])
        out_mp4 = job_dir / "output.mp4"
        out_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        cover_png = job_dir / "cover.png"
        cover_png.write_bytes(b"\x89PNG\r\n\x1a\n")
        skill._update_job(
            g["job_id"],
            status="ready_for_review",
            output_path=str(out_mp4),
            cover_path=str(cover_png),
            qc_json=json.dumps({"passed": True, "issues": []}),
            content_hash="hash-web-upload-01",
        )

        captured = {}

        async def fake_web_upload(**kwargs):
            captured.update(kwargs)
            return {"platform": "bilibili", "uploader": "web_api", "bvid": "BV1nativeWeb01", "aid": 321}

        monkeypatch.setattr("tuber_studio.agent.skills.remotion_video.upload_bilibili_video_web", fake_web_upload)

        pub = await stage_or_publish_bilibili_video(skill, {"job_id": g["job_id"], "approved": True})
        assert pub["ok"] is True
        assert pub["action"] == "published"
        assert pub["receipt"]["bvid"] == "BV1nativeWeb01"
        assert captured["output_path"] == out_mp4
        assert captured["cover_path"] == cover_png
        assert captured["credentials"] == {"sessdata": "s-web", "csrf": "c-web", "uid": "42"}

    asyncio.run(scenario())
