"""Native choices remove impossible contacts without deciding invitation semantics."""

import copy
import json

import pytest
from test_browsing import BV, finished
from test_browsing import browsing as browsing
from test_browsing_comment_only import attach_comment_source, enable_comment_choices, forbid_video_content
from test_response_inputs import prepared_row

from tuber_studio.message_outreach import (
    OutreachAssessment,
    assess_outreach,
    invitation_anchor,
    invitation_possibility,
)
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


async def test_first_contact_check_sees_the_earlier_invitation_beyond_short_timeline(browsing, monkeypatch):
    value = thread()
    value["addressing"]["source_mentions"] = [{"uid": "999", "name": "别人"}]
    value["items"] = [{"comment_id": "599", "platform_user_id": "300", "content": "这件事查到了私信告诉我",
                       "parent_comment_id": None, "mentions": [{"uid": "100", "name": "我方"}]}]
    seen = []

    async def check(packet, schema, *, max_tokens):
        source = json.loads(packet[-1]["content"])["public_thread"]
        seen.append(source)
        assert any(item["comment_id"] == "599" and item["text"] == "这件事查到了私信告诉我" for item in source["items"])
        return {"action": "wait", "reason": "此前有真实约定，尚待完成", "purpose": "查明此事后联系",
                "invitation_kind": "agreement", "invitation_comment_id": "599", "invitation_quote": "查到了私信告诉我",
                "invited_actor_uid": "100", "body_addresses_purpose": False}

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", check)
    character = browsing.runtime.character
    draft = {"account_id": "100", "platform_user_id": "300", "character_id": character["id"],
             "character_version": character["version"], "purpose": "查明此事后联系", "text": "当前候选"}
    assessment, disposition = await assess_outreach(browsing.runtime, draft, {"public_source_thread": value}, [], [])
    assert len(seen) == 1 and disposition == "wait" and assessment.invitation_comment_id == "599"


async def test_native_mask_keeps_public_choices_and_unresolved_but_removes_only_impossible_handoff(browsing):
    row = await prepared_row(browsing)
    row["config"]["comment_handoff_enabled"] = True
    for owner, expected in (("200", False), ("", True), ("100", True)):
        value = thread() | {"resource_author_uid": owner}
        data = {"kind": "reply_public", "thread": value, "evidence": [], "allowed_evidence_ids": ["public:601"]}
        _, _, schema = browsing.decisions._model_input(row, ResponseDecision, data, RESPONSE_INSTRUCTION)
        selected = actions(schema)
        assert ("handoff_private" in selected) is expected
        assert {"reply_public", "skip", "wait"} <= selected
    # Possibility does not certify the semantic invitation itself.
    assessment = OutreachAssessment(action="send", reason="普通聊天", purpose="闲聊", invitation_kind="none",
        invitation_comment_id="601", invitation_quote="当前原话", invited_actor_uid="100", body_addresses_purpose=True)
    assert invitation_anchor(assessment, {"account_id": "100", "platform_user_id": "300"}, thread() | {"resource_author_uid": "100"}) is None


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
