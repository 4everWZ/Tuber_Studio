"""Unit tests for Stage 2 operations kernel primitives and lossless pipeline v2 migration."""

import asyncio
import concurrent.futures
import json
import time

import pytest

from tuber_studio.core.errors import SendLimitReached
from tuber_studio.core.retention import RetentionManager
from tuber_studio.core.store import Store
from tuber_studio.infra.platforms.bilibili.account import BilibiliAccount
from tuber_studio.infra.platforms.bilibili.client import PlatformReadError
from tuber_studio.operations.browsing.repository import BrowsingRepository
from tuber_studio.operations.channels.comments import Comments
from tuber_studio.operations.channels.direct_messages import DirectMessages
from tuber_studio.operations.channels.dynamics import Dynamics
from tuber_studio.operations.kernel.claims import OperationClaims, comment_claim_key, floor_claim_key
from tuber_studio.operations.kernel.control import (
    AUTOMATIC_SWITCHES,
    PIPELINE_SCHEMA_VERSION,
    OperationsControl,
    canonical_intent,
    migrate_operations_pipeline_v2,
)
from tuber_studio.operations.kernel.models import BrowsingPolicy
from tuber_studio.operations.kernel.read_gate import PlatformReadGate
from tuber_studio.operations.kernel.sends import OperationSends


def _intent(character_id: str, suffix: str) -> str:
    return canonical_intent("bilibili", "BV1d8pU6xEZX", suffix, character_id)


def test_operation_claims_atomic_comment_and_floor_preemption(store, settings):
    character = store.character()
    claims_a = OperationClaims(store)
    second_store = Store(settings.runtime / "test.sqlite3", [character])
    claims_b = OperationClaims(second_store)
    now = 1_700_000_000.0

    # Concurrent comment claims across separate SQLite connections admit strictly one worker.
    def _try_claim(args):
        mgr, pipeline = args
        return mgr.claim_comment(
            account_id="100",
            character_id=character["id"],
            resource_type="video",
            resource_id="BV1d8pU6xEZX",
            root_id="501",
            target_comment_id="502",
            owner_pipeline=pipeline,
            ttl_seconds=120.0,
            now=now,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_try_claim, [(claims_a, "manual"), (claims_b, "manual")]))

    winners = [res for res in results if res["acquired"]]
    losers = [res for res in results if not res["acquired"]]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0]["reason"] == "already_claimed"

    winner = winners[0]
    assert claims_a.verify_lease(winner["claim_key"], winner["lease_token"], now=now + 10)
    # Wrong lease token cannot release an active claim.
    assert not claims_b.release(winner["claim_key"], lease_token="wrong-token")
    assert claims_a.release(winner["claim_key"], lease_token=winner["lease_token"])
    assert not claims_a.verify_lease(winner["claim_key"], winner["lease_token"], now=now + 10)

    # Discovery acquires floor lease first; Inbox preempts Discovery cleanly.
    disc = claims_a.acquire_floor_lease(
        account_id="100",
        character_id=character["id"],
        resource_type="video",
        resource_id="BV1d8pU6xEZX",
        root_id="501",
        owner_pipeline="discovery",
        ttl_seconds=180.0,
        now=now,
    )
    assert disc["acquired"] is True
    assert claims_a.verify_lease(disc["claim_key"], disc["lease_token"], now=now + 5)

    inbox = claims_b.acquire_floor_lease(
        account_id="100",
        character_id=character["id"],
        resource_type="video",
        resource_id="BV1d8pU6xEZX",
        root_id="501",
        owner_pipeline="inbox",
        ttl_seconds=180.0,
        now=now + 1,
    )
    assert inbox["acquired"] is True
    assert inbox["preempted_pipeline"] == "discovery"
    assert inbox["preempted_token"] == disc["lease_token"]

    # Preempted Discovery token is now invalid and cannot renew; new Discovery claims yield to Inbox.
    assert not claims_a.verify_lease(disc["claim_key"], disc["lease_token"], now=now + 2)
    assert not claims_a.renew(disc["claim_key"], disc["lease_token"], ttl_seconds=60.0, now=now + 2)

    blocked_disc_comment = claims_a.claim_comment(
        account_id="100",
        character_id=character["id"],
        resource_type="video",
        resource_id="BV1d8pU6xEZX",
        root_id="501",
        target_comment_id="509",
        owner_pipeline="discovery",
        ttl_seconds=60.0,
        now=now + 2,
    )
    assert not blocked_disc_comment["acquired"]
    assert blocked_disc_comment["reason"] == "inbox_floor_active"

    # Releasing Inbox lease unblocks Discovery acquisition again.
    assert claims_b.release(inbox["claim_key"], lease_token=inbox["lease_token"])
    resumed_disc = claims_a.acquire_floor_lease(
        account_id="100",
        character_id=character["id"],
        resource_type="video",
        resource_id="BV1d8pU6xEZX",
        root_id="501",
        owner_pipeline="discovery",
        ttl_seconds=60.0,
        now=now + 3,
    )
    assert resumed_disc["acquired"] is True


