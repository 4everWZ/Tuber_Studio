"""Lifecycle control, policy/status projection, preflight, and start/stop coordination."""

import asyncio
import time
from uuid import uuid4

from ..core.config import operation_profile
from ..core.store import dump
from ..infra.inference.providers import public_error
from .channels.direct_messages import MessagePolicy
from .kernel.evidence import BrowsingEvidence
from .kernel.models import BrowsingPolicy


def policy(svc):
    """Return validated `browsing_policy` merged with schema defaults."""
    saved = svc.store.get("browsing_policy") or {}
    if not saved:
        return BrowsingPolicy().model_dump()
    merged = BrowsingPolicy().model_dump() | saved
    try:
        return BrowsingPolicy.model_validate(merged).model_dump()
    except Exception:
        return merged


def configured(svc):
    """Return effective base policy overlayed with live DM flags and active review session."""
    messages = MessagePolicy.model_validate(svc.store.get("message_policy", {})).model_dump()
    base = svc.policy() | {
        "message_poll_enabled": messages["enabled"] and messages["auto_poll"],
        "message_reply_enabled": messages["enabled"] and messages["auto_send"],
        "comment_handoff_enabled": messages["enabled"] and messages["allow_comment_handoff"],
    }
    review = svc.store.get("browsing_review_session") or {}
    character = svc.runtime.character
    if (
        review.get("state") in {"starting", "running"}
        and review.get("account_id") == (svc.account or svc.operations.policy()["account_id"])
        and (review.get("character_id"), review.get("character_version"))
        == (character["id"], character["version"])
    ):
        base["review_session_id"] = review["id"]
    return base


def has_interactions(svc, config=None):
    """Return True if any Inbox or interactive comment/like toggle is enabled."""
    config = config or svc.effective()
    return any(
        config.get(key)
        for key in (
            "inbox_enabled",
            "mentions_enabled",
            "mention_reply_enabled",
            "replies_enabled",
            "reply_enabled",
            "comment_like_enabled",
            "dynamic_like_enabled",
        )
    )


def permissions(svc):
    """Project coarse channel permission summary for UI and resume checks."""
    config = svc.configured()
    messages = MessagePolicy.model_validate(svc.store.get("message_policy", {}))
    dynamics = svc.store.get("dynamic_policy") or {}

    def mode(enabled, automatic):
        return "auto" if enabled and automatic else "review" if enabled else "off"

    return {
        "discovery_enabled": bool(config.get("discovery_enabled")),
        "inbox_enabled": bool(config.get("inbox_enabled")),
        "video_enabled": config["discovery_enabled"],
        "top_level": mode(config["top_level_enabled"], config["top_level_auto_publish"]),
        "replies": mode(config["reply_enabled"], config["reply_auto_publish"]),
        "comment_like": mode(config["comment_like_enabled"], config["comment_like_auto_publish"]),
        "dynamic_like": mode(config["dynamic_like_enabled"], config["dynamic_like_auto_publish"]),
        "private_messages": mode(messages.enabled, messages.auto_send),
        "private_poll": messages.enabled and messages.auto_poll,
        "mentions": config["mentions_enabled"],
        "reply_notifications": config["replies_enabled"],
        "video_like": mode(config["video_like_enabled"], config["video_like_auto_publish"]),
        "video_fav": mode(config["video_fav_enabled"], config["video_fav_auto_publish"]),
        "dynamics": mode(dynamics.get("enabled", False), dynamics.get("auto_publish", True)),
    }


def model_signature(svc):
    """Compute signature of active operation LLM model + active character card."""
    llm, character = operation_profile(svc.settings.data["omni"]), svc.runtime.character
    return dump(
        [
            "operations_text",
            llm.get("model"),
            llm.get("base_url"),
            llm.get("enable_thinking"),
            character["id"],
            character["version"],
        ]
    )


