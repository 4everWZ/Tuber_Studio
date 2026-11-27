from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.skills.remotion_recap import (
    RECAP_MAX_SCENES,
    RECAP_MIN_SCENES,
    RECAP_TARGET_MAX_SECONDS,
    RECAP_TARGET_MIN_SECONDS,
    OperationsRecapBrief,
    estimate_recap_duration_seconds,
    recap_plan_fields,
)
from tuber_studio.agent.skills.remotion_video import DEFAULT_VIDEO_PRODUCTION_POLICY, RemotionVideoSkill
from tuber_studio.app import create_app


@dataclass(frozen=True)
class WeeklyExample:
    watched_url: str = "https://www.bilibili.com/video/BV1xx411c7mD"
    reply_url: str = "https://www.bilibili.com/video/BV1xx411c7mD/#reply41002"

    def brief(self):
        return {
            "title": "默认周记标题",
            "period_label": "11月21日至11月27日",
            "run_period_label": "11月27日 13:00–14:00",
            "watched_videos": 14,
            "public_comments": 1,
            "public_dynamics": 0,
            "review_dynamics": 1,
            "current_run": {"watched_videos": 1, "public_comments": 1},
            "editorial_items": [
                {
                    "source_id": "video:BV1xx411c7mD", "kind": "watched_video", "status": "watched",
                    "title": "把弹幕做成画面节拍", "creator": "弹幕研究员", "url": self.watched_url,
                    "bvid": "BV1xx411c7mD", "in_current_run": True, "excerpt": "先等这段声音走完。",
                    "excerpt_kind": "subtitle", "excerpt_url": self.watched_url + "?p=1&t=8",
                },
                {
                    "source_id": "public-reply:41002", "kind": "public_reply", "status": "public_visible",
                    "title": "同一条弹幕的两种档位", "excerpt": "配乐停一下，我就能听清了。",
                    "target_excerpt": "我剪的时候总觉得声音盖过了字幕。", "url": self.reply_url,
                    "target_url": self.watched_url + "/#reply41001", "target_author": "剪片的小雨",
                },
                {
                    "source_id": "draft-topic:7", "kind": "pending_dynamic", "status": "pending_review",
                    "title": "留一点安静给声音", "excerpt": "这是尚未授权公开的稿件正文。",
                    "url": "https://example.com/internal/draft", "creator": "工作台草稿人",
                },
                {
                    "source_id": "video:unselected", "kind": "watched_video", "status": "watched",
                    "title": "没有选入的作品", "url": "https://example.com/unselected",
                },
            ],
            "editorial": {
                "title": "给声音留点空",
                "opening": "有时候想让人把一句话真正听进去，第一步不是把音量再拉大，而是先把周围别的声音轻轻关小一点。这周刷完几条剪辑探讨和楼里讨论，我越发觉得留白比堆料更难。",
                "passages": [
                    {
                        "source_id": "video:BV1xx411c7mD",
                        "section": "deep_dive",
                        "chapter_title": "弹幕与声音的呼吸感",
                        "template_hint": "QuoteSpotlight",
                        "key_points": ["弹幕气口", "声画错峰", "主次分明"],
                        "narration": "我特别喜欢这位 UP 主给弹幕和台词同时留气口的做法。很多人做剪辑总怕冷场，恨不得每一帧都塞满音效和字幕，结果观众耳朵先累了。把关键那半秒空出来，声音落下去的时候，观众反而更愿意追着你的节奏往下听。",
                    },
                    {
                        "source_id": "video:BV1xx411c7mD",
                        "section": "deep_dive",
                        "chapter_title": "节奏与界面排版的呼应",
                        "template_hint": "UIShowcase",
                        "key_points": ["字幕对齐", "背景压暗", "视觉焦点"],
                        "narration": "如果从分镜和界面信息密度的角度看，这条原片还有一个很聪明的处理，就是把弹幕出场时机卡在句尾停顿上。画面上不会同时堆出三四块高饱和字卡，观众视线自然就能跟上说话人的重点，我自己做片子也得照着这条记下来。",
                    },
                    {
                        "source_id": "public-reply:41002",
                        "section": "deep_dive",
                        "chapter_title": "评论区关于配乐的争论",
                        "template_hint": "QuoteSpotlight",
                        "key_points": ["配乐盖过字幕", "人声压低配乐", "混音克制"],
                        "narration": "看到楼里有人说剪的时候总觉得配乐把字幕和台词盖过去了，我就顺手接了一句，先把背景配乐停一下其实就能听清了。回头想想这也是我自己最常踩的坑，台词、特效和伴奏绝对不能同时去抢同一支麦克风的主角位。",
                    },
                    {
                        "source_id": "draft-topic:7",
                        "section": "side_cut",
                        "chapter_title": "顺带本周切片",
                        "template_hint": "MemeCutaway",
                        "key_points": ["安静版选题", "打磨中"],
                        "narration": "顺带翻一下这周还留在待审夹里的一个新坑，我也打算抽空试一条极简安静版的短打，把多余的花活全撤掉，只靠台词和微小的停顿撑住场面，不过稿子还得再多捋两遍。",
                    },
                ],
                "closing": "下次我自己开剪之前，一定先把配乐轨静音两分钟，单独听听那段人声自己站不站得住。你们看视频的时候，最受不了哪种声音打架的混音？评论区交出你的吐槽吧。",
                "description": "有一条弹幕让我想关一会儿配乐。这期聊聊留白，也翻出一条还在打磨的选题。",
                "dynamic_post": "这周看片最大的感触就是音频留白太重要了，连着评论区也在聊配乐盖过人声的事，我自己先记在小本本上。",
                "dynamic_source_ids": ["video:BV1xx411c7mD", "public-reply:41002"],
            },
        }


