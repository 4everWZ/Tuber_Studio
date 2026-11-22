"""End-to-end Stage 2 Dual-Pipeline tests: Discovery (Pipeline A) vs Inbox (Pipeline B)."""

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support.operations_helpers import (
    BV,
    DMDelivery,
    dm_finished,
    finished,
    incoming,
)
from tests.support.operations_helpers import (
    browsing as browsing,
)
from tuber_studio.core.errors import UnknownPublication
from tuber_studio.core.store import dump
from tuber_studio.infra.platforms.bilibili.client import (
    BilibiliAccountExpired,
    PlatformReadError,
    PublishUncertain,
)
from tuber_studio.operations.activity import activity
from tuber_studio.operations.api import register_browsing_api
from tuber_studio.operations.inbox.comment_events import reuse_or_prepare_light_evidence
from tuber_studio.operations.kernel.control import OperationsControl
from tuber_studio.operations.kernel.repository import BrowsingRepository
from tuber_studio.operations.kernel.sends import ReconciliationPending
from tuber_studio.operations.service import OperationsService


async def _setup_dual_service(browsing_service, *, discovery=True, inbox=True):
    browsing_service.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing_service.configure(
        {
            "sources": ["specified"],
            "watch_videos": [BV],
            "discovery_enabled": discovery,
            "inbox_enabled": inbox,
            "top_level_enabled": discovery,
            "top_level_auto_publish": False,
            "reply_enabled": True,
            "reply_auto_publish": False,
            "mentions_enabled": inbox,
            "mention_reply_enabled": inbox,
            "replies_enabled": inbox,
            "recent_sample_limit": 0,
            "max_replies_per_video": 0,
            "recipient_cooldown_seconds": 0,
        }
    )
    await browsing_service.set_enabled(True)


async def test_dual_hit_same_comment_inbox_preempts_discovery_single_model_call_no_duplicate_draft(
    browsing, monkeypatch
):
    await _setup_dual_service(browsing, discovery=True, inbox=True)
    comment_row = {
        "comment_id": "601",
        "thread_id": "601",
        "platform_user_id": "300",
        "display_name": "邻居",
        "content": "加盐之后鸡蛋真的浮起来了吗？",
        "created_at": int(time.time()),
    }
    thread_payload = {
        "account_id": "100",
        "resource_type": "video",
        "target": {"oid": "42", "type": 1, "bvid": BV, "root": "601", "parent": "601"},
        "source": {"comment_id": "601", "author_uid": "300", "text": comment_row["content"]},
        "items": [comment_row],
    }

    async def read_video_thread(target):
        return thread_payload

    async def read_public_thread(event):
        return thread_payload

    monkeypatch.setattr(browsing.adapter, "read_video_thread", read_video_thread, raising=False)
    monkeypatch.setattr(browsing.adapter, "read_public_thread", read_public_thread)
    monkeypatch.setattr(browsing.adapter, "fetch", lambda *a, **k: asyncio.sleep(0, result={"items": [comment_row]}))

    response_calls = []
    original_gen = browsing.runtime.operation_llm.generate_json

    async def counting_generate(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            response_calls.append(schema["title"])
        return await original_gen(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", counting_generate)

    # Case A: Discovery holds uncommitted floor/comment claim -> Inbox arrives and atomically preempts!
    claims = browsing.operations.claims
    floor = claims.acquire_floor_lease(
        account_id="100",
        character_id=browsing.runtime.character["id"],
        resource_type="video",
        resource_id=BV,
        root_id="601",
        owner_pipeline="discovery",
        ttl_seconds=120,
    )
    comment_lease = claims.claim_comment(
        account_id="100",
        character_id=browsing.runtime.character["id"],
        resource_type="video",
        resource_id=BV,
        root_id="601",
        target_comment_id="601",
        owner_pipeline="discovery",
        ttl_seconds=120,
    )
    assert floor["acquired"] and comment_lease["acquired"]

    # Enqueue Inbox event on the same comment/floor.
    event_payload = {
        "event_id": "dual-hit-601",
        "kind": "reply",
        "resource_type": "video",
        "account_id": "100",
        "sender_uid": "300",
        "bvid": BV,
        "oid": "42",
        "root": "601",
        "comment_id": "601",
        "source_text": comment_row["content"],
        "timestamp": time.time(),
    }
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('dual-hit-601','100','reply',?,?,'received',?,NULL,?)",
        ("canonical-601", time.time(), dump(event_payload), time.time()),
    )

    inbox_round = await finished(browsing, automatic=True, interaction_only=True, local_interactions=True)
    assert inbox_round["status"] == "completed", inbox_round.get("error")

    # Discovery's uncommitted lease was preempted/invalidated cleanly.
    assert not claims.verify_lease(floor["claim_key"], floor["lease_token"])
    assert not claims.verify_lease(comment_lease["claim_key"], comment_lease["lease_token"])

    # Now simulate Discovery attempting the exact same video/comment thread while/after Inbox claimed it.
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.effective() | {"autonomous_batch": True}, browsing.operations.policy()["epoch"], False
    )
    # Re-establish active Inbox floor lock to prove Discovery immediately yields before calling model.
    inbox_floor = claims.acquire_floor_lease(
        account_id="100",
        character_id=browsing.runtime.character["id"],
        resource_type="video",
        resource_id=BV,
        root_id="601",
        owner_pipeline="inbox",
        ttl_seconds=120,
    )
    assert inbox_floor["acquired"]
    result = await browsing.decisions._reply_to_video_comment(
        row,
        {"metadata": {"bvid": BV, "aid": "42"}},
        None,
        set(),
        comment_row,
        [comment_row],
        {"failures": 0},
        metadata={"bvid": BV, "aid": "42"},
    )
    assert result is None
    assert len(response_calls) == 1
    intents = browsing.intents()
    assert len(intents) == 1
    assert intents[0]["source_pipeline"] == "inbox"