def control_state(svc, control=None):
    """Derive top-level service state (`emergency`/`stopping`/`draining`/`starting`/`blocked`/`running`/`off`)."""
    control = control or svc.operations.policy()
    if control["emergency"]:
        return "emergency"
    if svc.closed:
        return "stopping"
    if svc.operations.draining:
        return "draining"
    if svc.operations.starting:
        return "starting"
    if svc.blockers and (control["enabled"] or svc.last_error):
        return "blocked"
    return "running" if control["enabled"] else "off"


def has_uncertain_writes(svc) -> bool:
    """Return True when any cross-channel `publish_uncertain`/`uncertain` receipt blocks new writes."""
    account_id = str(svc.account or svc.discovery.history_scope()[0] or "")
    if not account_id:
        return False
    character_id = str((svc.runtime.character or {}).get("id") or "")
    for table in ("browsing_intents", "dm_drafts", "dynamic_posts"):
        if svc.store.one(
            f"SELECT 1 FROM {table} WHERE account_id=? AND character_id=? AND status='publish_uncertain' LIMIT 1",
            (account_id, character_id),
        ):
            return True
    return bool(
        svc.store.one(
            "SELECT 1 FROM operation_write_attempts WHERE account_id=? AND resolved_status='uncertain' LIMIT 1",
            (account_id,),
        )
    )


def inbox_status(svc) -> dict:
    """Return independent Pipeline B (Inbox) status snapshot."""
    control = svc.operations.policy()
    state = svc.control_state(control)
    enabled = svc.inbox_enabled()
    account_id = str(svc.account or svc.discovery.history_scope()[0] or "")
    pending_events = (
        svc.store.one(
            "SELECT COUNT(*) AS n FROM browsing_events WHERE account_id=? "
            "AND kind<>'comment_visit' AND status IN ('received','source_verified','context_ready','reply_deferred')",
            (account_id,),
        )["n"]
        if account_id
        else 0
    )
    effective_state = state if state != "running" else ("running" if enabled else "off")
    if effective_state == "running" and svc.has_uncertain_writes():
        effective_state = "frozen"
    return {
        "enabled": enabled,
        "configured_enabled": bool(svc.configured().get("inbox_enabled")),
        "state": effective_state,
        "pending_events": pending_events,
        "dm_enabled": bool((svc.store.get("message_policy") or {}).get("enabled")),
        "read_gate": {"allowed": True},
    }


