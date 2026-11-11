"""Verified invitations use the private worker; ordinary discussion stays public."""

import copy

import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_comment_only import attach_comment_source, enable_comment_choices, forbid_video_content
from test_direct_messages import Delivery
from test_response_inputs import prepared_row

from tuber_studio.message_outreach import (
    OutreachAssessment,
    assess_outreach,
    invitation_anchor,
    invitation_possibility,
)
from tuber_studio.operations.browsing.comment_reader import handoff_comment
from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, ResponseDecision


def thread():
    return {"source": {"comment_id": "601", "author_uid": "300", "author_name": "邻居", "text": "当前原话"},
        "target": {"bvid": BV, "root": "601", "parent": "601", "recipient": "300"},
        "root": {"comment_id": "601", "author_uid": "300", "text": "当前原话", "parent_comment_id": None},
        "parent": None, "timeline": [], "items": [], "resource_author_uid": "200",
        "addressing": {"source_mentions": [], "source_parent_comment_id": None}}


def actions(schema):
    return {branch["properties"]["action"]["enum"][0] for branch in schema["oneOf"]}


@pytest.mark.parametrize("change,expected", [
    ({}, "impossible"),
    ({"resource_author_uid": "100"}, "possible"),
    ({"resource_author_uid": ""}, "unresolved"),
    ({"addressing": {"source_mentions": [{"uid": "100", "name": "同名OC"}], "source_parent_comment_id": None}}, "possible"),
    ({"addressing": {"source_mentions": [{"uid": "999", "name": "同名OC"}], "source_parent_comment_id": None}}, "impossible"),
    ({"addressing": {"source_mentions": [{"name": "同名OC"}], "source_parent_comment_id": None}}, "unresolved"),
    ({"addressing": {"source_mentions": [], "source_parent_comment_id": "600"}, "parent": {"comment_id": "600", "author_uid": None, "text": "", "available": False}}, "unresolved"),
    ({"addressing": {"source_mentions": [], "source_parent_comment_id": "600"}, "parent": {"comment_id": "600", "author_uid": "100", "text": "我方真实回复", "parent_comment_id": None}}, "possible"),
])
def test_identity_possibility_has_no_nickname_or_invitation_keyword_shortcut(change, expected):
    value = thread() | change
    original = copy.deepcopy(value)
    assert invitation_possibility(value, "100", "300") == expected
    assert value == original


def test_missing_mention_metadata_is_unresolved_and_richer_duplicate_can_supply_real_uid():
    value = thread()
    value["addressing"].pop("source_mentions")
    assert invitation_possibility(value, "100", "300") == "unresolved"
    value["root"]["mentions"] = [{"uid": "100", "name": "我方"}]
    assert invitation_possibility(value, "100", "300") == "possible"


@pytest.mark.parametrize("raw_items", [False, True])
def test_earlier_recipient_invitation_survives_current_other_mention_and_timeline_prefix(raw_items):
    value = thread()
    value["addressing"]["source_mentions"] = [{"uid": "999", "name": "别人"}]
    invitation = {"comment_id": "599", "parent_comment_id": None, "mentions": [{"uid": "100", "name": "我方"}]}
    if raw_items:
        invitation.update(platform_user_id="300", content="这件事查到了私信告诉我")
        value["timeline"] = [{"comment_id": str(index), "author_uid": "999", "text": "其他讨论"} for index in range(8)]
        value["items"] = [*value["timeline"], invitation]
    else:
        invitation.update(author_uid="300", text="这件事查到了私信告诉我")
        value["timeline"] = [invitation]
    original = copy.deepcopy(value["source"])
    assert invitation_possibility(value, "100", "300") == "possible"
    assessment = OutreachAssessment(action="wait", reason="真实约定待完成", purpose="查明此事后联系",
        invitation_kind="agreement", invitation_comment_id="599", invitation_quote="查到了私信告诉我",
        invited_actor_uid="100", body_addresses_purpose=False)
    assert invitation_anchor(assessment, {"account_id": "100", "platform_user_id": "300"}, value)
    assert value["source"] == original