async def test_direct_quota_reserve_defers_discovery_without_starving_inbox_and_zero_disables(browsing):
    await _setup_dual_service(browsing, discovery=True, inbox=True)
    # 3/hour total limit, reserve 2 slots for direct=1 Inbox/manual -> only 1 non-reserved Discovery send allowed.
    browsing.operations.configure(
        {"max_per_hour": 3, "direct_quota_reserve": 2},
        account_id="100",
        character=browsing.runtime.character,
    )
    scope = browsing.operation_scope()
    epoch = browsing.operations.policy()["epoch"]

    row_disc, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.effective(), epoch, False)
    row_inbox, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.effective() | {"interaction_only": True}, epoch, False
    )
    now = time.time()
    for idx, (round_id, pipeline) in enumerate(
        [(row_disc["id"], "discovery"), (row_disc["id"], "discovery"), (row_inbox["id"], "inbox")],
        start=1,
    ):
        browsing.store.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,"
            "status,text,target,reason,auto_authorized,created_at,updated_at,source_pipeline) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (
                f"intent-{idx}",
                "100",
                browsing.runtime.character["id"],
                browsing.runtime.character["version"],
                f"src-{idx}",
                "reply_public",
                round_id,
                "approved",
                f"正文{idx}",
                dump({"bvid": BV, "parent": str(700 + idx), "root": str(700 + idx), "recipient": "300"}),
                "detail",
                now,
                now,
                pipeline,
            ),
        )

    # First reserve 1 attempt -> 2 slots left (== direct_quota_reserve=2).
    tok = browsing.operations.reserve("100", browsing.runtime.character, "seed-write", "comments")
    browsing.operations.finish(tok, "published", receipt={"rpid": "888"})

    browsing.queue_publication("intent-1", direct=False)
    browsing.queue_publication("intent-3", direct=True)
    check_now = time.time() + 1

    # Discovery direct=0 is blocked by direct_quota_reserve; Inbox direct=1 stays unblocked!
    assert browsing.sends.direct_reserve_block("100", now=check_now) is not None
    await browsing.configure({"direct_quota_reserve": 2})
    disc_item = browsing.store.one("SELECT * FROM operation_send_items WHERE source_id='intent-1'")
    inbox_item = browsing.store.one("SELECT * FROM operation_send_items WHERE source_id='intent-3'")
    assert disc_item["direct"] == 0 and inbox_item["direct"] == 1
    assert any(item["source_id"] == "intent-3" and item["blocked"] is None for item in browsing.sends.ready_heads(scope, check_now)), browsing.store.all("SELECT * FROM operation_send_items")

    # Disabling reserve (direct_quota_reserve=0) unblocks Discovery immediately!
    await browsing.configure({"direct_quota_reserve": 0})
    assert browsing.sends.direct_reserve_block("100", now=check_now) is None
    ready_after = browsing.sends.ready_heads(scope, check_now)
    assert any(item["source_id"] == "intent-1" and item["blocked"] is None for item in ready_after)