def status(svc):
    """Build complete unified operations status snapshot."""
    control = svc.operations.policy()
    state = svc.control_state(control)
    ready = svc.ready_signature is not None and svc.ready_signature == svc.credential_signature()
    rounds = [svc.repo.round(i) for i in svc.active]
    svc.status_sequence += 1
    account, character_id, character_version = svc.operation_scope()
    review = svc.store.get("browsing_review_session") or {}
    uncertain = svc.has_uncertain_writes()
    gate_status = svc.read_gate.status(account or svc.account or "")
    return {
        "status_sequence": svc.status_sequence,
        "started_at": svc.started_at,
        "state": state,
        "error": svc.last_error or svc.discovery.error,
        "blockers": list(svc.blockers),
        "startup_check": (
            "checking"
            if svc.preflight_lock.locked() or svc.operations.starting
            else "failed"
            if svc.blockers
            else "ready"
            if ready
            else "pending"
        ),
        "last_error": svc.last_error,
        "account_id": svc.account or control["account_id"],
        "character": {"id": character_id, "version": character_version},
        "character_id": character_id,
        "character_version": character_version,
        "policy": control | svc.comments.publication_limits(),
        "settings": svc.policy(),
        "effective_settings": svc.effective(),
        "permissions": svc.permissions(),
        "base": {
            "comments": svc.comments.policy()["base"],
            "messages": MessagePolicy.model_validate(svc.store.get("message_policy", {})).model_dump(),
            "dynamics": svc.store.get("dynamic_policy", {}),
            "browsing": svc.policy(),
        },
        "effective": {
            "comments": svc.comments.policy(),
            "messages": svc.messages.policy(),
            "dynamics": svc.dynamics.policy(),
            "browsing": svc.effective(),
        },
        "vision": svc.evidence.visual.capabilities(),
        "active_round": rounds[0] if rounds else None,
        "active_rounds": rounds,
        "send_error": svc.operations.last_send_error,
        "comment_cooldown_until": (
            due
            if (due := svc.sends.comments_cooldown_until(svc.account or control["account_id"])) > time.time()
            else None
        ),
        "waiting_round": svc.waiting_round(),
        "discovery": svc.discovery.status() | {
            "enabled": svc.discovery.enabled,
            "configured_enabled": bool(svc.configured().get("discovery_enabled")),
            "read_cooldown_until": (gate_status["discovery_backoff"] or {}).get("until"),
        },
        "inbox": svc.inbox_status(),
        "read_gate": gate_status,
        "frozen_reason": "reconciliation_pending" if uncertain else None,
        "backlog": svc.backlog_status(persist=False),
        "metrics": {
            "cache_hits": svc.evidence.hits,
            "merged_reads": svc.evidence.merged,
            "notifications": svc.notifications.last,
            "model_queue": svc.runtime.operation_turns.stats,
            "send_queue": svc.sends.stats,
        },
        "daily_watch": svc.repo.quotas.watch_status(
            svc.account or control["account_id"],
            svc.runtime.character["id"],
            svc.policy().get("daily_watch_limit", 0),
            svc.policy().get("business_timezone", "Asia/Taipei"),
        ),
        "automatic_sending_ready": svc.bilibili_account.credentials_configured and ready and not svc.blockers,
        "semantic_validation": svc.store.get("browsing_semantic_validation", {}),
        "sending_note": "按设置中已开放的动作与审核方式运行",
        "cache": svc.runtime.cache.usage("browsing"),
        "review_session": review,
        "claims": svc.operations.claims.list_claims(),
    }


async def configure(svc, data):
    """Validate and persist browsing/control policy updates and notify runners."""
    if not isinstance(data, dict):
        raise ValueError("设置必须为对象")
    raw = dict(data)
    raw.pop("comment_handoff_enabled", None)
    raw.pop("handoff_auto_send", None)
    control_patch = {}
    for key in (
        "recipient_cooldown_seconds",
        "direct_quota_reserve",
        "min_comment_interval_seconds",
        "min_reaction_interval_seconds",
    ):
        if key in raw:
            control_patch[key] = raw.pop(key)
    if "business_timezone" in raw and isinstance(raw["business_timezone"], str) and raw["business_timezone"].strip():
        raw["business_timezone"] = raw["business_timezone"].strip()
    if raw:
        merged = svc.policy() | raw
        validated = BrowsingPolicy.model_validate(merged).model_dump()
        if (
            validated.get("review_session_id")
            and (svc.store.get("browsing_review_session") or {}).get("state") not in {"starting", "running"}
        ):
            validated.pop("review_session_id", None)
        svc.store.set("browsing_policy", validated)
    if control_patch:
        svc.operations.configure(
            control_patch,
            account_id=svc.account,
            character=svc.runtime.character,
        )
    svc.changed.set()
    svc.interactions_changed.set()
    svc.notification_schedule.set()
    svc.sends.changed.set()
    return svc.status()


async def set_discovery_enabled(svc, enabled: bool):
    """Toggle Discovery (Pipeline A) independently without disturbing Inbox."""
    if type(enabled) is not bool:
        raise ValueError("enabled须为布尔值")
    if enabled and svc.operations.policy()["emergency"]:
        raise ValueError("紧急停止仍有效")
    await svc.configure({"discovery_enabled": enabled})
    if enabled:
        if not svc.operations.policy()["enabled"]:
            return await svc.set_enabled(True)
        await svc.preflight(for_automation=True)
        if svc.blockers:
            await svc.configure({"discovery_enabled": False})
            raise ValueError("；".join(svc.blockers))
        svc.discovery_runner.start()
        svc.changed.set()
        return svc.status()
    for identity, task in list(svc.active.items()):
        row = svc.repo.round(identity)
        if row and not row["preview"] and not row["config"].get("interaction_only") and not row["config"].get("full_video"):
            task.cancel()
    svc.changed.set()
    return svc.status()


