"""Evidence-gated workflows with real SQLite, cache and task cancellation."""

import asyncio
import json
import re
import time

import httpx
import pytest

from tuber_studio.browsing import BrowsingService
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.comments import Comments
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.dynamics import Dynamics

BV = "BV1d8pU6xEZX"


def model_packet(source):
    body = source.split("：", 1)[1]
    if "\n\n所选材料：\n" in body:
        header, content = body.split("\n\n所选材料：\n", 1)
        value = json.loads(header)
        value["evidence"] = [
            {"id": reference} for reference in re.findall(r"(?m)^\[(r\d+)(?:\||\])", content)
        ]
        value["content"] = content
        return value
    header, separator, content = body.partition("\n\n内容：\n")
    value = json.loads(header)
    if separator:
        value["content"] = content
        value["segments"] = [{"id": reference} for reference in re.findall(r"(?m)^\[(e\d+)\|", content)]
    return value


class ControlledPlatform:
    def __init__(self):
        self.account = "100"
        self.calls = []
        self.sent = []
        self.feed = {"mention": [], "reply": []}
        self.no_subtitles = False
        self.delay = 0
        self.description = "完整的盐水实验简介"
        self.failure = None
        self.before_submit = None

    async def close(self):
        pass

    async def self_uid(self):
        return self.account

    async def discover(self, source, keyword="", page=1, limit=20):
        self.calls.append(("discover", source, keyword, page))
        return {
            "items": [{"bvid": BV, "aid": 42, "title": "盐水实验", "owner_uid": "200", "duration": 30}],
            "has_more": False,
        }

    async def video_metadata(self, video):
        self.calls.append(("metadata", video))
        await asyncio.sleep(self.delay)
        return {
            "bvid": video,
            "aid": 42,
            "title": "盐水实验",
            "description": self.description,
            "tags": ["科普"],
            "owner_uid": "200",
            "duration": 30,
            "parts": [{"cid": "11", "title": "第一P", "duration": 30}],
        }

    async def subtitles(self, bvid, cid, languages):
        self.calls.append(("subtitles", bvid, cid))
        if self.no_subtitles:
            from tuber_studio.browsing_evidence import EvidenceUnavailable

            raise EvidenceUnavailable("no_subtitles", "无CC字幕")
        await asyncio.sleep(self.delay)
        return {
            "bvid": bvid,
            "cid": cid,
            "track_id": "track",
            "language": "zh-CN",
            "is_ai": True,
            "segments": [{"id": "0", "start": 0, "end": 10, "text": "加盐之后，鸡蛋浮了起来。"}],
            "coverage": {"complete": True},
        }

    async def fetch(self, video, max_pages=1):
        return {"video_id": video, "items": []}

    async def notifications(self, kind, cursor=None):
        return {"account_id": self.account, "items": self.feed[kind], "has_more": False, "cursor": None}

    async def read_public_thread(self, event):
        return {
            "account_id": self.account,
            "resource_type": "video",
            "target": {"type": 1, "oid": "42", "bvid": BV, "root": "501", "parent": "502"},
            "source": {"comment_id": "502", "author_uid": "300", "text": "大肥鱼你怎么看盐水里的鸡蛋？"},
            "items": [],
            "public_text": "",
        }

    async def publish_top_level(self, target, text, *, before_send):
        if self.before_submit:
            await self.before_submit()
        before_send()
        self.sent.append((target, text))
        if self.failure:
            raise self.failure
        return {"code": 0, "rpid": "900"}

    async def publish_resource_reply(self, target, text, *, before_send):
        return await self.publish_top_level(target, text, before_send=before_send)

    async def verify_publication(self, target, receipt):
        return {"status": "not_found"}


