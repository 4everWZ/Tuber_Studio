"""One-to-one response aliases preserve original evidence and recovery boundaries."""

import copy
import json
import re
from pathlib import Path

import pytest
from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.inputs import (
    ResponseDecisionInput,
    bind_response_actors,
    compact_actor_context,
)
from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, ResponseDecision


def packet(count=2):
    return {
        "kind": "reply",
        "evidence": [
            {
                "id": f"canonical-track:segment:{i}",
                "cid": "real-cid",
                "start": i + 0.123456789,
                "end": i + 0.987654321,
                "text": f"第{i}段原文\n  空白也保留。",
            }
            for i in range(count)
        ],
        "thread": {
            "source": {"author_uid": "200", "text": "对方完整的公开问题。"},
            "items": [{"id": "source-comment", "text": "完整的楼内文字，带数字12345。"}],
        },
        "recent_public_responses": [{"text": "完整的近期回应。"}],
        "coverage": "all_available_cc",
    }


def reply(refs):
    return ResponseDecision(
        action="reply_public",
        reason="specific_detail",
        text="鱼鱼接住这个话题，顺着往下聊。" * 50,
        evidence_ids=refs,
    )


@pytest.mark.parametrize(
    ("mentioned_uid", "parent_uid", "expected"),
    [("3706944382568764", "200", ["self", "other"]), ("300", "300", ["other"]), (None, None, [])],
)
def test_response_actors_keep_real_addressees_distinct_from_oc_and_source(
    mentioned_uid, parent_uid, expected
):
    source = {"comment_id": "32", "author_uid": "200", "author_name": "观众", "text": "@汐 你今天也在吗？"}
    data = packet(1)
    data["thread"] = {
        "source": source,
        "root": {"comment_id": "30", "author_uid": "300", "author_name": "汐", "text": "别人的OC"},
        "parent": {"comment_id": "31", "author_uid": parent_uid, "author_name": "汐", "text": "父评论"},
        "timeline": [{"comment_id": "31", "author_uid": parent_uid, "text": "父评论"}],
        "items": [{"comment_id": "32", "platform_user_id": "200", "content": source["text"]}],
        "addressing": {"source_mentions": [{"uid": mentioned_uid, "name": "汐"}] if mentioned_uid else []},
    }
    original = copy.deepcopy(data)
    actor = {
        "platform": "bilibili", "account_uid": "3706944382568764", "account_display_name": "我方账号",
        "character_id": "whale", "character_name": "大肥鱼",
    }
    bound = bind_response_actors(data, actor)
    assert data == original and bound["thread"]["source"] == source
    assert bound["self_actor"] == actor
    addressing = bound["thread"]["addressing"]
    assert addressing["source_relation_to_self"] == "other"
    assert [item["relation_to_self"] for item in addressing["addressed_to"]] == expected
    if mentioned_uid == parent_uid and mentioned_uid:
        assert addressing["addressed_to"][0]["basis"] == ["mention", "reply_parent"]
    assert bound["thread"]["root"]["relation_to_self"] == "other"
    assert bound["thread"]["items"][0]["relation_to_self"] == "other"
    assert bound["reply_target"]["text"] == source["text"]
    header, body = ResponseDecisionInput(bound).format().split("\n\n所选材料：\n", 1)
    assert list(json.loads(header))[:2] == ["self_actor", "reply_target"]
    assert source["text"] in header and original["evidence"][0]["text"] in body
    assert ResponseDecisionInput(bound).resolve(reply(["ref:1"])).evidence_ids == [original["evidence"][0]["id"]]


def test_missing_account_or_parent_identity_stays_unknown_despite_matching_names():
    data = {"thread": {"source": {"author_uid": "200", "author_name": "大肥鱼", "text": "@大肥鱼"}}}
    value = bind_response_actors(data, {"account_uid": None, "character_name": "大肥鱼"})
    assert value["thread"]["addressing"]["source_relation_to_self"] == "unknown"
    assert value["thread"]["addressing"]["addressed_to"] == []
    assert data == {"thread": {"source": {"author_uid": "200", "author_name": "大肥鱼", "text": "@大肥鱼"}}}