async def set_inbox_enabled(svc, enabled: bool):
    """Toggle Inbox (Pipeline B) independently without disturbing Discovery."""
    if type(enabled) is not bool:
        raise ValueError("enabled须为布尔值")
    if enabled and svc.operations.policy()["emergency"]:
        raise ValueError("紧急停止仍有效")
    await svc.configure({"inbox_enabled": enabled})
    if enabled:
        if not svc.operations.policy()["enabled"]:
            return await svc.set_enabled(True)
        await svc.preflight(for_automation=True)
        if svc.blockers:
            await svc.configure({"inbox_enabled": False})
            raise ValueError("；".join(svc.blockers))
        svc.inbox_runner.start()
        svc.notification_schedule.set()
        svc.interactions_changed.set()
        return svc.status()
    svc.notifications.pause(svc.account)
    svc.notification_schedule.set()
    svc.interactions_changed.set()
    return svc.status()


def credential_signature(svc):
    """Return tuple identifying active Bilibili adapter + credential pair."""
    return (
        id(svc.bilibili_account.adapter),
        svc.bilibili_account.credentials.get("sessdata"),
        svc.bilibili_account.credentials.get("csrf"),
    )


async def current_account(svc, expected=None):
    """Refresh active adapter reference if swapped and return verified account id."""
    if svc.adapter is not svc.bilibili_account.adapter:
        visual = svc.visual_provider
        await svc.evidence.close()
        svc.adapter = svc.bilibili_account.adapter
        svc.notifications.adapter = svc.adapter
        svc.evidence = BrowsingEvidence(svc.runtime, svc.adapter, svc._read, visual)
        if hasattr(svc.adapter, "request_gate"):
            svc.adapter.request_gate = svc._gate
    svc.account = str(await svc._read(svc.bilibili_account.account_id))
    if expected is not None and str(expected) != svc.account:
        raise ValueError("候选来源账号已变化")
    return svc.account


async def handle_auth_loss(svc, error=None):
    """Stop both Discovery and Inbox immediately upon 401/credential expiration."""
    svc.ready_signature = None
    svc.account = None
    message = public_error(error) if error else "账号登录态已失效，请重新登录后再开启"
    svc.last_error = message
    svc.blockers = [message]
    await svc.stop()


async def check_browsing_model(svc):
    """Verify configured operation LLM health before enabling automation."""
    health = await svc.runtime.operation_llm.health()
    if health.get("ok") is False:
        raise ValueError("请连接实际运营模型；离线演示不支持自主决策")


async def probe_discovery(svc, config):
    """Probe the configured Discovery feed source before starting automation."""
    source = config["sources"][0] if config["sources"] else "recommend"
    if source == "recommend" and config["keywords"]:
        source = "search"
    if source == "search" and not config["keywords"]:
        source = "recommend"
    if source == "specified":
        if not config["watch_videos"]:
            raise ValueError("指定视频模式未填写视频")
        return
    await svc._read(
        svc.adapter.discover,
        source,
        config["keywords"][0] if config["keywords"] else "",
        1,
        1,
    )


async def preflight(svc, *, for_automation=False):
    """Run serialized preflight checks and return updated status."""
    async with svc.preflight_lock:
        await svc._check_preflight(for_automation=for_automation)
    return svc.status()