async def test_first_contact_check_does_not_authorize_from_arbitrary_history(browsing, monkeypatch):
    value = thread()
    value["addressing"]["source_mentions"] = [{"uid": "999", "name": "别人"}]
    value["items"] = [{"comment_id": "599", "platform_user_id": "300", "content": "这件事查到了私信告诉我",
                       "parent_comment_id": None, "mentions": [{"uid": "100", "name": "我方"}]}]
    async def check(packet, schema, *, max_tokens):
        pytest.fail("A verified invitation must not require another channel-selection model call")

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", check)
    character = browsing.runtime.character
    draft = {"account_id": "100", "platform_user_id": "300", "character_id": character["id"],
             "character_version": character["version"], "purpose": "查明此事后联系", "text": "当前候选"}
    assessment, disposition = await assess_outreach(browsing.runtime, draft, {"public_source_thread": value}, [], [])
    assert disposition == "stay_public" and assessment.invitation_comment_id == ""
    assert assessment.invitation_kind == "none" and not assessment.promise_fulfilled


async def test_native_mask_keeps_public_choices_and_requires_actual_invitation(browsing):
    row = await prepared_row(browsing)
    row["config"]["comment_handoff_enabled"] = True
    for owner in ("200", "", "100"):
        value = thread() | {"resource_author_uid": owner}
        data = {"kind": "reply_public", "thread": value, "evidence": [], "allowed_evidence_ids": ["public:601"]}
        _, _, schema = browsing.decisions._model_input(row, ResponseDecision, data, RESPONSE_INSTRUCTION)
        selected = actions(schema)
        assert "handoff_private" not in selected
        assert {"reply_public", "skip", "wait"} <= selected
    value = thread()
    value["source"]["text"] = "这个实验可以私信我继续聊吗？"
    value["addressing"]["source_mentions"] = [{"uid": "100", "name": "我方"}]
    data["thread"] = value
    _, _, schema = browsing.decisions._model_input(row, ResponseDecision, data, RESPONSE_INSTRUCTION)
    assert "handoff_private" in actions(schema)
    # An identity relation alone does not certify an invitation.
    assessment = OutreachAssessment(action="send", reason="普通聊天", purpose="闲聊", invitation_kind="none",
        invitation_comment_id="601", invitation_quote="当前原话", invited_actor_uid="100", body_addresses_purpose=True)
    assert invitation_anchor(assessment, {"account_id": "100", "platform_user_id": "300"}, thread() | {"resource_author_uid": "100"}) is None


@pytest.mark.parametrize("text,mentions,owner", [
    ("盐水比例大概是多少？", [{"uid": "100"}], "100"),
    ("可以私信我继续聊这个实验吗？", [{"uid": "999"}], "100"),
    ("可以私信我继续聊这个实验吗？", [{"name": "大肥鱼"}], ""),
    ("不用私信我，就在这里说实验吧。", [{"uid": "100"}], "100"),
    ("你说的‘私信我’是什么意思？", [{"uid": "100"}], "100"),
])
async def test_response_without_verified_invitation_keeps_public_model_choice(browsing, monkeypatch, text, mentions, owner):
    row = await prepared_row(browsing)
    row["config"]["comment_handoff_enabled"] = True
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    context = thread() | {"resource_author_uid": owner}
    context["source"]["text"] = text
    context["addressing"]["source_mentions"] = mentions
    calls = []

    async def skip(row, schema, data, instruction, metrics, **options):
        calls.append(data)
        assert not browsing.decisions._handoff_allowed(row, data)
        return ResponseDecision(action="skip", reason="no_interest", explanation="暂时没有要补充的内容")

    monkeypatch.setattr(browsing.decisions, "_model", skip)
    reply = await browsing.decisions._response(row, None, context, ["public:601"], "reply_public", {})
    assert reply.action == "skip" and len(calls) == 1
    assert not browsing.store.all("SELECT * FROM dm_drafts")


