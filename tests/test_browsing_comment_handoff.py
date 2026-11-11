"""Real comment-to-DM execution with SQLite and isolated delivery transport."""

import json
import time

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet
from test_browsing_comment_only import (
    comment,
    enable_comment_choices,
    forbid_video_content,
)

INVITATION = "@大肥鱼 请私信告诉我盐水鸡蛋实验要多少盐"


def attach_invitation_source(browsing, monkeypatch):
    async def fetch(video, max_pages=1):
        return {"items": [comment() | {"content": INVITATION, "mentions": [{"uid": "100", "name": "大肥鱼"}]}]}

    async def read(target):
        return {"author_uid": "300", "text": INVITATION}

    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", read, raising=False)


class PrivateDelivery:
    def __init__(self):
        self.sent = []

    async def self_uid(self):
        return "100"

    async def send(self, uid, text, *, authorized):
        assert authorized()
        self.sent.append((uid, text))
        return {"msg_key": "private-1", "receiver_id": uid}

    async def close(self):
        pass


async def prepare_handoff(browsing, monkeypatch, *, automatic):
    browsing.store.set("message_policy", {
        "enabled": True, "allow_comment_handoff": True, "auto_send": automatic,
    })
    await enable_comment_choices(browsing, monkeypatch)
    attach_invitation_source(browsing, monkeypatch)
    forbid_video_content(browsing, monkeypatch)
    browsing.runtime.llm.response_action = "handoff_private"
    transport = PrivateDelivery()
    browsing.messages.adapter = transport
    browsing.messages.injected = False
    browsing.messages.credential_signature = ("test", "test")
    return transport


@pytest.mark.parametrize("automatic", [False, True])
async def test_comment_only_handoff_preserves_real_source_and_private_body_once(browsing, monkeypatch, automatic):
    transport = await prepare_handoff(browsing, monkeypatch, automatic=automatic)
    first = await finished(browsing, automatic=True, video_only=True)
    assert first["status"] == "completed", first
    assert not browsing.intents() and not transport.sent
    reply_round = await finished(browsing, automatic=True, interaction_only=True)
    assert reply_round["status"] == "completed", reply_round
    intent, = browsing.intents()
    draft = browsing.store.one("SELECT * FROM dm_drafts")
    assert intent["kind"] == intent["status"] == "private_handoff"
    assert intent["text"] == "" and draft["text"]
    assert draft["status"] == ("published" if automatic else "pending_review"), draft
    assert draft["account_id"] == "100" and draft["platform_user_id"] == "300"
    source = browsing.store.one("SELECT * FROM comments WHERE id=?", (draft["source_id"],))
    assert source["comment_id"] == source["thread_id"] == "601"
    assert source["platform_user_id"] == "300" and source["content"] == INVITATION
    evidence = json.loads(draft["video_evidence"])
    assert "source_event" not in evidence and evidence["material_keys"] == []
    assert evidence["public_source_thread"]["source"]["text"] == INVITATION
    assert evidence["public_source_thread"]["target"]["parent"] == "601"
    assert evidence["origin_round_id"] == reply_round["id"]
    assert "原评论请求私聊" in intent["reason"] and INVITATION.rstrip("。！？!?；;\n") in intent["reason"]
    assert intent["target"]["private_draft_id"] == draft["id"]
    assert draft["text"] not in json.dumps(browsing.inbox(), ensure_ascii=False)
    assert not browsing.adapter.sent
    assert len(transport.sent) == int(automatic)
    # Returning to this source neither regenerates nor resends the DM.
    second = await finished(browsing, automatic=True, video_only=True)
    assert second["status"] == "completed"
    assert (await finished(browsing, automatic=True, interaction_only=True))["status"] == "completed"
    assert len(browsing.store.all("SELECT * FROM dm_drafts")) == 1
    assert len(transport.sent) == int(automatic)
    assert not browsing.store.all("SELECT * FROM browsing_watch_starts")
    assert not browsing.store.all("SELECT * FROM browsing_video_progress")


async def test_handoff_source_changed_at_send_is_retained_without_send(browsing, monkeypatch):
    transport = await prepare_handoff(browsing, monkeypatch, automatic=True)

    async def changed(target):
        return {"author_uid": "999", "text": "另一位用户的不同评论"}

    monkeypatch.setattr(browsing.adapter, "read_video_reply", changed)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed"
    row = await finished(browsing, automatic=True, interaction_only=True)
    assert row["status"] == "partial"
    draft = browsing.store.one("SELECT * FROM dm_drafts")
    assert draft["status"] == "pending_review" and draft["text"]
    assert draft["platform_user_id"] == "300"
    assert not transport.sent and not browsing.adapter.sent