async def check_preflight(svc, *, for_automation=False):
    """Verify account identity, LLM health, required adapter capabilities, and read feeds."""
    svc.blockers = []
    svc.ready_signature = None
    try:
        await svc._current_account()
    except (ValueError, AttributeError) as error:
        svc.account = None
        svc.blockers.append("账号身份核验失败：" + public_error(error))
    try:
        await svc.check_browsing_model()
    except Exception as error:
        svc.blockers.append("模型不可用：" + public_error(error))
    config = svc.configured()
    video = config["discovery_enabled"] or not for_automation
    required = set()
    if video:
        required.update(("discover", "video_metadata", "subtitles"))
        if config["top_level_enabled"] and config["top_level_auto_publish"]:
            required.add("publish_top_level")
    if config["reply_enabled"] or config["comment_like_enabled"]:
        required.update(("read_public_thread", "fetch"))
        if config["reply_auto_publish"]:
            required.add("publish_resource_reply")
        if config["comment_like_enabled"]:
            required.add("like_comment")
    if config["mentions_enabled"] or config["replies_enabled"]:
        required.update(("notifications", "read_public_thread"))
    if config["dynamic_like_enabled"]:
        required.update(("read_dynamic_like", "like_dynamic"))
    svc.blockers.extend(
        "平台缺少能力：" + name
        for name in sorted(required)
        if not callable(getattr(svc.adapter, name, None))
    )
    if video and svc.account and not svc.blockers:
        try:
            await svc.probe_discovery(config)
        except ValueError as error:
            svc.blockers.append("发现来源不可用：" + public_error(error))
    if svc.account:
        for kind, enabled in (
            ("mention", config["mentions_enabled"]),
            ("reply", config["replies_enabled"]),
        ):
            if enabled:
                try:
                    page = await svc._read(svc.adapter.notifications, kind, None)
                    if str(page["account_id"]) != svc.account:
                        raise ValueError("通知账号与当前账号不同")
                except ValueError as error:
                    svc.blockers.append(
                        ("提及" if kind == "mention" else "收到回复")
                        + "读取不可用："
                        + public_error(error)
                    )
        if config["message_poll_enabled"]:
            try:
                if not svc.messages.injected and not svc.bilibili_account.credentials_configured:
                    raise ValueError("私信读取需要当前账号登录凭证")
                adapter = await svc.messages._adapter()
                own_uid, _ = await adapter.sessions()
                if str(own_uid) != svc.account:
                    raise ValueError("私信账号身份与公开互动不一致")
            except ValueError as error:
                svc.blockers.append("私信读取不可用：" + public_error(error))
    svc.ready_signature = svc.credential_signature() if not svc.blockers else None


async def start_review_session(svc, *, resume_session_id=None):
    """Start fresh review batches without discarding older paused progress."""
    if svc.operations.policy()["enabled"] or svc.active or svc.operations.starting:
        raise ValueError("请先停止当前运营，再开始新的review验收")
    if "auto" in svc.permissions().values():
        raise ValueError("请先将已启用渠道设为审核模式，再开始review验收")
    account = await svc._current_account()
    if svc.operations.policy()["enabled"] or svc.active or svc.operations.starting:
        raise ValueError("自主运营正在启动，请等待当前启动完成")
    if "auto" in svc.permissions().values():
        raise ValueError("请先将已启用渠道设为审核模式，再开始review验收")
    character = svc.runtime.character
    previous = svc.store.get("browsing_review_session", {})
    if resume_session_id is not None and (
        not isinstance(resume_session_id, str)
        or previous.get("id") != resume_session_id
        or previous.get("account_id") != account
        or previous.get("character_id") != character["id"]
        or previous.get("character_version") != character["version"]
    ):
        raise ValueError("review会话与当前账号或角色不匹配，未恢复")
    session = {
        "id": str(uuid4()),
        "account_id": account,
        "character_id": character["id"],
        "character_version": character["version"],
        "started_at": time.time(),
        "state": "starting",
    }
    if resume_session_id is not None:
        session = {key: value for key, value in previous.items() if key not in {"ended_at", "paused_at"}}
        session.update(state="starting", resumed_at=time.time())
    svc.store.set("browsing_review_session", session)
    try:
        await svc.set_enabled(True)
        current = svc.store.get("browsing_review_session", {})
        if current.get("state") != "starting" or not svc.operations.policy()["enabled"]:
            raise ValueError("review验收启动已取消")
        svc.store.set("browsing_review_session", current | {"state": "running"})
        return svc.status()
    except BaseException:
        await svc.stop()
        svc.store.set("browsing_review_session", previous)
        raise


