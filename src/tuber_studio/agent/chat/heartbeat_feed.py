"""Persist Always-On Heartbeat proactive briefs into the owner chat feed without cross-subpackage cycles."""

from __future__ import annotations

import time
from typing import Any

from ...core.store import dump


def record_heartbeat_pulse(store, ctx, pulse: dict[str, Any]) -> str:
    """Persist one proactive heartbeat brief as an idempotent `assistant_only` owner-chat turn."""
    pulse_id = str(pulse.get("id") or "").strip()
    body = str(pulse.get("brief") or pulse.get("body") or "").strip()
    if not pulse_id or not body:
        return ""

    char = ctx.runtime.character
    char_id = str(pulse.get("character_id") or char.get("id") or "")
    char_version = int(pulse.get("character_version") or char.get("version") or 1)
    created_at = float(pulse.get("created_at") or time.time())
    payload = {
        "account_id": ctx.account(),
        "reply_text": body,
        "reply_format": "markdown",
        "assistant_only": True,
        "memory_intent": "heartbeat_pulse",
        "character_name": char.get("name") or "Aqua",
        "sources": list(pulse.get("sources") or []),
        "heartbeat_pulse": {
            "id": pulse_id,
            "kind": str(pulse.get("kind") or "pulse"),
            "title": str(pulse.get("title") or ""),
            "created_at": created_at,
        },
    }
    store.execute(
        "INSERT OR IGNORE INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
        (pulse_id, char_id, char_version, "", dump(payload), created_at),
    )
    return pulse_id
