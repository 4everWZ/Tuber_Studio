"""Verify comment memories against their original account, floor and receipt."""

import hashlib
import json
import re


def comment_scope(scope):
    if not isinstance(scope, str) or not scope.startswith("comments:"):
        return None
    try:
        parts = json.loads(scope.removeprefix("comments:"))
    except (TypeError, ValueError):
        return None
    return tuple(parts) if isinstance(parts, list) and len(parts) == 4 and all(
        isinstance(value, str) for value in parts
    ) else None


def reply_receipt(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and value.get("code", 0) == 0 and re.fullmatch(
        r"[1-9][0-9]{0,19}", str(value.get("rpid", ""))
    ):
        return value
    return None


def comment_delivery(store, scope, character, event_id, text=None):
    parts = comment_scope(scope)
    match = re.fullmatch(r"comment:([1-9][0-9]*)", str(event_id))
    if not parts or not parts[0] or not match:
        return None
    account, platform, video, floor = parts
    row = store.one(
        "SELECT d.text,d.receipt FROM drafts d JOIN comments c ON c.id=d.comment_id "
        "WHERE c.id=? AND c.platform=? AND c.video_id=? AND c.thread_id=? "
        "AND d.account_id=? AND d.character_id=? AND d.character_version=? AND d.status='published'",
        (int(match[1]), platform, video, floor, account, character["id"], character["version"]),
    )
    receipt = reply_receipt(row["receipt"]) if row else None
    if not receipt or text is not None and row["text"] != text:
        return None
    return {"event_id": event_id, "rpid": str(receipt["rpid"]),
            "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest()}


def memory_source_allowed(store, row, *, account_id=None):
    if row.get("role") != "assistant" or not row.get("scope", "").startswith("comments:"):
        return True
    parts = comment_scope(row["scope"])
    if account_id is not None and (not parts or parts[0] != str(account_id)):
        return False
    return comment_delivery(store, row["scope"],
        {"id": row["character_id"], "version": row["character_version"]},
        row["event_id"], row["text"]) is not None