async def set_enabled(svc, enabled, *, guard=None):
    """Enable or disable unified autonomous operations with atomic epoch/resume wiring."""
    if type(enabled) is not bool:
        raise ValueError("enabled须为布尔值")
    if not enabled:
        return await svc.stop()
    if svc.operations.policy()["emergency"]:
        raise ValueError("紧急停止仍有效")
    if svc.operations.draining:
        raise ValueError("当前动作正在收尾")
    if svc.operations.starting:
        raise ValueError("自主运营正在启动")
    if svc.operations.policy()["enabled"]:
        return svc.status()
    svc.operations.starting = True
    svc.last_error = None
    svc.control_generation += 1
    generation = svc.control_generation
    try:
        await svc.preflight(for_automation=True)
        if generation != svc.control_generation:
            raise ValueError("自主运营启动已取消")
        if svc.blockers:
            raise ValueError("；".join(svc.blockers))
        if guard is not None:
            guard()
        previous = svc.operations.policy()
        current = svc.operations.configure(
            {"enabled": True}, account_id=svc.account, character=svc.runtime.character
        )
        resume_epoch = svc.store.get("operation_send_resume_epoch")
        if (
            resume_epoch is not None
            and previous["epoch"] in {resume_epoch, resume_epoch + 1}
            and svc.store.get("operation_send_resume_permissions") in (None, svc.send_permissions())
            and not svc.effective().get("review_session_id")
            and all(previous[key] == current[key] for key in ("account_id", "character_id", "character_version"))
        ):
            svc.store.execute(
                "UPDATE operation_send_queue SET epoch=? WHERE epoch=? AND account_id=? AND character_id=? AND character_version=?",
                (current["epoch"], resume_epoch, svc.account, current["character_id"], current["character_version"]),
            )
            svc.store.set("operation_send_resume_epoch", None)
            svc.store.set("operation_send_resume_permissions", None)
        svc.discovery.remember_account(svc.account)
        await svc.messages.resume_pending()
        if generation != svc.control_generation:
            raise ValueError("自主运营启动已取消")
    except (Exception, asyncio.CancelledError) as error:
        if generation == svc.control_generation:
            svc.operations.configure({"enabled": False})
            svc.last_error = public_error(error)
            if not svc.blockers:
                svc.blockers = [svc.last_error]
        raise
    finally:
        svc.operations.starting = False
    svc.comments.policy_changed.set()
    svc.messages.changed.set()
    svc.dynamics.changed.set()
    svc.changed.set()
    svc.interactions_changed.set()
    svc.notification_schedule.set()
    svc.sends.start()
    svc.sends.changed.set()
    svc._resume_reactions()
    return svc.status()


async def stop(svc, *, emergency=False):
    """Gracefully drain or emergency-halt active Discovery and Inbox operations."""
    if emergency or svc.closed:
        await svc._abort_operations(emergency=emergency)
    elif not svc.operations.draining:
        svc.control_generation += 1
        if svc.operations.policy()["enabled"] and not svc.operations.starting:
            svc.operations.draining = True
            svc.event_changed.set()
            svc.sends.changed.set()
            admitted = tuple(task for identity, task in svc.active.items() if not svc.repo.round(identity)["preview"])
            admitted += svc.operations.admitted_work
            if admitted:
                svc.drain_task = asyncio.create_task(svc._drain(admitted))
            else:
                svc._finish_stop()
        elif svc.operations.starting:
            await svc._abort_operations()
        else:
            standard_previews = [
                child
                for identity, child in svc.active.items()
                if not svc.repo.round(identity)["config"].get("full_video")
            ]
            for child in standard_previews:
                child.cancel()
            if standard_previews:
                await asyncio.gather(*standard_previews, return_exceptions=True)
            svc._finish_stop(stop_sends=True)
    return svc.status()