async def test_standalone_inbox_24h_all_four_types_no_discovery_batches_no_watch_starts_dm_isolated(
    browsing, monkeypatch
):
    browsing.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure(
        {
            "discovery_enabled": False,
            "inbox_enabled": True,
            "top_level_enabled": False,
            "reply_enabled": True,
            "reply_auto_publish": False,
            "mentions_enabled": True,
            "mention_reply_enabled": True,
            "replies_enabled": True,
            "comment_like_enabled": False,
            "recent_sample_limit": 0,
            "max_replies_per_video": 0,
            "recipient_cooldown_seconds": 0,
        }
    )
    await browsing.messages.configure({"enabled": True, "auto_poll": True, "auto_send": False})
    browsing.started_at = time.time() - 86400
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    await browsing.set_enabled(True)
    browsing.account = "100"
    browsing.ready_signature = browsing.credential_signature()
    browsing.store.set(
        "operations_binding",
        (browsing.store.get("operations_binding") or {}) | {"enabled_at": time.time() - 3600},
    )

    # 1 & 2: comment_reply + mention notifications
    base_ts = time.time() - 120
    monkeypatch.setattr(
        browsing.adapter,
        "notifications",
        lambda kind, cursor=None: asyncio.sleep(
            0,
            result={
                "account_id": "100",
                "items": [
                    {
                        "event_id": f"{kind}-ev-1",
                        "kind": kind,
                        "resource_type": "video",
                        "account_id": "100",
                        "sender_uid": "300" if kind == "reply" else "301",
                        "bvid": BV,
                        "oid": "42",
                        "root": "601" if kind == "reply" else "602",
                        "comment_id": "601" if kind == "reply" else "602",
                        "timestamp": base_ts + (10 if kind == "reply" else 20),
                    }
                ],
                "cursor": None,
                "has_more": False,
            },
        ),
    )

    # 3: own_content_comment (own uploads + own dynamics)
    monkeypatch.setattr(
        browsing.adapter,
        "own_uploads",
        lambda account: asyncio.sleep(0, result=[{"bvid": BV, "aid": "42"}]),
        raising=False,
    )
    monkeypatch.setattr(
        browsing.adapter,
        "fetch",
        lambda bvid, newest=True, max_pages=1: asyncio.sleep(
            0,
            result={
                "items": [
                    {
                        "comment_id": "603",
                        "thread_id": "603",
                        "platform_user_id": "302",
                        "display_name": "自家观众",
                        "content": "主播自己的视频讲得很清楚！",
                        "created_at": int(base_ts + 30),
                    }
                ]
            },
        ),
    )
    monkeypatch.setattr(
        browsing.adapter,
        "read_public_thread",
        lambda event: asyncio.sleep(
            0,
            result={
                "account_id": "100",
                "resource_type": "video",
                "target": {"oid": "42", "type": 1, "bvid": BV, "root": event["root"], "parent": event["comment_id"]},
                "source": {"comment_id": event["comment_id"], "author_uid": event["sender_uid"], "text": "来评正文"},
                "items": [
                    {
                        "comment_id": event["comment_id"],
                        "thread_id": event["root"],
                        "platform_user_id": event["sender_uid"],
                        "content": "来评正文",
                        "created_at": int(base_ts),
                    }
                ],
            },
        ),
    )

    # 4: DM poll isolated mock
    class DMFeed:
        async def sessions(self):
            return ("100", ["300"])

        async def history_page(self, uid, *, before=None):
            if before:
                return ([], 0, 1)
            return (
                [
                    {
                        "receiver_type": 1,
                        "msg_type": 1,
                        "msg_status": 0,
                        "msg_key": "1001",
                        "sender_uid": "300",
                        "receiver_id": "100",
                        "content": json.dumps({"content": "一条严格私密的私信内容 secret-dm-text"}),
                        "timestamp": base_ts + 40,
                        "msg_seqno": 1,
                    }
                ],
                0,
                1,
            )

        async def close(self):
            pass

    browsing.messages.adapter = DMFeed()
    browsing.messages.injected = True
    await browsing.messages.open()

    # Initialize notification watermarks before arrival, then run 24h Inbox ticks while Discovery stays OFF.
    browsing.store.execute("INSERT OR REPLACE INTO browsing_cursors VALUES('100','mention',?,'{}',1)", (base_ts - 60,))
    browsing.store.execute("INSERT OR REPLACE INTO browsing_cursors VALUES('100','reply',?,'{}',1)", (base_ts - 60,))

    for _ in range(3):
        await browsing.inbox_runner.poll_notifications_once()
        await browsing.inbox_runner.process_interactions_once()
        for active in list(browsing.active.values()):
            await active
        # Even if DiscoveryRunner.run_once() is ticked repeatedly over 24h, discovery stays completely idle!
        delay = await browsing.discovery_runner.run_once()
        assert delay >= 1.0

    # No discovery batches, no watch starts, no top_level comments.
    discovery_rounds = browsing.store.all(
        "SELECT * FROM browsing_rounds WHERE COALESCE(json_extract(config,'$.interaction_only'),0)=0"
    )
    assert discovery_rounds == []
    assert browsing.store.all("SELECT * FROM browsing_watch_starts") == []
    intents = browsing.intents()
    assert intents and all(item["kind"] != "top_level" for item in intents)
    assert {item["source_pipeline"] for item in intents} == {"inbox"}

    # Public inbox & public activity MUST NEVER leak DM text, and DM stays isolated by character_id.
    inbox_dump = json.dumps(browsing.inbox(), ensure_ascii=False)
    public_act = json.dumps(activity(browsing, kind="public", paginate=False), ensure_ascii=False)
    assert "secret-dm-text" not in inbox_dump
    assert "secret-dm-text" not in public_act
    dm_rows = browsing.inbox_runner.dm_bridge.list_recent(
        account_id="100", character_id=browsing.runtime.character["id"], platform_user_id="300"
    )
    assert len(dm_rows) == 1 and dm_rows[0].kind == "dm"
    other_char_drafts = browsing.store.all(
        "SELECT * FROM dm_drafts WHERE platform='bilibili' AND character_id='other-character'"
    )
    assert other_char_drafts == []

    # Multi-account DM privacy isolation: another account_id with same (character_id, platform_user_id) must not mix.
    browsing.store.execute(
        "INSERT INTO dm_inbox(platform,message_id,platform_user_id,display_name,content,sent_at,character_id,received_at,account_id) "
        "VALUES('bilibili','other-acct-1','300','好友','secret-other-account',?,?,?,'999')",
        (base_ts + 45, browsing.runtime.character["id"], base_ts + 45),
    )
    acct100 = browsing.inbox_runner.dm_bridge.list_recent(
        account_id="100", character_id=browsing.runtime.character["id"], platform_user_id="300"
    )
    acct999 = browsing.inbox_runner.dm_bridge.list_recent(
        account_id="999", character_id=browsing.runtime.character["id"], platform_user_id="300"
    )
    assert len(acct100) == 1 and "secret-other-account" not in acct100[0].text
    assert len(acct999) == 1 and acct999[0].text == "secret-other-account"