async def test_verified_notification_handoff_does_not_require_video_evidence(browsing, monkeypatch):
    transport = await prepare_handoff(browsing, monkeypatch, automatic=False)
    await browsing.configure({"mentions_enabled": True, "mention_reply_enabled": True})
    await browsing.preflight()
    await browsing.sync_notifications()
    browsing.adapter.feed["mention"] = [{
        "event_id": "invitation-notification", "kind": "mention", "resource_type": "video",
        "account_id": "100", "sender_uid": "300", "comment_id": "502", "timestamp": time.time(),
    }]
    original_thread = browsing.adapter.read_public_thread
    evidence_attempts = []

    async def invited_thread(event):
        value = await original_thread(event)
        value["source"]["text"] = INVITATION
        value["items"] = [comment("502", "501") | {
            "content": INVITATION, "parent_comment_id": "501", "mentions": [{"uid": "100", "name": "大肥鱼"}],
        }]
        return value

    async def unavailable(video, config):
        evidence_attempts.append(video)
        raise TimeoutError("视频取材端点暂时不可用")

    monkeypatch.setattr(browsing.adapter, "read_public_thread", invited_thread)
    monkeypatch.setattr(browsing, "_prepare_evidence", unavailable)
    row = await finished(browsing, preview=True, notifications_only=True)
    assert row["status"] == "completed" and not evidence_attempts, row
    intent, = browsing.intents()
    draft, = browsing.store.all("SELECT * FROM dm_drafts")
    assert intent["kind"] == "private_handoff" and intent["text"] == ""
    assert intent["target"]["private_draft_id"] == draft["id"]
    assert draft["status"] == "pending_review" and draft["text"], draft
    assert draft["account_id"] == "100" and draft["platform_user_id"] == "300"
    evidence = json.loads(draft["video_evidence"])
    assert evidence["source_event"]["event_id"] == "invitation-notification"
    assert evidence["public_source_thread"]["source"]["text"] == INVITATION
    assert evidence["material_keys"] == []
    assert not transport.sent and not browsing.adapter.sent


async def test_watched_video_handoff_reuses_caption_evidence_and_private_pipeline(browsing, monkeypatch):
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    mode, _ = await enable_comment_choices(browsing, monkeypatch)
    mode["action"] = "watch"
    await browsing.configure({"top_level_enabled": False})
    attach_invitation_source(browsing, monkeypatch)
    browsing.runtime.llm.response_action = "handoff_private"
    original = browsing.runtime.llm.generate_json

    async def discuss(messages, schema, *, max_tokens):
        value = await original(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ResponseDecision" and model_packet(messages[-1]["content"]).get("video_actions"):
            value.update(action="read_comments", text="")
        return value

    monkeypatch.setattr(browsing.runtime.llm, "generate_json", discuss)
    row = await finished(browsing, automatic=True, video_only=True)
    assert row["status"] == "completed", row
    row = await finished(browsing, automatic=True, interaction_only=True)
    assert row["status"] == "completed", row
    draft = browsing.store.one("SELECT * FROM dm_drafts")
    assert draft["status"] == "pending_review"
    evidence = json.loads(draft["video_evidence"])
    assert evidence["material_keys"] and evidence["segments"]
    assert evidence["public_source_thread"]["source"]["comment_id"] == "601"
    assert "source_event" not in evidence
    assert {intent["kind"] for intent in browsing.intents()} == {"private_handoff"}
    assert not browsing.adapter.sent


async def test_source_is_checked_again_at_private_send_after_generation(browsing, monkeypatch):
    transport = await prepare_handoff(browsing, monkeypatch, automatic=True)
    checks = 0

    async def changing(target):
        nonlocal checks
        checks += 1
        return {"author_uid": "300", "text": "评论已经编辑"}

    monkeypatch.setattr(browsing.adapter, "read_video_reply", changing)
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    await finished(browsing, automatic=True, interaction_only=True)
    draft = browsing.store.one("SELECT * FROM dm_drafts")
    assert checks == 1 and draft["text"]
    assert draft["status"] == "pending_review"
    assert not transport.sent and not browsing.adapter.sent


async def test_send_rechecks_real_mention_uid_even_when_nickname_and_text_match(browsing, monkeypatch):
    transport = await prepare_handoff(browsing, monkeypatch, automatic=True)
    reads = []

    async def read_thread(target):
        reads.append(target["parent"])
        item = comment() | {"content": INVITATION, "mentions": [{"uid": "999", "name": "大肥鱼"}]}
        return {"source": {"author_uid": "300", "text": INVITATION}, "items": [item]}

    monkeypatch.setattr(browsing.adapter, "read_video_thread", read_thread, raising=False)
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    await finished(browsing, automatic=True, interaction_only=True)
    draft = browsing.store.one("SELECT status,text FROM dm_drafts")
    assert reads == ["601"] and draft["text"] and draft["status"] == "not_initiated"
    assert not transport.sent and not browsing.adapter.sent


async def test_handoff_disabled_does_not_create_or_send_private_draft(browsing, monkeypatch):
    transport = await prepare_handoff(browsing, monkeypatch, automatic=True)
    browsing.store.set("message_policy", browsing.messages.policy()["base"] | {"allow_comment_handoff": False})
    assert (await finished(browsing, automatic=True, video_only=True))["status"] == "completed"
    await finished(browsing, automatic=True, interaction_only=True)
    assert not browsing.store.all("SELECT * FROM dm_drafts")
    assert not transport.sent and not browsing.adapter.sent