@pytest.mark.parametrize("location", ["root", "parent", "timeline", "items"])
async def test_response_history_can_resolve_identities_but_not_create_a_new_invitation(browsing, monkeypatch, location):
    row = await prepared_row(browsing)
    row["config"]["comment_handoff_enabled"] = True
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    context = thread()
    context["source"]["text"] = "盐水实验还有个问题，就在楼里聊吧。"
    context["addressing"]["source_mentions"] = [{"uid": "100"}]
    invitation = {"comment_id": "599", "author_uid": "300", "text": "这个实验私信我继续聊吧",
                  "parent_comment_id": None, "mentions": [{"uid": "100"}]}
    context[location] = [invitation] if location in {"timeline", "items"} else invitation

    async def public_choice(row, schema, data, instruction, metrics, **options):
        assert not browsing.decisions._handoff_allowed(row, data)
        return ResponseDecision(action="reply_public", reason="light_interaction", text="就在这里聊盐水比例吧。")

    monkeypatch.setattr(browsing.decisions, "_model", public_choice)
    reply = await browsing.decisions._response(row, None, context, ["public:601"], "reply_public", {})
    assert reply.action == "reply_public"
    assert not browsing.store.all("SELECT * FROM dm_drafts")


@pytest.mark.parametrize("withdrawn", [False, True])
async def test_response_unhandled_group_invitation_respects_current_withdrawal(browsing, monkeypatch, withdrawn):
    row = await prepared_row(browsing)
    row["config"]["comment_handoff_enabled"] = True
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    context = thread()
    context["source"]["text"] = "不用私信我，就在这里聊吧。" if withdrawn else "我再补充一下，是刚才那个盐水实验。"
    context["addressing"]["source_mentions"] = [{"uid": "100"}]
    invitation = {"comment_id": "599", "platform_user_id": "300", "content": "这个实验私信我继续聊吧",
                  "parent_comment_id": None, "mentions": [{"uid": "100"}]}
    context["items"] = [invitation]
    context["grouped_comments"] = [invitation, {
        "comment_id": "601", "platform_user_id": "300", "content": context["source"]["text"],
        "parent_comment_id": None, "mentions": [{"uid": "100"}],
    }]
    calls = []

    async def public_choice(row, schema, data, instruction, metrics, **options):
        calls.append(data)
        assert withdrawn and not browsing.decisions._handoff_allowed(row, data)
        return ResponseDecision(action="reply_public", reason="light_interaction", text="好，就在这里接着聊。")

    monkeypatch.setattr(browsing.decisions, "_model", public_choice)
    reply = await browsing.decisions._response(row, None, context, ["public:601"], "reply_public", {})
    assert reply.action == ("reply_public" if withdrawn else "handoff_private")
    assert len(calls) == int(withdrawn)
    if not withdrawn:
        assert reply.text == "" and reply.contact_trigger == "invitation"