async def test_412_rate_limit_backoff_pauses_discovery_first_while_inbox_stays_alive(browsing, monkeypatch):
    await _setup_dual_service(browsing, discovery=True, inbox=True)
    browsing.account = "100"
    browsing.ready_signature = browsing.credential_signature()

    async def restricted_discover(config, page=1):
        raise PlatformReadError("restricted", "HTTP 412 风控拦截", http_status=412)

    notif_calls = []

    async def healthy_notifications(kind, cursor=None):
        notif_calls.append(kind)
        return {"account_id": "100", "items": [], "cursor": None, "has_more": False}

    monkeypatch.setattr(browsing.adapter, "discover", restricted_discover)
    monkeypatch.setattr(browsing.adapter, "notifications", healthy_notifications)

    # Trigger 412 inside Discovery proactive read -> read_gate pauses discovery FIRST.
    try:
        await browsing._read_proactively(browsing._read, browsing.adapter.discover, browsing.effective(), 1)
    except PlatformReadError:
        pass

    assert browsing.read_gate.allows("discovery", account_id="100") is False
    assert browsing.read_gate.allows("inbox", account_id="100") is True
    assert browsing.read_gate.allows("discovery", account_id="100", reconciliation=True) is True
    assert browsing.status()["discovery"]["read_cooldown_until"] is not None

    # DiscoveryRunner pauses without spawning a new discovery round, while InboxRunner keeps polling!
    wait = await browsing.discovery_runner.run_once()
    assert wait >= 0.5
    assert browsing.discovery.attention["type"] == "read_cooldown"
    assert browsing.store.all("SELECT * FROM browsing_rounds") == []

    await browsing.inbox_runner.poll_notifications_once()
    assert notif_calls == ["mention", "reply"]


