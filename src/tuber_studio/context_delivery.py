"""Verify delivered operation history against stable sources and platform receipts."""

import hashlib
import json
import re

from .compaction import is_summary_message

DELIVERY_SCOPES = ("comments:", "dm:")


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


def comment_delivery(store, scope, character, event_id, text=None, *, legacy=False):
    parts = comment_scope(scope)
    if legacy and parts is None:
        try:
            old = json.loads(scope.removeprefix("comments:"))
        except (TypeError, ValueError):
            old = None
        if isinstance(old, list) and len(old) == 3 and all(isinstance(value, str) for value in old):
            parts = (None, *old)
    match = re.fullmatch(r"comment:([1-9][0-9]*)", str(event_id))
    if not parts or parts[0] == "" or not match:
        return None
    account, platform, video, floor = parts
    row = store.one(
        "SELECT d.text,d.receipt FROM drafts d JOIN comments c ON c.id=d.comment_id "
        "WHERE c.id=? AND c.platform=? AND c.video_id=? AND c.thread_id=? "
        "AND ((? IS NULL AND d.account_id<>'') OR d.account_id=?) "
        "AND d.character_id=? AND d.character_version=? AND d.status='published'",
        (int(match[1]), platform, video, floor, account, account, character["id"], character["version"]),
    )
    receipt = reply_receipt(row["receipt"]) if row else None
    if not receipt or text is not None and row["text"] != text:
        return None
    return {"event_id": event_id, "rpid": str(receipt["rpid"]),
            "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest()}


def dm_delivery(store, scope, character, event_id, text=None):
    match = re.fullmatch(r"dm:([1-9][0-9]*)", str(event_id))
    if not match:
        return None
    row = store.one("SELECT * FROM dm_drafts WHERE id=? AND character_id=? AND character_version=? "
                    "AND status='published'", (int(match[1]), character["id"], character["version"]))
    if not row:
        return None
    platform, account, recipient = (row.get(k) or "" for k in ("platform", "account_id", "platform_user_id"))
    expected_scope = "dm:" + (json.dumps([platform, account, recipient], separators=(",", ":"))
                              if account else f"{platform}:{recipient}")
    if scope != expected_scope:
        return None
    try:
        receipt = json.loads(row["receipt"] or "null")
    except (TypeError, ValueError):
        return None
    if (not isinstance(receipt, dict) or receipt.get("code", 0) != 0 or not receipt.get("msg_key")
        or str(receipt.get("receiver_id", recipient)) != recipient):
        return None
    contents = [row["text"]]
    if receipt.get("message_kind") == "image" and receipt.get("meme_id") == row.get("meme_id"):
        meme_id = receipt["meme_id"]
        asset = store.one("SELECT title FROM meme_assets WHERE asset_id=?", (meme_id,)) if store.one(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='meme_assets'") else None
        contents = [f"[发送表情包：{value}]" for value in (meme_id, (asset or {}).get("title")) if value]
    if text is not None and text not in contents:
        return None
    return {"event_id": event_id, "msg_key": str(receipt["msg_key"]),
            "text_sha256": hashlib.sha256(json.dumps(contents, ensure_ascii=False).encode()).hexdigest()}


def operation_delivery(store, scope, character, event_id, text=None, *, legacy=False):
    if scope.startswith("dm:"):
        return dm_delivery(store, scope, character, event_id, text)
    return comment_delivery(store, scope, character, event_id, text, legacy=legacy)


def delivered_history(store, scope, character, history, *, legacy=False):
    if not scope.startswith(DELIVERY_SCOPES):
        return history
    result = []
    for item in history:
        if item.get("role") == "assistant":
            if operation_delivery(store, scope, character, item.get("source_event_id"), item["content"], legacy=legacy):
                result.append(item)
        elif is_summary_message(item):
            provenance = item.get("delivery_provenance") or {}
            if (isinstance(provenance, dict) and provenance.get("version") == 1 and provenance.get("scope") == scope
                and isinstance(provenance.get("sources"), list)
                and all(isinstance(source, dict) and operation_delivery(store, scope, character, source.get("event_id")) == source
                        for source in provenance["sources"])):
                result.append(item)
        else:
            result.append(item)
    return result


def memory_source_allowed(store, row, *, account_id=None):
    if row.get("role") != "assistant" or not row.get("scope", "").startswith(DELIVERY_SCOPES):
        return True
    parts = comment_scope(row["scope"])
    if account_id is not None and (not parts or parts[0] != str(account_id)):
        return False
    return operation_delivery(store, row["scope"],
        {"id": row["character_id"], "version": row["character_version"]},
        row["event_id"], row["text"]) is not None
