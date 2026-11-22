"""Unified Inbox event normalization (`comment_reply`, `mention`, `own_content_comment`, `dm`) and views."""

import json
import time
from dataclasses import dataclass, field

from ..kernel.feedback import generation_view

INBOUND_KIND_TO_PIPELINE_KIND = {
    "reply": "comment_reply",
    "comment_reply": "comment_reply",
    "mention": "mention",
    "own_comment": "own_content_comment",
    "own_dynamic": "own_content_comment",
    "own_content_comment": "own_content_comment",
    "dm": "dm",
    "message": "dm",
}

INBOUND_PRIORITY = {
    "dm": 0,
    "comment_reply": 1,
    "mention": 2,
    "own_content_comment": 3,
}


@dataclass(frozen=True)
class InboundMessage:
    """Normalized representation of an inbound message or comment event handled by Pipeline B."""

    kind: str
    raw_kind: str
    event_id: str
    source_key: str
    account_id: str
    character_id: str
    author_uid: str
    author_name: str
    text: str
    timestamp: float
    resource_type: str = ""
    resource_id: str = ""
    bvid: str = ""
    oid: str = ""
    root_id: str = ""
    comment_id: str = ""
    conversation_id: str = ""
    status: str = ""
    payload: dict = field(default_factory=dict)


def _json_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_inbound_event(row: dict, *, kind_hint: str | None = None, character_id: str = "") -> InboundMessage:
    """Normalize a `browsing_events` or `dm_inbox` row into an `InboundMessage`."""
    raw_kind = str(kind_hint or row.get("kind") or "reply")
    normalized_kind = INBOUND_KIND_TO_PIPELINE_KIND.get(raw_kind, raw_kind)
    payload = _json_dict(row.get("payload"))
    if normalized_kind == "dm":
        account_id = str(row.get("account_id") or "")
        uid = str(row.get("platform_user_id") or payload.get("platform_user_id") or "")
        return InboundMessage(
            kind="dm",
            raw_kind=raw_kind,
            event_id=str(row.get("id") or ""),
            source_key=str(row.get("id") or ""),
            account_id=account_id,
            character_id=str(character_id or row.get("character_id") or ""),
            author_uid=uid,
            author_name=str(row.get("display_name") or ""),
            text=str(row.get("content") or ""),
            timestamp=float(row.get("sent_at") or row.get("received_at") or 0),
            resource_type="dm",
            resource_id=uid,
            conversation_id=f"{account_id}:{character_id or row.get('character_id') or ''}:{uid}",
            status=str(row.get("status") or ""),
            payload=payload or dict(row),
        )
    resource_type = str(payload.get("resource_type") or ("video" if payload.get("bvid") else ""))
    bvid = str(payload.get("bvid") or "")
    oid = str(payload.get("oid") or "")
    comment_id = str(payload.get("comment_id") or payload.get("parent") or "")
    root_id = str(payload.get("root") or payload.get("thread_id") or comment_id)
    resource_id = bvid or oid
    return InboundMessage(
        kind=normalized_kind,
        raw_kind=raw_kind,
        event_id=str(row.get("id") or payload.get("event_id") or ""),
        source_key=str(row.get("source_key") or comment_id),
        account_id=str(row.get("account_id") or payload.get("account_id") or ""),
        character_id=str(character_id or payload.get("character_id") or ""),
        author_uid=str(payload.get("sender_uid") or payload.get("platform_user_id") or ""),
        author_name=str(payload.get("sender_name") or payload.get("display_name") or ""),
        text=str(payload.get("source_text") or payload.get("content") or ""),
        timestamp=float(row.get("timestamp") or row.get("created_at") or 0),
        resource_type=resource_type,
        resource_id=resource_id,
        bvid=bvid,
        oid=oid,
        root_id=root_id,
        comment_id=comment_id,
        status=str(row.get("status") or ""),
        payload=payload,
    )


def inbox_priority_key(message: InboundMessage) -> tuple:
    """Sort key prioritizing Inbox direct interactions before older low-priority notices."""
    return (INBOUND_PRIORITY.get(message.kind, 9), float(message.timestamp or 0), message.event_id)


def inbox(browsing) -> dict:
    """Read-only projection of notification/own-content events and cursors (isolated from DM text)."""
    rows = [
        browsing.repo.decode(item)
        for item in browsing.store.all("SELECT * FROM browsing_events ORDER BY timestamp DESC LIMIT 100")
    ]
    account = getattr(browsing, "account", None) or (
        browsing.discovery.history_scope()[0] if getattr(browsing, "discovery", None) else ""
    )
    character = str((getattr(browsing.runtime, "character", None) or {}).get("id") or "")
    items = []
    for row in rows:
        saved = {}
        if row.get("error"):
            try:
                saved = json.loads(row["error"])
            except (ValueError, TypeError):
                pass
            if isinstance(saved, dict):
                row["error"] = saved.get("message", row["error"])
                row["next_at"] = saved.get("next_at")
                row["occurrences"] = saved.get("occurrences", 0)
            else:
                saved = {}
        if row["status"] == "checkpoint":
            row["continuation"] = generation_view(
                browsing.runtime, saved.get("generation_continuation_id"), "browsing", "event:" + row["id"]
            )
            row["error"] = saved.get("message")
        row["inbound_kind"] = INBOUND_KIND_TO_PIPELINE_KIND.get(row.get("kind", ""), row.get("kind", ""))
        row["source_pipeline"] = "discovery" if row.get("kind") == "comment_visit" else "inbox"
        items.append(row)
    cursors = browsing.store.all(
        "SELECT * FROM browsing_cursors" + (" WHERE account_id=?" if account else "") + " ORDER BY kind",
        (account,) if account else (),
    )
    dm_count = browsing.store.one("SELECT COUNT(*) AS n FROM dm_inbox")["n"]
    return {
        "items": items,
        "cursors": [browsing.repo.decode(c) for c in cursors],
        "messages": dm_count,
        "private_count": dm_count,
        "private_href": "#messages",
        "account_id": account,
        "character_id": character,
        "updated_at": time.time(),
    }