async def test_same_bv_evidence_single_flight_across_discovery_and_inbox(browsing, monkeypatch):
    await _setup_dual_service(browsing, discovery=True, inbox=True)
    subtitle_reads = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_subtitles(bvid, cid, languages):
        nonlocal subtitle_reads
        subtitle_reads += 1
        entered.set()
        await release.wait()
        return {
            "bvid": bvid,
            "cid": cid,
            "track_id": "1",
            "language": "zh",
            "is_ai": False,
            "segments": [{"id": f"{cid}:1:0", "start": 1, "end": 4, "text": "同一 BV 共用证据"}],
            "coverage": {"complete": True, "start": 1, "end": 4, "segment_count": 1},
            "fetched_at": time.time(),
        }

    monkeypatch.setattr(browsing.adapter, "subtitles", gated_subtitles)
    row_inbox, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.effective() | {"interaction_only": True}, 0, True
    )
    cfg = browsing.effective() | {"visual_enabled": False}

    task_discovery = asyncio.create_task(browsing.evidence.prepare(BV, cfg))
    await asyncio.wait_for(entered.wait(), 3)
    task_inbox = asyncio.create_task(
        reuse_or_prepare_light_evidence(browsing, row_inbox, BV, wait_inflight_only=False)
    )
    await asyncio.sleep(0.02)
    release.set()

    res_disc, res_inbox = await asyncio.gather(task_discovery, task_inbox)
    assert res_disc[0] == res_inbox[0]
    assert subtitle_reads == 1
    assert browsing.evidence.merged >= 1
    assert browsing.store.all("SELECT * FROM browsing_watch_starts") == []


async def test_emergency_stop_persists_across_restart_and_auth_loss_and_char_switch_halt_both(browsing, monkeypatch):
    await _setup_dual_service(browsing, discovery=True, inbox=True)
    await browsing.stop(emergency=True)
    assert browsing.operations.policy()["emergency"] is True

    # Simulate restart with fresh OperationsControl + OperationsService against same Store.
    restarted_ops = OperationsControl(browsing.store)
    browsing.runtime.operations = restarted_ops
    restarted = OperationsService(
        browsing.settings,
        browsing.store,
        browsing.runtime,
        browsing.comments,
        browsing.messages,
        browsing.dynamics,
    )
    assert restarted.operations.policy()["emergency"] is True
    assert restarted.control_state() == "emergency"
    for attempt in (restarted.set_enabled(True), restarted.set_discovery_enabled(True), restarted.set_inbox_enabled(True)):
        try:
            await attempt
            raise AssertionError("Emergency stop must block restart until explicitly reset")
        except ValueError as err:
            assert "紧急停止" in str(err)

    restarted.operations.configure({"emergency": False})
    await _setup_dual_service(restarted, discovery=True, inbox=True)

    # 401 / BilibiliAccountExpired stops BOTH Discovery & Inbox cleanly.
    async def expired_notifications(*args):
        raise BilibiliAccountExpired("401 unauthorized")

    monkeypatch.setattr(restarted.adapter, "notifications", expired_notifications)
    await restarted.inbox_runner.poll_notifications_once()
    assert restarted.ready_signature is None
    assert restarted.operations.policy()["enabled"] is False
    assert restarted.discovery.enabled is False and restarted.inbox_enabled() is False

    # Mid-round character switch halts stale round cleanly via _check_round.
    async def healthy_notifications(kind, cursor=None):
        return {"account_id": "100", "items": [], "cursor": None, "has_more": False}

    monkeypatch.setattr(restarted.adapter, "notifications", healthy_notifications)
    restarted.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await _setup_dual_service(restarted, discovery=True, inbox=True)
    stale_round, _ = restarted.repo.create_round("100", restarted.runtime.character, restarted.effective(), 1, False)
    restarted.store.save_character(restarted.runtime.character)
    try:
        restarted._check_round(stale_round)
        raise AssertionError("Character version change must halt stale round")
    except ValueError as err:
        assert "角色版本" in str(err)


