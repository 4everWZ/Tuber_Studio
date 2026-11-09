"""Tests for explicit UID-based mention authentication and third-party dialogue boundaries."""

import pytest
from tuber_studio.operations.browsing.inputs import bind_response_actors


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
