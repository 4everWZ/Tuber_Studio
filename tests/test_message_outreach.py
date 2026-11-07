"""Invited first contacts and ordinary incoming replies use different paths."""

import json
from types import SimpleNamespace

import pytest
from test_direct_messages import incoming
from test_direct_messages import messages as messages

from tuber_studio.direct_messages import MessageInputChanged
from tuber_studio.models import OperationReply
from tuber_studio.operation_generation import generate_with_recovery
from tuber_studio.operation_identity import identity_snapshot

ACTOR = "injected-test-account"


async def setup_delivery(messages, monkeypatch):
    messages.operations.configure({"enabled": True}, account_id=ACTOR, character=messages.runtime.character)
    await messages.configure({"enabled": True, "auto_send": True, "max_per_hour": 0, "max_per_day": 0})
    await messages.resume_pending()
    generated, checked = [], []

    async def generate(scope, character, event, relationship=None, *, mode):
        generated.append(event)
        return OperationReply(spoken_text="你问的那项配置在设置页，可以接着私下聊。", reply_to=[event["event_id"]]), {
            "generation_identity": messages.runtime.identity_snapshot(character, event["account_id"]),
            "output": {"needs_review": False},
        }

    async def assess(packet, schema, *, max_tokens):
        assert schema["title"] == "OutreachAssessment" and max_tokens == 1024
        data = json.loads(packet[-1]["content"])
        checked.append(data)
        source = data["public_thread"]["source"]
        return {"action": "send", "reason": "对方邀请当前账号私聊这项具体配置", "purpose": "讨论你问的配置位置",
            "invitation_kind": "agreement" if data["completion_evidence"] else "invitation",
            "invitation_comment_id": source["comment_id"], "invitation_quote": source["text"],
            "invited_actor_uid": ACTOR, "promise_fulfilled": bool(data["completion_evidence"]), "new_matter": False, "body_addresses_purpose": True}

    monkeypatch.setattr(messages.runtime, "generate_reply", generate)
    monkeypatch.setattr(messages.runtime.llm, "generate_json", assess, raising=False)
    return generated, checked


def invite(messages, *, cid="701", root="700", mentioned=ACTOR, trigger="invitation", proposal=True):
    text = "请把我问的那项配置私信告诉我" if trigger != "agreement" else "查到了私信告诉我"
    messages.comments.import_items("BV1d8pU6xEZX", [{"comment_id": cid, "thread_id": root,
        "parent_comment_id": root, "platform_user_id": "123", "display_name": "鱼宝", "content": text}])
    source = messages.store.one("SELECT * FROM comments WHERE comment_id=?", (cid,))
    thread = {"source": {"comment_id": cid, "author_uid": "123", "author_name": "鱼宝", "text": text},
        "target": {"type": 1, "oid": "42", "bvid": source["video_id"], "root": root, "parent": cid},
        "root": {"comment_id": root, "author_uid": ACTOR, "text": "我方此前的公开讨论"},
        "parent": {"comment_id": root, "author_uid": ACTOR, "text": "我方此前的公开讨论"},
        "addressing": {"source_mentions": [{"uid": mentioned, "name": "同名大肥鱼"}], "source_parent_comment_id": root}}
    identity = messages.enqueue("comment", source["id"], automatic=True, account_id=ACTOR,
        purpose="讨论你问的配置位置", contact_trigger=trigger, schedule=False,
        proposed_text="配置在设置页，我来接着说。" if proposal else "",
        proposal_identity=messages.runtime.identity_snapshot(messages.runtime.character, ACTOR),
        video_evidence={"material_keys": [], "source_versions": [], "public_source_thread": thread})
    return identity


