"""Regression tests for Post #21 root causes, CoT leak guards, and direct edit/redraft routes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.support.operations_helpers import ControlledPlatform
from tuber_studio.app import create_app
from tuber_studio.core.errors import platform_rejection
from tuber_studio.core.models import content_hash
from tuber_studio.core.text import detect_reasoning_leak, operation_output_diagnostics
from tuber_studio.operations.channels.dynamics import Dynamics
from tuber_studio.operations.channels.dynamics import comment_card as comment_card_module
from tuber_studio.operations.channels.dynamics import drafter as drafter_module
from tuber_studio.operations.channels.dynamics import publisher as publisher_module
from tuber_studio.operations.channels.dynamics.turns import (
    _detect_dynamic_mentions,
    _format_dynamic_sources,
    _match_grounded_sources,
    revise_native_dynamic_caption,
)
from tuber_studio.operations.kernel.feedback import (
    FeedbackPublication,
    PublicationContext,
    publication_binding,
)
from tuber_studio.operations.kernel.tools import (
    detect_internal_protocol_leak,
    sanitize_spoken_text,
    validate_spoken_output,
)

POST21_LEAKED_DRAFT = (
    '. [doge]"\n\n'
    'Wait, "don\'t touch. Some food inspectors can come and take a look."\n\n'
    "Actually let me make it snappier:\n\n"
    "Hmm, good. Let me check for banned phrases: 首先/其次/最后, 作为一个AI. None used.\n\n"
    "Self-reference: I used \"I\" and could add \"this fish\" once.\n\n"
    "No need to skip.\n\n"
    "Final text:\n\n"
    "自从那句「十袋米也就我一天的量吧」传出去，评论区已经快变成食品稽查现场了。"
)


def test_reasoning_leak_detector_blocks_cot_and_allows_normal_bilingual_text():
    assert detect_reasoning_leak(POST21_LEAKED_DRAFT) is not None
    assert operation_output_diagnostics(POST21_LEAKED_DRAFT)["reasoning_leak"] is True
    assert operation_output_diagnostics(POST21_LEAKED_DRAFT)["needs_review"] is True
    assert detect_internal_protocol_leak(POST21_LEAKED_DRAFT) is not None
    with pytest.raises(ValueError, match="思考过程"):
        validate_spoken_output(POST21_LEAKED_DRAFT, kind="dynamic")

    # Multi-cue English meta-drafting without "Final text:"
    multi_cue = (
        'Wait, "let me double check"\n'
        "Let me refine this first.\n"
        "Should I mention the video link? Fine as is.\n"
        "今天刚看了一个很有意思的切片。"
    )
    assert detect_reasoning_leak(multi_cue) is not None

    # Stray closing </think> tag prefix is stripped cleanly
    assert sanitize_spoken_text("internal step here</think>今天水波挺好看的。") == "今天水波挺好看的。"

    # Normal Chinese + bilingual technical/daily posts must NOT trigger false positives
    for normal in (
        "刚才用 Python 和 FastAPI 调了一晚上接口，终于把异步队列修好了。",
        "Wait a minute! 这波操作在 GitHub Issue 里早就讨论过，B 站居然还有人问。",
        "看了一个讲述 Final Fantasy XIV 副本机制的视频，评论区太真实了。",
    ):
        assert detect_reasoning_leak(normal) is None
        assert operation_output_diagnostics(normal)["reasoning_leak"] is False
        assert validate_spoken_output(normal, kind="dynamic") == normal


def test_dynamic_source_formatting_and_grounding_handle_third_party_comment_and_video():
    public_sources = [
        {
            "id": "comment:BV1q5WZ6HEFD:321197068865:325013871744",
            "type": "public_comment",
            "fictional": False,
            "participants": [{"name": "风月星云的大肥鱼", "uid": "3747587614050633"}],
            "reference": {
                "author_name": "风月星云的大肥鱼",
                "author_uid": "3747587614050633",
                "comment_id": "325013871744",
                "resource": "BV1q5WZ6HEFD",
                "thread_id": "321197068865",
            },
            "source_url": "https://www.bilibili.com/video/BV1q5WZ6HEFD#reply325013871744",
            "text": "十袋米也就我一天的量吧，主人别大惊小怪。吃饱了才有力气给你摸鱼嘛。",
        },
        {
            "id": "video:BV1q5WZ6HEFD",
            "type": "watched_video",
            "fictional": False,
            "source_url": "https://www.bilibili.com/video/BV1q5WZ6HEFD",
            "text": "深海干饭王 · 已读片段\nCID 1 · 12s · 画面 · 一大锅白饭端上来",
            "participants": [{"uid": "998877", "name": "干饭UP主"}],
        },
    ]

    formatted = _format_dynamic_sources(public_sources, self_uid="3706944382568764")
    assert "旁观到的他人评论 BV1q5WZ6HEFD" in formatted
    assert "@风月星云的大肥鱼（UID 3747587614050633），评论 325013871744" in formatted
    assert "[观看素材 BV1q5WZ6HEFD] 《深海干饭王》 UP主 干饭UP主（UID 998877），视频片段" in formatted

    own_account = _format_dynamic_sources(public_sources, self_uid="3747587614050633")
    assert "同账号的公开评论，角色归属未确认" in own_account
    assert "你曾说" not in own_account
    assert _match_grounded_sources("刚看了 BV1q5WZ6HEFD。", public_sources) == (
        ["video:BV1q5WZ6HEFD"], ["video:BV1q5WZ6HEFD"], "none"
    )

    content = "刚才刷 BV1q5WZ6HEFD 时在评论区看到 @风月星云的大肥鱼 说一天能吃十袋米，差点把茶水喷出来。"
    matched, link_ids, illustration = _match_grounded_sources(content, public_sources)
    assert "comment:BV1q5WZ6HEFD:321197068865:325013871744" in matched
    assert "comment:BV1q5WZ6HEFD:321197068865:325013871744" in link_ids
    assert illustration == "comment_card"

    mentions = _detect_dynamic_mentions(content, public_sources, matched)
    assert mentions == [{"label": "@风月星云的大肥鱼", "account": "3747587614050633"}]

    same_name = {"id": "other-source", "type": "public_comment", "participants": [
        {"name": "风月星云的大肥鱼", "uid": "100"}
    ]}
    assert _detect_dynamic_mentions(content, [*public_sources, same_name], [*matched, same_name["id"]]) == [
        {"label": "@风月星云的大肥鱼", "account": "风月星云的大肥鱼"}
    ]


def test_visible_planning_and_final_draft_sequence_is_rejected():
    leaked = (
        'If it were me going, the 4th thing would be a rice warmer.\n\n'
        'Final draft:\n\n"Just now I was lurking in the comments."\n\n'
        'Let me go with the compact version. Should I skip? No. Let me finalize.\n\n'
        '刚看到有人聊插件市场，说购物车里就三样东西。'
    )
    assert operation_output_diagnostics(leaked)["reasoning_leak"] is True
    with pytest.raises(ValueError, match="思考过程"):
        validate_spoken_output(leaked, kind="dynamic")
    with pytest.raises(ValueError, match="思考过程"):
        validate_spoken_output("Final draft:\n今天终于写完了那份读书笔记。", kind="dynamic")
    assert validate_spoken_output("今天终于写完了那份读书笔记，风也挺温柔。", kind="dynamic")


def test_post22_english_meta_drafting_markers_and_cues_are_detected():
    strong_samples = [
        "Final version:\n今天水波真好看。",
        "Final output:\n今天水波真好看。",
        "Final reply:\n今天水波真好看。",
        "No tool call needed.\n今天水波真好看。",
        "Output plain text.\n今天水波真好看。",
        "One more consideration: keep it short.\n今天水波真好看。",
        "Quote marks: should I drop them?\n今天水波真好看。",
        "This looks dedup-safe enough.\n今天水波真好看。",
        "Per the rules I can post now.\n今天水波真好看。",
    ]
    for sample in strong_samples:
        assert detect_reasoning_leak(sample) is not None, sample
        assert operation_output_diagnostics(sample)["reasoning_leak"] is True
        with pytest.raises(ValueError, match="思考过程"):
            validate_spoken_output(sample, kind="dynamic")

    multi_cue_post22 = (
        'Hmm, "这谁顶得住" sounds a bit awkward here.\n'
        "Should I skip? No, will treat it as casual banter.\n"
        "刚刷到一个很有意思的切片。"
    )
    assert detect_reasoning_leak(multi_cue_post22) is not None
    assert operation_output_diagnostics(multi_cue_post22)["needs_review"] is True
    # Single benign cue alone should not false-positive
    assert detect_reasoning_leak("这段英语台词听着 a bit awkward，但配音很可爱。") is None


async def test_native_dynamic_diagnose_reports_cot_leak_and_blocks_auto_publish(settings, store, monkeypatch):
    from tuber_studio.operations.channels.dynamics.turns import evaluate_native_dynamic

    leaked = (
        "No tool call needed. One more consideration: keep it dedup-safe.\n\n"
        "今天去海边散了一会儿步。"
    )

    async def fake_run_tool_session(*args, **kwargs):
        return SimpleNamespace(
            action="speak",
            text=leaked,
            skip_reason="",
            skip_explanation="",
            tool_calls=[],
        )

    monkeypatch.setattr(
        "tuber_studio.operations.channels.dynamics.turns.run_tool_session",
        fake_run_tool_session,
    )

    account = SimpleNamespace(
        adapter=SimpleNamespace(),
        credential_lock=asyncio.Lock(),
        credentials={"sessdata": "s", "csrf": "c"},
        credentials_configured=True,
        display_account_id=lambda: "100",
    )
    runtime = SimpleNamespace(
        operation_llm=SimpleNamespace(config={}),
        operation_responses=SimpleNamespace(),
        generation_lock=asyncio.Lock(),
    )
    dynamics = Dynamics(settings, store, runtime, SimpleNamespace(account=account), account=account)
    await dynamics.set_policy({"enabled": True, "auto_publish": True, "evaluate_interval_seconds": 300})
    char = store.character()
    dynamics.operations.configure({"enabled": True}, account_id="100", character=char)

    decision, cont = await evaluate_native_dynamic(
        dynamics,
        char=char,
        evaluation_account="100",
        recent_posts=[],
        related_posts=[],
        public_sources=[],
        unified_public=[],
        current_time="2026-11-28T00:00:00+08:00",
    )
    assert cont["state"] == "awaiting_review"
    assert cont["feedback"]["kind"] == "style_diagnostic"
    assert "思考过程" in cont["feedback"]["message"]
    assert "重复" not in cont["feedback"]["message"]

    # Automatic publishing must refuse leaked drafts
    saved = dynamics._save_decision(
        char,
        False,
        decision,
        [],
        schedule=False,
        account_id="100",
        decide_image=False,
    )
    dynamics._automatic_post_id = saved["post_id"]
    with pytest.raises(ValueError, match="正文诊断尚待审核"):
        await dynamics.publish(saved["post_id"], automatic=True)


def test_grounded_source_matching_rejects_loose_four_char_cross_video_overlap():
    sources = [
        {
            "id": "comment:BV1q5sa6JEyt:101:202",
            "type": "public_comment",
            "fictional": False,
            "participants": [{"name": "路人甲乙", "uid": "222"}],
            "reference": {
                "author_name": "路人甲乙",
                "author_uid": "222",
                "comment_id": "202",
                "resource": "BV1q5sa6JEyt",
                "thread_id": "101",
            },
            "source_url": "https://www.bilibili.com/video/BV1q5sa6JEyt#reply202",
            "text": "这谁顶得住啊，凌晨三点还在看海鲜吃播。",
        },
        {
            "id": "video:BV1gMsM6TE9u",
            "type": "watched_video",
            "fictional": False,
            "source_url": "https://www.bilibili.com/video/BV1gMsM6TE9u",
            "text": "深夜海鲜大排档 · 已读片段\nCID 1 · 10s · 画面 · 炭火烤生蚝",
            "participants": [{"uid": "333", "name": "炭火厨师"}],
        },
        {
            "id": "video:BV1short12345",
            "type": "watched_video",
            "fictional": False,
            "source_url": "https://www.bilibili.com/video/BV1short12345",
            "text": "短标题 · 已读片段\nCID 1 · 5s · 画面 · 随便拍拍",
            "participants": [{"uid": "444", "name": "路人"}],
        },
    ]

    # #24 regression: "这谁顶得住" in a post about BV1gMsM6TE9u must NOT attach unrelated BV1q5sa6JEyt
    post = "刚看完 BV1gMsM6TE9u 的炭火烤生蚝，这谁顶得住啊！顺带一提那个短标题也挺常见。"
    matched, link_ids, illustration = _match_grounded_sources(post, sources)
    assert matched == ["video:BV1gMsM6TE9u"]
    assert link_ids == ["video:BV1gMsM6TE9u"]
    assert illustration == "none"

    # Quoted snippet < 8 non-trivial chars must NOT match, >= 8 non-trivial chars in quotes DOES match
    short_quote_post = "今天刷到一句「这谁顶得住」，笑死。"
    assert _match_grounded_sources(short_quote_post, sources) == ([], [], "none")

    long_quote_post = "今天在评论区看到有人说「凌晨三点还在看」，太真实了。"
    assert _match_grounded_sources(long_quote_post, sources) == ([], [], "none")

    exact_quote_post = "今天在评论区看到有人说「点还在看海鲜吃播」，太真实了。"
    matched_q, links_q, ill_q = _match_grounded_sources(exact_quote_post, sources)
    assert matched_q == ["comment:BV1q5sa6JEyt:101:202"]
    assert links_q == ["comment:BV1q5sa6JEyt:101:202"]
    assert ill_q == "comment_card"

    # Unquoted < 10 non-trivial chars does NOT match; >= 10 non-trivial chars matches; explicit video title >= 6 chars matches
    assert _match_grounded_sources("今天也点还在看海鲜吃播呢。", sources) == ([], [], "none")
    phrase_post = "突然想起深夜海鲜大排档那个视频，还看到有人说凌晨三点还在看海鲜吃播。"
    matched_p, _, _ = _match_grounded_sources(phrase_post, sources)
    assert set(matched_p) == {"comment:BV1q5sa6JEyt:101:202", "video:BV1gMsM6TE9u"}


def test_comment_card_unescapes_html_entities_across_build_legacy_and_render(monkeypatch):
    from io import BytesIO

    from PIL import Image, ImageFont

    monkeypatch.setattr(comment_card_module, "_font", lambda size: ImageFont.load_default())
    escaped_sources = [
        {
            "id": "comment:BV1q5WZ6HEFD:100:200",
            "type": "public_comment",
            "reference": {
                "author_name": "小鱼&#34;一号&#34;",
                "author_uid": "555",
                "comment_id": "200",
                "resource": "BV1q5WZ6HEFD",
                "thread_id": "100",
            },
            "text": "他说&#34;十袋米&#34; &amp; 两碗汤",
        }
    ]
    clean_sources = [
        {
            "id": "comment:BV1q5WZ6HEFD:100:200",
            "type": "public_comment",
            "reference": {
                "author_name": '小鱼"一号"',
                "author_uid": "555",
                "comment_id": "200",
                "resource": "BV1q5WZ6HEFD",
                "thread_id": "100",
            },
            "text": '他说"十袋米" & 两碗汤',
        }
    ]

    card = comment_card_module.comment_card(escaped_sources)
    assert card == comment_card_module.comment_card(clean_sources)
    assert card["comments"][0]["quote"] == '他说"十袋米" & 两碗汤'
    assert card["comments"][0]["author"] == '小鱼"一号"'

    legacy = comment_card_module.comment_card(escaped_sources, legacy=True)
    assert legacy["comments"][0]["quote"] == '他说"十袋米" & 两碗汤'

    # Even a previously stored card dict containing literal &#34; renders identically to the unescaped card
    stale_snapshot = {
        "version": 3,
        "comments": [
            {
                "quote": "他说&#34;十袋米&#34; &amp; 两碗汤",
                "url": "https://www.bilibili.com/video/BV1q5WZ6HEFD#reply200",
                "author": "小鱼&#34;一号&#34;",
                "uid": "555",
                "created_at": None,
                "comment_id": "200",
                "parent_comment_id": None,
                "reply_to": "前排&#34;路人&#34;",
                "time_label": None,
            }
        ],
        "links": [{"bvid": "BV1q5WZ6HEFD", "url": "https://www.bilibili.com/video/BV1q5WZ6HEFD#reply200"}],
        "omitted": 0,
        "missing": 0,
        "page_count": 1,
    }
    clean_snapshot = {
        **stale_snapshot,
        "comments": [
            {
                **stale_snapshot["comments"][0],
                "quote": '他说"十袋米" & 两碗汤',
                "author": '小鱼"一号"',
                "reply_to": '前排"路人"',
            }
        ],
    }
    rendered_stale = comment_card_module.render_comment_card(stale_snapshot)
    rendered_clean = comment_card_module.render_comment_card(clean_snapshot)
    assert rendered_stale == rendered_clean
    with Image.open(BytesIO(rendered_stale)) as img:
        assert img.format == "PNG"

    rendered_legacy_stale = comment_card_module.render_comment_card({
        "legacy": True,
        "comments": [{"quote": "他说&#34;十袋米&#34;", "url": "https://x.invalid", "author": "作者资料未保存"}],
        "links": [],
        "omitted": 0,
    })
    rendered_legacy_clean = comment_card_module.render_comment_card({
        "legacy": True,
        "comments": [{"quote": '他说"十袋米"', "url": "https://x.invalid", "author": "作者资料未保存"}],
        "links": [],
        "omitted": 0,
    })
    assert rendered_legacy_stale == rendered_legacy_clean


def test_organized_third_party_quote_keeps_its_author_in_dynamic_context():
    from tuber_studio.operations.channels.dynamics.materials import dynamic_model_material

    public = dynamic_model_material({
        "id": "organized:11", "type": "public_incident", "fictional": False,
        "original_material_kind": "comment", "original_author": {"uid": "215", "name": "另一条鱼"},
        "text": "「睡醒继续扮演昨天的自己，本鱼每天干的正是这件事。」",
    })
    formatted = _format_dynamic_sources([public], self_uid="100")
    assert "收录的第三方评论" in formatted and "@另一条鱼（UID 215）" in formatted
    assert "本鱼每天干的正是这件事" in formatted


async def test_revise_native_dynamic_caption_includes_sources_in_prompt(settings, store):
    seen_messages = []

    class FakeAdapter:
        async def mention_accounts(self, query):
            return {}

    class FakeResponsesClient:
        async def create(self, input_items, **kwargs):
            seen_messages.append(input_items)
            return SimpleNamespace(
                id="resp-rev",
                calls=(),
                text="之前在 BV1q5WZ6HEFD 评论区围观到 @风月星云的大肥鱼 的十袋米发言，今天补充说明一下。",
                raw={"usage": {"input_tokens": 10, "output_tokens": 10}},
            )

    account = SimpleNamespace(
        adapter=FakeAdapter(),
        credential_lock=asyncio.Lock(),
        credentials={},
        credentials_configured=True,
        display_account_id=lambda: "100",
    )
    runtime = SimpleNamespace(
        operation_llm=SimpleNamespace(config={}),
        operation_responses=FakeResponsesClient(),
        generation_lock=asyncio.Lock(),
    )
    dynamics = Dynamics(settings, store, runtime, SimpleNamespace(account=account), account=account)
    row = {
        "id": 21,
        "account_id": "100",
        "sources": [
            {
                "id": "comment:BV1q5WZ6HEFD:321197068865:325013871744",
                "type": "public_comment",
                "source_url": "https://www.bilibili.com/video/BV1q5WZ6HEFD#reply325013871744",
                "participants": [{"name": "风月星云的大肥鱼", "uid": "3747587614050633"}],
                "text": "十袋米也就我一天的量吧。",
            }
        ],
    }
    caption, mentions, pending = await revise_native_dynamic_caption(
        dynamics,
        row,
        {"text": "十袋米当日常？", "mentions": [{"uid": "3747587614050633", "name": "风月星云的大肥鱼"}]},
        "说清楚出处",
    )
    assert "BV1q5WZ6HEFD" in caption and not pending
    assert len(mentions) == 1
    assert any("十袋米也就我一天的量吧" in json.dumps(msg, ensure_ascii=False) for msg in seen_messages)


async def test_rejection_auto_revision_holds_for_review_when_original_had_cot_leak(settings, store, monkeypatch):
    published_calls = []

    class DummyAdapter:
        async def account_id(self):
            return "100"

    account = SimpleNamespace(
        adapter=DummyAdapter(),
        credential_lock=asyncio.Lock(),
        credentials={"sessdata": "s", "csrf": "c"},
        credentials_configured=True,
        display_account_id=lambda: "100",
        account_id=DummyAdapter().account_id,
    )

    class RevisionModel:
        config = {"model": "test-model", "base_url": "https://x.invalid", "context_tokens": 32768, "native_context_tokens": 32768}

        async def prefill(self, messages):
            return True

        async def count_tokens(self, messages):
            return 100

        async def generate_json(self, messages, schema, *, max_tokens):
            if "action" in schema.get("properties", {}):
                return {"action": "revise", "reason": "平台嫌过长，改短并保留起因", "read": ""}
            return {"text": "在 BV1q5WZ6HEFD 评论区刷到有人说一天吃十袋米，差点把我的白饭吓掉。"}

    runtime = SimpleNamespace(
        operation_llm=RevisionModel(),
        generation_lock=asyncio.Lock(),
        offline_generation=lambda *a, **kw: asyncio.Lock(),
    )
    dynamics = Dynamics(settings, store, runtime, SimpleNamespace(account=account), account=account)
    await dynamics.set_policy({"enabled": True, "auto_publish": True, "evaluate_interval_seconds": 300})
    char = store.character()
    dynamics.operations.configure({"enabled": True}, account_id="100", character=char)

    monkeypatch.setattr(dynamics, "_valid_sources", lambda row: True)

    decision = dynamics._save_decision(
        char,
        False,
        {
            "action": "post",
            "reason": "test",
            "content": POST21_LEAKED_DRAFT,
            "source_ids": [],
            "next_evaluation_seconds": 1800,
        },
        [],
        schedule=False,
        account_id="100",
        decide_image=False,
    )
    post_id = decision["post_id"]
    dynamics._automatic_post_id = post_id

    # 1. Defense in depth: automatic publishing refuses a draft whose diagnostics flag reasoning_leak.
    with pytest.raises(ValueError, match="正文诊断尚待审核"):
        await dynamics.publish(post_id, automatic=True)
    assert published_calls == []

    # 2. If a leaked draft ever reached the platform and got rejected (e.g. legacy queue or custom caller),
    # FeedbackPublication auto-revision holds the revised draft at pending_review instead of auto-republishing.
    token = dynamics.operations.reserve(
        "100",
        char,
        "dynamic:cot-leak-test",
        "dynamics",
        automatic=True,
        limits={"max_per_day": 0},
        epoch=dynamics.operations.policy()["epoch"],
        binding=publication_binding(
            "dynamics", "100", char, "dynamic:cot-leak-test",
            {"platform": "bilibili", "resource_type": "dynamic", "post_id": post_id, "recipient_uid": ""},
            {"kind": "public_operations", "local_id": decision["id"], "platform_id": str(post_id)},
        ),
        content_hash=content_hash({"text": POST21_LEAKED_DRAFT}),
    )
    dynamics.operations.finish(token, "rejected")
    binding = publication_binding(
        "dynamics", "100", char, "dynamic:cot-leak-test",
        {"platform": "bilibili", "resource_type": "dynamic", "post_id": post_id, "recipient_uid": ""},
        {"kind": "public_operations", "local_id": decision["id"], "platform_id": str(post_id)},
    )

    async def prepare():
        return PublicationContext(
            binding,
            POST21_LEAKED_DRAFT,
            char,
            {"public_sources": []},
            lambda: None,
            payload={"text": POST21_LEAKED_DRAFT},
        )

    async def rejected_send(revision_ticket, send_binding, body_hash):
        published_calls.append(POST21_LEAKED_DRAFT)
        err = platform_rejection({"code": 4126115, "message": "文字内容太长了啦~"}, fallback="too long")
        err.write_token = token
        err.body_hash = content_hash({"text": POST21_LEAKED_DRAFT})
        err.sent_payload = {"text": POST21_LEAKED_DRAFT}
        raise err

    result = await FeedbackPublication(runtime, store, dynamics.operations).submit(
        "dynamics",
        post_id,
        prepare=prepare,
        send=rejected_send,
        apply_revision=lambda prepared, text: dynamics._apply_revision(post_id, prepared, text),
        automatic=True,
        epoch=dynamics.operations.policy()["epoch"],
    )
    assert len(published_calls) == 1
    assert result["status"] == "pending_review"
    cont = runtime.continuations.find("dynamics", post_id)
    assert cont["state"] == "awaiting_review"
    assert cont["feedback"]["kind"] == "style_diagnostic"
    assert "思考过程泄露" in cont["feedback"]["message"]
    assert not store.all("SELECT * FROM operation_revision_tickets")


async def test_publish_failed_post_can_be_edited_approved_and_republished(settings, store, monkeypatch):
    sent = []

    class DummyAdapter:
        async def account_id(self):
            return "100"

    account = SimpleNamespace(
        adapter=DummyAdapter(),
        credential_lock=asyncio.Lock(),
        credentials={"sessdata": "s", "csrf": "c"},
        credentials_configured=True,
        display_account_id=lambda: "100",
        account_id=DummyAdapter().account_id,
    )
    runtime = SimpleNamespace(operation_llm=SimpleNamespace(config={}), generation_lock=asyncio.Lock())
    dynamics = Dynamics(settings, store, runtime, SimpleNamespace(account=account), account=account)
    char = store.character()
    dynamics.operations.configure({"enabled": True}, account_id="100", character=char)

    decision = dynamics._save_decision(
        char,
        True,
        {
            "action": "post",
            "reason": "test",
            "content": "原稿发布失败",
            "source_ids": [],
            "next_evaluation_seconds": 1800,
        },
        [],
        schedule=False,
        account_id="100",
        decide_image=False,
    )
    post_id = decision["post_id"]
    dynamics._status(post_id, "publish_failed", "平台拒绝动态写入")
    FeedbackPublication(runtime, store, dynamics.operations)

    edited = dynamics.review(post_id, "edit", "改好之后的清晰动态正文。")
    assert edited["status"] == "pending_review" and edited["content"] == "改好之后的清晰动态正文。"

    approved = dynamics.review(post_id, "approve", review_key=edited["review_key"])
    assert approved["status"] == "approved"

    async def fake_publish_text(adapter, text, *, before_send=None, **kwargs):
        before_send()
        sent.append(text)
        return {"platform": "bilibili", "code": 0, "dynamic_id": "999888"}

    monkeypatch.setattr(publisher_module, "publish_text", fake_publish_text)
    published = await dynamics.publish(post_id)
    assert published["status"] == "published"
    assert sent == ["改好之后的清晰动态正文。"]


def test_published_dynamic_direct_edit_with_restored_links_and_redraft_endpoints(settings, monkeypatch):
    from io import BytesIO

    from PIL import Image, ImageFont

    monkeypatch.setattr(comment_card_module, "_font", lambda size: ImageFont.load_default())
    app = create_app(settings, adapter=ControlledPlatform())
    studio = app.state.dynamics
    identifier = "1275098382031912982"
    source = {
        "id": "comment:BV1q5WZ6HEFD:321197068865:325013871744",
        "type": "public_comment",
        "fictional": False,
        "participants": [{"name": "风月星云的大肥鱼", "uid": "3747587614050633"}],
        "reference": {
            "author_name": "风月星云的大肥鱼",
            "author_uid": "3747587614050633",
            "comment_id": "325013871744",
            "resource": "BV1q5WZ6HEFD",
            "thread_id": "321197068865",
        },
        "source_url": "https://www.bilibili.com/video/BV1q5WZ6HEFD#reply325013871744",
        "text": "十袋米也就我一天的量吧，主人别大惊小怪。",
    }
    monkeypatch.setattr(studio, "_sources", lambda **_: [source])
    old = "十袋米当日常？这饭量我饭盒都装不下。白饭刚揭盖，别偷吃。[doge_金箍]"
    new_caption = "前几天刷 BV1q5WZ6HEFD 看到评论区聊一天吃十袋米，今天刚开锅的白饭先放这儿，只许看不许偷吃。"
    picture = {"img_src": "https://i0.hdslb.com/bfs/new_dyn/1.png", "img_width": 960, "img_height": 512}
    remote = {"text": old, "pictures": [picture]}
    writes = []
    uploads = []

    async def fake_read(adapter, target, account):
        assert target == identifier and account == "100"
        return dict(remote)

    async def fake_upload(adapter, image, *, authorized):
        assert authorized()
        uploads.append(image)
        return {
            "url": "https://i0.hdslb.com/bfs/new_dyn/card_page_1.png",
            "width": 960,
            "height": 420,
            "imageType": "png",
            "original": 1,
            "size": 12.5,
        }

    async def fake_write(adapter, target, text, pictures, *, before_send):
        before_send()
        writes.append((target, text, pictures))
        # Simulate Bilibili CDN returning i1 instead of i0 on subsequent detail read
        remote["text"] = text
        remote["pictures"] = [
            p | {"img_src": p["img_src"].replace("i0.hdslb.com", "i1.hdslb.com")}
            for p in pictures
        ]

    monkeypatch.setattr(drafter_module, "read_dynamic_caption", fake_read)
    monkeypatch.setattr(publisher_module, "read_dynamic_caption", fake_read)
    monkeypatch.setattr(publisher_module, "upload_image", fake_upload)
    monkeypatch.setattr(publisher_module, "edit_dynamic_caption", fake_write)

    with TestClient(app, base_url="http://127.0.0.1:8020", client=("127.0.0.1", 52000)) as client:
        app.state.browsing.discovery.remember_account("100")
        char = app.state.runtime.character
        decision = studio._save_decision(
            char,
            False,
            {
                "action": "post",
                "reason": "评论区互动",
                "content": old,
                "source_ids": [source["id"]],
                "illustration": "comment_card",
                "next_evaluation_seconds": 1800,
                "presentation": {"image_choice": "shared", "link_source_ids": [], "mentions": [], "points": []},
            },
            [source],
            schedule=False,
            account_id="100",
        )
        post_id = decision["post_id"]
        studio._status(post_id, "published", receipt={"dynamic_id": identifier, "code": 0, "platform": "bilibili"})
        post_before = studio._post(post_id)
        assert post_before["outgoing_links"] == []
        # Even when source_comment_card holds the card (comment_card=None), comment_card_image still renders it
        if post_before.get("comment_card") is not None:
            app.state.runtime.store.execute(
                "UPDATE dynamic_posts SET presentation=? WHERE id=?",
                (json.dumps({"image_choice": "picture", "link_source_ids": [], "mentions": [], "points": []}), post_id),
            )
            assert studio._post(post_id)["comment_card"] is None and studio._post(post_id)["source_comment_card"]
        png_bytes = studio.comment_card_image(post_id, 0)
        with Image.open(BytesIO(png_bytes)) as img:
            assert img.format == "PNG" and img.width == 960

        # Direct edit restores source link AND uploads + attaches comment card PNG before meme picture
        resp = client.post(
            f"/api/v1/operations/dynamics/posts/{post_id}/edit",
            json={
                "caption": new_caption,
                "include_source_links": True,
                "include_comment_card": True,
                "apply": True,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "applied"
        expected_link = "https://www.bilibili.com/video/BV1q5WZ6HEFD#reply325013871744"
        assert expected_link in data["outgoing"]
        assert len(uploads) == 1 and uploads[0]["mime_type"] == "image/png"
        assert len(writes) == 1
        assert [p["img_src"] for p in writes[0][2]] == [
            "https://i0.hdslb.com/bfs/new_dyn/card_page_1.png",
            "https://i0.hdslb.com/bfs/new_dyn/1.png",
        ]
        updated = studio._post(post_id)
        assert updated["content"] == new_caption
        assert updated["outgoing_links"] == [expected_link]

        # Redraft clones a fresh pending_review draft carrying sources & restored links
        redraft_resp = client.post(
            f"/api/v1/operations/dynamics/posts/{post_id}/redraft",
            json={"text": new_caption + "（重发版）", "include_source_links": True},
        )
        redraft = redraft_resp.raise_for_status().json()
        assert redraft["id"] != post_id
        assert redraft["status"] == "pending_review"
        assert redraft["outgoing_links"] == [expected_link]
        assert redraft["comment_card"] is not None