async def drain(svc, admitted):
    """Await in-flight admitted work before finalizing service stop."""
    try:
        await asyncio.gather(*admitted, return_exceptions=True)
    finally:
        if not svc.closed:
            svc._finish_stop()
        svc.drain_task = None


def finish_stop(svc, *, emergency=False, stop_sends=True):
    """Finalize stopped control state, notification pause, and stale claims cleanup."""
    svc.operations.draining = False
    svc.operations.starting = False
    policy_row = svc.operations.policy()
    if policy_row["enabled"]:
        svc.store.set("operation_send_resume_epoch", policy_row["epoch"])
        svc.store.set("operation_send_resume_permissions", svc.send_permissions())
    svc.operations.configure({"enabled": False, **({"emergency": True} if emergency else {})})
    svc.notifications.pause(svc.account)
    review = svc.store.get("browsing_review_session") or {}
    if review.get("state") in {"starting", "running", "draining"}:
        svc.store.set("browsing_review_session", review | {"state": "stopped", "stopped_at": time.time()})
    if stop_sends and svc.closed:
        pass
    svc.operations.claims.cleanup_stale()
    svc.changed.set()
    svc.interactions_changed.set()
    svc.notification_schedule.set()
    svc.event_changed.set()
    svc.sends.changed.set()


async def abort_operations(svc, *, emergency=False):
    """Immediately cancel active tasks and persist paused full-video/emergency state."""
    svc.control_generation += 1
    if svc.drain_task is not None and not svc.drain_task.done():
        svc.drain_task.cancel()
        await asyncio.gather(svc.drain_task, return_exceptions=True)
        svc.drain_task = None
    policy_row = svc.operations.policy()
    if policy_row["enabled"]:
        svc.store.set("operation_send_resume_epoch", policy_row["epoch"])
        svc.store.set("operation_send_resume_permissions", svc.send_permissions())
    svc.operations.configure({"enabled": False, **({"emergency": True} if emergency else {})})
    if emergency:
        svc.store.execute(
            "UPDATE browsing_rounds SET status='paused',error='紧急停止，取材进度已保存',updated_at=? "
            "WHERE status='waiting' AND json_extract(config,'$.full_video')=1",
            (time.time(),),
        )
    tasks = [
        child
        for identity, child in list(svc.active.items())
        if emergency or not svc.repo.round(identity)["config"].get("full_video")
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if emergency:
        svc.notifications.discard_pending(svc.account)
    else:
        svc.notifications.pause(svc.account)
    svc._finish_stop(emergency=emergency, stop_sends=True)


async def open_service(svc):
    """Recover persisted state, enforce cold-start standby, and launch runners."""
    svc.closed = False
    svc.started_at = time.time()
    svc.repo.recover()
    saved_policy = svc.store.get("operations_policy", {})
    if saved_policy.get("enabled"):
        svc.store.set("operations_policy", saved_policy | {"enabled": False})
    for record in svc.store.all("SELECT key FROM kv WHERE key LIKE 'browsing_discovery_pending:%'"):
        svc.store.set(record["key"], False)
    svc.operations.claims.cleanup_stale()
    svc.discovery_runner.start()
    svc.inbox_runner.start()
    svc.loop = svc.discovery_runner._task
    svc.poller = svc.inbox_runner._poller_task
    svc.interaction_worker = svc.inbox_runner._interaction_task
    svc.sends.start()


async def close_service(svc):
    """Stop all background runners cleanly and release evidence resources."""
    svc.closed = True
    if svc.drain_task is not None:
        svc.drain_task.cancel()
    await svc.discovery_runner.stop()
    await svc.inbox_runner.stop()
    await svc.sends.close()
    svc.loop = svc.poller = svc.interaction_worker = None
    await svc.stop()
    await svc.evidence.close()
    for task in list(svc.active.values()):
        task.cancel()
    if svc.active:
        await asyncio.gather(*svc.active.values(), return_exceptions=True)
    svc.operations.claims.cleanup_stale()
    svc.changed.set()
    svc.interactions_changed.set()
    svc.notification_schedule.set()
    svc.event_changed.set()
