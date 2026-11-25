"""Single public boundary (`L3 operations -> agent/app`) hiding kernel, channel, discovery, and inbox internals."""

from __future__ import annotations

import json
import time

from .activity import acknowledge_issues, activity, clear_issues, history, issue_history
from .channels.attachments.attachments import (
    checked_image,
    comment_payload,
    image_scope,
    row_ref,
)
from .channels.attachments.attachments import (
    decision as image_decision,
)
from .channels.comments.timeline import (
    COMMENT_ID,
    VIDEO_ID,
    comment_href,
    comment_thread,
    ensure_comment_timeline,
    resource_key,
)
from .channels.direct_messages import MessagePolicy
from .kernel.caption_corrections import CORRECTED_VIDEOS, corrected_caption
from .kernel.control import canonical_intent
from .kernel.feedback import FeedbackPublication
from .kernel.models import BrowsingPolicy
from .kernel.quotas import day_window


def history_scope(browsing) -> tuple[str, str, int]:
    """Return `(account_id, character_id, character_version)` for the active browsing service."""
    return browsing.discovery.history_scope()


def business_timezone(browsing) -> str:
    """Return the configured business timezone name for operations."""
    if browsing is None:
        return "Asia/Taipei"
    return browsing.policy().get("business_timezone", "Asia/Taipei")


def material_excerpt_state(browsing, account_id: str, character_id: str, bvid: str) -> tuple[dict | None, dict | None]:
    """Return `(progress, watch_evidence)` for a video without exposing repository internals."""
    scope = {"account_id": account_id, "character_id": character_id}
    return browsing.repo.content_progress(scope, bvid), browsing.repo.watch_evidence(scope, bvid)


def control_policy(service) -> dict:
    """Return current kernel OperationsControl policy dictionary."""
    return service.operations.policy()


def control_starting(browsing) -> bool:
    """Return True while autonomous operations startup preflight is in flight."""
    return bool(browsing.operations.starting)


def get_attempt(service, token: str):
    """Look up a write reservation attempt by root/retry token."""
    return service.operations.get_attempt(token)


def reconcile_attempt(service, token: str, *, account_id: str, binding: dict, receipt: dict):
    """Settle an uncertain write attempt receipt via OperationsControl."""
    return service.operations.reconcile_attempt(token, account_id=account_id, binding=binding, receipt=receipt)


def create_feedback_publication(service) -> FeedbackPublication:
    """Create a FeedbackPublication coordinator bound to `service.runtime` and `service.operations`."""
    return FeedbackPublication(service.runtime, service.store, service.operations)


def comment_draft_source(comments, identity):
    return comments._draft_source(identity)


def comment_publication_binding(comments, row, account_id, character):
    return comments._publication_binding(row, account_id, character)


def dm_draft_source(store, identity):
    return store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))


def dm_message_source(messages, source_kind, source_id, character):
    return messages._source(source_kind, source_id, character)


def dm_publication_binding(messages, row, source, account_id, character):
    return messages._publication_binding(row, source, account_id, character)


def dm_reply_window(messages, row, source):
    return messages._reply_window(row, source)


def dm_outgoing_payload(messages, row, character):
    return messages._outgoing(row, character)


async def reconcile_channel_draft(service, identity, *, guard=None):
    return await service._reconcile(identity, guard=guard)


def dynamic_valid_sources(dynamics, row):
    return dynamics._valid_sources(row)


def dynamic_character(dynamics):
    return dynamics._character()


def dynamic_publication_binding(dynamics, row, account_id, character):
    return dynamics._publication_binding(row, account_id, character)


def dynamic_outgoing_payload(dynamics, row, character):
    return dynamics._outgoing(row, character)


def get_dynamic_post(dynamics, post_id):
    return dynamics._post(post_id)


def dynamic_publication_images(dynamics, row):
    return dynamics._publication_images(row)


def find_dynamic_edit(dynamics, request_id):
    return dynamics._find_edit(request_id)


async def reconcile_dynamic_post(dynamics, post_id, *, guard=None):
    return await dynamics._reconcile(post_id, guard=guard)


