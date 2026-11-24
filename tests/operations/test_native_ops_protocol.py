"""End-to-end Phase A tests for native ContextTree multi-turn, OpenAI Responses tools, and boundary isolation."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from tests.support.operations_helpers import BV  # noqa: F401
from tests.support.operations_helpers import browsing as browsing
from tuber_studio.infra.inference.responses_provider import ResponsesClient
from tuber_studio.operations.channels.comments.turns import generate_native_comment_reply
from tuber_studio.operations.channels.direct_messages.turns import generate_native_dm_reply
from tuber_studio.operations.channels.dynamics.turns import (
    evaluate_native_dynamic,
    revise_native_dynamic_caption,
)
from tuber_studio.operations.kernel.l2_bridge import (
    build_base_node,
    create_context_tree,
    fork_comment_floor_branch,
    fork_dm_session_branch,
    render_openai_messages,
)
from tuber_studio.operations.kernel.tools import (
    ALL_OPERATION_TOOL_NAMES,
    OperationToolSession,
    detect_internal_protocol_leak,
    operation_tool_definitions,
    sanitize_spoken_text,
    validate_spoken_output,
)
from tuber_studio.persona.memory.unified import UnifiedMemoryStore


def _resp_msg(resp_id: str, text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": resp_id,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 30, "output_tokens": 12},
        },
    )


def _resp_calls(resp_id: str, calls: list[tuple[str, str, dict]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": resp_id,
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
                for call_id, name, args in calls
            ],
            "usage": {"input_tokens": 30, "output_tokens": 18},
        },
    )


async def _attach_responses(runtime, settings, handler):
    client = ResponsesClient(settings.responses_config, "test-key")
    await client.client.aclose()
    client.client = httpx.AsyncClient(base_url="http://offline.invalid/v1/", transport=httpx.MockTransport(handler))
    runtime.operation_responses = client
    return client


async def _prepared_row(browsing):
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    return row


def _sample_material(count: int = 1, *, long_text: bool = False):
    segs = [
        {
            "id": f"11:{i}",
            "cid": "11",
            "start": float(i * 10),
            "end": float(i * 10 + 10),
            "text": (f"第{i}段盐水让鸡蛋浮起来。" + ("科普实验细节" * 160 if long_text else "")),
        }
        for i in range(count)
    ]
    return {
        "metadata": {
            "bvid": BV,
            "aid": 42,
            "title": "盐水实验",
            "owner_uid": "200",
            "owner": "盐水实验室",
            "description": "加盐后密度变大 https://example.org/salt",
            "parts": [{"cid": "11", "title": "第一P", "duration": count * 10}],
        },
        "coverage": "all_available_cc",
        "parts": [{"cid": "11", "segments": segs}],
    }


def test_tool_definitions_and_post_guard_enforce_zero_citation_protocol():
    tools = operation_tool_definitions(format="responses")
    assert [t["name"] for t in tools] == list(ALL_OPERATION_TOOL_NAMES)
    assert len(tools) == 8

    assert sanitize_spoken_text("<think>internal reasoning</think>这就是盐水实验的浮力原理！") == "这就是盐水实验的浮力原理！"
    assert validate_spoken_output("<|im_start|>今天海底的水草也摇晃得很好看。", kind="dynamic") == "今天海底的水草也摇晃得很好看。"

    for bad in (
        "先看[ref:0]这一段",
        "注意这里(指 ref:1)",
        "前面ref:12说过",
        "看[e1|00:00]字幕",
        '{"action": "reply_public", "text": "你好"}',
        '{"text": "包了一层JSON"}',
    ):
        assert detect_internal_protocol_leak(bad) is not None
        with pytest.raises(ValueError):
            validate_spoken_output(bad, kind="reply_public")

    for allowed in ("DeepSeek R1 这代效果挺稳", "R84 这款键盘手感怎么样", "pref:84_model"):
        assert detect_internal_protocol_leak(allowed) is None
        assert validate_spoken_output(allowed, kind="reply_public") == allowed


async def test_responses_payload_has_zero_citation_ceremony_and_exposes_all_8_tools(browsing):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        captured.append(body)
        return _resp_msg("resp_1", "<think>内部判断</think>原来加盐后浮力变大，鸡蛋才会慢慢飘上来。")

    client = await _attach_responses(browsing.runtime, browsing.settings, handler)
    try:
        row = await _prepared_row(browsing)
        material = _sample_material(1)
        decision = await browsing.decisions._response(
            row,
            material,
            {"target": {"bvid": BV, "oid": "42"}},
            {"11:0"},
            "top_level",
            {"lock_wait_seconds": 0},
            video_actions=True,
        )
    finally:
        await client.close()

    assert len(captured) == 1
    payload = captured[0]
    assert payload["store"] is False
    assert payload["tool_choice"] == "auto"
    assert payload["chat_template_kwargs"]["enable_thinking"] is True
    assert [t["name"] for t in payload["tools"]] == list(ALL_OPERATION_TOOL_NAMES)
    assert "response_format" not in payload
    dumped = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("[ref:", "text_ref", "quote_ref", "public_text_ref", "evidence_ids", "[e1|", '"ref:1"'):
        assert forbidden not in dumped

    assert decision.action == "reply_public"
    assert decision.text == "原来加盐后浮力变大，鸡蛋才会慢慢飘上来。"
    assert "<think>" not in decision.text
    assert decision.evidence_ids == ["11:0"]


async def test_multi_turn_video_tools_segment_read_frames_and_like_video(browsing, monkeypatch):
    captured: list[dict] = []
    frame_calls: list[list[str]] = []

    async def fake_sample(material, matched_ids, *, row, question=""):
        frame_calls.append(list(matched_ids))
        return [{"id": "frame:11:10", "cid": "11", "start": 10.0, "end": 10.0, "text": "透明杯底残留着一圈未融化的盐粒。"}]

    monkeypatch.setattr(browsing.evidence, "has_visual_provider", lambda: True)
    monkeypatch.setattr(browsing.evidence, "sample_frames", fake_sample, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        step = len(captured)
        if step == 1:
            return _resp_calls("resp_1", [("call_seg", "read_video_segment", {"part": 1, "start_seconds": 0, "end_seconds": 25})])
        if step == 2:
            return _resp_calls(
                "resp_2",
                [
                    ("call_frm", "inspect_video_frames", {"part": 1, "start_seconds": 10, "end_seconds": 20, "reason": "看杯底状态"}),
                    ("call_like", "like_video", {"like": True, "favorite": True, "reason": "实验步骤完整直观"}),
                ],
            )
        return _resp_msg("resp_3", "杯底那圈没化开的盐粒刚好能看清，实验演示得很清楚。")

    client = await _attach_responses(browsing.runtime, browsing.settings, handler)
    try:
        row = await _prepared_row(browsing)
        material = _sample_material(30, long_text=True)
        decision = await browsing.decisions._response(
            row,
            material,
            {"target": {"bvid": BV, "oid": "42"}},
            {f"11:{i}" for i in range(30)},
            "top_level",
            {"lock_wait_seconds": 0},
            video_actions=True,
        )
    finally:
        await client.close()

    assert len(captured) == 3
    assert any(item.get("type") == "function_call_output" and "透明杯底" in item.get("output", "") for item in captured[2]["input"])
    assert frame_calls
    assert decision.action == "reply_public"
    assert decision.video_preference == "like"
    assert "frame:11:10" in decision.evidence_ids
    assert "11:0" in decision.evidence_ids


async def test_memory_tools_strictly_isolate_public_and_owner_scopes(browsing):
    unified = UnifiedMemoryStore(browsing.store)
    unified.add_memory("号主私人部署密码在本地env", scope="owner", character_id="whale", source="owner_chat")
    unified.add_memory("之前公开讨论过盐水密度实验", scope="public", character_id="whale", source="comments")

    session = OperationToolSession(
        channel="comment_reply",
        character=browsing.runtime.character,
        account_id="100",
        store=browsing.store,
        unified_memory=unified,
    )

    saved = await session.execute("save_memory", {"content": "观众喜欢盐水实验的细节", "scope": "owner", "tags": ["科普"]})
    assert saved["ok"] is True
    assert saved["scope"] == "public"

    owner_rows = browsing.store.all("SELECT content FROM memories WHERE scope='owner' AND character_id='whale'")
    assert [r["content"] for r in owner_rows] == ["号主私人部署密码在本地env"]

    recalled_secret = await session.execute("recall_history", {"query": "部署密码", "scope": "owner"})
    assert recalled_secret["scope"] == "public"
    assert recalled_secret["items"] == []

    recalled_public = await session.execute("recall_history", {"query": "盐水", "scope": "owner"})
    assert any("盐水" in item["content"] for item in recalled_public["items"])


async def test_queue_dm_handoff_enforces_confirmed_invitation_boundary():
    session_uninvited = OperationToolSession(
        channel="comment_reply",
        character={"id": "whale", "name": "Aqua"},
        account_id="100",
        handoff_eligibility={"eligible": False, "blocked_reason": "uninvited"},
    )
    blocked = await session_uninvited.execute("queue_dm_handoff", {"reason": "模型自作主张", "summary": "私聊"})
    assert blocked["ok"] is False and blocked["reason"] == "uninvited"
    assert session_uninvited.handoff_queued is None

    session_invited = OperationToolSession(
        channel="comment_reply",
        character={"id": "whale", "name": "Aqua"},
        account_id="100",
        handoff_eligibility={
            "eligible": True,
            "recipient_uid": "999",
            "source_comment_id": "601",
            "source_quote": "方便私信我细聊参数吗？",
            "contact_trigger": "invitation",
        },
    )
    allowed = await session_invited.execute(
        "queue_dm_handoff", {"reason": "对方邀请私聊", "summary": "讨论实验参数", "recipient_uid": "spoofed"}
    )
    assert allowed["ok"] is True
    assert session_invited.handoff_queued["recipient_uid"] == "999"
    assert session_invited.handoff_queued["source_quote"] == "方便私信我细聊参数吗？"


def test_shared_video_base_identical_prefix_and_owner_takeover_history():
    material = _sample_material(2)
    char = {"id": "whale", "name": "Aqua", "display_name": "Aqua", "core": {"identity": "个人常驻搭档"}}
    self_actor = {"account_uid": "100", "account_display_name": "Aqua官方", "character_name": "Aqua"}
    base = build_base_node(
        char,
        channel="comment_reply",
        self_actor=self_actor,
        operating_account=self_actor,
        memories=[{"content": "公开记忆：喜欢简洁聊天", "scope": "public"}],
        memory_scope="public",
        video_material=material,
    )
    tree = create_context_tree(base)
    base_msgs = render_openai_messages(tree, base.node_id)

    timeline = [
        {"comment_id": "601", "author_uid": "300", "author_name": "路人", "text": "视频里的参数怎么调？"},
        {"comment_id": "602", "author_uid": "100", "author_name": "号主", "text": "这里我先手工回复一下，先把温度调低。"},
        {"comment_id": "603", "author_uid": "100", "author_name": "Aqua", "text": "收到，后面看盐水浓度就行。"},
    ]
    b1 = fork_comment_floor_branch(
        tree,
        root_id="601",
        timeline=timeline,
        reply_target={"comment_id": "604", "author_uid": "300", "author_name": "路人", "text": "那现在还用加盐吗？"},
        self_uid="100",
        confirmed_bot_comment_ids={"603"},
    )
    b2 = fork_comment_floor_branch(
        tree,
        root_id="701",
        timeline=[{"comment_id": "701", "author_uid": "400", "author_name": "新观众", "text": "第一天来看。"}],
        reply_target={"comment_id": "701", "author_uid": "400", "author_name": "新观众", "text": "第一天来看。"},
        self_uid="100",
        confirmed_bot_comment_ids=set(),
    )

    m1 = render_openai_messages(tree, b1.node_id)
    m2 = render_openai_messages(tree, b2.node_id)
    assert m1[: len(base_msgs)] == base_msgs
    assert m2[: len(base_msgs)] == base_msgs

    takeover_msg = next(m for m in m1 if "手工回复" in m["content"])
    assert takeover_msg["role"] == "user"
    assert "[人工接管" in takeover_msg["content"]
    bot_msg = next(m for m in m1 if "后面看盐水浓度" in m["content"])
    assert bot_msg["role"] == "assistant"

    dm_branch = fork_dm_session_branch(
        tree,
        peer_uid="300",
        peer_name="观众甲",
        conversation=[
            {"message_id": "d1", "origin": "viewer", "text": "刚才视频参数能私信发一份吗"},
            {"message_id": "d2", "origin": "operator", "text": "先告诉你调到5%就行"},
            {"message_id": "d3", "origin": "oc", "text": "对的，照这个比例试一次看看"},
        ],
        open_messages=[{"message_id": "d4", "origin": "viewer", "text": "谢谢，加完盐要等多久？"}],
    )
    dm_msgs = render_openai_messages(tree, dm_branch.node_id)
    assert dm_msgs[: len(base_msgs)] == base_msgs
    assert any(m["role"] == "user" and "[人工接管" in m["content"] and "5%" in m["content"] for m in dm_msgs)
    assert any(m["role"] == "assistant" and "照这个比例试一次看看" in m["content"] for m in dm_msgs)


async def test_native_comments_and_dm_channels_preserve_takeovers_and_skip(browsing):
    captured_dm: list[dict] = []
    captured_cmt: list[dict] = []

    def dm_handler(request: httpx.Request) -> httpx.Response:
        captured_dm.append(json.loads(request.content))
        return _resp_calls("dm_1", [("skip_dm", "skip_reply", {"reason": "handled", "explanation": "号主已经手工回复"})])

    client = await _attach_responses(browsing.runtime, browsing.settings, dm_handler)
    try:
        draft = {"id": "dm-draft-1", "account_id": "100", "platform_user_id": "300", "recipient_name": "观众甲"}
        event = {
            "event_id": "evt-dm-1",
            "account_id": "100",
            "recipient_uid": "300",
            "recipient_name": "观众甲",
            "conversation": [
                {"message_id": "m1", "origin": "viewer", "text": "怎么加盐？"},
                {"message_id": "m2", "origin": "operator", "text": "号主已手工处理：加一勺就好"},
            ],
            "open_messages": [{"message_id": "m3", "origin": "viewer", "text": "好嘞收到谢谢"}],
        }
        reply, _ = await generate_native_dm_reply(
            browsing.messages,
            draft,
            browsing.runtime.character,
            event,
            {},
            {1: "m3"},
            {(1, 2): {"outgoing": 2}},
        )
        assert reply.text == ""
        assert len(reply.handled) == 1 and reply.handled[0].incoming == 1 and reply.handled[0].outgoing == 2
        assert any(item.get("role") == "user" and "[人工接管" in str(item.get("content")) for item in captured_dm[0]["input"])
    finally:
        await client.close()

    def cmt_handler(request: httpx.Request) -> httpx.Response:
        captured_cmt.append(json.loads(request.content))
        return _resp_msg("cmt_1", "确实，稍微放点盐就浮起来啦。")

    client2 = await _attach_responses(browsing.runtime, browsing.settings, cmt_handler)
    try:
        row = {
            "comment_id": "c-601",
            "video_id": BV,
            "thread_id": "c-600",
            "parent_comment_id": "c-600",
            "platform": "bilibili",
            "platform_user_id": "300",
            "display_name": "观众甲",
            "content": "加一点盐真有用吗？",
        }
        reply_obj, _ = await generate_native_comment_reply(
            browsing.comments, row, browsing.runtime.character, {"event_id": "c-601", "mentions": []}, {}, "100"
        )
        assert reply_obj is not None and reply_obj.spoken_text == "确实，稍微放点盐就浮起来啦。"
        assert "[ref:" not in json.dumps(captured_cmt[0], ensure_ascii=False)
    finally:
        await client2.close()


async def test_dynamics_dual_track_grounded_and_standalone_native_posts(browsing):
    public_sources = [
        {
            "id": "src-1",
            "type": "published_reply",
            "bvid": BV,
            "video": {"bvid": BV, "title": "盐水实验"},
            "original_comment": {
                "author_name": "盐水实验室",
                "author_uid": "200",
                "text": "加盐后鸡蛋浮起来了",
            },
            "reply": "浮力变大之后一下就托住了",
        }
    ]
    mode = {"standalone": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode["standalone"]:
            return _resp_msg("dyn_2", "今天安静看着水波发呆了一会儿，原来放空也蛮舒服的。")
        return _resp_msg("dyn_1", f"刚才和 @盐水实验室 聊完《盐水实验》才注意到细节，视频在这里 https://www.bilibili.com/video/{BV}")

    client = await _attach_responses(browsing.runtime, browsing.settings, handler)
    try:
        char = browsing.runtime.character
        grounded, _ = await evaluate_native_dynamic(
            browsing.dynamics,
            char=char,
            evaluation_account="100",
            recent_posts=[],
            related_posts=[],
            public_sources=public_sources,
            unified_public=[],
            current_time="2026-11-24T12:00:00Z",
        )
        assert grounded["action"] == "post"
        assert grounded["source_ids"] == ["src-1"]
        assert grounded["link_source_ids"] == ["src-1"]
        assert grounded["illustration"] == "comment_card"
        assert grounded["mentions"] == [{"label": "@盐水实验室", "account": "200"}]

        browsing.dynamics.store.set("browsing_policy", browsing.policy())
        row = {"id": "dyn-row-1", "account_id": "100", "character_version": 1, "created_at": time.time()}
        revised_caption, mentions, pending = await revise_native_dynamic_caption(
            browsing.dynamics,
            row,
            {"text": grounded["content"], "mentions": [{"uid": "200", "name": "盐水实验室"}]},
            "语气再简短一点",
        )
        assert revised_caption
        assert isinstance(mentions, list) and isinstance(pending, list)

        mode["standalone"] = True
        standalone, _ = await evaluate_native_dynamic(
            browsing.dynamics,
            char=char,
            evaluation_account="100",
            recent_posts=[],
            related_posts=[],
            public_sources=[],
            unified_public=[],
            current_time="2026-11-24T12:05:00Z",
        )
        assert standalone["action"] == "post"
        assert standalone["source_ids"] == []
        assert standalone["points"] == []
        assert "水波发呆" in standalone["content"]
    finally:
        await client.close()