@pytest.mark.parametrize("uploader,expected", [("100", "self"), ("200", "other"), (None, "unknown")])
def test_video_uploader_relation_uses_verified_uid_not_the_oc_name(uploader, expected):
    bound = bind_response_actors({"video_source": {"owner_uid": uploader, "owner": "大肥鱼"}}, {"account_uid": "100"})
    assert bound["video_source"]["uploader_relation_to_self"] == expected


async def test_video_main_reply_uses_video_material_without_unrelated_comment_floors(browsing):
    from test_browsing import model_packet

    row = await prepared_row(browsing)
    source_id = "11:track:0"
    material = {"metadata": {"bvid": "BV1d8pU6xEZX", "aid": 42, "title": "盐水实验", "owner_uid": "200", "owner": "盐水实验室", "parts": [{"cid": "11"}]},
                "coverage": "all_available_cc", "parts": [{"cid": "11", "segments": [{"id": source_id, "text": "加盐后鸡蛋浮起来了。", "start": 0, "end": 3}]}]}
    context = {"source": {"comment_id": "99", "author_uid": "300", "text": "另一楼的私聊口号与未发送台词"},
               "target": {"bvid": "BV1d8pU6xEZX", "oid": "42"}, "items": [{"content": "另一楼的私聊口号与未发送台词"}]}
    captured = []

    async def generate(messages, schema, *, max_tokens):
        captured.append(model_packet(messages[-1]["content"]))
        return ResponseDecision(action="reply_public", reason="specific_detail", explanation="想聊鸡蛋浮起来的细节", text="盐水还能托住鸡蛋，鱼鱼想试试。", evidence_ids=["ref:1"], video_preference="like", preference_reason="这个实验挺有意思").model_dump()

    browsing.runtime.llm.generate_json = generate
    result = await browsing.decisions._response(row, material, context, {source_id}, "top_level", {"lock_wait_seconds": 0}, video_actions=True)
    assert "另一楼的私聊口号" not in json.dumps(captured, ensure_ascii=False)
    assert captured[0]["video_source"]["title"] == "盐水实验"
    assert captured[0]["video_source"]["uploader_relation_to_self"] == "other"
    assert "reply_target" not in captured[0] and "source" not in captured[0]["thread"]
    assert result.evidence_ids == [source_id] and not browsing.adapter.sent


async def test_live_response_packet_binds_current_account_before_reference_formatting(browsing):
    from tuber_studio.bilibili_account import PROFILE_CACHE_KEY, signature

    row = await prepared_row(browsing)
    browsing.store.set(PROFILE_CACHE_KEY, {
        "format": 1, "credential_signature": signature(browsing.comments.credentials),
        "profile": {"uid": "100", "name": "已核验账号名"}, "checked_at": 1,
    })
    data = packet(1)
    data["thread"]["source"]["comment_id"] = "30"
    data["thread"]["addressing"] = {"source_mentions": [{"uid": "300", "name": "汐"}]}
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(copy.deepcopy(messages))
        return reply(["ref:1"]).model_dump()

    browsing.runtime.llm.generate_json = generate
    result = await browsing.decisions._model(
        row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
    )
    header = json.loads(calls[0][-1]["content"].removeprefix("公开来源材料：").split("\n\n所选材料：\n")[0])
    assert {key: header["self_actor"][key] for key in (
        "platform", "account_uid", "account_display_name", "character_id", "character_name",
    )} == {
        "platform": "bilibili", "account_uid": "100", "account_display_name": "已核验账号名",
        "character_id": row["character_id"], "character_name": browsing.runtime.character["name"],
    }
    from tuber_studio.operation_identity import captured_identity

    assert captured_identity(calls[0]) == header["self_actor"] == result._generation_identity
    generation = browsing.runtime.continuations.get(result._generation_id)
    assert generation["binding"]["self_identity"] == result._generation_identity
    assert "_generation_id" not in result.model_dump() and "_generation_identity" not in result.model_dump()
    assert header["self_actor"]["character_version"] == row["character_version"]
    assert header["self_actor"]["account_bio"] is None
    assert header["reply_target"]["addressed_to"][0]["relation_to_self"] == "other"
    assert [message["role"] for message in calls[0]] == ["system", "system", "user"]
    instruction = calls[0][1]["content"]
    assert "接reply_target里的话" in instruction
    assert all(term not in instruction for term in ("提及鉴权", "严禁", "冒认"))
    assert result.evidence_ids == [data["evidence"][0]["id"]]
    assert not browsing.adapter.sent


