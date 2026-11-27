"""Tests for explicit UID-based mention authentication and third-party dialogue boundaries."""

from tuber_studio.operations.kernel.inputs import bind_response_actors
from tuber_studio.persona.context.tree import (
    build_base_node,
    create_context_tree,
    fork_comment_floor_branch,
    render_openai_messages,
)
from tuber_studio.persona.self_model import build_metacognitive_profile, render_self_model_prompt


def test_third_party_same_name_mention_is_flagged_as_third_party_dialogue():
    actor = {
        "platform": "bilibili",
        "account_uid": "10001",
        "account_display_name": "大肥鱼_Official",
        "character_id": "whale",
        "character_name": "大肥鱼",
    }
    # Comment: User A says "@大肥鱼快来看", but the mentioned UID is "99999" (another user/pet/friend), not 10001!
    data = {
        "thread": {
            "source": {
                "comment_id": "c1",
                "author_uid": "20002",
                "author_name": "路人甲",
                "text": "@大肥鱼 快来看这个好看的视频！",
            },
            "addressing": {
                "source_mentions": [{"uid": "99999", "name": "大肥鱼"}],
                "source_parent_comment_id": None,
                "source_root_comment_id": "c1",
            },
            "root": {"comment_id": "c1", "author_uid": "20002", "text": "@大肥鱼 快来看这个好看的视频！"},
            "parent": None,
            "timeline": [],
        }
    }
    bound = bind_response_actors(data, actor)
    addressing = bound["thread"]["addressing"]

    assert addressing["is_explicitly_mentioned"] is False
    assert addressing["is_reply_to_self"] is False
    assert addressing["has_third_party_mention"] is True
    assert addressing["conversation_nature"] == "third_party_dialogue"
    assert addressing["addressed_to"][0]["relation_to_self"] == "other"
    assert addressing["addressed_to"][0]["uid"] == "99999"


def test_actual_self_uid_mention_is_flagged_as_direct_interaction():
    actor = {
        "platform": "bilibili",
        "account_uid": "10001",
        "account_display_name": "大肥鱼_Official",
        "character_id": "whale",
        "character_name": "大肥鱼",
    }
    # Comment explicitly @'s our own account UID 10001
    data = {
        "thread": {
            "source": {
                "comment_id": "c2",
                "author_uid": "30003",
                "author_name": "真粉丝",
                "text": "@大肥鱼_Official 鱼宝今天播什么游戏呀？",
            },
            "addressing": {
                "source_mentions": [{"uid": "10001", "name": "大肥鱼_Official"}],
                "source_parent_comment_id": None,
                "source_root_comment_id": "c2",
            },
            "root": {"comment_id": "c2", "author_uid": "30003", "text": "@大肥鱼_Official 鱼宝今天播什么游戏呀？"},
            "parent": None,
            "timeline": [],
        }
    }
    bound = bind_response_actors(data, actor)
    addressing = bound["thread"]["addressing"]

    assert addressing["is_explicitly_mentioned"] is True
    assert addressing["conversation_nature"] == "direct_interaction_with_self"
    assert addressing["addressed_to"][0]["relation_to_self"] == "self"


def test_reply_to_own_comment_is_flagged_as_direct_interaction():
    actor = {
        "platform": "bilibili",
        "account_uid": "10001",
        "account_display_name": "大肥鱼_Official",
        "character_id": "whale",
        "character_name": "大肥鱼",
    }
    # User replies directly under a comment posted by our own account (parent author_uid == 10001)
    data = {
        "thread": {
            "source": {
                "comment_id": "c3",
                "author_uid": "40004",
                "author_name": "观众B",
                "text": "我也觉得这关太难了！",
            },
            "addressing": {
                "source_mentions": [],
                "source_parent_comment_id": "c_own",
                "source_root_comment_id": "c_root",
            },
            "root": {"comment_id": "c_root", "author_uid": "50005", "text": "根楼评论"},
            "parent": {"comment_id": "c_own", "author_uid": "10001", "text": "大肥鱼之前发的主评"},
            "timeline": [],
        }
    }
    bound = bind_response_actors(data, actor)
    addressing = bound["thread"]["addressing"]

    assert addressing["is_reply_to_self"] is True
    assert addressing["conversation_nature"] == "direct_interaction_with_self"


