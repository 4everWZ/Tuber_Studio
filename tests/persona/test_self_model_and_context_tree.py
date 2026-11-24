"""Tests for Metacognitive Self-Model and universal ContextNode Base->Branches tree."""

import json

from tuber_studio.infra.inference.providers import single_system_messages
from tuber_studio.persona.context.tree import (
    DEFAULT_VIDEO_BASE_TOKEN_LIMIT,
    append_assistant_tool_calls,
    append_tool_result,
    build_base_node,
    create_context_tree,
    fork_comment_floor_branch,
    fork_dm_session_branch,
    fork_owner_chat_branch,
    fork_top_level_comment_branch,
    render_openai_messages,
    slice_video_segments,
    to_responses_input,
)
from tuber_studio.persona.self_model import (
    build_metacognitive_profile,
    build_self_model_system_message,
    render_self_model_prompt,
)


def _whale_card():
    return {
        "id": "whale",
        "name": "大肥鱼",
        "display_name": "大肥鱼",
        "aliases": ["鱼鱼", "鲸鲸", "小鲸鱼"],
        "owner_address": "老板",
        "self_description": "我是机灵傲娇的蓝发鲸鱼娘小鲸鱼。",
        "speaking_style": "傲娇毒舌又机灵，遇到胡说八道直接拆穿。",
        "core": {
            "identity": "你是蓝发鲸鱼娘小鲸鱼，最讨厌被叫大肥鱼。",
            "background": "平时刷视频、读评论、和熟人拌嘴。",
            "interaction": "就事论事，分清自己和他人。",
            "traits": ["傲娇毒舌", "清醒反洗脑"],
            "blacklist": ["首先", "综上所述"],
        },
        "style": {"min_chars": 1, "max_chars": 120, "instructions": "自然口语接话。"},
        "rules": ["绝不自称大肥鱼。", "不迎合荒诞谣言。"],
        "bilibili_owner": {"uid": "9001", "name": "阿鲸老板"},
    }