async def test_publish_uncertain_freezes_writes_across_both_pipelines(browsing, monkeypatch):
    await _setup_dual_service(browsing, discovery=True, inbox=True)
    await browsing.configure(
        {
            "sources": ["recommend"],
            "watch_videos": [],
            "top_level_enabled": True,
            "top_level_auto_publish": True,
            "candidate_limit": 4,
        }
    )
    browsing.operations.configure(
        {"max_per_hour": 0, "max_per_day": 0},
        account_id="100",
        character=browsing.runtime.character,
    )
    await browsing.sends.close()
    monkeypatch.setattr(browsing.sends, "start", lambda: None)
    await browsing.messages.configure({"enabled": True, "auto_send": True, "auto_poll": True})
    epoch = browsing.operations.policy()["epoch"]

    first, second = BV, "BV1xx411c7mD"
    publish_writes = []
    reconcile_reads = []
    discover_calls = []
    notif_calls = []
    reconcile_visible = False

    async def discover(*args):
        discover_calls.append(args)
        return {"items": [{"bvid": first}, {"bvid": second}], "has_more": False}

    async def publish_top_level(target, text, *, before_send):
        before_send()
        publish_writes.append(target["bvid"])
        if target["bvid"] == first:
            raise PublishUncertain("发送响应超时")
        return {"code": 0, "rpid": "902"}

    async def reconcile_browsing(target, **kwargs):
        reconcile_reads.append(("browsing", target.get("oid")))
        if reconcile_visible:
            return {"status": "visible", "receipt": {"code": 0, "rpid": "9010"}}
        return {"status": "not_found"}

    async def notifications(kind, cursor=None):
        notif_calls.append(kind)
        return {"account_id": "100", "items": [], "cursor": None, "has_more": False}

    class UncertainDM(DMDelivery):
        def __init__(self):
            super().__init__()
            self.uncertain = True

        async def self_uid(self):
            return "100"

        async def sessions(self):
            return ("100", ["300"])

        async def history_page(self, uid, *, before=None):
            return ([], 0, 1)

        async def reconcile_publication(self, target, **kwargs):
            reconcile_reads.append(("messages", target.get("platform_user_id")))
            if reconcile_visible:
                return {"status": "visible", "receipt": {"msg_key": "dm-reconciled-901"}}
            return {"status": "not_found"}

    dm_adapter = UncertainDM()
    browsing.messages.adapter = dm_adapter
    browsing.messages.injected = True
    await browsing.messages.open()

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.adapter, "publish_top_level", publish_top_level)
    monkeypatch.setattr(browsing.adapter, "reconcile_publication", reconcile_browsing, raising=False)
    monkeypatch.setattr(browsing.adapter, "notifications", notifications)

    # Produce two real Discovery top-level intents, then drive `first` into `publish_uncertain`.
    round_res = await finished(browsing, automatic=True, video_only=True)
    assert round_res["status"] == "completed"
    intents = {item["target"]["bvid"]: item for item in browsing.intents()}
    assert first in intents and second in intents
    first_id, second_id = intents[first]["id"], intents[second]["id"]
    browsing.queue_publication(first_id, direct=False)
    browsing.queue_publication(second_id, direct=False)

    q_first = browsing.store.one("SELECT * FROM operation_send_items WHERE channel='browsing' AND source_id=?", (first_id,))
    q_second = browsing.store.one("SELECT * FROM operation_send_items WHERE channel='browsing' AND source_id=?", (second_id,))
    with pytest.raises(ReconciliationPending):
        await browsing._send_automatic(q_first)
    assert browsing.public_comment_intent(first_id)["status"] == "publish_uncertain"
    assert publish_writes == [first]

    # Create a real uncertain DM draft too.
    dm_source = incoming(browsing.messages, account_id="100", platform_user_id="300")
    dm_id = browsing.messages.enqueue("message", dm_source, automatic=True)
    await dm_finished(browsing.messages, dm_id)
    browsing.messages.authorizations[dm_id] = epoch
    with pytest.raises(UnknownPublication):
        await browsing.messages.send(dm_id, automatic=True, authorization_epoch=epoch)
    assert browsing.store.one("SELECT status FROM dm_drafts WHERE id=?", (dm_id,))["status"] == "publish_uncertain"
    assert len(dm_adapter.sent) == 1
    if not browsing.store.one("SELECT 1 FROM operation_send_items WHERE channel='messages' AND source_id=?", (str(dm_id),)):
        browsing.sends.enqueue("messages", str(dm_id), direct=True)
    q_dm = browsing.store.one("SELECT * FROM operation_send_items WHERE channel='messages' AND source_id=?", (str(dm_id),))

    # Global freeze signals & review edit/approve lock on uncertain items.
    assert browsing.has_uncertain_writes() is True
    status = browsing.status()
    assert status["frozen_reason"] == "reconciliation_pending"
    assert status["inbox"]["state"] == "frozen"
    with pytest.raises(ValueError):
        browsing.publication.review(first_id, "approve")
    with pytest.raises(ValueError):
        browsing.messages.review(dm_id, "approve")

    # Stage 2 runners freeze new discovery/inbox polling while uncertain reconciliation is unresolved.
    discover_calls.clear()
    notif_calls.clear()
    assert await browsing.discovery_runner.run_once() <= 10.0
    assert await browsing.inbox_runner.poll_notifications_once() <= 10.0
    assert await browsing.inbox_runner.process_interactions_once() <= 10.0
    assert discover_calls == [] and notif_calls == []

    # Queue dispatch on BOTH uncertain items only performs read-only reconciliation (zero new writes).
    writes_before, dm_sent_before, reads_before = len(publish_writes), len(dm_adapter.sent), len(reconcile_reads)
    with pytest.raises(ReconciliationPending):
        await browsing._send_automatic(q_first)
    with pytest.raises(ReconciliationPending):
        await browsing._send_automatic(q_dm)
    assert len(publish_writes) == writes_before and len(dm_adapter.sent) == dm_sent_before
    assert len(reconcile_reads) == reads_before + 2

    # Unrelated normal queued item continues flowing cleanly without being blocked.
    await browsing._send_automatic(q_second)
    assert browsing.public_comment_intent(second_id)["status"] == "published"
    assert publish_writes == [first, second]
    assert browsing.public_comment_intent(first_id)["status"] == "publish_uncertain"

    # Once read-only reconciliation confirms platform visibility, both settle and unfreeze the pipelines.
    reconcile_visible = True
    await browsing._send_automatic(q_first)
    await browsing._send_automatic(q_dm)
    assert browsing.public_comment_intent(first_id)["status"] == "published"
    assert browsing.store.one("SELECT status FROM dm_drafts WHERE id=?", (dm_id,))["status"] == "published"
    assert browsing.has_uncertain_writes() is False
    assert browsing.status()["frozen_reason"] is None