def illustration_for_row(runtime, catalog, channel: str, row: dict, *, artifact_id: str):
    """Verify & return scoped illustration asset bytes without leaking attachment helpers."""
    ref = image_decision(runtime, channel, row, row_ref(row))
    if not ref or ref.get("artifact_id") != artifact_id:
        return None
    return checked_image(catalog, ref, row["character_id"], runtime=runtime, scope=image_scope(channel, row))


class OperationsFacade:
    """High-level operations API for L3 `agent/` and L4 `app/` entry points (#13 §3B)."""

    def __init__(self, store, service, comments=None, messages=None, dynamics=None, account=None):
        self.store = store
        self.service = service
        self.comments = comments or service.comments
        self.messages = messages or service.messages
        self.dynamics = dynamics or service.dynamics
        self.account = account or service.bilibili_account

    async def open(self) -> None:
        await self.service.open()

    async def close(self) -> None:
        await self.service.close()

    def status(self) -> dict:
        return self.service.status()

    async def configure_control(self, patch: dict) -> dict:
        self.service.operations.configure(
            patch,
            account_id=self.service.account,
            character=self.service.runtime.character,
        )
        return self.status()

    async def configure_discovery(self, patch: dict) -> dict:
        return await self.service.configure(patch)

    async def configure_inbox(self, patch: dict) -> dict:
        return await self.service.configure(patch)

    async def set_discovery_enabled(self, enabled: bool) -> dict:
        return await self.service.set_discovery_enabled(enabled)

    async def set_inbox_enabled(self, enabled: bool) -> dict:
        return await self.service.set_inbox_enabled(enabled)

    async def start_review_session(self, *, resume_session_id: str | None = None) -> dict:
        return await self.service.start_review_session(resume_session_id=resume_session_id)

    async def stop(self, *, emergency: bool = False) -> dict:
        return await self.service.stop(emergency=emergency)

    def reset_emergency(self) -> dict:
        self.service.operations.configure({"emergency": False})
        return self.status()

    async def preflight(self, *, for_automation: bool = True) -> dict:
        return await self.service.preflight(for_automation=for_automation)

    def claims(self) -> list[dict]:
        return self.service.operations.claims.list_claims()

    def browsing_policy(self) -> dict:
        return self.service.policy()

    def operation_timezone(self) -> str:
        return business_timezone(self.service)

    def discovery_history_scope(self) -> tuple[str, str, int]:
        return self.service.discovery.history_scope()

    async def restore_history_scope(self):
        return await self.service.discovery.restore_history_scope()

    def recent_activity(self, *, day=None, kind="all", comment_filter="all", source="all", page=1, page_size=10, paginate=True):
        return activity(
            self.service,
            day=day,
            kind=kind,
            comment_filter=comment_filter,
            source=source,
            page=page,
            page_size=page_size,
            paginate=paginate,
        )

    def recent_history(self, *, day=None, page=1, page_size=10):
        return history(self.service, day=day, page=page, page_size=page_size)

    def acknowledge_issues(self, observations: list[dict]) -> dict:
        acknowledge_issues(self.service, observations)
        return self.service.discovery.status()

    def clear_issues(self) -> dict:
        clear_issues(self.service)
        return self.service.discovery.status()

    def issue_history(self, issues: list[dict]) -> list[dict]:
        return issue_history(self.service, issues)

    async def queue_full_video(self, request_id, character, selection):
        return await self.service.queue_full_video(request_id, character, selection)

    async def update_full_video(self, request_id, *, resume: bool):
        return await self.service.update_full_video(request_id, resume=resume)

    def get_full_video_status(self, round_id):
        row = self.service.repo.round(round_id)
        return self.service.full_video_status(row) if row else None

    def get_round(self, round_id):
        return self.service.repo.round(round_id)

    async def run_preview_browse(self, **kwargs):
        return await self.service.run(**kwargs)

    async def read_video_metadata(self, bvid: str):
        return await self.service._read(self.service.adapter.video_metadata, bvid)

    def visual_provider_capabilities(self) -> dict:
        provider = self.service.visual_provider
        return {"available": provider is not None, "timestamp": time.time()}