def test_weekly_editorial_plan_builds_rich_90_to_300s_show_and_keeps_attribution(settings, tmp_path):
    runtime = create_app(settings, adapter=ControlledPlatform()).state.runtime
    skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
    example = WeeklyExample()
    brief = example.brief()

    assert DEFAULT_VIDEO_PRODUCTION_POLICY["render_timeout_seconds"] == 600.0
    plan = skill.plan_operations_recap(brief, job_key="weekly-editorial")["plan"]
    scenes = plan["scenes"]
    assert RECAP_MIN_SCENES <= len(scenes) <= RECAP_MAX_SCENES
    assert plan["duration_preset"] in {"standard", "deep_dive"}

    estimated = estimate_recap_duration_seconds(scenes)
    assert RECAP_TARGET_MIN_SECONDS <= estimated <= RECAP_TARGET_MAX_SECONDS

    templates = [scene["template"] for scene in scenes]
    assert templates[0] == "TitleCard"
    assert templates[-1] == "KineticStatement"
    assert "ChapterBumper" in templates
    assert "QuoteSpotlight" in templates
    assert "UIShowcase" in templates
    assert "TimelineSummary" in templates
    assert "ChartCompare" in templates
    assert all(a != b for a, b in zip(templates, templates[1:])), f"adjacent duplicate template in {templates}"
    assert all(scene["theme"] in {"dark_tech", "aqua_light", "cinema"} for scene in scenes)
    assert all(scene["transition"] for scene in scenes)
    assert all(scene["camera_motion"] for scene in scenes)

    opening = scenes[0]
    watched = next(sc for sc in scenes if sc["heading"] == "把弹幕做成画面节拍" and sc["template"] == "QuoteSpotlight")
    reply = next(sc for sc in scenes if sc["heading"] == "同一条弹幕的两种档位")
    pending = next(sc for sc in scenes if sc["heading"] == "留一点安静给声音")
    closing = scenes[-1]

    assert plan["title"] == "给声音留点空"
    assert opening["chapter_title"] == brief["period_label"]
    assert opening["visual_props"]["subtitle"] == f"本轮 {brief['run_period_label']}"
    assert watched["visual_props"]["citation_label"] == "UP 主 弹幕研究员"
    assert watched["visual_props"]["subtitle"] == "原片字幕 · 先等这段声音走完。"
    assert reply["visual_props"]["subtitle"] == "配乐停一下，我就能听清了。"
    assert reply["visual_props"]["bullets"][0] == "原楼留言（剪片的小雨） · 我剪的时候总觉得声音盖过了字幕。"
    assert reply["visual_props"]["citation_label"] == "本账号的公开回复"
    assert "".join(line["text"] for line in reply["lines"]) == brief["editorial"]["passages"][2]["narration"]
    assert pending["heading"] == "留一点安静给声音"
    assert pending["visual_props"]["citation_label"] == "待审核 · 尚未发布"
    assert plan["sources"][2]["excerpt"] == ""
    assert plan["sources"][2]["url"] is None
    assert "工作台草稿人" not in str(plan)
    assert "尚未授权公开" not in str(plan)
    assert "没有选入的作品" not in str(plan)
    assert {source["url"] for source in plan["sources"]} == {example.watched_url + "?p=1&t=8", example.reply_url, None}
    assert plan["summary"] == brief["editorial"]["description"]
    assert {"大肥鱼", "deepseek"}.issubset(plan["tags"])
    assert "".join(line["text"] for line in closing["lines"]) == brief["editorial"]["closing"]
    assert skill.plan_operations_recap(brief | {"watched_videos": 30}, job_key="weekly-editorial")["plan"] == plan