class ControlledModel:
    def __init__(self):
        self.calls = []
        self.active = self.peak = 0
        self.interest = "like"
        self.worth = True
        self.response_action = "reply_public"
        self.bad_ref = False

    async def health(self):
        return {"ok": True}

    async def close(self):
        pass

    async def generate(self, messages, character, event_id, source_text=None):
        from tuber_studio.models import Reply

        return Reply(
            spoken_text="鱼鱼在这里陪你聊，我们慢慢说。",
            emotion="cheerful",
            animation_id="talk",
            reply_to=[event_id],
        )

    async def count_tokens(self, messages):
        return sum(len(m["content"]) for m in messages) // 2 + 1

    async def generate_json(self, messages, schema, *, max_tokens):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.001)
            data = model_packet(messages[-1]["content"])
            self.calls.append((schema["title"], data))
            if schema["title"] == "BrowsePlan":
                return {"action": "recommend", "keywords": [], "reason": "explore", "delay_seconds": 300}
            if schema["title"] == "ContentDecision":
                return {
                    "interest": self.interest,
                    "worth_commenting": self.worth,
                    "reason": "specific_detail" if self.worth else "nothing_to_add",
                    "evidence_ids": ["missing"] if self.bad_ref else [data["segments"][0]["id"]],
                    "coverage_required": "provided",
                }
            return {
                "action": self.response_action,
                "text": "盐水把鸡蛋托起来这段好有意思，鱼鱼也想试试！",
                "reason": "specific_detail" if self.response_action == "reply_public" else "private_details",
                "evidence_ids": [data["evidence"][0]["id"]],
            }
        finally:
            self.active -= 1


@pytest.fixture
async def browsing(settings, store, runtime):
    adapter = ControlledPlatform()
    comments = Comments(settings, store, runtime, adapter)

    class MessageTransport:
        async def sessions(self):
            return "100", []

        async def close(self):
            pass

    messages = DirectMessages(settings, store, runtime, comments, MessageTransport())
    dynamics = Dynamics(settings, store, runtime, comments)
    comments.direct_messages = messages
    service = BrowsingService(settings, store, runtime, comments, messages, dynamics, read_interval=0)
    runtime.browsing = service
    previous = runtime.llm
    runtime.llm = ControlledModel()
    settings.data["llm"]["kind"] = "llama_cpp"
    service.store.set("browsing_policy", BrowsingPolicy(top_level_enabled=True).model_dump())
    yield service
    await service.close()
    await comments.close()
    runtime.llm = previous


async def finished(service, **args):
    result = await service.run(**args)
    task = service.active.get(result["id"])
    if task:
        await asyncio.wait_for(task, 3)
    return service.repo.round(result["id"])


async def test_preview_complete_evidence_review_and_one_shot(browsing):
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed", row
    intent = browsing.intents()[0]
    assert intent["status"] == "pending_review" and not intent["auto_authorized"]
    assert intent["evidence"] == ["11:0"] and browsing.adapter.sent == []
    browsing.review(intent["id"], "approve")
    result = await browsing.publish(intent["id"])
    assert result["status"] == "published" and result["visibility"] == "not_found"
    assert browsing.adapter.sent[0][0]["root"] == "0"
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    assert len(browsing.adapter.sent) == 1


async def test_missing_cc_never_uses_title_or_comments(browsing):
    browsing.adapter.no_subtitles = True
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed"
    assert browsing.tasks()[0]["reason"] == "no_subtitles"
    assert not browsing.runtime.llm.calls and not browsing.intents()


async def test_content_decision_requests_required_sources_before_generation(browsing):
    captured = []
    generate = browsing.runtime.llm.generate_json

    async def observe(messages, schema, *, max_tokens):
        if schema["title"] == "ContentDecision":
            captured.append(schema)
        return await generate(messages, schema, max_tokens=max_tokens)

    browsing.runtime.llm.generate_json = observe
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed" and browsing.intents()
    branches = {branch["properties"]["worth_commenting"]["const"]: branch for branch in captured[0]["oneOf"]}
    for branch in branches.values():
        assert "evidence_ids" in branch["required"]
        assert branch["properties"]["evidence_ids"]["items"]["enum"] == ["e1"]
    assert branches[True]["properties"]["evidence_ids"]["minItems"] == 1
    assert branches[False]["properties"]["evidence_ids"]["minItems"] == 0