def operation_status_snapshot(browsing) -> dict:
    """Compact status helper used across operator chat & agent context layers."""
    return {
        "policy": control_policy(browsing),
        "state": browsing.control_state(),
        "discovery_enabled": browsing.discovery.enabled,
        "inbox_enabled": browsing.inbox_enabled(),
        "read_gate": browsing.read_gate.status(browsing.account or ""),
        "claims": browsing.operations.claims.list_claims(),
    }


def decode_target_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def _raw_browsing(target):
    return getattr(target, "browsing", target)


def _raw_dynamics(target):
    return getattr(target, "dynamics", target)


def _raw_comments(target):
    return getattr(target, "comments", target)


async def restore_history_scope(browsing) -> tuple:
    raw = _raw_browsing(browsing)
    return await raw.discovery.restore_history_scope()


def browsing_policy(browsing) -> dict:
    return _raw_browsing(browsing).policy()


def browsing_status(browsing) -> dict:
    return _raw_browsing(browsing).status()


def browsing_round(browsing, round_id: str):
    return _raw_browsing(browsing).repo.round(round_id)


def requested_preview_row(browsing, request_id: str, character: dict, selection: dict, account_id: str | None):
    return _raw_browsing(browsing).repo.requested_preview(request_id, character, selection, account_id)


async def queue_full_video(browsing, request_id: str, character: dict, selection: dict):
    return await _raw_browsing(browsing).queue_full_video(request_id, character, selection)


async def update_full_video(browsing, request_id: str, *, resume: bool):
    return await _raw_browsing(browsing).update_full_video(request_id, resume=resume)


def full_video_status(browsing, row):
    return _raw_browsing(browsing).full_video_status(row)


async def run_preview_browse(browsing, **kwargs):
    return await _raw_browsing(browsing).run(**kwargs)


async def read_video_metadata(browsing, bvid: str):
    raw = _raw_browsing(browsing)
    return await raw._read(raw.adapter.video_metadata, bvid)


def video_clip_adapter(browsing):
    adapter = getattr(_raw_browsing(browsing), "adapter", None)
    return adapter if adapter is not None and hasattr(adapter, "video_stream_clip") else None


def browsing_visual_provider(browsing, runtime=None):
    raw = _raw_browsing(browsing)
    evidence = getattr(raw, "evidence", None)
    if evidence is not None and getattr(evidence, "visual", None) is not None:
        return evidence.visual
    return getattr(runtime, "visual_provider", None) if runtime is not None else None


async def configure_browsing(browsing, patch: dict):
    return await _raw_browsing(browsing).configure(patch)


async def configure_messages(browsing, patch: dict):
    return await _raw_browsing(browsing).messages.configure(patch)


async def set_browsing_enabled(browsing, enabled: bool, *, guard=None):
    return await _raw_browsing(browsing).set_enabled(enabled, guard=guard)


async def stop_browsing(browsing):
    return await _raw_browsing(browsing).stop()


def dynamic_sources(dynamics, *, bound_ids=(), query=None) -> list:
    return _raw_dynamics(dynamics)._sources(bound_ids=bound_ids, query=query)


async def create_operator_dynamic_draft(dynamics, character, request_id: str, topic: str, *, account=None):
    return await _raw_dynamics(dynamics).create_operator_draft(character, request_id, topic, account=account)


async def propose_dynamic_edit(dynamics, character, request_id: str, post_id: str, message: str):
    return await _raw_dynamics(dynamics).propose_dynamic_edit(character, request_id, post_id, message)


def get_dynamic_edit(dynamics, request_id: str, account: str | None, character_id: str):
    return _raw_dynamics(dynamics).dynamic_edit(request_id, account, character_id)


async def set_dynamics_policy(dynamics, patch: dict):
    return await _raw_dynamics(dynamics).set_policy(patch)


def comment_context_scope(comments, video_id: str, thread_id: str) -> str:
    return _raw_comments(comments).account_context_scope(video_id, thread_id)


async def rebuild_comment_context(comments, video_id: str, thread_id: str):
    return await _raw_comments(comments).rebuild_context(video_id, thread_id)