async def test_valid_invitation_automatically_sends_once_and_reuses_proposal(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "published" and row["contact_key"]
    assert len(checks) == 1 and generated == []
    assert messages.adapter.sent == [("123", "配置在设置页，我来接着说。")]
    assert messages._draft_view(row)["type"] == "outreach"
    with pytest.raises(ValueError):
        await messages.send(identity)
    assert len(checks) == 1 and len(messages.adapter.sent) == 1


async def test_invitation_to_another_uid_cannot_be_claimed_by_model_reason(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages, mentioned="another-oc-account")
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert len(checks) == 1 and row["status"] == "not_initiated" and row["error"] is None
    assert not messages.adapter.sent


async def test_first_check_reviews_actual_image_instead_of_unsent_candidate_text(messages, monkeypatch):
    await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    messages.memes = SimpleNamespace(resolve=lambda *args: {
        "asset_id": "image-1", "title": "一张表情", "description": "只是一张招手图", "sha256": "fixture-image", "emotions": ["cheerful"],
    })
    messages.store.execute("UPDATE dm_drafts SET message_kind='image',meme_id='image-1' WHERE id=?", (identity,))
    original = messages.runtime.llm.generate_json

    async def check(packet, schema, *, max_tokens):
        data = json.loads(packet[-1]["content"])
        assert data["outgoing_text"] is None
        assert data["outgoing_image"]["asset_id"] == "image-1"
        value = await original(packet, schema, max_tokens=max_tokens)
        value["body_addresses_purpose"] = False
        return value

    monkeypatch.setattr(messages.runtime.llm, "generate_json", check)
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "waiting"
    assert messages._draft_view(row)["contact_state"] == "awaiting_content"
    assert not messages.adapter.sent


async def test_no_new_invitation_and_long_followup_is_not_a_contact_basis(messages, monkeypatch):
    await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    messages.store.execute("UPDATE comments SET content='这段真有意思' WHERE id=(SELECT source_id FROM dm_drafts WHERE id=?)", (identity,))
    raw = json.loads(messages.store.one("SELECT video_evidence FROM dm_drafts WHERE id=?", (identity,))["video_evidence"])
    raw["public_source_thread"]["source"]["text"] = "这段真有意思"
    raw["public_source_thread"]["addressing"]["source_mentions"] = []
    raw["public_source_thread"]["parent"] = None
    raw["public_source_thread"]["addressing"]["source_parent_comment_id"] = None
    messages.store.execute("UPDATE dm_drafts SET video_evidence=?,reason='long_followup' WHERE id=?", (json.dumps(raw, ensure_ascii=False), identity))
    await messages._generate(identity, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "not_initiated"
    assert not messages.adapter.sent


async def test_unanswered_contact_is_not_repeated_from_another_comment(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    await messages._generate(first, True)
    second = invite(messages, cid="702")
    await messages._generate(second, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (second,))
    assert row["status"] == "waiting" and row["error"] is None
    assert json.loads(row["outreach_check"])["previous_draft_id"] == first
    assert len(checks) == 1 and len(messages.adapter.sent) == 1


async def test_unknown_outreach_receipt_fences_same_matter_from_another_entry(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    messages.adapter.uncertain = True
    with pytest.raises(ValueError):
        await messages._generate(first, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (first,))["status"] == "publish_uncertain"
    second = invite(messages, cid="702")
    await messages._generate(second, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (second,))["status"] == "waiting"
    assert len(messages.adapter.sent) == 1 and len(checks) == 1


async def test_legacy_published_outreach_does_not_recontact_without_new_incoming(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    await messages._generate(first, True)
    messages.store.execute("UPDATE dm_drafts SET contact_type='',trigger_source=NULL,outreach_check=NULL,purpose='',contact_key='' WHERE id=?", (first,))
    second = invite(messages, cid="702")
    await messages._generate(second, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (second,))["status"] == "waiting"
    assert len(messages.adapter.sent) == 1 and len(checks) == 1


async def test_different_comment_thread_is_not_itself_a_new_matter(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    await messages._generate(first, True)
    second = invite(messages, cid="702", root="800")
    await messages._generate(second, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (second,))["status"] == "waiting"
    assert checks[-1]["previous_unanswered_contacts"][0]["id"] == first
    assert "text" not in checks[-1]["previous_unanswered_contacts"][0]
    assert len(messages.adapter.sent) == 1


async def test_pausing_parent_retains_queued_task_without_first_contact_calls(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    messages.operations.configure({"enabled": False})
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "queued" and row["resume_pending"] == 1
    assert not generated and not checks and not messages.adapter.sent


async def test_incoming_messages_merge_before_generation_without_outreach_check(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    first = incoming(messages, key="first", account_id=ACTOR, text="先问一句")
    identity = messages.enqueue("message", first, automatic=True, schedule=False)
    second = incoming(messages, key="second", account_id=ACTOR, text="刚才那句补充一下")
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "published" and row["source_id"] == second
    assert json.loads(row["trigger_source"])["ids"] == [first, second]
    assert generated[0]["text"] == "刚才那句补充一下"
    assert len(generated) == 1 and checks == []


async def test_new_message_during_generation_never_sends_old_answer(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = incoming(messages, key="first", account_id=ACTOR, text="第一句")
    identity = messages.enqueue("message", first, automatic=True, schedule=False)
    before = messages.runtime.generate_reply
    once = True

    async def generate(*args, **kwargs):
        nonlocal once
        if once:
            once = False
            incoming(messages, key="second", account_id=ACTOR, text="请按这一句来回复")
        return await before(*args, **kwargs)

    monkeypatch.setattr(messages.runtime, "generate_reply", generate)
    await messages._generate(identity, True)
    assert not messages.adapter.sent
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"
    await messages._generate(identity, True)
    assert len(messages.adapter.sent) == 1 and not checks


async def test_legacy_approved_comment_with_no_invitation_does_not_bypass_check(messages, monkeypatch):
    await setup_delivery(messages, monkeypatch)
    identity = invite(messages, mentioned="someone-else")
    messages.store.execute("UPDATE dm_drafts SET status='approved',text='旧稿',purpose='',trigger_source=NULL,proposed_text='',answer_check=? WHERE id=?",
        (json.dumps({"generation_identity": messages.runtime.identity_snapshot(messages.runtime.character, ACTOR)}), identity))
    row = await messages.send(identity)
    assert row["status"] == "not_initiated" and not messages.adapter.sent


async def test_legacy_outreach_recovers_real_participants_without_watching_video(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    source = messages.store.one("SELECT c.* FROM comments c JOIN dm_drafts d ON d.source_id=c.id WHERE d.id=?", (identity,))
    calls = []

    async def metadata(bvid):
        calls.append("metadata")
        return {"aid": "42", "bvid": bvid, "owner_uid": "other-uploader"}

    async def read_thread(target):
        calls.append("thread")
        return {"source": {"text": source["content"], "author_uid": "123"}, "items": [
            {"comment_id": "700", "thread_id": "700", "parent_comment_id": None,
             "user_id": ACTOR, "username": "我方", "text": "原讨论"},
            {"comment_id": "701", "thread_id": "700", "parent_comment_id": "700", "user_id": "123",
             "username": "鱼宝", "text": source["content"], "mentions": [{"uid": "different-account", "name": "另一个OC"}]},
        ]}

    async def read(method, *args):
        return await method(*args)

    async def validate(draft):
        return None

    monkeypatch.setattr(messages.runtime, "browsing", SimpleNamespace(
        adapter=SimpleNamespace(video_metadata=metadata, read_video_thread=read_thread),
        _read=read, validate_private_source=validate,
    ), raising=False)
    messages.store.execute("UPDATE dm_drafts SET status='approved',text='原待发稿',video_evidence=NULL,answer_check=? WHERE id=?",
        (json.dumps({"generation_identity": messages.runtime.identity_snapshot(messages.runtime.character, ACTOR)}), identity))
    result = await messages.send(identity)
    assert result["status"] == "not_initiated" and not messages.adapter.sent
    assert calls == ["metadata", "thread"] and len(checks) == 1
    assert checks[0]["public_thread"]["addressing"]["source_mentions"][0]["uid"] == "different-account"


async def test_revised_outreach_payload_cannot_bypass_the_actual_body_check(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    await messages._generate(identity, False)
    assert len(checks) == 1
    messages.review(identity, "edit", text="与邀请事项无关的修订")
    messages.review(identity, "approve")
    original = messages.runtime.llm.generate_json

    async def check(packet, schema, *, max_tokens):
        data = json.loads(packet[-1]["content"])
        assert data["outgoing_text"] == "与邀请事项无关的修订"
        value = await original(packet, schema, max_tokens=max_tokens)
        value["body_addresses_purpose"] = False
        return value

    monkeypatch.setattr(messages.runtime.llm, "generate_json", check)
    result = await messages._send_once(identity)
    assert result["contact_state"] == "awaiting_content" and not messages.adapter.sent
    assert len(checks) == 2


async def test_send_refreshes_real_conversation_then_merges_before_transport(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    first = incoming(messages, key="first", account_id=ACTOR, text="第一句")
    identity = messages.enqueue("message", first, schedule=False)
    await messages._generate(identity, False)
    messages.review(identity, "approve")

    async def self_uid():
        return ACTOR

    async def history(uid):
        return [{"sender_uid": "123", "receiver_id": ACTOR, "msg_type": 1, "msg_key": "9002",
                 "timestamp": __import__("time").time(), "content": {"content": "发送前的新补充"}}]

    monkeypatch.setattr(messages.adapter, "self_uid", self_uid, raising=False)
    monkeypatch.setattr(messages.adapter, "history", history)
    monkeypatch.setattr(messages, "_schedule", lambda *args: None)
    messages.injected = False
    messages.credential_signature = (None, None)
    with pytest.raises(MessageInputChanged):
        await messages.send(identity)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    source = messages.store.one("SELECT content FROM dm_inbox WHERE id=?", (row["source_id"],))
    assert row["status"] == "queued" and source["content"] == "发送前的新补充"
    assert len(generated) == 1 and checks == [] and not messages.adapter.sent


async def test_arrival_at_authorization_boundary_cancels_old_submission(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = messages.enqueue("message", incoming(messages, account_id=ACTOR), schedule=False)
    await messages._generate(identity, False)
    messages.review(identity, "approve")
    original = messages.adapter.send

    async def send(uid, text, *, authorized):
        incoming(messages, key="late", account_id=ACTOR, text="等一下，按这句来")
        return await original(uid, text, authorized=authorized)

    monkeypatch.setattr(messages.adapter, "send", send)
    monkeypatch.setattr(messages, "_schedule", lambda *args: None)
    with pytest.raises(MessageInputChanged):
        await messages.send(identity)
    assert not messages.adapter.sent and checks == []
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"
    assert messages.store.one("SELECT status FROM dm_attempts")["status"] == "cancelled"


async def test_identity_update_regenerates_and_keeps_the_explicit_send_authorization(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    identity = messages.enqueue("message", incoming(messages, account_id=ACTOR), schedule=False)
    await messages._generate(identity, False)
    messages.review(identity, "approve")
    character = messages.runtime.character
    current = identity_snapshot({"account_uid": ACTOR, "account_display_name": "新账号名", "account_bio": "新简介"}, character)

    async def refreshed(*args, **kwargs):
        return current

    monkeypatch.setattr(messages.runtime, "identity_snapshot", lambda *args, **kwargs: current)
    monkeypatch.setattr(messages.runtime, "refresh_identity", refreshed)
    row = await messages.send(identity)
    assert row["status"] == "published" and len(generated) == 2
    assert checks == [] and len(messages.adapter.sent) == 1
    saved = messages.store.one("SELECT answer_check FROM dm_drafts WHERE id=?", (identity,))
    assert json.loads(saved["answer_check"])["generation_identity"] == current


async def test_account_uid_change_never_migrates_private_context(messages, monkeypatch):
    generated, _ = await setup_delivery(messages, monkeypatch)
    identity = messages.enqueue("message", incoming(messages, account_id=ACTOR), schedule=False)
    await messages._generate(identity, False)
    messages.review(identity, "approve")

    async def changed(*args, **kwargs):
        return {"account_uid": "different-account", "identity_version": "changed"}

    monkeypatch.setattr(messages.runtime, "refresh_identity", changed)
    with pytest.raises(ValueError, match="不能迁移"):
        await messages.send(identity)
    assert len(generated) == 1 and not messages.adapter.sent


async def test_profile_change_at_actual_submit_requeues_without_sending_old_identity(messages, monkeypatch):
    await setup_delivery(messages, monkeypatch)
    identity = messages.enqueue("message", incoming(messages, account_id=ACTOR), schedule=False)
    await messages._generate(identity, False)
    messages.review(identity, "approve")
    original = messages.adapter.send
    changed = identity_snapshot({"account_uid": ACTOR, "account_bio": "提交前修改的简介"}, messages.runtime.character)

    async def send(uid, text, *, authorized):
        monkeypatch.setattr(messages.runtime, "identity_snapshot", lambda *args, **kwargs: changed)
        return await original(uid, text, authorized=authorized)

    monkeypatch.setattr(messages.adapter, "send", send)
    monkeypatch.setattr(messages, "_schedule", lambda *args: None)
    with pytest.raises(MessageInputChanged):
        await messages.send(identity)
    assert not messages.adapter.sent
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"


async def test_agreement_waits_without_check_until_real_bound_web_fetch_completes(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages, trigger="agreement")
    await messages._generate(identity, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "waiting"
    assert checks == [] and not messages.adapter.sent
    assert not await messages.fulfill_agreement(identity, 999999)

    # Execute the existing generation+web_fetch completion path with isolated HTTP.
    from test_operation_generation import model_for

    from tuber_studio import web_fetch

    previous = messages.runtime.llm
    original_generate = messages.runtime.generate_reply
    requests, _, _ = await model_for(messages.runtime, messages.settings,
        first={"web_fetch": "https://example.org/config"}, corrected={"spoken_text": "查到了，配置说明在设置页。"})
    provider = messages.runtime.llm

    async def fetch(url):
        return {"url": url, "title": "配置说明", "excerpt": "具体配置位于设置页", "truncated": False}

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    messages.runtime.browsing = SimpleNamespace(messages=messages)
    messages.operations_resumed = False
    event = {"event_id": "lookup", "text": "完成之前约定的配置查找", "account_id": ACTOR, "agreement_id": identity}
    try:
        async def invoke(packet, *, web_fetch=None):
            schema = {"type": "object", "properties": {"spoken_text": {"type": "string"}}, "required": ["spoken_text"]}
            if web_fetch:
                return await web_fetch.complete(packet, schema, lambda p, s: provider.generate_json(p, s, max_tokens=1024))
            return await provider.generate_json(packet, schema, max_tokens=1024)

        await generate_with_recovery(messages.runtime, channel="test-agreement-lookup", identity="complete-config",
            binding={"task": "generation", "character": {"id": messages.runtime.character["id"], "version": messages.runtime.character["version"]}, "source": {"account_id": ACTOR, "agreement_id": identity}},
            character=messages.runtime.character, messages=[{"role": "user", "content": "找到配置说明"}],
            generate=invoke, validate=lambda value: value, max_tokens=1024, guard=lambda: None,
            context_data={"event": event}, allow_web_fetch=True)
        assert requests
        assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "queued"
    finally:
        await provider.close()
        messages.runtime.llm = previous
        messages.runtime.generate_reply = original_generate
        messages.runtime.browsing = None
        messages.operations_resumed = True
    await messages._generate(identity, True)
    assert len(checks) == 1 and len(messages.adapter.sent) == 1
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "published"