def test_plain_text_at_handles_without_structured_mentions_flag_third_party_dialogue():
    actor = {
        "platform": "bilibili",
        "account_uid": "10001",
        "account_display_name": "大肥鱼_Official",
        "character_id": "whale",
        "character_name": "Aqua",
    }
    data = {
        "thread": {
            "source": {
                "comment_id": "c4",
                "author_uid": "20002",
                "author_name": "观众甲",
                "text": "@Eeeveevee @Mocha_Eeeveevee 你俩也是用了同一个对话模板",
            },
            "addressing": {
                "source_mentions": None,
                "source_parent_comment_id": "c0",
                "source_root_comment_id": "c0",
            },
            "parent": {"comment_id": "c0", "author_uid": "10001", "text": "哈哈挺像的"},
            "timeline": [],
        }
    }
    bound = bind_response_actors(data, actor)
    addressing = bound["thread"]["addressing"]

    assert addressing["is_explicitly_mentioned"] is False
    assert addressing["has_third_party_mention"] is True
    assert addressing["conversation_nature"] == "third_party_dialogue"
    names = {item["name"] for item in addressing["addressed_to"] if item.get("relation_to_self") == "other"}
    assert {"Eeeveevee", "Mocha_Eeeveevee"} <= names


def test_floor_branch_frames_third_party_public_comments_neutrally_and_gates_zhao_mao():
    card = {
        "id": "whale",
        "name": "Aqua",
        "display_name": "Aqua",
        "aliases": ["大肥鱼", "小鲸鱼"],
        "rules": [],
        "core": {"identity": "蓝发小鲸鱼少女 Aqua。"},
    }
    self_actor = {
        "platform": "bilibili",
        "account_uid": "10001",
        "account_display_name": "Aqua_Official",
        "character_id": "whale",
        "character_name": "Aqua",
    }
    base = build_base_node(
        card,
        channel="video_browsing",
        self_actor=self_actor,
        operating_account=self_actor,
    )
    tree = create_context_tree(base)

    public_branch = fork_comment_floor_branch(
        tree,
        root_id="801",
        timeline=[{"comment_id": "801", "author_uid": "30001", "author_name": "路人", "text": "善良的大肥鱼可爱滴捏🥰催更！"}],
        reply_target={"comment_id": "801", "author_uid": "30001", "author_name": "路人", "text": "善良的大肥鱼可爱滴捏🥰催更！"},
        self_uid="10001",
        uploader_uid="88888",
        addressing={"conversation_nature": "general_public_comment"},
        video_author={"uid": "88888", "name": "原作UP", "is_self": False},
    )
    public_text = "\n".join(m["content"] for m in render_openai_messages(tree, public_branch.node_id))
    assert "第三方作者，不是你" in public_text
    assert "当前浏览到的公开评论（作者：路人" in public_text
    assert "请针对 路人" not in public_text
    assert "切勿对号入座" in public_text

    direct_branch = fork_comment_floor_branch(
        tree,
        root_id="802",
        timeline=[{"comment_id": "802", "author_uid": "30002", "author_name": "粉丝", "text": "@Aqua_Official 大肥鱼今天怎么摸鱼啦"}],
        reply_target={"comment_id": "802", "author_uid": "30002", "author_name": "粉丝", "text": "@Aqua_Official 大肥鱼今天怎么摸鱼啦"},
        self_uid="10001",
        uploader_uid="88888",
        addressing={"conversation_nature": "direct_interaction_with_self"},
        video_author={"uid": "88888", "name": "原作UP", "is_self": False},
    )
    direct_text = "\n".join(m["content"] for m in render_openai_messages(tree, direct_branch.node_id))
    assert "请针对 粉丝" in direct_text
    assert "炸毛纠正" in direct_text

    public_prompt = render_self_model_prompt(
        build_metacognitive_profile(card, channel="video_browsing", self_actor=self_actor),
        character=card,
    )
    assert "炸毛纠正" not in public_prompt
    assert "不把别人做过的事说成自己亲身经历" in public_prompt