@pytest.mark.parametrize("case", json.loads(
    (Path(__file__).parent / "fixtures/browsing_reply_participants_20261107.json").read_text(encoding="utf-8")
)["cases"], ids=lambda case: case["case_id"])
def test_real_public_thread_mentions_remain_third_party_in_response_input(case):
    from tuber_studio.operation_continuations import content_hash
    from tuber_studio.operations.browsing.thread_context import comment_thread_context
    from tuber_studio.platforms.bilibili import BilibiliAdapter

    raw = case["platform_response"]
    items = [BilibiliAdapter._comment(raw["root"])] + [
        BilibiliAdapter._comment(item) for item in raw["replies"]
    ]
    source = next(item for item in items if item["comment_id"] == case["expected_source"]["comment_id"])
    context = comment_thread_context(source, items, case["target"])
    data = {"thread": context, "evidence": [], "allowed_evidence_ids": ["public:" + source["comment_id"]]}
    bound = bind_response_actors(data, {
        "platform": "bilibili", "account_uid": case["actor"]["uid"],
        "account_display_name": case["actor"]["name"], "character_id": "whale", "character_name": "大肥鱼",
    })
    assert context["source"] == case["expected_source"]
    assert content_hash(bound["thread"]["source"]) == case["source_hash"]
    mentioned = [entry for entry in bound["reply_target"]["addressed_to"] if "mention" in entry["basis"]]
    assert [entry["uid"] for entry in mentioned] == [entry["uid"] for entry in case["expected_mentions"]]
    assert all(entry["relation_to_self"] == "other" for entry in mentioned)
    wire = ResponseDecisionInput(bound).format()
    assert wire.count(json.dumps(source["content"], ensure_ascii=False)[1:-1]) == 1
    header = json.loads(wire.split("\n\n所选材料：\n")[0])
    assert "items" not in header["thread"] and header["thread"]["source"]["text_ref"] == "reply_target.text"
    assert header["reply_target"]["text"] == source["content"]
    assert bound["thread"] == bind_response_actors(data, bound["self_actor"])["thread"]


def test_compact_thread_preserves_each_other_comment_once_including_raw_rows_beyond_timeline():
    source = {"comment_id": "2", "author_uid": "200", "text": "当前评论的原文"}
    root = {"comment_id": "1", "author_uid": "300", "text": "根楼的完整原文"}
    rows = [root, source] + [
        {"comment_id": str(index), "platform_user_id": "400", "content": f"后续原文第{index}条"}
        for index in range(3, 14)
    ]
    bound = bind_response_actors({
        "thread": {"source": source, "root": root, "parent": root,
                   "timeline": rows[:8], "items": rows, "public_text": source["text"]},
    }, {"account_uid": "100"})
    original = copy.deepcopy(bound)
    compact = compact_actor_context(bound, "r1")
    assert bound == original and compact["reply_target"]["evidence_ref"] == "r1"
    assert compact["thread"]["root"]["text_ref"] == compact["thread"]["parent"]["text_ref"]
    assert compact["thread"]["public_text_ref"] == "reply_target.text"
    assert "items" not in compact["thread"] and "public_text" not in compact["thread"]
    serialized = json.dumps(compact, ensure_ascii=False)
    for row in rows:
        assert serialized.count(row.get("text") or row["content"]) == 1
    assert compact_actor_context(compact, "r1") == compact
    external = compact_actor_context(bound, current_text_ref="当前观众消息.text")
    assert "text" not in external["reply_target"]
    assert external["reply_target"]["text_ref"] == external["thread"]["source"]["text_ref"] == "当前观众消息.text"
    assert source["text"] not in json.dumps(external, ensure_ascii=False)