def test_weekly_editorial_enforces_duration_floor_and_cap_and_dry_run_render(settings, tmp_path):
    async def scenario():
        runtime = create_app(settings, adapter=ControlledPlatform()).state.runtime
        skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
        brief = WeeklyExample().brief()

        # Short editorial below 90s floor must be rejected.
        short_brief = WeeklyExample().brief()
        short_brief["editorial"]["opening"] = "太短的开场。"
        short_brief["editorial"]["closing"] = "太短的结尾。"
        for passage in short_brief["editorial"]["passages"]:
            passage["narration"] = "太短的一段话。"
        with pytest.raises(ValueError, match="低于 90s 下限"):
            skill.plan_operations_recap(short_brief)

        # Oversized editorial over 300s soft cap must be rejected.
        long_brief = WeeklyExample().brief()
        long_brief["editorial"]["passages"] = [
            {
                "source_id": "video:BV1xx411c7mD",
                "section": "deep_dive",
                "chapter_title": f"超长章节 {idx}",
                "narration": "字" * 310,
            }
            for idx in range(1, 9)
        ]
        with pytest.raises(ValueError, match="超过 300s 上限"):
            skill.plan_operations_recap(long_brief)

        # Full dry-run project generation on standard editorial recap stays >= 90s and <= 300s.
        planned = skill.plan_operations_recap(brief, job_key="weekly-dry-run")
        gen = await skill.generate_remotion_project({"job_id": planned["job_id"], "tts_mode": "dry_run"})
        assert gen["ok"] is True
        assert RECAP_TARGET_MIN_SECONDS <= gen["total_duration_seconds"] <= RECAP_TARGET_MAX_SECONDS

    import asyncio

    asyncio.run(scenario())


def test_weekly_editorial_rejects_unbound_or_duplicate_sources_and_wrong_status():
    brief = WeeklyExample().brief()
    editorial = brief["editorial"]
    excerpt = editorial["passages"][0]
    patched = brief | {"editorial": editorial | {"passages": [excerpt | {"source_id": "generated"}]}}
    with pytest.raises(ValueError, match="缺少对应"):
        recap_plan_fields(OperationsRecapBrief.model_validate(patched))
    patched = brief | {"editorial": editorial | {"passages": [excerpt, excerpt]}}
    with pytest.raises(ValueError, match="重复引用"):
        recap_plan_fields(OperationsRecapBrief.model_validate(patched))
    patched = brief | {"editorial_items": [{**brief["editorial_items"][0], "status": "public_visible"}]}
    with pytest.raises(ValueError, match="核实状态不符"):
        OperationsRecapBrief.model_validate(patched)


def test_weekly_editorial_description_may_include_only_selected_public_links():
    example = WeeklyExample()
    brief = example.brief()
    original = brief["editorial"]["description"]
    brief["editorial"]["description"] = f"{original}\n片子在这儿 {example.watched_url}。"
    assert recap_plan_fields(OperationsRecapBrief.model_validate(brief))["summary"] == brief["editorial"]["description"]
    brief["editorial"]["description"] = f"{original}\nhttps://example.com/internal/draft"
    with pytest.raises(ValueError, match="所选公开素材"):
        recap_plan_fields(OperationsRecapBrief.model_validate(brief))
    brief["editorial"]["description"] = "字" * 301
    with pytest.raises(ValueError, match="300"):
        OperationsRecapBrief.model_validate(brief)


def test_weekly_editorial_shows_dynamic_submission_receipt_without_claiming_public_visibility():
    brief = WeeklyExample().brief()
    brief["editorial_items"].append({
        "source_id": "dynamic:41001", "kind": "public_dynamic", "status": "confirmed",
        "title": "有发布回执的动态", "url": "https://t.bilibili.com/41001", "excerpt": "尚未核对匿名公开页。",
    })
    brief["editorial"]["passages"][-1] = {
        "source_id": "dynamic:41001",
        "section": "side_cut",
        "chapter_title": "",
        "narration": "顺带提一下刚提交的一条动态，目前只拿到了平台这边的提交回执，公开页面上到底能不能稳稳刷出来，还得等一会儿再去确认一下看看。",
    }
    scenes = recap_plan_fields(OperationsRecapBrief.model_validate(brief))["scenes"]
    dynamic = next(sc for sc in scenes if sc.heading == "有发布回执的动态")
    assert dynamic.chapter_title == "动态发布回执"
    assert not dynamic.visual_props.subtitle