async def test_lossless_legacy_v2_upgrade_and_stage2_api_controls(store, settings, runtime, browsing):
    # Inject legacy pre-v2 policies (no discovery_enabled/inbox_enabled keys, zero limits, filters).
    store.set(
        "browsing_policy",
        {
            "mentions_enabled": True,
            "mention_reply_enabled": True,
            "replies_enabled": True,
            "top_level_enabled": True,
            "daily_watch_limit": 0,
            "max_replies_per_video": 0,
            "exclude_words": ["剧透"],
            "business_timezone": "Asia/Shanghai",
        },
    )
    store.set("operations_policy", {"enabled": True, "emergency": False})
    store.set("operation_policy_epoch", 4)
    store.set("operations_pipeline_schema_version", 1)

    upgraded = OperationsControl(store)
    repo = BrowsingRepository(store)
    svc = OperationsService(settings, store, runtime, browsing.comments, browsing.messages, browsing.dynamics)
    policy = svc.policy()
    assert upgraded.policy()["enabled"] is False
    assert policy["discovery_enabled"] is False
    assert policy["inbox_enabled"] is False
    assert policy["mentions_enabled"] is True
    assert policy["top_level_enabled"] is True
    assert policy["daily_watch_limit"] == 0
    assert policy["max_replies_per_video"] == 0
    assert policy["exclude_words"] == ["剧透"]
    assert policy["business_timezone"] == "Asia/Shanghai"
    assert store.get("operations_pipeline_schema_version") == 2

    # Verify Stage 2 FastAPI endpoints cleanly.
    svc.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    app = FastAPI()
    register_browsing_api(app, svc, lambda: True)
    client = TestClient(app)

    res = client.put("/api/automation/settings", json={"direct_quota_reserve": 4}).raise_for_status().json()
    assert res["policy"]["direct_quota_reserve"] == 4

    claims_res = client.get("/api/automation/claims").raise_for_status().json()
    assert claims_res == {"items": []}

    act_inbox = client.get("/api/automation/activity", params={"source": "inbox"}).raise_for_status().json()
    assert "items" in act_inbox and act_inbox["source"] == "inbox"

    repo.close() if hasattr(repo, "close") else None