def test_compact_public_experience_references_only_exact_same_source_quote():
    source = {"comment_id": "2", "author_uid": "200", "text": "观众正在吐槽我刚才接错话"}
    reference = {"bvid": "BV1RLH367EQN", "thread_id": "1", "comment_id": "2", "quote": source["text"]}
    bound = bind_response_actors({
        "thread": {"source": source, "root": source, "items": [source]},
        "own_public_experiences": [{"public_sources": [reference]}],
    }, {"account_uid": "100"})
    original = copy.deepcopy(bound)
    compact = compact_actor_context(bound)
    citation = compact["own_public_experiences"][0]["public_sources"][0]
    assert citation == {key: value for key, value in reference.items() if key != "quote"} | {
        "quote_ref": "reply_target.text"
    }
    assert json.dumps(compact, ensure_ascii=False).count(source["text"]) == 1
    assert bound == original
    external = compact_actor_context(bound, current_text_ref="当前观众消息.text")
    assert external["own_public_experiences"][0]["public_sources"][0]["quote_ref"] == "当前观众消息.text"
    for changed in (reference | {"comment_id": "3"}, reference | {"quote": "同一楼另一段话"}):
        unrelated = copy.deepcopy(bound)
        unrelated["own_public_experiences"][0]["public_sources"] = [changed]
        assert compact_actor_context(unrelated)["own_public_experiences"][0]["public_sources"] == [changed]


async def test_video_comment_can_cite_original_comment_alongside_video_and_publish_locally(browsing, monkeypatch):
    from test_browsing import BV

    row = await prepared_row(browsing)
    key, material = await browsing._prepare_evidence(BV, row["config"])
    source = {
        "comment_id": "501", "thread_id": "501", "parent_comment_id": None,
        "platform_user_id": "200", "display_name": "观众", "content": "@汐 可以看看这条鱼的视频",
        "mentions": [{"uid": "300", "name": "汐"}],
    }
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(copy.deepcopy(messages))
        content = messages[-1]["content"]
        alias = re.search(r"\[(ref:\d+)\] 当前评论原文", content)[1]
        assert content.index("当前评论原文") < content.index("加盐之后")
        return {
            "action": "reply_public", "reason": "specific_detail", "explanation": "想顺着观众安利视频的话聊一句",
            "text": "有人给朋友安利视频呢，我也来凑个热闹，这条鱼的出场方式还挺有意思。",
            "evidence_ids": [alias],
        }

    async def read_reply(target):
        assert target["parent"] == source["comment_id"]
        return {"author_uid": "200", "text": source["content"]}

    browsing.runtime.llm.generate_json = generate
    monkeypatch.setattr(browsing.adapter, "read_video_reply", read_reply, raising=False)
    result = await browsing.decisions._reply_to_video_comment(
        row, material, key, {segment["id"] for segment in browsing._segments(material)},
        source, [source], {"lock_wait_seconds": 0},
    )
    assert len(calls) == 1 and result["evidence"] == ["public:501"]
    assert result["material_keys"] == [key] and not browsing.adapter.sent
    browsing.review(result["id"], "approve")
    sent = await browsing.publish(result["id"])
    assert sent["status"] == "published" and sent["evidence"] == ["public:501"]
    assert browsing.adapter.sent == [(result["target"], result["text"])]


async def test_response_recalls_own_public_incident_from_this_floor_without_keyword_trigger(browsing):
    from test_browsing import BV

    row = await prepared_row(browsing)
    char = browsing.runtime.character
    memory = browsing.runtime.daily_memory
    for root, incident in (("501", "this-floor"), ("601", "other-floor")):
        memory.record_public_incident(char["id"], char["version"],
            incident_key=incident, highlight_date="2026-11-07", headline="认错了说话对象",
            anecdote="我把别人喊朋友的评论误接成在叫自己，后来核对了公开原楼。",
            public_sources=[{"bvid": BV, "thread_id": root, "comment_id": root, "quote": "公开原话"}],
            tags=["大肥鱼"])
    data = {"kind": "reply_public", "evidence": [], "allowed_evidence_ids": ["public:502"],
            "thread": {"source": {"comment_id": "502", "author_uid": "200", "text": "哈哈"},
                       "target": {"bvid": BV, "root": "501", "parent": "502"}}}
    _, messages, _ = browsing.decisions._model_input(row, ResponseDecision, data, RESPONSE_INSTRUCTION)
    header = json.loads(messages[-1]["content"].removeprefix("公开来源材料：").split("\n\n所选材料：\n")[0])
    assert [item["incident_key"] for item in header["own_public_experiences"]] == ["this-floor"]
    assert header["own_public_experiences"][0]["fictional"] is False