@pytest.mark.parametrize("entrypoint", ["video", "comment"])
async def test_browsing_reads_comments_through_real_adapter_fetch(browsing, entrypoint):
    from tuber_studio.bilibili_browsing import BrowsingAdapter

    requests = []
    text = "我也想试试这个盐水实验"

    def handle(request):
        requests.append(request)
        if request.url.path == "/x/web-interface/view":
            data = {"bvid": BV, "aid": 42, "title": "盐水实验"}
        elif request.url.path == "/x/v2/reply":
            data = {
                "replies": [
                    {
                        "rpid": 501,
                        "member": {"mid": "200", "uname": "观众"},
                        "content": {"message": text},
                    }
                ],
                "page": {"count": 1},
            }
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json={"code": 0, "data": data})

    adapter = BrowsingAdapter()
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handle)
    )
    adapter._read_spacing = 0
    browsing.adapter.fetch = adapter.fetch
    try:
        if entrypoint == "video":
            row = await finished(browsing, preview=True)
            assert row["status"] == "completed", row
            assert browsing.intents()[0]["status"] == "pending_review"
        else:
            browsing.comments.import_items(
                BV,
                [{"comment_id": "501", "platform_user_id": "200", "content": text}],
            )
            comment = browsing.store.one("SELECT * FROM comments")
            result = await browsing.comment_evidence(comment, browsing.runtime.character)
            assert result["segments"]
        assert [request.url.path for request in requests] == [
            "/x/web-interface/view",
            "/x/v2/reply",
        ]
        assert all(request.method == "GET" for request in requests)
        assert not browsing.adapter.sent
    finally:
        await adapter.close()


async def test_evidence_workers_inherit_parent_budget_and_deadline(browsing, monkeypatch):
    original, received = browsing.evidence.prepare, []

    async def observe(video, config, **options):
        received.append(options)
        return await original(video, config, **options)

    monkeypatch.setattr(browsing.evidence, "prepare", observe)
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed" and received
    assert all(option["task"]["round_id"] == row["id"] for option in received)
    assert all(option["task"]["id"] for option in received)
    assert all(option["deadline"] <= row["deadline"] for option in received)


async def test_visual_evidence_cannot_charge_a_different_parent_round(browsing):
    from tuber_studio.browsing import read_round

    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    token = read_round.set(row["id"])
    try:
        with pytest.raises(ValueError, match="父轮预算"):
            await browsing._prepare_evidence(BV, browsing.policy(), task={"round_id": "other-round"})
    finally:
        read_round.reset(token)
    assert not browsing.adapter.calls


async def test_invalid_citation_cannot_create_candidate(browsing):
    browsing.runtime.llm.bad_ref = True
    row = await finished(browsing, preview=True)
    assert row["status"] == "partial" and not browsing.intents()


async def test_liking_does_not_require_comment(browsing):
    browsing.runtime.llm.worth = False
    await finished(browsing, preview=True)
    assert browsing.tasks()[0]["status"] == "viewed_without_comment"
    assert not browsing.intents()


async def test_no_keywords_is_recommend_and_never_invented_search(browsing):
    await finished(browsing, preview=True)
    calls = [c for c in browsing.adapter.calls if c[0] == "discover"]
    assert calls and all(c[1:3] == ("recommend", "") for c in calls)


async def test_user_keywords_preserved_and_sources_deduplicated(browsing):
    browsing.store.set("browsing_policy", BrowsingPolicy(keywords=["盐水", "鸡蛋"]).model_dump())
    await finished(browsing, preview=True)
    assert len([c for c in browsing.adapter.calls if c[0] == "metadata"]) == 1
    assert {c[2] for c in browsing.adapter.calls if c[0] == "discover"} == {"盐水", "鸡蛋"}


async def test_parent_cancel_collects_workers_and_preserves_no_send(browsing):
    browsing.adapter.delay = 0.2
    result = await browsing.run(preview=True)
    await asyncio.sleep(0.02)
    await browsing.stop()
    assert browsing.repo.round(result["id"])["status"] == "cancelled"
    assert not browsing.active and not browsing.evidence.pending and not browsing.heartbeats
    assert not browsing.adapter.sent