async def test_discovery_revisits_run_inside_discovery_runner_when_inbox_disabled(browsing, monkeypatch):
    await _setup_dual_service(browsing, discovery=True, inbox=False)
    await browsing.configure({"top_level_enabled": False, "reply_enabled": True, "reply_auto_publish": False})

    fetch_pages = []

    async def discover(*args):
        return {"items": [], "has_more": False}

    async def fetch(bvid, *args, newest=False, start_page=1):
        fetch_pages.append((bvid, start_page))
        return {
            "video_id": bvid,
            "aid": "42",
            "owner_uid": "200",
            "items": [
                {
                    "comment_id": "701",
                    "thread_id": "701",
                    "root_id": "701",
                    "parent_id": "0",
                    "platform_user_id": "300",
                    "display_name": "路人观众",
                    "content": "这个实验参数是多少？",
                    "created_at": time.time() - 5,
                }
            ]
            if start_page == 2
            else [],
            "next_page": None,
        }

    async def read_video_thread(target):
        return {
            "account_id": "100",
            "resource_type": "video",
            "target": {"oid": "42", "type": 1, "bvid": BV, "root": "701", "parent": "701"},
            "source": {"comment_id": "701", "author_uid": "300", "author_name": "路人观众", "text": "这个实验参数是多少？"},
            "items": [
                {
                    "comment_id": "701",
                    "thread_id": "701",
                    "platform_user_id": "300",
                    "display_name": "路人观众",
                    "content": "这个实验参数是多少？",
                    "created_at": int(time.time() - 5),
                }
            ],
        }

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_video_thread", read_video_thread, raising=False)

    _, material = await browsing.evidence.prepare(BV, browsing.effective() | {"visual_enabled": False})
    browsing.repo.save_watch_evidence(
        {"account_id": "100", "character_id": browsing.runtime.character["id"]},
        BV,
        {"material": material},
    )
    browsing.store.execute(
        "INSERT INTO browsing_comment_revisits VALUES('100',?,?,?,?,'prior-round',2,?,NULL)",
        (browsing.runtime.character["id"], browsing.runtime.character["version"], "", BV, time.time() - 5),
    )

    assert browsing.inbox_enabled() is False
    await browsing.discovery_runner.run_once()
    for active in list(browsing.active.values()):
        await active

    assert (BV, 2) in fetch_pages
    intents = browsing.intents()
    events = browsing.store.all("SELECT id,kind,status,error FROM browsing_events")
    assert any(item["target"].get("parent") == "701" and item["source_pipeline"] == "discovery" for item in intents), (
        intents,
        events,
        browsing.store.all("SELECT id,state,feedback FROM operation_continuations"),
    )

    # Conversely, InboxRunner.process_interactions_once MUST NOT pick up `comment_visit` events.
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('cv-ignored','100','comment_visit','cv-ignored',?,'received',?,NULL,?)",
        (
            time.time(),
            dump(
                {
                    "event_id": "cv-ignored",
                    "kind": "comment_visit",
                    "resource_type": "video",
                    "account_id": "100",
                    "character_id": browsing.runtime.character["id"],
                    "bvid": BV,
                    "oid": "42",
                    "root": "702",
                    "comment_id": "702",
                    "sender_uid": "300",
                    "source_text": "留给Discovery处理",
                }
            ),
            time.time(),
        ),
    )
    await browsing.configure({"discovery_enabled": False, "inbox_enabled": True})
    await browsing.inbox_runner.process_interactions_once()
    assert browsing.store.one("SELECT status FROM browsing_events WHERE id='cv-ignored'")["status"] == "received"


async def test_set_discovery_enabled_while_inbox_active_runs_preflight_and_rolls_back_on_blocker(browsing, monkeypatch):
    await _setup_dual_service(browsing, discovery=False, inbox=True)
    assert browsing.operations.policy()["enabled"] is True
    assert browsing.inbox_enabled() is True
    assert browsing.discovery.enabled is False

    original_discover = browsing.adapter.discover
    monkeypatch.setattr(browsing.adapter, "discover", None)
    with pytest.raises(ValueError, match="discover"):
        await browsing.set_discovery_enabled(True)
    assert browsing.configured()["discovery_enabled"] is False
    assert browsing.discovery.enabled is False
    assert browsing.inbox_enabled() is True

    monkeypatch.setattr(browsing.adapter, "discover", original_discover)
    status = await browsing.set_discovery_enabled(True)
    await browsing.discovery_runner.stop()
    assert status["discovery"]["enabled"] is True
    assert status["inbox"]["enabled"] is True