def test_response_presentation_keeps_all_text_thread_recent_and_exact_times():
    data = packet()
    original = copy.deepcopy(data)
    view = ResponseDecisionInput(data)
    header, body = view.format().split("\n\n所选材料：\n", 1)
    assert json.loads(header) == {
        k: v for k, v in original.items() if k not in {"evidence", "allowed_evidence_ids"}
    }
    for segment in original["evidence"]:
        assert segment["text"] in body
        assert segment["id"] not in body and segment["cid"] not in body
        assert str(segment["start"]) in body and str(segment["end"]) in body
    assert body.count("材料1") == 1 and len(view.references) == 2
    assert view.references[0].source() == original["evidence"][0]
    fresh = view.references[0].source()
    fresh["text"] = "mutated copy"
    data["evidence"][0]["id"] = "mutated original"
    data["thread"]["source"]["text"] = "mutated thread"
    assert view.references[0].source() == original["evidence"][0]
    assert json.loads(view.format().split("\n\n所选材料：\n")[0])["thread"] == original["thread"]
    bound = view.resolve(reply(["ref:1", "ref:2"]))
    assert bound.evidence_ids == [s["id"] for s in original["evidence"]]
    assert bound.text == reply(["ref:1"]).text


def test_one_to_one_29_segments_still_allow_only_eight_original_references():
    data = packet(29)
    view = ResponseDecisionInput(data)
    assert len(view.references) == 29
    result = view.resolve(reply([f"ref:{i}" for i in range(1, 9)]))
    assert len(result.evidence_ids) == 8 and result.evidence_ids == [s["id"] for s in data["evidence"][:8]]
    invalid = reply(["ref:1"]).model_copy(update={"evidence_ids": [f"ref:{i}" for i in range(1, 10)]})
    with pytest.raises(ValueError):
        view.resolve(invalid)
    with pytest.raises(ValueError, match="短编号"):
        view.resolve(reply(["ref:30"]))


@pytest.mark.parametrize("canonical_id,wire_ref", [("r1", "ref:1"), ("ref:1", "ref:2")])
def test_aliases_do_not_collide_with_original_ids_and_are_invocation_local(canonical_id, wire_ref):
    first = packet()
    first["evidence"][0]["id"] = canonical_id
    view = ResponseDecisionInput(first)
    assert view.references[0].alias == wire_ref
    for bad in [canonical_id, first["evidence"][1]["id"], "unknown"]:
        with pytest.raises(ValueError, match="短编号"):
            view.resolve(reply([bad]))
    another = ResponseDecisionInput(packet())
    assert another.resolve(reply(["ref:1"])).evidence_ids == ["canonical-track:segment:0"]
    assert view.resolve(reply([wire_ref])).evidence_ids == [canonical_id]


def test_public_text_reference_is_bound_without_inventing_video_segments():
    data = {
        "kind": "reply",
        "evidence": [],
        "thread": {"source": {"text": "完整文字动态", "author_uid": "200"}},
        "allowed_evidence_ids": ["public:123"],
        "recent_public_responses": [],
        "coverage": "public_text",
    }
    view = ResponseDecisionInput(data)
    assert "[ref:1] 当前公开来源" in view.format()
    assert "public:123" not in view.format()
    assert view.references[0].source() is None
    assert view.resolve(reply(["ref:1"])).evidence_ids == ["public:123"]
    skip = ResponseDecision(action="skip", reason="no_interest", evidence_ids=[])
    assert view.resolve(skip) == skip
    with pytest.raises(ValueError, match="为空"):
        view.resolve(reply([]))