async def test_rejected_top_level_stays_deduplicated_after_reread(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "reject")
    result = await browsing.reread(browsing.tasks()[0]["id"])
    await browsing.active[result["id"]]
    assert len(browsing.intents()) == 1 and browsing.intents()[0]["status"] == "rejected"


async def test_shared_quota_and_uncertain_block_new_auto_writes(browsing):
    from tuber_studio.bilibili import PublishUncertain

    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    browsing.adapter.failure = PublishUncertain("timeout")
    with pytest.raises(PublishUncertain):
        await browsing.publish(intent["id"])
    assert browsing.intents()[0]["status"] == "publish_uncertain"
    assert browsing.store.one("SELECT status FROM account_writes")["status"] == "uncertain"
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    assert len(browsing.adapter.sent) == 1


async def test_source_change_invalidates_reviewed_reply(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    browsing.adapter.description = "原始视频说明已修改"
    with pytest.raises(ValueError, match="材料已变化"):
        await browsing.publish(intent["id"])
    assert not browsing.adapter.sent


async def test_adapter_replacement_cannot_publish_with_old_account_credentials(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    previous = browsing.adapter
    replacement = ControlledPlatform()
    replacement.account = "200"
    browsing.comments.adapter = replacement
    with pytest.raises(ValueError, match="账号已变化"):
        await browsing.publish(intent["id"])
    assert not previous.sent and not replacement.sent


async def test_expired_evidence_is_reread_and_compared_before_manual_publish(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    browsing.store.execute("UPDATE browsing_materials SET expires_at=0")
    result = await browsing.publish(intent["id"])
    assert result["status"] == "published"
    assert result["material_keys"] != intent["material_keys"]
    assert browsing.evidence.load(result["material_keys"][0])


async def test_emergency_during_preflight_to_http_means_no_submit(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")

    async def change():
        browsing.store.activate("whale") if False else None
        browsing.comments.credentials["csrf"] = "changed"

    browsing.adapter.before_submit = change
    with pytest.raises(ValueError, match="账号"):
        await browsing.publish(intent["id"])
    assert not browsing.adapter.sent


async def test_per_account_round_owner_is_shared(browsing):
    browsing.adapter.delay = 0.05
    a, b = await asyncio.gather(browsing.run(preview=True), browsing.run(preview=True))
    assert a["id"] == b["id"]
    await browsing.active[a["id"]]


async def test_lease_takeover_fences_old_results(browsing):
    repo = browsing.repo
    row, _ = repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    task = repo.task(row["id"], {"bvid": BV})
    first = repo.claim(task, "first")
    browsing.store.execute("UPDATE browsing_tasks SET lease_until=? WHERE id=?", (time.time() - 1, task))
    second = repo.claim(task, "second")
    with pytest.raises(ValueError, match="旧Worker"):
        repo.complete(task, "first", first, "selected", {})
    repo.complete(task, "second", second, "viewed_without_comment", {})
    assert second == first + 1


async def test_atomic_budget_cannot_expand_on_parallel_reservations(browsing):
    config = browsing.policy() | {"max_model_calls": 1}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    browsing.repo.reserve_budget(row["id"], model_calls=1)
    with pytest.raises(ValueError, match="预算耗尽"):
        browsing.repo.reserve_budget(row["id"], model_calls=1)
    assert browsing.repo.round(row["id"])["budget"]["model_calls"] == 1


async def test_model_not_semantically_validated_retains_review(browsing):
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    await finished(browsing, automatic=True)
    assert browsing.intents()[0]["status"] == "pending_review"
    assert not browsing.status()["automatic_sending_ready"] and not browsing.adapter.sent


async def test_notification_baseline_then_alias_dedup_not_video_seen_dedup(browsing):
    config = BrowsingPolicy(
        mentions_enabled=True, mention_reply_enabled=True, replies_enabled=True, reply_enabled=True
    ).model_dump()
    browsing.store.set("browsing_policy", config)
    event = {
        "event_id": "event-1",
        "kind": "mention",
        "resource_type": "video",
        "account_id": "100",
        "sender_uid": "300",
        "comment_id": "502",
        "timestamp": time.time() - 60,
    }
    browsing.adapter.feed["mention"] = [event]
    await browsing.preflight()
    await browsing.sync_notifications()
    assert browsing.inbox()["items"][0]["status"] == "historical"
    event = dict(event, event_id="event-2", timestamp=time.time() + 0.01)
    browsing.adapter.feed["mention"] = [event]
    browsing.adapter.feed["reply"] = [dict(event, kind="reply")]
    await finished(browsing, preview=True, notifications_only=True)
    assert len(browsing.intents()) == 1
    assert browsing.intents()[0]["target"]["parent"] == "502"
    assert browsing.adapter.sent == []


async def test_cache_pins_drafts_and_clear_does_not_clear_audit(browsing):
    await finished(browsing, preview=True)
    status = browsing.runtime.cache.status()
    assert status["categories"]["browsing"]["protected_bytes"] > 0
    assert status["settings"]["music_limit_mib"] == 15360
    await browsing.runtime.cache.clear("browsing")
    assert browsing.evidence.load(browsing.intents()[0]["material_keys"][0])
    browsing.review(browsing.intents()[0]["id"], "reject")
    browsing.runtime.cache.leases.clear()
    await browsing.runtime.cache.clear("browsing")
    assert len(browsing.intents()) == 1
    with pytest.raises(ValueError, match="清理"):
        browsing.evidence.load(browsing.intents()[0]["material_keys"][0])
    task = browsing.tasks()[0]
    assert task["cache_status"] == "missing" and task["evidence"] == []
    stored = json.loads(browsing.store.one("SELECT payload FROM browsing_tasks")["payload"])
    assert "evidence" not in stored and "description" not in stored.get("metadata", {})


async def test_long_cc_chunks_use_provider_tokens_and_keep_every_reference(browsing):
    await browsing.preflight()
    browsing.settings.data["llm"]["context_tokens"] = 4096
    segments = [{"id": str(i), "start": i, "end": i + 1, "text": "盐水实验浮力测试" * 70} for i in range(30)]
    material = {
        "metadata": {"parts": [{"cid": "11", "title": "第一P"}]},
        "parts": [{"cid": "11", "segments": segments}],
        "coverage": "all_available_cc",
        "missing_parts": [],
    }
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    chunks = await browsing._chunks(material, row)
    assert len(chunks) > 1
    assert [s["id"] for chunk in chunks for s in chunk] == [s["id"] for s in segments]
    from tuber_studio.browsing_inputs import ContentDecisionInput

    presentation = ContentDecisionInput(material)
    for chunk in chunks:
        assert (
            await browsing.runtime.llm.count_tokens(
                [{"role": "user", "content": json.dumps(presentation.present(chunk)[0], ensure_ascii=False)}]
            )
            <= 2048
        )


async def test_restart_never_replays_or_restores_publishing(browsing):
    await finished(browsing, preview=True)
    browsing.store.execute("UPDATE browsing_intents SET status='publishing'")
    browsing.repo.recover()
    assert browsing.intents()[0]["status"] == "publish_uncertain"
    assert not browsing.adapter.sent


async def test_notification_private_handoff_owned_by_round_and_never_public_body(browsing):
    browsing.store.set(
        "browsing_policy",
        BrowsingPolicy(mentions_enabled=True, mention_reply_enabled=True, reply_enabled=True).model_dump(),
    )
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    await browsing.preflight()
    await browsing.sync_notifications()
    browsing.adapter.feed["mention"] = [
        {
            "event_id": "private-event",
            "kind": "mention",
            "resource_type": "video",
            "account_id": "100",
            "sender_uid": "300",
            "comment_id": "502",
            "timestamp": time.time(),
        }
    ]
    browsing.runtime.llm.response_action = "handoff_private"
    row = await finished(browsing, preview=True, notifications_only=True)
    assert row["status"] == "completed", row
    intent = browsing.intents()[0]
    assert intent["status"] == "private_handoff" and intent["text"] == ""
    draft = browsing.store.one("SELECT * FROM dm_drafts")
    assert draft["status"] == "pending_review" and draft["account_id"] == "100"
    assert json.loads(draft["video_evidence"])["origin_round_id"] == row["id"]
    assert browsing.messages.queue.empty() and not browsing.adapter.sent
    relation = browsing.runtime.relationships.view(
        browsing.runtime.character["id"], "bilibili", "300", account_id="100"
    )
    assert relation["affinity"] == 21
    assert draft["text"] not in json.dumps(browsing.inbox(), ensure_ascii=False)
    browsing.store.execute("UPDATE browsing_rounds SET status='cancelled' WHERE id=?", (row["id"],))
    browsing.store.execute("UPDATE dm_drafts SET status='cancelled' WHERE id=?", (draft["id"],))
    browsing.messages.retry(draft["id"])
    browsing.messages.queue.get_nowait()
    browsing.messages.queue.task_done()
    await browsing.messages._generate(draft["id"], automatic=False)
    retried = browsing.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft["id"],))
    assert retried["status"] == "pending_review"
    assert not json.loads(retried["video_evidence"]).get("origin_round_id")
    assert not browsing.adapter.sent


async def test_worker_crash_has_bounded_takeover_without_losing_other_video(browsing):
    original = browsing.adapter.video_metadata
    attempts = 0

    async def fail_once(video):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("worker lost before platform write")
        return await original(video)

    browsing.adapter.video_metadata = fail_once
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed", row
    assert row["metrics"]["takeovers"] == 1 and attempts == 2
    assert browsing.tasks()[0]["generation"] == 2


async def test_automatic_queue_wait_rechecks_old_source_draft(browsing):
    # A manual workflow wins the GPU lock while a notification is waiting.
    browsing.comments.import_items(
        BV,
        [
            {
                "comment_id": "502",
                "thread_id": "501",
                "platform_user_id": "300",
                "content": "大肥鱼你怎么看盐水里的鸡蛋？",
            }
        ],
    )
    comment = browsing.store.one("SELECT id FROM comments")["id"]
    config = BrowsingPolicy(
        mentions_enabled=True, mention_reply_enabled=True, reply_enabled=True
    ).model_dump()
    browsing.store.set("browsing_policy", config)
    await browsing.preflight()
    await browsing.sync_notifications()
    browsing.adapter.feed["mention"] = [
        {
            "event_id": "duplicate",
            "kind": "mention",
            "resource_type": "video",
            "account_id": "100",
            "sender_uid": "300",
            "comment_id": "502",
            "timestamp": time.time(),
        }
    ]
    await browsing.runtime.generation_lock.acquire()
    response = await browsing.run(preview=True, notifications_only=True)
    try:
        for _ in range(100):
            if browsing.store.one("SELECT id FROM browsing_events WHERE status='context_ready'"):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("notification did not reach waiting generation")
        browsing.store.execute(
            "INSERT INTO drafts(comment_id,text,status,character_id,character_version) VALUES(?,?,'pending_review',?,?)",
            (comment, "人工候选", browsing.runtime.character["id"], browsing.runtime.character["version"]),
        )
    finally:
        browsing.runtime.generation_lock.release()
    await browsing.active[response["id"]]
    assert not browsing.intents() and not browsing.runtime.llm.calls


async def test_reread_really_refreshes_raw_material_but_keeps_rejection(browsing):
    await finished(browsing, preview=True)
    browsing.review(browsing.intents()[0]["id"], "reject")
    before = len([c for c in browsing.adapter.calls if c[0] == "metadata"])
    response = await browsing.reread(browsing.tasks()[0]["id"])
    await browsing.active[response["id"]]
    assert len([c for c in browsing.adapter.calls if c[0] == "metadata"]) == before + 1
    assert len(browsing.intents()) == 1 and browsing.intents()[0]["status"] == "rejected"