def test_operation_claims_cold_start_cleanup_and_retention_preservation(settings, store, runtime):
    character = store.character()
    OperationClaims(store)
    past = time.time() - 600
    expired_floor = floor_claim_key("100", character["id"], "video", "BV1d8pU6xEZX", "777")
    active_comment = comment_claim_key("100", character["id"], "video", "BV1d8pU6xEZX", "888")

    with store.db:
        store.db.execute(
            "INSERT INTO operation_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                expired_floor,
                "floor",
                "100",
                character["id"],
                "video",
                "BV1d8pU6xEZX",
                "777",
                None,
                "discovery",
                "stale-tok",
                past,
                past - 10,
            ),
        )
        store.db.execute(
            "INSERT INTO operation_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                active_comment,
                "comment",
                "100",
                character["id"],
                "video",
                "BV1d8pU6xEZX",
                "777",
                "888",
                "inbox",
                "live-tok",
                time.time() + 600,
                time.time(),
            ),
        )

    # Cold-start re-initialization removes expired/stale leases while retaining live ones.
    restarted = OperationClaims(store)
    rows = restarted.list_claims()
    assert [row["claim_key"] for row in rows] == [active_comment]
    assert "operation_claims" in RetentionManager(settings, store).preserved()


async def test_direct_quota_reserve_protects_direct_and_respects_zero_unlimited(settings, store, runtime, monkeypatch):
    clock = [1_700_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    BrowsingRepository(store)
    account = BilibiliAccount(settings, store)
    comments = Comments(settings, store, runtime, account)
    DirectMessages(settings, store, runtime, comments)
    Dynamics(settings, store, runtime, comments)
    control = runtime.operations
    character = store.character()
    assert control.policy()["direct_quota_reserve"] == 2
    assert "message_poll_enabled" not in AUTOMATIC_SWITCHES["browsing"]
    assert "message_reply_enabled" not in AUTOMATIC_SWITCHES["browsing"]
    assert {"discovery_enabled", "inbox_enabled"} <= AUTOMATIC_SWITCHES["browsing"]

    control.configure({"enabled": True, "max_per_hour": 4, "max_per_day": 10, "direct_quota_reserve": 2},
                      account_id="100", character=character)
    dispatched = []

    async def dispatch(item):
        dispatched.append((item["channel"], item["source_id"], item["direct"]))
        token = control.reserve(
            "100", character, _intent(character["id"], f"{item['channel']}:{item['source_id']}"),
            "comments" if item["channel"] == "browsing" else item["channel"],
            automatic=True,
        )
        control.finish(token, "published")
        return True

    sends = OperationSends(
        store,
        control,
        lambda: ("100", character["id"], character["version"]),
        dispatch,
        waiting_for_image=lambda head: False,
    )

    # First 2 non-direct writes consume quota down to remaining == 2 == direct_quota_reserve.
    t1 = control.reserve("100", character, _intent(character["id"], "seed-1"), "comments", automatic=True)
    control.finish(t1, "published")
    clock[0] += 10
    t2 = control.reserve("100", character, _intent(character["id"], "seed-2"), "comments", automatic=True)
    control.finish(t2, "published")
    clock[0] += 10

    # At remaining = 2 <= reserve(2), non-direct preparation blocks while direct preparation succeeds.
    blocked = sends.preparation_block("100", "dynamics", {}, retry_seconds=60, direct=False)
    assert isinstance(blocked, SendLimitReached)
    assert blocked.next_at == pytest.approx(1_700_000_000.0 + 3600)
    assert sends.preparation_block("100", "messages", {}, retry_seconds=60, direct=True) is None
    assert sends.preparation_block("100", "comments", {}, retry_seconds=60, direct=True) is None

    # Reaction channels remain unaffected by reserve.
    assert sends.preparation_block("100", "comment_likes", {}, retry_seconds=60, direct=False) is None

    # In send queue: direct=0 is deferred while direct=1 proceeds on reserved capacity.
    sends.enqueue("browsing", "disc-1", direct=False)
    sends.enqueue("messages", "dm-1", direct=True)
    for _ in range(40):
        await asyncio.sleep(0.02)
        if dispatched:
            break
    await sends.close()

    assert dispatched == [("messages", "dm-1", 1)]
    deferred = store.one("SELECT * FROM operation_send_queue WHERE channel='browsing' AND source_id='disc-1'")
    assert deferred is not None and deferred["wake_at"] > clock[0]

    # Setting direct_quota_reserve=0 disables reservation and unblocks direct=0 until remaining=0.
    control.configure({"direct_quota_reserve": 0})
    assert sends.preparation_block("100", "dynamics", {}, retry_seconds=60, direct=False) is None

    # Also enforce rolling 24h reserve separately from hourly limits.
    control.configure({"max_per_hour": 0, "max_per_day": 3, "direct_quota_reserve": 1})
    blocked_24h = sends.preparation_block("100", "comments", {}, retry_seconds=60, direct=False)
    assert isinstance(blocked_24h, SendLimitReached)
    assert blocked_24h.next_at == pytest.approx(1_700_000_010.0 + 86400)


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: PlatformReadError.restricted(412),
        lambda: PlatformReadError.restricted(429),
        lambda: PlatformReadError("restricted", "API -352 风控限制"),
        lambda: PlatformReadError("restricted", "API -412 请求拦截"),
        lambda: PlatformReadError("restricted", "API -509 请求过于频繁"),
    ],
)
async def test_platform_read_gate_pauses_discovery_first_and_keeps_inbox_and_reconcile_alive(
    monkeypatch, make_error
):
    now = [10_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    gate = PlatformReadGate(read_interval=0, discovery_base_cooldown=60.0, discovery_max_cooldown=600.0)

    async def restricted_read():
        raise make_error()

    # Discovery hit by 412/429/-352/-412/-509 raises once and arms Discovery cooldown.
    with pytest.raises(PlatformReadError):
        await gate.read_proactively(gate.read, restricted_read, retries=0, account_id="100")

    assert not gate.allows("discovery", account_id="100", now=now[0])
    assert gate.allows("inbox", account_id="100", now=now[0])
    assert gate.allows("discovery", account_id="100", reconciliation=True, now=now[0])

    async def healthy_read(label):
        return f"ok:{label}"

    # Subsequent Discovery read is blocked with SendLimitReached wakeup.
    with pytest.raises(SendLimitReached) as paused:
        await gate.read_proactively(gate.read, healthy_read, "discovery", retries=0, account_id="100")
    assert paused.value.next_at == pytest.approx(now[0] + 60.0)

    # Inbox polling and read-only publish_uncertain reconciliation remain allowed immediately.
    assert await gate.read(healthy_read, "inbox", pipeline="inbox", account_id="100") == "ok:inbox"
    assert (
        await gate.read(healthy_read, "reconcile", pipeline="discovery", account_id="100", reconciliation=True)
        == "ok:reconcile"
    )

    # Even if an Inbox read hits a throttle error, Discovery backoff widens while Inbox stays unblocked.
    with pytest.raises(PlatformReadError):
        await gate.read(restricted_read, pipeline="inbox", account_id="100")
    assert gate.discovery_backoff("100", now=now[0])["cooldown_seconds"] == pytest.approx(120.0)
    assert gate.allows("inbox", account_id="100", now=now[0])

    # After cooldown expires and a Discovery read succeeds, strikes clear cleanly.
    now[0] += 121.0
    assert await gate.read_proactively(gate.read, healthy_read, "recovered", retries=0, account_id="100") == "ok:recovered"
    assert gate.discovery_backoff("100", now=now[0]) is None


def test_lossless_v2_migration_preserves_quotas_zero_filters_and_forces_safe_toggles(store):
    legacy_ops = {
        "enabled": True,
        "emergency": False,
        "max_per_hour": 0,
        "max_per_day": 0,
        "recipient_cooldown_seconds": 450,
        "max_recoveries": 12,
    }
    legacy_browsing = {
        "sources": ["search", "specified"],
        "keywords": ["深度学习"],
        "watch_videos": ["BV1d8pU6xEZX"],
        "autonomous_topics": True,
        "discovery_enabled": True,
        "top_level_enabled": True,
        "top_level_auto_publish": False,
        "reply_enabled": True,
        "reply_auto_publish": True,
        "daily_watch_limit": 0,
        "max_replies_per_video": 0,
        "max_per_hour": 0,
        "max_per_day": 0,
        "business_timezone": "Asia/Tokyo",
        "mentions_enabled": True,
        "replies_enabled": True,
        "exclude_words": ["广告", "抽奖"],
        "allowed_author_uids": ["12345"],
    }
    store.set("operations_policy", legacy_ops)
    store.set("browsing_policy", legacy_browsing)
    store.execute("DELETE FROM kv WHERE key='operations_pipeline_schema_version'")

    # Seed pre-existing browsing tables without source_pipeline column.
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS browsing_rounds(
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, character_id TEXT NOT NULL,
            character_version INTEGER NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS browsing_intents(
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, character_id TEXT NOT NULL,
            source_key TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL
        );
        INSERT INTO browsing_rounds VALUES('r1','100','whale',1,'completed','{}');
        INSERT INTO browsing_intents VALUES('i1','100','whale','src-1','top_level','published');
        """
    )

    control = OperationsControl(store)
    assert store.get("operations_pipeline_schema_version") == PIPELINE_SCHEMA_VERSION

    migrated_ops = control.policy()
    assert migrated_ops["enabled"] is False
    assert migrated_ops["max_per_hour"] == 0
    assert migrated_ops["max_per_day"] == 0
    assert migrated_ops["recipient_cooldown_seconds"] == 450
    assert migrated_ops["max_recoveries"] == 12
    assert migrated_ops["direct_quota_reserve"] == 2

    migrated_browsing = BrowsingPolicy.model_validate(store.get("browsing_policy")).model_dump()
    assert migrated_browsing["discovery_enabled"] is False
    assert migrated_browsing["inbox_enabled"] is False
    assert migrated_browsing["daily_watch_limit"] == 0
    assert migrated_browsing["max_replies_per_video"] == 0
    assert migrated_browsing["max_per_hour"] == 0
    assert migrated_browsing["max_per_day"] == 0
    assert migrated_browsing["business_timezone"] == "Asia/Tokyo"
    assert migrated_browsing["exclude_words"] == ["广告", "抽奖"]
    assert migrated_browsing["allowed_author_uids"] == ["12345"]
    assert migrated_browsing["top_level_enabled"] is True
    assert migrated_browsing["top_level_auto_publish"] is False
    assert migrated_browsing["mentions_enabled"] is True
    assert migrated_browsing["replies_enabled"] is True

    # Existing DB rows remain intact and source_pipeline column was added additively.
    assert store.one("SELECT id, source_pipeline FROM browsing_rounds WHERE id='r1'") == {
        "id": "r1",
        "source_pipeline": None,
    }
    assert store.one("SELECT id, source_pipeline FROM browsing_intents WHERE id='i1'") == {
        "id": "i1",
        "source_pipeline": None,
    }

    # Subsequent user edits to discovery_enabled/inbox_enabled are never clobbered by idempotent re-runs.
    store.set("browsing_policy", migrated_browsing | {"discovery_enabled": True, "inbox_enabled": True})
    assert migrate_operations_pipeline_v2(store) is False
    after = json.loads(store.one("SELECT value FROM kv WHERE key='browsing_policy'")["value"])
    assert after["discovery_enabled"] is True and after["inbox_enabled"] is True