def test_current_comment_text_is_readable_at_its_reference_without_losing_thread_context():
    source = {"comment_id": "322901117856", "author_uid": "200", "text": "钱给够能肘赢360吗？\n  原来的空白也保留。"}
    data = {"kind": "reply_public", "evidence": [], "thread": {"source": source, "root": source},
            "allowed_evidence_ids": ["public:" + source["comment_id"]], "coverage": "public_text"}
    before = copy.deepcopy(data)
    view = ResponseDecisionInput(data)
    header, body = view.format().split("\n\n所选材料：\n", 1)
    assert body == "[ref:1] 当前评论原文：\n" + source["text"]
    assert json.loads(header)["thread"] == before["thread"]
    assert data == before and view.references[0].source() is None
    assert view.resolve(reply(["ref:1"])).evidence_ids == ["public:" + source["comment_id"]]


@pytest.mark.parametrize("change", [{"start": -1}, {"end": False}, {"start": 9, "end": 1}, {"text": None}])
def test_invalid_source_records_are_rejected_before_model_generation(change):
    data = packet()
    data["evidence"][0].update(change)
    with pytest.raises(ValueError):
        ResponseDecisionInput(data)


async def prepared_row(browsing):
    await browsing.preflight(for_automation=False)
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    return row


async def test_canonical_id_in_wire_is_invalid_without_an_automatic_repair_call(browsing):
    from tuber_studio.operation_generation import GenerationInvalid

    browsing.store.set("browsing_policy", browsing.policy() | {"max_output_tokens": 32768})
    row = await prepared_row(browsing)
    data = packet(1)
    calls = []
    bad = reply([data["evidence"][0]["id"]]).model_dump()

    async def generate(messages, schema, *, max_tokens):
        calls.append((schema["title"], copy.deepcopy(messages), copy.deepcopy(schema)))
        return bad

    browsing.runtime.llm.generate_json = generate
    with pytest.raises(GenerationInvalid):
        await browsing.decisions._model(
            row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
        )
    assert [title for title, _, _ in calls] == ["ResponseDecision"]
    for title, messages, schema in calls:
        if title == "ResponseDecision":
            assert (
                "[ref:1|" in messages[2]["content"] and data["evidence"][0]["id"] not in messages[2]["content"]
            )
            assert schema["$defs"]["evidence_id"]["enum"] == ["ref:1"]
            assert all(b["properties"]["evidence_ids"]["items"] == {"$ref": "#/$defs/evidence_id"}
                       for b in schema["oneOf"])
    saved = browsing.store.one("SELECT id FROM operation_continuations WHERE channel='generation.browsing'")
    episode = browsing.runtime.continuations.get(saved["id"])
    assert episode["binding"]["input_format"] == ResponseDecisionInput.FORMAT_VERSION
    assert episode["state"] == "invalid" and episode["original_output"] == bad
    assert any("回应引用不属于本次短编号" in str(step["feedback"]) for step in episode["revisions"])
    assert not browsing.adapter.sent and not browsing.intents()


async def test_format_version_change_does_not_reuse_previous_wire_checkpoint(browsing, monkeypatch):
    row = await prepared_row(browsing)
    data = packet(1)
    calls = []

    async def generate(messages, schema, *, max_tokens):
        calls.append(schema["title"])
        return reply(["ref:1"]).model_dump()

    browsing.runtime.llm.generate_json = generate
    first = await browsing.decisions._model(
        row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
    )
    first_capacity = browsing.decisions._capacity_signature(row["config"])
    first_format = ResponseDecisionInput.FORMAT_VERSION
    monkeypatch.setattr(ResponseDecisionInput, "FORMAT_VERSION", "test-response-v2")
    assert browsing.decisions._capacity_signature(row["config"]) != first_capacity
    second = await browsing.decisions._model(
        row, ResponseDecision, data, RESPONSE_INSTRUCTION, {"lock_wait_seconds": 0}
    )
    assert calls == ["ResponseDecision", "ResponseDecision"] and first.model_dump() == second.model_dump()
    assert first._generation_id != second._generation_id
    episodes = [
        browsing.runtime.continuations.get(r["id"])
        for r in browsing.store.all(
            "SELECT id FROM operation_continuations WHERE channel='generation.browsing'"
        )
    ]
    assert {r["binding"]["input_format"] for r in episodes} == {
        first_format,
        "test-response-v2",
    }
    assert all(r["current_output"]["evidence_ids"] == ["ref:1"] for r in episodes)
    assert not browsing.adapter.sent