def comments_display_account_id(comments) -> str | None:
    raw = _raw_comments(comments)
    messages = getattr(raw, "direct_messages", None)
    return messages.display_account_id() if messages else raw.account.display_account_id()


def _facade_table_exists(store, table: str) -> bool:
    if store is None:
        return False
    return (
        store.one("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,))
        is not None
    )


def _operations_host(target):
    if target is None:
        return None
    raw = _raw_browsing(target)
    return getattr(raw, "operations", raw)


def reserve_write_attempt(
    target,
    *,
    account_id: str,
    character: dict,
    intent: str,
    channel: str,
    recipient: str = "",
    automatic: bool = False,
    limits: dict | None = None,
    epoch: int | None = None,
    binding: dict | None = None,
    content_hash: str | None = None,
) -> str:
    """Reserve an idempotent platform write attempt through OperationsControl."""
    ops = _operations_host(target)
    if ops is None or not hasattr(ops, "reserve"):
        raise ValueError("OperationsControl 未就绪，无法预约平台投稿")
    return ops.reserve(
        account_id,
        character,
        intent,
        channel,
        recipient=recipient,
        automatic=automatic,
        limits=limits,
        epoch=epoch,
        binding=binding,
        content_hash=content_hash,
    )


def finish_write_attempt(
    target,
    token: str,
    status: str,
    *,
    receipt: dict | None = None,
    error: str | None = None,
) -> None:
    """Settle a reserved platform write attempt through OperationsControl."""
    ops = _operations_host(target)
    if ops is None or not hasattr(ops, "finish"):
        raise ValueError("OperationsControl 未就绪，无法结算投稿回执")
    ops.finish(token, status, receipt=receipt, error=error)


def find_write_attempt_by_intent(target, *, account_id: str, character_id: str, intent: str) -> dict | None:
    """Look up the latest write attempt matching `(account_id, character_id, intent)`."""
    ops = _operations_host(target)
    store = getattr(ops, "store", None) or getattr(_raw_browsing(target), "store", None)
    if not _facade_table_exists(store, "operation_write_attempts"):
        return None
    row = store.one(
        "SELECT token, status, resolved_status, receipt, error, started_at FROM operation_write_attempts "
        "WHERE account_id=? AND character_id=? AND intent=? ORDER BY started_at DESC LIMIT 1",
        (str(account_id or ""), str(character_id or ""), intent),
    )
    if row is None:
        return None
    if isinstance(row.get("receipt"), str):
        row["receipt"] = decode_target_json(row["receipt"])
    return row


def _extract_video_candidate(row: dict, *, source_kind: str) -> dict | None:
    material = decode_target_json(row.get("material"))
    payload = decode_target_json(row.get("payload"))
    meta = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
    if not meta and isinstance(payload.get("metadata"), dict):
        meta = payload["metadata"]
    bvid = str(row.get("bvid") or meta.get("bvid") or "").strip()
    if not bvid:
        return None
    title = str(meta.get("title") or payload.get("title") or bvid).strip()
    owner = str(meta.get("owner") or payload.get("owner") or "").strip()
    owner_uid = str(meta.get("owner_uid") or payload.get("owner_uid") or "").strip()
    duration = float(meta.get("duration") or payload.get("duration") or 0.0)
    tags = [str(t).strip() for t in (meta.get("tags") or payload.get("tags") or ()) if str(t).strip()][:10]
    excerpts: list[str] = []
    for part in material.get("parts") or ():
        if not isinstance(part, dict):
            continue
        for seg in (part.get("segments") or ())[:6]:
            if isinstance(seg, dict) and seg.get("text"):
                excerpts.append(" ".join(str(seg["text"]).split())[:180])
            if len(excerpts) >= 6:
                break
        if len(excerpts) >= 6:
            break
    summary = str(meta.get("description") or payload.get("explanation") or "").strip()[:300]
    if not summary and excerpts:
        summary = " / ".join(excerpts[:3])[:300]
    sources_meta = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    popular = any(isinstance(s, dict) and s.get("source") in {"popular", "recommend", "search"} for s in sources_meta)
    return {
        "bvid": bvid,
        "title": title,
        "owner": owner,
        "owner_uid": owner_uid,
        "duration": duration,
        "tags": tags,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "summary": summary,
        "segments_preview": excerpts,
        "popular": popular,
        "read_state": str(row.get("read_state") or row.get("status") or ("watched" if row.get("decisions") is not None else "")),
        "source_kind": source_kind,
        "scope": "public",
        "updated_at": float(row.get("updated_at") or row.get("created_at") or 0.0),
    }


def select_video_candidates(
    browsing,
    *,
    store=None,
    limit: int = 8,
    query: str | None = None,
) -> list[dict]:
    """Return strictly public watched/discovered Bilibili video candidates for content production."""
    raw = _raw_browsing(browsing) if browsing is not None else None
    db_store = store or getattr(raw, "store", None)
    if db_store is None:
        return []
    account_id = ""
    character_id = ""
    if raw is not None and hasattr(raw, "discovery"):
        scope = raw.discovery.history_scope()
        account_id, character_id = str(scope[0] or ""), str(scope[1] or "")
    candidates: dict[str, dict] = {}
    needle = " ".join((query or "").split()).casefold()

    if _facade_table_exists(db_store, "browsing_video_progress"):
        clauses, params = [], []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if character_id:
            clauses.append("character_id=?")
            params.append(character_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db_store.all(
            f"SELECT bvid, material, decisions, updated_at FROM browsing_video_progress {where} "
            "ORDER BY updated_at DESC LIMIT 40",
            tuple(params),
        )
        for row in rows:
            item = _extract_video_candidate(row, source_kind="watched_video")
            if item is not None:
                candidates.setdefault(item["bvid"], item)

    if _facade_table_exists(db_store, "browsing_tasks"):
        rows = db_store.all(
            "SELECT bvid, status, payload, updated_at, created_at FROM browsing_tasks "
            "WHERE bvid IS NOT NULL AND status IN ('selected','viewed_without_comment','evidence_ready','comments_checked') "
            "ORDER BY updated_at DESC LIMIT 40"
        )
        for row in rows:
            item = _extract_video_candidate(row, source_kind="discovered_video")
            if item is not None and item["bvid"] not in candidates:
                candidates[item["bvid"]] = item

    ordered = list(candidates.values())
    if needle:
        filtered = [
            item
            for item in ordered
            if needle in f"{item['bvid']} {item['title']} {' '.join(item['tags'])} {item['summary']}".casefold()
        ]
        ordered = filtered
    ordered.sort(key=lambda item: (bool(item["popular"]), item["updated_at"]), reverse=True)
    return ordered[: max(1, min(int(limit), 30))]


def heartbeat_pulse_snapshot(browsing, *, store=None, hot_limit: int = 6) -> dict:
    """Collect read-only local health & hot-topic signals across Discovery, Inbox, DM, and Dynamics."""
    raw = _raw_browsing(browsing) if browsing is not None else None
    db_store = store or getattr(raw, "store", None)
    if raw is None:
        return {
            "account_id": "",
            "character_id": "",
            "control_state": "off",
            "automatic_ready": False,
            "account_error": None,
            "credentials_configured": False,
            "discovery": {"state": "off", "enabled": False, "error": None, "attention": []},
            "inbox": {
                "state": "off",
                "enabled": False,
                "pending_events": 0,
                "dm_enabled": False,
                "dm_error": None,
                "dm_pending": 0,
                "dm_pending_ids": [],
                "dm_latest_created_at": 0.0,
            },
            "dynamics": {
                "enabled": False,
                "auto_publish": False,
                "pending_review": 0,
                "pending_ids": [],
                "pending_latest_created_at": 0.0,
                "uncertain": 0,
                "error": None,
            },
            "writes": {"uncertain_count": 0, "uncertain_ids": []},
            "hot_topics": select_video_candidates(None, store=db_store, limit=hot_limit),
        }

    ops_policy = raw.operations.policy() if hasattr(raw, "operations") else {}
    discovery_obj = getattr(raw, "discovery", None)
    scoped_account = str(discovery_obj.history_scope()[0] or "") if discovery_obj is not None else ""
    account_id = str(getattr(raw, "account", None) or ops_policy.get("account_id") or scoped_account or "")
    character_id = str((getattr(getattr(raw, "runtime", None), "character", None) or {}).get("id") or "")
    control_state = raw.control_state(ops_policy) if hasattr(raw, "control_state") else "off"
    disc_state = discovery_obj.state if discovery_obj is not None else "off"
    disc_enabled = bool(getattr(discovery_obj, "enabled", False))
    disc_error = getattr(raw, "last_error", None) or getattr(discovery_obj, "error", None)
    disc_attention = getattr(discovery_obj, "attention", None)
    read_gate = getattr(raw, "read_gate", None)
    disc_cooldown = None
    if read_gate is not None:
        gate_info = read_gate.status(account_id).get("discovery_backoff") or {}
        disc_cooldown = gate_info.get("until")

    inbox_info = raw.inbox_status() if hasattr(raw, "inbox_status") else {"enabled": False, "state": "off", "pending_events": 0, "dm_enabled": False}
    messages_svc = getattr(raw, "messages", None)
    dm_error = getattr(messages_svc, "error", None)
    dm_pending = 0
    dm_pending_ids: list[str] = []
    dm_latest_created_at = 0.0
    if db_store is not None and _facade_table_exists(db_store, "dm_drafts"):
        dm_rows = db_store.all(
            "SELECT id, created_at FROM dm_drafts "
            "WHERE status IN ('pending_review','queued','publish_uncertain') "
            "ORDER BY created_at DESC, id DESC"
        )
        dm_pending = len(dm_rows)
        dm_pending_ids = [str(row["id"]) for row in dm_rows]
        if dm_rows:
            dm_latest_created_at = max(float(row["created_at"] or 0.0) for row in dm_rows)

    dynamics_svc = getattr(raw, "dynamics", None)
    dyn_policy = dynamics_svc.policy() if dynamics_svc is not None and hasattr(dynamics_svc, "policy") else {}
    dyn_error = getattr(dynamics_svc, "error", None)
    dyn_pending = dyn_uncertain = 0
    dyn_pending_ids: list[str] = []
    dyn_pending_latest_created_at = 0.0
    if db_store is not None and _facade_table_exists(db_store, "dynamic_posts"):
        p_rows = db_store.all(
            "SELECT id, created_at FROM dynamic_posts "
            "WHERE status IN ('pending_review','approved') "
            "ORDER BY created_at DESC, id DESC"
        )
        u_row = db_store.one("SELECT COUNT(*) AS n FROM dynamic_posts WHERE status='publish_uncertain'")
        dyn_pending = len(p_rows)
        dyn_pending_ids = [str(row["id"]) for row in p_rows]
        if p_rows:
            dyn_pending_latest_created_at = max(float(row["created_at"] or 0.0) for row in p_rows)
        dyn_uncertain = int(u_row["n"]) if u_row else 0

    uncertain_writes = 0
    uncertain_ids: list[str] = []
    if db_store is not None and _facade_table_exists(db_store, "operation_write_attempts"):
        u_rows = db_store.all(
            "SELECT token FROM operation_write_attempts WHERE resolved_status='uncertain' "
            "ORDER BY started_at DESC, token DESC"
        )
        uncertain_writes = len(u_rows)
        uncertain_ids = [str(row["token"]) for row in u_rows]

    acct_svc = getattr(raw, "bilibili_account", None)
    creds_ok = bool(getattr(acct_svc, "credentials_configured", False))
    acct_status = acct_svc.status() if (acct_svc is not None and callable(getattr(acct_svc, "status", None))) else {}
    acct_error: str | None = None
    if isinstance(acct_status, dict):
        raw_err = acct_status.get("error")
        if isinstance(raw_err, str) and raw_err.strip():
            acct_error = raw_err.strip()
        elif acct_status.get("state") in {"expired", "restricted", "error"}:
            acct_error = f"B站账号状态异常：{acct_status['state']}"
    if not acct_error and acct_svc is not None:
        raw_attr = getattr(acct_svc, "account_error", None) or getattr(acct_svc, "error", None)
        if isinstance(raw_attr, str) and raw_attr.strip():
            acct_error = raw_attr.strip()
    blockers = list(getattr(raw, "blockers", ()) or ())
    return {
        "account_id": account_id,
        "character_id": character_id,
        "control_state": control_state,
        "automatic_ready": bool(creds_ok and not blockers and not uncertain_writes),
        "account_error": acct_error,
        "credentials_configured": creds_ok,
        "blockers": blockers,
        "discovery": {
            "state": disc_state,
            "enabled": disc_enabled,
            "error": disc_error,
            "attention": [disc_attention] if disc_attention else [],
            "read_cooldown_until": disc_cooldown,
        },
        "inbox": {
            "state": inbox_info.get("state", "off"),
            "enabled": bool(inbox_info.get("enabled")),
            "pending_events": int(inbox_info.get("pending_events") or 0),
            "dm_enabled": bool(inbox_info.get("dm_enabled")),
            "dm_error": dm_error,
            "dm_pending": dm_pending,
            "dm_pending_ids": dm_pending_ids,
            "dm_latest_created_at": dm_latest_created_at,
        },
        "dynamics": {
            "enabled": bool(dyn_policy.get("enabled", False)),
            "auto_publish": bool(dyn_policy.get("auto_publish", False)),
            "pending_review": dyn_pending,
            "pending_ids": dyn_pending_ids,
            "pending_latest_created_at": dyn_pending_latest_created_at,
            "uncertain": dyn_uncertain,
            "error": dyn_error,
        },
        "writes": {
            "uncertain_count": uncertain_writes,
            "uncertain_ids": uncertain_ids,
        },
        "hot_topics": select_video_candidates(raw, store=db_store, limit=hot_limit),
    }


__all__ = [
    "BrowsingPolicy",
    "COMMENT_ID",
    "CORRECTED_VIDEOS",
    "FeedbackPublication",
    "MessagePolicy",
    "OperationsFacade",
    "VIDEO_ID",
    "acknowledge_issues",
    "activity",
    "browsing_policy",
    "browsing_round",
    "browsing_status",
    "browsing_visual_provider",
    "business_timezone",
    "canonical_intent",
    "clear_issues",
    "comment_context_scope",
    "comment_draft_source",
    "comment_href",
    "comment_payload",
    "comment_publication_binding",
    "comment_thread",
    "comments_display_account_id",
    "configure_browsing",
    "configure_messages",
    "control_policy",
    "control_starting",
    "corrected_caption",
    "create_feedback_publication",
    "create_operator_dynamic_draft",
    "day_window",
    "decode_target_json",
    "dm_draft_source",
    "dm_message_source",
    "dm_outgoing_payload",
    "dm_publication_binding",
    "dm_reply_window",
    "dynamic_character",
    "dynamic_outgoing_payload",
    "dynamic_publication_binding",
    "dynamic_publication_images",
    "dynamic_sources",
    "dynamic_valid_sources",
    "ensure_comment_timeline",
    "find_dynamic_edit",
    "find_write_attempt_by_intent",
    "finish_write_attempt",
    "full_video_status",
    "get_attempt",
    "get_dynamic_edit",
    "get_dynamic_post",
    "heartbeat_pulse_snapshot",
    "history",
    "history_scope",
    "illustration_for_row",
    "issue_history",
    "material_excerpt_state",
    "operation_status_snapshot",
    "propose_dynamic_edit",
    "queue_full_video",
    "read_video_metadata",
    "rebuild_comment_context",
    "reconcile_attempt",
    "reconcile_channel_draft",
    "reconcile_dynamic_post",
    "requested_preview_row",
    "reserve_write_attempt",
    "resource_key",
    "restore_history_scope",
    "run_preview_browse",
    "select_video_candidates",
    "set_browsing_enabled",
    "set_dynamics_policy",
    "stop_browsing",
    "update_full_video",
    "video_clip_adapter",
]