@pytest.mark.parametrize("automatic", [False, True])
async def test_verified_response_bypasses_skip_model_and_hands_off_once(browsing, monkeypatch, automatic):
    await enable_comment_choices(browsing, monkeypatch, automatic=automatic)
    await browsing.configure({"comment_handoff_enabled": True})
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True, "auto_send": automatic})
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.effective(), browsing.operations.policy()["epoch"], False
    )
    browsing.store.execute("UPDATE browsing_rounds SET status='running' WHERE id=?", (row["id"],))
    context = thread()
    context["source"]["text"] = "这个盐水实验可以私信我继续聊吗？"
    context["addressing"]["source_mentions"] = [{"uid": "100", "name": "我方"}]
    target = context["target"] | {"type": 1, "oid": "42"}
    context["target"] = target
    delivery = Delivery()

    async def sessions():
        return "100", []

    async def self_uid():
        return "100"

    async def read_reply(target):
        return {"author_uid": "300", "text": context["source"]["text"]}

    async def no_channel_model(*args, **kwargs):
        pytest.fail("A verified invitation must bypass the model's public/skip/channel decision")

    monkeypatch.setattr(delivery, "sessions", sessions)
    monkeypatch.setattr(delivery, "self_uid", self_uid, raising=False)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", read_reply, raising=False)
    monkeypatch.setattr(browsing.messages, "adapter", delivery)
    monkeypatch.setattr(browsing.messages, "injected", False)
    monkeypatch.setattr(browsing.messages, "credential_signature", ("test", "test"))
    monkeypatch.setattr(browsing.decisions, "_model", no_channel_model)
    reply = await browsing.decisions._response(row, None, context, ["public:601"], "reply_public", {})
    assert reply.action == "handoff_private" and reply.text == ""
    assert reply.contact_trigger == "invitation" and "盐水实验" in reply.contact_purpose
    intent = await handoff_comment(browsing, row, reply, target, context, None, [])
    draft, = browsing.store.all("SELECT * FROM dm_drafts")
    assert draft["account_id"] == "100" and draft["platform_user_id"] == "300"
    assert draft["text"] and not draft["proposed_text"]
    assert draft["status"] == ("published" if automatic else "pending_review"), draft
    assert intent["kind"] == "private_handoff" and intent["text"] == ""
    assert intent["target"]["private_draft_id"] == draft["id"]
    if not automatic:
        browsing.messages.review(draft["id"], "approve")
        assert (await browsing.messages.send(draft["id"]))["status"] == "published"
    assert delivery.sent == [("300", draft["text"])]
    assert not browsing.adapter.sent
    again = await handoff_comment(browsing, row, reply, target, context, None, [])
    assert again["target"]["private_draft_id"] == draft["id"]
    assert len(browsing.store.all("SELECT * FROM dm_drafts")) == 1 and len(delivery.sent) == 1
    public_scope = browsing.comments.account_context_scope(BV, "601", account_id="100")
    public = browsing.runtime.context.existing(public_scope, browsing.runtime.character)
    assert not public or draft["text"] not in public["history"]


async def test_comment_only_metadata_owner_is_reused_without_reading_video_material(browsing, monkeypatch):
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    captured = []
    original = browsing.decisions._response

    async def response(row, material, context, *args, **kwargs):
        captured.append(context["resource_author_uid"])
        return await original(row, material, context, *args, **kwargs)

    monkeypatch.setattr(browsing.decisions, "_response", response)
    main = await finished(browsing, automatic=True, video_only=True)
    assert main["status"] == "completed" and captured == []
    result = await finished(browsing, automatic=True, interaction_only=True)
    assert result["status"] == "completed" and captured == ["200"]
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")


async def test_schema_ignoring_fake_handoff_is_rejected_before_creating_private_draft(browsing, monkeypatch):
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    await enable_comment_choices(browsing, monkeypatch)
    attach_comment_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    browsing.runtime.llm.response_action = "handoff_private"
    original = browsing.runtime.llm.generate_json
    schemas = []

    async def generic(packet, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            schemas.append(schema)
        if schema["title"] == "ContinuationDecision":
            return {"action": "abandon", "reason": "该来源不存在我方受邀关系", "read": ""}
        return await original(packet, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", generic)
    main = await finished(browsing, automatic=True, video_only=True)
    assert main["status"] == "completed" and schemas == []
    await finished(browsing, automatic=True, interaction_only=True)
    assert schemas and all("handoff_private" not in actions(schema) for schema in schemas)
    assert not browsing.store.all("SELECT * FROM dm_drafts")
    assert not browsing.store.one("SELECT id FROM browsing_intents WHERE kind='private_handoff'")
    assert not browsing.adapter.sent
    continuation = browsing.store.one("SELECT feedback FROM operation_continuations WHERE channel='generation.browsing' AND json_extract(binding,'$.schema')='ResponseDecision'")
    assert continuation and "私信渠道与来源" in continuation["feedback"]
