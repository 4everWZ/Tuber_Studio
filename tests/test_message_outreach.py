"""Invited first contacts and ordinary incoming replies use different paths."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_direct_messages import incoming
from test_direct_messages import messages as messages
from test_message_memes import image_http as image_http
from test_message_memes import meme_messages as meme_messages
from test_message_memes import prepared

from tuber_studio.direct_messages import MessageInputChanged
from tuber_studio.message_outreach import invitation_possibility
from tuber_studio.models import OperationReply
from tuber_studio.operation_generation import generate_with_recovery
from tuber_studio.operation_identity import identity_snapshot

ACTOR = "injected-test-account"


def test_own_video_does_not_make_other_peoples_reply_a_private_invitation():
    thread = {"resource_author_uid": ACTOR,
              "source": {"comment_id": "801", "author_uid": "100", "text": "可以私信我接着聊"},
              "addressing": {"source_parent_comment_id": "800", "source_mentions": []},
              "parent": {"comment_id": "800", "author_uid": "200", "text": "别人家的 OC 在讨论"}}
    assert invitation_possibility(thread, ACTOR, "100") == "impossible"
    thread["parent"]["author_uid"] = ACTOR
    assert invitation_possibility(thread, ACTOR, "100") == "possible"
    thread["addressing"]["source_parent_verified"] = False
    assert invitation_possibility(thread, ACTOR, "100") == "unresolved"


async def setup_delivery(messages, monkeypatch, *, auto_send=True):
    messages.operations.configure({"enabled": True}, account_id=ACTOR, character=messages.runtime.character)
    await messages.configure({"enabled": True, "auto_send": auto_send, "max_per_hour": 0, "max_per_day": 0})
    await messages.resume_pending()
    generated, checked = [], []

    async def generate(scope, character, event, relationship=None, *, mode):
        generated.append(event)
        return OperationReply(spoken_text="你问的那项配置在设置页，可以接着私下聊。", reply_to=[event["event_id"]]), {
            "generation_identity": messages.runtime.identity_snapshot(character, event["account_id"]),
            "output": {"needs_review": False},
        }

    async def conversation(packet, schema, *, max_tokens):
        if schema["title"] == "ConversationReply":
            event = next(json.loads(message["content"].split("：", 1)[1]) for message in packet
                         if message["content"].startswith("当前观众消息："))
            generated.append(event)
            return {"text": "鱼鱼一起回答你刚才的两条消息。", "reason": "合并回答两条来信",
                    "reply_to": [item["message"] for item in event["conversation"] if item.get("open")], "handled": []}
        checked.append(schema["title"])
        pytest.fail("邀请和渠道由程序绑定，不应请求模型：" + schema["title"])

    monkeypatch.setattr(messages.runtime, "generate_reply", generate)
    monkeypatch.setattr(messages.runtime.llm, "generate_json", conversation, raising=False)
    return generated, checked


def invitation_source(messages, *, cid="701", root="700", mentioned=ACTOR, trigger="invitation"):
    text = "请把我问的那项配置私信告诉我" if trigger != "agreement" else "查到了私信告诉我"
    messages.comments.import_items("BV1d8pU6xEZX", [{"comment_id": cid, "thread_id": root,
        "parent_comment_id": root, "platform_user_id": "123", "display_name": "鱼宝", "content": text}])
    source = messages.store.one("SELECT * FROM comments WHERE comment_id=?", (cid,))
    thread = {"source": {"comment_id": cid, "author_uid": "123", "author_name": "鱼宝", "text": text},
        "target": {"type": 1, "oid": "42", "bvid": source["video_id"], "root": root, "parent": cid},
        "root": {"comment_id": root, "author_uid": ACTOR, "text": "我方此前的公开讨论"},
        "parent": {"comment_id": root, "author_uid": ACTOR, "text": "我方此前的公开讨论"},
        "addressing": {"source_mentions": [{"uid": mentioned, "name": "同名大肥鱼"}], "source_parent_comment_id": root}}
    return source, thread


def invite(messages, *, cid="701", root="700", mentioned=ACTOR, trigger="invitation", proposal=True):
    source, thread = invitation_source(messages, cid=cid, root=root, mentioned=mentioned, trigger=trigger)
    identity = messages.enqueue("comment", source["id"], automatic=True, account_id=ACTOR,
        purpose="讨论你问的配置位置", contact_trigger=trigger, schedule=False,
        proposed_text="配置在设置页，我来接着说。" if proposal else "",
        proposal_identity=messages.runtime.identity_snapshot(messages.runtime.character, ACTOR),
        video_evidence={"material_keys": [], "source_versions": [], "public_source_thread": thread})
    return identity


@pytest.mark.parametrize("mentioned", [ACTOR, "third-party-oc"])
async def test_route_uses_real_invitation_not_model_channel_choice(messages, monkeypatch, mentioned):
    generated, checks = await setup_delivery(messages, monkeypatch)
    source, thread = invitation_source(messages, mentioned=mentioned)
    # Both mentions have the same name; only their actual UID determines the route.
    event = messages.comments._comment_event(source) | {"account_id": ACTOR,
        "video_evidence": {"material_keys": [], "source_versions": [], "public_source_thread": thread}}
    model_requests = []

    async def opposite_model_choice(packet, schema, *, max_tokens):
        model_requests.append(schema["title"])
        return {"channel": "comment" if mentioned == ACTOR else "direct_message", "reason": "user_requested"}

    monkeypatch.setattr(messages.runtime.llm, "generate_json", opposite_model_choice, raising=False)
    routed = await messages.route_comment(source, messages.runtime.character, event, automatic=True)
    await asyncio.wait_for(messages.queue.join(), 2)
    assert not model_requests and not checks
    if mentioned != ACTOR:
        assert routed is None and not generated and not messages.adapter.sent
        assert not messages.store.all("SELECT * FROM dm_drafts")
        return
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (routed,))
    assert row["status"] == "published" and row["source_id"] == source["id"]
    assert len(generated) == 1 and len(messages.adapter.sent) == 1
    assert generated[0]["contact_invitation"]["comment_id"] == source["comment_id"]
    assert generated[0]["recipient"]["uid"] == "123"
    assert await messages.route_comment(source, messages.runtime.character, event, automatic=True) == routed
    await asyncio.wait_for(messages.queue.join(), 2)
    assert len(generated) == 1 and len(messages.adapter.sent) == 1


async def test_valid_invitation_automatically_sends_once_and_reuses_proposal(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "published" and row["contact_key"]
    assert not checks and generated == []
    assert messages.adapter.sent == [("123", "配置在设置页，我来接着说。")]
    assert messages._draft_view(row)["type"] == "outreach"
    with pytest.raises(ValueError):
        await messages.send(identity)
    assert not checks and len(messages.adapter.sent) == 1


@pytest.mark.parametrize("invited", [True, False])
async def test_review_mode_still_checks_invitation_and_never_queues_uninvited_drafts_for_review(messages, monkeypatch, invited):
    generated, checks = await setup_delivery(messages, monkeypatch, auto_send=False)
    identity = invite(messages, mentioned=ACTOR if invited else "other-account")
    # Even a previously queued automatic task must obey the current review mode.
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == ("pending_review" if invited else "not_initiated")
    assessment = json.loads(row["outreach_check"])["assessment"]
    assert assessment["invitation_comment_id"] == ("701" if invited else "")
    assert not checks and not generated and not messages.adapter.sent


async def test_outreach_is_not_reviewable_until_body_generation_finishes(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch, auto_send=False)
    identity = invite(messages, proposal=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = messages.runtime.generate_reply

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(messages.runtime, "generate_reply", delayed)
    task = asyncio.create_task(messages._generate(identity, True))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "generating"
        assert not messages.store.one("SELECT id FROM dm_drafts WHERE id=? AND status='pending_review'", (identity,))
        with pytest.raises(ValueError, match="不能编辑或审核"):
            messages.review(identity, "approve")
    finally:
        release.set()
        await task
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "pending_review"
    assert not checks and not messages.adapter.sent


async def test_incoming_during_outreach_generation_replaces_the_cold_candidate(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch, auto_send=False)
    identity = invite(messages, proposal=False)
    original = messages.runtime.generate_reply

    async def arrival(*args, **kwargs):
        incoming(messages, key="during-first-check", account_id=ACTOR, text="先别讲配置了，我补一句")
        return await original(*args, **kwargs)

    monkeypatch.setattr(messages.runtime, "generate_reply", arrival)
    await messages._generate(identity, True)
    await asyncio.wait_for(messages.queue.join(), 2)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "waiting" and json.loads(row["outreach_check"])["state"] == "reply_received"
    reply = messages.store.one("SELECT * FROM dm_drafts WHERE source_kind='message'")
    assert reply["status"] == "pending_review" and len(generated) == 2
    assert not checks and not messages.adapter.sent


async def test_review_activation_does_not_send_existing_approved_published_or_unknown_drafts(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch, auto_send=False)
    identities = []
    for index, status in enumerate(("approved", "published", "publish_uncertain")):
        identity = invite(messages, cid=str(801 + index))
        messages.store.execute("UPDATE dm_drafts SET status=?,resume_pending=1,receipt=? WHERE id=?",
            (status, json.dumps({"msg_key": "actual-fixture-receipt"}) if status == "published" else None, identity))
        identities.append(identity)
    await messages.pause()
    await messages.resume_pending()
    await asyncio.wait_for(messages.queue.join(), 2)
    assert [messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] for identity in identities] == ["approved", "published", "publish_uncertain"]
    assert not generated and not checks and not messages.adapter.sent


async def test_invitation_to_another_uid_cannot_be_claimed_by_model_reason(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages, mentioned="another-oc-account")
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert not checks and row["status"] == "not_initiated" and row["error"] is None
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
    assert not checks and len(messages.adapter.sent) == 1


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
    assert len(messages.adapter.sent) == 1 and not checks


async def test_legacy_published_outreach_does_not_recontact_without_new_incoming(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    await messages._generate(first, True)
    messages.store.execute("UPDATE dm_drafts SET contact_type='',trigger_source=NULL,outreach_check=NULL,purpose='',contact_key='' WHERE id=?", (first,))
    second = invite(messages, cid="702")
    await messages._generate(second, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (second,))["status"] == "waiting"
    assert len(messages.adapter.sent) == 1 and not checks


async def test_different_comment_thread_is_not_itself_a_new_matter(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    await messages._generate(first, True)
    second = invite(messages, cid="702", root="800")
    await messages._generate(second, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (second,))
    assert row["status"] == "waiting" and json.loads(row["outreach_check"])["previous_draft_id"] == first
    history = messages._unanswered_outreach(row)
    assert history[0]["id"] == first and history[0]["public_source"]["text"] == "请把我问的那项配置私信告诉我"
    assert "text" not in history[0] and not checks and len(messages.adapter.sent) == 1


async def test_new_invitation_retains_real_old_public_source_instead_of_only_purpose(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    first = invite(messages)
    await messages._generate(first, True)
    await messages.configure({"enabled": True, "auto_send": False, "max_per_hour": 0, "max_per_day": 0})
    second = invite(messages, cid="702", root="800")
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (second,))
    source = "刚才那项之外，我还想问屏幕颜色配置，可以私信继续聊吗？"
    evidence = json.loads(row["video_evidence"])
    evidence["public_source_thread"]["source"]["text"] = source
    messages.store.execute("UPDATE comments SET content=? WHERE id=?", (source, row["source_id"]))
    messages.store.execute("UPDATE dm_drafts SET video_evidence=? WHERE id=?", (json.dumps(evidence, ensure_ascii=False), second))
    await messages._generate(second, True)
    assert not checks
    prior = messages._unanswered_outreach(messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (second,)))[0]
    assert prior["id"] == first and prior["public_source"]["text"] == "请把我问的那项配置私信告诉我"
    assert prior["public_source"]["author_uid"] == "123" and "text" not in prior
    assert len(messages.adapter.sent) == 1


async def test_nickname_only_mention_without_real_uid_does_not_invite_this_account(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    text = "@大肥鱼 把配置私信告诉我"
    evidence = json.loads(row["video_evidence"])
    thread = evidence["public_source_thread"]
    thread["source"]["text"] = text
    thread["root"] = thread["source"]
    thread["parent"] = None
    thread["addressing"].update(source_mentions=[], source_parent_comment_id=None)
    messages.store.execute("UPDATE comments SET content=? WHERE id=?", (text, row["source_id"]))
    messages.store.execute("UPDATE dm_drafts SET video_evidence=? WHERE id=?", (json.dumps(evidence, ensure_ascii=False), identity))
    await messages._generate(identity, True)
    assert not checks and not messages.adapter.sent
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "not_initiated"


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
    assert calls == ["metadata", "thread"] and not checks
    recovered = json.loads(messages.store.one("SELECT video_evidence FROM dm_drafts WHERE id=?", (identity,))["video_evidence"])
    assert recovered["public_source_thread"]["addressing"]["source_mentions"][0]["uid"] == "different-account"


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


async def test_other_agreements_successful_fetch_cannot_complete_this_invitation(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages, trigger="agreement", proposal=False)
    other = invite(messages, cid="702", trigger="agreement", proposal=False)
    coordinator = messages.runtime.continuations
    record = coordinator.begin("test-agreement-lookup", "other-agreement",
        {"task": "generation", "character": {"id": messages.runtime.character["id"],
            "version": messages.runtime.character["version"]},
         "source": {"account_id": ACTOR, "agreement_id": other}},
        {"kind": "generation_start"})
    coordinator._step(record, "web_fetch", "ok", context={
        "status": "ok", "url": "https://example.org/config", "title": "其他约定的配置说明"})
    step = messages.store.one("SELECT id FROM operation_continuation_steps WHERE continuation_id=? AND phase='web_fetch'",
                              (record["id"],))
    draft = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    evidence = json.loads(draft["video_evidence"])
    evidence["completion_evidence"] = [{"step_id": step["id"]}]
    messages.store.execute("UPDATE dm_drafts SET video_evidence=? WHERE id=?",
                           (json.dumps(evidence, ensure_ascii=False), identity))
    await messages._generate(identity, True)
    draft = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert draft["status"] == "waiting" and not draft["text"] and draft["receipt"] is None
    assert messages._completion_evidence(draft) == []
    check = json.loads(draft["outreach_check"])
    assert check["state"] == "awaiting_completion" and check["invitation_verified"] is True
    assert not generated and not checks and not messages.adapter.sent
    assert not messages.store.all("SELECT * FROM dm_attempts")
    assert not await messages.fulfill_agreement(identity, step["id"])


async def test_agreement_is_checked_before_waiting_and_rechecks_actual_result_after_bound_fetch(messages, monkeypatch):
    _, checks = await setup_delivery(messages, monkeypatch)
    identity = invite(messages, trigger="agreement")
    await messages._generate(identity, True)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "waiting"
    assert not checks and not messages.adapter.sent
    first = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    first_check = json.loads(first["outreach_check"])
    assert first_check["invitation_verified"] is True and first_check["state"] == "awaiting_completion"
    assert messages._event(first, messages.runtime.character)["agreement_task"]["agreement_id"] == identity
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
    assert not checks and len(messages.adapter.sent) == 1
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "published"


async def test_real_third_party_comment_cannot_become_an_agreement(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch, auto_send=False)
    identity = invite(messages, cid="319387030577", root="319387030577", trigger="agreement", mentioned="3707027413010556")
    source_text = "@点就睡觉sj 正常男大，这条是给咱俩照镜子：翻 60 个号 2368 次到访，8 个号扛了 97%，插件教程最能钓鱼。看完心虚三秒，然后我继续刷 (￣ー￣)"
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    evidence = json.loads(row["video_evidence"])
    thread = evidence["public_source_thread"]
    thread["source"].update(text=source_text, author_uid="3747574395702579")
    thread["root"] = thread["source"] | {"parent_comment_id": None, "mentions": thread["addressing"]["source_mentions"]}
    thread["timeline"] = [thread["root"]]
    thread["parent"] = None
    thread["addressing"]["source_parent_comment_id"] = None
    messages.store.execute("UPDATE comments SET content=?,platform_user_id=? WHERE id=?", (source_text, "3747574395702579", row["source_id"]))
    messages.store.execute("UPDATE dm_drafts SET platform_user_id=?,video_evidence=? WHERE id=?", ("3747574395702579", json.dumps(evidence, ensure_ascii=False), identity))
    await messages._generate(identity, True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    check = json.loads(row["outreach_check"])
    assert row["status"] == "not_initiated" and check["invitation_verified"] is False
    assert check["action"] == "stay_public" and not checks
    assert "agreement_task" not in messages._event(row, messages.runtime.character)
    assert not generated and not messages.adapter.sent
    with pytest.raises(ValueError):
        messages.retry(identity)


async def test_legacy_unchecked_agreement_cannot_be_researched_or_retried(messages, monkeypatch):
    generated, checks = await setup_delivery(messages, monkeypatch, auto_send=False)
    identity = invite(messages, trigger="agreement")
    messages.store.execute("UPDATE dm_drafts SET status='waiting',outreach_check=? WHERE id=?",
        (json.dumps({"action": "wait", "state": "awaiting_completion", "reason": "约定事项尚无完成回执"}), identity))
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert "agreement_task" not in messages._event(row, messages.runtime.character)
    assert not await messages.fulfill_agreement(identity, 999999)
    with pytest.raises(ValueError):
        messages.retry(identity)
    assert not generated and not checks and not messages.adapter.sent


@pytest.mark.parametrize("state", ["starting", "running"])
@pytest.mark.parametrize("image", [False, True])
async def test_review_fence_blocks_approved_text_and_real_catalog_image_before_preparation(meme_messages, monkeypatch, state, image):
    messages, _, _, transport = meme_messages
    identity = await prepared(meme_messages, image=image, account_id="900")
    if not image:
        messages.review(identity, "approve")
    messages.operations.configure({"enabled": True}, account_id="900", character=messages.runtime.character)
    messages.store.set("browsing_review_session", {"id": "isolated-review", "state": state,
        "account_id": "900", "character_id": messages.runtime.character["id"]})
    before = len(transport["requests"])

    async def cannot_refresh(*args, **kwargs):
        pytest.fail("review写入限制须在身份刷新、生成和图片上传之前执行")

    monkeypatch.setattr(messages.runtime, "refresh_identity", cannot_refresh)
    with pytest.raises(ValueError, match="review验收"):
        await messages.send(identity)
    # Cover the lower delivery boundary used after publication revisions too.
    with pytest.raises(ValueError, match="review验收"):
        await messages._send_once(identity)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "approved" and row["receipt"] is None
    assert len(transport["requests"]) == before
    assert not messages.store.one("SELECT id FROM dm_attempts WHERE draft_id=?", (identity,))
    assert not messages.store.one("SELECT token FROM account_writes WHERE channel='messages'")


async def test_review_unknown_receipt_only_reconciles_and_never_resends(messages, monkeypatch):
    await setup_delivery(messages, monkeypatch)
    identity = invite(messages)
    messages.adapter.uncertain = True
    with pytest.raises(ValueError):
        await messages._generate(identity, True)
    messages.store.set("browsing_review_session", {"id": "isolated-review", "state": "running", "account_id": ACTOR})
    reads = []

    async def actor():
        return ACTOR

    async def reconcile(*args, **kwargs):
        reads.append(kwargs)
        return {"status": "unsupported"}

    monkeypatch.setattr(messages.adapter, "self_uid", actor, raising=False)
    monkeypatch.setattr(messages.adapter, "reconcile_publication", reconcile, raising=False)
    before = len(messages.adapter.sent)
    with pytest.raises(ValueError, match="仍待核对"):
        await messages.send(identity)
    assert len(reads) == 1 and len(messages.adapter.sent) == before == 1
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "publish_uncertain"


async def test_review_unbound_legacy_draft_cannot_start_identity_regeneration_or_attempt(messages, monkeypatch):
    generated, _ = await setup_delivery(messages, monkeypatch, auto_send=False)
    identity = messages.enqueue("message", incoming(messages, key="legacy-unbound"), schedule=False)
    await messages._generate(identity, False)
    messages.review(identity, "approve")
    messages.store.set("browsing_review_session", {"state": "running", "account_id": ACTOR})
    assert messages.store.one("SELECT account_id FROM dm_drafts WHERE id=?", (identity,))["account_id"] == ""
    with pytest.raises(ValueError, match="review验收"):
        await messages.send(identity)
    assert len(generated) == 1 and not messages.adapter.sent
    assert not messages.store.one("SELECT id FROM dm_attempts WHERE draft_id=?", (identity,))
