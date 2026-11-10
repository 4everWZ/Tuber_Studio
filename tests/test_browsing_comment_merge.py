import pytest
from test_browsing import BV, model_packet
from test_browsing import browsing as browsing
from test_browsing_comment_only import enable_comment_choices

from tuber_studio.operations.browsing.comment_reader import merge_continuous_comments
from tuber_studio.operations_control import canonical_intent


def comment(cid, text, ctime, *, uid="300", parent="600", mentions=None):
    return {
        "comment_id": cid,
        "thread_id": "600",
        "parent_comment_id": parent,
        "platform_user_id": uid,
        "content": text,
        "ctime": ctime,
        "mentions": mentions or [],
    }


def test_group_keeps_original_target_and_requires_same_verified_addressee():
    first = comment("601", "大肥鱼今天开播吗", 1000)
    second = comment("602", "我等你好久了", 1050)
    different_parent = comment("603", "上面说得对", 1060, parent="601")
    different_mention = comment("604", "另一位也来吗", 1070, mentions=[{"uid": "333", "name": "同名"}])
    other_user = comment("605", "我也等了好久", 1075, uid="301")
    later = comment("606", "晚上好呀", 1800)
    grouped = merge_continuous_comments(
        [second, first, different_parent, different_mention, other_user, later]
    )
    assert grouped == [
        second | {"grouped_comments": [first, second]},
        different_parent,
        different_mention,
        other_user,
        later,
    ]


def test_group_requires_a_known_parent_and_bounded_total_duration():
    first = comment("601", "第一句", 100)
    next_ = comment("602", "第二句", 500)
    later = comment("603", "第三句", 900)
    unknown = comment("604", "未知目标", 950) | {"parent_verified": False}
    assert merge_continuous_comments([first, next_, later, unknown]) == [
        next_ | {"grouped_comments": [first, next_]},
        later,
        unknown,
    ]


@pytest.mark.parametrize("reject", [False, True])
async def test_group_owns_only_covered_sources_and_new_followup_uses_its_real_target(browsing, monkeypatch, reject):
    await enable_comment_choices(browsing, monkeypatch)
    character = browsing.runtime.character
    row, _ = browsing.repo.create_round(
        "100", character, browsing.effective(), browsing.operations.policy()["epoch"], False
    )
    first = comment("601", "实验用了几杯水？", 100)
    second = comment("602", "盐是最后放吗？", 105)
    followup = comment("603", "鸡蛋要先煮熟吗？", 110)
    metadata = {"bvid": BV, "aid": 42}
    calls = []

    async def reply(messages, schema, *, max_tokens):
        data = model_packet(messages[-1]["content"])
        target = data["reply_target"]
        calls.append(target["comment_id"])
        result = {
            "action": "reply_public",
            "text": "问的那一步可以单独看一下。",
            "reason": "specific_detail",
            "explanation": "接着讨论实验步骤",
            "evidence_ids": [target["evidence_ref"]],
        }
        branch = next(
            branch for branch in schema["oneOf"] if branch["properties"]["action"]["enum"] == ["reply_public"]
        )
        if "covered_comments" in branch["properties"]:
            result["covered_comments"] = branch["properties"]["covered_comments"]["items"]["enum"]
        return result

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", reply)

    async def run(items):
        return await browsing.decisions._reply_to_video_comment(
            row,
            None,
            None,
            set(),
            merge_continuous_comments(items)[0],
            items,
            {"model_calls": 0, "lock_wait_seconds": 0},
            metadata=metadata,
        )

    original = await run([first, second])
    assert original["target"]["parent"] == "602" and original["target"]["source_text"] == second["content"]
    first_key = canonical_intent("bilibili", BV, "601", character["id"])
    assert browsing.publication.source_intent(row, first_key)["id"] == original["id"]
    assert await run([first, second]) is None
    if reject:
        browsing.review(original["id"], "reject")
        assert browsing.publication.source_intent(row, first_key) is None
    new = await run([first, second, followup])
    assert new["target"]["parent"] == "603" and new["target"]["source_text"] == followup["content"]
    assert browsing.publication.source_intent(row, first_key)["id"] == (new["id"] if reject else original["id"])
    assert calls == ["602", "603"]