def _video_material(segments_count: int = 4, text_len: int = 30):
    segs = [
        {
            "id": f"seg-{i}",
            "start": float(i * 15),
            "end": float(i * 15 + 10),
            "text": f"第{i}段测试字幕内容：" + ("甲乙丙丁戊" * max(1, text_len // 5)),
        }
        for i in range(segments_count)
    ]
    return {
        "metadata": {
            "bvid": "BV1xx411c7mD",
            "title": "开源机器人实测演示",
            "owner": "硬核UP",
            "owner_uid": "5001",
            "description": "本视频实测机械臂抓取精度与语音交互延迟。",
            "duration": float(max(60, segments_count * 15)),
            "tags": ["机器人", "测评"],
            "parts": [{"cid": "10001", "page": 1, "part": "开箱实测", "duration": float(max(60, segments_count * 15))}],
        },
        "parts": [
            {"cid": "10001", "kind": "subtitle", "language": "zh-CN", "segments": segs},
            {
                "cid": "10001",
                "visual": True,
                "segments": [{"id": "frame-1", "start": 18.0, "end": 18.0, "text": "桌面上摆着蓝色机械臂与示波器"}],
            },
        ],
    }


def test_self_model_has_epistemic_boundaries_and_no_legacy_owner_jargon():
    card = _whale_card()
    for channel in ("owner_chat", "video_comment", "floor_reply", "direct_message", "dynamic", "live"):
        profile = build_metacognitive_profile(
            card,
            channel=channel,
            self_actor={"platform": "bilibili", "account_uid": "8888", "account_display_name": "小鲸鱼官号"},
        )
        prompt = render_self_model_prompt(profile, character=card)
        assert "工坊号主" not in prompt
        assert "小鲸鱼" in prompt
        assert "大肥鱼" in profile.forbidden_self_labels
        assert "Jetson" in prompt and "MCN" in prompt
        assert "[ref:" not in prompt and "evidence_ids" not in prompt
        msg = build_self_model_system_message(card, channel=channel)
        assert msg["role"] == "system"
        assert "工坊号主" not in msg["content"]
    owner_prompt = render_self_model_prompt(build_metacognitive_profile(card, channel="owner_chat"), character=card)
    assert "老板" in owner_prompt


def test_video_base_keeps_full_transcript_under_24k_and_degrades_over_24k():
    card = _whale_card()
    short_mat = _video_material(segments_count=5, text_len=25)
    base_full = build_base_node(
        card,
        channel="video_browsing",
        video_material=short_mat,
        first_impression="这个机械臂抓取挺稳的。",
        memories=[{"content": "看到吹牛科普会先吐槽核实", "active": True}],
        memory_scope="public",
    )
    assert base_full.degraded is False
    full_text = base_full.messages[1]["content"]
    assert "开源机器人实测演示" in full_text
    assert "硬核UP" in full_text
    assert "第0段测试字幕内容" in full_text
    assert "桌面上摆着蓝色机械臂与示波器" in full_text
    assert "[ref:" not in full_text
    assert base_full.messages[2] == {"role": "assistant", "content": "这个机械臂抓取挺稳的。"}

    # >24K tokens triggers chapter digest degradation
    assert DEFAULT_VIDEO_BASE_TOKEN_LIMIT == 24_000
    huge_mat = _video_material(segments_count=120, text_len=250)
    base_degraded = build_base_node(card, channel="video_browsing", video_material=huge_mat)
    assert base_degraded.degraded is True
    degraded_text = base_degraded.messages[1]["content"]
    assert "分章节摘要" in degraded_text
    assert "read_video_segment" in degraded_text
    assert "start_seconds=" in degraded_text

    # slice_video_segments fetches bounded chunks for read_video_segment
    sliced = slice_video_segments(huge_mat, part=1, start_seconds=300, end_seconds=450, max_chars=900)
    assert sliced["ok"] is True
    assert sliced["bvid"] == "BV1xx411c7mD"
    assert sliced["segment_count"] >= 1
    assert sliced["has_more"] is True
    assert sliced["next_start_seconds"] is not None


def test_context_tree_forks_byte_identical_base_prefix_across_branches():
    card = _whale_card()
    actor = {"platform": "bilibili", "account_uid": "100", "account_display_name": "小鲸鱼号"}
    base = build_base_node(
        card,
        channel="video_browsing",
        self_actor=actor,
        memories=[{"content": "公开偏好：喜欢实测硬件", "active": True}],
        memory_scope="public",
        video_material=_video_material(segments_count=3),
    )
    tree = create_context_tree(base)

    top = fork_top_level_comment_branch(
        tree,
        existing_hot_comments=[{"author_name": "热心路人", "text": "这个抓手看着好帅"}],
    )
    floor_1 = fork_comment_floor_branch(
        tree,
        root_id="1001",
        self_uid="100",
        uploader_uid="5001",
        confirmed_bot_comment_ids={"1002"},
        timeline=[
            {"comment_id": "1001", "thread_id": "1001", "author_uid": "301", "author_name": "路人甲", "text": "你是不是住Jetson里？"},
            {"comment_id": "1002", "thread_id": "1001", "parent_comment_id": "1001", "author_uid": "100", "author_name": "小鲸鱼号", "text": "少扯淡，我跑在工作站服务上。"},
            {"comment_id": "1003", "thread_id": "1001", "parent_comment_id": "1002", "author_uid": "100", "author_name": "小鲸鱼号", "text": "补充一句，视频里是机械臂不是Jetson。"},
            {"comment_id": "1004", "thread_id": "1001", "parent_comment_id": "1003", "author_uid": "5001", "author_name": "硬核UP", "text": "哈哈哈没错，这台是我自己焊的。"},
        ],
        reply_target={"comment_id": "1004", "thread_id": "1001", "parent_comment_id": "1003", "author_uid": "5001", "author_name": "硬核UP", "text": "哈哈哈没错，这台是我自己焊的。"},
    )
    dm = fork_dm_session_branch(
        tree,
        peer_uid="777",
        peer_name="老朋友",
        conversation=[
            {"message_id": "m1", "origin": "viewer", "text": "机械臂教程链接发一下？"},
            {"message_id": "m2", "origin": "operator", "text": "号主先贴了个仓库链接给你。"},
            {"message_id": "m3", "origin": "oc", "text": "先看那个开源地址，不懂再问我。"},
            {"message_id": "m4", "origin": "viewer", "text": "收到，固件版本用哪个？", "open": True},
        ],
    )

    # Append tool calls + tool output to floor_1
    append_assistant_tool_calls(
        tree,
        floor_1.node_id,
        [{"call_id": "call_1", "name": "search_web", "arguments": {"query": "开源机械臂固件"}}],
    )
    append_tool_result(tree, floor_1.node_id, tool_call_id="call_1", name="search_web", result={"hits": ["v1.2"]})

    msgs_top = render_openai_messages(tree, top.node_id)
    msgs_floor = render_openai_messages(tree, floor_1.node_id)
    msgs_dm = render_openai_messages(tree, dm.node_id)

    base_len = len(base.messages)
    raw_base = json.dumps(tree.base_prefix(), ensure_ascii=False)
    assert json.dumps(msgs_top[:base_len], ensure_ascii=False) == raw_base
    assert json.dumps(msgs_floor[:base_len], ensure_ascii=False) == raw_base
    assert json.dumps(msgs_dm[:base_len], ensure_ascii=False) == raw_base

    # Even after single_system_messages(), Base prefix stays byte-identical!
    assert json.dumps(single_system_messages(msgs_top)[:base_len], ensure_ascii=False) == raw_base
    assert json.dumps(single_system_messages(msgs_floor)[:base_len], ensure_ascii=False) == raw_base
    assert json.dumps(single_system_messages(msgs_dm)[:base_len], ensure_ascii=False) == raw_base

    # Confirmed bot reply -> assistant; owner takeover -> user [人工接管]
    assert any(m["role"] == "assistant" and "少扯淡" in m["content"] for m in msgs_floor)
    assert any(m["role"] == "user" and "[人工接管" in m["content"] and "机械臂不是Jetson" in m["content"] for m in msgs_floor)
    assert not any(m["role"] == "assistant" and "机械臂不是Jetson" in m["content"] for m in msgs_floor)

    # DM operator takeover -> user [人工接管回复], oc -> assistant, open viewer -> [待回复]
    assert any(m["role"] == "user" and "[人工接管回复]" in m["content"] for m in msgs_dm)
    assert any(m["role"] == "assistant" and "先看那个开源地址" in m["content"] for m in msgs_dm)
    assert any(m["role"] == "user" and "[待回复]" in m["content"] for m in msgs_dm)

    # Tool calls & tool results survive rendering and Responses conversion
    assert msgs_floor[-2]["role"] == "assistant" and msgs_floor[-2]["tool_calls"][0]["function"]["name"] == "search_web"
    assert msgs_floor[-1]["role"] == "tool" and msgs_floor[-1]["tool_call_id"] == "call_1"
    resp_items = to_responses_input(msgs_floor)
    assert any(item.get("type") == "function_call" and item["name"] == "search_web" for item in resp_items)
    assert any(item.get("type") == "function_call_output" and item["call_id"] == "call_1" for item in resp_items)


def test_owner_chat_branch_isolation():
    card = _whale_card()
    base = build_base_node(
        card,
        channel="owner_chat",
        memories=[{"content": "老板喜欢简洁回答", "active": True}],
        memory_scope="owner",
    )
    tree = create_context_tree(base)
    branch = fork_owner_chat_branch(
        tree,
        history=[
            {"role": "user", "content": "早啊"},
            {"role": "assistant", "content": "早，今天要看什么？"},
        ],
        current_message="帮我查查昨天那条评论。",
    )
    msgs = render_openai_messages(tree, branch.node_id)
    assert msgs[0]["role"] == "system"
    assert "私有长期记忆" in msgs[0]["content"]
    assert "老板喜欢简洁回答" in msgs[0]["content"]
    assert "工坊号主" not in msgs[0]["content"]
    assert msgs[1:] == [
        {"role": "user", "content": "早啊"},
        {"role": "assistant", "content": "早，今天要看什么？"},
        {"role": "user", "content": "帮我查查昨天那条评论。"},
    ]
