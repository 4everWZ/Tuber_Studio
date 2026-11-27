from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.agent.skills.remotion_recap import OperationsRecapBrief, recap_plan_fields
from tuber_studio.agent.skills.remotion_video import RemotionVideoSkill
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
                "opening": "有时候想让人听进去，先得把别的声音关小一点。",
                "passages": [
                    {"source_id": "video:BV1xx411c7mD", "narration": "我喜欢这位 UP 主给弹幕留气口的做法。"},
                    {
                        "source_id": "public-reply:41002",
                        "narration": "看到楼里聊到配乐把字幕盖过去，我就接了一句。自己也得记住这事，别让台词和配乐跟我抢同一支麦。",
                    },
                    {"source_id": "draft-topic:7", "narration": "我还留着一个安静版的动态选题，稿子得再捋捋。"},
                ],
                "closing": "下次我想先把配乐关掉，听听那段话自己站不站得住。",
                "description": "有一条弹幕让我想关一会儿配乐。这期聊聊留白，也翻出一条还在打磨的选题。",
            },
        }


def test_weekly_editorial_plan_keeps_visual_evidence_and_narration_separate(settings, tmp_path):
    runtime = create_app(settings, adapter=ControlledPlatform()).state.runtime
    skill = RemotionVideoSkill(runtime, runtime.store, renders_root=tmp_path)
    example = WeeklyExample()
    brief = example.brief()

    plan = skill.plan_operations_recap(brief, job_key="weekly-editorial")["plan"]
    opening, watched, reply, pending, closing = plan["scenes"]
    assert plan["title"] == "给声音留点空"
    assert opening["chapter_title"] == brief["period_label"]
    assert opening["visual_props"]["subtitle"] == f"本轮 {brief['run_period_label']}"
    assert watched["heading"] == "把弹幕做成画面节拍"
    assert watched["chapter_title"] == "本轮观看"
    assert watched["visual_props"]["citation_label"] == "UP 主 弹幕研究员"
    assert watched["visual_props"]["subtitle"] == "原片字幕 · 先等这段声音走完。"
    assert reply["visual_props"]["subtitle"] == "配乐停一下，我就能听清了。"
    assert reply["visual_props"]["bullets"] == ["原楼留言（剪片的小雨） · 我剪的时候总觉得声音盖过了字幕。"]
    assert reply["visual_props"]["citation_label"] == "本账号的公开回复"
    assert len(reply["lines"]) == 2
    assert "".join(line["text"] for line in reply["lines"]) == brief["editorial"]["passages"][1]["narration"]
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
    assert closing["lines"][0]["text"] == brief["editorial"]["closing"]
    assert "ChartCompare" not in [scene["template"] for scene in plan["scenes"]]
    assert skill.plan_operations_recap(brief | {"watched_videos": 30}, job_key="weekly-editorial")["plan"] == plan


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
    brief["editorial"]["description"] = "字" * 251
    with pytest.raises(ValueError, match="250"):
        OperationsRecapBrief.model_validate(brief)


def test_weekly_editorial_shows_dynamic_submission_receipt_without_claiming_public_visibility():
    brief = WeeklyExample().brief()
    brief["editorial_items"].append({
        "source_id": "dynamic:41001", "kind": "public_dynamic", "status": "confirmed",
        "title": "有发布回执的动态", "url": "https://t.bilibili.com/41001", "excerpt": "尚未核对匿名公开页。",
    })
    brief["editorial"]["passages"] = [{"source_id": "dynamic:41001", "narration": "先等那条动态的公开页。"}]
    _, dynamic, _ = recap_plan_fields(OperationsRecapBrief.model_validate(brief))["scenes"]
    assert dynamic.chapter_title == "动态发布回执"
    assert not dynamic.visual_props.subtitle
